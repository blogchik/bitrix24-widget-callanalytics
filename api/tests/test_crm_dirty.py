"""The dirty queue, the delete guards and CRM retention, against the real database (§5.12).

Every property here is about rows: a `seq` that must still match, a hold that must keep an id
out of `due`, a tombstone that must NULL every value under a CHECK, a retention job that must
work under row-level security. A mocked session would prove none of it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.crm_items import DEAL_ITEM, ItemRow, parse_item
from app.db.session import control_txn, tenant_txn
from app.services import crm_dirty
from app.sync.crm_dirty_refresh import RefreshOutcome, store_refresh
from app.sync.crm_upsert import upsert_items
from app.sync.lease import WORKER_ID, Fence, acquire_leases
from app.sync.purge import purge_crm_retention
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal
from tests.fixtures.crm import deal_row

pytestmark = pytest.mark.asyncio

DEAL = 2


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


async def _mark(portal_id: int, ids: list[int], reasons: int, now: datetime) -> list[crm_dirty.DirtyId]:
    async with tenant_txn(portal_id) as session:
        await crm_dirty.mark(session, portal_id, DEAL, ids, reasons=reasons, now=now)
        return await crm_dirty.due(session, portal_id, DEAL, now=now, limit=100)


async def _due(portal_id: int, now: datetime) -> list[crm_dirty.DirtyId]:
    async with tenant_txn(portal_id) as session:
        return await crm_dirty.due(session, portal_id, DEAL, now=now, limit=100)


async def _deal(portal_id: int, item_id: int) -> dict[str, Any] | None:
    async with tenant_txn(portal_id) as session:
        row = (
            await session.execute(
                text("SELECT * FROM crm_items WHERE entity_type_id = 2 AND id = :id"), {"id": item_id}
            )
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


async def test_a_mark_that_arrives_mid_refresh_survives_the_consume(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, _fence = leased
    now = datetime.now(UTC)
    read = await _mark(portal.portal_id, [1, 2], crm_dirty.REASON_RECONCILE_LOCAL, now)
    assert [(item.id, item.seq) for item in read] == [(1, 1), (2, 1)]

    # A second source marks id 2 while the refresh that read seq 1 is still running.
    await _mark(portal.portal_id, [2], crm_dirty.REASON_RECONCILE_REMOTE, now)
    async with tenant_txn(portal.portal_id) as session:
        removed = await crm_dirty.consume(session, portal.portal_id, read)

    assert removed == 1
    left = await _due(portal.portal_id, now)
    assert [(item.id, item.seq) for item in left] == [(2, 2)]
    assert left[0].reasons == crm_dirty.REASON_RECONCILE_LOCAL | crm_dirty.REASON_RECONCILE_REMOTE


async def test_defer_backs_off_and_holds_an_id_that_keeps_failing(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, _fence = leased
    now = datetime.now(UTC)
    item = (await _mark(portal.portal_id, [5], crm_dirty.REASON_SIGNAL, now))[0]

    async with tenant_txn(portal.portal_id) as session:
        await crm_dirty.defer(session, portal.portal_id, [item], now=now)
    assert await _due(portal.portal_id, now) == []
    retried = await _due(portal.portal_id, now + timedelta(seconds=61))
    assert [(entry.id, entry.attempts) for entry in retried] == [(5, 1)]

    async with tenant_txn(portal.portal_id) as session:
        await crm_dirty.defer(session, portal.portal_id, [replace(item, attempts=9)], now=now)
    assert await _due(portal.portal_id, now + timedelta(hours=2)) == [], "ten failures: a day's hold"
    assert await _due(portal.portal_id, now + timedelta(hours=25)) != []


async def test_a_confirmed_deletion_is_tombstoned_only_behind_the_admin_guard(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    now = datetime.now(UTC)
    await upsert_items(fence, [_item(1), _item(2), _item(3)], read_at=now - timedelta(minutes=5))
    items = await _mark(portal.portal_id, [2], crm_dirty.REASON_SIGNAL_DELETE, now)
    outcome = RefreshOutcome(read_at=now, deleted=items)

    held = await store_refresh(fence, DEAL, outcome, allow_deletes=False, now=now)
    assert held
    alive = await _deal(portal.portal_id, 2)
    assert alive is not None and alive["deleted_at"] is None, "an unverified installer deletes nothing"
    assert await _due(portal.portal_id, now) == [], "the candidate waits out its hold"
    async with control_txn() as session:
        events = (
            await session.execute(
                text(
                    "SELECT count(*) FROM portal_events "
                    "WHERE portal_id = :pid AND kind = 'crm_delete_held'"
                ),
                {"pid": portal.portal_id},
            )
        ).scalar_one()
    assert events == 1

    assert not await store_refresh(fence, DEAL, outcome, allow_deletes=True, now=now)
    gone = await _deal(portal.portal_id, 2)
    assert gone is not None
    assert gone["delete_reason"] == "not_found" and gone["stage_id"] is None and gone["contact_ids"] is None
    assert await _due(portal.portal_id, now + timedelta(days=2)) == [], "the mark is consumed"


async def test_a_mass_deletion_is_held_by_the_ratio_guard(leased: tuple[SeededPortal, Fence]) -> None:
    portal, fence = leased
    now = datetime.now(UTC)
    ids = list(range(1, 61))
    await upsert_items(fence, [_item(item_id) for item_id in ids], read_at=now - timedelta(minutes=5))
    items = await _mark(portal.portal_id, ids, crm_dirty.REASON_RECONCILE_LOCAL, now)

    outcome = RefreshOutcome(read_at=now, deleted=items)
    held = await store_refresh(fence, DEAL, outcome, allow_deletes=True, now=now)

    assert held, "60 confirmed deletions of 60 records is a permissions change until proven otherwise"
    async with tenant_txn(portal.portal_id) as session:
        tombstones = (
            await session.execute(text("SELECT count(*) FROM crm_items WHERE deleted_at IS NOT NULL"))
        ).scalar_one()
    assert tombstones == 0


async def test_unreadable_keeps_values_and_a_tombstone_blocks_an_older_page(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    now = datetime.now(UTC)
    await upsert_items(fence, [_item(7)], read_at=now - timedelta(minutes=5))
    items = await _mark(portal.portal_id, [7, 8], crm_dirty.REASON_RECONCILE_LOCAL, now)
    by_id = {item.id: item for item in items}

    outcome = RefreshOutcome(read_at=now, unreadable=[by_id[7]], deleted=[by_id[8]])
    await store_refresh(fence, DEAL, outcome, allow_deletes=True, now=now)

    unreadable = await _deal(portal.portal_id, 7)
    assert unreadable is not None and unreadable["unreadable_since"] is not None
    assert unreadable["stage_id"] == "NEW", "a narrowed credential proves nothing about the record"

    never_stored = await _deal(portal.portal_id, 8)
    assert never_stored is not None and never_stored["delete_reason"] == "not_found"
    await upsert_items(fence, [_item(8)], read_at=now - timedelta(minutes=1))
    still_gone = await _deal(portal.portal_id, 8)
    assert still_gone is not None and still_gone["deleted_at"] is not None, (
        "a page read before the deletion must not import the record afterwards"
    )


async def test_retention_forgets_old_tombstones_and_evicts_the_long_unreadable(
    leased: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = leased
    now = datetime.now(UTC)
    await upsert_items(fence, [_item(1), _item(2), _item(3)], read_at=now - timedelta(days=40))
    async with tenant_txn(portal.portal_id) as session:
        await session.execute(
            text(
                """
                UPDATE crm_items SET deleted_at = now() - interval '36 days', delete_reason = 'not_found',
                    category_id = NULL, stage_id = NULL, stage_semantic = NULL, assigned_by_id = NULL,
                    created_time = NULL, updated_time = NULL, moved_time = NULL, closed = NULL,
                    opportunity = NULL, currency_id = NULL, lead_id = NULL, contact_ids = NULL,
                    company_id = NULL, utm_source = NULL, utm_medium = NULL, utm_campaign = NULL,
                    utm_content = NULL, utm_term = NULL
                WHERE id = 1
                """
            )
        )
        await session.execute(
            text("UPDATE crm_items SET unreadable_since = now() - interval '31 days' WHERE id = 2")
        )
        await session.execute(
            text("UPDATE crm_items SET unreadable_since = now() - interval '5 days' WHERE id = 3")
        )

    await purge_crm_retention()

    assert await _deal(portal.portal_id, 1) is None, "a 36-day-old tombstone is forgotten"
    evicted = await _deal(portal.portal_id, 2)
    assert evicted is not None and evicted["delete_reason"] == "evicted" and evicted["stage_id"] is None
    recent = await _deal(portal.portal_id, 3)
    assert recent is not None and recent["deleted_at"] is None and recent["stage_id"] == "NEW"
