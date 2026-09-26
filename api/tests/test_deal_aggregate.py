"""The deal report's pure half: command shapes, parsers and the fold (§4.12).

No HTTP and no database here. What this module protects is the set of mistakes that produce
a report which is *plausible and wrong* - the only failure mode of this feature that nobody
downstream can catch:

* **`DEAL_STAGE_0`.** Bitrix24 answers an empty list with NO error for that entity id, so a
  one-character bug produces a funnel with zero columns and no exception, no log line and
  no symptom at runtime. The assertion below is the only thing that will ever catch it.
* **A bare stage code colliding across funnels.** `STATUS_ID` uniqueness is documented as
  limited to its own directory, and the default funnel's codes are unprefixed, so keying a
  column on the code alone silently merges two funnels' numbers.
* **The period filter being ignored rather than refused.** If a build drops the
  `createdTime` key, the selection becomes every deal the viewer can see and the report is
  simply too big, with every number internally consistent. `honour_verdict` is the guard,
  and its *inconclusive* case matters as much as its negative one.
* **A deal counted for a period it was not created in.** Owner decision 3 counts creation
  alone; a filter that also matched modification or closing would quietly pull old deals
  into every report.
* **A deal whose stage the dictionary does not name.** Dropping it would make a row's
  `total` disagree with the sum of its own cells, which is the one discrepancy a reader can
  see and cannot explain.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.bitrix.deals import (
    DEAL_DIALECT,
    ITEM_DIALECT,
    Dialect,
    as_int,
    honour_probe_commands,
    honour_verdict,
    list_page_commands,
    normalise_semantic,
    page_key,
    parse_funnels,
    parse_stages,
    period_filter,
    stage_entity_id,
    stage_key,
    status_commands,
    status_key,
)
from app.services.deal_stats import _fold, _Group, _Measures

START = "2026-06-01T00:00:00+00:00"
END = "2026-07-01T00:00:00+00:00"


# --- entity ids and keys ----------------------------------------------------------------


def test_stage_entity_id_has_no_zero_suffix() -> None:
    """`DEAL_STAGE`, never `DEAL_STAGE_0` - the silent-empty-funnel bug."""
    assert stage_entity_id(0) == "DEAL_STAGE"
    assert stage_entity_id(10) == "DEAL_STAGE_10"
    # A negative id cannot come from a portal, but the branch must not invent a suffix.
    assert stage_entity_id(-1) == "DEAL_STAGE"


def test_status_command_carries_the_entity_id_the_funnel_needs() -> None:
    commands = status_commands([0, 7], universal=True)
    assert [key for key, _, _ in commands] == [status_key(0), status_key(7)]
    assert commands[0][2]["filter"]["ENTITY_ID"] == "DEAL_STAGE"
    assert commands[1][2]["filter"]["ENTITY_ID"] == "DEAL_STAGE_7"
    # The portal's own kanban order, which is the order the reader already has in mind.
    assert commands[0][2]["order"] == {"SORT": "ASC"}


def test_page_keys_are_unique_and_the_first_is_the_preflight() -> None:
    assert page_key(0) == "pre"
    assert len({page_key(start) for start in (0, 50, 100)}) == 3
    with pytest.raises(ValueError):
        page_key(17)


# --- the period filter --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "created"), [(ITEM_DIALECT, "createdTime"), (DEAL_DIALECT, "DATE_CREATE")]
)
def test_the_period_is_creation_time_and_nothing_else(dialect: Dialect, created: str) -> None:
    """Owner decision 3: a deal modified or closed in the period but created before it is out."""
    assert period_filter(dialect, start_iso=START, end_iso=END) == {
        f">={created}": START,
        f"<{created}": END,
    }


def test_select_never_asks_for_customer_content() -> None:
    commands = list_page_commands(ITEM_DIALECT, filter_={}, starts=[0])
    selected = set(commands[0][2]["select"])
    assert selected == {"id", "categoryId", "stageId", "assignedById", "stageSemanticId"}
    assert commands[0][2]["entityTypeId"] == 2


def test_employee_filter_is_server_side() -> None:
    commands = list_page_commands(ITEM_DIALECT, filter_={}, starts=[0], assigned_to=[7, 9])
    assert commands[0][2]["filter"]["@assignedById"] == [7, 9]


# --- the honour probe ----------------------------------------------------------------------


def test_honour_probe_asks_the_same_flat_key_the_period_sends() -> None:
    """A probe in any other shape could pass on a build that drops the real filter."""
    commands = honour_probe_commands(ITEM_DIALECT)
    assert [key for key, _, _ in commands] == ["hp0", "hp1"]
    assert commands[0][2]["filter"] == {}
    assert commands[1][2]["filter"] == {">=createdTime": "2999-01-01T00:00:00+00:00"}


def test_honour_verdict_is_inconclusive_when_the_viewer_sees_nothing() -> None:
    """A zero baseline proves nothing, and caching it would pin an untested verdict."""
    assert honour_verdict(baseline=0, future=0) is None
    assert honour_verdict(baseline=None, future=0) is None
    assert honour_verdict(baseline=5, future=0) is True
    # A filter that was ignored returns rows from the year 2999 that cannot exist.
    assert honour_verdict(baseline=5, future=5) is False
    assert honour_verdict(baseline=5, future=None) is None


# --- scalars ---------------------------------------------------------------------------------


def test_as_int_accepts_both_spellings_bitrix_uses() -> None:
    """`"1"` and `1` are the same operator; a raw comparison would split their rows."""
    assert as_int("17") == as_int(17) == as_int(17.0) == 17
    assert as_int(True) is None
    assert as_int("") is None
    assert as_int("17a") is None


def test_semantics_folds_every_spelling_of_in_progress() -> None:
    """The docs disagree about null versus ""; an unknown value must not count as won."""
    assert normalise_semantic(None) == "P"
    assert normalise_semantic("") == "P"
    assert normalise_semantic("  ") == "P"
    assert normalise_semantic("apology") == "P"
    assert normalise_semantic("s") == "S"
    assert normalise_semantic("F") == "F"


# --- dictionary parsers -------------------------------------------------------------------------


def test_parse_funnels_reads_both_dictionary_dialects() -> None:
    universal = {
        "categories": [
            {"id": 0, "name": "Общая", "sort": 300, "entityTypeId": 2, "isDefault": "Y"},
            {"id": 7, "name": "Grow Dermozil", "sort": 100, "entityTypeId": 2, "isDefault": "N"},
        ]
    }
    funnels = parse_funnels(universal, universal=True)
    # Sorted by the portal's own `sort`, not by id.
    assert [funnel.id for funnel in funnels] == [7, 0]
    assert funnels[1].is_default is True

    legacy = [{"ID": "0", "NAME": "Общая", "SORT": "10"}, {"ID": "4", "NAME": "B", "SORT": "20"}]
    funnels = parse_funnels(legacy, universal=False)
    assert [funnel.id for funnel in funnels] == [0, 4]
    # No `isDefault` on the frozen method: id 0 is the default by definition there.
    assert funnels[0].is_default is True
    assert funnels[1].is_default is False


def test_parse_stages_keys_on_the_pair_not_the_bare_code() -> None:
    """Two funnels whose default codes collide must not merge into one column."""
    rows: list[dict[str, Any]] = [
        {"STATUS_ID": "NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": "WON", "NAME": "Успех", "SORT": "80", "SEMANTICS": "S"},
    ]
    default = parse_stages(rows, category_id=0)
    other = parse_stages(
        [{"STATUS_ID": "C7:NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": None}],
        category_id=7,
    )
    assert default[0].key == "0:NEW"
    assert other[0].key == "7:C7:NEW"
    assert default[0].key != other[0].key
    assert default[1].semantic == "S"
    # SORT arrives as a string and orders the columns as the kanban shows them.
    assert [stage.sort for stage in default] == [10, 80]


def test_parse_stages_survives_a_row_that_is_not_a_mapping() -> None:
    """One malformed row from a portal must never take a report down."""
    assert parse_stages(["nonsense", None], category_id=0) == []
    assert parse_stages({}, category_id=0) == []


# --- the fold ----------------------------------------------------------------------------------


def _deal(identifier: int, category: int, stage: str, user: int | None, semantic: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "categoryId": category,
        "stageId": stage,
        "assignedById": user,
        "stageSemanticId": semantic,
    }


def test_fold_counts_each_deal_once_and_keeps_the_row_arithmetic() -> None:
    groups: dict[int | None, _Group] = {}
    seen: set[int] = set()
    rows = [
        _deal(1, 7, "C7:NEW", 11, "P"),
        _deal(2, 7, "C7:WON", 11, "S"),
        _deal(3, 7, "C7:LOSE", 12, "F"),
        # The same deal arriving again: the three legs of the fallback union overlap.
        _deal(1, 7, "C7:NEW", 11, "P"),
    ]
    _fold(rows, dialect=ITEM_DIALECT, known={}, groups=groups, seen=seen)

    group = groups[7]
    assert group.subtotal.total == 3
    assert (group.subtotal.won, group.subtotal.lost, group.subtotal.in_progress) == (1, 1, 1)
    # Every deal is filed under a column, so the cells add up to the total.
    assert sum(group.subtotal.cells.values()) + group.subtotal.unknown_stage == 3
    assert group.rows[11].total == 2
    assert group.rows[12].total == 1


def test_fold_keeps_a_deal_whose_stage_the_dictionary_cannot_name() -> None:
    """A dropped deal makes `total` disagree with the sum of its own cells."""
    groups: dict[int | None, _Group] = {}
    _fold(
        [_deal(1, 7, "C7:DELETED_LAST_WEEK", 11, "P")],
        dialect=ITEM_DIALECT,
        known={},
        groups=groups,
        seen=set(),
    )
    group = groups[7]
    assert group.extra_columns == {stage_key(7, "C7:DELETED_LAST_WEEK"): "C7:DELETED_LAST_WEEK"}
    assert group.subtotal.total == sum(group.subtotal.cells.values())


def test_fold_keeps_the_unassigned_row() -> None:
    """`load_hours` keeps its NULL row for the same reason: the totals must agree."""
    groups: dict[int | None, _Group] = {}
    _fold(
        [_deal(1, 0, "NEW", None, "P"), _deal(2, 0, "NEW", 0, "P")],
        dialect=ITEM_DIALECT,
        known={},
        groups=groups,
        seen=set(),
    )
    # `0` is not a user id either; both fold into the one unassigned row.
    assert set(groups[0].rows) == {None}
    assert groups[0].rows[None].total == 2


def test_fold_falls_back_to_the_dictionary_semantic_only_upwards() -> None:
    """A deal that says `S` is won whatever the directory thinks - it is the live record."""
    stages = {stage.key: stage for stage in parse_stages(
        [{"STATUS_ID": "NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": "F"}],
        category_id=0,
    )}
    groups: dict[int | None, _Group] = {}
    _fold(
        [
            # No `stageSemanticId` at all: the dictionary answers.
            {"id": 1, "categoryId": 0, "stageId": "NEW", "assignedById": 11},
            # The deal says success; the stale directory says failure and loses.
            _deal(2, 0, "NEW", 11, "S"),
        ],
        dialect=ITEM_DIALECT,
        known=stages,
        groups=groups,
        seen=set(),
    )
    assert groups[0].subtotal.lost == 1
    assert groups[0].subtotal.won == 1


def test_fold_sums_every_failure_stage_into_lost() -> None:
    """A portal that added refusal reasons has several `F` stages, not one."""
    groups: dict[int | None, _Group] = {}
    _fold(
        [
            _deal(1, 0, "LOSE", 11, "F"),
            _deal(2, 0, "APOLOGY", 11, "F"),
            _deal(3, 0, "DECLINED", 11, "F"),
        ],
        dialect=ITEM_DIALECT,
        known={},
        groups=groups,
        seen=set(),
    )
    assert groups[0].subtotal.lost == 3
    assert len(groups[0].subtotal.cells) == 3


def test_measures_merge_is_additive_across_funnels() -> None:
    left, right = _Measures(), _Measures()
    left.add(semantic="S", column="0:WON")
    right.add(semantic="S", column="0:WON")
    right.add(semantic="F", column="7:C7:LOSE")
    left.merge(right)
    assert left.total == 3
    assert left.cells == {"0:WON": 2, "7:C7:LOSE": 1}
    # The grand total drops `cells` on purpose: two funnels' stages are not comparable.
    assert "cells" not in left.wire(with_cells=False)
