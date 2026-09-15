"""`sync/crm_reconcile.py` - the count-and-split plan, without a portal or a database.

`test_crm_sync_e2e.py` runs the same plan through the worker against a fake CRM.
"""

from __future__ import annotations

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.crm_items import DEAL_ITEM
from app.bitrix.errors import AccessDenied
from app.sync import crm_reconcile as reconcile


def test_the_first_ranges_cover_every_id_newest_first() -> None:
    large = reconcile.start(reconcile.ReconcileCursor(), 100_000, ranges=20)

    assert large.started and large.marked == 0
    assert len(large.queue) == 20
    assert large.queue[0] == (95_001, 100_001)
    assert large.queue[-1][0] == 1
    covered = sum(hi - lo for lo, hi in large.queue)
    assert covered == 100_000, "every id in exactly one range"

    small = reconcile.start(reconcile.ReconcileCursor(), 300, ranges=20)
    assert small.queue == ((1, 301),), "never narrower than a leaf"


def test_agreeing_ranges_drop_small_ones_are_listed_large_ones_split() -> None:
    cursor = reconcile.ReconcileCursor(
        started=True, queue=((1, 10_001), (10_001, 11_001), (11_001, 12_001))
    )

    after = reconcile.after_counts(
        cursor, [(1, 10_001), (10_001, 11_001)], local=[500, 40], remote=[499, 40]
    )
    assert after.queue == ((11_001, 12_001), (1, 5_001), (5_001, 10_001))
    assert after.leaves == ()

    leafy = reconcile.after_counts(after, [(11_001, 12_001)], local=[3], remote=[4])
    assert leafy.leaves == ((11_001, 12_001, 11_000),)
    assert not leafy.finished


def test_a_count_that_did_not_come_back_is_not_zero() -> None:
    commands = reconcile.count_commands(DEAL_ITEM, [(1, 101), (101, 201)])
    assert [params["start"] for _key, _method, params in commands] == [0, 0], "a count needs start 0"

    refused = BatchResult(
        commands=(
            CommandResult(key="c0", result={"items": []}, error=None, time=None, total=5),
            CommandResult(key="c1", result=None, error=AccessDenied(), time=None),
        ),
        time=None,
    )
    totals, errors = reconcile.read_counts(refused, commands)
    assert totals is None and isinstance(errors[0], AccessDenied)

    no_total = BatchResult(
        commands=(
            CommandResult(key="c0", result={"items": []}, error=None, time=None, total=5),
            CommandResult(key="c1", result={"items": []}, error=None, time=None, total=None),
        ),
        time=None,
    )
    assert reconcile.read_counts(no_total, commands)[0] is None

    answered = BatchResult(
        commands=(
            CommandResult(key="c0", result={"items": []}, error=None, time=None, total=5),
            CommandResult(key="c1", result={"items": []}, error=None, time=None, total=0),
        ),
        time=None,
    )
    assert reconcile.read_counts(answered, commands) == ([5, 0], [])


def test_the_cursor_round_trips_and_a_broken_one_starts_over() -> None:
    cursor = reconcile.ReconcileCursor(
        started=True, queue=((1, 101),), leaves=((101, 201, 150),), marked=3
    )

    assert reconcile.ReconcileCursor.from_json(cursor.to_json()) == cursor
    assert reconcile.ReconcileCursor(started=True).finished
    broken = {"started": True, "leaves": [[10, 5, 0]]}
    assert reconcile.ReconcileCursor.from_json(broken) == reconcile.ReconcileCursor()
