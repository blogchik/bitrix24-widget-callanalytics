"""`sync/crm_ranges.py`: newest-first id ranges, the keyset walk, and its two guards."""

from __future__ import annotations

from typing import Any

import pytest

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.crm_items import DEAL_ITEM
from app.bitrix.errors import classify
from app.sync.crm_ranges import (
    RangeStream,
    apply_range_batch,
    coverage_floor,
    plan_ranges,
    range_commands,
)
from tests.fixtures.crm import deal_row


def answer(
    sent: list[tuple[int, Any]], pages: dict[int, Any], errors: dict[int, str] | None = None
) -> BatchResult:
    """A batch answering each sent command with `pages[stream index]` ids as deal rows."""
    results = []
    for index, (key, _method, _params) in sent:
        if errors and index in errors:
            results.append(CommandResult(key=key, result=None, error=classify(errors[index]), time=None))
            continue
        value = pages[index]
        result = {"items": [deal_row(i) for i in value]} if isinstance(value, list) else value
        results.append(CommandResult(key=key, result=result, error=None, time={"operating": 1.5}))
    return BatchResult(commands=tuple(results), time={"operating": 1.5})


# --- planning --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("high", "width"), [(1, 1), (7, 3), (100, 100), (101, 100), (20_000, 2_000), (12_345, 777)]
)
def test_ranges_cover_every_id_once_newest_first(high: int, width: int) -> None:
    streams = plan_ranges(high, width=width)
    covered = [i for stream in streams for i in range(stream.lo, stream.hi)]
    assert sorted(covered) == list(range(1, high + 1)), "every id exactly once"
    assert [s.lo for s in streams] == sorted((s.lo for s in streams), reverse=True), "newest first"
    assert all(s.cursor == s.lo - 1 and not s.done for s in streams)


def test_an_empty_table_plans_nothing() -> None:
    assert plan_ranges(0, width=10) == []


def test_a_floor_bounds_the_plan() -> None:
    assert min(s.lo for s in plan_ranges(100, width=30, floor=41)) == 41


def test_stream_validation() -> None:
    with pytest.raises(ValueError):
        RangeStream(lo=5, hi=5, cursor=4)
    with pytest.raises(ValueError):
        RangeStream(lo=5, hi=10, cursor=10)
    with pytest.raises(ValueError):
        plan_ranges(10, width=0)


def test_coverage_floor_is_the_contiguous_done_prefix() -> None:
    streams = [RangeStream(lo=20, hi=30, cursor=29, done=True), RangeStream(lo=10, hi=20, cursor=9),
               RangeStream(lo=1, hi=10, cursor=9, done=True)]
    assert coverage_floor(streams) == 20, "the older done range does not count past an open one"
    assert coverage_floor(streams[1:]) is None


def test_commands_skip_done_ranges_and_respect_the_cap() -> None:
    streams = [RangeStream(lo=20, hi=30, cursor=29, done=True), *plan_ranges(19, width=5)]
    sent = range_commands(DEAL_ITEM, streams, max_commands=2)
    assert [index for index, _ in sent] == [1, 2]
    assert sent[0][1][2]["filter"] == {">id": 14, "<id": 20}
    with pytest.raises(ValueError):
        range_commands(DEAL_ITEM, streams, max_commands=0)


# --- applying an answer ----------------------------------------------------------------


def test_a_full_page_advances_and_a_short_page_finishes() -> None:
    streams = plan_ranges(200, width=100)
    sent = range_commands(DEAL_ITEM, streams, max_commands=2)
    full = list(range(101, 151))
    batch = answer(sent, {0: full, 1: [3, 4]})
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, batch, utm_max_chars=120)
    assert outcome.clean
    assert (outcome.streams[0].cursor, outcome.streams[0].done) == (150, False)
    assert (outcome.streams[1].cursor, outcome.streams[1].done) == (4, True)
    assert [row.id for row in outcome.rows] == [*full, 3, 4]
    assert outcome.time_block is not None and outcome.time_block["operating"] == 1.5


def test_an_empty_page_finishes_without_moving() -> None:
    streams = plan_ranges(10, width=10)
    sent = range_commands(DEAL_ITEM, streams, max_commands=1)
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, answer(sent, {0: []}), utm_max_chars=120)
    assert outcome.streams[0].done and outcome.streams[0].cursor == 0


def test_an_error_holds_only_its_own_range() -> None:
    streams = plan_ranges(300, width=100)
    sent = range_commands(DEAL_ITEM, streams, max_commands=3)
    batch = answer(sent, {0: [201], 2: [1]}, errors={1: "QUERY_LIMIT_EXCEEDED"})
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, batch, utm_max_chars=120)
    assert not outcome.clean and [index for index, _ in outcome.errors] == [1]
    assert outcome.streams[1] == streams[1], "the errored range did not move"
    assert outcome.streams[0].done and outcome.streams[2].done, "independent ranges still progress"


def test_an_unusable_shape_is_an_error_not_an_empty_page() -> None:
    streams = plan_ranges(10, width=10)
    sent = range_commands(DEAL_ITEM, streams, max_commands=1)
    batch = answer(sent, {0: {"rows": [deal_row(1)]}})  # a map without `items`
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, batch, utm_max_chars=120)
    assert outcome.errors and outcome.streams[0] == streams[0]


@pytest.mark.parametrize("ids", [[5, 250], [7, 6], [4, 4], [300]])
def test_a_page_breaking_its_range_neuters_the_whole_batch(ids: list[int]) -> None:
    streams = plan_ranges(200, width=100)
    other = RangeStream(lo=101, hi=201, cursor=100)
    streams = [other, RangeStream(lo=1, hi=101, cursor=0)]
    sent = range_commands(DEAL_ITEM, streams, max_commands=2)
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, answer(sent, {0: [150], 1: ids}), utm_max_chars=120)
    assert outcome.filter_violation is not None
    assert list(outcome.streams) == streams, "no range moves when any page broke its filter"


def test_a_refused_row_is_reported_and_the_cursor_follows_the_readable_ids() -> None:
    streams = plan_ranges(100, width=100)
    sent = range_commands(DEAL_ITEM, streams, max_commands=1)
    page = {"items": [deal_row(3), {"id": "bogus"}, deal_row(9)]}
    outcome = apply_range_batch(DEAL_ITEM, streams, sent, answer(sent, {0: page}), utm_max_chars=120)
    assert outcome.clean and [row.id for row in outcome.rows] == [3, 9]
    assert outcome.rejected == [(None, "missing or non-positive id")]
    assert outcome.streams[0].cursor == 9 and outcome.streams[0].done


def test_a_full_page_ending_on_the_last_id_of_its_range_finishes_it() -> None:
    """A range read to its last id by a full page is done: the next command would ask for
    ids above hi - 1 and below hi, a range with no ids, which the builder refuses."""
    streams = [RangeStream(lo=11, hi=61, cursor=10)]
    sent = range_commands(DEAL_ITEM, streams, max_commands=5)
    batch = answer(sent, {0: list(range(11, 61))})

    outcome = apply_range_batch(DEAL_ITEM, streams, sent, batch, utm_max_chars=120)

    assert outcome.streams[0].cursor == 60
    assert outcome.streams[0].done
    assert range_commands(DEAL_ITEM, outcome.streams, max_commands=5) == []
