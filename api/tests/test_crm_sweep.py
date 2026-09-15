"""`sync/crm_sweep.py` - one sweep page against its cursor, without a portal.

The three rules the module docblock argues for are pinned here: the watermark is the server's
clock at the START of a pass, a full page moves only within the pass, and a page the filter
cannot have produced moves nothing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.crm_items import DEAL_ITEM, parse_timestamp
from app.bitrix.errors import AccessDenied, BitrixError
from app.sync.crm_sweep import INITIAL_LOOKBACK, KEY, SweepCursor, apply, bound, command
from tests.fixtures.crm import deal_row

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
OVERLAP = timedelta(minutes=10)
SERVER = "2026-09-15T15:00:00+03:00"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def _answer(
    rows: list[dict[str, Any]], *, error: BitrixError | None = None, date_start: str = SERVER
) -> BatchResult:
    time = {"operating": 1.0, "date_start": date_start}
    command = CommandResult(
        key=KEY, result=None if error else {"items": rows}, error=error, time=time
    )
    return BatchResult(commands=(command,), time=time)


def _apply(cursor: SweepCursor, batch: BatchResult) -> Any:
    return apply(cursor, DEAL_ITEM, batch, overlap=OVERLAP, now=NOW, utm_max_chars=120)


def test_a_short_page_ends_the_pass_at_the_server_clock_of_its_start() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE)
    step = _apply(cursor, _answer([deal_row(5)]))

    assert step.pass_complete
    assert [row.id for row in step.rows] == [5]
    assert step.cursor == SweepCursor(dialect="item", since=parse_timestamp(SERVER))


def test_a_full_page_advances_within_the_pass_and_keeps_its_start() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE)
    first = _apply(cursor, _answer([deal_row(item_id) for item_id in range(1, 51)]))

    assert not first.pass_complete
    assert first.cursor.after_id == 50
    assert first.cursor.since == SINCE, "the watermark moves only when the pass ends"
    assert first.cursor.pass_started == parse_timestamp(SERVER)

    later = _apply(first.cursor, _answer([deal_row(60)], date_start="2026-09-15T15:05:00+03:00"))
    assert later.pass_complete
    assert later.cursor.since == parse_timestamp(SERVER), (
        "the next pass must start where this one STARTED, or an edit made mid-pass to an id "
        "the walk had already passed is never read"
    )


def test_an_id_at_or_below_the_cursor_is_a_violation_and_moves_nothing() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE, after_id=10)
    step = _apply(cursor, _answer([deal_row(10)]))

    assert step.violation is not None
    assert step.cursor == cursor
    assert not step.rows


def test_a_row_updated_before_the_window_is_a_violation() -> None:
    cursor = SweepCursor(dialect="item", since=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
    step = _apply(cursor, _answer([deal_row(3, updatedTime="2026-09-10T10:00:00+03:00")]))

    assert step.violation is not None, "an ignored >=updatedTime would re-read the whole table"
    assert step.cursor == cursor


def test_an_error_keeps_the_cursor() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE, after_id=7)
    step = _apply(cursor, _answer([], error=AccessDenied(description="no CRM rights")))

    assert isinstance(step.error, AccessDenied)
    assert step.cursor == cursor


def test_a_fresh_cursor_reaches_back_an_hour_and_round_trips() -> None:
    cursor = SweepCursor.from_json({}, now=NOW)

    assert cursor == SweepCursor(dialect="item", since=NOW - INITIAL_LOOKBACK)
    started = SweepCursor(dialect="legacy", since=SINCE, after_id=9, pass_started=NOW)
    assert SweepCursor.from_json(started.to_json(), now=NOW) == started


# --- the filter clock (S-A.10) ------------------------------------------------------------------


def test_a_measured_shift_moves_the_bound_the_sweep_sends() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE, shift_seconds=7200, calibrated_at=NOW)

    _key, _method, params = command(cursor, DEAL_ITEM, overlap=OVERLAP)

    assert parse_timestamp(params["filter"][">=updatedTime"]) == SINCE - OVERLAP + timedelta(hours=2)


def test_an_unmeasured_sweep_sends_its_bound_early_and_accepts_the_wide_answer() -> None:
    cursor = SweepCursor(dialect="item", since=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
    nineteen_hours_early = _answer([deal_row(3, updatedTime="2026-09-14T20:00:00+03:00")])

    assert bound(cursor, overlap=OVERLAP) == cursor.since - OVERLAP - timedelta(hours=14)
    assert _apply(cursor, nineteen_hours_early).violation is None, (
        "a row an unmeasured shift can explain is not an ignored filter"
    )
    measured = replace(cursor, shift_seconds=0, calibrated_at=NOW)
    assert _apply(measured, nineteen_hours_early).violation is not None
    five_days_early = _answer([deal_row(4, updatedTime="2026-09-10T10:00:00+03:00")])
    assert _apply(cursor, five_days_early).violation is not None


def test_the_measured_shift_outlives_a_pass_and_a_round_trip() -> None:
    cursor = SweepCursor(dialect="item", since=SINCE, shift_seconds=-13500, calibrated_at=NOW)

    step = _apply(cursor, _answer([deal_row(5)]))

    assert step.pass_complete
    assert (step.cursor.shift_seconds, step.cursor.calibrated_at) == (-13500, NOW)
    assert SweepCursor.from_json(step.cursor.to_json(), now=NOW) == step.cursor
