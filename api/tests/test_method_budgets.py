"""`sync/budgets.py` (§5.6 per method) and the worker's slot refill (§5.9).

The decisions are `sync/throttle.py`'s, reused through a `ThrottleState` view, so these
tests pin the mapping onto a budget row rather than re-testing the rules. The worker-level
consequences - one method's limit never parking another - are in `test_sync_e2e.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.errors import classify
from app.config import settings
from app.db.session import control_txn
from app.jobs import definitions
from app.services.portals import mark_uninstalled
from app.sync import budgets, throttle
from app.sync.budgets import MethodBudget
from tests.fixtures.bitrix import delete_portal, seed_portal

NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
RESET = int((NOW + dt.timedelta(minutes=7)).timestamp())


# --- decisions -------------------------------------------------------------------------


def test_below_the_soft_limit_the_accumulator_is_recorded_and_nothing_stops() -> None:
    updated, stop = budgets.observe(
        MethodBudget("user.get"), {"operating": 100.5, "operating_reset_at": RESET}, now=NOW
    )
    assert not stop
    assert updated.operating_seconds == Decimal("100.5") and updated.blocked_until is None


def test_past_the_soft_limit_the_method_waits_for_its_baskets() -> None:
    updated, stop = budgets.observe(
        MethodBudget("user.get"), {"operating": 400, "operating_reset_at": RESET}, now=NOW
    )
    assert stop
    reset = dt.datetime.fromtimestamp(RESET, tz=dt.UTC)
    assert updated.blocked_until == reset + dt.timedelta(seconds=throttle.RESET_GRACE_SECONDS)
    assert (updated.throttle_hits, updated.clean_visits) == (1, 0)


def test_an_absent_measurement_changes_nothing() -> None:
    budget = MethodBudget("app.info", operating_seconds=Decimal("3"))
    assert budgets.observe(budget, None, now=NOW) == (budget, False)


def test_a_429_we_caused_lowers_the_limit_and_a_foreign_one_does_not() -> None:
    error = classify("OPERATION_TIME_LIMIT")
    ours = budgets.on_operation_time_limit(
        MethodBudget("crm.item.list", operating_seconds=Decimal("420")), error, now=NOW
    )
    assert ours.limit_s == 420.0 and ours.blocked_until is not None and ours.throttle_hits == 1
    foreign = budgets.on_operation_time_limit(
        MethodBudget("crm.item.list", operating_seconds=Decimal("20")), error, now=NOW
    )
    assert foreign.limit_s is None and foreign.blocked_until is not None


def test_clean_visits_restore_the_default_limit() -> None:
    budget = MethodBudget("user.get", limit_s=300.0)
    for _ in range(throttle.CLEAN_VISITS_TO_RECOVER - 1):
        budget = budgets.on_clean_visit(budget)
    assert budget.limit_s == 300.0 and budget.clean_visits == throttle.CLEAN_VISITS_TO_RECOVER - 1
    budget = budgets.on_clean_visit(budget)
    assert budget.limit_s is None and budget.clean_visits == 0


def test_blocked_is_strictly_before_the_deadline() -> None:
    budget = MethodBudget("user.get", blocked_until=NOW)
    assert budget.blocked(NOW - dt.timedelta(seconds=1))
    assert not budget.blocked(NOW)


# --- storage ---------------------------------------------------------------------------


async def test_rows_round_trip_and_leave_with_the_install(app_engine: AsyncEngine) -> None:
    seeded = await seed_portal(status="active")
    try:
        row = MethodBudget(
            "user.get",
            operating_seconds=Decimal("12.34"),
            blocked_until=NOW,
            limit_s=333.3,
            throttle_hits=2,
            clean_visits=1,
        )
        async with control_txn() as session:
            await budgets.store_budgets(session, seeded.portal_id, [row])
            await budgets.store_budgets(
                session, seeded.portal_id, [dataclasses.replace(row, throttle_hits=3)]
            )
        async with control_txn() as session:
            loaded = await budgets.load_budgets(session, seeded.portal_id)
        assert loaded == {"user.get": dataclasses.replace(row, throttle_hits=3)}

        async with control_txn() as session:
            assert await mark_uninstalled(session, seeded.portal_id)
        async with control_txn() as session:
            assert await budgets.load_budgets(session, seeded.portal_id) == {}, (
                "an uninstalled portal must not carry its old API budgets into a reinstall"
            )
    finally:
        await delete_portal(seeded.member_id)


# --- slot refill (§5.9) ----------------------------------------------------------------


@pytest.fixture()
def leases(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stub `acquire_leases`: record how many slots each dispatch asked for, lease nothing."""
    asked: list[int] = []

    async def fake_acquire(limit: int, owner: str) -> list[Any]:
        asked.append(limit)
        return []

    monkeypatch.setattr(definitions, "acquire_leases", fake_acquire)
    return asked


async def test_a_finished_visit_refills_its_slot_without_waiting_for_the_tick(
    leases: list[int],
) -> None:
    async def visit() -> None:
        return None

    definitions._spawn(definitions._inflight, 910_001, visit(), "sync_portal", on_done=definitions._refill)
    await definitions._inflight[910_001]
    await asyncio.sleep(0)
    await asyncio.gather(*definitions._refills)
    assert leases == [settings.global_portal_concurrency]
    assert 910_001 not in definitions._inflight


async def test_a_cancelled_visit_starts_nothing(leases: list[int]) -> None:
    async def hang() -> None:
        await asyncio.sleep(3600)

    definitions._spawn(definitions._inflight, 910_002, hang(), "sync_portal", on_done=definitions._refill)
    task = definitions._inflight[910_002]
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert leases == [] and not definitions._refills
