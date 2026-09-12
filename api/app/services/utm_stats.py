"""`POST /api/v1/utm` - UTM x leads/deals analytics, read live from CRM (§4.13).

This is §4.12's twin, and the places it differs are the places worth reading.

**1. Nothing is stored.** The same owner decision `/deals` was built on: no table, no
migration, no sync phase. What it buys is a report that is never stale; what it costs is
that every open spends the portal's REST budget, so the whole feature is a refusal ladder
rather than a cache of rows.

**2. There is no dictionary phase at all.** `/deals` spends its cold path on
`crm.category.list` plus one `crm.status.list` per funnel because its COLUMNS are portal
data. This page's columns are UTM values, which arrive on the rows; won/lost/in-progress
come off `stageSemanticId`, which is on the row too. So there is no dictionary, no
dictionary cache, no dictionary TTL - and the cold path is TWO round trips, not three.

**3. The period is creation time only, one flat leg.** Decision 3, and it makes the legacy
dialects cost exactly what the universal ones cost. It also makes an ignored filter far more
dangerous than it was in §4.12 - see `bitrix/utm.py`'s docblock - which is why the honour
probe survives being halved.

**4. Two entities, and only one of them is required.** Leads can be turned off in a portal.
A missing lead leg DEGRADES the report to deals-only and says so; it never refuses, and
`leads.available == false` is deliberately distinguishable on the wire from `leads == 0`,
because "this portal has no leads module" and "nobody created a lead last month" are
different facts that a marketer would act on differently.

**5. Cardinality never refuses.** A `utm_term` that is unique per click would make the
combination count enormous - but the count is unknowable until the scan that produced it has
already been paid for. Refusing there would spend the whole budget and then decline to
answer, which is exactly what §4.12's ladder forbids ("refuse BEFORE spending the batches
that cannot finish, not after"). So instead there is a bucket ladder: truncate the value,
then fold everything past the top N of each dimension into ONE `other` bucket, then lift the
two finest dimensions out of the key entirely. No lead and no deal is ever dropped, and
`sum(rows) == totals` holds exactly at every rung. That is relabelling, not truncation.

**6. Money is deals-only, and the page would rather show nothing than a wrong sum.** See
`_amounts`.

---------------------------------------------------------------------------------------
Both the capability cache and both limiters are PROCESS-LOCAL, which is correct only because
v1 runs exactly one `api` container (`docker-compose.yml`; the caveat `deal_stats.py`,
`oauth.py`, `calls.py` and `open.py` already carry). A second replica would silently double
every budget and halve the hit rate. That is a note for §4.13 and for whoever first adds
`--workers`.
---------------------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from fastapi.responses import JSONResponse
from starlette.datastructures import QueryParams

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.deals import field_names_present
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
from app.bitrix.utm import (
    BUCKET_COLLAPSED,
    BUCKET_NONE,
    BUCKET_OTHER,
    DEAL_ITEM,
    DIMENSIONS,
    KIND_DEAL,
    KIND_LEAD,
    LEAD_ITEM,
    ME_KEY,
    PAGE_SIZE,
    EntityDialect,
    core_names,
    fields_command,
    fields_key,
    honour_key,
    honour_probe_commands,
    honour_verdict,
    legacy_of,
    list_page_commands,
    me_command,
    page_key,
    parse_rows,
    period_filter,
    read_id,
    read_lead_id,
    read_money,
    read_semantic,
    utm_names,
    utm_values,
)
from app.config import settings
from app.db.models import Portal
from app.logging import get_logger
from app.security.principal import Principal
from app.services.stats import CallFilters, FilterError, parse_filters, resolve_timezone
from app.sync.throttle import read_time_block

__all__ = [
    "UtmReportError",
    "load_utm_report",
    "parse_utm_filters",
    "reset_utm_caches",
    "reset_utm_report_rate_limit",
]

_log = get_logger(__name__)

#: Query-string names that belong to `calls` and mean nothing to a CRM record. Refused
#: rather than ignored: serving an answer to a different question is worse than a 400.
_CALL_ONLY_FACETS: Final[tuple[tuple[str, ...], ...]] = (
    ("direction", "directions"),
    ("result", "results"),
    ("line", "lines"),
)

#: Page commands per batch. NOT the 50 a batch allows: Bitrix24 caps a single request at
#: sixty seconds, and fifty nested list executions on a busy portal can exceed that - at
#: which point the whole batch is lost, not just its tail. (`deal_stats.py`'s number, for
#: the same reason, and this page packs TWO entities' pages into the same budget.)
_PAGE_BATCH: Final[int] = 25

#: The per-user report window, in seconds.
_REPORT_WINDOW_S: Final[float] = 600.0

#: How long to wait for an admission slot before answering "come back in a moment". Short on
#: purpose: a queue in front of a page whose own budget is eighteen seconds is a spinner
#: nobody understands.
_ADMIT_WAIT_S: Final[float] = 0.5

#: The order dimensions are lifted out of the composite key when the row count is still past
#: the cap after bucketing. PUBLISHED rather than incidental: the page names the dimension it
#: lost, and a reader must be able to predict which one that will be.
_COLLAPSE_ORDER: Final[tuple[str, ...]] = ("utm_term", "utm_content")

#: Money is quantised HERE, per record, not at serialisation. Every aggregate is then a sum
#: of already-quantised values, so the row strings, the facet strings and the total string
#: agree exactly whichever way the browser adds them up. Quantising only at the end would let
#: a column of rows disagree with its own total by a cent - the discrepancy §4.12 forbids.
_CENTS: Final[Decimal] = Decimal("0.01")


class UtmReportError(Exception):
    """A UTM report that cannot be served, as a machine code (§8).

    Shaped like `stats.FilterError` so a route can hand it straight to the client, and
    deliberately a different type, for `DealReportError`'s reason: a refusal here is about
    budget and about Bitrix24, not about a malformed query string, and the two must stay
    separable in a log.
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
class _Capability:
    """What this portal's build can actually answer, and in which spelling.

    Cached by PORTAL alone, not by viewer: which methods exist, which field names they
    declare and whether a filter is applied are properties of the build. Permission is NOT
    cached here - every request still asks Bitrix24 on the viewer's own token, so a
    salesperson and an administrator share this entry and still see different rows.

    The one permission-shaped thing that does live here is `lead`/`deal` being `None`, and
    that is why `_decide` only ever sets it from a *structural* error (the method is missing)
    and never from `AccessDenied`, which is about the caller and would poison the entry for
    the whole portal.
    """

    lead: EntityDialect | None
    deal: EntityDialect | None
    lead_reason: str
    deal_reason: str
    dimensions: tuple[str, ...]

    def entities(self) -> tuple[EntityDialect, ...]:
        return tuple(d for d in (self.lead, self.deal) if d is not None)


#: `portal_id` -> `(expires_at, capability)`.
_capability_cache: dict[int, tuple[float, _Capability]] = {}

#: `(portal_id, user_id)` -> recent report timestamps.
_report_window: dict[tuple[int, int], deque[float]] = {}

#: Per-portal admission, so one portal can only ever queue behind itself.
_portal_gates: dict[int, asyncio.Semaphore] = {}

_global_gate: asyncio.Semaphore | None = None


def reset_utm_caches() -> None:
    """Drop the capability cache (tests, and an api process re-reading its configuration)."""
    _capability_cache.clear()


def reset_utm_report_rate_limit() -> None:
    """Drop the per-user window and the admission gates (tests)."""
    _report_window.clear()
    _portal_gates.clear()
    global _global_gate
    _global_gate = None


def _gate() -> asyncio.Semaphore:
    """The process-wide admission gate, created on the running loop.

    Built lazily rather than at import: a `Semaphore` binds to the loop that first awaits it,
    and a module-level one created under a different loop is how a test suite ends up
    deadlocked on something that works in production.
    """
    global _global_gate
    if _global_gate is None:
        _global_gate = asyncio.Semaphore(settings.utm_report_concurrency)
    return _global_gate


def _charge_report(portal_id: int, user_id: int) -> bool:
    """One report against the per-viewer window; False means refuse.

    Keyed on `(portal, user)` rather than on the portal alone: this is the page's only
    action and it re-runs on every period click, so a portal-wide window would let one person
    exploring a month lock the page for their whole team.
    """
    now = time.monotonic()
    window = _report_window.setdefault((portal_id, user_id), deque())
    while window and now - window[0] > _REPORT_WINDOW_S:
        window.popleft()
    if len(window) >= settings.utm_report_limit:
        return False
    window.append(now)
    return True


# --- filters -----------------------------------------------------------------------------


def parse_utm_filters(
    params: QueryParams, principal: Principal
) -> tuple[CallFilters, tuple[str, ...]]:
    """The shared filter vocabulary, narrowed to what a UTM scan can honour (§4.13).

    Three differences from `parse_filters`, and two of them are refusals:

    * `direction`, `result` and `line` are `calls` facets. A lead has no direction and no
      telephony line, so a request carrying one is answered `unsupported_filter` naming the
      offending parameter rather than served an answer to a different question. (Verbatim
      from `parse_deal_filters`, which made the same choice for the same reason.)
    * the period cap is `UTM_REPORT_MAX_PERIOD_DAYS`, not `MAX_PERIOD_DAYS`. A live REST scan
      of two entities cannot honestly offer a year inside the SPA's thirty-second timeout.
    * `dimensions` selects which tags form the composite key. It narrows the FOLD, never the
      SCAN - the same pages are fetched either way - so it is a response-size control rather
      than a cost control, and the page says so.

    `employee` is kept: it maps cleanly onto `@assignedById` and is the cheapest cost lever a
    user has when a period is refused as too large.
    """
    for names in _CALL_ONLY_FACETS:
        for name in names:
            if any(value.strip() for value in params.getlist(name)):
                raise FilterError("unsupported_filter", filter=names[0])

    filters = parse_filters(params, principal)
    if filters.days > settings.utm_report_max_period_days:
        raise FilterError("period_too_long", max_days=settings.utm_report_max_period_days)

    requested: list[str] = []
    for raw in params.getlist("dimensions"):
        for piece in raw.split(","):
            name = piece.strip()
            if not name:
                continue
            if name not in DIMENSIONS:
                raise FilterError("bad_dimension", dimension=name)
            if name not in requested:
                requested.append(name)
    # Empty means every dimension, which is also the default the SPA sends nothing for.
    chosen = tuple(name for name in DIMENSIONS if name in requested) if requested else DIMENSIONS
    return filters, chosen


# --- aggregates ---------------------------------------------------------------------------


@dataclass
class _Measures:
    """One entity's counters for one key. `total` is the sum of the other three, always."""

    total: int = 0
    in_progress: int = 0
    won: int = 0
    lost: int = 0

    def add(self, semantic: str) -> None:
        self.total += 1
        if semantic == "S":
            self.won += 1
        elif semantic == "F":
            self.lost += 1
        else:
            self.in_progress += 1

    def merge(self, other: _Measures) -> None:
        self.total += other.total
        self.in_progress += other.in_progress
        self.won += other.won
        self.lost += other.lost

    def wire(self) -> dict[str, int]:
        return {
            "total": self.total,
            "in_progress": self.in_progress,
            "won": self.won,
            "lost": self.lost,
        }


@dataclass
class _Acc:
    """Everything one combination row carries.

    Both money sums are kept because the report picks ONE source for the whole report rather
    than one per row - see `_amounts`.
    """

    leads: _Measures = field(default_factory=_Measures)
    deals: _Measures = field(default_factory=_Measures)
    amount_account: Decimal = Decimal("0.00")
    amount_native: Decimal = Decimal("0.00")
    deals_from_lead: int = 0

    def merge(self, other: _Acc) -> None:
        self.leads.merge(other.leads)
        self.deals.merge(other.deals)
        self.amount_account += other.amount_account
        self.amount_native += other.amount_native
        self.deals_from_lead += other.deals_from_lead

    def weight(self) -> int:
        """Ranking key for the value cap: how much of the report this row accounts for."""
        return self.leads.total + self.deals.total


@dataclass
class _Scan:
    """Everything one scan of both entities produced, before any bucketing."""

    #: Full five-tuple -> aggregate. Bounded by `UTM_SCAN_CAP`, so at most a few thousand.
    raw: dict[tuple[str, ...], _Acc] = field(default_factory=dict)
    #: Local day -> `[leads, deals]`, in the VIEWER's zone.
    days: dict[dt.date, list[int]] = field(default_factory=dict)
    folded: dict[str, int] = field(default_factory=lambda: {KIND_LEAD: 0, KIND_DEAL: 0})
    totals: dict[str, int] = field(default_factory=lambda: {KIND_LEAD: 0, KIND_DEAL: 0})
    #: Records carrying at least one non-empty tag. Feeds the sentence the page prints when
    #: a portal returns no UTM at all - the one failure `crm.item.fields` cannot detect.
    tagged: dict[str, int] = field(default_factory=lambda: {KIND_LEAD: 0, KIND_DEAL: 0})
    account_currencies: set[str] = field(default_factory=set)
    native_currencies: set[str] = field(default_factory=set)
    amount_rows: int = 0
    account_rows: int = 0
    rows_without_amount: int = 0


# --- the scan -------------------------------------------------------------------------------


@dataclass
class _Budget:
    """The wall clock and the operating-time reading, in one place.

    `deadline` is absolute monotonic seconds. It is consulted BEFORE each batch and the batch
    itself is bounded by what remains, because `BitrixClient` has a 120 s httpx timeout and
    `batch()` takes no timeout of its own: a check that only ran between batches could pass
    at 17.9 s and then block for two minutes, and the browser would abort at thirty with a
    generic network error instead of the explanation this endpoint computed.
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
        `crm.item.list` away from this app across the whole portal - including the CRM detail
        tab's own reads and the deal report's. `ThrottleState` is deliberately not reused:
        its `operating_limit_s` was learned from `voximplant.statistic.get`, a different
        method with a different budget.
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

    Never a bare date: Bitrix24 reads one in the PORTAL's timezone, and this app computes its
    bounds in the viewer's, which is the normal case for a business with staff in more than
    one place. An explicit offset removes the question entirely.
    """
    return moment.astimezone(dt.UTC).isoformat()


def _classify(exc: BitrixError) -> UtmReportError:
    """A typed Bitrix24 failure as the machine code the SPA already knows how to render.

    `ExpiredToken` answers **409 `viewer_token_required`**, never 401, for the reason
    `deal_stats.py` states: a 401 makes `apiFetch` run the session exchange, which re-mints
    OUR JWT - a different credential entirely - then re-posts the identical stale Bitrix
    token and clears the session when that fails.
    """
    if isinstance(exc, (ExpiredToken, NoAuthFound, InvalidCredentials)):
        return UtmReportError("viewer_token_required", 409)
    if isinstance(exc, (AccessDenied, UserAccessError)):
        return UtmReportError("crm_no_access", 403)
    if isinstance(exc, InsufficientScope):
        return UtmReportError("insufficient_scope", 403)
    if isinstance(exc, MethodNotFound):
        return UtmReportError("method_missing", 409)
    if isinstance(exc, OperationTimeLimit):
        return UtmReportError("operation_time_limit", 503, retry_after=60)
    if isinstance(exc, QueryLimitExceeded):
        return UtmReportError("query_limit_exceeded", 503, retry_after=30)
    if isinstance(exc, (PortalDeleted, PaymentRequired)):
        return UtmReportError("portal_inactive", 409)
    return UtmReportError("utm_report_failed", 502, error=exc.code)


def _too_large(scan: _Scan, days: int) -> UtmReportError:
    """The one refusal a user will actually meet, carrying everything they need to act.

    The two counts, the shared limit and the period length together are what turn "too large"
    into "narrow this by about a third" - the difference between a refusal a supervisor can
    work with and one they can only complain about. Both entity counts are named separately
    because the lever differs: a portal drowning in leads and a portal drowning in deals need
    different advice.
    """
    leads = scan.totals[KIND_LEAD]
    deals = scan.totals[KIND_DEAL]
    return UtmReportError(
        "utm_scan_too_large",
        400,
        leads=leads,
        deals=deals,
        total=leads + deals,
        max_total=settings.utm_scan_cap,
        days=days,
    )


async def _run_batch(
    client: BitrixClient,
    commands: Sequence[tuple[str, str, dict[str, Any]]],
    budget: _Budget,
    scan: _Scan,
    days: int,
) -> BatchResult:
    """One batch, hard-bounded by what is left of the deadline.

    A timeout here is a refusal with a code, not a dropped connection: the SPA can explain
    "this selection is too large for a live read" and cannot explain a network error.
    """
    remaining = budget.remaining()
    if remaining <= 0:
        raise _too_large(scan, days)
    try:
        result = await asyncio.wait_for(client.batch(list(commands)), timeout=max(0.5, remaining))
    except TimeoutError:
        raise _too_large(scan, days) from None
    except BitrixError as exc:
        raise _classify(exc) from exc
    budget.requests += 1
    budget.observe(result)
    if budget.over_operating_budget():
        raise UtmReportError("operation_time_limit", 503, retry_after=budget.retry_after())
    return result


def _identity_ok(batch: BatchResult, principal: Principal) -> bool:
    """`user.current` must name the viewer whose JWT this is.

    Byte-identical in intent to `POST /calls/{id}/play-url` and to §4.12: the posted token is
    attacker-supplied, and without this check a token belonging to somebody else would answer
    somebody else's report under this session's identity.
    """
    result = batch.get(ME_KEY)
    if not isinstance(result, Mapping):
        return False
    raw = result.get("ID") if "ID" in result else result.get("id")
    try:
        return int(str(raw).strip()) == principal.user_id
    except (TypeError, ValueError):
        return False


# --- capability probe ---------------------------------------------------------------------


def _structurally_absent(error: BitrixError | None) -> bool:
    """True when an error says the METHOD is not there, rather than that YOU are not allowed.

    The distinction decides what may be cached. "This build has no `crm.item.list`" is a
    property of the portal and is safe to remember for an hour; "this user may not read
    leads" is about the caller, and remembering it would serve one salesperson's permissions
    to their whole company.
    """
    return isinstance(error, MethodNotFound)


def _probe_commands(dialects: Iterable[EntityDialect], *, with_identity: bool) -> list[
    tuple[str, str, dict[str, Any]]
]:
    """One cold batch: identity, both field maps, and both honour probes.

    Seven commands for two entities, well inside the batch's fifty and the page cap's
    twenty-five, so the whole capability question costs ONE round trip.
    """
    commands: list[tuple[str, str, dict[str, Any]]] = [me_command()] if with_identity else []
    for dialect in dialects:
        commands.append(fields_command(dialect))
        commands.extend(honour_probe_commands(dialect))
    return commands


def _decide(
    batch: BatchResult, dialect: EntityDialect
) -> tuple[str, tuple[str, ...], str]:
    """One entity's verdict from one probe batch.

    Returns `(outcome, dimensions, reason)` where outcome is:

    * `ok` - believe this dialect; `dimensions` are the tags it declares.
    * `demote` - try the legacy spelling. Reached three different ways, all of them meaning
      "this build does not answer the inferred camelCase question": the method is missing, a
      core field name is missing, or no UTM name is declared. The third is the interesting
      one - `UTM_SOURCE` is documented on `crm.lead.fields`, and only its camelCase
      re-exposure is inferred, so "the universal method has never heard of these" is a reason
      to use the old method, not a reason to give up.
    * `unavailable` - this entity cannot be read here at all. `reason` carries the Bitrix24
      code so the page can say which.
    * `inconclusive` - the dialect works but the honour probe proved nothing (this viewer can
      see no records of this kind at all). Usable, NEVER cacheable.
    """
    error = batch.error(fields_key(dialect.kind))
    if error is not None:
        if _structurally_absent(error):
            return ("demote", (), "method_missing")
        if isinstance(error, (AccessDenied, UserAccessError)):
            return ("unavailable", (), error.code)
        raise _classify(error)

    declared = field_names_present(
        batch.get(fields_key(dialect.kind)), (*core_names(dialect), *utm_names(dialect))
    )
    if not set(core_names(dialect)).issubset(declared):
        return ("demote", (), "core_missing")

    dimensions = tuple(name for name in DIMENSIONS if dialect.utm[name] in declared)
    if not dimensions:
        return ("demote", (), "utm_missing")

    probe_key = honour_key(dialect.kind)
    probe_error = batch.error(probe_key)
    if probe_error is not None:
        if _structurally_absent(probe_error):
            return ("demote", (), "method_missing")
        if isinstance(probe_error, (AccessDenied, UserAccessError)):
            return ("unavailable", (), probe_error.code)
        raise _classify(probe_error)

    verdict = honour_verdict(future=batch.total_of(probe_key))
    if verdict is False:
        return ("demote", (), "filter_ignored")
    if verdict is None:
        return ("inconclusive", dimensions, "")
    return ("ok", dimensions, "")


async def _capability(
    client: BitrixClient,
    principal: Principal,
    budget: _Budget,
    scan: _Scan,
    days: int,
) -> tuple[_Capability, bool]:
    """Which dialects this portal answers, and whether the verdict may be cached.

    Two batches at worst: the universal probe, then - only if something demoted - the legacy
    one. The second is not a formality. `crm.lead.fields` is where `UTM_SOURCE` is actually
    documented, so it is the probe that decides whether this page can exist for this portal
    at all, and its answer is what separates `utm_unsupported` from a silently empty report.
    """
    probe = _probe_commands((LEAD_ITEM, DEAL_ITEM), with_identity=True)
    first = await _run_batch(client, probe, budget, scan, days)
    if not _identity_ok(first, principal):
        raise UtmReportError("invalid_session", 401)

    chosen: dict[str, EntityDialect | None] = {}
    dims: dict[str, tuple[str, ...]] = {}
    reasons: dict[str, str] = {KIND_LEAD: "", KIND_DEAL: ""}
    demoted: list[EntityDialect] = []
    cacheable = True

    for dialect in (LEAD_ITEM, DEAL_ITEM):
        outcome, dimensions, reason = _decide(first, dialect)
        if outcome == "demote":
            demoted.append(legacy_of(dialect))
            continue
        if outcome == "unavailable":
            chosen[dialect.kind] = None
            reasons[dialect.kind] = reason
            cacheable = False
            continue
        chosen[dialect.kind] = dialect
        dims[dialect.kind] = dimensions
        cacheable = cacheable and outcome == "ok"

    if demoted:
        legacy_probe = _probe_commands(demoted, with_identity=False)
        second = await _run_batch(client, legacy_probe, budget, scan, days)
        for dialect in demoted:
            outcome, dimensions, reason = _decide(second, dialect)
            if outcome == "demote" and reason == "utm_missing":
                # This entity reads perfectly well; the portal simply stores no UTM on it.
                # Keeping the dialect with ZERO dimensions is what turns "both entities are
                # tagless" into `utm_unsupported` below instead of `crm_no_access` - which
                # would tell the reader their permissions are wrong when their build is.
                chosen[dialect.kind] = dialect
                dims[dialect.kind] = ()
                continue
            if outcome in ("unavailable", "demote"):
                # There is nothing left to demote TO: the legacy pair is the oldest spelling
                # Bitrix24 has. A `demote` here means this build answers neither vocabulary,
                # which for a lead is a degradation and for a deal ends the report.
                chosen[dialect.kind] = None
                reasons[dialect.kind] = reason or "method_missing"
                cacheable = cacheable and _structurally_absent(second.error(fields_key(dialect.kind)))
                continue
            chosen[dialect.kind] = dialect
            dims[dialect.kind] = dimensions
            cacheable = cacheable and outcome == "ok"

    lead = chosen.get(KIND_LEAD)
    deal = chosen.get(KIND_DEAL)
    if lead is None and deal is None:
        # Indistinguishable on screen from an empty table, and §4.11 forbids a blank state
        # that could equally mean "you may see none" - so it is a refusal with mandated copy.
        raise UtmReportError("crm_no_access", 403)

    # The intersection, not the union: a dimension only one entity stores would render as a
    # permanently empty column on the other half of every funnel row.
    available = [dims[kind] for kind in (KIND_LEAD, KIND_DEAL) if chosen.get(kind) is not None]
    shared = tuple(name for name in DIMENSIONS if all(name in group for group in available))
    if not shared:
        raise UtmReportError("utm_unsupported", 409)

    capability = _Capability(
        lead=lead,
        deal=deal,
        lead_reason=reasons[KIND_LEAD],
        deal_reason=reasons[KIND_DEAL],
        dimensions=shared,
    )
    return capability, cacheable


def _cached_capability(portal_id: int) -> _Capability | None:
    entry = _capability_cache.get(portal_id)
    if entry is not None and entry[0] > time.monotonic():
        return entry[1]
    return None


def _remember_capability(portal_id: int, capability: _Capability) -> None:
    ttl = settings.utm_capability_ttl_sec
    if ttl <= 0:
        return
    _capability_cache[portal_id] = (time.monotonic() + ttl, capability)


# --- folding ---------------------------------------------------------------------------------


def _local_day(raw: Any, zone: Any) -> dt.date | None:
    """`createdTime` as a calendar day in the VIEWER's zone.

    Bitrix24 sends an ISO timestamp with an offset. A value without one is read as UTC rather
    than as the server's local zone - the same rule `resolve_timezone` states: this app never
    lets the machine it happens to run on decide where a day starts.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(zone).date()


def _fold(
    rows: Sequence[Mapping[str, Any]],
    *,
    dialect: EntityDialect,
    scan: _Scan,
    seen: dict[str, set[int]],
    zone: Any,
) -> None:
    """One page of one entity into the accumulator.

    Always keyed on the FULL five-tuple, never on the requested `dimensions`. The projection
    happens once at the end, which keeps `facets` exact for a dimension the caller did not
    ask to group by and costs nothing: the intermediate is bounded by `UTM_SCAN_CAP`.
    """
    kind = dialect.kind
    for row in rows:
        identifier = read_id(row, dialect)
        if identifier is None or identifier in seen[kind]:
            continue
        seen[kind].add(identifier)
        scan.folded[kind] += 1

        key = utm_values(row, dialect, max_chars=settings.utm_value_max_chars)
        if any(value != BUCKET_NONE for value in key):
            scan.tagged[kind] += 1

        entry = scan.raw.get(key)
        if entry is None:
            entry = _Acc()
            scan.raw[key] = entry

        day = _local_day(row.get(dialect.created), zone)
        if day is not None:
            bucket = scan.days.setdefault(day, [0, 0])
            bucket[0 if kind == KIND_LEAD else 1] += 1

        semantic = read_semantic(row, dialect)
        if kind == KIND_LEAD:
            entry.leads.add(semantic)
            continue

        entry.deals.add(semantic)
        if read_lead_id(row, dialect) is not None:
            entry.deals_from_lead += 1

        money = read_money(row, dialect)
        if money.account is None and money.native is None:
            scan.rows_without_amount += 1
            continue
        scan.amount_rows += 1
        if money.account is not None:
            scan.account_rows += 1
            entry.amount_account += money.account.quantize(_CENTS, rounding=ROUND_HALF_UP)
            if money.account_currency:
                scan.account_currencies.add(money.account_currency)
        if money.native is not None:
            entry.amount_native += money.native.quantize(_CENTS, rounding=ROUND_HALF_UP)
            if money.native_currency:
                scan.native_currencies.add(money.native_currency)


async def _run_scan(
    client: BitrixClient,
    budget: _Budget,
    *,
    capability: _Capability,
    filters: CallFilters,
    assigned_to: Sequence[int],
    first_batch: BatchResult | None,
) -> tuple[_Scan, _Capability]:
    """Page both selections, refusing rather than truncating.

    The two entities share one batch and one budget: their page commands are interleaved, so
    a report whose leads and deals both fit in one page costs exactly one round trip.

    Returns the capability alongside the scan because it may have NARROWED: a warm report
    can discover that leads are gone, and the caller has to evict the cached verdict that
    said otherwise.
    """
    zone, _ = resolve_timezone(filters.tz_name)
    start_iso = _iso(filters.start_utc)
    end_iso = _iso(filters.end_utc)
    entities = capability.entities()
    filter_of = {
        d.kind: period_filter(d, start_iso=start_iso, end_iso=end_iso) for d in entities
    }
    scan = _Scan()

    if first_batch is None:
        commands: list[tuple[str, str, dict[str, Any]]] = []
        for dialect in entities:
            commands.extend(
                list_page_commands(
                    dialect,
                    filter_=filter_of[dialect.kind],
                    starts=[0],
                    assigned_to=assigned_to,
                )
            )
        first_batch = await _run_batch(client, commands, budget, scan, filters.days)

    dropped: list[str] = []
    for dialect in entities:
        key = page_key(dialect.kind, 0)
        error = first_batch.error(key)
        if error is not None:
            # A WARM report can meet a portal that turned leads off since the capability was
            # cached. Degrading here rather than refusing keeps that property unconditional:
            # the cold path already answers deals-only, and an hour of 409s in between would
            # be the same fact rendered as a failure. Only a structurally-absent method
            # qualifies - `AccessDenied` is about the caller and still refuses.
            if dialect.kind == KIND_LEAD and _structurally_absent(error) and len(entities) > 1:
                dropped.append(dialect.kind)
                continue
            raise _classify(error)
        scan.totals[dialect.kind] = first_batch.total_of(key) or 0

    if dropped:
        capability = _Capability(
            lead=None,
            deal=capability.deal,
            lead_reason="method_missing",
            deal_reason=capability.deal_reason,
            dimensions=capability.dimensions,
        )
        entities = capability.entities()

    if scan.totals[KIND_LEAD] + scan.totals[KIND_DEAL] > settings.utm_scan_cap:
        raise _too_large(scan, filters.days)

    seen: dict[str, set[int]] = {KIND_LEAD: set(), KIND_DEAL: set()}
    for dialect in entities:
        _fold(
            parse_rows(first_batch.get(page_key(dialect.kind, 0)), universal=dialect.universal),
            dialect=dialect,
            scan=scan,
            seen=seen,
            zone=zone,
        )

    pending: list[tuple[EntityDialect, int]] = [
        (dialect, start)
        for dialect in entities
        for start in range(PAGE_SIZE, scan.totals[dialect.kind], PAGE_SIZE)
    ]
    page_cost = 0.0
    while pending:
        remaining = budget.remaining()
        if remaining <= 0:
            raise _too_large(scan, filters.days)
        # Refuse BEFORE spending the batches that cannot finish, not after. The projection
        # uses the cost actually observed on this portal rather than a guess, so a fast
        # portal is allowed the whole cap and a slow one is stopped early with the same
        # explanation the preflight gate gives.
        if page_cost > 0 and len(pending) * page_cost > remaining:
            raise _too_large(scan, filters.days)

        size = _PAGE_BATCH
        if page_cost > 0:
            size = max(1, min(_PAGE_BATCH, int(remaining / page_cost)))
        chunk, pending = pending[:size], pending[size:]

        commands = []
        for dialect, start in chunk:
            commands.extend(
                list_page_commands(
                    dialect,
                    filter_=filter_of[dialect.kind],
                    starts=[start],
                    assigned_to=assigned_to,
                )
            )
        began = time.monotonic()
        batch = await _run_batch(client, commands, budget, scan, filters.days)
        page_cost = max(page_cost, (time.monotonic() - began) / max(1, len(chunk)))

        for dialect, start in chunk:
            key = page_key(dialect.kind, start)
            error = batch.error(key)
            if error is not None:
                # A silently dropped page under-counts one UTM row and reads to the user as
                # data, with nothing on screen to say so. Refusing is the only honest answer.
                raise _classify(error)
            _fold(
                parse_rows(batch.get(key), universal=dialect.universal),
                dialect=dialect,
                scan=scan,
                seen=seen,
                zone=zone,
            )

    return scan, capability


# --- the bucket ladder ------------------------------------------------------------------------


def _facets(scan: _Scan) -> dict[str, dict[str, _Acc]]:
    """Exact per-dimension marginals, computed BEFORE any bucketing.

    This is what makes the whole design work. The filter controls are populated from here, so
    their option lists are the real values with their real counts and NEVER shrink when a
    filter is applied - the standard cross-filter trap, where selecting one source collapses
    the medium list to one entry and the reader cannot get back. It is also what lets the
    page state an exact distinct count for a dimension that was later collapsed out of the
    key, which turns an apology into a fact.
    """
    out: dict[str, dict[str, _Acc]] = {name: {} for name in DIMENSIONS}
    for key, acc in scan.raw.items():
        for index, name in enumerate(DIMENSIONS):
            bucket = out[name].setdefault(key[index], _Acc())
            bucket.merge(acc)
    return out


def _ranked(values: Mapping[str, _Acc]) -> list[str]:
    """Values by weight descending, ties broken by the value itself so paging is stable."""
    return sorted(values, key=lambda value: (-values[value].weight(), value))


def _remaps(facets: Mapping[str, Mapping[str, _Acc]]) -> dict[str, dict[str, str]]:
    """Per dimension, the value rewrites the `other` bucket implies.

    Rung 2 of the ladder. Everything past the top `UTM_VALUE_CAP` for a dimension is rewritten
    to one reserved bucket. Nothing is dropped: the counts still land, so `sum(rows)` still
    equals `totals` exactly. `deals.py`'s "Other" row and `EmployeeBars`' ninth employee make
    the same move, and for the same stated reason - a residue bucket is a row a reader can
    see and reason about, where a silently missing row is not.
    """
    cap = settings.utm_value_cap
    out: dict[str, dict[str, str]] = {}
    for name, values in facets.items():
        if len(values) <= cap:
            out[name] = {}
            continue
        keep = set(_ranked(values)[:cap])
        out[name] = {value: BUCKET_OTHER for value in values if value not in keep}
    return out


def _project(
    scan: _Scan,
    *,
    dimensions: Sequence[str],
    remaps: Mapping[str, Mapping[str, str]],
    collapsed: Sequence[str],
) -> dict[tuple[str, ...], _Acc]:
    """The full five-tuple accumulator folded onto the dimensions the caller asked for."""
    positions = [(DIMENSIONS.index(name), name) for name in dimensions]
    out: dict[tuple[str, ...], _Acc] = {}
    for key, acc in scan.raw.items():
        projected = tuple(
            BUCKET_COLLAPSED
            if name in collapsed
            else remaps[name].get(key[index], key[index])
            for index, name in positions
        )
        entry = out.get(projected)
        if entry is None:
            entry = _Acc()
            out[projected] = entry
        entry.merge(acc)
    return out


def _combinations(
    scan: _Scan, *, dimensions: Sequence[str], remaps: Mapping[str, Mapping[str, str]]
) -> tuple[dict[tuple[str, ...], _Acc], tuple[str, ...]]:
    """Rung 3: lift the finest dimensions out of the key until the row count fits.

    Collapsing a dimension cannot change any OTHER dimension's marginal - that is a property
    of a projection, not a hope - so `utm_source` reads bit-identically whether or not
    `utm_term` survived. The order is published (`_COLLAPSE_ORDER`) so the page can say in
    advance which control it will lose.
    """
    collapsed: list[str] = []
    rows = _project(scan, dimensions=dimensions, remaps=remaps, collapsed=collapsed)
    for name in _COLLAPSE_ORDER:
        if len(rows) <= settings.utm_combination_cap:
            break
        if name not in dimensions:
            continue
        collapsed.append(name)
        rows = _project(scan, dimensions=dimensions, remaps=remaps, collapsed=collapsed)
    return rows, tuple(collapsed)


# --- the wire form ----------------------------------------------------------------------------


def _amounts(scan: _Scan) -> tuple[str, dict[str, Any]]:
    """Which money column to publish, and whether to publish one at all.

    Two decisions, and the second one is the reason this function exists.

    **Which source.** `opportunityAccount` is Bitrix24's own conversion into the portal's
    account currency, computed with the portal's own rates, and it is the number the portal's
    own CRM reports print. Using it means agreeing with Bitrix24 rather than inventing a
    second exchange rate - `crm_context.py`'s standing doctrine. It is chosen only when EVERY
    amount-bearing row carried it: choosing per row would mix two denominations inside one
    column the moment a single record was missing the pair.

    **Whether at all.** The account currency is a PORTAL setting, so the set of values
    observed must be a singleton. If it is not - or if the native fallback is in play and the
    portal trades in more than one currency - then there is no honest single number, and the
    page HIDES the amount column, the average-deal column and every money chart rather than
    printing a sum across currencies that nobody converted. A missing column a sentence
    explains is recoverable; a wrong total a supervisor acts on is not.
    """
    use_account = scan.amount_rows > 0 and scan.account_rows == scan.amount_rows
    source = "account" if use_account else "native"
    currencies = sorted(scan.account_currencies if use_account else scan.native_currencies)
    trusted = len(currencies) <= 1
    return source, {
        "trusted": trusted,
        "source": source,
        "currency": currencies[0] if currencies else "",
        "currencies": currencies,
        "rows_without_amount": scan.rows_without_amount,
    }


def _money(acc: _Acc, source: str) -> str:
    """One aggregate's amount, as a decimal STRING at scale two.

    Never a JSON float. Every contributing record was quantised on the way in, so this is a
    sum of exact cents and the browser's own column sum agrees with it digit for digit.
    """
    value = acc.amount_account if source == "account" else acc.amount_native
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _acc_wire(acc: _Acc, source: str) -> dict[str, Any]:
    return {
        "leads": acc.leads.wire(),
        "deals": acc.deals.wire(),
        "amount": _money(acc, source),
        "deals_from_lead": acc.deals_from_lead,
    }


def _facet_wire(
    name: str,
    values: Mapping[str, _Acc],
    *,
    source: str,
    selected: bool,
    collapsed: bool,
) -> dict[str, Any]:
    """One dimension's marginal, top `UTM_VALUE_CAP` plus the residue.

    The residue is emitted as a real row rather than omitted, so `sum(values) == totals` holds
    for every dimension independently - the invariant that catches an off-by-one in the ladder
    where nothing else would.
    """
    cap = settings.utm_value_cap
    order = _ranked(values)
    kept, rest = order[:cap], order[cap:]
    rows = [{"value": value, **_acc_wire(values[value], source)} for value in kept]
    if rest:
        residue = _Acc()
        for value in rest:
            residue.merge(values[value])
        rows.append({"value": BUCKET_OTHER, **_acc_wire(residue, source)})
    return {
        "dimension": name,
        "distinct_total": len(values),
        "distinct_kept": len(kept),
        "selected": selected,
        "collapsed": collapsed,
        "values": rows,
    }


def _days_wire(scan: _Scan, filters: CallFilters) -> list[dict[str, Any]]:
    """One entry per calendar day of the period, dense.

    A day with nothing on it is drawn as an empty slot, never skipped: a series that silently
    drops days lies about the shape of a week, which is the rule `CallsPerDayChart` already
    states for its own bars.
    """
    out: list[dict[str, Any]] = []
    day = filters.date_from
    while day <= filters.date_to:
        leads, deals = scan.days.get(day, (0, 0))
        out.append({"date": day.isoformat(), "leads": leads, "deals": deals})
        day += dt.timedelta(days=1)
    return out


def _entity_scan_wire(
    scan: _Scan, capability: _Capability, kind: str
) -> dict[str, Any]:
    dialect = capability.lead if kind == KIND_LEAD else capability.deal
    reason = capability.lead_reason if kind == KIND_LEAD else capability.deal_reason
    if dialect is None:
        return {"available": False, "reason": reason, "folded": 0, "total": 0}
    return {
        "available": True,
        "reason": "",
        "folded": scan.folded[kind],
        "total": scan.totals[kind],
        "dialect": dialect.name,
        # The one thing `crm.item.fields` cannot prove: that `select` actually RETURNS the
        # names it declares. Zero tagged rows over a non-zero scan is reported rather than
        # detected, because "nobody used tagged links" and "this build drops the field" are
        # indistinguishable in a response that omits null keys.
        "tagged_rows": scan.tagged[kind],
        "utm_fields": list(capability.dimensions),
    }


def _build_response(
    *,
    filters: CallFilters,
    dimensions: Sequence[str],
    capability: _Capability,
    scan: _Scan,
    budget: _Budget,
    from_cache: bool,
    assigned_to: Sequence[int],
) -> dict[str, Any]:
    """Counts and decimal strings only; every ratio is the browser's to compute.

    Conversion, average deal size and every share are functions of integers already on the
    wire. Deriving them in the browser keeps the wire honest - every number on it is a count
    somebody could verify - and lets the page's toggles re-render without a REST round trip,
    which on this page costs a live CRM scan. `DealStageTable` states the same rule.
    """
    facets = _facets(scan)
    remaps = _remaps(facets)
    rows, collapsed = _combinations(scan, dimensions=dimensions, remaps=remaps)
    source, amounts = _amounts(scan)

    totals = _Acc()
    for acc in scan.raw.values():
        totals.merge(acc)

    ordered = sorted(rows, key=lambda key: (-rows[key].weight(), key))
    described = filters.describe()
    return {
        "range": described["range"],
        # Hand-built rather than echoed: `direction`/`result`/`line` are `calls` facets this
        # endpoint refuses outright, so repeating `describe()["filters"]` would state four
        # things about the scan that are not true of it.
        "filters": {"employees": list(assigned_to)},
        "dimensions": list(dimensions),
        "buckets": {
            "none": BUCKET_NONE,
            "other": BUCKET_OTHER,
            "collapsed": BUCKET_COLLAPSED,
        },
        "facets": [
            _facet_wire(
                name,
                facets[name],
                source=source,
                selected=name in dimensions,
                collapsed=name in collapsed,
            )
            for name in capability.dimensions
        ],
        "combinations": [{"k": list(key), **_acc_wire(rows[key], source)} for key in ordered],
        "totals": _acc_wire(totals, source),
        "days": _days_wire(scan, filters),
        "amounts": amounts,
        "scan": {
            "leads": _entity_scan_wire(scan, capability, KIND_LEAD),
            "deals": _entity_scan_wire(scan, capability, KIND_DEAL),
            "scanned_total": scan.folded[KIND_LEAD] + scan.folded[KIND_DEAL],
            "scan_cap": settings.utm_scan_cap,
            "value_cap": settings.utm_value_cap,
            "combination_cap": settings.utm_combination_cap,
            "value_max_chars": settings.utm_value_max_chars,
            "combinations": len(rows),
            "collapsed": list(collapsed),
            "rest_requests": budget.requests,
            "from_cache": from_cache,
        },
    }


# --- orchestration ------------------------------------------------------------------------------


async def _report(
    principal: Principal,
    portal: Portal,
    filters: CallFilters,
    dimensions: Sequence[str],
    *,
    viewer_token: str,
    correlation_id: uuid.UUID,
) -> dict[str, Any]:
    """One report, from admission to wire form."""
    budget = _Budget(deadline=time.monotonic() + settings.utm_scan_deadline_sec)

    # §4.7 applied to a CRM read: an `own` viewer sees their own records and nothing else.
    # Pinned server-side rather than trusted to the control, and on BOTH entity legs - a
    # report that pinned only the deals would let an `own` viewer count the whole company's
    # leads in the denominator of their own conversion rate.
    assigned_to: tuple[int, ...] = (
        filters.employees if principal.access == "all" else (principal.user_id,)
    )

    capability = _cached_capability(portal.id)
    scan_probe = _Scan()

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
        if capability is not None:
            # The warm path: identity proof and the first page of BOTH entities in one
            # request, so a portal whose month fits in a page answers in one round trip.
            commands: list[tuple[str, str, dict[str, Any]]] = [me_command()]
            start_iso = _iso(filters.start_utc)
            end_iso = _iso(filters.end_utc)
            for dialect in capability.entities():
                commands.extend(
                    list_page_commands(
                        dialect,
                        filter_=period_filter(dialect, start_iso=start_iso, end_iso=end_iso),
                        starts=[0],
                        assigned_to=assigned_to,
                    )
                )
            first = await _run_batch(client, commands, budget, scan_probe, filters.days)
            if not _identity_ok(first, principal):
                raise UtmReportError("invalid_session", 401)
            from_cache = True
        else:
            capability, cacheable = await _capability(
                client, principal, budget, scan_probe, filters.days
            )
            first = None
            from_cache = False

        scan, scanned = await _run_scan(
            client,
            budget,
            capability=capability,
            filters=filters,
            assigned_to=assigned_to,
            first_batch=first,
        )
    if scanned is not capability:
        # The cached verdict is now known to be wrong. Evicting rather than rewriting it
        # keeps one rule about this cache: it is only ever filled from a full probe.
        _capability_cache.pop(portal.id, None)
        capability = scanned
    elif not from_cache:
        # The verdict is remembered HERE rather than straight after the probe, because the
        # last piece of evidence it needs comes from the scan: a `>=created: 2999` answering
        # zero only proves the filter held if this viewer can read records at all, and a
        # selection that returned rows proves exactly that. Asking Bitrix24 the same question
        # directly - an unfiltered list, counting the whole table - is what this page used to
        # do, and it is what made a production portal answer `operation_time_limit` forever.
        #
        # A period with nothing in it therefore leaves the portal un-cached and the next open
        # cold. That is the honest trade and it is cheap now: the cold path is two field maps
        # and two selections that match nothing.
        if cacheable and scan.totals[KIND_LEAD] + scan.totals[KIND_DEAL] > 0:
            _remember_capability(portal.id, capability)

    # A dimension the caller asked to group by that this portal does not store would be a
    # column of nothing but the `none` bucket. Narrow to what both the portal and the caller
    # named; the response echoes the result so the page knows which controls to draw.
    chosen = tuple(name for name in dimensions if name in capability.dimensions)
    if not chosen:
        chosen = capability.dimensions

    return _build_response(
        filters=filters,
        dimensions=chosen,
        capability=capability,
        scan=scan,
        budget=budget,
        from_cache=from_cache,
        assigned_to=assigned_to,
    )


async def load_utm_report(
    principal: Principal,
    portal: Portal,
    filters: CallFilters,
    dimensions: Sequence[str],
    *,
    viewer_token: str,
    correlation_id: uuid.UUID,
) -> dict[str, Any]:
    """Admission, then the report (§4.13).

    Three guards, in this order and for three different reasons - `deal_stats.py`'s ladder
    verbatim, because this page shares the egress address and the operating budget with it:

    * the **process gate** protects the box and the shared egress IP - the leaky bucket is
      counted per source address and every tenant of this deployment shares one, so a
      per-portal limit alone does nothing when twenty portals click at once;
    * the **portal gate** is a `Semaphore(1)` so a portal can only ever queue behind itself,
      and one busy customer cannot hold a slot another customer is waiting for;
    * the **per-viewer window** is charged LAST, once the request is actually about to spend
      REST. Charging it before the gates would burn one of a user's twelve slots on a refusal
      that protected nothing.
    """
    gate = _gate()
    try:
        await asyncio.wait_for(gate.acquire(), _ADMIT_WAIT_S)
    except TimeoutError:
        raise UtmReportError("retry", 503, retry_after=10) from None
    try:
        portal_gate = _portal_gates.setdefault(portal.id, asyncio.Semaphore(1))
        try:
            await asyncio.wait_for(portal_gate.acquire(), _ADMIT_WAIT_S)
        except TimeoutError:
            raise UtmReportError("retry", 503, retry_after=5) from None
        try:
            if not _charge_report(portal.id, principal.user_id):
                raise UtmReportError("rate_limited", 429, retry_after=60)
            return await _report(
                principal,
                portal,
                filters,
                dimensions,
                viewer_token=viewer_token,
                correlation_id=correlation_id,
            )
        finally:
            portal_gate.release()
    finally:
        gate.release()
