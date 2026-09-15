"""The worker's units of work, as plain async functions (§5.9, decision 15).

**Nothing here imports a scheduler, and nothing here takes a non-primitive argument.**
That is the whole design of §5.9's "Celery swap": a second backend is one new file
whose tasks call these same functions with the same `int` arguments. If a job took a
`Fence`, a session or a client, the swap would be a rewrite - and the worker would hold
state outside the database, which decision 15 forbids ("durable state instead of a job
queue: every unit of work is derivable from `portal_sync` / `portals` columns").

This module **composes; it never re-decides**. How far a cursor may advance is
`sync/fetch.py`; what one visit does to the head, the forward cursor, the backfill and
the late-update passes is `sync/head_fetch.py`, `incremental.py`, `backfill.py`,
`rescan.py` and `employees_refresh.py`; how fast a portal may be asked is
`sync/throttle.py`; who may write at all is `sync/lease.py`; how a tenant's rows are
destroyed and *proven* destroyed is `sync/purge.py`. What is left here - and it is the
part that has to be right - is four things those modules deliberately do not own:

1. **Dispatch.** The tick never awaits sync work and never shares an APScheduler job
   id. Every job id carries `max_instances=1`, so four portals dispatched under one id
   run one and drop three, leaving them leased and idle until the lease expires and
   each is charged a crash it never had. Dispatch is `asyncio.create_task` under a
   process-wide `Semaphore(GLOBAL_PORTAL_CONCURRENCY)`, keyed per portal.
2. **Order and due-ness.** §5.9 fixes the order, and it is not arbitrary: what a human
   is waiting for first (rows the SPA asked to re-read, then the newest calls), the
   unbounded work after it (the backfill, capped per visit), the maintenance nobody is
   watching last - so a portal that runs out of the account's shared operating-time
   budget loses the least valuable work rather than the most.
3. **Terminal states (§5.8).** The phase modules *report* Bitrix24 failures instead of
   raising them; deciding that a failure means "this credential is dead" or "this build
   ignores `>ID`" and parking the portal at `next_run_at='infinity'` happens here, in
   one place, branching on the exception TYPE and on the method it happened to.
4. **The exit.** One visit, one `next_run_at`, always - never a loop inside the worker.

One seam deserves naming because it is the only place two modules could double-count:
`sync/lease.py::release_lease` owns the *failure accounting* columns
(`consecutive_failures`, `throttle_hits`, `last_error_*` and the 6 h pause) while
`sync/throttle.py` owns the *rate* columns (`batch_pages`, `clean_visits`,
`operating_*`) and computes the delay. `_close_visit` therefore writes only the rate
half and hands the computed `next_run_at` to `release_lease`, which applies the
counters exactly once.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.bitrix import crm_items
from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.crm_items import DEAL_ITEM, DEAL_LEGACY, LEAD_ITEM, LEAD_LEGACY, MirrorDialect
from app.bitrix.deals import CRM_STATUS_LIST
from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    EntityTypeNotSupported,
    ExpiredToken,
    InsufficientScope,
    IntranetUserOnly,
    InvalidGrant,
    MethodNotFound,
    NoAuthFound,
    OperationTimeLimit,
    PaymentRequired,
    PortalDeleted,
    QueryLimitExceeded,
    TransportError,
    UnknownBitrixError,
    UserAccessError,
    classify,
)
from app.bitrix.oauth import CredentialUnavailable, MemberIdMismatch, with_portal_token
from app.bitrix.statistic import STATISTIC_METHOD
from app.bitrix.users import USER_ADMIN, USER_GET, parse_admin_flag
from app.config import settings
from app.db.models import AppFlag, Portal, PortalSync
from app.db.session import control_txn
from app.logging import get_logger, set_request_id
from app.services.portals import (
    mark_uninstalled,
    record_event,
    record_placements,
    set_token_status,
)
from app.sync import budgets as method_budgets
from app.sync import crm_backfill, crm_dict, crm_lanes, crm_sweep, throttle
from app.sync.backfill import run_backfill
from app.sync.budgets import MethodBudget
from app.sync.crm_fetch import fetch_ranges
from app.sync.crm_upsert import upsert_items
from app.sync.employees_refresh import run_employees_refresh
from app.sync.head_fetch import run_head_fetch
from app.sync.incremental import run_incremental
from app.sync.lease import (
    WORKER_ID,
    Fence,
    FenceLost,
    acquire_leases,
    fenced_update,
    heartbeat,
    release_lease,
)
from app.sync.purge import outside_incomplete_cooldown, purge_crm_data, purge_portal_data
from app.sync.purge import purge_crm_contexts as _purge_crm_context_rows
from app.sync.purge import purge_rest_log as _purge_rest_log_rows
from app.sync.rescan import run_id_window_rescan, run_record_recheck, run_refresh_requested

__all__ = [
    "JOBS",
    "purge_crm",
    "purge_crm_contexts",
    "purge_portal",
    "purge_rest_log",
    "sweep_inferred_uninstalls",
    "sync_portal",
    "tick",
]

log = get_logger(__name__)


# --------------------------------------------------------------------------- constants

#: `app.info` is the daily liveness probe of §5.8; `user.admin` rides the same batch.
APP_INFO_METHOD: Final[str] = "app.info"

#: §5.2: `head_fetch` runs while the portal is in one of these two states and is
#: idempotent, so a crash between its two steps costs one re-run and nothing else.
_HEAD_STATES: Final[frozenset[str]] = frozenset({"pending", "head"})
_RUNNING: Final[str] = "running"

#: §5.9: "`next_run_at = now() + 2 s` while backfilling" - the yield that stops one busy
#: portal from holding a concurrency slot for the length of its whole history.
BACKFILL_YIELD_SECONDS: Final[float] = 2.0

#: §5.7.2 / §5.8: the once-a-day passes inside a visit.
DAILY_SECONDS: Final[float] = 86_400.0

#: §5.8: "after 3 consecutive connect/DNS/TLS failures perform one refresh under the
#: single-flight lock purely to re-learn `client_endpoint`" - the portal was renamed.
TRANSPORT_FAILURES_BEFORE_REFRESH: Final[int] = 3

#: Highest rung `_previous_ladder_step` will read back. 2 s x 2**8 = 512 s is already
#: past §5.6's 300 s ceiling, so counting further would only cost loop iterations.
_MAX_LADDER_STEP: Final[int] = 8

#: §5.8 terminal `token_status` values. Plain strings: `portals_token_status_chk` is
#: the authority and `services/portals.py` validates every write against it.
_REAUTH_REQUIRED: Final[str] = "reauth_required"
_NO_STATS_PERMISSION: Final[str] = "no_stats_permission"
_METHOD_MISSING: Final[str] = "method_missing"
_FILTER_UNSUPPORTED: Final[str] = "filter_unsupported"
#: Not a `token_status`: §5.8's `PORTAL_DELETED` is the uninstall transition of §4.9.
_UNINSTALLED: Final[str] = "uninstalled"

#: Columns `sync/lease.py::release_lease` writes itself. Writing them here too would
#: increment them twice - see the module docstring's "one seam".
_RELEASE_OWNED: Final[frozenset[str]] = frozenset(
    {
        "consecutive_failures",
        "throttle_hits",
        "next_run_at",
        "last_error_code",
        "last_error_text",
        "last_error_at",
    }
)

#: One sweep must not mark a thousand portals uninstalled in a single burst; the job is
#: daily and the remainder is picked up tomorrow (durable state, §5.9).
_SWEEP_LIMIT: Final[int] = 200

#: The `app_flags` row whose `enabled = false` stops every CRM request the worker makes.
CRM_MIRROR_FLAG: Final[str] = "crm_mirror"

#: CRM refusals the portal decides rather than faults: CRM or leads switched off, a build
#: without the method. The lane asks again tomorrow (`crm_lanes.unavailable`).
_CRM_UNAVAILABLE: Final[tuple[type[BitrixError], ...]] = (
    AccessDenied,
    UserAccessError,
    EntityTypeNotSupported,
    IntranetUserOnly,
    MethodNotFound,
)

#: Shares of a method's operating limit the CRM lanes may spend (§5.10). Until the mirror
#: replaces them, the live Deals and Sources reports spend the same `crm.item.list` budget under
#: the same application, and a backfill that took all of it would answer their readers
#: `operation_time_limit` - which is how the live report failed on production once (b8393bd).
CRM_SWEEP_SHARE: Final[float] = 0.6
CRM_BACKFILL_SHARE: Final[float] = 0.5

#: `(sweep lane, backfill lane, universal dialect, legacy dialect)` per mirrored entity.
_CRM_ENTITIES: Final[tuple[tuple[str, str, MirrorDialect, MirrorDialect], ...]] = (
    (crm_lanes.DEAL_SWEEP, crm_lanes.DEAL_BACKFILL, DEAL_ITEM, DEAL_LEGACY),
    (crm_lanes.LEAD_SWEEP, crm_lanes.LEAD_BACKFILL, LEAD_ITEM, LEAD_LEGACY),
)


# ------------------------------------------------------------------ dispatch registry

#: Portals with a visit in flight IN THIS PROCESS. Two purposes: it is the reference
#: that keeps the task alive (asyncio holds only a weak one, so a fire-and-forget task
#: can be collected mid-visit), and it is what makes "free slots" a fact rather than a
#: guess - leasing a portal we cannot dispatch is what earns a phantom crash charge.
_inflight: dict[int, asyncio.Task[None]] = {}

#: The same for purges; at most one at a time. It is a long series of 10 000-row
#: deletes against tables the api reads, and a second one would only add contention.
_purging: dict[int, asyncio.Task[None]] = {}

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _dispatch_semaphore() -> asyncio.Semaphore:
    """The process-wide `Semaphore(GLOBAL_PORTAL_CONCURRENCY)` of §5.9.

    Rebuilt when the running loop changes: a semaphore created under one loop is
    unusable under another, and tests (and `uvicorn --reload`) create several.
    """
    # One gate for the whole process by design: the limit is about the shared
    # Bitrix24 source IP, not about this coroutine.
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(settings.global_portal_concurrency)
        _semaphore_loop = loop
    return _semaphore


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _due(last: dt.datetime | None, seconds: float) -> bool:
    """"Has it been `seconds` since `last`?" - `None` means "never ran", i.e. due."""
    if last is None:
        return True
    if last.tzinfo is None:  # a naive timestamp can only come from a hand edit
        last = last.replace(tzinfo=dt.UTC)
    return (_utcnow() - last).total_seconds() >= seconds


class _TerminalStop(Exception):
    """A §5.8 terminal state discovered mid-visit; carries what to write on exit.

    An exception rather than a flag because it must unwind every remaining phase at
    once: once the credential is known to be dead, or the build known to ignore `>ID`,
    every further request is guaranteed to fail and would spend operating time the
    whole customer account shares.

    `audited` marks a state a phase module has *already* written (`sync/fetch.py`'s
    filter guard parks the portal itself): the exit still has to re-assert the block,
    because `release_lease` writes `next_run_at` unconditionally, but it must not write
    a second `portal_events(sync_blocked)` for one event.
    """

    def __init__(self, token_status: str, error: BitrixError, *, audited: bool = False) -> None:
        super().__init__(token_status)
        self.token_status = token_status
        self.error = error
        self.audited = audited


# --------------------------------------------------------------------------- the visit


@dataclass
class _Visit:
    """One portal's sync visit: the fenced identity, the cursors, the rate state.

    The `portal_sync` values are read once, here, and handed to each phase module -
    which is safe *because* of the fence: while the lease is ours nobody else writes
    them, and the moment that stops being true `fenced_update` raises `FenceLost` and
    the whole visit is abandoned (decision 13).
    """

    portal_id: int
    member_id: str
    endpoint: str
    token_user_id: int | None
    correlation_id: uuid.UUID
    fence: Fence
    state: throttle.ThrottleState

    high_id: int
    low_id: int | None
    rescan_from_id: int | None
    backfill_status: str
    backfill_done: int
    last_incremental_at: dt.datetime | None
    last_rescan_at: dt.datetime | None
    last_recheck_at: dt.datetime | None
    last_appinfo_at: dt.datetime | None
    #: `portals.token_admin_verified_at` - the worker's OWN clock for the daily
    #: `user.admin` re-verification, deliberately not `portal_sync.last_appinfo_at`
    #: (which the `/app/` handler also writes). See `_phase_daily_probe`.
    token_admin_verified_at: dt.datetime | None
    last_error_code: str | None
    #: `portal_sync.last_error_at` / `next_run_at` AS THE PREVIOUS VISIT LEFT THEM. Read
    #: only by `_backoff_attempt`: their difference is the rung of §5.6's 503 ladder the
    #: last visit climbed to, and `portal_sync` has no column that carries it.
    last_error_at: dt.datetime | None
    next_run_at: dt.datetime | None
    consecutive_failures: int

    #: Set by the statistics operating-time guard; when present it replaces the "clean
    #: visit" decision at close time, so a soft-limit stop cannot count as a clean visit.
    decision: throttle.ThrottleDecision | None = None
    #: `operating_*` observed mid-visit that no phase committed.
    pending: dict[str, Any] = field(default_factory=dict)
    #: `sync_method_budgets` as loaded at open and updated during the visit (§5.6): the
    #: operating-time state of every method except `voximplant.statistic.get`.
    method_budgets: dict[str, MethodBudget] = field(default_factory=dict)
    #: Methods whose phases are over for this visit - a soft limit, a 429, or a phase that
    #: reported it stopped. Other methods go on: each spends a budget of its own.
    stopped_methods: set[str] = field(default_factory=set)
    #: Methods that made at least one request in this visit.
    used_methods: set[str] = field(default_factory=set)
    #: Methods whose budget row this visit changed.
    touched_methods: set[str] = field(default_factory=set)
    #: The first non-terminal, non-throttle Bitrix24 failure of the visit. Reported at
    #: the exit so §5.6's "after 10 consecutive failures pause 6 h" can count it, but
    #: never aborts the visit: a failed page is re-read next time (§5.2 prefix rule).
    soft_error: BitrixError | None = None
    #: The method the current phase is talking to - `AccessDenied` is terminal only on
    #: `voximplant.statistic.get` (§5.8), never on `user.get`.
    method: str = ""
    #: 503s seen in THIS visit; `throttle.on_query_limit` uses it as the backoff
    #: exponent, so a lifetime counter cannot start a busy portal at the ceiling.
    query_limit_hits: int = 0
    #: A phase reported it still had work; come back in seconds, not minutes.
    more: bool = False
    #: CRM lanes run in this visit: the portal mirrors CRM, its administrator has not turned
    #: CRM analytics off, and the fleet kill switch is on (§4.14).
    crm_active: bool = False
    #: `crm_lanes` as loaded at open and advanced during the visit (§5.10).
    lanes: dict[str, crm_lanes.Lane] = field(default_factory=dict)
    #: CRM batches this visit may still send (`CRM_BATCHES_PER_VISIT`).
    crm_batches: int = 0

    @property
    def backfilling(self) -> bool:
        return self.backfill_status == _RUNNING


async def _open_visit(portal_id: int, correlation_id: uuid.UUID) -> _Visit | None:
    """Claim the run: verify the lease is ours, stamp `run_started_at`, read the state.

    `run_started_at` is what tells the *next* lease expiry apart from a dispatch miss
    (§3, §5.9): with it set, an expired lease is a crashed run and is charged; without
    it, the tick simply leases the portal again free of charge. Writing it through
    `fenced_update` also proves - before a single request is made - that the lease and
    the generation we were dispatched with are still current.
    """
    async with control_txn() as session:
        row = (
            await session.execute(
                select(Portal, PortalSync)
                .join(PortalSync, PortalSync.portal_id == Portal.id)
                .where(Portal.id == portal_id)
            )
        ).one_or_none()
        if row is None:
            log.warning("sync: dispatched for an unknown portal", extra={"portal_id": portal_id})
            return None
        portal, sync = row

        expires = sync.lease_expires_at
        if sync.lease_owner != WORKER_ID or expires is None or expires <= _utcnow():
            # The dispatch lost a race with a lease expiry or another worker. Returning
            # is the only correct answer: writing anything now would be exactly the
            # stale-runner write decision 13 exists to stop.
            log.info(
                "sync: lease is no longer ours; abandoning the dispatch",
                extra={"portal_id": portal_id, "lease_owner": sync.lease_owner},
            )
            return None
        # S105: `token_status` is a state enum (§3 `portals_token_status_chk`), not a secret.
        if (
            portal.status != "active"
            or portal.purge_pending
            or portal.token_status != "ok"  # noqa: S105
        ):
            # State moved between the lease and the dispatch (an uninstall event, a
            # terminal status written by another path). Never sync into a pending purge.
            log.info(
                "sync: portal is no longer syncable",
                extra={
                    "portal_id": portal_id,
                    "status": portal.status,
                    "token_status": portal.token_status,
                    "purge_pending": portal.purge_pending,
                },
            )
            return None

        fence = Fence(portal_id=portal_id, owner=WORKER_ID, generation=int(sync.sync_generation))
        await fenced_update(session, fence, {"run_started_at": func.now()})
        budgets = await method_budgets.load_budgets(session, portal_id)
        crm_active = await _crm_active(session, portal)
        lanes = await crm_lanes.ensure_lanes(session, portal_id, _utcnow()) if crm_active else {}

    return _Visit(
        portal_id=portal_id,
        member_id=portal.member_id,
        endpoint=portal.client_endpoint,
        token_user_id=portal.token_user_id,
        correlation_id=correlation_id,
        fence=fence,
        state=throttle.ThrottleState.from_row(sync, portal.capabilities),
        high_id=int(sync.high_id or 0),
        low_id=None if sync.low_id is None else int(sync.low_id),
        rescan_from_id=None if sync.rescan_from_id is None else int(sync.rescan_from_id),
        backfill_status=sync.backfill_status,
        backfill_done=int(sync.backfill_done or 0),
        last_incremental_at=sync.last_incremental_at,
        last_rescan_at=sync.last_rescan_at,
        last_recheck_at=sync.last_recheck_at,
        last_appinfo_at=sync.last_appinfo_at,
        token_admin_verified_at=portal.token_admin_verified_at,
        last_error_code=sync.last_error_code,
        last_error_at=sync.last_error_at,
        next_run_at=sync.next_run_at,
        consecutive_failures=int(sync.consecutive_failures or 0),
        method_budgets=budgets,
        crm_active=crm_active,
        lanes=lanes,
        crm_batches=settings.crm_batches_per_visit,
    )


async def _crm_active(session: AsyncSession, portal: Portal) -> bool:
    """Whether this visit mirrors CRM: the portal's mode, its administrator's switch, the fleet's.

    A missing flag row reads as off: the switch exists to stop requests, and a row an operator
    deleted is more likely a mistake to be noticed than an instruction to carry on.
    """
    if portal.crm_mode == "off" or portal.crm_opt_out_at is not None or portal.crm_purge_pending:
        return False
    enabled = (
        await session.execute(select(AppFlag.enabled).where(AppFlag.name == CRM_MIRROR_FLAG))
    ).scalar_one_or_none()
    return bool(enabled)


def _observe(visit: _Visit, block: Any) -> None:
    """Fold one `time{}` block into the guard of the method that answered it (§5.6).

    Recorded even when the guard does not fire: `operating_seconds` is the evidence
    `throttle.on_operation_time_limit` needs to decide whether a 429 was *ours* at all,
    and a portal that never records it would have every foreign 429 attributed to it.

    Per method, because Bitrix24 accounts operating time per method and `time.operating`
    is that method's own accumulator (docs/spike-crm-mirror.md, S-A.6). The statistics
    keep `portal_sync.operating_*`; every other method has its own `sync_method_budgets`
    row, so a slow `user.get` can no longer park the call sync.
    """
    if visit.method and visit.method != STATISTIC_METHOD:
        budget = visit.method_budgets.get(visit.method) or MethodBudget(method=visit.method)
        updated, stop = method_budgets.observe(budget, block)
        if updated != budget:
            visit.method_budgets[visit.method] = updated
            visit.touched_methods.add(visit.method)
        if stop:
            visit.stopped_methods.add(visit.method)
        return

    decision = throttle.observe_time_block(visit.state, block)
    updates = decision.updates
    if "operating_seconds" in updates or "operating_reset_at" in updates:
        visit.state = replace(
            visit.state,
            operating_seconds=updates.get("operating_seconds", visit.state.operating_seconds),
            operating_reset_at=updates.get("operating_reset_at", visit.state.operating_reset_at),
        )
        visit.pending["operating_seconds"] = visit.state.operating_seconds
        visit.pending["operating_reset_at"] = visit.state.operating_reset_at
    if decision.stop_visit:
        visit.decision = decision
        visit.stopped_methods.add(STATISTIC_METHOD)


def _pacer(visit: _Visit) -> Callable[[dict[str, Any] | None], Awaitable[bool]]:
    """The `Pacer` hook the phase modules call between two requests of one visit.

    Three things happen here and they belong together, which is why the phase modules
    take one callable rather than three: the response that just came back is measured
    against the operating budget (§5.6), the lease is extended ("after every batch",
    §5.9), and the next request is delayed to at least `1 / SYNC_RATE_PER_SEC`.

    Returning `False` ends the phase where it stands. Nothing is lost by that: every
    batch committed its rows and its cursor together, so the next visit resumes from
    the committed state (§5.9 durability).

    `heartbeat` is allowed to raise `FenceLost`, and must be: this is the earliest
    moment a replaced runner can discover it is a zombie, and continuing would spend
    the account's shared operating budget on rows that can never be committed.
    """

    async def pace(time_block: dict[str, Any] | None) -> bool:
        _observe(visit, time_block)
        if visit.method in visit.stopped_methods:
            return False
        await heartbeat(visit.fence)
        await throttle.get_pacer(visit.portal_id).wait()
        return True

    return pace


async def _with_client[T](
    visit: _Visit, method: str, fn: Callable[[BitrixClient], Awaitable[T]]
) -> T:
    """Run one phase with the portal credential (§5.8 single-flight, one retry).

    The client is built INSIDE the callback because the access token is a constructor
    argument: `with_portal_token` may call the callback a second time with a refreshed
    token, and reusing a client bound to the dead one would retry with the same
    credential. This is also why the phase modules let `ExpiredToken` propagate - the
    retry then resumes from the cursor the first attempt already committed.
    """
    visit.method = method

    async def run(token: str) -> T:
        await throttle.get_pacer(visit.portal_id).wait()
        async with BitrixClient(
            endpoint=visit.endpoint,
            access_token=token,
            portal_id=visit.portal_id,
            member_id=visit.member_id,
            token_user_id=visit.token_user_id,
            correlation_id=visit.correlation_id,
        ) as client:
            try:
                return await fn(client)
            finally:
                # Even an error response carries the `time{}` block the guard needs.
                if client.last_time is not None:
                    visit.used_methods.add(method)
                _observe(visit, client.last_time)

    try:
        return await with_portal_token(visit.portal_id, run)
    except CredentialUnavailable as exc:
        # No stored refresh token, or the row vanished: nothing the worker can retry.
        raise _TerminalStop(
            _REAUTH_REQUIRED,
            UnknownBitrixError(
                "credential_unavailable", description=str(exc.args[0]) if exc.args else ""
            ),
        ) from exc
    except MemberIdMismatch as exc:
        # The stored chain exchanged to a different portal (§4.1): fail closed.
        raise _TerminalStop(_REAUTH_REQUIRED, UnknownBitrixError("member_id_mismatch")) from exc


def _terminal_for(visit: _Visit, exc: BaseException) -> str | None:
    """The §5.8 terminal mapping, by exception TYPE and by the method it happened on.

    `AccessDenied` is terminal only for `voximplant.statistic.get`: there it means the
    stored credential lost the "Call statistics - view" right and the cache would
    silently narrow to a subset of the portal's calls. The same error from `user.get`
    is an employee-cache miss that `bitrix/users.py` already retries without
    `ADMIN_MODE`, and parking a portal forever over display names would be a
    self-inflicted outage.
    """
    if isinstance(exc, PortalDeleted):
        return _UNINSTALLED
    if isinstance(exc, (InvalidGrant, NoAuthFound, PaymentRequired, InsufficientScope)):
        # `insufficient_scope` is here to "fail loudly, never retried" (§5.8): a missing
        # scope is a deploy-time bug and the settings page must name it.
        return _REAUTH_REQUIRED
    if visit.method == STATISTIC_METHOD:
        if isinstance(exc, AccessDenied):
            return _NO_STATS_PERMISSION
        if isinstance(exc, MethodNotFound):
            # Decision 25: an on-premise build without the method becomes an explicit
            # portal state, not a mysterious failure repeated every five minutes.
            return _METHOD_MISSING
    return None


def _absorb(
    visit: _Visit,
    errors: Sequence[BitrixError],
    *,
    blocked: bool = False,
    stopped: bool = False,
) -> None:
    """Weigh what a phase reported instead of raised, and decide whether to go on.

    The phase modules return Bitrix24 failures as values because §5.2's prefix rule
    needs the whole ordered picture and because §5.6/§5.8 are the runner's decisions,
    not theirs. Three outcomes:

    * **terminal** - park the portal (§5.8); every further request would fail anyway;
    * **503, or a 429 on the statistics** - the shared bucket or the call sync's budget is
      empty, so the rest of this visit would only deepen the block; raise and let
      `sync/throttle.py` choose the delay (§5.6). A 429 on any other method parks only
      that method (`_block_method`), because it spends a budget of its own;
    * **anything else** - one page failed. Its rows are re-read next visit from the
      committed cursor, so the visit continues; the error is remembered for the
      failure counter and nothing else.
    """
    if blocked:
        # `sync/fetch.py`'s filter guard already parked the portal and wrote the event.
        raise _TerminalStop(
            _FILTER_UNSUPPORTED,
            UnknownBitrixError(
                _FILTER_UNSUPPORTED, description="the portal ignored an ID filter"
            ),
            audited=True,
        )
    for error in errors:
        terminal = _terminal_for(visit, error)
        if terminal is not None:
            raise _TerminalStop(terminal, error)
        if isinstance(error, QueryLimitExceeded):
            # Not counted here: this re-raise lands in `sync_portal`'s `except BitrixError`,
            # which is the ONE place §5.6's ladder counts a 503 (counting in both would
            # double the exponent for a per-command 503 and not for a whole-request one).
            raise error
        if isinstance(error, OperationTimeLimit):
            if visit.method and visit.method != STATISTIC_METHOD:
                _block_method(visit, visit.method, error)
                continue
            raise error
        if visit.soft_error is None:
            visit.soft_error = error
    if stopped:
        visit.stopped_methods.add(visit.method or STATISTIC_METHOD)


def _block_method(visit: _Visit, method: str, error: BitrixError) -> None:
    """A 429 on a method other than the statistics: that method waits, the visit goes on."""
    budget = visit.method_budgets.get(method) or MethodBudget(method=method)
    visit.method_budgets[method] = method_budgets.on_operation_time_limit(budget, error)
    visit.touched_methods.add(method)
    visit.stopped_methods.add(method)


def _method_blocked(visit: _Visit, method: str) -> bool:
    """Whether a previous visit left `method` waiting for its operating-time baskets."""
    budget = visit.method_budgets.get(method)
    return budget is not None and budget.blocked(_utcnow())


# ---------------------------------------------------------------------------- phases


async def _phase_refresh_requested(visit: _Visit) -> None:
    """§5.7 item 3: rows the SPA flagged after a failed playback go first.

    First because it is the only part of the sync a person is actually waiting on.
    """
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_refresh_requested(
            visit.fence, client, batch_pages=visit.state.batch_pages, pace=_pacer(visit)
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)


async def _phase_head_fetch(visit: _Visit) -> None:
    """§5.2: newest page, then one descending batch. Idempotent from `pending` OR `head`.

    Runs before the incremental phase and gates it: until the head fetch has
    established `high_id` and `low_id` there is no forward cursor to walk, and a
    crash between its two steps leaves `backfill_status='head'` so the next visit
    simply repeats step 1.
    """
    if visit.backfill_status not in _HEAD_STATES:
        return
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_head_fetch(
            visit.fence, client, batch_pages=visit.state.batch_pages, pace=_pacer(visit)
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)
    if outcome.backfill_status is not None:
        visit.backfill_status = outcome.backfill_status
    if outcome.high_id is not None:
        visit.high_id = outcome.high_id
    if outcome.low_id is not None:
        visit.low_id = outcome.low_id


async def _phase_incremental(visit: _Visit) -> None:
    """§5.4: probe one page forward, then pack only the pages that actually exist.

    Due-ness is the runner's call because the backfill re-dispatches this portal every
    two seconds (§5.9): without the interval check, a portal importing its history
    would probe for new calls thirty times a minute and spend the account's operating
    budget on empty answers.
    """
    if visit.backfill_status in _HEAD_STATES:
        return  # the forward cursor does not exist yet
    if not _due(visit.last_incremental_at, settings.sync_interval_sec):
        return
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_incremental(
            visit.fence,
            client,
            high_id=visit.high_id,
            batch_pages=visit.state.batch_pages,
            rescan_from_id=visit.rescan_from_id,
            window_hours=settings.rescan_window_hours,
            pace=_pacer(visit),
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)
    visit.high_id = outcome.high_id
    if outcome.rescan_from_id is not None:
        visit.rescan_from_id = outcome.rescan_from_id
    visit.last_incremental_at = _utcnow()
    visit.more = visit.more or outcome.more


async def _phase_backfill(visit: _Visit) -> None:
    """§5.3: walk backwards through `<low_id`, newest history first, resumably.

    `BACKFILL_BATCHES_PER_VISIT` is a yield, not a budget: the visit ends with
    `next_run_at = now() + 2 s`, so the import continues immediately while the other
    concurrency slots get a turn (§5.9).
    """
    if visit.backfill_status != _RUNNING:
        return
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_backfill(
            visit.fence,
            client,
            low_id=visit.low_id,
            batch_pages=visit.state.batch_pages,
            backfill_done=visit.backfill_done,
            batches=settings.backfill_batches_per_visit,
            pace=_pacer(visit),
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)
    visit.low_id = outcome.low_id
    visit.backfill_status = outcome.backfill_status
    visit.backfill_done = outcome.backfill_done
    visit.more = visit.more or outcome.more


async def _phase_rescan(visit: _Visit) -> None:
    """§5.7 item 1: re-read the last `RESCAN_WINDOW_HOURS` of ids for late updates.

    Recordings, transcripts, votes and comments are attached after the row exists and
    Bitrix24 exposes no modified date, so reading the window again is the only way to
    see them. No cursor moves - the window deliberately overlaps what is already read.
    """
    if visit.backfill_status in _HEAD_STATES:
        return
    if not _due(visit.last_rescan_at, settings.rescan_interval_sec):
        return
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_id_window_rescan(
            visit.fence,
            client,
            rescan_from_id=visit.rescan_from_id,
            high_id=visit.high_id,
            batch_pages=visit.state.batch_pages,
            pace=_pacer(visit),
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)
    if outcome.ran:
        visit.last_rescan_at = _utcnow()


async def _phase_recheck(visit: _Visit) -> None:
    """§5.7 item 2: the daily recording-recheck budget, at most two tries per call.

    This is what catches a recording attached after the 72 h rescan window has moved
    past the call, and the two-try cap is what stops a portal whose calls are simply
    never recorded from costing a request per call per day forever.
    """
    if not _due(visit.last_recheck_at, DAILY_SECONDS):
        return
    outcome = await _with_client(
        visit,
        STATISTIC_METHOD,
        lambda client: run_record_recheck(
            visit.fence, client, batch_pages=visit.state.batch_pages, pace=_pacer(visit)
        ),
    )
    _absorb(visit, outcome.errors, blocked=outcome.blocked, stopped=outcome.stopped)
    visit.last_recheck_at = _utcnow()


async def _phase_employees(visit: _Visit) -> None:
    """§7 writer (c): resolve placeholder and stale `employees` rows through `user.get`.

    Called on every visit rather than on a timer because the due-ness is in the data:
    `run_employees_refresh` returns immediately when there is no placeholder
    (`fetched_at IS NULL`, inserted by the call upsert for a user id nobody has
    resolved) and nothing older than `EMPLOYEE_TTL_HOURS`. That is what makes a portal
    that just imported its history get names in the filter list on the very next visit
    instead of hours later.
    """
    outcome = await _with_client(
        visit,
        USER_GET,
        lambda client: run_employees_refresh(
            visit.fence, client, ttl_hours=settings.employee_ttl_hours
        ),
    )
    _absorb(visit, outcome.errors)


async def _phase_daily_probe(visit: _Visit) -> None:
    """§5.8: one daily `app.info` + `user.admin` on the STORED credential.

    The `user.admin` half is why this is not optional. The credential invariant (§4.1
    decision 4) is proven once, at install: an administrator's token sees every call.
    If that person is later demoted or dismissed the token keeps working - and
    `voximplant.statistic.get` quietly starts returning only their own calls. Nothing
    fails, nothing is logged, and the dashboard simply shows less than the truth.
    Re-verifying daily turns that silent narrowing into an explicit
    `no_stats_permission` state, a `sync_blocked` event and a settings-page banner.
    """
    # Two daily jobs live in this phase and they need two clocks. `last_appinfo_at`
    # means "when did we last ask this portal about itself" and is ALSO written by the
    # open handler (`handlers/open.py::_housekeeping`, §4.4 step 4) after an `app.info`
    # made with the OPENER's token - which proves nothing about the stored credential.
    # Gating the `user.admin` half on it let a portal that is opened once a day starve
    # the only check that catches a demoted installer (§5.8 "Daily admin
    # re-verification", architecture.md:260 "at write time and daily by the worker").
    # So the admin half is due on the column only this phase and the credential writer
    # ever set, `portals.token_admin_verified_at`, and the `app.info` half keeps its own.
    if not (
        _due(visit.token_admin_verified_at, DAILY_SECONDS)
        or _due(visit.last_appinfo_at, DAILY_SECONDS)
    ):
        return

    async def probe(client: BitrixClient) -> BatchResult:
        return await client.batch(
            [("info", APP_INFO_METHOD, {}), ("admin", USER_ADMIN, {})], halt=0
        )

    result = await _with_client(visit, APP_INFO_METHOD, probe)
    _observe(visit, throttle.merge_time_blocks([command.time for command in result.commands]))

    admin_error = result.error("admin")
    if isinstance(admin_error, (UserAccessError, AccessDenied)):
        # The stored user can no longer use the application at all: the same silent
        # narrowing, arriving as a refusal instead of as `false`.
        raise _TerminalStop(
            _NO_STATS_PERMISSION,
            UnknownBitrixError(
                "user_admin_unavailable", description="daily re-verification was refused"
            ),
        )
    if result.ok("admin") and not parse_admin_flag(result.get("admin")):
        raise _TerminalStop(
            _NO_STATS_PERMISSION,
            UnknownBitrixError(
                "user_admin_false",
                description="the stored credential is no longer an administrator",
            ),
        )

    now = _utcnow()
    portal_values: dict[str, Any] = {}
    if result.ok("admin"):
        portal_values["token_admin_verified_at"] = now
    info = result.get("info")
    if isinstance(info, dict):
        status = info.get("STATUS") or info.get("status")
        version = info.get("VERSION") or info.get("version")
        installed = info.get("INSTALLED", info.get("installed"))
        if isinstance(status, str) and status:
            portal_values["app_status"] = status[:4]
        if isinstance(version, (int, str)) and str(version).isdigit():
            portal_values["app_version"] = int(version)
        if isinstance(installed, bool):
            portal_values["installed_flag"] = installed
    if admin_error is not None:
        # Not fatal and not "not an admin": an unreadable answer must not be mistaken
        # for a demotion, so it is logged and re-tried tomorrow.
        log.warning(
            "sync: daily admin re-verification could not run",
            extra={"portal_id": visit.portal_id, "error_code": admin_error.code},
        )
        _absorb(visit, [admin_error])

    async with control_txn() as session:
        if portal_values:
            await session.execute(
                update(Portal).where(Portal.id == visit.portal_id).values(**portal_values)
            )
        await fenced_update(session, visit.fence, {"last_appinfo_at": now})
    visit.last_appinfo_at = now
    if "token_admin_verified_at" in portal_values:
        # Only a proven `user.admin` advances the worker's own clock: an unreadable
        # answer above leaves the re-verification due, so it is retried instead of
        # being silently credited by the `app.info` half's timestamp.
        visit.token_admin_verified_at = now


# ----------------------------------------------------------------------- CRM mirror


def _crm_lane_errors(visit: _Visit, errors: Sequence[BitrixError]) -> list[BitrixError]:
    """The CRM command errors that belong to the lane, after the portal-wide ones are raised.

    A dead credential or a deleted portal is the portal's state (§5.8) and a 503 empties the
    bucket every lane shares, so those end the visit exactly as they do for the statistics. A
    429 parks the method (§5.6). Everything else - a refused lead list, a build without the
    method, one failed page - stays on the lane, so the call sync's failure counter never
    hears about it (§5.10).
    """
    remaining: list[BitrixError] = []
    for error in errors:
        terminal = _terminal_for(visit, error)
        if terminal is not None:
            raise _TerminalStop(terminal, error)
        if isinstance(error, QueryLimitExceeded):
            raise error
        if isinstance(error, OperationTimeLimit):
            _block_method(visit, visit.method, error)
            continue
        remaining.append(error)
    return remaining


def _lane_trouble(
    visit: _Visit,
    name: str,
    errors: Sequence[BitrixError],
    *,
    now: dt.datetime,
    cursor: dict[str, Any] | None = None,
) -> None:
    """Back a lane off for its first error: a day for a refusal, escalating for a fault."""
    lane = visit.lanes[name]
    first = errors[0]
    if isinstance(first, _CRM_UNAVAILABLE):
        code = first.code or type(first).__name__
        visit.lanes[name] = crm_lanes.unavailable(lane, code, now=now, cursor=cursor)
    else:
        visit.lanes[name] = crm_lanes.failed(lane, first, now=now, cursor=cursor)


async def _save_lanes(visit: _Visit, *names: str) -> None:
    """Store lanes that moved without rows - a failure, a park, a demotion - behind the fence."""
    async with control_txn() as session:
        await crm_lanes.store_lanes(session, visit.portal_id, [visit.lanes[name] for name in names])
        await fenced_update(session, visit.fence, {})


async def _crm_within_share(visit: _Visit, name: str, method: str, share: float) -> bool:
    """Whether a lane may send another batch on `method`; if not, it rests until the reset.

    Read from the method's own accumulator (`sync_method_budgets`), which counts the live
    reports' requests too - the same application, the same method. Resting is not failing:
    the lane keeps its streak and its reason, and comes back when the baskets have drained.
    """
    budget = visit.method_budgets.get(method)
    if budget is None or budget.operating_seconds is None:
        return True
    now = _utcnow()
    reset_at = budget.operating_reset_at
    if reset_at is not None and reset_at <= now:
        return True
    if float(budget.operating_seconds) < share * budget.state().operating_limit_s:
        return True
    until = (reset_at or now) + dt.timedelta(seconds=throttle.RESET_GRACE_SECONDS)
    visit.lanes[name] = crm_lanes.resting(visit.lanes[name], until)
    await _save_lanes(visit, name)
    return False


def _dialect(name: str, item: MirrorDialect, legacy: MirrorDialect) -> MirrorDialect:
    return legacy if name == crm_backfill.DIALECT_LEGACY else item


def _crm_lane_ready(visit: _Visit, name: str) -> crm_lanes.Lane | None:
    lane = visit.lanes.get(name)
    if not visit.crm_active or lane is None or visit.crm_batches <= 0:
        return None
    return lane if lane.runnable(_utcnow()) else None


async def _guarded_crm(visit: _Visit, name: str, work: Awaitable[None]) -> None:
    """Run one CRM lane so that a defect in its code backs off the lane and nothing else.

    Portal-wide outcomes still leave: a terminal credential state, a lost fence and a Bitrix24
    error the visit handles (a 503, a whole-request 429, a transport failure). Anything else is
    a bug in this module's own code, and charging it to `portal_sync.consecutive_failures`
    would pause the call sync for six hours over a CRM planner exception (§5.10).
    """
    try:
        await work
    except (_TerminalStop, FenceLost, BitrixError):
        raise
    except Exception:
        log.exception("crm: lane raised", extra={"portal_id": visit.portal_id, "lane": name})
        lane = visit.lanes.get(name)
        if lane is not None:
            visit.lanes[name] = crm_lanes.failed(lane, None, now=_utcnow(), reason="internal_error")
            await _save_lanes(visit, name)


async def _phase_crm_dict(visit: _Visit) -> None:
    """§5.10: funnel and stage names, hourly, read with the installer's credential (G0 Q1)."""
    await _guarded_crm(visit, crm_lanes.DICT, _run_crm_dict(visit))


async def _run_crm_dict(visit: _Visit) -> None:
    lane = _crm_lane_ready(visit, crm_lanes.DICT)
    if lane is None:
        return
    dictionary = await _with_client(
        visit,
        CRM_STATUS_LIST,
        lambda client: crm_dict.read_dictionary(client, pace=_pacer(visit)),
    )
    visit.crm_batches -= dictionary.batches
    errors = _crm_lane_errors(visit, dictionary.errors)
    now = _utcnow()
    if errors:
        _lane_trouble(visit, crm_lanes.DICT, errors, now=now)
    elif dictionary.complete:
        visit.lanes[crm_lanes.DICT] = crm_lanes.succeeded(
            lane, now=now, due_at=now + dt.timedelta(seconds=settings.crm_dict_interval_sec)
        )
    # Otherwise a stop or a 429 interrupted the read: the lane stays due for the next visit.
    await crm_dict.store_dictionary(
        visit.fence, dictionary, now=now, lanes=[visit.lanes[crm_lanes.DICT]]
    )


async def _phase_crm_sweep(visit: _Visit) -> None:
    """§5.10: re-read what changed, deals then leads, every CRM_SWEEP_INTERVAL_SEC."""
    for sweep, _backfill, item, legacy in _CRM_ENTITIES:
        await _guarded_crm(visit, sweep, _run_crm_sweep(visit, sweep, item, legacy))


async def _run_crm_sweep(
    visit: _Visit, name: str, item: MirrorDialect, legacy: MirrorDialect
) -> None:
    lane = _crm_lane_ready(visit, name)
    if lane is None:
        return
    start = crm_sweep.SweepCursor.from_json(lane.cursor, now=_utcnow())
    method = _dialect(start.dialect, item, legacy).method
    if method in visit.stopped_methods or _method_blocked(visit, method):
        return
    if not await _crm_within_share(visit, name, method, CRM_SWEEP_SHARE):
        return
    overlap = dt.timedelta(seconds=settings.crm_sweep_overlap_sec)

    async def run(client: BitrixClient) -> None:
        pace = _pacer(visit)
        while visit.crm_batches > 0:
            current = visit.lanes[name]
            cursor = crm_sweep.SweepCursor.from_json(current.cursor, now=_utcnow())
            dialect = _dialect(cursor.dialect, item, legacy)
            if not await _crm_within_share(visit, name, dialect.method, CRM_SWEEP_SHARE):
                return
            read_at = _utcnow()
            batch = await client.batch([crm_sweep.command(cursor, dialect, overlap=overlap)], halt=0)
            visit.crm_batches -= 1
            step = crm_sweep.apply(
                cursor,
                dialect,
                batch,
                overlap=overlap,
                now=read_at,
                utm_max_chars=settings.utm_value_max_chars,
            )
            now = _utcnow()
            if step.error is not None:
                if isinstance(step.error, MethodNotFound) and cursor.dialect == crm_backfill.DIALECT_ITEM:
                    demoted = replace(
                        cursor, dialect=crm_backfill.DIALECT_LEGACY, after_id=0, pass_started=None
                    )
                    visit.lanes[name] = replace(current, cursor=demoted.to_json())
                    await _save_lanes(visit, name)
                    visit.more = True
                    return
                errors = _crm_lane_errors(visit, [step.error])
                if errors:
                    _lane_trouble(visit, name, errors, now=now)
                    await _save_lanes(visit, name)
                return
            if step.violation is not None:
                visit.lanes[name] = crm_lanes.parked(current, _FILTER_UNSUPPORTED, now=now)
                await _save_lanes(visit, name)
                log.warning(
                    "crm: the portal ignored a sweep filter; lane parked",
                    extra={"portal_id": visit.portal_id, "lane": name, "detail": step.violation},
                )
                return
            if step.pass_complete:
                advanced = crm_lanes.succeeded(
                    current,
                    now=now,
                    due_at=now + dt.timedelta(seconds=settings.crm_sweep_interval_sec),
                    cursor=step.cursor.to_json(),
                )
            else:
                advanced = replace(current, status=crm_lanes.ACTIVE, cursor=step.cursor.to_json())
            visit.lanes[name] = advanced
            await upsert_items(
                visit.fence,
                step.rows,
                read_at=read_at,
                lanes=[advanced],
                rejected=[(dialect.entity_type_id, item_id, reason) for item_id, reason in step.rejected],
            )
            if step.pass_complete:
                return
            visit.more = True
            if not await pace(step.time_block):
                return

    await _with_client(visit, method, run)


async def _phase_crm_backfill(visit: _Visit) -> None:
    """§5.10: newest-first history, deals then leads, until CRM_BATCHES_PER_VISIT is spent."""
    for _sweep, backfill, item, legacy in _CRM_ENTITIES:
        await _guarded_crm(visit, backfill, _run_crm_backfill(visit, backfill, item, legacy))


async def _run_crm_backfill(
    visit: _Visit, name: str, item: MirrorDialect, legacy: MirrorDialect
) -> None:
    lane = _crm_lane_ready(visit, name)
    if lane is None:
        return
    method = _dialect(crm_backfill.BackfillCursor.from_json(lane.cursor).dialect, item, legacy).method
    if method in visit.stopped_methods or _method_blocked(visit, method):
        return
    if not await _crm_within_share(visit, name, method, CRM_BACKFILL_SHARE):
        return

    async def run(client: BitrixClient) -> None:
        pace = _pacer(visit)
        while visit.crm_batches > 0:
            current = visit.lanes[name]
            cursor = crm_backfill.BackfillCursor.from_json(current.cursor)
            dialect = _dialect(cursor.dialect, item, legacy)
            if not await _crm_within_share(visit, name, dialect.method, CRM_BACKFILL_SHARE):
                return
            read_at = _utcnow()
            rows: list[crm_items.ItemRow] = []
            rejected: list[tuple[int | None, str]] = []
            command_errors: list[BitrixError] = []
            time_block: dict[str, Any] | None = None

            if not cursor.headed:
                batch = await client.batch([crm_items.high_id_command(dialect, "hi")], halt=0)
                visit.crm_batches -= 1
                answer = batch.commands[0] if batch.commands else None
                time_block = throttle.merge_time_blocks(
                    [batch.time, None if answer is None else answer.time]
                )
                high_id = None
                if answer is not None and answer.error is not None:
                    command_errors = [answer.error]
                elif answer is not None:
                    high_id = crm_backfill.read_high_id(answer.result, dialect)
                if not command_errors and high_id is None:
                    command_errors = [classify(None, description="hi: unexpected result shape")]
                if high_id is not None:
                    cursor = crm_backfill.headed(
                        cursor,
                        high_id,
                        commands=settings.crm_batch_commands,
                        max_width=settings.crm_backfill_range_max,
                    )
            else:
                cursor = crm_backfill.plan(cursor, streams=settings.crm_batch_commands)
                if cursor.open:
                    outcome = await fetch_ranges(
                        client,
                        dialect,
                        cursor.open,
                        max_commands=settings.crm_batch_commands,
                        utm_max_chars=settings.utm_value_max_chars,
                    )
                    if outcome is not None:
                        visit.crm_batches -= 1
                        time_block = outcome.time_block
                        if outcome.filter_violation is not None:
                            visit.lanes[name] = crm_lanes.parked(
                                current, _FILTER_UNSUPPORTED, now=_utcnow()
                            )
                            await _save_lanes(visit, name)
                            log.warning(
                                "crm: the portal ignored a backfill filter; lane parked",
                                extra={
                                    "portal_id": visit.portal_id,
                                    "lane": name,
                                    "detail": outcome.filter_violation,
                                },
                            )
                            return
                        cursor = crm_backfill.settle(cursor, outcome.streams)
                        rows, rejected = outcome.rows, outcome.rejected
                        command_errors = [error for _index, error in outcome.errors]

            now = _utcnow()
            if (
                command_errors
                and all(isinstance(error, MethodNotFound) for error in command_errors)
                and cursor.dialect == crm_backfill.DIALECT_ITEM
            ):
                # Same ids in either dialect, so the ranges already walked stay walked.
                demoted = replace(cursor, dialect=crm_backfill.DIALECT_LEGACY)
                visit.lanes[name] = replace(current, cursor=demoted.to_json())
                await _save_lanes(visit, name)
                visit.more = True
                return
            errors = _crm_lane_errors(visit, command_errors)
            done, total = cursor.progress
            if errors:
                _lane_trouble(visit, name, errors, now=now, cursor=cursor.to_json())
                advanced = replace(visit.lanes[name], progress_done=done, progress_total=total)
            elif command_errors:
                # Only 429s: the method is parked, the progress made is kept, the lane stays due.
                advanced = replace(
                    current,
                    status=crm_lanes.ACTIVE,
                    cursor=cursor.to_json(),
                    progress_done=done,
                    progress_total=total,
                )
            else:
                advanced = crm_lanes.succeeded(
                    current,
                    now=now,
                    due_at=now,
                    cursor=cursor.to_json(),
                    status=crm_lanes.DONE if cursor.finished else crm_lanes.ACTIVE,
                    progress_done=done,
                    progress_total=total,
                )
            visit.lanes[name] = advanced
            await upsert_items(
                visit.fence,
                rows,
                read_at=read_at,
                lanes=[advanced],
                rejected=[(dialect.entity_type_id, item_id, reason) for item_id, reason in rejected],
            )
            if advanced.status == crm_lanes.DONE or command_errors:
                return
            visit.more = True
            if not await pace(time_block):
                return

    await _with_client(visit, method, run)


#: §5.9's order, extended by the CRM mirror: refresh_requested -> head_fetch -> incremental ->
#: backfill -> rescan -> recheck -> CRM dictionary -> CRM sweeps -> CRM backfills -> employees
#: -> app.info + user.admin. The CRM lanes run before the employee refresh so an assignee they
#: placeholder is named in the same visit. Each phase is paired with the method whose budget it
#: spends, which is what lets a stop skip a phase and not a visit.
_PHASES: Final[tuple[tuple[str, Callable[[_Visit], Awaitable[None]]], ...]] = (
    (STATISTIC_METHOD, _phase_refresh_requested),
    (STATISTIC_METHOD, _phase_head_fetch),
    (STATISTIC_METHOD, _phase_incremental),
    (STATISTIC_METHOD, _phase_backfill),
    (STATISTIC_METHOD, _phase_rescan),
    (STATISTIC_METHOD, _phase_recheck),
    (CRM_STATUS_LIST, _phase_crm_dict),
    (crm_items.CRM_ITEM_LIST, _phase_crm_sweep),
    (crm_items.CRM_ITEM_LIST, _phase_crm_backfill),
    (USER_GET, _phase_employees),
    (APP_INFO_METHOD, _phase_daily_probe),
)


# ------------------------------------------------------------------- exit and recovery


async def _relearn_endpoint(portal_id: int) -> None:
    """§5.8: one refresh, under the single-flight lock, purely to re-learn the endpoint.

    A renamed portal (or a newly connected custom domain) answers the stored
    `client_endpoint` with a connection error forever, and the OAuth response is the
    only sanctioned source of the new one (§4.1 endpoint invariant) -
    `bitrix/oauth.py::_refresh_locked` writes it as part of every refresh.

    There is no public "refresh now" entry point and there must not be one: refreshing
    is reactive by contract, and a callable that refreshes on demand is exactly how an
    application gets blocked for "renewing excessively". So the refresh is provoked the
    only honest way - by handing `with_portal_token` a callback that reports the token
    as expired ONCE. The callback re-reads `token_version` first, so if the wrapper has
    already refreshed (a token that was expiring anyway) it returns quietly and no
    second exchange happens.
    """
    async with control_txn() as session:
        before = (
            await session.execute(select(Portal.token_version).where(Portal.id == portal_id))
        ).scalar_one_or_none()
    if before is None:
        return
    seen = int(before)

    async def probe(_token: str) -> None:
        async with control_txn() as session:
            current = (
                await session.execute(
                    select(Portal.token_version).where(Portal.id == portal_id)
                )
            ).scalar_one_or_none()
        if current is not None and int(current) > seen:
            return  # the wrapper already refreshed; one exchange is the whole budget
        raise ExpiredToken(description="forced endpoint re-learn after transport failures")

    try:
        await with_portal_token(portal_id, probe)
    except Exception:
        # The refresh may itself fail (the chain is dead, the OAuth host unreachable).
        # `with_portal_token` has already recorded any terminal `token_status`; the
        # caller's back-off is unaffected either way.
        log.warning(
            "sync: endpoint re-learn failed", exc_info=True, extra={"portal_id": portal_id}
        )
    else:
        log.info(
            "sync: client_endpoint re-learned after transport failures",
            extra={"portal_id": portal_id},
        )


async def _apply_terminal(
    visit: _Visit, kind: str, error: BitrixError | None, *, audited: bool
) -> None:
    """Write a §5.8 terminal state. Always AFTER the lease is released.

    Order is the one thing to get right here: `release_lease` writes `next_run_at`, and
    `set_token_status(block_sync=True)` writes `next_run_at='infinity'`. The other way
    round would schedule a portal that is known to be unusable back into the tick every
    five minutes, burning the account's shared operating time on calls that cannot
    succeed until an administrator re-authorizes.
    """
    if kind == _UNINSTALLED:
        async with control_txn() as session:
            await mark_uninstalled(
                session,
                visit.portal_id,
                kind="uninstall",
                details={"source": "sync_worker", "reason": "portal_deleted"},
            )
        log.warning(
            "sync: portal reported deleted; marked uninstalled and queued for purge",
            extra={"portal_id": visit.portal_id},
        )
        return

    code = (error.code or type(error).__name__)[:64] if error is not None else kind
    async with control_txn() as session:
        await set_token_status(
            session,
            visit.portal_id,
            kind,
            block_sync=True,
            last_error_code=code,
            last_error_text=str(error) if error is not None else None,
        )
        if not audited:
            # `audited` states were written by the phase that detected them; a second
            # event for one cause would make the audit trail lie about how often it
            # happened.
            await record_event(
                session,
                visit.portal_id,
                "sync_blocked",
                details={"reason": kind, "error_code": code, "source": "sync_worker"},
            )
    log.warning("sync: portal blocked", extra={"portal_id": visit.portal_id, "reason": kind})


def _previous_ladder_step(visit: _Visit) -> int | None:
    """Which rung of §5.6's 503 ladder the PREVIOUS visit ended on, or None.

    §5.6's "exponential 2, 4, 8 ... 300 s" is an escalation ACROSS visits: a 503 always
    ends the visit that saw it (`_absorb` re-raises it and `bitrix/client.py` never
    retries), so a counter that lives only inside one visit can never leave the first
    rung. The rung therefore has to be read back from the row the previous visit wrote,
    and `portal_sync` has exactly two columns that carry it: `release_lease` writes
    `last_error_at` and the parked `next_run_at` together, from the same 503, so their
    difference IS the delay that was chosen. `last_error_code` is what makes the streak
    consecutive - a clean visit NULLs it (`release_lease`), any other failure overwrites
    it - which is the same evidence §5.8's transport-failure branch below uses.

    Deliberately NOT derived from `throttle_hits` (a lifetime counter: a portal
    throttled two hundred times over a month would open every visit at the 300 s
    ceiling) nor from `consecutive_failures` (§5.6 rule 1: throttling is not failure).
    """
    parked, since = visit.next_run_at, visit.last_error_at
    if visit.last_error_code is None or parked is None or since is None:
        return None
    if parked.tzinfo is None or since.tzinfo is None:
        # `'infinity'` arrives from asyncpg as a NAIVE datetime.max (§5.4's park); it is
        # never a 503 park, and subtracting it would raise instead of deciding.
        return None
    if not isinstance(classify(visit.last_error_code), QueryLimitExceeded):
        return None

    previous_delay = (parked - since).total_seconds()
    step = 0
    rung = throttle.BASE_BACKOFF_SECONDS
    # The rung the previous delay is CLOSEST to, not the first one above it: the two
    # timestamps are written milliseconds apart, so a 2 s park reads back as 1.995 s.
    while step < _MAX_LADDER_STEP and rung * 1.5 < previous_delay:
        rung *= 2.0
        step += 1
    return step


def _backoff_attempt(visit: _Visit) -> int:
    """The `attempt` exponent `throttle.on_query_limit` turns into the §5.6 delay.

    Zero for anything that is not a 503 (the other branches ignore it), and otherwise
    one rung above wherever the previous visit stopped - plus one more per extra 503
    seen inside this visit.
    """
    if visit.query_limit_hits <= 0:
        return 0
    previous = _previous_ladder_step(visit)
    climbed = 0 if previous is None else previous + 1
    return climbed + visit.query_limit_hits - 1


async def _close_visit(
    visit: _Visit, *, error: BitrixError | None, terminal: str | None, audited: bool
) -> None:
    """The single exit: rate state, then the lease, then any terminal status.

    `decision.updates` is filtered against `_RELEASE_OWNED` because
    `sync/lease.py::release_lease` writes those same columns from the same error - see
    the module docstring's "one seam". What is left is the half only
    `sync/throttle.py` knows: `batch_pages`, `clean_visits` and the observed
    `operating_*`.
    """
    now = _utcnow()
    if error is None and terminal is None and (visit.backfilling or visit.more):
        # §5.9: "`now() + 2 s` while backfilling" - and the same for an incremental run
        # that still reported `next`, which is the same "there is more, come back" case.
        due = now + dt.timedelta(seconds=BACKFILL_YIELD_SECONDS)
    else:
        due = now + dt.timedelta(seconds=settings.sync_interval_sec)
    if visit.crm_active:
        # A CRM lane due sooner - a sweep, a lane whose back-off ends - brings the visit
        # forward; the statistics phases stay behind their own due checks when it comes.
        wake = crm_lanes.next_wake(visit.lanes.values())
        if wake is not None and wake < due:
            due = max(wake, now + dt.timedelta(seconds=BACKFILL_YIELD_SECONDS))

    if visit.decision is not None:
        decision = visit.decision  # the operating-time guard already decided
    elif error is not None:
        decision = throttle.on_error(visit.state, error, attempt=_backoff_attempt(visit))
    else:
        decision = throttle.on_clean_visit(visit.state, next_run_at=due)

    next_run_at = decision.updates.get("next_run_at", due)
    rate_updates = {
        key: value
        for key, value in {**visit.pending, **decision.updates}.items()
        if key not in _RELEASE_OWNED
    }

    # The other methods' budgets: every row this visit changed, and every method that made
    # a request and was not stopped counts a clean visit towards its limit's recovery.
    clean = error is None and terminal is None
    budget_rows: list[MethodBudget] = []
    for method in sorted((visit.touched_methods | visit.used_methods) - {STATISTIC_METHOD}):
        budget = visit.method_budgets.get(method) or MethodBudget(method=method)
        if clean and method in visit.used_methods and method not in visit.stopped_methods:
            budget = method_budgets.on_clean_visit(budget)
        budget_rows.append(budget)

    try:
        async with control_txn() as session:
            if rate_updates or budget_rows:
                await fenced_update(session, visit.fence, rate_updates)
            if budget_rows:
                await method_budgets.store_budgets(session, visit.portal_id, budget_rows)
            if decision.operating_limit_s is not None:
                # `portals.capabilities` is written through its owning service so the
                # install-time probe results survive the merge (§4.3 step 5).
                await record_placements(
                    session,
                    visit.portal_id,
                    capabilities_patch={"operating_limit_s": decision.operating_limit_s},
                )
    except FenceLost:
        log.warning(
            "sync: fence lost while writing throttle state",
            extra={"portal_id": visit.portal_id},
        )
        return

    await release_lease(visit.fence, next_run_at=next_run_at, error=error)

    if isinstance(error, TransportError):
        # §5.8: three consecutive transport failures look exactly like a renamed portal.
        # `release_lease` has just incremented the counter, so the value we reached is
        # the snapshot + 1; `last_error_code` tells us the earlier ones were transport
        # too, which is what makes them *consecutive* rather than merely three.
        reached = visit.consecutive_failures + 1
        if (
            reached >= TRANSPORT_FAILURES_BEFORE_REFRESH
            and visit.last_error_code == TransportError.default_code
        ):
            await _relearn_endpoint(visit.portal_id)

    if terminal is not None:
        await _apply_terminal(visit, terminal, error, audited=audited)


# ------------------------------------------------------------------------------- jobs


async def sync_portal(portal_id: int) -> None:
    """One visit to one portal, in the §5.9 order. Never raises.

    Never raises because it is dispatched as a bare task: an exception escaping here
    would be an unretrieved task exception, the lease would be left to expire, and the
    portal would be charged a crash on the next tick. Every path - including a lost
    fence - ends either in `_close_visit` or in a deliberate, logged return.
    """
    correlation_id = uuid.uuid4()
    set_request_id(correlation_id.hex)
    try:
        visit = await _open_visit(portal_id, correlation_id)
        if visit is None:
            return

        error: BitrixError | None = None
        terminal: str | None = None
        audited = False
        try:
            for method, phase in _PHASES:
                if method in visit.stopped_methods or _method_blocked(visit, method):
                    # Skip the phase, never `break`: a statistics soft limit must not cost
                    # the daily admin re-verification, which spends another budget (§5.6).
                    continue
                try:
                    await phase(visit)
                except OperationTimeLimit as exc:
                    # A whole-request 429 raised by the client. On the statistics it parks
                    # the portal as before; on any other method only that method waits.
                    if method == STATISTIC_METHOD:
                        raise
                    _block_method(visit, method, exc)
            error = visit.soft_error
        except FenceLost:
            # The lease or the generation moved: an uninstall, a reinstall, or another
            # runner. Nothing of ours is left to release, and every write we could
            # still make is one decision 13 forbids.
            log.info("sync: fence lost; abandoning the visit", extra={"portal_id": portal_id})
            return
        except _TerminalStop as stop:
            terminal, error, audited = stop.token_status, stop.error, stop.audited
        except BitrixError as exc:
            if isinstance(exc, QueryLimitExceeded):
                # The single place a 503 is counted, and the reason §5.6's ladder can
                # leave its 2 s floor at all: every 503 arrives here, whether it was the
                # whole request (raised by `bitrix/client.py`) or one command of a
                # halt=0 batch (re-raised by `_absorb`), and it always ends the visit.
                visit.query_limit_hits += 1
            terminal = _terminal_for(visit, exc)
            error = exc
        except Exception as exc:
            log.exception("sync: visit raised", extra={"portal_id": portal_id})
            # Only the class name: an arbitrary exception's message can quote data, and
            # `last_error_text` is a column support reads (§6).
            error = UnknownBitrixError("internal_error", description=type(exc).__name__)

        await _close_visit(visit, error=error, terminal=terminal, audited=audited)
    finally:
        set_request_id(None)


def _spawn(
    registry: dict[int, asyncio.Task[None]],
    portal_id: int,
    coro: Any,
    label: str,
    *,
    on_done: Callable[[asyncio.Task[None]], None] | None = None,
) -> None:
    """Start a background task, keep a reference, and drop it when it finishes.

    The reference is not cosmetic: asyncio keeps only a weak reference to a running
    task, so a fire-and-forget `create_task` can be garbage collected mid-visit.
    `on_done` runs after the registry entry is gone, so it sees the slot as free.
    """
    if portal_id in registry:
        coro.close()
        return
    task = asyncio.create_task(coro, name=f"{label}:{portal_id}")
    registry[portal_id] = task
    task.add_done_callback(lambda _task: registry.pop(portal_id, None))
    if on_done is not None:
        task.add_done_callback(on_done)


async def _run_sync(portal_id: int) -> None:
    """The semaphore wrapper. `sync_portal` never raises, but a cancel still can."""
    async with _dispatch_semaphore():
        await sync_portal(portal_id)


#: Refill tasks in flight; the reference keeps them alive (see `_spawn`).
_refills: set[asyncio.Task[None]] = set()
_dispatch_lock: asyncio.Lock | None = None
_dispatch_lock_loop: asyncio.AbstractEventLoop | None = None


def _dispatch_guard() -> asyncio.Lock:
    """One lease-and-dispatch at a time in this process, rebuilt per loop like the semaphore."""
    global _dispatch_lock, _dispatch_lock_loop
    loop = asyncio.get_running_loop()
    if _dispatch_lock is None or _dispatch_lock_loop is not loop:
        _dispatch_lock = asyncio.Lock()
        _dispatch_lock_loop = loop
    return _dispatch_lock


async def _dispatch_due() -> None:
    """Lease as many due portals as there are free slots, and start them (§5.9 tick (a)).

    Only as many as there are free slots, and that is a correctness rule: a leased portal
    that is not dispatched sits idle until its lease expires and is then charged a
    `consecutive_failures` it did not earn. The lock keeps the tick and a refill from
    leasing the same free slot twice.
    """
    async with _dispatch_guard():
        free = settings.global_portal_concurrency - len(_inflight)
        if free <= 0:
            return
        for fence in await acquire_leases(free, WORKER_ID):
            _spawn(
                _inflight, fence.portal_id, _run_sync(fence.portal_id), "sync_portal", on_done=_refill
            )


def _refill(task: asyncio.Task[None]) -> None:
    """A visit ended, so its slot is free now: lease the next due portal without the tick.

    Without this a slot freed a second after a tick stays empty for fourteen more, which
    caps the whole worker at `GLOBAL_PORTAL_CONCURRENCY` visits per tick however short they
    are. A cancelled visit refills nothing: cancellation means the worker is stopping.
    """
    if task.cancelled():
        return
    refill = asyncio.get_running_loop().create_task(_refill_safely(), name="sync_refill")
    _refills.add(refill)
    refill.add_done_callback(_refills.discard)


async def _refill_safely() -> None:
    try:
        await _dispatch_due()
    except asyncio.CancelledError:
        raise
    except Exception:
        # The tick retries in at most 15 s; a refill must never become an unhandled error.
        log.exception("sync: slot refill failed")


async def _run_purge(portal_id: int) -> None:
    try:
        await purge_portal(portal_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("purge: job crashed", extra={"portal_id": portal_id})


async def _run_crm_purge(portal_id: int) -> None:
    try:
        await purge_crm(portal_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("crm purge: job crashed", extra={"portal_id": portal_id})


async def tick() -> None:
    """The 15 s heartbeat of §5.9: lease what is due, dispatch it, then one purge.

    Only as many portals are leased as there are free slots, and that is a correctness
    rule rather than politeness: a leased portal that is not dispatched sits idle until
    its lease expires and is then charged a `consecutive_failures` it did not earn, and
    ten of those park a healthy tenant for six hours.
    """
    await _dispatch_due()

    # (b) one portal at a time: the purge is a long series of 10 000-row deletes
    # against tables the api reads, and a second one would only add lock contention.
    if _purging:
        return
    async with control_txn() as session:
        pending = (
            await session.execute(
                # §5.9 (b) is `SELECT id FROM portals WHERE purge_pending LIMIT 1`, and it
                # assumed the tick "picks purge work on every visit". `sync/purge.py` paces
                # a failed purge for INCOMPLETE_RETRY_SECONDS, so the selection has to see
                # that pacing too: without the join the one purge slot is spent on a portal
                # `purge_portal_data` will only skip, and every other uninstalled tenant
                # waits out its cooldown - unbounded when opens keep renewing it, and their
                # rows stay on disk (brief rule 7) while `lease.py` also refuses to sync
                # them. LEFT join: a portal without a `portal_sync` row is still purgeable.
                select(Portal.id)
                .outerjoin(PortalSync, PortalSync.portal_id == Portal.id)
                .where(Portal.purge_pending.is_(True), outside_incomplete_cooldown())
                .order_by(Portal.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if pending is not None:
        portal_id = int(pending)
        _spawn(_purging, portal_id, _run_purge(portal_id), "purge_portal")
        return

    # (c) the same slot for a CRM-only purge (an administrator turned CRM analytics off),
    # after uninstalls: an uninstall owes every table, and a portal that is both waiting for
    # the whole-portal purge and for this one is emptied by the whole-portal one.
    async with control_txn() as session:
        crm_pending = (
            await session.execute(
                select(Portal.id)
                .outerjoin(PortalSync, PortalSync.portal_id == Portal.id)
                .where(
                    Portal.crm_purge_pending.is_(True),
                    Portal.purge_pending.is_(False),
                    outside_incomplete_cooldown(),
                )
                .order_by(Portal.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if crm_pending is not None:
        portal_id = int(crm_pending)
        _spawn(_purging, portal_id, _run_crm_purge(portal_id), "purge_crm")


async def purge_portal(portal_id: int) -> None:
    """Delete one uninstalled tenant's rows and PROVE the tables are empty (§5.9).

    Every count and every delete in `sync/purge.py` runs inside `tenant_txn(portal_id)`
    re-opened per chunk, because RLS is transaction-local and fails *silently* closed:
    a `DELETE FROM calls` issued from a control transaction reports success having
    deleted nothing, clears `purge_pending` and writes `portal_events(purge_done)`
    while every row is still on disk. That is why this job asserts instead of assuming,
    and why the tick dispatches it rather than doing the deletes itself.
    """
    outcome = await purge_portal_data(portal_id)
    if outcome.skipped:
        return
    log.info(
        "purge: visit finished",
        extra={
            "portal_id": portal_id,
            "deleted": outcome.total_deleted,
            "incomplete": outcome.incomplete,
            "bodies_redacted": outcome.bodies_redacted,
        },
    )


async def purge_crm(portal_id: int) -> None:
    """Delete one portal's CRM mirror after CRM analytics was turned off, verified (§5.12)."""
    await purge_crm_data(portal_id)


async def purge_rest_log() -> None:
    """Daily `rest_log` retention (§6). `config.py` refuses a window below 3 days."""
    await _purge_rest_log_rows()


async def purge_crm_contexts() -> None:
    """Daily `crm_contexts` retention (§6), portal by portal because of RLS."""
    await _purge_crm_context_rows()


async def sweep_inferred_uninstalls() -> None:
    """§5.8 / decision 18: uninstall a portal nobody can re-authorize any more.

    This is what makes brief rule 7 ("delete the customer's cached data when the app is
    removed") independent of the vendor cabinet. If the events URL cannot be registered
    - or one `ONAPPUNINSTALL` is simply lost - a removed portal shows up only as a dead
    token chain that no administrator ever comes back to fix. After
    `UNINSTALL_GRACE_DAYS` that is treated as an uninstall: `status='uninstalled'`,
    `purge_pending`, `sync_generation + 1` (fencing any in-flight run) and
    `portal_events(uninstall_inferred)`, so the inferred path stays distinguishable
    from a real event in the audit.

    The grace clock is the last time an ADMIN opened the app, because that is the only
    event that could have re-seeded the credential (§4.4 step 6); `uninstalled_at` and
    the install timestamps are the fallbacks for a portal nobody ever opened.
    """
    cutoff = _utcnow() - dt.timedelta(days=settings.uninstall_grace_days)
    async with control_txn() as session:
        candidates = [
            int(portal_id)
            for portal_id in (
                await session.execute(
                    select(Portal.id)
                    .where(
                        Portal.status == "active",
                        Portal.token_status == _REAUTH_REQUIRED,
                        func.coalesce(
                            Portal.last_admin_opened_at,
                            Portal.uninstalled_at,
                            Portal.install_completed_at,
                            Portal.installed_at,
                        )
                        < cutoff,
                    )
                    .order_by(Portal.id)
                    .limit(_SWEEP_LIMIT)
                )
            )
            .scalars()
            .all()
        ]

    for portal_id in candidates:
        # One transaction per portal: `mark_uninstalled` takes a row lock and writes an
        # audit event, and a single transaction spanning hundreds of tenants would hold
        # those locks against the open handler for the whole sweep.
        async with control_txn() as session:
            await mark_uninstalled(
                session,
                portal_id,
                kind="uninstall_inferred",
                details={
                    "reason": "token_chain_dead",
                    "grace_days": settings.uninstall_grace_days,
                },
            )
    if candidates:
        log.warning(
            "uninstall inferred after the grace period",
            extra={"portal_ids": candidates, "grace_days": settings.uninstall_grace_days},
        )


#: The name -> function registry every backend resolves against (§5.9). The names are
#: the contract with the scheduler: a Celery backend registers one task per entry and
#: nothing else in this module changes.
JOBS: Final[dict[str, Callable[..., Awaitable[None]]]] = {
    "tick": tick,
    "sync_portal": sync_portal,
    "purge_portal": purge_portal,
    "purge_crm": purge_crm,
    "purge_rest_log": purge_rest_log,
    "purge_crm_contexts": purge_crm_contexts,
    "sweep_inferred_uninstalls": sweep_inferred_uninstalls,
}
