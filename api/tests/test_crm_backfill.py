"""`sync/crm_backfill.py` - the backfill cursor: which ranges exist and what "covered" means.

Pure functions only; `test_crm_sync_e2e.py` walks the same cursor through the worker.
"""

from __future__ import annotations

from dataclasses import replace

from app.bitrix.crm_items import DEAL_ITEM
from app.sync import crm_backfill as backfill


def _planned(high_id: int, *, commands: int = 20, max_width: int = 20_000) -> backfill.BackfillCursor:
    cursor = backfill.headed(backfill.BackfillCursor(), high_id, commands=commands, max_width=max_width)
    return backfill.plan(cursor, streams=commands)


def test_a_small_portal_is_walked_as_parallel_ranges_from_the_first_batch() -> None:
    cursor = _planned(500)

    assert cursor.width == 50, "never narrower than one page"
    assert [(stream.lo, stream.hi) for stream in cursor.open[:2]] == [(451, 501), (401, 451)]
    assert cursor.open[-1].lo == 1
    assert len(cursor.open) == 10
    assert cursor.next_hi == 1, "the whole history is planned"
    assert cursor.progress == (0, 500)


def test_a_large_portal_is_capped_at_the_widest_range_newest_first() -> None:
    cursor = _planned(1_000_000)

    assert cursor.width == 20_000
    assert len(cursor.open) == 20
    assert (cursor.open[0].lo, cursor.open[0].hi) == (980_001, 1_000_001)
    assert cursor.next_hi == 600_001, "only the ranges being walked are planned"


def test_covered_moves_only_over_a_contiguous_prefix_of_done_ranges() -> None:
    cursor = _planned(500)

    streams = list(cursor.open)
    streams[1] = replace(streams[1], done=True)
    gap = backfill.settle(cursor, streams)
    assert gap.covered == 501, "a done range behind an open one must not claim coverage"
    assert len(gap.open) == 10

    streams = list(gap.open)
    streams[0] = replace(streams[0], done=True)
    closed = backfill.settle(gap, streams)
    assert closed.covered == 401
    assert len(closed.open) == 8
    assert closed.progress == (100, 500)


def test_planning_refills_the_window_as_ranges_finish() -> None:
    cursor = _planned(1_000_000, commands=4, max_width=1_000)
    done = backfill.settle(cursor, [replace(stream, done=True) for stream in cursor.open])
    refilled = backfill.plan(done, streams=4)

    assert done.covered == 996_001 and not done.open
    assert [(stream.lo, stream.hi) for stream in refilled.open] == [
        (995_001, 996_001),
        (994_001, 995_001),
        (993_001, 994_001),
        (992_001, 993_001),
    ]


def test_an_empty_table_is_finished_at_once() -> None:
    cursor = backfill.headed(backfill.BackfillCursor(), 0, commands=20, max_width=20_000)

    assert cursor.finished
    assert cursor.progress == (0, 0)


def test_the_cursor_round_trips_and_a_broken_one_starts_over() -> None:
    cursor = _planned(1_234)
    assert backfill.BackfillCursor.from_json(cursor.to_json()) == cursor

    broken = {"dialect": "legacy", "high_id": 5, "open": "junk"}
    assert backfill.BackfillCursor.from_json(broken) == backfill.BackfillCursor(dialect="legacy")
    assert backfill.BackfillCursor.from_json({"dialect": "sql"}).dialect == backfill.DIALECT_ITEM


def test_the_head_is_the_newest_id_zero_when_empty_none_when_unusable() -> None:
    assert backfill.read_high_id({"items": [{"id": 42}, {"id": 41}]}, DEAL_ITEM) == 42
    assert backfill.read_high_id({"items": []}, DEAL_ITEM) == 0
    assert backfill.read_high_id("not a page", DEAL_ITEM) is None
    assert backfill.read_high_id({"items": [{"title": "no id"}]}, DEAL_ITEM) is None
