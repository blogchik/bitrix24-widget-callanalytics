"""Pacing, back-off and the operating-time guard for one portal (§5.6).

Bitrix24 rejects a caller in two entirely different ways and this module is the only
place that knows the difference:

* **Intensity** - a leaky bucket that drains 2 requests/second (Basic plan; burst 50)
  *per account and per source IP*. Every tenant of this app shares one source address,
  so a hot loop in one portal is an outage for all of them. Exceeding it is
  `QUERY_LIMIT_EXCEEDED` / `OVERLOAD_LIMIT` (HTTP 503).
* **Operating time** - accumulated execution seconds of one method for this app over ten
  one-minute baskets (480 s cloud, 420 s self-hosted default). Exceeding it blocks the
  method with HTTP 429 `OPERATION_TIME_LIMIT` for *every* app on that account until the
  baskets expire. A `batch` is one request for the intensity bucket but each of its 50
  sub-commands burns operating time separately (research note (e)), which is why the
  guard reads `result_time` and not just the envelope's `time{}`.

Three rules of §5.6 exist because the obvious implementation is wrong:

1. **Throttling is not failure.** 429/503 move `throttle_hits`, never
   `consecutive_failures`. A backfill of 500k rows is *expected* to be throttled; if
   those hits counted as failures the tenth one would park a perfectly healthy portal
   for six hours and the backfill would never finish.
2. **A 429 is not proof that we caused it.** The operating limit is per account, shared
   with every other app the customer installed. The observed limit is therefore lowered
   only when our own last `operating` was within 20 % of it, never below
   `OPERATING_LIMIT_FLOOR`, and it is restored after `CLEAN_VISITS_TO_RECOVER` clean
   visits - otherwise one foreign 429 would throttle a portal forever.
3. **Every retry path ends in `next_run_at`.** Nothing here sleeps until a limit clears;
   the visit stops and the next tick picks the portal up. The only sleeping this module
   does is the sub-second pacing between two requests of one visit.

Everything except `PortalPacer` is a pure function: it takes the current `portal_sync`
values plus one error or one `time{}` block and returns the column updates the caller
hands to `sync/lease.py::fenced_update`. No database, no clock, no I/O - so the tests
can drive every branch, and so the fencing update stays in the caller's transaction
where §5.3 needs it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Final

from app.bitrix.errors import BitrixError, OperationTimeLimit, QueryLimitExceeded
from app.config import settings
from app.logging import get_logger

__all__ = [
    "BASE_BACKOFF_SECONDS",
    "CLEAN_VISITS_TO_RECOVER",
    "DEFAULT_BATCH_PAGES",
    "DEFAULT_OPERATING_LIMIT_S",
    "FAILURES_BEFORE_PAUSE",
    "FAILURE_PAUSE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MIN_BATCH_PAGES",
    "OPERATING_ATTRIBUTION_RATIO",
    "RESET_GRACE_SECONDS",
    "PortalPacer",
    "ThrottleDecision",
    "ThrottleState",
    "get_pacer",
    "merge_time_blocks",
    "observe_time_block",
    "on_clean_visit",
    "on_error",
    "on_operation_time_limit",
    "on_query_limit",
    "on_transient_failure",
    "read_time_block",
    "reset_pacers",
]

_log = get_logger(__name__)

#: Cloud default of the per-method operating budget, in seconds (research note (e)).
#: Self-hosted defaults to 420 s and is admin-configurable, which is exactly why the
#: soft ratio exists: 0.8 x 480 = 384 s still stops short of a 420 s block.
DEFAULT_OPERATING_LIMIT_S: Final[float] = 480.0

#: An operating limit above this is not a portal, it is a parsing accident.
MAX_OPERATING_LIMIT_S: Final[float] = 3_600.0

#: `portal_sync.batch_pages` (§3): 20 commands per batch, halved under pressure, never
#: below 5 - a batch of one page spends a whole request for a fiftieth of the work.
DEFAULT_BATCH_PAGES: Final[int] = 20
MIN_BATCH_PAGES: Final[int] = 5

#: Consecutive visits without a throttle hit before `batch_pages` and the observed
#: operating limit are restored (§5.6). The recovery is what keeps a foreign 429 from
#: being permanent.
CLEAN_VISITS_TO_RECOVER: Final[int] = 5

#: 503 back-off ladder: 2, 4, 8 ... 300 s (§5.6).
BASE_BACKOFF_SECONDS: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 300.0

#: A `Retry-After` from the portal is honoured, but never beyond an hour: the visit is
#: resumable state, not a promise, and a stray header must not park a tenant for a day.
MAX_RETRY_AFTER_SECONDS: Final[float] = 3_600.0

#: Baskets are one minute wide; resuming exactly at `operating_reset_at` would race the
#: oldest basket dropping out and earn a second 429 (§5.6).
RESET_GRACE_SECONDS: Final[float] = 60.0

#: "Our own last `operating` was within 20 % of the current limit" (§5.6) - only then is
#: a 429 evidence about *our* consumption rather than about another app on the account.
OPERATING_ATTRIBUTION_RATIO: Final[float] = 0.8

#: Non-throttle transient errors: escalate 60, 120, 240 ... then pause (§5.6).
FAILURE_BASE_BACKOFF_SECONDS: Final[float] = 60.0
FAILURE_MAX_BACKOFF_SECONDS: Final[float] = 3_600.0
FAILURES_BEFORE_PAUSE: Final[int] = 10
FAILURE_PAUSE_SECONDS: Final[float] = 6 * 3_600.0

#: `portal_sync.last_error_code` is varchar(64); `last_error_text` is unbounded, but a
#: Bitrix24 description is not a log sink.
_ERROR_CODE_LIMIT: Final[int] = 64
_ERROR_TEXT_LIMIT: Final[int] = 1_000

# The exponent is bounded before `2 ** n` is evaluated: `attempt` reaches these functions
# from a caller's counter, and an unbounded shift is a denial of service on ourselves.
_MAX_BACKOFF_EXPONENT: Final[int] = 32


# --------------------------------------------------------------------------- coercion


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_float(value: Any) -> float | None:
    """Seconds from `12.34`, `"12.34"` or `12`; anything else is not a measurement."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    return number


def _as_decimal(value: Any) -> Decimal | None:
    """`portal_sync.operating_seconds` is numeric(8,2): Decimal, never float (§3)."""
    number = _as_float(value)
    if number is None:
        return None
    try:
        return Decimal(str(round(number, 2)))
    except (InvalidOperation, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    """`operating_reset_at` arrives as a unix timestamp; ISO strings are tolerated.

    Bitrix24 documents a unix timestamp, but `time{}` also carries ISO
    `date_start` / `date_finish`, and an on-premise build that spells the reset the same
    way must not silently disable the guard.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        seconds = _as_float(value)
        return None if seconds is None else datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        seconds = _as_float(text)
        if seconds is not None:
            return datetime.fromtimestamp(seconds, tz=UTC)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _read(row: Any, name: str, default: Any) -> Any:
    """One field of a `portal_sync` row, whether it is an ORM object or a mapping.

    The tests build states from plain dicts and the worker passes the mapped class; a
    branch that only works for one of the two would be a branch nobody tests.
    """
    value = row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)
    return default if value is None else value


def _error_code(error: BitrixError | None) -> str | None:
    if error is None:
        return None
    code = (error.code or type(error).__name__).strip()
    return code[:_ERROR_CODE_LIMIT] or None


def _error_text(error: BitrixError | None) -> str | None:
    """`str(BitrixError)` renders code, status and description - never the payload.

    That matters: an OAuth or event payload can carry a live token (§6), and this text is
    written to a column support reads.
    """
    if error is None:
        return None
    return str(error)[:_ERROR_TEXT_LIMIT] or None


def _retry_after(error: BitrixError | None) -> float | None:
    """`Retry-After` as seconds, parked on the payload by `bitrix/client.py` (§5.6)."""
    if error is None:
        return None
    seconds = _as_float(error.payload.get("retry_after"))
    if seconds is None:
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


# ---------------------------------------------------------------- the `time{}` block


def read_time_block(block: Mapping[str, Any] | None) -> tuple[Decimal | None, datetime | None]:
    """`(operating seconds, operating_reset_at)` from one `time{}` block.

    Returns `(None, None)` for a block that carries neither - an on-premise build may
    omit `operating` entirely, and the guard must then simply not fire rather than treat
    a missing measurement as zero (which would read as "plenty of budget left").
    """
    if not isinstance(block, Mapping):
        return None, None
    return _as_decimal(block.get("operating")), _as_datetime(block.get("operating_reset_at"))


def merge_time_blocks(blocks: Sequence[Mapping[str, Any] | None]) -> dict[str, Any] | None:
    """Fold the per-command `result_time` blocks of one batch into one (§5.6).

    `operating` is the MAX over the sub-commands, never the sum: the value is already the
    account-wide accumulator as each sub-command saw it, so summing would count the same
    seconds up to fifty times and park a healthy portal every visit. `operating_reset_at`
    is the LATEST seen, because waiting slightly too long costs one sync interval while
    waiting too little costs a second 429 for every app on the customer's account.
    """
    operating: float | None = None
    reset_at: datetime | None = None
    merged: dict[str, Any] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        merged.update(dict(block))
        seconds = _as_float(block.get("operating"))
        if seconds is not None and (operating is None or seconds > operating):
            operating = seconds
        moment = _as_datetime(block.get("operating_reset_at"))
        if moment is not None and (reset_at is None or moment > reset_at):
            reset_at = moment
    if not merged:
        return None
    if operating is None:
        merged.pop("operating", None)
    else:
        merged["operating"] = operating
    if reset_at is None:
        merged.pop("operating_reset_at", None)
    else:
        merged["operating_reset_at"] = reset_at.timestamp()
    return merged


# -------------------------------------------------------------------- state / result


@dataclass(frozen=True)
class ThrottleState:
    """The `portal_sync` columns §5.6 reads, plus the portal's observed limit.

    A snapshot, deliberately: every decision below is `state -> updates`, so a test can
    construct the exact state that produces a branch instead of arranging a database.
    """

    batch_pages: int = DEFAULT_BATCH_PAGES
    clean_visits: int = 0
    throttle_hits: int = 0
    consecutive_failures: int = 0
    operating_seconds: Decimal | None = None
    operating_reset_at: datetime | None = None
    #: `portals.capabilities.operating_limit_s`; 480 s until a 429 teaches us otherwise.
    operating_limit_s: float = DEFAULT_OPERATING_LIMIT_S

    @classmethod
    def from_row(cls, row: Any, capabilities: Mapping[str, Any] | None = None) -> ThrottleState:
        """Build the snapshot from a `portal_sync` row and `portals.capabilities`.

        The limit is clamped into `[OPERATING_LIMIT_FLOOR, MAX_OPERATING_LIMIT_S]` here
        rather than at the write site, because it lives in JSONB that support edits by
        hand: a `0` there would make the soft guard fire on every visit and stop the
        portal forever, and a `999999` would disable the guard silently.
        """
        raw_limit = (
            capabilities.get("operating_limit_s") if isinstance(capabilities, Mapping) else None
        )
        limit = _as_float(raw_limit)
        if limit is None:
            limit = DEFAULT_OPERATING_LIMIT_S
        limit = max(float(settings.operating_limit_floor), min(limit, MAX_OPERATING_LIMIT_S))
        return cls(
            batch_pages=max(1, min(int(_read(row, "batch_pages", DEFAULT_BATCH_PAGES)), 50)),
            clean_visits=int(_read(row, "clean_visits", 0)),
            throttle_hits=int(_read(row, "throttle_hits", 0)),
            consecutive_failures=int(_read(row, "consecutive_failures", 0)),
            operating_seconds=_as_decimal(_read(row, "operating_seconds", None)),
            operating_reset_at=_as_datetime(_read(row, "operating_reset_at", None)),
            operating_limit_s=limit,
        )

    @property
    def soft_limit_s(self) -> float:
        """`OPERATING_SOFT_RATIO` x the observed limit - the point the visit stops."""
        return settings.operating_soft_ratio * self.operating_limit_s

    @property
    def halved_batch_pages(self) -> int:
        """`batch_pages` under pressure: halved, never below `MIN_BATCH_PAGES` (§5.6)."""
        return max(MIN_BATCH_PAGES, self.batch_pages // 2)


@dataclass(frozen=True)
class ThrottleDecision:
    """What one observation means, as column updates plus the runner's marching orders.

    `updates` is fed verbatim to `sync/lease.py::fenced_update`, so the throttle state
    lands under the same lease and generation check as the cursor it belongs to: a stale
    runner can no more re-open a paused portal than it can move a cursor.
    """

    updates: dict[str, Any] = field(default_factory=dict)
    #: True when the runner must end this visit now. The portal is parked in
    #: `next_run_at`; there is nothing left to retry inside the visit (rule 4).
    stop_visit: bool = False
    #: A new `portals.capabilities.operating_limit_s`; None leaves it untouched. It is a
    #: different TABLE from `updates`, hence a separate field rather than a column.
    operating_limit_s: float | None = None
    #: The chosen delay in seconds, for logs and tests; None when nothing was parked.
    delay_s: float | None = None
    #: Short machine tag naming the branch taken (`operating_soft_limit`, `foreign_429`,
    #: `operation_time_limit`, `query_limit`, `transient_failure`, `failure_pause`,
    #: `clean`, `recovered`, or `""` for "nothing to decide").
    reason: str = ""


def _resume_at(now: datetime, reset_at: datetime | None) -> tuple[datetime, float]:
    """When to come back after an operating block, and how long that is from `now`.

    `max(now, reset_at)` matters: a `time{}` block read from a response minutes old (or
    from a portal whose clock disagrees) can carry a reset that is already in the past,
    and `reset + 60 s` would then schedule the retry BEFORE now - a hot loop against the
    one limit that is shared with every other app on the customer's account.
    """
    base = now if reset_at is None or reset_at < now else reset_at
    resume = base + timedelta(seconds=RESET_GRACE_SECONDS)
    return resume, (resume - now).total_seconds()


def _throttle_updates(
    state: ThrottleState, error: BitrixError | None, now: datetime
) -> dict[str, Any]:
    """The columns every throttle branch writes.

    `consecutive_failures` is deliberately absent: §5.6's "throttling is not failure".
    """
    return {
        "throttle_hits": state.throttle_hits + 1,
        "clean_visits": 0,
        "batch_pages": state.halved_batch_pages,
        "last_error_code": _error_code(error),
        "last_error_text": _error_text(error),
        "last_error_at": now,
    }


# ---------------------------------------------------------------------- the decisions


def observe_time_block(
    state: ThrottleState,
    time_block: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> ThrottleDecision:
    """Store `operating` / `operating_reset_at` and stop the visit above the soft limit.

    This is the branch that keeps the app off the 429 path entirely: the operating limit
    is enforced per ACCOUNT, so blowing through it takes telephony REST away from every
    other app the customer installed, and only then from us. Stopping at 80 % of the
    observed limit is cheap - the work is resumable state (§5.9), so an early stop costs
    one sync interval and nothing else.

    A block without `operating` (older on-premise builds) records nothing and never
    fires: an absent measurement must not read as "0 s used".
    """
    moment = now or _utcnow()
    operating, reset_at = read_time_block(time_block)
    updates: dict[str, Any] = {}
    if operating is not None:
        updates["operating_seconds"] = operating
    if reset_at is not None:
        updates["operating_reset_at"] = reset_at

    if operating is None or float(operating) <= state.soft_limit_s:
        return ThrottleDecision(updates=updates)

    resume, delay = _resume_at(moment, reset_at or state.operating_reset_at)
    updates.update(
        {
            "next_run_at": resume,
            # A soft-limit hit is a throttle event by definition: it must not count as a
            # clean visit, or `batch_pages` would be restored while we are still at the
            # ceiling. It is NOT a failure (§5.6), so `consecutive_failures` is untouched.
            "throttle_hits": state.throttle_hits + 1,
            "clean_visits": 0,
            "batch_pages": state.halved_batch_pages,
        }
    )
    _log.info(
        "sync: operating soft limit reached, parking the portal",
        extra={
            "operating": float(operating),
            "soft_limit_s": state.soft_limit_s,
            "operating_limit_s": state.operating_limit_s,
            "delay_s": delay,
        },
    )
    return ThrottleDecision(
        updates=updates, stop_visit=True, delay_s=delay, reason="operating_soft_limit"
    )


def on_operation_time_limit(
    state: ThrottleState,
    error: BitrixError | None = None,
    *,
    now: datetime | None = None,
) -> ThrottleDecision:
    """HTTP 429 `OPERATION_TIME_LIMIT`: park until the baskets expire (§5.6).

    The observed limit is lowered only when our own last `operating` was within 20 % of
    it. The limit is per ACCOUNT and shared with every other app the customer installed,
    so a 429 arriving while we had used 30 s of a 480 s budget says nothing about our
    ceiling - lowering it there would let a foreign app halve this portal's throughput,
    permanently. When the evidence does point at us, the new ceiling is what we actually
    got blocked at, floored at `OPERATING_LIMIT_FLOOR`, and `on_clean_visit` restores the
    default after five quiet visits.
    """
    moment = now or _utcnow()
    error_payload = error.payload if error is not None else {}
    _, error_reset = read_time_block(
        error_payload.get("time") if isinstance(error_payload, Mapping) else None
    )
    resume, delay = _resume_at(moment, error_reset or state.operating_reset_at)

    # A `Retry-After` on a 429 is rare but authoritative when it is longer than our own
    # estimate; never shorter, because the block is keyed to the basket reset.
    retry_after = _retry_after(error)
    if retry_after is not None and retry_after > delay:
        delay = retry_after
        resume = moment + timedelta(seconds=retry_after)

    updates = _throttle_updates(state, error, moment)
    updates["next_run_at"] = resume
    if error_reset is not None:
        updates["operating_reset_at"] = error_reset

    ours = state.operating_seconds
    new_limit: float | None = None
    reason = "foreign_429"
    if ours is not None and float(ours) >= OPERATING_ATTRIBUTION_RATIO * state.operating_limit_s:
        reason = "operation_time_limit"
        candidate = max(
            float(settings.operating_limit_floor), min(float(ours), state.operating_limit_s)
        )
        if candidate < state.operating_limit_s:
            new_limit = candidate

    _log.warning(
        "sync: operating time limit hit",
        extra={
            "reason": reason,
            "our_last_operating": float(ours) if ours is not None else None,
            "operating_limit_s": state.operating_limit_s,
            "new_operating_limit_s": new_limit,
            "batch_pages": updates["batch_pages"],
            "delay_s": delay,
        },
    )
    return ThrottleDecision(
        updates=updates,
        stop_visit=True,
        operating_limit_s=new_limit,
        delay_s=delay,
        reason=reason,
    )


def on_query_limit(
    state: ThrottleState,
    error: BitrixError | None = None,
    *,
    now: datetime | None = None,
    attempt: int = 0,
) -> ThrottleDecision:
    """HTTP 503 `QUERY_LIMIT_EXCEEDED` / `OVERLOAD_LIMIT`: back off (§5.6).

    `Retry-After` wins when the portal sent one - it is the only number that knows about
    the other apps draining the same bucket. Otherwise the ladder is 2, 4, 8 ... 300 s.

    `attempt` is supplied by the caller (hits seen in THIS visit) rather than derived from
    `throttle_hits`, which is a lifetime counter: a busy portal throttled two hundred
    times over a month would otherwise start every visit at the 300 s ceiling and never
    finish its backfill.
    """
    moment = now or _utcnow()
    delay = _retry_after(error)
    if delay is None:
        exponent = max(0, min(int(attempt), _MAX_BACKOFF_EXPONENT))
        delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2.0**exponent))
    delay = max(BASE_BACKOFF_SECONDS, delay)

    updates = _throttle_updates(state, error, moment)
    updates["next_run_at"] = moment + timedelta(seconds=delay)
    _log.info(
        "sync: request intensity limit hit",
        extra={"delay_s": delay, "attempt": attempt, "batch_pages": updates["batch_pages"]},
    )
    return ThrottleDecision(updates=updates, stop_visit=True, delay_s=delay, reason="query_limit")


def on_transient_failure(
    state: ThrottleState,
    error: BitrixError | None = None,
    *,
    now: datetime | None = None,
) -> ThrottleDecision:
    """Anything that is neither throttling nor terminal: escalate, then pause 6 h (§5.6).

    The escalation (60, 120, 240 ... 3600 s) is the point: a portal whose REST is down
    must cost one request per hour, not one per tick. `FAILURES_BEFORE_PAUSE` failures in
    a row park it for six hours with the last error visible on the settings page - still
    never `infinity`, because the cause is usually transient and a silently retired
    portal is a support ticket nobody can answer.
    """
    moment = now or _utcnow()
    failures = state.consecutive_failures + 1
    if failures >= FAILURES_BEFORE_PAUSE:
        delay = FAILURE_PAUSE_SECONDS
        reason = "failure_pause"
    else:
        exponent = max(0, min(failures - 1, _MAX_BACKOFF_EXPONENT))
        delay = min(FAILURE_MAX_BACKOFF_SECONDS, FAILURE_BASE_BACKOFF_SECONDS * (2.0**exponent))
        reason = "transient_failure"

    updates: dict[str, Any] = {
        "consecutive_failures": failures,
        "clean_visits": 0,
        "next_run_at": moment + timedelta(seconds=delay),
        "last_error_code": _error_code(error),
        "last_error_text": _error_text(error),
        "last_error_at": moment,
    }
    _log.warning(
        "sync: visit failed",
        extra={
            "reason": reason,
            "consecutive_failures": failures,
            "delay_s": delay,
            "error_code": _error_code(error),
        },
    )
    return ThrottleDecision(updates=updates, stop_visit=True, delay_s=delay, reason=reason)


def on_error(
    state: ThrottleState,
    error: BitrixError,
    *,
    now: datetime | None = None,
    attempt: int = 0,
) -> ThrottleDecision:
    """Route one error to its §5.6 branch **by type**, never by error string.

    `errors.py` owns the string mapping (`classify`); everything here branches on the
    class it returned, so an undocumented spelling is corrected in one place. Terminal
    states (`ACCESS_DENIED`, `insufficient_scope`, `PORTAL_DELETED`, ...) are NOT handled
    here: they belong to §5.8 and end in `next_run_at='infinity'`, which is a decision
    about the credential, not about the rate.
    """
    if isinstance(error, OperationTimeLimit):
        return on_operation_time_limit(state, error, now=now)
    if isinstance(error, QueryLimitExceeded):
        return on_query_limit(state, error, now=now, attempt=attempt)
    return on_transient_failure(state, error, now=now)


def on_clean_visit(
    state: ThrottleState,
    *,
    now: datetime | None = None,
    next_run_at: datetime | None = None,
) -> ThrottleDecision:
    """A visit that finished without a throttle hit or a failure (§5.6).

    Recovery is the half of the adaptive limit that is easy to forget: without it the
    first foreign 429 of a portal's life would leave it at 5 pages per batch and a 300 s
    operating ceiling forever. After `CLEAN_VISITS_TO_RECOVER` quiet visits both go back
    to the defaults; the soft ratio still stops short of a self-hosted 420 s block.

    `next_run_at` stays the caller's decision (`SYNC_INTERVAL_SEC`, or ~2 s while a
    backfill is running, §5.9) and is copied into the updates only when given.
    `last_error_*` is left alone on purpose - the settings page shows the last error as
    history, and `consecutive_failures = 0` is the health signal.
    """
    _ = now or _utcnow()  # one clock source per decision, even where nothing uses it yet
    visits = state.clean_visits + 1
    updates: dict[str, Any] = {"consecutive_failures": 0}
    limit: float | None = None
    reason = "clean"

    if visits >= CLEAN_VISITS_TO_RECOVER:
        updates["clean_visits"] = 0
        if state.batch_pages < DEFAULT_BATCH_PAGES:
            updates["batch_pages"] = DEFAULT_BATCH_PAGES
            reason = "recovered"
        if state.operating_limit_s < DEFAULT_OPERATING_LIMIT_S:
            limit = DEFAULT_OPERATING_LIMIT_S
            reason = "recovered"
    else:
        updates["clean_visits"] = visits

    if next_run_at is not None:
        updates["next_run_at"] = next_run_at
    if reason == "recovered":
        _log.info(
            "sync: throttle state recovered after clean visits",
            extra={"batch_pages": updates.get("batch_pages"), "operating_limit_s": limit},
        )
    return ThrottleDecision(updates=updates, operating_limit_s=limit, reason=reason)


# ------------------------------------------------------------------------------ pacing


class PortalPacer:
    """At least `1 / SYNC_RATE_PER_SEC` seconds between two HTTP requests to one portal.

    The leaky bucket is counted per account AND per source IP (research note (e)), and
    every tenant of this app leaves from the same address, so this is not a per-portal
    politeness knob - it is what keeps one backfill from returning 503 to every other
    tenant's incremental sync.

    One `batch` is ONE request for the bucket no matter how many sub-commands it carries,
    so callers pace around the HTTP call and never around the pages inside it.

    `clock` and `sleep` are injectable because the alternative is a test suite that
    really sleeps: with a stub clock the reservation still advances by exactly one
    interval per call, so every branch is reachable without wall time.
    """

    __slots__ = ("_clock", "_lock", "_min_interval", "_next_allowed", "_sleep")

    def __init__(
        self,
        *,
        rate_per_sec: float | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        rate = rate_per_sec if rate_per_sec is not None else settings.sync_rate_per_sec
        if rate <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._min_interval = 1.0 / rate
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_allowed = float("-inf")

    @property
    def min_interval(self) -> float:
        """Seconds between two requests - 0.5 s at the Basic plan's 2 req/s."""
        return self._min_interval

    async def wait(self) -> None:
        """Block until the next request to this portal may be sent.

        The reservation is taken under a lock and BEFORE the await returns, so two
        coroutines that both wait cannot be released into the same slot; the second is
        scheduled a full interval after the first.
        """
        async with self._lock:
            now = self._clock()
            remaining = self._next_allowed - now
            if remaining > 0:
                await self._sleep(remaining)
                now = max(self._clock(), self._next_allowed)
            self._next_allowed = now + self._min_interval


_pacers: dict[int, PortalPacer] = {}


def get_pacer(portal_id: int) -> PortalPacer:
    """The process-wide pacer for one portal.

    Per portal and not global: the bucket is per account, and one global pacer would
    serialize `GLOBAL_PORTAL_CONCURRENCY` tenants behind each other for no reason. The
    registry is keyed by portal id so the interval survives across visits within one
    process - two visits 200 ms apart (the backfill re-dispatch of §5.9) still pace.
    """
    pacer = _pacers.get(portal_id)
    if pacer is None:
        pacer = PortalPacer()
        _pacers[portal_id] = pacer
    return pacer


def reset_pacers() -> None:
    """Drop the registry (tests, and a worker that re-reads its configuration)."""
    _pacers.clear()
