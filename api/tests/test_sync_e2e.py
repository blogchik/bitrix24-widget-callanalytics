"""End-to-end proof of the sync worker: a real portal, the real entry point, real rows.

`test_cursor.py` drives each cursor module in isolation and `test_upsert.py` drives the
writer. Neither proves the thing milestone 1 step 4 actually promises: that
`jobs.definitions.sync_portal` - the function the worker really calls - takes a freshly
installed portal, walks the whole ladder of §5.9 (head fetch, incremental, backfill,
rescan, employees) against a rate-limited API, and leaves correct rows and correct
cursors in PostgreSQL.

The fake here is deliberately stricter than the one in `fixtures/bitrix.py`: it honours
`FILTER`, `SORT`, `ORDER` and `start` per sub-command and reports `result_total` /
`result_next` the way Bitrix24 does, so an off-by-one in the cursor arithmetic shows up
as missing rows rather than as a passing test.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qsl, unquote_plus

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.db.session import control_txn, tenant_txn
from tests.fixtures.bitrix import delete_portal, patch_httpx, seed_portal

pytestmark = pytest.mark.asyncio

PAGE: Final[int] = 50
#: Big enough that the backfill needs several batches and the per-visit budget bites.
TOTAL_CALLS: Final[int] = 640
BASE_TS: Final[datetime] = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# a Bitrix24 that actually filters
# --------------------------------------------------------------------------------------


def _row(bx_id: int) -> dict[str, Any]:
    """One statistics row, serialised the way Bitrix24 really serialises: strings."""
    started = BASE_TS + timedelta(minutes=bx_id)
    user_id = 100 + (bx_id % 3)
    return {
        "ID": str(bx_id),
        "CALL_ID": f"call.{bx_id}",
        "EXTERNAL_CALL_ID": None,
        "CALL_CATEGORY": "external",
        "CALL_TYPE": str(1 + (bx_id % 2)),
        "CALL_START_DATE": started.isoformat().replace("+00:00", "+03:00"),
        "CALL_DURATION": str(bx_id % 300),
        "CALL_FAILED_CODE": "200" if bx_id % 4 else "304",
        "CALL_FAILED_REASON": "",
        "PORTAL_USER_ID": str(user_id),
        "PORTAL_NUMBER": "+998710000000",
        "PHONE_NUMBER": f"+99890{bx_id:07d}",
        "CRM_ENTITY_TYPE": "CONTACT",
        "CRM_ENTITY_ID": str(500 + bx_id % 7),
        "CRM_ACTIVITY_ID": str(9000 + bx_id),
        "COST": "0.0000",
        "COST_CURRENCY": "UZS",
        "CALL_VOTE": None,
        "CALL_RECORD_URL": "",
        "RECORD_FILE_ID": None,
        "RECORD_DURATION": None,
        "REST_APP_ID": None,
        "REST_APP_NAME": None,
        "TRANSCRIPT_ID": None,
        "TRANSCRIPT_PENDING": "N",
        "SESSION_ID": str(700000 + bx_id),
        "REDIAL_ATTEMPT": "0",
        "COMMENT": "",
        "CALL_LOG": None,
    }


class FilteringBitrix:
    """A Bitrix24 REST endpoint whose statistics method respects what it is asked."""

    _CMD_RE: Final = re.compile(r"^cmd\[(?P<key>[^\]]+)\]$")

    def __init__(self, *, total: int = TOTAL_CALLS) -> None:
        self.ids: list[int] = list(range(1, total + 1))
        self.requests: int = 0
        self.statistic_commands: int = 0
        #: Sub-command keys that must fail once, to exercise the contiguous-prefix rule.
        self.fail_offsets: set[int] = set()
        self._failed: set[int] = set()

    # -- transport -----------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode() if request.content else ""
        params = dict(parse_qsl(body, keep_blank_values=True))
        params.update(dict(parse_qsl(request.url.query.decode(), keep_blank_values=True)))

        if "/oauth/token" in path:
            return httpx.Response(200, json=self._token())

        method = path.partition("/rest/")[2].strip("/").removesuffix(".json").lower()
        self.requests += 1
        if method == "batch":
            return self._batch(params)
        return httpx.Response(200, json={"result": self._dispatch(method, params), "time": _time()})

    def _token(self) -> dict[str, Any]:
        return {
            "access_token": "e2e-access",
            "refresh_token": "e2e-refresh",
            "expires_in": 3600,
            "client_endpoint": "https://portal.bitrix24.test/rest/",
            "server_endpoint": "https://oauth.bitrix.info/rest/",
            "member_id": self.member_id,
            "user_id": 100,
            "status": "L",
            "scope": "crm,telephony,placement,user_brief",
        }

    # -- batch ---------------------------------------------------------------------

    def _batch(self, params: dict[str, str]) -> httpx.Response:
        commands: dict[str, str] = {}
        for key, value in params.items():
            found = self._CMD_RE.match(key)
            if found:
                commands[found.group("key")] = value

        results: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        totals: dict[str, Any] = {}
        nexts: dict[str, Any] = {}
        times: dict[str, Any] = {}

        for key, raw in commands.items():
            method, _, query = raw.partition("?")
            cmd_params = dict(parse_qsl(unquote_plus(query), keep_blank_values=True))
            times[key] = _time()
            method = method.strip().lower()

            if method == "voximplant.statistic.get":
                start = int(cmd_params.get("start", 0) or 0)
                if start in self.fail_offsets and start not in self._failed:
                    self._failed.add(start)
                    errors[key] = {
                        "error": "INTERNAL_SERVER_ERROR",
                        "error_description": "scripted failure",
                    }
                    continue
                page, total, nxt = self._statistics(cmd_params)
                results[key] = page
                totals[key] = total
                if nxt is not None:
                    nexts[key] = nxt
            else:
                results[key] = self._dispatch(method, cmd_params)

        return httpx.Response(
            200,
            json={
                "result": {
                    "result": results,
                    "result_error": errors,
                    "result_total": totals,
                    "result_next": nexts,
                    "result_time": times,
                },
                "time": _time(),
            },
        )

    # -- methods -------------------------------------------------------------------

    def _dispatch(self, method: str, params: dict[str, str]) -> Any:
        if method == "voximplant.statistic.get":
            page, _, _ = self._statistics(params)
            return page
        if method == "user.admin":
            return True
        if method == "user.current":
            return {"ID": "100", "NAME": "Ada", "LAST_NAME": "Admin", "TIME_ZONE": "Asia/Tashkent"}
        if method == "app.info":
            return {"INSTALLED": True, "VERSION": 1, "STATUS": "L"}
        if method == "method.get":
            return {"isExisting": True, "isAvailable": True}
        if method == "user.get":
            wanted = _filter_ids(params)
            return [
                {
                    "ID": str(uid),
                    "NAME": f"User{uid}",
                    "LAST_NAME": "Operator",
                    "ACTIVE": True,
                    "WORK_POSITION": "Sales",
                    "UF_DEPARTMENT": [1],
                }
                for uid in wanted
            ]
        return True

    def _statistics(self, params: dict[str, str]) -> tuple[list[dict[str, Any]], int, int | None]:
        """Serve one page honouring the FILTER operators, SORT/ORDER and start."""
        self.statistic_commands += 1
        low, high, exact = _bounds(params)
        ids = [i for i in self.ids if (low is None or i > low) and (high is None or i <= high)]
        if exact is not None:
            ids = [i for i in self.ids if i in exact]

        order = (params.get("ORDER") or params.get("order") or "ASC").upper()
        ids.sort(reverse=order == "DESC")

        total = len(ids)
        start = int(params.get("start", 0) or 0)
        page = ids[start : start + PAGE]
        nxt = start + PAGE if start + PAGE < total else None
        return [_row(i) for i in page], total, nxt


def _time(operating: float = 0.2) -> dict[str, Any]:
    return {
        "start": 0.0,
        "finish": operating,
        "duration": operating,
        "processing": operating,
        "date_start": "2026-09-07T10:00:00+00:00",
        "date_finish": "2026-09-07T10:00:01+00:00",
        "operating_reset_at": 1_800_000_000,
        "operating": operating,
    }


def _bounds(params: dict[str, str]) -> tuple[int | None, int | None, set[int] | None]:
    """Read FILTER[>ID] / FILTER[<ID] / FILTER[<=ID] / FILTER[>=ID] / FILTER[ID][n]."""
    low: int | None = None
    high: int | None = None
    exact: set[int] = set()
    for key, value in params.items():
        found = re.match(r"(?i)^filter\[(?P<op>[<>=]*)ID\](?:\[\d+\])?$", key)
        if not found:
            continue
        op = found.group("op")
        if not value:
            continue
        if op == ">":
            low = int(value)
        elif op == ">=":
            low = int(value) - 1
        elif op == "<":
            high = int(value) - 1
        elif op == "<=":
            high = int(value)
        else:
            exact.add(int(value))
    return low, high, (exact or None)


def _filter_ids(params: dict[str, str]) -> list[int]:
    out: list[int] = []
    for key, value in params.items():
        if re.match(r"(?i)^filter\[@?ID\]", key) and value:
            out.append(int(value))
    return out or [100, 101, 102]


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def portal_and_bitrix(app_engine):  # type: ignore[no-untyped-def]
    """A fresh tenant plus a Bitrix24 that filters.

    Depends on `app_engine` so the shared async engine is disposed between tests: its
    pool holds asyncpg connections bound to the event loop that created them, and reusing
    one across loops fails at teardown with "Event loop is closed".
    """
    fake = FilteringBitrix()
    seeded = await seed_portal(backfill_status="pending", high_id=0, low_id=None)
    fake.member_id = seeded.member_id
    try:
        with patch_httpx(fake):  # type: ignore[arg-type]
            yield seeded, fake
    finally:
        await delete_portal(seeded.member_id)


async def _count_calls(portal_id: int) -> int:
    async with tenant_txn(portal_id) as session:
        return int((await session.execute(text("SELECT count(*) FROM calls"))).scalar_one())


async def _sync_row(portal_id: int) -> dict[str, Any]:
    async with control_txn() as session:
        row = (
            await session.execute(
                text("SELECT * FROM portal_sync WHERE portal_id = :pid"), {"pid": portal_id}
            )
        ).mappings().one()
    return dict(row)


async def _drive(portal_id: int, *, visits: int) -> None:
    """Run the real worker entry point `visits` times, exactly as the tick would.

    The lease is acquired through `acquire_leases`, not forged: `sync_portal` verifies
    that the lease is held by this worker before it writes anything (decision 13), so a
    test that skipped this step would silently exercise nothing at all.
    """
    from app.jobs.definitions import sync_portal
    from app.sync.lease import WORKER_ID, acquire_leases

    for _ in range(visits):
        async with control_txn() as session:
            # Each iteration is "the next visit, SYNC_INTERVAL_SEC later". Backdating
            # `last_incremental_at` is what makes the forward cursor due again; without it
            # the phase is correctly skipped and the visit would be a no-op. The daily
            # timestamps are deliberately left alone so the daily jobs run once, as they
            # would in production, instead of on every visit.
            await session.execute(
                text(
                    "UPDATE portal_sync SET next_run_at = now() - interval '1 second', "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "last_incremental_at = last_incremental_at - make_interval(secs => :iv) "
                    "WHERE portal_id = :pid"
                ),
                {"pid": portal_id, "iv": settings.sync_interval_sec + 60},
            )
        leased = [f for f in await acquire_leases(8, WORKER_ID) if f.portal_id == portal_id]
        assert leased, f"portal {portal_id} was not due for a lease; the visit would be a no-op"
        await sync_portal(portal_id)


# --------------------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------------------


async def test_a_fresh_portal_imports_its_whole_history_and_lands_on_exact_cursors(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """The headline promise of §5.2-§5.3: every call, no gaps, cursors at the extremes."""
    seeded, _fake = portal_and_bitrix

    # Enough visits for head fetch plus a backfill that yields after its per-visit budget.
    await _drive(seeded.portal_id, visits=8)

    assert await _count_calls(seeded.portal_id) == TOTAL_CALLS, (
        "the backfill must reach every historical row; a short count means a cursor "
        "skipped a page (§5.2 contiguous-prefix rule)"
    )
    sync = await _sync_row(seeded.portal_id)
    assert sync["backfill_status"] == "done"
    assert int(sync["high_id"]) == TOTAL_CALLS
    assert int(sync["low_id"]) == 1
    assert int(sync["rejected_rows"]) == 0

    async with tenant_txn(seeded.portal_id) as session:
        ids = [
            int(r[0])
            for r in (await session.execute(text("SELECT bx_id FROM calls ORDER BY bx_id"))).all()
        ]
    assert ids == list(range(1, TOTAL_CALLS + 1)), "no gap and no duplicate is the whole point"


async def test_new_calls_arrive_on_the_next_visit_without_re_reading_history(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """§5.4: the forward cursor picks up new rows and a quiet portal stays cheap."""
    seeded, fake = portal_and_bitrix
    await _drive(seeded.portal_id, visits=8)
    assert await _count_calls(seeded.portal_id) == TOTAL_CALLS

    fake.statistic_commands = 0
    await _drive(seeded.portal_id, visits=1)
    quiet_cost = fake.statistic_commands
    assert quiet_cost <= 2, (
        f"a quiet portal probed with {quiet_cost} statistics commands; §5.4 requires one "
        "probe command, not a packed batch of empty pages"
    )

    fake.ids.extend([TOTAL_CALLS + 1, TOTAL_CALLS + 2])
    await _drive(seeded.portal_id, visits=1)

    assert await _count_calls(seeded.portal_id) == TOTAL_CALLS + 2
    sync = await _sync_row(seeded.portal_id)
    assert int(sync["high_id"]) == TOTAL_CALLS + 2


async def test_a_failing_page_leaves_no_permanent_hole(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """The review blocker: one failed sub-command must not orphan 50 rows forever."""
    seeded, fake = portal_and_bitrix
    # Fail the third page of the first backfill batch, once.
    fake.fail_offsets = {100}

    await _drive(seeded.portal_id, visits=10)

    assert await _count_calls(seeded.portal_id) == TOTAL_CALLS, (
        "the rows behind the failed page were never re-fetched: the cursor advanced past "
        "an errored command instead of stopping at the error-free prefix"
    )


async def test_employees_referenced_by_calls_are_resolved(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """§7: the upsert leaves placeholders and the refresh job fills them in."""
    seeded, _ = portal_and_bitrix
    await _drive(seeded.portal_id, visits=8)

    async with tenant_txn(seeded.portal_id) as session:
        rows = (
            await session.execute(
                text("SELECT bx_user_id, name, fetched_at FROM employees ORDER BY bx_user_id")
            )
        ).mappings().all()

    assert {int(r["bx_user_id"]) for r in rows} == {100, 101, 102}
    assert all(r["fetched_at"] is not None for r in rows), (
        "every placeholder should have been resolved by employees_refresh (§7)"
    )


async def test_the_worker_stays_inside_the_rate_limit_budget(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """§5.6: importing 640 calls must not cost hundreds of HTTP round trips."""
    seeded, fake = portal_and_bitrix
    await _drive(seeded.portal_id, visits=8)

    assert await _count_calls(seeded.portal_id) == TOTAL_CALLS
    pages = (TOTAL_CALLS + PAGE - 1) // PAGE
    assert fake.requests <= pages, (
        f"{fake.requests} HTTP requests for {pages} pages of data - batching is not "
        "working, and a busy portal would exhaust the 2 req/s budget (§5.6)"
    )


async def test_every_exchange_is_logged_without_secrets(
    portal_and_bitrix,  # type: ignore[no-untyped-def]
) -> None:
    """§6: the moderation log records the work, redaction keeps it safe to keep."""
    seeded, _ = portal_and_bitrix
    await _drive(seeded.portal_id, visits=3)

    async with control_txn() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT kind, request::text AS req, response::text AS resp "
                    "FROM rest_log WHERE portal_id = :pid"
                ),
                {"pid": seeded.portal_id},
            )
        ).mappings().all()

    assert rows, "the sync worker wrote no rest_log rows; §6 requires every REST exchange"
    blob = json.dumps([dict(r) for r in rows])
    for secret in ("e2e-access", "e2e-refresh", seeded.access, seeded.refresh):
        assert secret not in blob, f"{secret!r} leaked into rest_log"
