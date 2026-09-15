"""`sync/crm_clock.py` - measuring how a portal shifts a datetime filter, against a stub CRM.

The stub reads `>=updatedTime: v` as `v - filter_shift`, which is what production did on
2026-09-15 (S-A.10). What is pinned here is the property the sweep depends on: a shift comes back
exactly, to the quarter hour, or it does not come back at all.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest

from app.bitrix.client import BatchResult
from app.bitrix.crm_items import DEAL_ITEM
from app.bitrix.errors import AccessDenied
from app.sync import crm_clock
from tests.fixtures.crm import CrmStub

pytestmark = pytest.mark.asyncio

UPDATED = "2026-09-10T14:20:00+03:00"
REFERENCE = 42


def _stub(shift: dt.timedelta) -> CrmStub:
    crm = CrmStub(deal_ids=[40, 41, 42, 43])
    for row in crm.items[2].values():
        row["updatedTime"] = UPDATED
    crm.filter_shift = shift
    return crm


async def _measure(crm: CrmStub) -> crm_clock.Measurement:
    return await crm_clock.measure(
        crm,  # type: ignore[arg-type]
        DEAL_ITEM,
        REFERENCE,
        dt.datetime.fromisoformat(UPDATED),
    )


@pytest.mark.parametrize(
    "shift",
    [
        dt.timedelta(hours=2),
        dt.timedelta(0),
        -dt.timedelta(hours=3, minutes=45),
        dt.timedelta(hours=5, minutes=45),
        dt.timedelta(hours=13, minutes=45),
        -dt.timedelta(hours=14),
    ],
)
async def test_a_shift_is_measured_to_the_quarter_hour_in_two_requests(shift: dt.timedelta) -> None:
    crm = _stub(shift)

    measured = await _measure(crm)

    assert measured.shift == shift
    assert measured.error is None
    assert measured.batches == 2 and len(crm.requests) == 2


async def test_an_ignored_filter_measures_nothing() -> None:
    crm = _stub(dt.timedelta(0))
    crm.ignore_filter = True

    measured = await _measure(crm)

    assert measured.shift is None, "a filter that matches at every bound is not a clock"
    assert measured.batches == 1


async def test_a_record_edited_while_it_is_measured_measures_nothing() -> None:
    class Editing(CrmStub):
        async def batch(
            self, commands: Sequence[tuple[str, str, dict[str, Any]]], *, halt: int = 0
        ) -> BatchResult:
            answered = await super().batch(commands, halt=halt)
            if len(self.requests) == 1:
                self.items[2][REFERENCE]["updatedTime"] = "2026-09-15T09:00:00+03:00"
            return answered

    crm = Editing(deal_ids=[42])
    crm.items[2][REFERENCE]["updatedTime"] = UPDATED
    crm.filter_shift = dt.timedelta(hours=2)

    measured = await _measure(crm)

    assert measured.shift is None
    assert "changed" in measured.reason


async def test_an_error_is_returned_rather_than_guessed_around() -> None:
    crm = _stub(dt.timedelta(hours=2))
    crm.errors[(0, 5)] = AccessDenied(description="no CRM rights")

    measured = await _measure(crm)

    assert measured.shift is None
    assert isinstance(measured.error, AccessDenied)


async def test_only_a_clean_prefix_of_matches_counts() -> None:
    assert crm_clock.prefix_length([True, True, False, False]) == 2
    assert crm_clock.prefix_length([False, False]) == 0
    assert crm_clock.prefix_length([]) == 0
    assert crm_clock.prefix_length([True, False, True]) is None


async def test_a_measurement_is_due_daily_and_fresh_for_ten_minutes() -> None:
    now = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)

    assert crm_clock.due(None, now=now)
    assert not crm_clock.due(now - dt.timedelta(hours=23), now=now)
    assert crm_clock.due(now - dt.timedelta(hours=25), now=now)
    assert crm_clock.fresh(now - dt.timedelta(minutes=5), now=now)
    assert not crm_clock.fresh(now - dt.timedelta(minutes=11), now=now)
    assert not crm_clock.fresh(None, now=now)
