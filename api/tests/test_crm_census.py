"""`services/crm_scope.py`: the predicate a census produces, and when one is believed.

The REST half - whether Bitrix24 answered the question that was asked - is pinned by
`test_crm_items.py`'s `census_proven` cases. What is pinned here is the half that decides
what a proven cell then SELECTS, and the three fences that refuse a census rather than
serve a stale one. Both are where an over-show would come from, and an over-show is the one
failure this feature is not allowed to have.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select, text

from app.config import settings
from app.db.models import CrmItem
from app.db.session import control_txn, tenant_txn
from app.services import crm_scope
from tests.conftest import TwoPortals

pytestmark = pytest.mark.asyncio

VIEWER: int = 5151


def _scope(
    *,
    deal_cells: tuple[tuple[int, int], ...] = (),
    lead_assignees: tuple[int, ...] = (),
    deal_verdict: str = crm_scope.VERDICT_OK,
    lead_verdict: str = crm_scope.VERDICT_OK,
    generation: int = 1,
    age_sec: float = 0.0,
) -> crm_scope.ViewerScope:
    return crm_scope.ViewerScope(
        user_id=VIEWER,
        resolved_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_sec),
        sync_generation=generation,
        deal_verdict=deal_verdict,
        lead_verdict=lead_verdict,
        deal_reason="",
        lead_reason="",
        deal_cells=deal_cells,
        lead_assignees=lead_assignees,
        truncated=False,
        commands=0,
    )


async def _count(portal_id: int, predicate) -> int:  # noqa: ANN001 - a SQLAlchemy clause
    async with tenant_txn(portal_id) as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(CrmItem)
                    .where(CrmItem.portal_id == portal_id, predicate)
                )
            ).scalar_one()
        )


# --- what a proven cell selects ----------------------------------------------------------


async def test_an_empty_census_selects_nothing_rather_than_everything(
    two_portals: TwoPortals,
) -> None:
    """The one mistake this module may not make. An omitted term would mean EVERYTHING."""
    a = two_portals.a
    assert await _count(a.portal_id, crm_scope.scope_predicate(_scope())) == 0


async def test_the_deal_grid_is_not_applied_to_leads(two_portals: TwoPortals) -> None:
    """`utm_counts` reads leads and deals in ONE statement.

    A flat predicate appended after `entity_type_id IN (1, 2)` would hand the deal grid to
    leads and the lead grid to deals, so the entity term and the scope term are one term.
    The fixture seeds one deal in funnel 0 for `user_ids[0]` and one lead for `user_ids[1]`.
    """
    a = two_portals.a
    deals_only = crm_scope.scope_predicate(_scope(deal_cells=((0, a.user_ids[0]),)))
    async with tenant_txn(a.portal_id) as session:
        rows = (
            (
                await session.execute(
                    select(CrmItem.entity_type_id).where(
                        CrmItem.portal_id == a.portal_id, deals_only
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "the fixture must seed a deal for this cell"
    assert all(value == 2 for value in rows), "a deal cell must never select a lead"

    leads_only = crm_scope.scope_predicate(_scope(lead_assignees=(a.user_ids[1],)))
    async with tenant_txn(a.portal_id) as session:
        rows = (
            (
                await session.execute(
                    select(CrmItem.entity_type_id).where(
                        CrmItem.portal_id == a.portal_id, leads_only
                    )
                )
            )
            .scalars()
            .all()
        )
    assert all(value == 1 for value in rows), "a lead assignee must never select a deal"


async def test_a_cell_for_another_assignee_selects_nothing(two_portals: TwoPortals) -> None:
    a = two_portals.a
    other = crm_scope.scope_predicate(_scope(deal_cells=((0, 999_999),)))
    assert await _count(a.portal_id, other) == 0


async def test_an_unprovable_entity_selects_nothing_even_holding_cells(
    two_portals: TwoPortals,
) -> None:
    """A verdict the census could not trust must not be rescued by the cells beside it."""
    a = two_portals.a
    broken = _scope(
        deal_cells=((0, a.user_ids[0]),), deal_verdict=crm_scope.VERDICT_UNPROVABLE
    )
    assert await _count(a.portal_id, crm_scope.scope_predicate(broken)) == 0


async def test_a_row_the_installer_cannot_read_is_served_to_nobody_scoped(
    two_portals: TwoPortals,
) -> None:
    """`unreadable_since` means the mirror's copy of its funnel is stale by definition."""
    a = two_portals.a
    cells = ((0, a.user_ids[0]),)
    before = await _count(a.portal_id, crm_scope.scope_predicate(_scope(deal_cells=cells)))
    assert before >= 1, "the fixture must seed a deal for this cell"

    async with tenant_txn(a.portal_id) as session:
        await session.execute(
            text(
                "UPDATE crm_items SET unreadable_since = now() "
                "WHERE portal_id = :pid AND entity_type_id = 2"
            ),
            {"pid": a.portal_id},
        )
    assert await _count(a.portal_id, crm_scope.scope_predicate(_scope(deal_cells=cells))) == 0


# --- when a census is believed -----------------------------------------------------------


async def test_a_stale_census_is_refused(two_portals: TwoPortals) -> None:
    a = two_portals.a
    await _seed(a.portal_id, generation=await _generation(a.portal_id))
    assert await crm_scope.load_scope(a.portal_id, VIEWER) is not None

    future = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=settings.crm_scope_ttl_sec + 60)
    assert await crm_scope.load_scope(a.portal_id, VIEWER, now=future) is None


async def test_a_census_from_a_previous_install_is_refused(two_portals: TwoPortals) -> None:
    """A reinstall bumps `sync_generation` and resets the mirror.

    A proof taken under the previous install describes records that no longer exist, so the
    row is orphaned rather than trusted - and it is not deleted, because the next census
    overwrites it anyway and a delete here would hide what went wrong.
    """
    a = two_portals.a
    await _seed(a.portal_id, generation=await _generation(a.portal_id))
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portal_sync SET sync_generation = sync_generation + 1 "
                "WHERE portal_id = :pid"
            ),
            {"pid": a.portal_id},
        )
    assert await crm_scope.load_scope(a.portal_id, VIEWER) is None


async def test_a_census_that_measured_nothing_is_refused(two_portals: TwoPortals) -> None:
    a = two_portals.a
    await _seed(
        a.portal_id,
        generation=await _generation(a.portal_id),
        deal_verdict=crm_scope.VERDICT_UNPROVABLE,
        lead_verdict=crm_scope.VERDICT_UNPROVABLE,
    )
    assert await crm_scope.load_scope(a.portal_id, VIEWER) is None


async def test_an_empty_portal_is_an_answer_not_a_failure(two_portals: TwoPortals) -> None:
    """`empty` on both sides is a census: the portal had nothing to ask about."""
    a = two_portals.a
    await _seed(
        a.portal_id,
        generation=await _generation(a.portal_id),
        deal_verdict=crm_scope.VERDICT_EMPTY,
        lead_verdict=crm_scope.VERDICT_EMPTY,
    )
    scope = await crm_scope.load_scope(a.portal_id, VIEWER)
    assert scope is not None and scope.measured_nothing is False


# --- helpers -----------------------------------------------------------------------------


async def _generation(portal_id: int) -> int:
    async with control_txn() as session:
        return int(
            (
                await session.execute(
                    text("SELECT sync_generation FROM portal_sync WHERE portal_id = :pid"),
                    {"pid": portal_id},
                )
            ).scalar_one()
        )


async def _seed(
    portal_id: int,
    *,
    generation: int,
    deal_verdict: str = crm_scope.VERDICT_OK,
    lead_verdict: str = crm_scope.VERDICT_OK,
) -> None:
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO crm_viewer_scopes (portal_id, user_id, sync_generation,
                                               deal_verdict, lead_verdict)
                VALUES (:pid, :uid, :gen, :dv, :lv)
                ON CONFLICT (portal_id, user_id) DO UPDATE
                    SET sync_generation = excluded.sync_generation,
                        deal_verdict = excluded.deal_verdict,
                        lead_verdict = excluded.lead_verdict,
                        resolved_at = now()
                """
            ),
            {
                "pid": portal_id,
                "uid": VIEWER,
                "gen": generation,
                "dv": deal_verdict,
                "lv": lead_verdict,
            },
        )
