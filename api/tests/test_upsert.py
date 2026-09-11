"""§5.5 - the upsert, against the real database (RLS, generated columns, ON CONFLICT).

WHY not a mocked session: every property under test here is a *database* property.
In-chunk dedupe exists because Postgres raises "ON CONFLICT DO UPDATE command cannot
affect row a second time"; the quarantine exists because a chunk that raises is a chunk
that rolls back, taking the cursor with it; `content_changed_at` is a SQL CASE over
`IS DISTINCT FROM`; the placeholders land in a table with FORCED row-level security
that fails *silently* closed (§3). A fake session would prove none of it and would keep
passing while the real statement no longer works.

The four things this file pins, all of them from §5.5:
  1. one poison row is quarantined while the rest of its chunk lands AND the cursor moves;
  2. a duplicate `bx_id` inside one chunk is absorbed, not raised;
  3. `content_changed_at` moves for a data change and stays put for a re-read or for a
     newly attached recording URL (which is per-read volatile and would otherwise rewrite
     every row in the rescan window every hour);
  4. `employees` placeholders appear for unseen `PORTAL_USER_ID`s without resetting the
     rows the employee refresh has already filled, and `refresh_requested` is cleared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.identity import Identity
from app.bitrix.statistic import parse_rows
from app.db.session import control_txn, tenant_txn
from app.services.employees import upsert_viewer
from app.sync.lease import WORKER_ID, Fence, acquire_leases
from app.sync.upsert import upsert_calls
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, portal_sync_snapshot, seed_portal

_START: Final[str] = "2025-08-06T14:08:40+03:00"
_START_UTC: Final[datetime] = datetime(2025, 8, 6, 11, 8, 40, tzinfo=UTC)


# --------------------------------------------------------------------------- fixtures


async def _drop_tenant_rows(portal_id: int) -> None:
    """Delete the RLS-protected rows explicitly, as conftest does and for the same reason.

    Whether a `ON DELETE CASCADE` is exempt from FORCED row-level security is a Postgres
    implementation detail; a suite that relied on it would silently poison the next run
    on a version where it is not.
    """
    async with tenant_txn(portal_id) as session:
        for table in TENANT_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


async def lease_of(portal_id: int) -> Fence:
    """Take the real lease the way `tick()` does - a hand-built Fence would never fence."""
    for fence in await acquire_leases(50, WORKER_ID):
        if fence.portal_id == portal_id:
            return fence
    raise AssertionError(f"acquire_leases did not offer portal {portal_id}")


@pytest_asyncio.fixture()
async def leased(app_engine: AsyncEngine) -> AsyncIterator[tuple[SeededPortal, Fence]]:
    """One seeded, leased tenant - the state `sync_portal` runs in (§5.9)."""
    portal = await seed_portal(backfill_status="running", high_id=0, low_id=None)
    try:
        yield portal, await lease_of(portal.portal_id)
    finally:
        await _drop_tenant_rows(portal.portal_id)
        await delete_portal(portal.member_id)


# ------------------------------------------------------------------------ row builders


def raw_row(bx_id: int, **overrides: Any) -> dict[str, Any]:
    """One `voximplant.statistic.get` row in the wire shape (strings, research note (a))."""
    row: dict[str, Any] = {
        "ID": str(bx_id),
        "CALL_ID": f"b24-{bx_id}@voximplant",
        "CALL_CATEGORY": "external",
        "PORTAL_USER_ID": "42",
        "PORTAL_NUMBER": "+998710000001",
        "PHONE_NUMBER": "+998901234567",
        "CALL_TYPE": "1",
        "CALL_DURATION": "30",
        "CALL_START_DATE": _START,
        "CALL_RECORD_URL": None,
        "CALL_VOTE": None,
        "COST": "0.0000",
        "COST_CURRENCY": "UZS",
        "CALL_FAILED_CODE": "200",
        "CRM_ENTITY_TYPE": "CONTACT",
        "CRM_ENTITY_ID": "275",
        "TRANSCRIPT_PENDING": "N",
        "SESSION_ID": str(3_841_000_000 + bx_id),
        "REDIAL_ATTEMPT": "0",
        "RECORD_FILE_ID": None,
    }
    row.update(overrides)
    return row


def parsed(*raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse through the real parser so the column names are never guessed here."""
    outcome = parse_rows(list(raw))
    assert not outcome.rejected, f"the builder produced an unparsable row: {outcome.rejected}"
    return outcome.rows


async def call_rows(portal_id: int, columns: str = "*") -> list[dict[str, Any]]:
    async with tenant_txn(portal_id) as session:
        result = await session.execute(
            text(f"SELECT {columns} FROM calls WHERE portal_id = :pid ORDER BY bx_id"),  # noqa: S608
            {"pid": portal_id},
        )
        return [dict(row) for row in result.mappings().all()]


async def one_call(portal_id: int, bx_id: int) -> dict[str, Any]:
    async with tenant_txn(portal_id) as session:
        result = await session.execute(
            text("SELECT * FROM calls WHERE portal_id = :pid AND bx_id = :bx"),
            {"pid": portal_id, "bx": bx_id},
        )
        return dict(result.mappings().one())


async def employee_rows(portal_id: int) -> dict[int, dict[str, Any]]:
    async with tenant_txn(portal_id) as session:
        result = await session.execute(
            text("SELECT * FROM employees WHERE portal_id = :pid"), {"pid": portal_id}
        )
        return {int(row["bx_user_id"]): dict(row) for row in result.mappings().all()}


async def event_kinds(portal_id: int) -> list[str]:
    async with control_txn() as session:
        result = await session.execute(
            text("SELECT kind FROM portal_events WHERE portal_id = :pid ORDER BY id"),
            {"pid": portal_id},
        )
        return [str(kind) for kind in result.scalars().all()]


def bx_ids(rows: Sequence[dict[str, Any]]) -> list[int]:
    return [int(row["bx_id"]) for row in rows]


# ----------------------------------------------------------------------------- tests


async def test_poison_row_is_quarantined_while_the_chunk_lands_and_the_cursor_moves(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """Decision 11: one unexpected value may cost one row and nothing else (§5.5).

    The poison is a row the *database* refuses (`call_start_date` is NOT NULL), which is
    the shape the SAVEPOINT + row-by-row retry exists for. Whether the implementation
    catches it before the INSERT or after, the observable outcome must be the same:
    two rows stored, one counted as quarantined, and the cursor advanced - because a
    cursor that stops here stops forever, and nobody would be paged for it.
    """
    portal, fence = leased
    rows = parsed(raw_row(10), raw_row(11), raw_row(12))
    rows[1]["call_start_date"] = None  # survives parsing only because we broke it by hand

    result = await upsert_calls(fence, rows, cursor_values={"low_id": 10})

    assert result.quarantined == 1
    assert result.inserted_or_updated == 2
    assert bx_ids(await call_rows(portal.portal_id, "bx_id")) == [10, 12]
    # The poison sits BETWEEN the good ids, so these hold whether the bounds are computed
    # over the requested rows or over the stored ones - what matters is that they exist.
    assert result.min_bx_id == 10
    assert result.max_bx_id == 12

    sync = await portal_sync_snapshot(portal.portal_id)
    assert sync is not None
    assert sync["low_id"] == 10, "the cursor must commit with the rows that survived"


async def test_rejected_rows_are_counted_and_audited(leased: tuple[SeededPortal, Fence]) -> None:
    """§5.5: a parser rejection is a support signal (`rejected_rows` + `row_rejected`)."""
    portal, fence = leased
    before = await portal_sync_snapshot(portal.portal_id)
    assert before is not None

    await upsert_calls(
        fence,
        parsed(raw_row(20)),
        rejected=[(21, "call_start_date is not parsable")],
    )

    after = await portal_sync_snapshot(portal.portal_id)
    assert after is not None
    assert after["rejected_rows"] == before["rejected_rows"] + 1
    assert "row_rejected" in await event_kinds(portal.portal_id)


async def test_duplicate_bx_id_inside_one_chunk_does_not_raise(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§5.5: a row deleted between two sub-commands can appear twice in one chunk.

    Postgres answers a repeated conflict target with "ON CONFLICT DO UPDATE command
    cannot affect row a second time" - an exception that would roll back the chunk and
    park the cursor on data Bitrix24 is free to produce at any time.
    """
    portal, fence = leased

    await upsert_calls(fence, parsed(raw_row(30, CALL_DURATION="10"), raw_row(30, CALL_DURATION="99")))

    stored = await call_rows(portal.portal_id, "bx_id, call_duration")
    assert bx_ids(stored) == [30]
    assert stored[0]["call_duration"] in (10, 99)


async def test_content_changed_at_ignores_a_re_read_and_a_new_recording_url(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§5.5 / decision 21: only a real data change is a content change.

    The rescan re-reads the trailing 72 h every hour. If `call_record_url` counted as
    content, and the URL is per-read volatile (which the spike has not ruled out), every
    row in the window would be rewritten hourly and `content_changed_at` would become
    noise. A changed duration, on the other hand, is exactly what the column is for.
    """
    portal, fence = leased
    record_url = "https://portal.bitrix24.test/download/30.mp3"

    await upsert_calls(fence, parsed(raw_row(40, CALL_DURATION="30")))
    inserted = await one_call(portal.portal_id, 40)
    assert inserted["content_changed_at"] is None
    first_seen = inserted["first_seen_at"]

    await upsert_calls(fence, parsed(raw_row(40, CALL_DURATION="30")))
    unchanged = await one_call(portal.portal_id, 40)
    assert unchanged["content_changed_at"] is None, "a re-read is not a change"
    assert unchanged["last_synced_at"] > inserted["last_synced_at"], "the row was re-read"
    assert unchanged["first_seen_at"] == first_seen

    await upsert_calls(fence, parsed(raw_row(40, CALL_DURATION="30", CALL_RECORD_URL=record_url)))
    recorded = await one_call(portal.portal_id, 40)
    assert recorded["call_record_url"] == record_url, "the URL is still stored"
    assert recorded["has_record"] is True
    assert recorded["content_changed_at"] is None, "call_record_url is excluded from the compare"

    await upsert_calls(fence, parsed(raw_row(40, CALL_DURATION="31", CALL_RECORD_URL=record_url)))
    changed = await one_call(portal.portal_id, 40)
    assert changed["call_duration"] == 31
    assert changed["content_changed_at"] is not None, "a changed duration IS a content change"


async def test_placeholders_appear_for_unseen_users_without_clobbering_cached_ones(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§7 writer (a): the refresh job finds its work through `employees`, not `calls`.

    Re-inserting a placeholder over an already-fetched employee would set `fetched_at`
    back to NULL and make `employees_refresh` re-fetch the whole portal on every visit -
    which is why the insert is ON CONFLICT DO NOTHING.
    """
    portal, fence = leased
    async with tenant_txn(portal.portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (portal_id, bx_user_id, name, active, found, fetched_at)
                VALUES (:pid, 888, 'Cached', true, true, now())
                """
            ),
            {"pid": portal.portal_id},
        )

    await upsert_calls(
        fence,
        parsed(raw_row(50, PORTAL_USER_ID="777"), raw_row(51, PORTAL_USER_ID="888")),
    )

    employees = await employee_rows(portal.portal_id)
    assert 777 in employees, "an unseen PORTAL_USER_ID must become a placeholder"
    assert employees[777]["fetched_at"] is None
    assert employees[888]["name"] == "Cached"
    assert employees[888]["fetched_at"] is not None, "a cached employee was reset to a placeholder"


async def test_the_viewer_upsert_leaves_what_only_the_refresher_can_know(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§7 writer (b): `/app/` writes the viewer from `user.current`, and stops there.

    Three columns are deliberately absent from that write, and all three for one reason:
    `user.current` is not documented to report them, and a field it omits would be written
    as NULL over the value `employees_refresh` learned from `user.get`. `active` would
    resurrect a dismissed employee, `departments` would erase the department list, and
    `phone_inner` would take the extension out of the employee filter - on every single
    app open, for the one person most likely to notice: the viewer themselves.

    The failure this pins is not a crash. Adding the missing keys to `upsert_viewer` looks
    like completing an unfinished function, and everything keeps working except that three
    columns quietly empty themselves in production.
    """
    portal, _ = leased
    async with tenant_txn(portal.portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (portal_id, bx_user_id, name, last_name, phone_inner,
                                       active, departments, found, fetched_at)
                VALUES (:pid, 100, 'Ada', 'Admin', '101', false, '{7}', true, now())
                """
            ),
            {"pid": portal.portal_id},
        )

    await upsert_viewer(
        portal.portal_id,
        Identity(
            user_id=100,
            is_admin=True,
            timezone="Asia/Tashkent",
            name="Ada",
            last_name="Admin",
            second_name=None,
            work_position="Head of Sales",
            photo_url=None,
        ),
    )

    row = (await employee_rows(portal.portal_id))[100]
    assert row["work_position"] == "Head of Sales", (
        "the columns `user.current` does report must still be refreshed on open"
    )
    assert row["phone_inner"] == "101", (
        "the viewer upsert erased the internal extension. `user.current` does not report "
        "UF_PHONE_INNER, so writing it here empties the employee filter one open at a time."
    )
    assert row["active"] is False, "a dismissed employee was resurrected by an app open"
    assert list(row["departments"]) == [7], "the department list was erased by an app open"


async def test_refresh_requested_is_cleared_by_the_upsert(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§5.7 step 3: the flag is the SPA's request, and the re-read is the answer."""
    portal, fence = leased
    await upsert_calls(fence, parsed(raw_row(60)))
    async with tenant_txn(portal.portal_id) as session:
        await session.execute(
            text("UPDATE calls SET refresh_requested = true WHERE portal_id = :pid AND bx_id = 60"),
            {"pid": portal.portal_id},
        )

    await upsert_calls(fence, parsed(raw_row(60, CALL_RECORD_URL="https://p.test/60.mp3")))

    assert (await one_call(portal.portal_id, 60))["refresh_requested"] is False


async def test_an_empty_chunk_still_commits_the_cursor(leased: tuple[SeededPortal, Fence]) -> None:
    """§5.3: "zero rows" is the backfill's termination condition, not an error path."""
    portal, fence = leased

    result = await upsert_calls(fence, [], cursor_values={"backfill_status": "done"})

    assert result.inserted_or_updated == 0
    assert result.max_bx_id is None
    assert result.min_bx_id is None
    sync = await portal_sync_snapshot(portal.portal_id)
    assert sync is not None and sync["backfill_status"] == "done"


async def test_rows_are_stored_under_the_tenant_of_the_fence(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§3: the write happens inside `tenant_txn`, so the row carries that portal_id.

    A row written with the wrong `portal_id` would be rejected by the policy's WITH CHECK
    rather than silently misfiled - this asserts the happy path of that guarantee, since
    every later read (calls_repo, dashboard) trusts it completely.
    """
    portal, fence = leased
    await upsert_calls(fence, parsed(raw_row(70)))

    stored = await one_call(portal.portal_id, 70)
    assert stored["portal_id"] == portal.portal_id
    assert stored["call_start_date"] == _START_UTC
    assert stored["result_group"] == "answered"  # generated from call_failed_code 200


@pytest.mark.parametrize("cursor", [{"high_id": 123}, {"low_id": 45, "backfill_done": 2}])
async def test_cursor_values_land_with_the_rows(
    leased: tuple[SeededPortal, Fence], cursor: dict[str, Any]
) -> None:
    """§5.3: rows and the cursor commit together, so a crash costs one re-upserted batch."""
    portal, fence = leased

    await upsert_calls(fence, parsed(raw_row(80), raw_row(81)), cursor_values=cursor)

    sync = await portal_sync_snapshot(portal.portal_id)
    assert sync is not None
    for column, value in cursor.items():
        assert sync[column] == value
    assert bx_ids(await call_rows(portal.portal_id, "bx_id")) == [80, 81]
