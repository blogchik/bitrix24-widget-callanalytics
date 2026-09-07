"""Decision 13 - the worker's writes are fenced, against the real database.

WHY this is the most safety-critical file of the milestone: the fence is the only thing
standing between a stale runner and the two accidents that leave no trace. A worker whose
process froze past its lease (or whose portal was uninstalled and purged mid-run) is still
holding rows in memory and a cursor it believes in; without the fence it would re-insert a
purged tenant's call records - a data-retention breach that looks exactly like normal sync
traffic - or overwrite a fresher runner's cursor and silently skip a range of history.

The fence is one `UPDATE portal_sync ... WHERE lease_owner=:me AND lease_expires_at>now()
AND sync_generation=:gen` in the SAME transaction as the rows, so "zero rows updated" is
the abort signal. Each test below breaks exactly one of those three predicates and asserts
both halves of the guarantee: `FenceLost` is raised, and **nothing changed**.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.statistic import parse_rows
from app.db.session import control_txn, tenant_txn
from app.services.portals import mark_uninstalled
from app.sync.lease import (
    WORKER_ID,
    Fence,
    FenceLost,
    acquire_leases,
    fenced_update,
    heartbeat,
    release_lease,
)
from app.sync.upsert import upsert_calls
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, portal_sync_snapshot, seed_portal

_START = "2025-08-06T14:08:40+03:00"


# --------------------------------------------------------------------------- helpers


async def _drop_tenant_rows(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for table in TENANT_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


async def lease_of(portal_id: int, owner: str = WORKER_ID) -> Fence:
    for fence in await acquire_leases(50, owner):
        if fence.portal_id == portal_id:
            return fence
    raise AssertionError(f"acquire_leases did not offer portal {portal_id}")


async def sync_row(portal_id: int) -> dict[str, Any]:
    snapshot = await portal_sync_snapshot(portal_id)
    assert snapshot is not None
    return snapshot


async def set_sync(portal_id: int, assignments: str, **binds: Any) -> None:
    """Move `portal_sync` behind the runner's back - what a crash or an uninstall does."""
    async with control_txn() as session:
        await session.execute(
            text(f"UPDATE portal_sync SET {assignments} WHERE portal_id = :pid"),  # noqa: S608
            {"pid": portal_id, **binds},
        )


async def call_count(portal_id: int) -> int:
    async with tenant_txn(portal_id) as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM calls WHERE portal_id = :pid"), {"pid": portal_id}
                )
            ).scalar_one()
        )


def rows(*bx_ids: int) -> list[dict[str, Any]]:
    outcome = parse_rows(
        [
            {
                "ID": str(bx_id),
                "CALL_ID": f"b24-{bx_id}",
                "PORTAL_USER_ID": "42",
                "PHONE_NUMBER": "+998901234567",
                "CALL_TYPE": "1",
                "CALL_DURATION": "30",
                "CALL_START_DATE": _START,
                "CALL_FAILED_CODE": "200",
            }
            for bx_id in bx_ids
        ]
    )
    assert not outcome.rejected
    return outcome.rows


@pytest_asyncio.fixture()
async def leased(app_engine: AsyncEngine) -> AsyncIterator[tuple[SeededPortal, Fence]]:
    portal = await seed_portal(backfill_status="running", high_id=100)
    try:
        yield portal, await lease_of(portal.portal_id)
    finally:
        await _drop_tenant_rows(portal.portal_id)
        await delete_portal(portal.member_id)


@pytest_asyncio.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal(backfill_status="running", high_id=100)
    try:
        yield seeded
    finally:
        await _drop_tenant_rows(seeded.portal_id)
        await delete_portal(seeded.member_id)


# ------------------------------------------------------- the three broken predicates


async def test_a_stale_lease_owner_cannot_write(leased: tuple[SeededPortal, Fence]) -> None:
    """Another worker holds the lease now; this run is a ghost (§5.9, decision 13)."""
    seeded, fence = leased
    intruder = Fence(portal_id=fence.portal_id, owner="other-host:999", generation=fence.generation)
    before = await sync_row(seeded.portal_id)

    with pytest.raises(FenceLost):
        async with tenant_txn(seeded.portal_id) as session:
            await fenced_update(session, intruder, {"high_id": 999})

    with pytest.raises(FenceLost):
        await upsert_calls(intruder, rows(1, 2), cursor_values={"high_id": 999})

    after = await sync_row(seeded.portal_id)
    assert after["high_id"] == before["high_id"] == 100
    assert await call_count(seeded.portal_id) == 0, "a ghost runner inserted rows"


async def test_an_expired_lease_cannot_write(leased: tuple[SeededPortal, Fence]) -> None:
    """The 5-minute lease expired while the run was still in flight (§5.1).

    All REST calls carry a 120 s timeout precisely so this stays rare - but "rare" is not
    "impossible", and the expired runner must lose the race rather than resolve it.
    """
    seeded, fence = leased
    await set_sync(seeded.portal_id, "lease_expires_at = now() - interval '1 minute'")

    with pytest.raises(FenceLost):
        await upsert_calls(fence, rows(3), cursor_values={"high_id": 3})

    assert (await sync_row(seeded.portal_id))["high_id"] == 100
    assert await call_count(seeded.portal_id) == 0


async def test_a_bumped_generation_cannot_write(leased: tuple[SeededPortal, Fence]) -> None:
    """Install/reinstall/uninstall bump `sync_generation`; the old run is void (§3)."""
    seeded, fence = leased
    await set_sync(seeded.portal_id, "sync_generation = sync_generation + 1")

    with pytest.raises(FenceLost):
        await upsert_calls(fence, rows(4), cursor_values={"low_id": 4})

    after = await sync_row(seeded.portal_id)
    assert after["low_id"] is None
    assert await call_count(seeded.portal_id) == 0


async def test_an_uninstall_stops_an_in_flight_run_from_re_inserting_after_the_purge(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """Decision 13 / §4.9 rule 3 - the accident the fence exists for.

    Sequence: the run is mid-visit with rows already fetched from Bitrix24; the uninstall
    webhook arrives, wipes the tokens, bumps `sync_generation` and flags the purge; the
    purge deletes the tenant's rows. The run then tries to write the rows it fetched
    BEFORE any of that. If that write landed, the portal's call records would be back in
    the database after we told Bitrix24 (and the customer) they were gone, and no later
    purge would ever be scheduled to remove them.
    """
    seeded, fence = leased
    await upsert_calls(fence, rows(5, 6), cursor_values={"high_id": 6})
    assert await call_count(seeded.portal_id) == 2

    async with control_txn() as session:
        assert await mark_uninstalled(session, seeded.portal_id) is True
    await _drop_tenant_rows(seeded.portal_id)  # stands in for purge_portal (§5.9)
    assert await call_count(seeded.portal_id) == 0

    with pytest.raises(FenceLost):
        await upsert_calls(fence, rows(7, 8), cursor_values={"high_id": 8})

    assert await call_count(seeded.portal_id) == 0, "rows returned after the purge"


# -------------------------------------------------------------------- lease handout


async def test_acquire_leases_never_hands_one_portal_to_two_workers(portal: SeededPortal) -> None:
    """`FOR UPDATE SKIP LOCKED` (§5.9): two workers, one portal, one winner.

    Two runners on the same portal would double every REST request against a shared
    2 req/s bucket and race each other's cursor - and because both writes carry a valid
    lease predicate for their own owner, nothing downstream would notice.
    """
    first, second = await asyncio.gather(
        acquire_leases(50, "worker-a:1"),
        acquire_leases(50, "worker-b:2"),
    )
    holders = [
        owner
        for owner, fences in (("worker-a:1", first), ("worker-b:2", second))
        if any(fence.portal_id == portal.portal_id for fence in fences)
    ]
    assert len(holders) == 1, f"the portal was leased by {holders}"

    # And a third attempt while the lease is live must not get it either.
    again = await acquire_leases(50, "worker-c:3")
    assert all(fence.portal_id != portal.portal_id for fence in again)

    row = await sync_row(portal.portal_id)
    assert row["lease_owner"] == holders[0]
    assert row["lease_expires_at"] is not None


async def test_an_expired_lease_without_a_started_run_is_a_dispatch_miss(
    portal: SeededPortal,
) -> None:
    """§5.9 (a): a lease that expired before `sync_portal` began is not a crash.

    `tick()` leases first and dispatches second; a process that dies in between, or a
    semaphore that never let the task start, leaves a lease with `run_started_at IS NULL`.
    Charging that a failure would walk a perfectly healthy portal into the 6 h pause of
    §5.6 after ten restarts, and the settings page would blame the customer's portal.
    """
    await set_sync(
        portal.portal_id,
        "lease_owner = 'dead-worker:1', lease_expires_at = now() - interval '1 minute', "
        "run_started_at = NULL, consecutive_failures = 0, next_run_at = now()",
    )

    fence = await lease_of(portal.portal_id)

    row = await sync_row(portal.portal_id)
    assert row["lease_owner"] == WORKER_ID
    assert fence.owner == WORKER_ID
    assert fence.generation == row["sync_generation"]
    assert row["consecutive_failures"] == 0, "a dispatch miss was charged as a failure"


async def test_heartbeat_extends_the_lease_and_release_gives_it_back(
    leased: tuple[SeededPortal, Fence],
) -> None:
    """§5.9: the lease is extended after every batch and cleared on exit.

    Without the heartbeat a legitimate long backfill visit would outlive its own lease and
    lose the fence mid-run; without the release the portal would sit idle until the lease
    expires and then be re-leased as a suspected crash.
    """
    seeded, fence = leased
    before = await sync_row(seeded.portal_id)

    await heartbeat(fence)
    assert (await sync_row(seeded.portal_id))["lease_expires_at"] >= before["lease_expires_at"]

    await release_lease(fence)
    after = await sync_row(seeded.portal_id)
    assert after["lease_owner"] is None
    assert after["next_run_at"] is not None
