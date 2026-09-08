"""§5.6 - the throttle's decisions, as pure functions over state (no database, no HTTP).

WHY these are worth their own file: every rule in §5.6 exists because getting it wrong is
invisible until it is expensive. Counting a 429 as a failure walks a legitimately busy
backfill into the 6 h pause and the customer sees "sync stopped" with no reason. Retrying
a 503 without honouring `Retry-After` turns our one shared source IP into an abuser of an
account-wide leaky bucket that every other app on that portal also draws from. Lowering
the observed operating limit on someone ELSE's 429 throttles a healthy tenant forever.
None of that shows up in a unit test of the fetch loop, and all of it shows up here.

CONTRACT (`app/sync/throttle.py`). The decision layer is pure - state in, columns out,
no clock of its own - which is the only reason these branches can be driven at all:

    on_error(state, error, *, now, attempt) -> ThrottleDecision      # 429 / 503 / other
    observe_time_block(state, time_block, *, now) -> ThrottleDecision  # every response
    on_clean_visit(state, *, now) -> ThrottleDecision                # a visit with no hit

    ThrottleDecision.updates          -> `portal_sync` columns, incl. `next_run_at`
    ThrottleDecision.operating_limit_s-> a NEW observed limit, or None to keep the current
    ThrottleState(batch_pages, clean_visits, throttle_hits, consecutive_failures,
                  operating_seconds, operating_reset_at, operating_limit_s)

The tiny adapter below resolves those names (and reads the decision through `.updates`
or plain attributes) so a rename reports the contract rather than an ImportError; every
assertion is on the SHAPE above, never on the adapter.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any, Final
from urllib.parse import parse_qsl

import httpx
import pytest

from app.bitrix.errors import (
    BitrixError,
    OperationTimeLimit,
    QueryLimitExceeded,
    TransportError,
    UnknownBitrixError,
)
from app.config import settings

# --------------------------------------------------------------------------- adapter

_STATE_DEFAULTS: Final[dict[str, Any]] = {
    "batch_pages": 20,
    "clean_visits": 0,
    "throttle_hits": 0,
    "consecutive_failures": 0,
    "operating_limit_s": 480,
    "operating_seconds": None,
    "operating_reset_at": None,
}

_ARG_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "error": ("error", "err", "exc", "failure"),
    "state": ("state", "current", "sync", "throttle_state"),
    "time_block": ("time_block", "time", "block"),
    "now": ("now", "at", "moment"),
    "attempt": ("attempt", "attempts", "hits_this_visit"),
}

_MISSING: Final[object] = object()


def _module() -> ModuleType:
    return importlib.import_module("app.sync.throttle")


def _resolve(*names: str) -> Callable[..., Any]:
    module = _module()
    for name in names:
        found = getattr(module, name, None)
        if callable(found):
            return found
    raise AssertionError(
        f"app/sync/throttle.py must expose one of {names} - see the CONTRACT in this file's docstring"
    )


def constant(default: int, *names: str) -> int:
    """A tunable §5.6 constant, with the design's value as the fallback."""
    module = _module()
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return default


def state(**overrides: Any) -> Any:
    """Build the module's own state object when it has one, else a plain mapping."""
    values = {**_STATE_DEFAULTS, **overrides}
    module = _module()
    for name in ("ThrottleState", "SyncState", "State", "ThrottleInput"):
        candidate = getattr(module, name, None)
        if isinstance(candidate, type):
            parameters = inspect.signature(candidate).parameters
            required = {
                key
                for key, parameter in parameters.items()
                if parameter.default is parameter.empty and key not in values
            }
            assert not required, f"{name} requires fields this test cannot fill: {sorted(required)}"
            return candidate(**{key: value for key, value in values.items() if key in parameters})
    return values


def _pick(parameter_name: str, supplied: Mapping[str, Any]) -> Any:
    for canonical, aliases in _ARG_ALIASES.items():
        if parameter_name in aliases and canonical in supplied:
            return supplied[canonical]
    return _MISSING


def invoke(function: Callable[..., Any], **supplied: Any) -> Any:
    """Call a decision function with whatever subset of the arguments it declares."""
    positional: list[Any] = []
    keywords: dict[str, Any] = {}
    skipped = False
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        value = _pick(parameter.name, supplied)
        if value is _MISSING:
            assert parameter.default is not parameter.empty, (
                f"{function.__name__} requires an unknown argument {parameter.name!r}; "
                "see the CONTRACT in this file's docstring"
            )
            skipped = True
            continue
        if parameter.kind is parameter.KEYWORD_ONLY:
            keywords[parameter.name] = value
        else:
            assert not skipped, f"{function.__name__} has an unfillable positional argument"
            positional.append(value)
    result = function(*positional, **keywords)
    # The decisions are meant to be pure and synchronous; tolerate an async spelling so a
    # signature choice cannot masquerade as a throttle bug.
    return asyncio.run(result) if inspect.isawaitable(result) else result


def values_of(decision: Any) -> dict[str, Any]:
    """The `portal_sync` columns a decision asks to be written."""
    if isinstance(decision, Mapping):
        return dict(decision)
    for attribute in ("values", "updates", "columns", "changes"):
        candidate = getattr(decision, attribute, None)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    if is_dataclass(decision):
        return {field.name: getattr(decision, field.name) for field in fields(decision)}
    raise AssertionError(f"cannot read column updates from {decision!r}")


def field_of(decision: Any, name: str, default: Any = _MISSING) -> Any:
    """One decided column: from `.values`, else from an attribute of the decision."""
    values = values_of(decision)
    if name in values and values[name] is not None:
        return values[name]
    attribute = getattr(decision, name, None)
    if attribute is not None:
        return attribute
    assert default is not _MISSING, f"the decision carries no {name!r}: {decision!r}"
    return default


def next_run_of(decision: Any) -> datetime:
    when = field_of(decision, "next_run_at")
    assert isinstance(when, datetime), f"next_run_at must be a datetime, got {when!r}"
    return when


def delay_of(decision: Any, now: datetime) -> float:
    return (next_run_of(decision) - now).total_seconds()


def limit_of(decision: Any, default: int) -> int:
    """The observed operating limit after the decision (§5.6, `capabilities`)."""
    values = values_of(decision)
    for key in ("operating_limit_s", "operating_limit"):
        if values.get(key) is not None:
            return int(values[key])
        attribute = getattr(decision, key, None)
        if attribute is not None:
            return int(attribute)
    capabilities = values.get("capabilities")
    if isinstance(capabilities, Mapping) and capabilities.get("operating_limit_s") is not None:
        return int(capabilities["operating_limit_s"])
    return default


def decide_error(error: BitrixError, current: Any, now: datetime, attempt: int = 0) -> Any:
    function = _resolve("decide_error", "on_error", "error_decision", "plan_error", "handle_error")
    return invoke(function, error=error, state=current, now=now, attempt=attempt)


def decide_time_block(block: dict[str, Any] | None, current: Any, now: datetime) -> Any:
    function = _resolve(
        "decide_time_block", "on_time_block", "time_block_decision", "observe_time_block"
    )
    return invoke(function, time_block=block, state=current, now=now)


def decide_clean_visit(current: Any, now: datetime) -> Any:
    function = _resolve("on_clean_visit", "decide_clean_visit", "clean_visit", "on_success")
    return invoke(function, state=current, now=now)


def time_block(operating: float, reset_at: datetime) -> dict[str, Any]:
    """The `time{}` block as Bitrix24 sends it: `operating_reset_at` is a unix timestamp."""
    return {
        "start": reset_at.timestamp() - 600,
        "duration": 0.2,
        "processing": 0.2,
        "operating": operating,
        "operating_reset_at": int(reset_at.timestamp()),
    }


NOW: Final[datetime] = datetime(2025, 9, 1, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------------- tests


@pytest.mark.parametrize(
    "error",
    [
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        QueryLimitExceeded("QUERY_LIMIT_EXCEEDED", http_status=503),
        QueryLimitExceeded("OVERLOAD_LIMIT", http_status=503),
    ],
    ids=["429", "503", "overload"],
)
def test_throttling_is_not_failure(error: BitrixError) -> None:
    """§5.6 - 429/503 bump `throttle_hits`, never `consecutive_failures`.

    A backfill of 500k rows is *supposed* to meet the limit; if each hit counted as a
    failure the portal would be dropped into the 6 h pause after ten of them and the
    settings page would show a scary error for a healthy import that simply needs to wait.
    """
    current = state(throttle_hits=3, consecutive_failures=2)

    decision = decide_error(error, current, NOW)

    assert field_of(decision, "throttle_hits") == 4
    assert field_of(decision, "consecutive_failures", 2) == 2, "a limit hit is not a failure"
    assert next_run_of(decision) > NOW, "a limit hit must always park the portal for a while"


def test_retry_after_is_honoured_when_present() -> None:
    """§5.6 - the server told us when to come back; guessing shorter is abuse."""
    error = QueryLimitExceeded("QUERY_LIMIT_EXCEEDED", http_status=503, payload={"retry_after": 90})

    decision = decide_error(error, state(throttle_hits=0), NOW)

    delay = delay_of(decision, NOW)
    assert 90 <= delay <= 120, f"Retry-After: 90 was not honoured (waited {delay}s)"


def test_backoff_grows_and_is_capped_when_retry_after_is_absent() -> None:
    """§5.6 - exponential 2, 4, 8 … 300 s, so a busy portal is retried, never hammered.

    The ladder is driven by the hits seen in THIS visit, not by the lifetime
    `throttle_hits`: a portal legitimately throttled a few hundred times over a month
    would otherwise open every visit at the 300 s ceiling and never finish its backfill.
    """
    delays = [
        delay_of(
            decide_error(
                QueryLimitExceeded("QUERY_LIMIT_EXCEEDED", http_status=503),
                state(throttle_hits=200),  # lifetime counter: must not enter the ladder
                NOW,
                attempt=attempt,
            ),
            NOW,
        )
        for attempt in (0, 1, 2, 3, 10)
    ]

    assert delays[0] == 2, f"the first retry of a visit waits 2 s, not {delays[0]}"
    assert delays == sorted(delays), f"backoff must not shrink as attempts accumulate: {delays}"
    assert all(delay <= 300 for delay in delays), f"backoff exceeded the 300 s cap: {delays}"
    assert delays[-1] == 300, "the ladder must saturate at the cap, not keep doubling"


@pytest.mark.parametrize(
    ("before", "after"),
    [(20, 10), (10, 5), (5, 5)],
    ids=["20->10", "10->5", "floor"],
)
def test_batch_pages_halve_on_a_limit_error_and_stop_at_the_floor(before: int, after: int) -> None:
    """§5.6 / decision 12 - fewer pages per batch means less operating time per request."""
    decision = decide_error(
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429), state(batch_pages=before), NOW
    )

    assert field_of(decision, "batch_pages", before) == after
    assert after >= constant(5, "MIN_BATCH_PAGES", "BATCH_PAGES_FLOOR")


def test_batch_pages_are_restored_only_after_the_clean_visit_count() -> None:
    """§5.6 - recovery is earned, so one foreign 429 cannot halve a portal forever."""
    needed = constant(5, "CLEAN_VISITS_TO_RECOVER", "CLEAN_VISITS_TO_RECOVERY")
    default = constant(20, "DEFAULT_BATCH_PAGES", "BATCH_PAGES_DEFAULT")

    too_early = decide_clean_visit(state(batch_pages=5, clean_visits=0), NOW)
    assert field_of(too_early, "batch_pages", 5) == 5, "restored before the count was reached"
    assert field_of(too_early, "clean_visits", 0) == 1

    earned = decide_clean_visit(state(batch_pages=5, clean_visits=needed - 1), NOW)
    assert field_of(earned, "batch_pages", 5) == default
    assert field_of(earned, "clean_visits", 0) == 0, "the counter restarts after a recovery"


def test_the_operating_limit_is_lowered_only_on_our_own_near_limit_observation() -> None:
    """§5.6 - a 429 is account-wide: another app's traffic can trigger ours.

    Lowering our own budget because a colleague's integration burned the account's
    operating time would throttle this portal permanently for a reason no one can see. The
    limit may only move when OUR last observed `operating` was within 20 % of it.
    """
    near = decide_error(
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        state(operating_seconds=470, operating_limit_s=480),
        NOW,
    )
    assert limit_of(near, 480) < 480, "our own near-limit observation must lower the budget"
    assert limit_of(near, 480) >= settings.operating_limit_floor

    foreign = decide_error(
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        state(operating_seconds=100, operating_limit_s=480),
        NOW,
    )
    assert limit_of(foreign, 480) == 480, "someone else's 429 must not shrink our limit"


def test_the_operating_limit_never_falls_below_the_floor() -> None:
    """§5.6 - OPERATING_LIMIT_FLOOR (300 s) keeps the adaptation from collapsing to zero."""
    floor = settings.operating_limit_floor
    decision = decide_error(
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        state(operating_seconds=floor + 5, operating_limit_s=floor + 10),
        NOW,
    )

    assert limit_of(decision, floor + 10) >= floor


def test_the_soft_limit_pause_lands_on_operating_reset_at_plus_a_minute() -> None:
    """§5.6 - stop BEFORE the 429, and come back after the baskets have rolled over.

    `operating` accumulates over ten one-minute baskets; returning exactly at
    `operating_reset_at` races the oldest basket dropping out, which is why the design
    adds a minute. Below the soft ratio the visit simply continues.
    """
    reset_at = NOW + timedelta(minutes=4)
    limit = 480
    hot = decide_time_block(
        time_block(limit * settings.operating_soft_ratio + 20, reset_at),
        state(operating_limit_s=limit),
        NOW,
    )

    assert abs((next_run_of(hot) - (reset_at + timedelta(seconds=60))).total_seconds()) <= 2

    cool = decide_time_block(time_block(10.0, reset_at), state(operating_limit_s=limit), NOW)
    cool_next = field_of(cool, "next_run_at", None)
    assert cool_next is None or cool_next <= NOW + timedelta(seconds=settings.sync_interval_sec), (
        "a response well below the soft limit must not pause the portal"
    )


@pytest.mark.parametrize(
    "error",
    [
        UnknownBitrixError("INTERNAL_SERVER_ERROR", http_status=500),
        TransportError("transport_error", description="connect timeout"),
    ],
    ids=["500", "transport"],
)
def test_ten_ordinary_failures_pause_the_portal_for_six_hours(error: BitrixError) -> None:
    """§5.6 - the breaker: ten strikes, then six hours, and never a hot loop.

    The counter is what stops a permanently broken portal from being retried every 5
    minutes forever against an API shared by every other tenant; the six-hour pause is
    what keeps the settings page's "last error" readable instead of a scrolling blur.
    """
    breaking = decide_error(error, state(consecutive_failures=9), NOW)

    assert field_of(breaking, "consecutive_failures") == 10
    assert field_of(breaking, "throttle_hits", 0) == 0, "an ordinary failure is not a throttle hit"
    pause = delay_of(breaking, NOW)
    assert 5.5 * 3600 <= pause <= 6.5 * 3600, f"expected a ~6 h pause, got {pause / 3600:.2f} h"

    early = decide_error(error, state(consecutive_failures=0), NOW)
    assert field_of(early, "consecutive_failures") == 1
    assert delay_of(early, NOW) < 3600, "the first failure must not cost the portal an hour"


# ------------------------------------------------------ the ladder, through the runner
#
# The decisions above are pure, but WHICH rung of §5.6's ladder a 503 lands on is a fact
# about the portal across visits, not inside one: a 503 always ends the visit that saw it
# (`jobs/definitions.py::_absorb` re-raises it, `bitrix/client.py` never retries), so a
# ladder driven from a per-visit counter alone can never leave its first rung. The test
# below therefore drives the real runner against a portal that only ever answers 503.


class _AlwaysThrottled:
    """A Bitrix24 whose REST endpoint answers every call with the 503 of §5.6.

    Both shapes the limit arrives in are covered, because they reach the runner by
    different routes and only one of them passes through `_absorb`:

    * `"envelope"` - HTTP 503 for the whole request, raised by `bitrix/client.py`;
    * `"per_command"` - HTTP 200 with `result_error` on every sub-command of the halt=0
      batch, which `sync/fetch.py` reports as a value and `_absorb` re-raises.

    No `Retry-After` in either: that header short-circuits the ladder
    (`throttle._retry_after`), and the ladder is exactly what is under test.
    """

    _CMD_RE: Final = re.compile(r"^cmd\[(?P<key>[^\]]+)\]$")

    def __init__(self, member_id: str, *, shape: str = "envelope") -> None:
        self.member_id = member_id
        self.shape = shape
        self.rest_calls = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if "/oauth/token" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "access_token": "throttled-access",
                    "refresh_token": "throttled-refresh",
                    "expires_in": 3600,
                    "client_endpoint": "https://portal.bitrix24.test/rest/",
                    "server_endpoint": "https://oauth.bitrix.info/rest/",
                    "member_id": self.member_id,
                    "user_id": 100,
                    "status": "L",
                    "scope": "crm,telephony,placement,user_brief",
                },
            )
        self.rest_calls += 1
        blocked = {
            "error": "QUERY_LIMIT_EXCEEDED",
            "error_description": "Too many requests.",
        }
        if self.shape == "envelope":
            return httpx.Response(503, json=blocked)

        body = request.content.decode() if request.content else ""
        keys = [
            found.group("key")
            for key, _value in parse_qsl(body, keep_blank_values=True)
            if (found := self._CMD_RE.match(key))
        ]
        return httpx.Response(
            200,
            json={
                "result": {
                    "result": {},
                    "result_error": dict.fromkeys(keys, blocked),
                    "result_time": {},
                    "result_next": {},
                    "result_total": {},
                }
            },
        )


async def _park_and_lease(portal_id: int) -> None:
    """Let the parked time pass, then lease the portal exactly as the tick would.

    Both timestamps are shifted by the SAME amount, because that is what waiting does:
    moving `next_run_at` alone would rewrite the very back-off this test is measuring.
    An hour rather than a second so this portal sorts to the front of `acquire_leases`'
    `ORDER BY next_run_at LIMIT n` even when the shared database holds other tenants.
    """
    from sqlalchemy import text

    from app.db.session import control_txn
    from app.sync.lease import WORKER_ID, acquire_leases

    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portal_sync SET "
                "  last_error_at = last_error_at - (next_run_at - (now() - interval '1 hour')), "
                "  next_run_at = now() - interval '1 hour', "
                "  lease_owner = NULL, lease_expires_at = NULL "
                "WHERE portal_id = :pid"
            ),
            {"pid": portal_id},
        )
    leased = [f for f in await acquire_leases(8, WORKER_ID) if f.portal_id == portal_id]
    assert leased, "the portal was not due for a lease; the visit would be a no-op"


async def _sync_row(portal_id: int) -> Mapping[str, Any]:
    from sqlalchemy import text

    from app.db.session import control_txn

    async with control_txn() as session:
        row = (
            await session.execute(
                text("SELECT * FROM portal_sync WHERE portal_id = :pid"), {"pid": portal_id}
            )
        ).mappings().one()
    return dict(row)


@pytest.mark.parametrize("shape", ["envelope", "per_command"], ids=["http-503", "result_error"])
async def test_consecutive_503s_climb_the_backoff_ladder_instead_of_parking_at_the_floor(
    app_engine: Any, shape: str
) -> None:
    """§5.6 - "exponential 2, 4, 8 … 300 s", measured on the row the visit leaves behind.

    Every tenant of this deployment shares one source IP and the bucket drains at 2 req/s
    for the whole account, so a portal that retries a 503 at the 2 s floor for ever is an
    outage for every other portal this worker serves - at exactly the moment the bucket
    is empty. Throttling must still stay out of the failure counters (§5.6 rule 1).
    """
    from app.jobs.definitions import sync_portal
    from tests.fixtures.bitrix import delete_portal, patch_httpx, seed_portal

    seeded = await seed_portal(backfill_status="pending", high_id=0, low_id=None)
    fake = _AlwaysThrottled(seeded.member_id, shape=shape)
    delays: list[float] = []
    rows: list[Mapping[str, Any]] = []
    try:
        with patch_httpx(fake):  # type: ignore[arg-type]
            for _ in range(4):
                await _park_and_lease(seeded.portal_id)
                await sync_portal(seeded.portal_id)
                row = await _sync_row(seeded.portal_id)
                rows.append(row)
                delays.append((row["next_run_at"] - row["last_error_at"]).total_seconds())
    finally:
        await delete_portal(seeded.member_id)

    assert fake.rest_calls >= 4, "every visit must have actually reached the 503"
    assert delays[0] == pytest.approx(2.0, abs=0.5), (
        f"the first 503 parks the portal for the 2 s floor, not {delays[0]:.1f} s"
    )
    assert delays == pytest.approx([2.0, 4.0, 8.0, 16.0], abs=0.6), (
        f"§5.6's 2, 4, 8 … 300 s ladder never escalated: {[round(d, 1) for d in delays]}"
    )
    assert [int(row["throttle_hits"]) for row in rows] == [1, 2, 3, 4]
    assert all(int(row["consecutive_failures"]) == 0 for row in rows), (
        "§5.6 rule 1: throttling is not failure - it must never approach the 6 h pause"
    )
