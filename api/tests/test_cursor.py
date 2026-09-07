"""§5.2-§5.4 - the cursor arithmetic, driven by an in-memory Bitrix24 statistics table.

WHY a hand-written fake instead of `tests/fixtures/bitrix.py`: every property here is
about what the NEXT request asks for given what the last one answered. `FakeBitrix`
answers per method name and returns no `result_total` / `result_next`, so it cannot
express "page 3 of a filtered, sorted selection" - which is the entire subject. The fake
below is a real (tiny) statistics table: it honours `FILTER` operators, `SORT`/`ORDER`
and `start`, pages at 50, and reports `total`/`next` exactly as the API documents
(research note (b)). Any implementation that walks it correctly walks a real portal
correctly, and one that guesses offsets is caught here rather than on a customer's data.

The five failures this file exists to prevent, all of them silent:
  * a batch whose command k failed advancing the cursor past k's page - a permanent
    50-row hole neither cursor ever revisits (decision 10, the hole the review found);
  * a crash between `head_fetch`'s two steps wedging the portal in a state where neither
    head_fetch nor backfill runs (decision 12);
  * a quiet portal paying 50 commands of shared operating time every 5 minutes (§5.4);
  * a backfill starving every other tenant of the global concurrency budget (§5.3);
  * a build that ignores `>ID` turning the worker into a hot loop against a rate-limited
    API shared by every tenant (§5.4, decision 25).

Every assertion is on the `portal_sync` row the visit committed and on the requests the
fake actually received - never on a stage's return value - so a refactor of the outcome
dataclasses cannot quietly turn a cursor bug into a green suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.errors import BitrixError, TransportError, UnknownBitrixError
from app.bitrix.statistic import PAGE_SIZE, STATISTIC_METHOD
from app.config import settings
from app.db.session import control_txn, tenant_txn
from app.sync.backfill import run_backfill
from app.sync.fetch import fetch_pages
from app.sync.head_fetch import run_head_fetch
from app.sync.incremental import run_incremental
from app.sync.lease import WORKER_ID, Fence, acquire_leases
from app.sync.rescan import run_id_window_rescan
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, portal_sync_snapshot, seed_portal

_BASE_START: Final[datetime] = datetime(2025, 8, 6, 11, 8, 40, tzinfo=UTC)


def is_parked(when: datetime) -> bool:
    """True when `next_run_at` is the `'infinity'` of §5.4's terminal stop.

    asyncpg renders an infinite `timestamptz` as the NAIVE `datetime.max`, so a plain
    comparison against an aware "now" raises TypeError - which would read as a test bug
    rather than as the parking assertion it is.
    """
    horizon = datetime.now(tz=UTC) + timedelta(days=3650)
    return when > (horizon.replace(tzinfo=None) if when.tzinfo is None else horizon)


# ============================================================== the fake statistics table


def raw_row(bx_id: int) -> dict[str, Any]:
    """One wire-shaped statistics row; `CALL_START_DATE` walks with the id."""
    started = _BASE_START + timedelta(minutes=bx_id)
    return {
        "ID": str(bx_id),
        "CALL_ID": f"b24-{bx_id}@voximplant",
        "CALL_CATEGORY": "external",
        "PORTAL_USER_ID": "42",
        "PORTAL_NUMBER": "+998710000001",
        "PHONE_NUMBER": "+998901234567",
        "CALL_TYPE": "1",
        "CALL_DURATION": "30",
        "CALL_START_DATE": started.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "CALL_FAILED_CODE": "200",
        "COST": "0.0000",
        "COST_CURRENCY": "UZS",
        "SESSION_ID": str(3_841_000_000 + bx_id),
    }


def _time_block(operating: float = 0.4) -> dict[str, Any]:
    """A `time{}` block far below the §5.6 soft limit, so pacing never masks a bug."""
    now = datetime.now(tz=UTC)
    return {
        "start": now.timestamp(),
        "finish": now.timestamp() + operating,
        "duration": operating,
        "processing": operating,
        "operating": operating,
        "operating_reset_at": int((now + timedelta(minutes=10)).timestamp()),
    }


def _lower(params: Mapping[str, Any], *names: str) -> Any:
    """Read a request parameter whatever case the caller spelled it in."""
    folded = {str(key).lower(): value for key, value in params.items()}
    for name in names:
        if name.lower() in folded:
            return folded[name.lower()]
    return None


def id_bounds(params: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """(inclusive lower, inclusive upper) ID bound of one request's FILTER.

    Bitrix24 puts the operator on the FIELD NAME (`">ID"`, `"<=ID"`, research note (b)),
    and an array value means IN - which is how §5.7's recording recheck asks for ids.
    """
    filter_ = _lower(params, "FILTER") or {}
    low: int | None = None
    high: int | None = None
    for key, value in filter_.items():
        field = str(key).strip()
        operator = ""
        while field and field[0] in "<>=!":
            operator += field[0]
            field = field[1:]
        if field.upper() != "ID":
            continue
        if isinstance(value, (list, tuple, set)):
            numbers = [int(item) for item in value]
            return (min(numbers), max(numbers)) if numbers else (None, None)
        number = int(value)
        if operator == ">":
            low = number + 1
        elif operator in ("", "=", ">="):
            low = number
            high = number if operator in ("", "=") else high
        elif operator == "<":
            high = number - 1
        elif operator == "<=":
            high = number
    return low, high


class FakeStatistics:
    """An in-memory `voximplant.statistic.get` with a full request log.

    Duck-typed as `BitrixClient` on purpose: the stages take a client, and giving them a
    real one plus a mock transport would only move the guesswork into HTTP encoding.
    Every non-statistics method answers benignly so a stage that also calls `app.info` or
    `user.get` is not fought by the fixture.
    """

    def __init__(self, ids: Iterable[int]) -> None:
        self.rows: dict[int, dict[str, Any]] = {int(i): raw_row(int(i)) for i in sorted(ids)}
        #: One entry per HTTP request: the `(key, method, params)` list it carried.
        self.requests: list[list[tuple[str, str, dict[str, Any]]]] = []
        #: (request index, command index) -> error returned in `result_error` (halt=0).
        self.errors: dict[tuple[int, int], BitrixError] = {}
        #: Raise (a transport failure) on the first request carrying more than one command.
        self.crash_on_multi_command: BaseException | None = None
        #: An id the store must return even though the FILTER excludes it (§5.4 guard).
        self.inject_violating_id: int | None = None
        self._injected = False
        self.last_time: dict[str, Any] | None = None
        self.endpoint = "https://portal.bitrix24.test/rest/"

    # -- inspection ------------------------------------------------------------------

    @property
    def stat_requests(self) -> list[list[tuple[str, str, dict[str, Any]]]]:
        return [
            request
            for request in self.requests
            if any(method == STATISTIC_METHOD for _, method, _ in request)
        ]

    @property
    def stat_commands(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [
            command
            for request in self.requests
            for command in request
            if command[1] == STATISTIC_METHOD
        ]

    # -- client surface --------------------------------------------------------------

    async def __aenter__(self) -> FakeStatistics:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        result = await self.batch([("0", method, dict(params or {}))])
        error = result.error("0")
        if error is not None:
            raise error
        return result.get("0")

    async def batch(
        self, commands: Sequence[tuple[str, str, dict[str, Any]]], *, halt: int = 0
    ) -> BatchResult:
        index = len(self.requests)
        recorded = [(key, method, dict(params)) for key, method, params in commands]
        self.requests.append(recorded)

        if self.crash_on_multi_command is not None and len(recorded) > 1:
            raise self.crash_on_multi_command

        results: list[CommandResult] = []
        for position, (key, method, params) in enumerate(recorded):
            error = self.errors.get((index, position))
            if error is not None:
                results.append(CommandResult(key=key, result=None, error=error, time=None))
                if halt:
                    break
                continue
            if method != STATISTIC_METHOD:
                results.append(
                    CommandResult(key=key, result=[], error=None, time=_time_block(), total=0)
                )
                continue
            page, total, next_start = self._page(params)
            results.append(
                CommandResult(
                    key=key,
                    result=page,
                    error=None,
                    time=_time_block(),
                    next=next_start,
                    total=total,
                )
            )
        self.last_time = _time_block()
        return BatchResult(commands=tuple(results), time=self.last_time)

    # -- the store itself ------------------------------------------------------------

    def _page(self, params: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int, int | None]:
        low, high = id_bounds(params)
        selected = [
            bx_id
            for bx_id in self.rows
            if (low is None or bx_id >= low) and (high is None or bx_id <= high)
        ]
        order = str(_lower(params, "ORDER") or "ASC").upper()
        sort = str(_lower(params, "SORT") or "ID").upper()
        assert sort == "ID", f"the cursor is the statistics ID, not {sort!r} (§5.2, decision 9)"
        selected.sort(reverse=order == "DESC")

        start = int(_lower(params, "start", "START") or 0)
        window = selected[start : start + PAGE_SIZE]
        page = [self.rows[bx_id] for bx_id in window]
        if self.inject_violating_id is not None and not self._injected and page:
            # A build that ignores the operator returns rows outside the requested range.
            self._injected = True
            page = [raw_row(self.inject_violating_id), *page]
        total = len(selected)
        next_start = start + PAGE_SIZE if start + PAGE_SIZE < total else None
        return page, total, next_start


# ================================================================ stage entry points
#
# The stages take `(fence, client)` plus the `portal_sync` values the runner already
# holds (§5.9 reads the row once per visit and hands the cursors down). These helpers do
# that read, so every test below drives the stage exactly as `sync_portal` does and the
# assertions can stay on the database rows the visit leaves behind.


async def head_fetch_visit(tenant: Tenant, client: FakeStatistics) -> Any:
    sync = await tenant.sync()
    return await run_head_fetch(tenant.fence, client, batch_pages=int(sync["batch_pages"]))


async def incremental_visit(tenant: Tenant, client: FakeStatistics) -> Any:
    sync = await tenant.sync()
    return await run_incremental(
        tenant.fence,
        client,
        high_id=int(sync["high_id"]),
        batch_pages=int(sync["batch_pages"]),
        rescan_from_id=sync["rescan_from_id"],
    )


async def backfill_visit(tenant: Tenant, client: FakeStatistics) -> Any:
    sync = await tenant.sync()
    return await run_backfill(
        tenant.fence,
        client,
        low_id=sync["low_id"],
        batch_pages=int(sync["batch_pages"]),
        backfill_done=int(sync["backfill_done"]),
    )


async def rescan_visit(tenant: Tenant, client: FakeStatistics) -> Any:
    sync = await tenant.sync()
    return await run_id_window_rescan(
        tenant.fence,
        client,
        rescan_from_id=sync["rescan_from_id"],
        high_id=int(sync["high_id"]),
        batch_pages=int(sync["batch_pages"]),
    )


# ========================================================================== fixtures


async def _drop_tenant_rows(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for table in TENANT_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


async def lease_of(portal_id: int) -> Fence:
    for fence in await acquire_leases(50, WORKER_ID):
        if fence.portal_id == portal_id:
            return fence
    raise AssertionError(f"acquire_leases did not offer portal {portal_id}")


class Tenant:
    """A seeded, leased portal plus the readbacks the assertions need."""

    def __init__(self, portal: SeededPortal, fence: Fence) -> None:
        self.portal = portal
        self.fence = fence
        self.portal_id = portal.portal_id

    async def sync(self) -> dict[str, Any]:
        snapshot = await portal_sync_snapshot(self.portal_id)
        assert snapshot is not None
        return snapshot

    async def set_sync(self, assignments: str, **binds: Any) -> None:
        async with control_txn() as session:
            await session.execute(
                text(f"UPDATE portal_sync SET {assignments} WHERE portal_id = :pid"),  # noqa: S608
                {"pid": self.portal_id, **binds},
            )

    async def bx_ids(self) -> list[int]:
        async with tenant_txn(self.portal_id) as session:
            result = await session.execute(
                text("SELECT bx_id FROM calls WHERE portal_id = :pid ORDER BY bx_id"),
                {"pid": self.portal_id},
            )
            return [int(value) for value in result.scalars().all()]

    async def token_status(self) -> str:
        async with control_txn() as session:
            return str(
                (
                    await session.execute(
                        text("SELECT token_status FROM portals WHERE id = :pid"),
                        {"pid": self.portal_id},
                    )
                ).scalar_one()
            )

    async def event_kinds(self) -> list[str]:
        async with control_txn() as session:
            result = await session.execute(
                text("SELECT kind FROM portal_events WHERE portal_id = :pid ORDER BY id"),
                {"pid": self.portal_id},
            )
            return [str(kind) for kind in result.scalars().all()]


@pytest_asyncio.fixture()
async def tenant_factory(
    app_engine: AsyncEngine,
) -> AsyncIterator[Callable[..., Awaitable[Tenant]]]:
    created: list[SeededPortal] = []

    async def make(**state: Any) -> Tenant:
        portal = await seed_portal(**state)
        created.append(portal)
        return Tenant(portal, await lease_of(portal.portal_id))

    try:
        yield make
    finally:
        for portal in created:
            await _drop_tenant_rows(portal.portal_id)
            await delete_portal(portal.member_id)


# ============================================================================= tests


async def test_head_fetch_sets_both_cursors_and_dedupes_the_page_zero_overlap(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.2 - step 1 finds M, step 2 pages the immutable `<=M` window down from it.

    Step 2's page 0 re-reads exactly the rows step 1 already returned. Without the
    in-chunk dedupe of §5.5 Postgres answers "ON CONFLICT DO UPDATE command cannot affect
    row a second time", the chunk rolls back and a fresh install never gets past its first
    visit - so the row count is the dedupe assertion.
    """
    tenant = await tenant_factory(backfill_status="pending", high_id=0, low_id=None)
    client = FakeStatistics(range(1, 121))

    await head_fetch_visit(tenant, client)

    sync = await tenant.sync()
    assert sync["high_id"] == 120, "high_id must be M = max(ID), the newest row seen"
    assert sync["low_id"] == 1, "low_id must be the smallest id the head window reached"
    assert sync["backfill_total"] == 120
    assert sync["backfill_status"] in ("running", "done")
    assert await tenant.bx_ids() == list(range(1, 121))


async def test_a_failed_command_leaves_the_cursor_at_the_previous_boundary(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """Decision 10 - the contiguous-prefix rule, with the exact cursor value asserted.

    Commands 0 and 1 returned ids 999..900; command 2 failed; commands 3+ returned rows
    below the hole. The cursor may only cross the error-free PREFIX, so it stops at 900 -
    the boundary of command 1. Advancing to command 3's minimum would skip ids 899..850
    permanently: the backward cursor never revisits them and the forward cursor is above
    them, so those 50 calls would be missing from the customer's dashboard forever with
    every counter reporting success.
    """
    tenant = await tenant_factory(backfill_status="running", high_id=1000, low_id=1000)
    client = FakeStatistics(range(1, 1000))
    client.errors[(0, 2)] = UnknownBitrixError(
        "INTERNAL_SERVER_ERROR", description="oops", http_status=500
    )

    await backfill_visit(tenant, client)

    sync = await tenant.sync()
    assert sync["low_id"] == 900, "the cursor crossed a failed command"

    stored = set(await tenant.bx_ids())
    assert {999, 950, 900} <= stored, "the error-free prefix must have landed"
    assert 875 not in stored, "the failed command returned nothing to store"
    assert 849 in stored, "rows after the failure are still upserted (harmlessly)"

    assert len(client.stat_requests) == 1, "an errored batch must end the visit, not retry in a loop"


async def test_a_crash_between_head_fetch_steps_resumes_on_the_next_visit(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """Decision 12 - `head_fetch` is idempotent and runs for `pending` AND `head`.

    Step 1 (one command) succeeds and records M; the process dies during step 2 (the
    multi-command batch). If the portal were left in `pending` with a cursor already
    moved, or in `running` with no `low_id`, neither head_fetch nor backfill would have a
    valid starting point and the tenant would be wedged with no error to look at.
    """
    tenant = await tenant_factory(backfill_status="pending", high_id=0, low_id=None)
    crashing = FakeStatistics(range(1, 121))
    crashing.crash_on_multi_command = TransportError("transport_error", description="connection lost")

    with pytest.raises(TransportError):
        # A whole-batch transport failure raises out of `fetch_pages` by design: nothing
        # about the request succeeded, so there is no prefix for the caller to reason about.
        await head_fetch_visit(tenant, crashing)

    mid = await tenant.sync()
    assert mid["backfill_status"] == "head", "a crash between the two steps must be resumable"
    assert mid["high_id"] == 0, "high_id may only move once the head window is stored"
    assert mid["low_id"] is None

    healthy = FakeStatistics(range(1, 121))
    await head_fetch_visit(tenant, healthy)

    done = await tenant.sync()
    assert done["high_id"] == 120
    assert done["low_id"] == 1
    assert await tenant.bx_ids() == list(range(1, 121))


async def test_a_quiet_portal_costs_one_command_not_fifty(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.4 - probe first. Operating time is shared by every app on the account.

    Fifty blind offset commands every 5 minutes per tenant is how a fleet of quiet
    portals collectively trips the account's operating-time limit and blocks the
    customer's OTHER integrations - a failure they would report to Bitrix24, not to us.
    """
    tenant = await tenant_factory(backfill_status="done", high_id=100)
    client = FakeStatistics(range(1, 101))

    await incremental_visit(tenant, client)

    assert len(client.stat_commands) == 1, "a portal with nothing new must cost exactly one command"
    assert (await tenant.sync())["high_id"] == 100


async def test_incremental_packs_only_the_pages_the_probe_proved_exist(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.4 - probe, then `min(ceil(total/50) - 1, batch_pages)` further pages.

    120 new rows = 3 pages. The probe is page 0, so exactly 2 more may be packed; a
    fourth command would be paid for out of the shared operating budget and answer
    nothing.
    """
    tenant = await tenant_factory(backfill_status="done", high_id=100)
    client = FakeStatistics(range(1, 221))

    await incremental_visit(tenant, client)

    assert len(client.stat_requests[0]) == 1, "the first request must be the single-command probe"
    assert len(client.stat_commands) == 3, "ceil(120/50) pages: one probe plus two packed"
    for _, _, params in client.stat_commands:
        low, _high = id_bounds(params)
        assert low == 101, "every incremental command must carry the >high_id filter"
        assert str(_lower(params, "ORDER") or "ASC").upper() == "ASC"

    sync = await tenant.sync()
    assert sync["high_id"] == 220
    assert await tenant.bx_ids() == list(range(101, 221))


async def test_backfill_walks_down_and_yields_after_the_per_visit_budget(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.3 - newest history first, and one busy portal may not starve the others.

    `batch_pages` is pinned to 1 so a visit's budget is countable: with
    BACKFILL_BATCHES_PER_VISIT batches of one 50-row page each, a store of 1200 rows
    cannot finish in one visit, and the run must hand the concurrency slot back instead
    of holding it for the whole history. `low_id` is the proof of where it stopped.
    """
    tenant = await tenant_factory(backfill_status="running", high_id=1200, low_id=1201)
    await tenant.set_sync("batch_pages = 1")
    budget = int(settings.backfill_batches_per_visit)
    client = FakeStatistics(range(1, 1201))

    await backfill_visit(tenant, client)

    first = await tenant.sync()
    fetched = len(await tenant.bx_ids())
    assert len(client.stat_requests) <= budget, "the visit exceeded BACKFILL_BATCHES_PER_VISIT"
    assert fetched == PAGE_SIZE * len(client.stat_requests)
    assert first["low_id"] == 1201 - fetched, "low_id must be the minimum id of the visit"
    assert first["backfill_status"] == "running", "1200 rows cannot fit in one visit's budget"

    # The next visit continues strictly below the cursor - it never re-reads the top.
    second_client = FakeStatistics(range(1, 1201))
    await backfill_visit(tenant, second_client)
    second = await tenant.sync()
    assert second["low_id"] < first["low_id"]
    for _, _, params in second_client.stat_commands:
        _low, high = id_bounds(params)
        assert high is not None and high < first["low_id"], "a backfill page re-read stored history"


async def test_backfill_stops_at_zero_rows_with_status_done(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.3 - "a fetch returned no rows" is the ONLY termination condition."""
    tenant = await tenant_factory(backfill_status="running", high_id=50, low_id=51)
    await tenant.set_sync("batch_pages = 1")
    client = FakeStatistics(range(1, 51))

    for _ in range(3):  # the budget per visit is 1 page here, so allow the walk to finish
        if (await tenant.sync())["backfill_status"] == "done":
            break
        await backfill_visit(tenant, client)

    sync = await tenant.sync()
    assert sync["backfill_status"] == "done"
    assert await tenant.bx_ids() == list(range(1, 51))


async def test_a_row_outside_the_requested_filter_is_reported_as_a_violation() -> None:
    """§5.4 guard - `fetch_pages` must NOTICE, because the caller cannot see the rows.

    An on-premise build that ignores `<ID` answers every page with the same newest rows.
    The upsert would happily store them, the cursor would never move, and the worker
    would hammer a shared, rate-limited API forever. This is the detection half; the
    parking is the caller's half, below.
    """
    client = FakeStatistics(range(1, 500))
    client.inject_violating_id = 777  # violates {"<ID": 500}

    outcome = await fetch_pages(
        client,
        filter={"<ID": 500},
        sort="ID",
        order="DESC",
        starts=[0],
    )

    assert outcome.filter_violation, "a row outside the requested ID range went unnoticed"
    assert isinstance(outcome.filter_violation, str)


async def test_a_filter_violation_parks_the_portal_instead_of_looping(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.4 / decision 25 - a terminal, explained state, never a hot loop.

    `token_status='filter_unsupported'` + `next_run_at='infinity'` + a `sync_blocked`
    event is what the settings page reads to tell the admin their build cannot serve this
    app, instead of the worker quietly burning the account's request budget forever.
    """
    tenant = await tenant_factory(backfill_status="running", high_id=500, low_id=500)
    client = FakeStatistics(range(1, 500))
    client.inject_violating_id = 777

    outcome = await backfill_visit(tenant, client)

    assert outcome.blocked is True
    assert await tenant.token_status() == "filter_unsupported"
    sync = await tenant.sync()
    assert is_parked(sync["next_run_at"]), "the portal must be parked, not merely delayed"
    assert "sync_blocked" in await tenant.event_kinds()
    assert len(client.stat_requests) <= 2, "the violation must stop the visit at once"


async def test_a_quiet_period_does_not_turn_the_rescan_into_a_full_history_re_read(
    tenant_factory: Callable[..., Awaitable[Tenant]],
) -> None:
    """§5.7 step 1 - the window's lower bound is persisted, never derived from the rows.

    Deriving it from `min(bx_id)` of an empty window yields "start at the beginning", so
    a quiet weekend would re-read the entire history every hour, on every quiet tenant at
    once. The persisted `rescan_from_id` is also monotonic: it may catch up to `high_id`,
    never fall back below where it already was.
    """
    quiet = await tenant_factory(backfill_status="done", high_id=500)
    await quiet.set_sync("rescan_from_id = 500, last_rescan_at = NULL")
    idle_client = FakeStatistics(range(1, 501))

    await rescan_visit(quiet, idle_client)

    assert idle_client.stat_commands == [], "an empty window must cost no request at all"
    assert (await quiet.sync())["rescan_from_id"] == 500

    active = await tenant_factory(backfill_status="done", high_id=600)
    await active.set_sync("rescan_from_id = 100, last_rescan_at = NULL")
    client = FakeStatistics(range(1, 601))

    await rescan_visit(active, client)

    assert client.stat_commands, "a non-empty window must actually be re-read"
    for _, _, params in client.stat_commands:
        low, _high = id_bounds(params)
        assert low is not None and low >= 100, "the rescan reached below its persisted bound"

    after = await active.sync()
    assert 100 <= after["rescan_from_id"] <= 600, "rescan_from_id must advance monotonically"
