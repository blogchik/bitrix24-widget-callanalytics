"""`POST /api/v1/deals` - funnel x operator analytics, read live from CRM (§4.12).

Five decisions shape this module, and each of them costs something that is worth naming.

**1. Nothing is stored.** The owner chose an on-demand read: no table, no migration, no sync
phase. What that buys is a report that is never stale and an app whose schema does not grow.
What it costs is that every open spends the portal's REST budget, so the whole feature is
built around a refusal ladder rather than around a cache of rows. `/dashboard` and `/hours`
answer from Postgres in milliseconds; this page answers from Bitrix24 in seconds, and
sometimes it must answer "no".

**2. One table per funnel.** Each funnel owns its own stage directory - `crm.status.list` is
called once per funnel and `STATUS_ID` uniqueness is documented as limited to its own
directory. A single grid would need a header that is either the union of every funnel's
stages (fifteen funnels of eight stages is a hundred and twenty columns, nearly all empty) or
simply wrong. So `groups` is a list of blocks and the cross-funnel `totals` deliberately
carries no per-stage cells: summing two funnels' "Проверка" columns would be an invented
number.

**3. Offset paging, not cursor paging.** `start: -1` is faster (it disables the count) and is
immune to drift, and it forfeits `total` - which is the preflight gate, the one thing that
lets this endpoint refuse an impossible report in two seconds instead of discovering it after
twenty-eight. The trade is deliberate and its cost is stated in `_scan`: the selection filters
on modification time, so a deal edited mid-scan can shift later pages and produce a
one-row undercount. Ordering by id ascending and de-duplicating by id means drift can only
ever lose a row, never double one.

**4. The viewer's own Bitrix24 token, never the installer's.** `principal.access` is decided
from `user.admin` plus a `voximplant.statistic.get` probe - it is a TELEPHONY verdict, and
`acc='all'` is not permission to read a single deal. `services/crm_context.py` states the
doctrine this module follows: *we never model Bitrix24's CRM permissions, we borrow the
answer.* `crm.category.list` is documented as returning only the funnels the CALLER may see,
so the viewer's token produces a report that is correct by construction for an administrator,
a team lead and a salesperson alike, while the installer's token would produce one strictly
wider than what that user may see in their own CRM - and serving it would be exactly the
replay `load_crm_context`'s per-resolver rule exists to prevent. `with_portal_token` must not
appear in this module.

**5. Every rung of the budget ladder REFUSES; none truncates.** A partial report with a
partial Итого row is more dangerous than no report: a supervisor may act on it, and nothing
on screen says which operator's rows were dropped. So a selection too large to finish is a
400 with the count, the limit and the period length attached, and the page tells the reader
exactly how far to narrow.

---------------------------------------------------------------------------------------
Both caches and both limiters are PROCESS-LOCAL, which is correct only because v1 runs
exactly one `api` container (`docker-compose.yml`; the same caveat `oauth.py`, `calls.py` and
`open.py` already carry). A second replica would silently double every budget and halve both
hit rates. That is a note for §4.12 and for whoever first adds `--workers`.
---------------------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.datastructures import QueryParams

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.deals import (
    CATEGORIES_KEY,
    DEAL_DIALECT,
    DEAL_PAGE_SIZE,
    FIELDS_KEY,
    HONOUR_KEYS,
    ITEM_DIALECT,
    ME_KEY,
    Dialect,
    Funnel,
    Stage,
    as_int,
    category_commands,
    field_names_present,
    fields_command,
    honour_probe_commands,
    honour_verdict,
    list_page_commands,
    me_command,
    normalise_semantic,
    page_key,
    parse_deal_rows,
    parse_funnels,
    parse_stages,
    period_leg_filters,
    period_or_filter,
    stage_key,
    status_commands,
    status_key,
)
from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    ExpiredToken,
    InsufficientScope,
    InvalidCredentials,
    MethodNotFound,
    NoAuthFound,
    OperationTimeLimit,
    PaymentRequired,
    PortalDeleted,
    QueryLimitExceeded,
    UserAccessError,
)
from app.config import settings
from app.db.models import Employee, Portal
from app.db.session import tenant_txn
from app.logging import get_logger
from app.security.principal import Principal
from app.services.stats import CallFilters, FilterError, parse_filters
from app.sync.throttle import read_time_block

__all__ = [
    "DealReportError",
    "load_deal_report",
    "parse_deal_filters",
    "reset_deal_caches",
    "reset_deal_report_rate_limit",
]

_log = get_logger(__name__)

#: Facets `parse_filters` understands that mean nothing to a deal scan. Rejected rather
#: than silently dropped: a page that accepts `?result=answered` and ignores it answers a
#: different question than the one the caller asked, and says so nowhere.
_CALL_ONLY_FACETS: Final[tuple[tuple[str, ...], ...]] = (
    ("direction",),
    ("result",),
    ("line", "line_id"),
)

#: How long a portal's dialect verdict stands. An hour, because method existence is a
#: property of the build and a build does not change between two clicks.
_DIALECT_TTL_S: Final[float] = 3600.0

#: Operator rows per funnel block. The subtotal still covers every operator the funnel
#: aggregated, so a truncated block's Итого row is right even when its rows are not all
#: there - the rule `HourlyTalkTable` states for its own row cap.
_ROW_CAP: Final[int] = 200

#: Operator ids one report will look names up for. It bounds a single indexed read against
#: `employees`, and it matches `_ROW_CAP` because a row past the cap is never drawn and so
#: never needs a name.
_OPERATOR_LOOKUP_CAP: Final[int] = 200

#: Page commands per batch. NOT the 50 a batch allows: Bitrix24 caps a single request at
#: sixty seconds, and fifty nested list executions on a busy portal can exceed that - at
#: which point the whole batch is lost, not just its tail.
_PAGE_BATCH: Final[int] = 25

#: The per-user report window, in seconds.
_REPORT_WINDOW_S: Final[float] = 600.0

#: How long to wait for an admission slot before answering "come back in a moment". Short
#: on purpose: a queue in front of a page whose own budget is eighteen seconds is a spinner
#: nobody understands.
_ADMIT_WAIT_S: Final[float] = 0.5

_FIELD_NAMES: Final[tuple[str, ...]] = (
    ITEM_DIALECT.created,
    ITEM_DIALECT.updated,
    ITEM_DIALECT.moved,
    ITEM_DIALECT.closed,
)


class DealReportError(Exception):
    """A deal report that cannot be served, as a machine code (§8).

    Shaped like `stats.FilterError` so a route can hand it straight to the client, and
    deliberately a different type: a refusal here is about budget and about Bitrix24, not
    about a malformed query string, and the two must stay separable in a log.
    """

    def __init__(self, code: str, http_status: int = 400, **extra: Any) -> None:
        self.code = code
        self.http_status = http_status
        self.extra = extra
        super().__init__(code)

    def as_response(self) -> JSONResponse:
        """The wire form: a machine code plus whatever the SPA needs to explain it (§8)."""
        headers: dict[str, str] = {}
        retry_after = self.extra.pop("retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        return JSONResponse(
            {"code": self.code, **self.extra}, status_code=self.http_status, headers=headers
        )


# --- process-local state ---------------------------------------------------------------


@dataclass(frozen=True)
class _Dictionary:
    """One viewer's view of one portal's funnels and their stages."""

    funnels: tuple[Funnel, ...]
    stages: dict[int, tuple[Stage, ...]]
    failed: tuple[int, ...]
    truncated: tuple[int, ...]
    dialect: str


#: `(portal_id, user_id)` -> `(expires_at, dictionary)`. Keyed by USER, not by portal:
#: `crm.category.list` is filtered by the caller's rights, so a portal-wide cache would
#: serve an administrator's funnel list to a salesperson - the same replay §4.8's
#: `resolved_by_user_id` rule refuses for CRM contexts.
_dictionary_cache: dict[tuple[int, int], tuple[float, _Dictionary]] = {}

#: `portal_id` -> `(expires_at, honoured)`. Keyed by portal alone: whether a method exists
#: and whether it applies a filter are properties of the build, not of a permission.
_dialect_cache: dict[int, tuple[float, bool]] = {}

#: `(portal_id, user_id)` -> recent report timestamps.
_report_window: dict[tuple[int, int], deque[float]] = {}

#: Per-portal admission, so one portal can only ever queue behind itself.
_portal_gates: dict[int, asyncio.Semaphore] = {}

_global_gate: asyncio.Semaphore | None = None


def reset_deal_caches() -> None:
    """Drop both caches (tests, and an api process re-reading its configuration)."""
    _dictionary_cache.clear()
    _dialect_cache.clear()


def reset_deal_report_rate_limit() -> None:
    """Drop the per-user window and the admission gates (tests)."""
    _report_window.clear()
    _portal_gates.clear()
    global _global_gate
    _global_gate = None


def _gate() -> asyncio.Semaphore:
    """The process-wide admission gate, created on the running loop.

    Built lazily rather than at import: a `Semaphore` binds to the loop that first awaits
    it, and a module-level one created under a different loop is how a test suite ends up
    deadlocked on something that works in production.
    """
    global _global_gate
    if _global_gate is None:
        _global_gate = asyncio.Semaphore(settings.deal_report_concurrency)
    return _global_gate


def _charge_report(portal_id: int, user_id: int) -> bool:
    """One report against the per-viewer window; False means refuse.

    Keyed on `(portal, user)` rather than on the portal alone, unlike `calls.py`'s refresh
    hint. That endpoint is a best-effort background nudge the SPA swallows; this is the
    page's ONLY action and it re-runs on every period click, so a portal-wide window would
    let one person exploring a month lock the page for their whole team.
    """
    now = time.monotonic()
    window = _report_window.setdefault((portal_id, user_id), deque())
    while window and now - window[0] > _REPORT_WINDOW_S:
        window.popleft()
    if len(window) >= settings.deal_report_limit:
        return False
    window.append(now)
    return True


# --- filters -----------------------------------------------------------------------------


def parse_deal_filters(params: QueryParams, principal: Principal) -> CallFilters:
    """The shared filter vocabulary, narrowed to what a deal scan can honour (§4.12).

    Two differences from `parse_filters`, both of them refusals:

    * `direction`, `result` and `line` are `calls` facets. A deal has no direction and no
      telephony line, so a request carrying one is answered `unsupported_filter` naming the
      offending parameter rather than served an answer to a different question.
    * the period cap is `DEAL_REPORT_MAX_PERIOD_DAYS`, not `MAX_PERIOD_DAYS`. A live REST
      scan cannot honestly offer a year inside the SPA's thirty-second fetch timeout, and
      the other two pages keep their 366 days because they read Postgres.

    `employee` is kept: it maps cleanly onto `@assignedById` and is the cheapest cost lever
    a user has when a period is refused as too large.
    """
    for names in _CALL_ONLY_FACETS:
        for name in names:
            if any(value.strip() for value in params.getlist(name)):
                raise FilterError("unsupported_filter", filter=names[0])

    filters = parse_filters(params, principal)
    if filters.days > settings.deal_report_max_period_days:
        raise FilterError("period_too_long", max_days=settings.deal_report_max_period_days)
    return filters


# --- aggregation ---------------------------------------------------------------------------


@dataclass
class _Measures:
    """One row's counters. `cells` is sparse - an absent key is zero, not a missing value."""

    total: int = 0
    in_progress: int = 0
    won: int = 0
    lost: int = 0
    unknown_stage: int = 0
    cells: dict[str, int] = field(default_factory=dict)

    def add(self, *, semantic: str, column: str | None) -> None:
        self.total += 1
        if semantic == "S":
            self.won += 1
        elif semantic == "F":
            self.lost += 1
        else:
            self.in_progress += 1
        if column is None:
            self.unknown_stage += 1
        else:
            self.cells[column] = self.cells.get(column, 0) + 1

    def merge(self, other: _Measures) -> None:
        self.total += other.total
        self.in_progress += other.in_progress
        self.won += other.won
        self.lost += other.lost
        self.unknown_stage += other.unknown_stage
        for key, value in other.cells.items():
            self.cells[key] = self.cells.get(key, 0) + value

    def wire(self, *, with_cells: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "total": self.total,
            "in_progress": self.in_progress,
            "won": self.won,
            "lost": self.lost,
            "unknown_stage": self.unknown_stage,
        }
        if with_cells:
            out["cells"] = dict(self.cells)
        return out


@dataclass
class _Group:
    """One funnel's aggregate: its operators, its subtotal and the columns it discovered."""

    category_id: int | None
    rows: dict[int | None, _Measures] = field(default_factory=dict)
    subtotal: _Measures = field(default_factory=_Measures)
    #: Stage keys seen on deals but absent from the dictionary, in first-seen order.
    extra_columns: dict[str, str] = field(default_factory=dict)


# --- the scan -------------------------------------------------------------------------------


@dataclass
class _Budget:
    """The wall clock and the operating-time reading, in one place.

    `deadline` is absolute monotonic seconds. It is consulted BEFORE each batch and the
    batch itself is bounded by what remains, because `BitrixClient` has a 120 s httpx
    timeout and `batch()` takes no timeout of its own: a check that only ran between
    batches could pass at 17.9 s and then block for two minutes, and the browser would
    abort at thirty with a generic network error instead of the explanation this endpoint
    computed.
    """

    deadline: float
    requests: int = 0
    operating: Decimal | None = None
    operating_reset_at: dt.datetime | None = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def observe(self, batch: BatchResult) -> None:
        blocks = [command.time for command in batch.commands]
        blocks.append(batch.time)
        for block in blocks:
            operating, reset_at = read_time_block(block)
            if operating is None:
                continue
            if self.operating is None or operating > self.operating:
                self.operating = operating
                self.operating_reset_at = reset_at

    def over_operating_budget(self) -> bool:
        """True once this method's own budget is close enough to the ceiling to stop.

        The limit is enforced per method per app, so blowing through it takes
        `crm.item.list` away from this app across the whole portal - including the CRM
        detail tab's own reads. `ThrottleState` is deliberately not reused: its
        `operating_limit_s` was learned from `voximplant.statistic.get`, a different method
        with a different budget.
        """
        if self.operating is None:
            return False
        ceiling = Decimal(str(settings.operating_soft_ratio)) * Decimal(
            settings.operating_limit_floor
        )
        return self.operating >= ceiling

    def retry_after(self) -> int:
        if self.operating_reset_at is None:
            return 60
        delta = (self.operating_reset_at - dt.datetime.now(dt.UTC)).total_seconds()
        return max(1, min(600, int(delta) + 1))


def _iso(moment: dt.datetime) -> str:
    """A period bound with an explicit offset.

    Never a bare date: Bitrix24 reads one in the PORTAL's timezone, and this app computes
    its bounds in the viewer's, which is the normal case for a business with staff in more
    than one place. An explicit offset removes the question entirely.
    """
    return moment.astimezone(dt.UTC).isoformat()


def _classify(exc: BitrixError) -> DealReportError:
    """A typed Bitrix24 failure as the machine code the SPA already knows how to render.

    `ExpiredToken` answers **409 `viewer_token_required`**, never 401. A 401 makes
    `apiFetch` run the session exchange, which re-mints OUR JWT - a different credential
    entirely - then re-posts the identical stale Bitrix token and clears the session when
    that fails. 409 is the shape the SPA already reads as "go and fetch something, then
    come back", and `web/src/lib/calls.ts` already exports the code.
    """
    if isinstance(exc, (ExpiredToken, NoAuthFound, InvalidCredentials)):
        return DealReportError("viewer_token_required", 409)
    if isinstance(exc, (AccessDenied, UserAccessError)):
        return DealReportError("crm_no_access", 403)
    if isinstance(exc, InsufficientScope):
        return DealReportError("insufficient_scope", 403)
    if isinstance(exc, MethodNotFound):
        return DealReportError("method_missing", 409)
    if isinstance(exc, OperationTimeLimit):
        return DealReportError("operation_time_limit", 503, retry_after=60)
    if isinstance(exc, QueryLimitExceeded):
        return DealReportError("query_limit_exceeded", 503, retry_after=30)
    if isinstance(exc, (PortalDeleted, PaymentRequired)):
        return DealReportError("portal_inactive", 409)
    return DealReportError("deal_report_failed", 502, error=exc.code)


async def _run_batch(
    client: BitrixClient,
    commands: Sequence[tuple[str, str, dict[str, Any]]],
    budget: _Budget,
) -> BatchResult:
    """One batch, hard-bounded by what is left of the deadline.

    A timeout here is a refusal with a code, not a dropped connection: the SPA can explain
    "this selection is too large for a live read" and cannot explain a network error.
    """
    remaining = budget.remaining()
    if remaining <= 0:
        raise DealReportError("deal_scan_too_large", 400)
    try:
        result = await asyncio.wait_for(client.batch(list(commands)), timeout=max(0.5, remaining))
    except TimeoutError:
        raise DealReportError("deal_scan_too_large", 400) from None
    except BitrixError as exc:
        raise _classify(exc) from exc
    budget.requests += 1
    budget.observe(result)
    if budget.over_operating_budget():
        raise DealReportError(
            "operation_time_limit", 503, retry_after=budget.retry_after()
        )
    return result


def _dialect_for(portal_id: int) -> Dialect:
    entry = _dialect_cache.get(portal_id)
    if entry is not None and entry[0] > time.monotonic():
        return ITEM_DIALECT if entry[1] else DEAL_DIALECT
    return ITEM_DIALECT


def _remember_dialect(portal_id: int, honoured: bool) -> None:
    _dialect_cache[portal_id] = (time.monotonic() + _DIALECT_TTL_S, honoured)


def _build_dictionary(
    batch: BatchResult, funnels: Sequence[Funnel], *, dialect_name: str
) -> _Dictionary:
    """Fold one batch of `crm.status.list` answers into the column dictionary.

    A funnel whose stage list errored is recorded in `failed` rather than taking the page
    down: its deals still arrive, their stages are synthesised as `known: false` columns,
    and the arithmetic stays correct - only the column HEADERS are missing, and the page
    says so. A funnel whose returned row count is short of its own `total` is recorded in
    `truncated`: `crm.status.list` documents no `start` parameter, so a directory of more
    than fifty stages simply cannot be read in full.
    """
    stages: dict[int, tuple[Stage, ...]] = {}
    failed: list[int] = []
    truncated: list[int] = []
    for funnel in funnels:
        key = status_key(funnel.id)
        if not batch.ok(key):
            failed.append(funnel.id)
            stages[funnel.id] = ()
            continue
        parsed = parse_stages(batch.get(key), category_id=funnel.id)
        total = batch.total_of(key)
        if total is not None and len(parsed) < total:
            truncated.append(funnel.id)
        stages[funnel.id] = tuple(parsed)
    return _Dictionary(
        funnels=tuple(funnels),
        stages=stages,
        failed=tuple(failed),
        truncated=tuple(truncated),
        dialect=dialect_name,
    )


async def _employee_labels(portal_id: int, user_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Names for the operators on screen, from the local cache and nothing else.

    Deliberately no `user.get` fan-out. The `employees` cache is filled by the sync worker
    with `ADMIN_MODE: true` under the INSTALLER's credential, so it holds people a
    restricted viewer may not be entitled to see; resolving arbitrary ids through it on a
    page whose whole point is to honour the viewer's own rights would hand back exactly
    what the rest of the design refuses to. An id the cache has never seen renders as
    "User #id", which is the state §7 already defines for it.
    """
    if not user_ids:
        return {}
    capped = set(sorted(user_ids)[:_OPERATOR_LOOKUP_CAP])
    async with tenant_txn(portal_id) as session:
        rows = (
            await session.execute(
                select(
                    Employee.bx_user_id,
                    Employee.name,
                    Employee.last_name,
                    Employee.second_name,
                    Employee.phone_inner,
                    Employee.active,
                ).where(Employee.portal_id == portal_id, Employee.bx_user_id.in_(capped))
            )
        ).all()
    labels: dict[int, dict[str, Any]] = {}
    for row in rows:
        parts = [row.last_name, row.name, row.second_name]
        joined = " ".join(part.strip() for part in parts if part and part.strip())
        labels[int(row.bx_user_id)] = {
            "name": joined or None,
            "phone_inner": row.phone_inner,
            "active": bool(row.active),
        }
    return labels


def _streams(dialect: Dialect, *, start_iso: str, end_iso: str) -> tuple[dict[str, Any], ...]:
    """The period as one filter, or as three when the dialect has no OR.

    `crm.item.list` expresses owner decision 3 in a single documented `logic: "OR"` group.
    `crm.deal.list` has no OR at all, so the same question becomes three independent
    selections whose rows are folded together by id - three times the pages and three times
    the operating time, which is the whole reason the universal method is the primary path.
    """
    if dialect.universal:
        return (period_or_filter(dialect, start_iso=start_iso, end_iso=end_iso),)
    return period_leg_filters(dialect, start_iso=start_iso, end_iso=end_iso)


def _identity_ok(batch: BatchResult, principal: Principal) -> bool:
    """`user.current` must name the viewer whose JWT this is.

    Byte-identical in intent to `POST /calls/{id}/play-url`: the posted token is attacker
    supplied, and without this check a token belonging to somebody else would answer
    somebody else's report under this session's identity.
    """
    result = batch.get(ME_KEY)
    if not isinstance(result, Mapping):
        return False
    return as_int(result.get("ID") if "ID" in result else result.get("id")) == principal.user_id


async def _load_dictionary(
    client: BitrixClient,
    principal: Principal,
    portal: Portal,
    budget: _Budget,
    *,
    list_dialect: Dialect,
    probe_honour: bool,
) -> tuple[_Dictionary, Dialect]:
    """The cold path: identity, funnels, field names, stages and the honour probe.

    Returns the dictionary and the LIST dialect the scan should use, which are decided
    **independently**. That independence is the whole shape of this function and it is not
    cosmetic: the two questions are about different methods.

    * "Does this build have `crm.category.list`?" is answered by `MethodNotFound` on the
      dictionary, and its fallback is `crm.dealcategory.*`.
    * "Does this build honour a `logic: OR` date filter on `crm.item.list`?" is answered by
      the probe, and its fallback is three `crm.deal.list` selections.

    A portal can fail either without failing the other, and an earlier revision of this
    module re-read the whole dictionary whenever the probe failed - which cost a round trip,
    and on a portal whose `crm.category.list` worked perfectly would then ask for
    `crm.dealcategory.list` and render an empty report if that one was unavailable.

    Two round trips in the ordinary case, because the funnel ids are not known until the
    first answers and `crm.status.list` needs one command per funnel. The honour probe rides
    in the second batch rather than costing a third.
    """
    dictionary_universal = True

    first = [me_command(), *category_commands(universal=True)]
    if list_dialect.universal:
        first.append(fields_command())
    batch = await _run_batch(client, first, budget)

    if not _identity_ok(batch, principal):
        raise DealReportError("invalid_session", 401)

    error = batch.error(CATEGORIES_KEY)
    if error is not None:
        if not isinstance(error, MethodNotFound):
            raise _classify(error)
        # An older build without the universal dictionary. Only the dictionary half moves;
        # the list half is still decided by the probe below.
        _log.info(
            "deals: crm.category.list is absent, using the frozen dictionary methods",
            extra={"portal_id": portal.id},
        )
        dictionary_universal = False
        legacy = await _run_batch(client, category_commands(universal=False), budget)
        legacy_error = legacy.error(CATEGORIES_KEY)
        if legacy_error is not None:
            raise _classify(legacy_error)
        batch = legacy

    funnels = parse_funnels(batch.get(CATEGORIES_KEY), universal=dictionary_universal)
    if not funnels:
        # An empty list is indistinguishable from "you may see no funnels", and the honest
        # answer to both is the state §4.11 already defines - never a blank table.
        raise DealReportError("crm_no_access", 403)

    if list_dialect.universal:
        missing = set(_FIELD_NAMES) - field_names_present(batch.get(FIELDS_KEY), _FIELD_NAMES)
        if missing:
            # A name the union filter depends on does not exist here. Sending it anyway
            # risks it being IGNORED rather than refused, which silently widens the period.
            _log.info(
                "deals: universal field names missing, using the deal dialect",
                extra={"portal_id": portal.id, "missing": sorted(missing)},
            )
            list_dialect = DEAL_DIALECT
            _remember_dialect(portal.id, honoured=False)

    ids = [funnel.id for funnel in funnels]
    probe = probe_honour and list_dialect.universal
    room = 50 - (len(HONOUR_KEYS) if probe else 0)
    second: list[tuple[str, str, dict[str, Any]]] = list(
        status_commands(ids[:room], universal=dictionary_universal)
    )
    if probe:
        second.extend(honour_probe_commands(list_dialect))
    stage_batch = await _run_batch(client, second, budget)

    # A portal with more funnels than one batch holds pays one more round trip for the rest
    # rather than losing their columns.
    for offset in range(room, len(ids), 50):
        extra = await _run_batch(
            client,
            status_commands(ids[offset : offset + 50], universal=dictionary_universal),
            budget,
        )
        stage_batch = BatchResult(
            commands=stage_batch.commands + extra.commands, time=stage_batch.time
        )

    if probe:
        verdict = honour_verdict(
            baseline=stage_batch.total_of(HONOUR_KEYS[0]),
            future_dates=stage_batch.total_of(HONOUR_KEYS[1]),
            future_closed=stage_batch.total_of(HONOUR_KEYS[2]),
        )
        if verdict is False:
            # The filter was sent and not applied. Believing this portal would produce a
            # report that is plausible, larger than the truth, and wrong with no symptom.
            _log.warning(
                "deals: the portal does not honour the OR date filter, using the deal dialect",
                extra={"portal_id": portal.id},
            )
            list_dialect = DEAL_DIALECT
            _remember_dialect(portal.id, honoured=False)
        elif verdict is True:
            _remember_dialect(portal.id, honoured=True)
        # `None` is deliberately NOT cached: a viewer who can see no deals proves nothing
        # about the build, and recording their measurement would pin an untested verdict
        # on the whole portal for an hour.

    dictionary = _build_dictionary(
        stage_batch,
        funnels,
        dialect_name="category" if dictionary_universal else "dealcategory",
    )
    _dictionary_cache[(portal.id, principal.user_id)] = (
        time.monotonic() + settings.deal_dictionary_ttl_sec,
        dictionary,
    )
    return dictionary, list_dialect


def _stage_index(dictionary: _Dictionary) -> dict[str, Stage]:
    """Every known column, by its composite `"<category_id>:<status_id>"` key."""
    return {stage.key: stage for stages in dictionary.stages.values() for stage in stages}


def _fold(
    rows: Sequence[Mapping[str, Any]],
    *,
    dialect: Dialect,
    known: Mapping[str, Stage],
    groups: dict[int | None, _Group],
    seen: set[int],
) -> None:
    """Fold one page of deals into the per-funnel aggregates.

    Deduped by id, which is mandatory on the `deal` dialect (three overlapping selections)
    and cheap insurance on the universal one, where offset drift over a selection that
    filters on modification time can show the same row twice.

    A deal whose stage is absent from the dictionary is NOT dropped. It gets a synthesised
    column instead, because a dropped deal makes the row's `total` disagree with the sum of
    its cells, and a row whose cells do not add up to its own total is a report nobody can
    trust. The three routine ways this happens - a stage deleted since the cache was filled,
    a directory past its unpageable fiftieth row, and a funnel whose stage list errored -
    are all invisible to the reader otherwise.
    """
    for row in rows:
        identifier = as_int(row.get(dialect.id))
        if identifier is None or identifier in seen:
            continue
        seen.add(identifier)

        category_id = as_int(row.get(dialect.category_id))
        group = groups.get(category_id)
        if group is None:
            group = _Group(category_id=category_id)
            groups[category_id] = group

        raw_stage = row.get(dialect.stage_id)
        status_text = raw_stage.strip() if isinstance(raw_stage, str) else ""
        column = stage_key(category_id, status_text) if category_id is not None else None
        if column is not None and column not in known and column not in group.extra_columns:
            group.extra_columns[column] = status_text

        semantic = normalise_semantic(row.get(dialect.semantic))
        if semantic == "P" and column is not None:
            # `stageSemanticId` is the primary classifier because it needs no join, but an
            # older build may not carry it. The dictionary's own `SEMANTICS` is the fallback,
            # and only in that direction: a deal that says `S` is won whatever the directory
            # thinks, because the deal is the live record.
            stage = known.get(column)
            if stage is not None:
                semantic = stage.semantic

        operator = as_int(row.get(dialect.assigned_by_id))
        if operator is not None and operator <= 0:
            operator = None
        measures = group.rows.get(operator)
        if measures is None:
            measures = _Measures()
            group.rows[operator] = measures
        measures.add(semantic=semantic, column=column)
        group.subtotal.add(semantic=semantic, column=column)


def _too_large(deals_total: int, days: int) -> DealReportError:
    """The one refusal a user will actually meet, carrying everything they need to act.

    The count, the limit and the period length together are what turn "too large" into
    "narrow this by about a third" - which is the difference between a refusal a supervisor
    can work with and one they can only complain about.
    """
    return DealReportError(
        "deal_scan_too_large",
        400,
        deals=deals_total,
        max_deals=settings.deal_scan_cap,
        days=days,
    )


async def _scan(
    client: BitrixClient,
    budget: _Budget,
    *,
    dialect: Dialect,
    dictionary: _Dictionary,
    filters: CallFilters,
    assigned_to: Sequence[int],
    first_batch: BatchResult | None,
) -> tuple[dict[int | None, _Group], int, int]:
    """Page the selection, refusing rather than truncating.

    Returns `(groups, folded, deals_total)`. `first_batch` is the preflight the warm path
    already sent alongside `user.current`; the cold path passes None and pays one request
    for it here.
    """
    start_iso = _iso(filters.start_utc)
    end_iso = _iso(filters.end_utc)
    streams = _streams(dialect, start_iso=start_iso, end_iso=end_iso)

    if first_batch is None:
        commands: list[tuple[str, str, dict[str, Any]]] = []
        for index, stream in enumerate(streams):
            commands.extend(
                list_page_commands(
                    dialect, filter_=stream, starts=[0], assigned_to=assigned_to, stream=index
                )
            )
        first_batch = await _run_batch(client, commands, budget)

    totals: list[int] = []
    for index in range(len(streams)):
        key = page_key(0, index)
        error = first_batch.error(key)
        if error is not None:
            raise _classify(error)
        totals.append(first_batch.total_of(key) or 0)

    # On the fallback dialect this is the SUM of three overlapping legs, which is at least
    # the size of their union. Refusing on the conservative number is deliberate: the honest
    # count is unknowable before the scan, and over-refusing costs a narrower period while
    # under-refusing costs a report that dies at the browser's timeout with no explanation.
    deals_total = sum(totals)
    if deals_total > settings.deal_scan_cap:
        raise _too_large(deals_total, filters.days)

    known = _stage_index(dictionary)
    groups: dict[int | None, _Group] = {}
    seen: set[int] = set()
    for index in range(len(streams)):
        _fold(
            parse_deal_rows(first_batch.get(page_key(0, index)), universal=dialect.universal),
            dialect=dialect,
            known=known,
            groups=groups,
            seen=seen,
        )

    pending: list[tuple[int, int]] = [
        (index, start)
        for index, total in enumerate(totals)
        for start in range(DEAL_PAGE_SIZE, total, DEAL_PAGE_SIZE)
    ]
    page_cost = 0.0
    while pending:
        remaining = budget.remaining()
        if remaining <= 0:
            raise _too_large(deals_total, filters.days)
        # Refuse BEFORE spending the batches that cannot finish, not after. The projection
        # uses the cost actually observed on this portal rather than a guess, so a fast
        # portal is allowed the whole cap and a slow one is stopped early with the same
        # explanation the preflight gate gives.
        if page_cost > 0 and len(pending) * page_cost > remaining:
            raise _too_large(deals_total, filters.days)

        size = _PAGE_BATCH
        if page_cost > 0:
            size = max(1, min(_PAGE_BATCH, int(remaining / page_cost)))
        chunk, pending = pending[:size], pending[size:]

        commands = []
        for index, start in chunk:
            commands.extend(
                list_page_commands(
                    dialect,
                    filter_=streams[index],
                    starts=[start],
                    assigned_to=assigned_to,
                    stream=index,
                )
            )
        began = time.monotonic()
        batch = await _run_batch(client, commands, budget)
        page_cost = max(page_cost, (time.monotonic() - began) / max(1, len(chunk)))

        for index, start in chunk:
            key = page_key(start, index)
            error = batch.error(key)
            if error is not None:
                # A silently dropped page under-counts one stage column and reads to the
                # user as data, with nothing on screen to say so. Refusing is the only
                # honest answer.
                raise _classify(error)
            _fold(
                parse_deal_rows(batch.get(key), universal=dialect.universal),
                dialect=dialect,
                known=known,
                groups=groups,
                seen=seen,
            )

    return groups, len(seen), deals_total


def _stage_wire(stage: Stage) -> dict[str, Any]:
    return {
        "key": stage.key,
        "category_id": stage.category_id,
        "status_id": stage.status_id,
        "name": stage.name,
        "semantic": stage.semantic,
        "sort": stage.sort,
        "known": stage.known,
    }


def _synthesised(column: str, status_id: str, category_id: int, order: int) -> Stage:
    """A column discovered on a deal but absent from the dictionary.

    `sort` is pushed past any real stage so these land on the right of the columns the
    portal actually defines, and `known: false` is what the page uses to label them from
    the status id rather than from a name it does not have.
    """
    return Stage(
        key=column,
        category_id=category_id,
        status_id=status_id,
        name="",
        semantic="P",
        sort=1_000_000 + order,
        known=False,
    )


def _columns(dictionary: _Dictionary, group: _Group) -> list[Stage]:
    """One funnel's columns: its dictionary stages in SORT order, then what the deals found."""
    category_id = group.category_id
    base = list(dictionary.stages.get(category_id, ())) if category_id is not None else []
    extras = [
        _synthesised(column, status_id, category_id, order)
        for order, (column, status_id) in enumerate(group.extra_columns.items())
        if category_id is not None
    ]
    return base + extras


def _row_wire(
    user_id: int | None, measures: _Measures, labels: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any]:
    label = labels.get(user_id) if user_id is not None else None
    return {
        "user_id": user_id,
        "name": label["name"] if label else None,
        "phone_inner": label["phone_inner"] if label else None,
        "active": label["active"] if label else None,
        **measures.wire(),
    }


def _sorted_rows(group: _Group) -> list[tuple[int | None, _Measures]]:
    """Busiest operator first; the unassigned row always last.

    Unassigned is KEPT rather than dropped - `load_hours` keeps its NULL `portal_user_id`
    row for the same reason. Dropping it would make this page's Итого disagree with its own
    rows, and a reader who spots that has no way to find out why.
    """
    rows: list[tuple[int | None, _Measures]] = [
        (uid, m) for uid, m in group.rows.items() if uid is not None
    ]
    rows.sort(key=lambda pair: (-pair[1].total, pair[0] or 0))
    if None in group.rows:
        rows.append((None, group.rows[None]))
    return rows


def _build_response(
    *,
    filters: CallFilters,
    dictionary: _Dictionary,
    dialect: Dialect,
    groups: Mapping[int | None, _Group],
    labels: Mapping[int, Mapping[str, Any]],
    folded: int,
    deals_total: int,
    budget: _Budget,
    from_cache: bool,
    assigned_to: Sequence[int],
) -> dict[str, Any]:
    """The wire form (§4.12).

    Funnel order follows the portal's own `sort`, and a funnel the deals mention but the
    dictionary does not is appended after them: `crm.category.list` is filtered by the
    caller's rights and cached for a few minutes while the deals are always live, so a
    funnel created five minutes ago is routine rather than exceptional. Dropping its deals
    would make the grand total disagree with the blocks above it.
    """
    order = {funnel.id: index for index, funnel in enumerate(dictionary.funnels)}
    names = {funnel.id: funnel for funnel in dictionary.funnels}

    def rank(category_id: int | None) -> tuple[int, int]:
        if category_id is None:
            return (2, 0)
        return (0, order[category_id]) if category_id in order else (1, category_id)

    stages: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    totals = _Measures()

    for category_id in sorted(groups, key=rank):
        group = groups[category_id]
        totals.merge(group.subtotal)
        columns = _columns(dictionary, group)
        stages.extend(_stage_wire(stage) for stage in columns)
        rows = _sorted_rows(group)
        funnel = names.get(category_id) if category_id is not None else None
        blocks.append(
            {
                "category_id": category_id,
                "name": funnel.name if funnel else "",
                "is_default": bool(funnel.is_default) if funnel else False,
                "stage_keys": [stage.key for stage in columns],
                "rows": [_row_wire(uid, m, labels) for uid, m in rows[:_ROW_CAP]],
                "subtotal": group.subtotal.wire(),
                "total_rows": len(rows),
                "truncated": len(rows) > _ROW_CAP,
            }
        )

    hidden = sum(1 for funnel in dictionary.funnels if funnel.id not in groups)
    described = filters.describe()
    return {
        "range": described["range"],
        # Hand-built rather than echoed: direction/result/line are `calls` facets that this
        # endpoint refuses outright, so repeating `describe()["filters"]` would state four
        # things about the scan that are not true of it.
        "filters": {"employees": list(assigned_to)},
        "stages": stages,
        "groups": blocks,
        "totals": totals.wire(with_cells=False),
        "scan": {
            "deals": folded,
            "deals_total": deals_total,
            "deal_cap": settings.deal_scan_cap,
            "stage_dictionary_failed": list(dictionary.failed),
            "stage_dictionary_truncated": list(dictionary.truncated),
            "funnels_hidden": hidden,
            "list_dialect": dialect.name,
            "dictionary_dialect": dictionary.dialect,
            "rest_requests": budget.requests,
            "from_cache": from_cache,
        },
    }


async def _report(
    principal: Principal,
    portal: Portal,
    filters: CallFilters,
    *,
    viewer_token: str,
    correlation_id: uuid.UUID,
) -> dict[str, Any]:
    """One report, from admission to wire form."""
    budget = _Budget(deadline=time.monotonic() + settings.deal_scan_deadline_sec)

    # §4.7 applied to a CRM read: an `own` viewer sees their own deals and nothing else.
    # Pinned server-side rather than trusted to the control, and not merely because the
    # control could be bypassed: operator NAMES come from the `employees` cache, which the
    # worker fills under the INSTALLER's credential with `ADMIN_MODE: true`, so a report
    # that aggregated other people's deals would also put their names on screen.
    assigned_to: tuple[int, ...] = (
        filters.employees if principal.access == "all" else (principal.user_id,)
    )

    cache_key = (portal.id, principal.user_id)
    cached = _dictionary_cache.get(cache_key)
    dictionary = cached[1] if cached is not None and cached[0] > time.monotonic() else None
    dialect_entry = _dialect_cache.get(portal.id)
    dialect_known = dialect_entry is not None and dialect_entry[0] > time.monotonic()
    dialect = _dialect_for(portal.id)

    async with BitrixClient(
        endpoint=portal.client_endpoint,
        access_token=viewer_token,
        portal_id=portal.id,
        member_id=portal.member_id,
        # §6: whose token is in play. The VIEWER's, always - copying `portal.token_user_id`
        # here (as the installer-credential paths do) would file every row of this page's
        # audit trail under the technical user who installed the app.
        token_user_id=principal.user_id,
        correlation_id=correlation_id,
    ) as client:
        if dictionary is not None and dialect_known:
            # The warm path: identity proof and the first page of deals in ONE request, so
            # a portal with fifty matching deals answers the whole report in one round trip.
            commands: list[tuple[str, str, dict[str, Any]]] = [me_command()]
            streams = _streams(
                dialect, start_iso=_iso(filters.start_utc), end_iso=_iso(filters.end_utc)
            )
            for index, stream in enumerate(streams):
                commands.extend(
                    list_page_commands(
                        dialect, filter_=stream, starts=[0], assigned_to=assigned_to, stream=index
                    )
                )
            first = await _run_batch(client, commands, budget)
            if not _identity_ok(first, principal):
                raise DealReportError("invalid_session", 401)
            from_cache = True
        else:
            dictionary, dialect = await _load_dictionary(
                client,
                principal,
                portal,
                budget,
                list_dialect=dialect,
                probe_honour=not dialect_known,
            )
            first = None
            from_cache = False

        groups, folded, deals_total = await _scan(
            client,
            budget,
            dialect=dialect,
            dictionary=dictionary,
            filters=filters,
            assigned_to=assigned_to,
            first_batch=first,
        )

    operators = {uid for group in groups.values() for uid in group.rows if uid is not None}
    labels = await _employee_labels(portal.id, operators)

    return _build_response(
        filters=filters,
        dictionary=dictionary,
        dialect=dialect,
        groups=groups,
        labels=labels,
        folded=folded,
        deals_total=deals_total,
        budget=budget,
        from_cache=from_cache,
        assigned_to=assigned_to,
    )


async def load_deal_report(
    principal: Principal,
    portal: Portal,
    filters: CallFilters,
    *,
    viewer_token: str,
    correlation_id: uuid.UUID,
) -> dict[str, Any]:
    """Admission, then the report (§4.12).

    Three guards, in this order and for three different reasons:

    * the **process gate** protects the box and the shared egress IP - the leaky bucket is
      counted per source address and every tenant of this deployment shares one, so a
      per-portal limit alone does nothing when twenty portals click at once;
    * the **portal gate** is a `Semaphore(1)` so a portal can only ever queue behind
      itself, and one busy customer cannot hold a slot another customer is waiting for;
    * the **per-viewer window** is charged LAST, once the request is actually about to
      spend REST. Charging it before the gates would burn one of a user's twelve slots on
      a refusal that protected nothing.
    """
    gate = _gate()
    try:
        await asyncio.wait_for(gate.acquire(), _ADMIT_WAIT_S)
    except TimeoutError:
        raise DealReportError("retry", 503, retry_after=10) from None
    try:
        portal_gate = _portal_gates.setdefault(portal.id, asyncio.Semaphore(1))
        try:
            await asyncio.wait_for(portal_gate.acquire(), _ADMIT_WAIT_S)
        except TimeoutError:
            raise DealReportError("retry", 503, retry_after=5) from None
        try:
            if not _charge_report(portal.id, principal.user_id):
                raise DealReportError("rate_limited", 429, retry_after=60)
            return await _report(
                principal,
                portal,
                filters,
                viewer_token=viewer_token,
                correlation_id=correlation_id,
            )
        finally:
            portal_gate.release()
    finally:
        gate.release()
