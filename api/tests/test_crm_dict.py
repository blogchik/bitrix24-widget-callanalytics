"""`sync/crm_dict.py` - reading funnels and stages, and forgetting them only on evidence.

The read is driven through a scripted client; the store runs against the real database,
because "a row missing from one read is marked, not deleted" is a statement about rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.crm_items import ENTITY_DEAL, ENTITY_LEAD
from app.bitrix.deals import Funnel
from app.bitrix.errors import AccessDenied, MethodNotFound
from app.db.session import tenant_txn
from app.sync.crm_dict import Dictionary, read_dictionary, store_dictionary
from app.sync.lease import WORKER_ID, Fence, acquire_leases
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

pytestmark = pytest.mark.asyncio

_STATUSES: dict[str, list[dict[str, Any]]] = {
    "DEAL_STAGE": [
        {"STATUS_ID": "NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": "WON", "NAME": "Won", "SORT": "20", "SEMANTICS": "S"},
    ],
    "DEAL_STAGE_3": [{"STATUS_ID": "C3:NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None}],
    "STATUS": [
        {"STATUS_ID": "NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": "CONVERTED", "NAME": "Converted", "SORT": "20", "SEMANTICS": "S"},
        {"STATUS_ID": "JUNK", "NAME": "Junk", "SORT": "30", "SEMANTICS": "F"},
    ],
}


class DictStub:
    """Answers the dictionary methods; `universal=False` is a build without crm.category.list."""

    def __init__(self, *, universal: bool = True, refuse_leads: bool = False) -> None:
        self.universal = universal
        self.refuse_leads = refuse_leads
        self.requests: list[list[tuple[str, str, dict[str, Any]]]] = []

    async def batch(
        self, commands: Sequence[tuple[str, str, dict[str, Any]]], *, halt: int = 0
    ) -> BatchResult:
        self.requests.append(list(commands))
        answers: list[CommandResult] = []
        for key, method, params in commands:
            error = None
            result: Any = None
            if method == "crm.category.list":
                if self.universal:
                    result = {
                        "categories": [
                            {"id": 0, "name": "Main", "sort": 10, "isDefault": "Y"},
                            {"id": 3, "name": "B2B", "sort": 20, "isDefault": "N"},
                        ]
                    }
                else:
                    error = MethodNotFound(description="no such method")
            elif method == "crm.dealcategory.list":
                result = [{"ID": "3", "NAME": "B2B", "SORT": "20"}]
            elif method == "crm.dealcategory.stage.list":
                result = [{"STATUS_ID": f"C{params['id']}:NEW", "NAME": "New", "SORT": "10"}]
            elif method == "crm.status.list":
                entity = params["filter"]["ENTITY_ID"]
                if entity == "STATUS" and self.refuse_leads:
                    error = AccessDenied(description="leads are off")
                else:
                    result = _STATUSES.get(entity, [])
            answers.append(
                CommandResult(key=key, result=result, error=error, time={"operating": 0.1})
            )
        return BatchResult(commands=tuple(answers), time=None)


async def _go(_block: dict[str, Any] | None) -> bool:
    return True


async def test_a_universal_portal_reads_funnels_stages_and_lead_statuses_in_two_batches() -> None:
    stub = DictStub()
    dictionary = await read_dictionary(stub, pace=_go)  # type: ignore[arg-type]

    assert len(stub.requests) == 2
    assert dictionary.complete and not dictionary.errors
    assert {(entity, funnel.id, funnel.name) for entity, funnel in dictionary.funnels} == {
        (ENTITY_DEAL, 0, "Main"),
        (ENTITY_DEAL, 3, "B2B"),
        (ENTITY_LEAD, 0, ""),
    }
    stages = {
        (entity, stage.category_id, stage.status_id, stage.semantic)
        for entity, stage in dictionary.stages
    }
    assert (ENTITY_DEAL, 0, "WON", "S") in stages
    assert (ENTITY_DEAL, 3, "C3:NEW", "P") in stages
    assert (ENTITY_LEAD, 0, "JUNK", "F") in stages


async def test_a_legacy_build_falls_back_and_keeps_the_default_funnel() -> None:
    stub = DictStub(universal=False)
    dictionary = await read_dictionary(stub, pace=_go)  # type: ignore[arg-type]

    assert len(stub.requests) == 3, "one refused universal batch, one legacy, one for stages"
    assert not dictionary.universal
    deal_funnels = sorted(funnel.id for entity, funnel in dictionary.funnels if entity == ENTITY_DEAL)
    assert deal_funnels == [0, 3], "a legacy list without funnel 0 still gets one"
    assert dictionary.complete


async def test_refused_lead_statuses_leave_the_deal_half_complete() -> None:
    dictionary = await read_dictionary(DictStub(refuse_leads=True), pace=_go)  # type: ignore[arg-type]

    assert dictionary.complete_entities == {ENTITY_DEAL}
    assert any(isinstance(error, AccessDenied) for error in dictionary.errors)
    assert not any(entity == ENTITY_LEAD for entity, _ in dictionary.funnels)


async def test_a_stop_before_the_stages_is_an_incomplete_read() -> None:
    async def stop(_block: dict[str, Any] | None) -> bool:
        return False

    dictionary = await read_dictionary(DictStub(), pace=stop)  # type: ignore[arg-type]

    assert ENTITY_DEAL not in dictionary.complete_entities
    assert not any(entity == ENTITY_DEAL for entity, _ in dictionary.stages)


# ------------------------------------------------------------------------------ store


@pytest_asyncio.fixture()
async def leased(app_engine: AsyncEngine) -> AsyncIterator[tuple[SeededPortal, Fence]]:
    portal = await seed_portal(backfill_status="done", crm_mode="sync")
    try:
        fences = [f for f in await acquire_leases(50, WORKER_ID) if f.portal_id == portal.portal_id]
        assert fences, "the seeded portal was not offered a lease"
        yield portal, fences[0]
    finally:
        async with tenant_txn(portal.portal_id) as session:
            for table in TENANT_TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                    {"pid": portal.portal_id},
                )
        await delete_portal(portal.member_id)


async def _funnels(portal_id: int) -> dict[int, dict[str, Any]]:
    async with tenant_txn(portal_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT category_id, name, missing_since FROM crm_funnels "
                    "WHERE entity_type_id = 2"
                )
            )
        ).mappings().all()
    return {int(row["category_id"]): dict(row) for row in rows}


def _deal_dictionary(*funnel_ids: int, complete: bool = True) -> Dictionary:
    return Dictionary(
        funnels=[
            (ENTITY_DEAL, Funnel(id=fid, name=f"F{fid}", sort=fid, is_default=fid == 0))
            for fid in funnel_ids
        ],
        complete_entities={ENTITY_DEAL} if complete else set(),
    )


async def test_a_funnel_no_longer_listed_is_marked_then_removed_a_day_later(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    start = datetime.now(UTC)

    await store_dictionary(fence, _deal_dictionary(0, 3), now=start)
    assert set(await _funnels(portal.portal_id)) == {0, 3}

    marked_at = start + timedelta(hours=1)
    await store_dictionary(fence, _deal_dictionary(0), now=marked_at)
    funnels = await _funnels(portal.portal_id)
    assert funnels[3]["missing_since"] == marked_at, "one read that omits a funnel only marks it"
    assert funnels[0]["missing_since"] is None

    await store_dictionary(fence, _deal_dictionary(0), now=marked_at + timedelta(hours=23))
    assert 3 in await _funnels(portal.portal_id)

    await store_dictionary(fence, _deal_dictionary(0), now=marked_at + timedelta(hours=25))
    assert set(await _funnels(portal.portal_id)) == {0}


async def test_an_incomplete_read_marks_nothing_and_a_returning_funnel_is_unmarked(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    start = datetime.now(UTC)
    await store_dictionary(fence, _deal_dictionary(0, 3), now=start)

    await store_dictionary(fence, _deal_dictionary(0, complete=False), now=start + timedelta(hours=1))
    assert (await _funnels(portal.portal_id))[3]["missing_since"] is None

    await store_dictionary(fence, _deal_dictionary(0), now=start + timedelta(hours=2))
    await store_dictionary(fence, _deal_dictionary(0, 3), now=start + timedelta(hours=3))
    assert (await _funnels(portal.portal_id))[3]["missing_since"] is None
