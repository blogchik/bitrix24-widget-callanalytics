"""`sync/crm_upsert.py` against the real database: versions, tombstones, quarantine, the fence.

Every property here is a database property - the conflict WHERE, the tombstone CHECK, a
varchar that refuses a value, row-level security on the placeholders - so a mocked session
would prove none of it (`test_upsert.py` makes the same argument for calls).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.crm_items import DEAL_ITEM, ItemRow, parse_item
from app.db.session import control_txn, tenant_txn
from app.sync import crm_lanes
from app.sync.crm_upsert import upsert_items
from app.sync.lease import WORKER_ID, Fence, FenceLost, acquire_leases
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal
from tests.fixtures.crm import deal_row

pytestmark = pytest.mark.asyncio

T1 = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=5)
T3 = T2 + timedelta(minutes=5)


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


def _item(item_id: int, **overrides: Any) -> ItemRow:
    parsed = parse_item(deal_row(item_id, **overrides), DEAL_ITEM, utm_max_chars=120)
    assert isinstance(parsed, ItemRow)
    return parsed


async def _row(portal_id: int, item_id: int) -> dict[str, Any]:
    async with tenant_txn(portal_id) as session:
        return dict(
            (
                await session.execute(
                    text("SELECT * FROM crm_items WHERE entity_type_id = 2 AND id = :id"),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )


async def test_rows_land_with_their_assignee_placeholder_and_the_lane(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    lane = crm_lanes.Lane(name=crm_lanes.DEAL_BACKFILL, due_at=T1, cursor={"high_id": 2})

    result = await upsert_items(
        fence,
        [_item(1, utmSource="google", opportunity="99.995"), _item(2)],
        read_at=T1,
        lanes=[lane],
    )

    assert result.written == 2 and result.quarantined == 0
    row = await _row(portal.portal_id, 1)
    assert row["utm_source"] == "google" and row["utm_medium"] == ""
    assert row["opportunity"] == Decimal("100.00"), "half-up to cents, as /utm rounds"
    assert row["contact_ids"] == [12]
    assert row["closed"] is False and row["read_at"] == T1
    async with tenant_txn(portal.portal_id) as session:
        placeholders = (
            await session.execute(text("SELECT bx_user_id, fetched_at FROM employees"))
        ).all()
    assert [(int(uid), fetched) for uid, fetched in placeholders] == [(7, None)]
    async with control_txn() as session:
        stored = (
            await session.execute(
                text("SELECT cursor FROM crm_lanes WHERE portal_id = :pid AND lane = :lane"),
                {"pid": portal.portal_id, "lane": crm_lanes.DEAL_BACKFILL},
            )
        ).scalar_one()
    assert stored == {"high_id": 2}


async def test_an_older_read_never_overwrites_a_newer_one(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    await upsert_items(fence, [_item(5, stageId="WON", stageSemanticId="S")], read_at=T2)
    await upsert_items(fence, [_item(5, stageId="NEW", stageSemanticId="P")], read_at=T1)

    row = await _row(portal.portal_id, 5)
    assert row["stage_id"] == "WON", "a slow page read before the edit undid the edit"
    assert row["read_at"] == T2


async def test_a_newer_read_updates_and_only_a_real_change_moves_content_changed_at(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    await upsert_items(fence, [_item(6)], read_at=T1)
    assert (await _row(portal.portal_id, 6))["content_changed_at"] is None

    await upsert_items(fence, [_item(6, stageId="WON", stageSemanticId="S")], read_at=T2)
    changed = await _row(portal.portal_id, 6)
    assert changed["stage_id"] == "WON" and changed["content_changed_at"] is not None

    await upsert_items(fence, [_item(6, stageId="WON", stageSemanticId="S")], read_at=T3)
    again = await _row(portal.portal_id, 6)
    assert again["read_at"] == T3
    assert again["content_changed_at"] == changed["content_changed_at"]


async def test_a_not_found_tombstone_stays_dead_and_an_evicted_row_comes_back(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    await upsert_items(fence, [_item(8), _item(9)], read_at=T1)
    async with tenant_txn(portal.portal_id) as session:
        for item_id, reason in ((8, "not_found"), (9, "evicted")):
            await session.execute(
                text(
                    """
                    UPDATE crm_items SET deleted_at = now(), delete_reason = :reason,
                        category_id = NULL, stage_id = NULL, stage_semantic = NULL,
                        assigned_by_id = NULL, created_time = NULL, updated_time = NULL,
                        moved_time = NULL, closed = NULL, opportunity = NULL,
                        currency_id = NULL, lead_id = NULL, contact_ids = NULL,
                        company_id = NULL, utm_source = NULL, utm_medium = NULL,
                        utm_campaign = NULL, utm_content = NULL, utm_term = NULL
                    WHERE entity_type_id = 2 AND id = :id
                    """
                ),
                {"id": item_id, "reason": reason},
            )

    await upsert_items(fence, [_item(8), _item(9)], read_at=T2)

    dead = await _row(portal.portal_id, 8)
    assert dead["delete_reason"] == "not_found" and dead["stage_id"] is None, (
        "decision 29: a proven deletion is never undone by a read; a restore arrives as a new id"
    )
    back = await _row(portal.portal_id, 9)
    assert back["deleted_at"] is None and back["delete_reason"] is None
    assert back["stage_id"] == "NEW"


async def test_a_value_the_database_refuses_is_quarantined_and_the_rest_lands(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    poison = replace(_item(11), stage_id="X" * 200)

    result = await upsert_items(fence, [_item(10), poison, _item(12)], read_at=T1)

    assert result.written == 2 and result.quarantined == 1
    async with tenant_txn(portal.portal_id) as session:
        ids = sorted(int(v) for v in (await session.execute(text("SELECT id FROM crm_items"))).scalars())
    assert ids == [10, 12]
    async with control_txn() as session:
        details = (
            await session.execute(
                text(
                    "SELECT details FROM portal_events "
                    "WHERE portal_id = :pid AND kind = 'row_rejected'"
                ),
                {"pid": portal.portal_id},
            )
        ).scalars().all()
    assert [(d["entity_type_id"], d["id"]) for d in details] == [(2, 11)]


async def test_a_lost_fence_commits_nothing(leased: tuple[SeededPortal, Fence]) -> None:
    portal, fence = leased
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portal_sync SET sync_generation = sync_generation + 1 WHERE portal_id = :pid"),
            {"pid": portal.portal_id},
        )

    with pytest.raises(FenceLost):
        await upsert_items(fence, [_item(20)], read_at=T1)

    async with tenant_txn(portal.portal_id) as session:
        assert (await session.execute(text("SELECT count(*) FROM crm_items"))).scalar_one() == 0
        assert (await session.execute(text("SELECT count(*) FROM employees"))).scalar_one() == 0
