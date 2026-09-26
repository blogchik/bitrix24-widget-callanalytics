"""The CRM mirror's read path against the real database (§4.14 constraint 5).

The promise is that a report built from Postgres IS the report the live read would have drawn
from the same records, apart from the fields that name where it came from. So the parity tests
fold the same rows twice: once through the live `_fold` the POST path runs on Bitrix24's pages,
and once into `crm_items` through the worker's own parser and upsert and back out through the
mirror SQL. Then the two wire bodies are compared whole.

The rows are chosen to exercise what a plausible-but-wrong mirror would get wrong: a deal
created in the period and closed after it (counted), deals modified or closed in the period but
created before it (not counted - the deal period is creation time alone), a tombstone, the
explicit-P quirk G0 Q3 keeps until cutover, a stage the dictionary does not know, an amount that
rounds, a record without one, a tag longer than the cut, and a creation time that falls on the
next local day in the viewer's zone.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Mapping
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import QueryParams

from app.bitrix import crm_items
from app.bitrix.deals import ITEM_DIALECT, Funnel, parse_stages
from app.bitrix.utm import DEAL_ITEM as UTM_DEAL
from app.bitrix.utm import DIMENSIONS, KIND_DEAL, KIND_LEAD
from app.bitrix.utm import LEAD_ITEM as UTM_LEAD
from app.config import settings
from app.db.models import Portal
from app.db.session import control_txn, tenant_txn
from app.security.principal import Principal, PrincipalError
from app.services import crm_repo, crm_shadow, deal_stats, utm_stats
from app.services.stats import parse_filters
from app.sync import crm_lanes
from app.sync.crm_upsert import upsert_items
from app.sync.lease import WORKER_ID, Fence, acquire_leases
from app.tools import crm_mode
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal
from tests.fixtures.crm import deal_row

pytestmark = pytest.mark.asyncio

GROW = 7
DEFAULT_FUNNEL = 0
VIEWER = 101
OTHER = 202
TZ = "Asia/Tashkent"
JUNE = QueryParams({"period": "custom", "from": "2026-06-01", "to": "2026-06-30"})
IN = "2026-06-10T12:00:00+05:00"
BEFORE = "2026-05-10T12:00:00+05:00"
AFTER = "2026-07-10T12:00:00+05:00"

_FUNNELS: tuple[tuple[int, str, int, bool], ...] = (
    (GROW, "Grow Dermozil", 100, False),
    (DEFAULT_FUNNEL, "Общая", 300, True),
)
_SOURCE_KEYS = ("list_dialect", "dictionary_dialect", "rest_requests", "from_cache")


# --- fixtures and helpers ---------------------------------------------------------------------


@pytest_asyncio.fixture()
async def mirrored(app_engine: AsyncEngine) -> AsyncIterator[tuple[SeededPortal, Fence]]:
    crm_repo.reset_report_gate()
    portal = await seed_portal(backfill_status="done", crm_mode="mirror")
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


def viewer(portal: SeededPortal, *, access: str = "all") -> Principal:
    return Principal(
        portal_id=portal.portal_id,
        member_id=portal.member_id,
        user_id=VIEWER,
        is_admin=access == "all",
        access=access,
        timezone=TZ,
        lang="ru",
        placement="DEFAULT",
        entity=None,
        issued_at=0,
    )


async def portal_row(portal_id: int) -> Portal:
    async with control_txn() as session:
        return (await session.execute(select(Portal).where(Portal.id == portal_id))).scalar_one()


async def set_lane(
    portal_id: int,
    lane: str,
    *,
    status: str = crm_lanes.ACTIVE,
    block_reason: str | None = None,
    done: int = 0,
    total: int | None = None,
    clean: dt.datetime | None = None,
) -> None:
    async with control_txn() as session:
        await session.execute(
            text(
                """
                INSERT INTO crm_lanes (portal_id, lane, status, block_reason, progress_done,
                                       progress_total, last_clean_at)
                VALUES (:pid, :lane, :status, :reason, :done, :total, :clean)
                ON CONFLICT (portal_id, lane) DO UPDATE SET
                    status = excluded.status, block_reason = excluded.block_reason,
                    progress_done = excluded.progress_done,
                    progress_total = excluded.progress_total,
                    last_clean_at = excluded.last_clean_at
                """
            ),
            {
                "pid": portal_id,
                "lane": lane,
                "status": status,
                "reason": block_reason,
                "done": done,
                "total": total,
                "clean": clean,
            },
        )


async def loaded(portal_id: int, *, now: dt.datetime, leads: bool = True) -> None:
    """Lanes as a portal whose history the worker has finished reading."""
    await set_lane(portal_id, crm_lanes.DEAL_BACKFILL, status=crm_lanes.DONE, done=9, total=9, clean=now)
    await set_lane(portal_id, crm_lanes.DEAL_SWEEP, clean=now)
    await set_lane(
        portal_id,
        crm_lanes.LEAD_BACKFILL,
        status=crm_lanes.DONE if leads else crm_lanes.ACTIVE,
        block_reason=None if leads else "ACCESS_DENIED",
        done=5,
        total=5,
        clean=now,
    )
    await set_lane(portal_id, crm_lanes.LEAD_SWEEP, clean=now)


def _stage_rows(category_id: int) -> list[dict[str, Any]]:
    prefix = f"C{category_id}:" if category_id else ""
    return [
        {"STATUS_ID": f"{prefix}NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": f"{prefix}WON", "NAME": "Успех", "SORT": "40", "SEMANTICS": "S"},
        {"STATUS_ID": f"{prefix}LOSE", "NAME": "Отказ", "SORT": "50", "SEMANTICS": "F"},
    ]


def live_dictionary() -> Any:
    """The dictionary the live read builds from `crm.category.list` + `crm.status.list`."""
    funnels = sorted(
        (Funnel(id=cid, name=name, sort=sort, is_default=default) for cid, name, sort, default in _FUNNELS),
        key=lambda funnel: (funnel.sort, funnel.id),
    )
    return deal_stats._Dictionary(
        funnels=tuple(funnels),
        stages={cid: tuple(parse_stages(_stage_rows(cid), category_id=cid)) for cid, *_ in _FUNNELS},
        failed=(),
        truncated=(),
        dialect="category",
    )


async def seed_dictionary(portal_id: int) -> None:
    """The same dictionary as the worker stores it, plus a stage the lane stopped seeing."""
    async with tenant_txn(portal_id) as session:
        for cid, name, sort, default in _FUNNELS:
            await session.execute(
                text(
                    "INSERT INTO crm_funnels (portal_id, entity_type_id, category_id, name, sort, "
                    "is_default) VALUES (:pid, 2, :cid, :name, :sort, :default)"
                ),
                {"pid": portal_id, "cid": cid, "name": name, "sort": sort, "default": default},
            )
            for stage in parse_stages(_stage_rows(cid), category_id=cid):
                await session.execute(
                    text(
                        "INSERT INTO crm_stages (portal_id, entity_type_id, category_id, status_id, "
                        "name, sort, semantic) VALUES (:pid, 2, :cid, :sid, :name, :sort, :semantic)"
                    ),
                    {
                        "pid": portal_id,
                        "cid": cid,
                        "sid": stage.status_id,
                        "name": stage.name,
                        "sort": stage.sort,
                        "semantic": stage.semantic,
                    },
                )
        await session.execute(
            text(
                "INSERT INTO crm_stages (portal_id, entity_type_id, category_id, status_id, name, "
                "sort, semantic, missing_since) VALUES (:pid, 2, :cid, 'C7:OLD', 'Старая', 5, 'S', now())"
            ),
            {"pid": portal_id, "cid": GROW},
        )


async def store(fence: Fence, rows: list[dict[str, Any]], dialect: crm_items.MirrorDialect) -> None:
    parsed = [crm_items.parse_item(row, dialect, utm_max_chars=settings.utm_value_max_chars) for row in rows]
    items = [item for item in parsed if isinstance(item, crm_items.ItemRow)]
    assert len(items) == len(rows)
    await upsert_items(fence, items, read_at=dt.datetime.now(dt.UTC))


async def tombstone(portal_id: int, item_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                UPDATE crm_items SET deleted_at = now(), delete_reason = 'not_found',
                    category_id = NULL, stage_id = NULL, stage_semantic = NULL, assigned_by_id = NULL,
                    created_time = NULL, updated_time = NULL, moved_time = NULL, closed = NULL,
                    opportunity = NULL, currency_id = NULL, lead_id = NULL, contact_ids = NULL,
                    company_id = NULL, utm_source = NULL, utm_medium = NULL, utm_campaign = NULL,
                    utm_content = NULL, utm_term = NULL
                WHERE id = :id
                """
            ),
            {"id": item_id},
        )


def without_source(body: Mapping[str, Any]) -> dict[str, Any]:
    """A report minus what says where it came from; everything else must be identical."""
    out = {key: value for key, value in body.items() if key not in ("data_as_of", "coverage", "stale")}
    scan = {key: value for key, value in out["scan"].items() if key not in _SOURCE_KEYS}
    for entity in ("leads", "deals"):
        if isinstance(scan.get(entity), Mapping):
            scan[entity] = {key: value for key, value in scan[entity].items() if key != "dialect"}
    out["scan"] = scan
    return out


def deal(
    item_id: int, *, category: int, stage: str, user: int, semantic: str = "P", **times: Any
) -> dict[str, Any]:
    fields = {"createdTime": IN, "updatedTime": IN, "movedTime": IN, "closed": "N", **times}
    return deal_row(
        item_id,
        categoryId=category,
        stageId=stage,
        assignedById=user,
        stageSemanticId=semantic,
        **fields,
    )


def lead(item_id: int, *, source: str = "google", medium: str = "cpc", campaign: str = "",
         semantic: str = "P", created: str = IN) -> dict[str, Any]:
    return {
        "id": item_id,
        "stageId": "NEW",
        "stageSemanticId": semantic,
        "assignedById": VIEWER,
        "createdTime": created,
        "updatedTime": created,
        "movedTime": created,
        "utmSource": source,
        "utmMedium": medium,
        "utmCampaign": campaign,
        "utmContent": "",
        "utmTerm": "",
    }


# --- /deals ------------------------------------------------------------------------------------


def deal_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(rows the live June selection returns, rows that exist and it must not)."""
    selected = [
        deal(1, category=GROW, stage="C7:NEW", user=VIEWER),
        # The explicit-P quirk: the deal says in progress, its stage says won.
        deal(2, category=GROW, stage="C7:WON", user=VIEWER),
        # A stage the dictionary has never heard of.
        deal(5, category=DEFAULT_FUNNEL, stage="MYSTERY", user=OTHER),
        # A stage the dictionary lane stopped seeing, whose stored outcome must not be used.
        deal(6, category=GROW, stage="C7:OLD", user=VIEWER),
        # Created in June and closed in July: still June's, and nobody's.
        deal(10, category=DEFAULT_FUNNEL, stage="WON", user=0, semantic="S", updatedTime=AFTER,
             movedTime=AFTER, closed="Y"),
        # Created in June, lost and edited in July: still June's.
        deal(11, category=GROW, stage="C7:LOSE", user=OTHER, semantic="F", updatedTime=AFTER,
             movedTime=AFTER, closed="Y"),
    ]
    unselected = [
        deal(7, category=GROW, stage="C7:NEW", user=VIEWER, createdTime=BEFORE, updatedTime=BEFORE,
             movedTime=BEFORE),
        # Only modified in June: the period is creation time alone.
        deal(3, category=GROW, stage="C7:LOSE", user=OTHER, semantic="F", createdTime=BEFORE,
             movedTime=BEFORE),
        # Only closed in June.
        deal(4, category=DEFAULT_FUNNEL, stage="WON", user=0, semantic="S", createdTime=BEFORE,
             updatedTime=BEFORE, closed="Y"),
        # Moved in June but neither created nor closed in it.
        deal(8, category=GROW, stage="C7:LOSE", user=VIEWER, semantic="F", createdTime=BEFORE,
             updatedTime=BEFORE),
    ]
    return selected, unselected


async def live_deal_report(portal: SeededPortal, rows: list[dict[str, Any]], filters: Any) -> dict[str, Any]:
    dictionary = live_dictionary()
    groups: dict[int | None, Any] = {}
    seen: set[int] = set()
    deal_stats._fold(
        rows,
        dialect=ITEM_DIALECT,
        known=deal_stats._stage_index(dictionary),
        groups=groups,
        seen=seen,
    )
    operators = {uid for group in groups.values() for uid in group.rows if uid is not None}
    return deal_stats._build_response(
        filters=filters,
        dictionary=dictionary,
        list_dialect=ITEM_DIALECT.name,
        groups=groups,
        labels=await deal_stats._employee_labels(portal.portal_id, operators),
        folded=len(seen),
        deals_total=len(seen),
        rest_requests=0,
        from_cache=False,
        assigned_to=filters.employees,
    )


async def test_the_deal_report_from_the_mirror_is_the_live_report(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = mirrored
    now = dt.datetime.now(dt.UTC)
    selected, unselected = deal_rows()
    await seed_dictionary(portal.portal_id)
    doomed = deal(9, category=GROW, stage="C7:NEW", user=VIEWER)
    await store(fence, [*selected, *unselected, doomed], crm_items.DEAL_ITEM)
    await tombstone(portal.portal_id, 9)
    await loaded(portal.portal_id, now=now)
    principal = viewer(portal)
    filters = parse_filters(JUNE, principal)

    live = await live_deal_report(portal, selected, filters)
    mirror = await deal_stats.load_deal_report_mirror(principal, await portal_row(portal.portal_id), filters)

    assert without_source(mirror) == without_source(live)
    # Equal because both are right, not because both are empty.
    assert mirror["totals"]["total"] == len(selected)
    grow = next(group for group in mirror["groups"] if group["category_id"] == GROW)
    assert grow["subtotal"]["won"] == 1, "the quirk: a P deal on a won stage counts as won"
    assert "7:C7:OLD" in grow["stage_keys"]
    assert mirror["scan"]["list_dialect"] == "mirror" and mirror["scan"]["rest_requests"] == 0
    assert mirror["coverage"] == {"window_from": None, "history_complete": True, "progress_pct": 100}
    assert mirror["stale"] is None


async def test_an_employee_filter_narrows_the_mirror_as_it_narrows_the_scan(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = mirrored
    selected, unselected = deal_rows()
    await seed_dictionary(portal.portal_id)
    await store(fence, [*selected, *unselected], crm_items.DEAL_ITEM)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC))
    principal = viewer(portal)
    filters = parse_filters(
        QueryParams([*JUNE.multi_items(), ("employee", str(OTHER))]),
        principal,
    )

    live = await live_deal_report(portal, [row for row in selected if row["assignedById"] == OTHER], filters)
    mirror = await deal_stats.load_deal_report_mirror(principal, await portal_row(portal.portal_id), filters)

    assert without_source(mirror) == without_source(live)
    assert mirror["totals"]["total"] == 2


# --- /utm --------------------------------------------------------------------------------------


def utm_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(leads in June, deals in June, records outside it)."""
    leads = [
        lead(11),
        lead(12, semantic="S"),
        lead(13, source="", medium="", semantic="F"),
        # Longer than the cut, and created on the last local day of the period.
        lead(14, source="facebook", campaign="x" * 150, created="2026-06-30T23:30:00+05:00"),
    ]
    deals = [
        deal_row(21, utmSource="google", utmMedium="cpc", stageSemanticId="S", createdTime=IN,
                 opportunity="1500.555", currencyId="KZT", leadId=12),
        # No amount at all; created on 10 June UTC, which is 11 June in Tashkent.
        deal_row(22, utmSource="google", utmMedium="cpc", stageSemanticId="P",
                 createdTime="2026-06-10T20:30:00+00:00", opportunity=None, currencyId=""),
        deal_row(23, stageSemanticId="F", createdTime=IN, opportunity=2000, currencyId="KZT"),
    ]
    outside = [lead(15, created=BEFORE), deal_row(24, createdTime=BEFORE)]
    return leads, deals, outside


def live_utm_report(leads: list[dict[str, Any]], deals: list[dict[str, Any]], filters: Any,
                    *, with_leads: bool = True) -> dict[str, Any]:
    zone = ZoneInfo(TZ)
    scan = utm_stats._Scan()
    seen: dict[str, set[int]] = {KIND_LEAD: set(), KIND_DEAL: set()}
    if with_leads:
        utm_stats._fold(leads, dialect=UTM_LEAD, scan=scan, seen=seen, zone=zone)
    utm_stats._fold(deals, dialect=UTM_DEAL, scan=scan, seen=seen, zone=zone)
    scan.totals = dict(scan.folded)
    capability = utm_stats._Capability(
        lead=UTM_LEAD if with_leads else None,
        deal=UTM_DEAL,
        lead_reason="" if with_leads else "ACCESS_DENIED",
        deal_reason="",
        dimensions=DIMENSIONS,
    )
    return utm_stats._build_response(
        filters=filters,
        dimensions=DIMENSIONS,
        capability=capability,
        scan=scan,
        rest_requests=0,
        from_cache=False,
        assigned_to=filters.employees,
    )


async def test_the_utm_report_from_the_mirror_is_the_live_report(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = mirrored
    leads, deals, outside = utm_rows()
    await store(fence, [*leads, outside[0]], crm_items.LEAD_ITEM)
    await store(fence, [*deals, outside[1], deal_row(25, createdTime=IN)], crm_items.DEAL_ITEM)
    await tombstone(portal.portal_id, 25)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC))
    principal = viewer(portal)
    filters = parse_filters(JUNE, principal)

    live = live_utm_report(leads, deals, filters)
    mirror = await utm_stats.load_utm_report_mirror(
        principal, await portal_row(portal.portal_id), filters, DIMENSIONS
    )

    assert without_source(mirror) == without_source(live)
    assert mirror["totals"]["leads"]["total"] == 4
    assert mirror["totals"]["deals"]["total"] == 3
    assert mirror["totals"]["amount"] == "3500.56"
    assert mirror["amounts"]["rows_without_amount"] == 1
    assert mirror["scan"]["deals"]["dialect"] == "mirror"


async def test_a_portal_whose_leads_are_refused_degrades_to_deals_only(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, fence = mirrored
    leads, deals, _ = utm_rows()
    await store(fence, leads, crm_items.LEAD_ITEM)
    await store(fence, deals, crm_items.DEAL_ITEM)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC), leads=False)
    principal = viewer(portal)
    filters = parse_filters(JUNE, principal)

    live = live_utm_report(leads, deals, filters, with_leads=False)
    mirror = await utm_stats.load_utm_report_mirror(
        principal, await portal_row(portal.portal_id), filters, DIMENSIONS
    )

    assert without_source(mirror) == without_source(live)
    assert mirror["scan"]["leads"] == {"available": False, "reason": "ACCESS_DENIED", "folded": 0, "total": 0}


# --- coverage, scope, promotion --------------------------------------------------------------


async def test_coverage_states_progress_and_a_late_sweep(mirrored: tuple[SeededPortal, Fence]) -> None:
    portal, _ = mirrored
    now = dt.datetime.now(dt.UTC)
    late = now - dt.timedelta(hours=2)
    await set_lane(portal.portal_id, crm_lanes.DEAL_BACKFILL, done=300, total=1000, clean=now)
    await set_lane(portal.portal_id, crm_lanes.DEAL_SWEEP, clean=late)
    await set_lane(portal.portal_id, crm_lanes.LEAD_BACKFILL, status=crm_lanes.DONE, done=100, total=100)
    await set_lane(portal.portal_id, crm_lanes.LEAD_SWEEP, clean=now)

    coverage = await crm_repo.coverage(portal.portal_id)
    wire = coverage.wire(now=now)

    assert not coverage.history_complete
    assert coverage.progress_pct == 36, "400 of 1100 records, rounded down"
    assert wire["data_as_of"] == late.isoformat(), "the older sweep bounds what the report knows"
    assert wire["stale"] == {"since": late.isoformat(), "reason": "sync_delayed"}


async def test_a_sweep_that_never_finished_is_reported_as_never_synced(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, _ = mirrored
    await set_lane(portal.portal_id, crm_lanes.DEAL_BACKFILL, status=crm_lanes.DONE, done=1, total=1)
    await set_lane(portal.portal_id, crm_lanes.DEAL_SWEEP)
    await set_lane(portal.portal_id, crm_lanes.LEAD_BACKFILL, block_reason="METHOD_NOT_FOUND")

    coverage = await crm_repo.coverage(portal.portal_id)

    assert coverage.history_complete, "leads the portal refuses are not history still to load"
    assert not coverage.leads_available and coverage.lead_reason == "METHOD_NOT_FOUND"
    assert coverage.wire(now=dt.datetime.now(dt.UTC))["stale"] == {"since": None, "reason": "never_synced"}


async def test_only_an_administrator_is_served_from_the_mirror(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, _ = mirrored
    row = await portal_row(portal.portal_id)

    assert crm_repo.serves_mirror(row, viewer(portal))
    assert not crm_repo.serves_mirror(row, viewer(portal, access="own"))
    with pytest.raises(PrincipalError) as refused:
        crm_repo.crm_scope(viewer(portal, access="own"))
    assert refused.value.code == crm_repo.MIRROR_UNAVAILABLE


async def test_the_mode_tool_refuses_mirror_until_the_deal_history_is_loaded(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    portal, _ = mirrored
    assert await crm_mode._set(portal.portal_id, "mirror") == 1, "no lane at all"

    await set_lane(portal.portal_id, crm_lanes.DEAL_BACKFILL, done=10, total=100)
    assert await crm_mode._set(portal.portal_id, "mirror") == 1
    assert await crm_mode._set(portal.portal_id, "mirror", force=True) == 0

    await set_lane(portal.portal_id, crm_lanes.DEAL_BACKFILL, status=crm_lanes.DONE, done=100, total=100)
    assert await crm_mode._set(portal.portal_id, "mirror") == 0


# --- shadow ------------------------------------------------------------------------------------


async def test_shadow_counts_the_rows_that_differ_and_nothing_else() -> None:
    def body(won: int) -> dict[str, Any]:
        measures = {"total": 2, "in_progress": 2 - won, "won": won, "lost": 0, "unknown_stage": 0,
                    "cells": {"7:C7:NEW": 2 - won, "7:C7:WON": won}}
        return {
            "groups": [
                {"category_id": GROW, "subtotal": measures, "rows": [{"user_id": VIEWER, **measures}]}
            ],
            "totals": {"total": 2},
        }

    assert crm_shadow.deal_differences(body(1), body(1))["equal"]
    differences = crm_shadow.deal_differences(body(1), body(2))
    assert not differences["equal"]
    assert differences["rows_compared"] == 2 and differences["rows_differing"] == 2


async def test_only_an_administrators_report_on_a_shadow_portal_is_compared() -> None:
    admin = Principal(
        portal_id=1, member_id="m", user_id=VIEWER, is_admin=True, access="all", timezone=TZ,
        lang="ru", placement="DEFAULT", entity=None, issued_at=0,
    )
    own = Principal(
        portal_id=1, member_id="m", user_id=VIEWER, is_admin=False, access="own", timezone=TZ,
        lang="ru", placement="DEFAULT", entity=None, issued_at=0,
    )

    assert crm_shadow.shadows(Portal(crm_mode="shadow", crm_opt_out_at=None), admin)
    assert not crm_shadow.shadows(Portal(crm_mode="shadow", crm_opt_out_at=None), own)
    assert not crm_shadow.shadows(Portal(crm_mode="sync", crm_opt_out_at=None), admin)


# --- Bitrix24 Simple CRM ------------------------------------------------------------------------


async def set_crm_mode(portal_id: int, mode: int) -> None:
    """What the dictionary lane stores from `crm.settings.mode.get` (1 Classic, 2 Simple)."""
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portals SET capabilities = capabilities || "
                "jsonb_build_object('crm_bitrix_mode', CAST(:mode AS int)) WHERE id = :pid"
            ),
            {"pid": portal_id, "mode": mode},
        )


async def test_a_simple_crm_portal_gets_a_deals_only_sources_report(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    """Every lead there became a deal carrying its tags, so leads beside deals count twice."""
    portal, fence = mirrored
    leads, deals, _ = utm_rows()
    await store(fence, leads, crm_items.LEAD_ITEM)
    await store(fence, deals, crm_items.DEAL_ITEM)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC))
    await set_crm_mode(portal.portal_id, 2)
    principal = viewer(portal)
    filters = parse_filters(JUNE, principal)

    mirror = await utm_stats.load_utm_report_mirror(
        principal, await portal_row(portal.portal_id), filters, DIMENSIONS
    )

    assert mirror["scan"]["leads"]["available"] is False
    assert mirror["scan"]["leads"]["reason"] == utm_stats.SIMPLE_CRM_REASON
    assert mirror["totals"]["leads"]["total"] == 0
    assert mirror["totals"]["deals"]["total"] == len(deals)
    live = live_utm_report(leads, deals, filters, with_leads=False)
    live["scan"]["leads"]["reason"] = utm_stats.SIMPLE_CRM_REASON
    assert without_source(mirror) == without_source(live)


async def test_a_classic_crm_portal_keeps_its_leads(mirrored: tuple[SeededPortal, Fence]) -> None:
    portal, fence = mirrored
    leads, deals, _ = utm_rows()
    await store(fence, leads, crm_items.LEAD_ITEM)
    await store(fence, deals, crm_items.DEAL_ITEM)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC))
    await set_crm_mode(portal.portal_id, 1)
    principal = viewer(portal)

    mirror = await utm_stats.load_utm_report_mirror(
        principal, await portal_row(portal.portal_id), parse_filters(JUNE, principal), DIMENSIONS
    )

    assert mirror["scan"]["leads"]["available"] is True
    assert mirror["totals"]["leads"]["total"] == len(leads)


async def test_an_operator_bitrix24_no_longer_returns_is_not_drawn_as_a_current_employee(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    """Portal 1 had such a row (an import's placeholder user): full opacity, no name."""
    portal, fence = mirrored
    selected, _ = deal_rows()
    await seed_dictionary(portal.portal_id)
    await store(fence, selected, crm_items.DEAL_ITEM)
    await loaded(portal.portal_id, now=dt.datetime.now(dt.UTC))
    # The upsert already left placeholder rows for every assignee; `user.get` then answered
    # for VIEWER and not for OTHER.
    async with tenant_txn(portal.portal_id) as session:
        await session.execute(
            text(
                "UPDATE employees SET active = true, found = (bx_user_id = :viewer) "
                "WHERE portal_id = :pid AND bx_user_id IN (:other, :viewer)"
            ),
            {"pid": portal.portal_id, "other": OTHER, "viewer": VIEWER},
        )
    principal = viewer(portal)

    body = await deal_stats.load_deal_report_mirror(
        principal, await portal_row(portal.portal_id), parse_filters(JUNE, principal)
    )

    rows = {row["user_id"]: row for group in body["groups"] for row in group["rows"]}
    assert rows[OTHER]["active"] is False
    assert rows[VIEWER]["active"] is True


async def test_a_simple_crm_sources_report_does_not_wait_for_the_lead_history(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    """It reads no leads, so an unfinished lead backfill must not say "history is loading"."""
    portal, fence = mirrored
    _, deals, _ = utm_rows()
    await store(fence, deals, crm_items.DEAL_ITEM)
    now = dt.datetime.now(dt.UTC)
    await set_lane(
        portal.portal_id, crm_lanes.DEAL_BACKFILL, status=crm_lanes.DONE, done=9, total=9, clean=now
    )
    await set_lane(portal.portal_id, crm_lanes.DEAL_SWEEP, clean=now)
    await set_lane(portal.portal_id, crm_lanes.LEAD_BACKFILL, done=10, total=1000, clean=now)
    await set_lane(portal.portal_id, crm_lanes.LEAD_SWEEP)
    await set_crm_mode(portal.portal_id, 2)
    principal = viewer(portal)

    body = await utm_stats.load_utm_report_mirror(
        principal, await portal_row(portal.portal_id), parse_filters(JUNE, principal), DIMENSIONS
    )

    assert body["coverage"] == {"window_from": None, "history_complete": True, "progress_pct": 100}
    assert body["stale"] is None


async def test_the_deals_report_does_not_wait_for_the_lead_history_either(
    mirrored: tuple[SeededPortal, Fence],
) -> None:
    """The Deals page reads no leads on any portal, Classic included."""
    portal, fence = mirrored
    selected, _ = deal_rows()
    await seed_dictionary(portal.portal_id)
    await store(fence, selected, crm_items.DEAL_ITEM)
    now = dt.datetime.now(dt.UTC)
    await set_lane(
        portal.portal_id, crm_lanes.DEAL_BACKFILL, status=crm_lanes.DONE, done=9, total=9, clean=now
    )
    await set_lane(portal.portal_id, crm_lanes.DEAL_SWEEP, clean=now)
    await set_lane(portal.portal_id, crm_lanes.LEAD_BACKFILL, done=10, total=1000, clean=now)
    await set_lane(portal.portal_id, crm_lanes.LEAD_SWEEP)
    principal = viewer(portal)

    body = await deal_stats.load_deal_report_mirror(
        principal, await portal_row(portal.portal_id), parse_filters(JUNE, principal)
    )

    assert body["coverage"] == {"window_from": None, "history_complete": True, "progress_pct": 100}
    assert body["stale"] is None
