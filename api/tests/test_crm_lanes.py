"""`sync/crm_lanes.py` - one lane's schedule and back-off, without a database.

A lane is how a CRM failure stays where it happened (§5.10). These pin the three shapes of
"not now": an escalating back-off for a fault, a day's wait for a refusal the portal decides,
and a park that only a person lifts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.bitrix.errors import AccessDenied, UnknownBitrixError
from app.sync import crm_lanes

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _lane(**overrides: Any) -> crm_lanes.Lane:
    values: dict[str, Any] = {"name": crm_lanes.DEAL_SWEEP, "due_at": NOW, **overrides}
    return crm_lanes.Lane(**values)


def test_a_failing_lane_backs_off_escalating_then_pauses_six_hours() -> None:
    lane = _lane()
    delays: list[float] = []
    for _ in range(crm_lanes.FAILURES_BEFORE_PAUSE):
        lane = crm_lanes.failed(lane, UnknownBitrixError("INTERNAL_SERVER_ERROR"), now=NOW)
        assert lane.paused_until is not None
        delays.append((lane.paused_until - NOW).total_seconds())

    assert delays[:7] == [60.0, 120.0, 240.0, 480.0, 960.0, 1920.0, 3600.0]
    assert delays[-1] == 6 * 3600.0
    assert lane.failures == crm_lanes.FAILURES_BEFORE_PAUSE
    assert lane.last_error_code == "INTERNAL_SERVER_ERROR"
    assert not lane.runnable(NOW + timedelta(hours=5))
    assert lane.runnable(NOW + timedelta(hours=6, seconds=1))


def test_a_clean_run_ends_the_streak_and_the_back_off() -> None:
    lane = crm_lanes.failed(_lane(), UnknownBitrixError("X"), now=NOW)
    later = NOW + timedelta(minutes=2)
    lane = crm_lanes.succeeded(
        lane, now=later, due_at=later + timedelta(minutes=5), cursor={"after_id": 7}
    )

    assert lane.failures == 0
    assert lane.paused_until is None and lane.last_error_code is None
    assert lane.last_clean_at == later
    assert lane.cursor == {"after_id": 7}
    assert lane.wakes_at() == later + timedelta(minutes=5)


def test_a_refusal_waits_a_day_without_counting_as_a_failure() -> None:
    lane = crm_lanes.unavailable(_lane(failures=3), AccessDenied().code, now=NOW)

    assert lane.failures == 3, "a portal that switched CRM off is not a broken lane"
    assert lane.block_reason == "ACCESS_DENIED"
    assert lane.paused_until == NOW + timedelta(days=1)
    assert lane.wakes_at() == NOW + timedelta(days=1)


def test_done_and_parked_lanes_never_ask_for_a_visit() -> None:
    finished = _lane(status=crm_lanes.DONE)
    stopped = crm_lanes.parked(_lane(), "filter_unsupported", now=NOW)

    for lane in (finished, stopped):
        assert not lane.runnable(NOW + timedelta(days=30))
        assert lane.wakes_at() is None
    assert stopped.block_reason == "filter_unsupported"
    assert crm_lanes.next_wake([finished, stopped]) is None


def test_the_next_wake_is_the_earliest_lane_counting_its_back_off() -> None:
    soon = _lane(due_at=NOW + timedelta(minutes=5))
    backing_off = _lane(due_at=NOW, paused_until=NOW + timedelta(minutes=2))

    assert backing_off.wakes_at() == NOW + timedelta(minutes=2)
    assert crm_lanes.next_wake([soon, backing_off]) == NOW + timedelta(minutes=2)
