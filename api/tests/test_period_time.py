"""A period cut to the minute: `from_time` / `to_time` on the shared filter parser.

Every period control in the app - dashboard, by-hour, deals, sources - ends in
`stats.parse_filters`, so this file pins what a clock time does there and nowhere else. Three
rules carry the whole feature:

* the start is inclusive and the end is EXCLUSIVE. "To 19:00" stops at 19:00:00, so a record
  created at 19:00:30 is not in the period and two periods sharing a boundary never count one
  record twice;
* a clock time is the viewer's wall clock, converted in the viewer's zone exactly as a whole
  day already is - 10:00 in Tashkent is 05:00 UTC;
* a period has one spelling. A start at 00:00 is the whole-day start and an end at 00:00 is
  the whole of the day before, so `date_to` stays the last day the period touches and every
  per-day series keeps drawing only days that are in it.

These are pure: `parse_filters` needs a principal and a query string, not a database.
"""

from __future__ import annotations

import datetime as dt

import pytest
from starlette.datastructures import QueryParams

from app.config import settings
from app.security.principal import Principal
from app.services import deal_stats, utm_stats
from app.services.stats import CallFilters, FilterError, parse_filters

TASHKENT = "Asia/Tashkent"


def principal(timezone: str = TASHKENT) -> Principal:
    return Principal(
        portal_id=1,
        member_id="m",
        user_id=101,
        is_admin=True,
        access="all",
        timezone=timezone,
        lang="ru",
        placement="DEFAULT",
        entity=None,
        issued_at=0,
    )


def parse(**params: str) -> CallFilters:
    return parse_filters(QueryParams({"period": "custom", **params}), principal())


def utc(*parts: int) -> dt.datetime:
    return dt.datetime(*parts, tzinfo=dt.UTC)


def test_a_whole_day_period_is_unchanged_and_echoes_two_nulls() -> None:
    filters = parse(**{"from": "2026-09-28", "to": "2026-09-29"})
    assert filters.start_utc == utc(2026, 9, 27, 19)
    assert filters.end_utc == utc(2026, 9, 29, 19)
    assert filters.previous_start_utc == utc(2026, 9, 25, 19)
    echo = filters.describe()["range"]
    assert echo["from_time"] is None and echo["to_time"] is None
    assert echo["days"] == 2


def test_clock_times_are_the_viewers_wall_clock_and_the_end_is_exclusive() -> None:
    filters = parse(
        **{"from": "2026-09-28", "from_time": "10:00", "to": "2026-09-29", "to_time": "19:00"}
    )
    # Tashkent is UTC+5: 10:00 local is 05:00 UTC, 19:00 local is 14:00 UTC.
    assert filters.start_utc == utc(2026, 9, 28, 5)
    assert filters.end_utc == utc(2026, 9, 29, 14)
    echo = filters.describe()["range"]
    assert (echo["from"], echo["from_time"]) == ("2026-09-28", "10:00")
    assert (echo["to"], echo["to_time"]) == ("2026-09-29", "19:00")
    assert echo["days"] == 2

    # Exclusive: a record at 19:00:30 is outside, one at 18:59:59 inside - by the same
    # half-open predicate every reader applies (`>= start_utc`, `< end_utc`).
    inside = utc(2026, 9, 29, 13, 59, 59)
    outside = utc(2026, 9, 29, 14, 0, 30)
    assert filters.start_utc <= inside < filters.end_utc
    assert not outside < filters.end_utc


def test_either_end_may_carry_a_time_alone() -> None:
    start_only = parse(**{"from": "2026-09-28", "from_time": "10:30", "to": "2026-09-28"})
    assert start_only.start_utc == utc(2026, 9, 28, 5, 30)
    assert start_only.end_utc == utc(2026, 9, 28, 19)

    end_only = parse(**{"from": "2026-09-28", "to": "2026-09-28", "to_time": "12:00"})
    assert end_only.start_utc == utc(2026, 9, 27, 19)
    assert end_only.end_utc == utc(2026, 9, 28, 7)


def test_midnight_is_the_day_edge_so_a_period_has_one_spelling() -> None:
    spelt_with_midnights = parse(
        **{"from": "2026-09-28", "from_time": "00:00", "to": "2026-09-30", "to_time": "00:00"}
    )
    whole_days = parse(**{"from": "2026-09-28", "to": "2026-09-29"})
    assert spelt_with_midnights == whole_days, (
        "`to 30 Sep 00:00` (exclusive) is the whole of 29 Sep; it must parse to the same "
        "period, or the per-day series draws an empty 30 Sep that is not in the period"
    )


def test_the_previous_window_of_a_timed_period_is_the_same_length_in_time() -> None:
    filters = parse(
        **{"from": "2026-09-28", "from_time": "10:00", "to": "2026-09-28", "to_time": "19:00"}
    )
    assert filters.end_utc - filters.start_utc == dt.timedelta(hours=9)
    assert filters.previous_start_utc == filters.start_utc - dt.timedelta(hours=9)


@pytest.mark.parametrize(
    "params",
    [
        # The end is not after the start on the same day.
        {"from": "2026-09-28", "from_time": "19:00", "to": "2026-09-28", "to_time": "10:00"},
        {"from": "2026-09-28", "from_time": "10:00", "to": "2026-09-28", "to_time": "10:00"},
        # An end at 00:00 of the start day is the day before it, i.e. backwards.
        {"from": "2026-09-28", "to": "2026-09-28", "to_time": "00:00"},
        # Malformed clocks: the picker only ever sends zero-padded 24-hour minutes.
        {"from": "2026-09-28", "from_time": "9:00", "to": "2026-09-28"},
        {"from": "2026-09-28", "from_time": "24:00", "to": "2026-09-28"},
        {"from": "2026-09-28", "from_time": "10:00:00", "to": "2026-09-28"},
        {"from": "2026-09-28", "to": "2026-09-28", "to_time": "7pm"},
        # A time with no date to belong to.
        {"from_time": "10:00"},
        # A timestamp in the date field is still not a date.
        {"from": "2026-09-28T10:00", "to": "2026-09-29"},
        # The start of the calendar has no day before it for an end at 00:00.
        {"from": "0001-01-01", "to": "0001-01-01", "to_time": "00:00"},
    ],
)
def test_a_period_that_cannot_be_honoured_is_bad_period(params: dict[str, str]) -> None:
    with pytest.raises(FilterError) as caught:
        parse(**params)
    assert caught.value.code == "bad_period"


def test_the_day_cap_counts_calendar_days_the_period_touches() -> None:
    start = dt.date(2025, 1, 1)
    last = start + dt.timedelta(days=settings.max_period_days - 1)
    inside = parse(
        **{"from": start.isoformat(), "from_time": "23:00", "to": last.isoformat(), "to_time": "01:00"}
    )
    assert inside.days == settings.max_period_days
    with pytest.raises(FilterError) as caught:
        parse(
            **{
                "from": start.isoformat(),
                "from_time": "23:00",
                "to": (last + dt.timedelta(days=1)).isoformat(),
                "to_time": "01:00",
            }
        )
    assert caught.value.code == "period_too_long"


def test_the_live_crm_reports_take_the_time_and_send_it_to_bitrix24_with_an_offset() -> None:
    params = QueryParams(
        {
            "period": "custom",
            "from": "2026-09-28",
            "from_time": "10:00",
            "to": "2026-09-29",
            "to_time": "19:00",
        }
    )
    deals = deal_stats.parse_deal_filters(params, principal())
    sources, _ = utm_stats.parse_utm_filters(params, principal())
    for filters in (deals, sources):
        assert filters.start_utc == utc(2026, 9, 28, 5)
        assert filters.end_utc == utc(2026, 9, 29, 14)
    # What `>=DATE_CREATE` / `<DATE_CREATE` receive: an instant, never a bare local date.
    assert deal_stats._iso(deals.start_utc) == "2026-09-28T05:00:00+00:00"
    assert deal_stats._iso(deals.end_utc) == "2026-09-29T14:00:00+00:00"
