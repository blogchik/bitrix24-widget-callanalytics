"""Shadow mode: every successful live report recomputed from the mirror (§4.14 constraint 8).

A portal in `crm_mode = shadow` still serves its Deals and Sources pages live. After each live
report for an administrator has been sent, the same report is built from Postgres in the
background and the two are compared. This is how a portal earns `mirror`: not by a test
fixture, but by its own traffic agreeing with Bitrix24.

**What is kept, and what is not.** The plan keeps the differing ids for 30 days in a table
that has not shipped (`crm_shadow_diffs`). Until it does, a comparison leaves one log line of
COUNTS - rows compared, rows that differ, the two totals - and never an id, a name or a tag
value: a log is not a tenant table, and decision 29's rule that per-record CRM data lives only
under row-level security holds for logs too.

**Why some differences are expected.** The live read is the portal now; the mirror is the
portal as of its last sweep, up to `CRM_SWEEP_INTERVAL_SEC` behind. A deal edited in the
last few minutes differs and is not a defect. The line carries `data_as_of` so a reader can
tell the two apart; the classifier that does it automatically comes with the table.

Fail-open by construction: the comparison runs after the response, and anything it raises is
logged and swallowed. A shadow defect must never cost a viewer their report.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from app.db.models import Portal
from app.logging import get_logger
from app.security.principal import Principal
from app.services import deal_stats, utm_stats
from app.services.stats import CallFilters

__all__ = ["compare_deals", "compare_utm", "deal_differences", "shadows", "utm_differences"]

_log = get_logger(__name__)

_SHADOW_MODE: Final[str] = "shadow"
_ALL: Final[str] = "all"


def shadows(portal: Portal, principal: Principal) -> bool:
    """True when a live report for this viewer should be compared with the mirror.

    Administrators only, for the reason `crm_repo.crm_scope` refuses everyone else: an `own`
    viewer's mirror report cannot be built yet, so there is nothing to compare it with.
    """
    return (
        portal.crm_mode == _SHADOW_MODE
        and portal.crm_opt_out_at is None
        and principal.access == _ALL
    )


def _measures(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("total"),
        row.get("in_progress"),
        row.get("won"),
        row.get("lost"),
        row.get("unknown_stage"),
        tuple(sorted((row.get("cells") or {}).items())),
    )


def _deal_rows(body: Mapping[str, Any]) -> dict[tuple[Any, Any], tuple[Any, ...]]:
    rows: dict[tuple[Any, Any], tuple[Any, ...]] = {}
    for group in body.get("groups") or ():
        category_id = group.get("category_id")
        rows[(category_id, "subtotal")] = _measures(group.get("subtotal") or {})
        for row in group.get("rows") or ():
            rows[(category_id, row.get("user_id"))] = _measures(row)
    return rows


def deal_differences(live: Mapping[str, Any], mirror: Mapping[str, Any]) -> dict[str, Any]:
    """Two `/deals` bodies as counts of agreement."""
    left, right = _deal_rows(live), _deal_rows(mirror)
    keys = left.keys() | right.keys()
    differing = sum(1 for key in keys if left.get(key) != right.get(key))
    live_total = (live.get("totals") or {}).get("total")
    mirror_total = (mirror.get("totals") or {}).get("total")
    return {
        "equal": differing == 0 and live_total == mirror_total,
        "rows_compared": len(keys),
        "rows_differing": differing,
        "live_deals": live_total,
        "mirror_deals": mirror_total,
        "live_funnels": len(live.get("groups") or ()),
        "mirror_funnels": len(mirror.get("groups") or ()),
    }


def _utm_rows(body: Mapping[str, Any]) -> dict[tuple[str, ...], tuple[Any, ...]]:
    return {
        tuple(row.get("k") or ()): (
            row.get("leads"),
            row.get("deals"),
            row.get("amount"),
            row.get("deals_from_lead"),
        )
        for row in body.get("combinations") or ()
    }


def utm_differences(live: Mapping[str, Any], mirror: Mapping[str, Any]) -> dict[str, Any]:
    """Two `/utm` bodies as counts of agreement."""
    left, right = _utm_rows(live), _utm_rows(mirror)
    keys = left.keys() | right.keys()
    differing = sum(1 for key in keys if left.get(key) != right.get(key))
    live_days = {day.get("date"): day for day in live.get("days") or ()}
    mirror_days = {day.get("date"): day for day in mirror.get("days") or ()}
    days_differing = sum(
        1 for day in live_days.keys() | mirror_days.keys() if live_days.get(day) != mirror_days.get(day)
    )
    live_totals = live.get("totals") or {}
    mirror_totals = mirror.get("totals") or {}
    return {
        "equal": differing == 0 and days_differing == 0 and live_totals == mirror_totals,
        "combinations_compared": len(keys),
        "combinations_differing": differing,
        "days_differing": days_differing,
        "live_leads": (live_totals.get("leads") or {}).get("total"),
        "mirror_leads": (mirror_totals.get("leads") or {}).get("total"),
        "live_deals": (live_totals.get("deals") or {}).get("total"),
        "mirror_deals": (mirror_totals.get("deals") or {}).get("total"),
        "amount_equal": live_totals.get("amount") == mirror_totals.get("amount"),
    }


async def compare_deals(
    principal: Principal, portal: Portal, filters: CallFilters, live: Mapping[str, Any]
) -> None:
    """Background task after a live `POST /deals`."""
    try:
        mirror = await deal_stats.load_deal_report_mirror(principal, portal, filters)
        summary = deal_differences(live, mirror)
    except Exception as exc:  # fail-open: see the module docstring
        _log.warning(
            "crm_shadow: deals comparison failed",
            extra={"portal_id": portal.id, "error": type(exc).__name__},
        )
        return
    _log.info(
        "crm_shadow: deals compared",
        extra={
            "portal_id": portal.id,
            "days": filters.days,
            "data_as_of": mirror.get("data_as_of"),
            **summary,
        },
    )


async def compare_utm(
    principal: Principal,
    portal: Portal,
    filters: CallFilters,
    dimensions: tuple[str, ...],
    live: Mapping[str, Any],
) -> None:
    """Background task after a live `POST /utm`."""
    try:
        mirror = await utm_stats.load_utm_report_mirror(principal, portal, filters, dimensions)
        summary = utm_differences(live, mirror)
    except Exception as exc:  # fail-open: see the module docstring
        _log.warning(
            "crm_shadow: utm comparison failed",
            extra={"portal_id": portal.id, "error": type(exc).__name__},
        )
        return
    _log.info(
        "crm_shadow: utm compared",
        extra={
            "portal_id": portal.id,
            "days": filters.days,
            "data_as_of": mirror.get("data_as_of"),
            **summary,
        },
    )
