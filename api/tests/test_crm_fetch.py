"""`sync/crm_fetch.py` against an in-memory CRM: whole walks, and the three reads' guarantees."""

from __future__ import annotations

from app.bitrix.crm_items import DEAL_ITEM, ItemRow
from app.bitrix.errors import classify
from app.sync.crm_fetch import confirm_absent, fetch_by_ids, fetch_ranges
from app.sync.crm_ranges import coverage_floor, plan_ranges
from tests.fixtures.crm import CrmStub


async def test_a_backfill_walks_the_whole_table_newest_first() -> None:
    ids = [i for i in range(1, 1_300) if i % 7 != 0]  # gaps, like real deletions
    crm = CrmStub(ids)
    streams = tuple(plan_ranges(max(ids), width=400))
    seen: list[int] = []
    while True:
        outcome = await fetch_ranges(crm, DEAL_ITEM, streams, max_commands=4, utm_max_chars=120)
        if outcome is None:
            break
        assert outcome.clean
        seen.extend(row.id for row in outcome.rows)
        streams = outcome.streams
    assert sorted(seen) == ids and len(seen) == len(set(seen)), "every id once, none twice"
    assert coverage_floor(streams) == 1
    commands = [command for request in crm.requests for command in request]
    assert all(params["start"] == -1 for _, _, params in commands), "no page ever counts"
    assert all("title" not in params["select"] for _, _, params in commands), "D-3 select only"


async def test_a_build_ignoring_the_filter_parks_instead_of_looping() -> None:
    crm = CrmStub(range(1, 500))
    crm.ignore_filter = True
    streams = tuple(plan_ranges(499, width=100))
    outcome = await fetch_ranges(crm, DEAL_ITEM, streams, max_commands=5, utm_max_chars=120)
    assert outcome is not None and outcome.filter_violation is not None
    assert outcome.streams == streams


async def test_ids_missing_from_a_clean_answer_are_candidates_and_errored_ones_are_unknown() -> None:
    crm = CrmStub([i for i in range(1, 101) if i not in (4, 60)])
    crm.errors[(0, 1)] = classify("QUERY_LIMIT_EXCEEDED")
    outcome = await fetch_by_ids(crm, DEAL_ITEM, list(range(1, 101)), utm_max_chars=120)
    assert all(isinstance(row, ItemRow) for row in outcome.rows)
    assert {row.id for row in outcome.rows} == set(range(1, 51)) - {4}
    assert outcome.missing == {4}, "id 60 sat in the command that errored"
    assert outcome.unresolved == set(range(51, 101))
    assert len(outcome.errors) == 1


async def test_an_unhonoured_id_filter_keeps_nothing_from_that_command() -> None:
    crm = CrmStub(range(1, 200))
    crm.ignore_filter = True
    outcome = await fetch_by_ids(crm, DEAL_ITEM, [150, 151], utm_max_chars=120)
    assert outcome.rows == [] and outcome.missing == set()
    assert outcome.unresolved == {150, 151} and outcome.errors


async def test_confirmation_sorts_candidates_by_what_the_answer_proves() -> None:
    crm = CrmStub([1, 2, 3])
    crm.forbidden.add((2, 2))
    crm.errors[(0, 3)] = classify("OPERATION_TIME_LIMIT")
    outcome = await confirm_absent(crm, 2, [1, 2, 9, 10])
    assert outcome.present == {1}
    assert outcome.access_denied == {2}
    assert outcome.not_found == {9}
    assert set(outcome.unresolved) == {10}


async def test_empty_inputs_cost_no_request() -> None:
    crm = CrmStub()
    assert (await fetch_by_ids(crm, DEAL_ITEM, [], utm_max_chars=120)).rows == []
    assert (await confirm_absent(crm, 2, [0, -1])).present == set()
    assert crm.requests == []
