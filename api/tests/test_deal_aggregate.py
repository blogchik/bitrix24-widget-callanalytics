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
* **A deal counted for a period it was not created in.** Owner decision 3 counts creation,
  and since 2026-10-02 movement into or modification on a stage the administrator NAMED -
  and nothing else. A filter that matched modification on every stage would quietly pull
  old deals into every report; one that lost the stage would pull every won deal ever into
  this month's «Успешные».
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
    Stage,
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
    stage_leg_filters,
    status_commands,
    status_key,
)
from app.services import deal_period
from app.services.deal_stats import _fold, _Group, _Measures

START = "2026-06-01T00:00:00+00:00"
END = "2026-07-01T00:00:00+00:00"
NEVER = "2999-01-01T00:00:00+00:00"


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


# --- named stages (owner decision 3, 2026-10-02) ---------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "stage", "moved", "updated"),
    [
        (ITEM_DIALECT, "stageId", "movedTime", "updatedTime"),
        (DEAL_DIALECT, "STAGE_ID", "MOVED_TIME", "DATE_MODIFY"),
    ],
)
def test_a_named_stage_adds_two_flat_legs_on_movement_and_modification(
    dialect: Dialect, stage: str, moved: str, updated: str
) -> None:
    """Flat keys on both dialects: one shape, and a probe that tests exactly what is sent."""
    names = ["C16:UC_0U9IW2", "C16:WON"]
    moved_leg, updated_leg = stage_leg_filters(
        dialect, stage_ids=names, start_iso=START, end_iso=END
    )
    assert moved_leg == {f"@{stage}": names, f">={moved}": START, f"<{moved}": END}
    assert updated_leg == {f"@{stage}": names, f">={updated}": START, f"<{updated}": END}


def test_the_stage_legs_page_keys_never_collide_with_the_creation_stream() -> None:
    """Stream 0 keeps the keys it always had; the legs get their own."""
    keys = [page_key(start, stream) for stream in (0, 1, 2) for start in (0, 50, 100)]
    assert len(set(keys)) == len(keys)
    assert (page_key(0, 0), page_key(50, 0)) == ("pre", "p50")
    assert (page_key(0, 1), page_key(50, 2)) == ("s1p0", "s2p50")
    with pytest.raises(ValueError):
        page_key(0, -1)


def test_the_leg_probes_are_sent_only_for_a_portal_with_named_stages() -> None:
    """A portal without a rule asks exactly the two questions it always asked."""
    assert [key for key, _, _ in honour_probe_commands(ITEM_DIALECT)] == ["hp0", "hp1"]
    commands = honour_probe_commands(ITEM_DIALECT, legs=True)
    assert [key for key, _, _ in commands] == ["hp0", "hp1", "hp2", "hp3"]
    assert commands[2][2]["filter"] == {">=movedTime": NEVER}
    assert commands[3][2]["filter"] == {">=updatedTime": NEVER}


def test_honour_verdict_needs_every_leg_probe_at_zero() -> None:
    """An ignored `>=movedTime` would put every won deal ever into this month's column."""
    assert honour_verdict(baseline=5, future=0, legs=[0, 0]) is True
    assert honour_verdict(baseline=5, future=0, legs=[3, 0]) is False
    assert honour_verdict(baseline=5, future=0, legs=[0, 3]) is False
    assert honour_verdict(baseline=5, future=0, legs=[0, None]) is None
    assert honour_verdict(baseline=0, future=0, legs=[0, 0]) is None


def test_a_leg_row_outside_the_named_stages_is_skipped_and_not_marked_seen() -> None:
    """The legs ask by bare status id; the pair is what decides, and the deal may come back."""
    groups: dict[int | None, _Group] = {}
    seen: set[int] = set()
    named = frozenset({"16:C16:WON"})
    _fold(
        [
            # A build that ignored `@stageId` hands back a stage nobody named.
            _deal(1, 16, "C16:NEW", 11, "P"),
            _deal(2, 16, "C16:WON", 11, "S"),
            # The same status id in another funnel is a different column.
            _deal(3, 18, "C16:WON", 11, "S"),
        ],
        dialect=ITEM_DIALECT,
        known={},
        groups=groups,
        seen=seen,
        only=named,
    )
    assert seen == {2}
    # Deal 1 was also created in the period: the creation stream still counts it.
    _fold([_deal(1, 16, "C16:NEW", 11, "P")], dialect=ITEM_DIALECT, known={}, groups=groups, seen=seen)
    assert groups[16].subtotal.total == 2
    assert 18 not in groups


def _dictionary() -> dict[int, tuple[Stage, ...]]:
    """Portal 1's two working funnels, as they stood on 2026-10-02."""
    return {
        16: tuple(
            parse_stages(
                [
                    {"STATUS_ID": "C16:NEW", "NAME": "Новый", "SORT": "10", "SEMANTICS": None},
                    {"STATUS_ID": "C16:UC_0U9IW2", "NAME": "Заклад", "SORT": "80", "SEMANTICS": None},
                    {"STATUS_ID": "C16:WON", "NAME": "Успешний", "SORT": "90", "SEMANTICS": "S"},
                    {"STATUS_ID": "C16:LOSE", "NAME": "Цена дорогая", "SORT": "100", "SEMANTICS": "F"},
                ],
                category_id=16,
            )
        ),
        18: tuple(
            parse_stages(
                [
                    {"STATUS_ID": "C18:NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": None},
                    {"STATUS_ID": "C18:WON", "NAME": "Успешний", "SORT": "60", "SEMANTICS": "S"},
                ],
                category_id=18,
            )
        ),
    }


def test_the_rule_resolves_to_the_named_stages_the_dictionary_still_has() -> None:
    """In the dictionary's order; a stage that is gone simply drops out."""
    rule = {"stage_keys": ["16:C16:WON", "16:C16:UC_0U9IW2", "16:C16:GONE"]}
    resolved = deal_period.resolve(rule, _dictionary())
    assert [stage.key for stage in resolved] == ["16:C16:UC_0U9IW2", "16:C16:WON"]
    # Per stage, never per outcome: «База»'s won stage is not named, so it is not a leg.
    assert "18:C18:WON" not in {stage.key for stage in resolved}
    assert deal_period.resolve({}, _dictionary()) == ()
    assert deal_period.resolve(None, _dictionary()) == ()


def test_a_stored_rule_that_is_malformed_reads_as_creation_time_alone() -> None:
    """A hand-edited row degrades to today's report, never to a 500."""
    assert deal_period.stage_keys({"stage_keys": "16:C16:WON"}) == ()
    assert deal_period.stage_keys({"stage_keys": [16]}) == ()
    assert deal_period.stage_keys({"stage_keys": ["C16:WON"]}) == ()
    assert deal_period.stage_keys({"other": []}) == ()
    assert deal_period.stage_keys({"stage_keys": ["16:C16:WON", "16:C16:WON"]}) == ("16:C16:WON",)


def test_the_settings_body_is_shape_checked_and_validated_against_the_dictionary() -> None:
    assert deal_period.parse_body({"stage_keys": ["16:C16:WON"]}) == ("16:C16:WON",)
    assert deal_period.parse_body({"stage_keys": []}) == ()
    assert deal_period.parse_body({"stage_keys": ["16:C16:WON"], "won": True}) is None
    assert deal_period.parse_body({"stage_keys": ["16: spaced"]}) is None
    assert deal_period.parse_body(["16:C16:WON"]) is None
    too_many = [f"16:S{index}" for index in range(deal_period.MAX_STAGE_KEYS + 1)]
    assert deal_period.parse_body({"stage_keys": too_many}) is None

    stages = _dictionary()
    assert deal_period.validate(("16:C16:WON", "16:C16:UC_0U9IW2"), stages) == (
        "16:C16:UC_0U9IW2",
        "16:C16:WON",
    )
    assert deal_period.validate(("16:C16:GONE",), stages) is None
    assert deal_period.validate((), stages) == ()
