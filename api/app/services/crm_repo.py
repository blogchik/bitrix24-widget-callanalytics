"""Every read of the CRM mirror starts here (§4.14, decision 28).

`calls_repo.py` is this module's model, for the same reason: a report that builds its own
`select(CrmItem)` is a report that can forget the tenant, the tombstone or the viewer. Three
things live here and nowhere else.

* **The scope.** `crm_scope` is the only producer of a CRM visibility predicate, and it
  knows two answers. An administrator sees the whole mirror (decision 28). Anyone else sees
  it only if an administrator granted them a scope (`services/crm_grants.py`), and then they
  see exactly what that grant says. A viewer with neither is refused with
  `crm_mirror_unavailable` and the SPA keeps reading Bitrix24 live on their own token, until
  the per-viewer census stores what Bitrix24 itself proves each of them may see.
* **The tombstone.** Every read carries `deleted_at IS NULL`, which is also the predicate of
  every partial index `0004` built, so the planner can use them.
* **Coverage.** What the lanes know about how much of the portal is loaded, as the sentence a
  report prints above itself.

The aggregations return counts, never rows: `deal_stats` and `utm_stats` fold them into the
accumulators the live read folds pages into, and one `_build_response` writes both wires
(§4.14 constraint 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Date, and_, case, cast, func, literal, select
from sqlalchemy.sql.elements import ColumnElement

from app.bitrix.deals import Funnel, Stage, normalise_semantic, stage_key
from app.bitrix.utm import DIMENSIONS
from app.config import settings
from app.db.models import CrmFunnel, CrmItem, CrmLane, CrmStage, Portal
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger
from app.security.principal import Principal, PrincipalError
from app.services.crm_grants import ViewerGrant, grant_scope
from app.services.crm_scope import ViewerScope, load_scope, scope_predicate
from app.services.stats import CallFilters
from app.sync import crm_dict, crm_lanes

__all__ = [
    "DEAL",
    "LEAD",
    "MIRROR_UNAVAILABLE",
    "Coverage",
    "StageCount",
    "UtmCount",
    "ViewerAccess",
    "ViewerGrant",
    "ViewerScope",
    "coverage",
    "crm_scope",
    "deal_dictionary",
    "deal_stage_counts",
    "report_gate",
    "reset_report_gate",
    "serves_mirror",
    "utm_counts",
    "utm_days",
    "viewer_access",
]

_log = get_logger(__name__)

LEAD: Final[int] = 1
DEAL: Final[int] = 2

#: The one refusal a mirror route gives a viewer it cannot serve yet. The SPA reads it as
#: "ask `/me` again": the portal's mode or this viewer's path changed under an open page.
MIRROR_UNAVAILABLE: Final[str] = "crm_mirror_unavailable"

_ALL: Final[str] = "all"
_OWN: Final[str] = "own"
_DENIED: Final[str] = "denied"
_MIRROR_MODE: Final[str] = "mirror"

_WON_OR_LOST: Final[tuple[str, str]] = ("S", "F")
_IN_PROGRESS: Final[str] = "P"

_global_gate: asyncio.Semaphore | None = None


# --- who may read what ---------------------------------------------------------------------


@dataclass(frozen=True)
class ViewerAccess:
    """Everything outside the JWT that widens one viewer, resolved once per request.

    At most one of the two is ever set, and that is the rule "a grant replaces the derived
    scope" expressed where it cannot be forgotten rather than in two call sites.
    """

    grant: ViewerGrant | None = None
    scope: ViewerScope | None = None

    @property
    def widens_crm(self) -> bool:
        """Whether the CRM reports are opened past this viewer's own records.

        A grant only counts for the area it names: one that opens the calls pages and not
        the CRM ones leaves the CRM gate exactly where it was.
        """
        if self.grant is not None:
            return self.grant.covers_crm
        return self.scope is not None

    @property
    def widens_calls(self) -> bool:
        """Whether the Summary, By hour and call list pages are opened.

        Only a grant can do this. A census measures CRM cells and says nothing about
        telephony, which Bitrix24 scopes with its own separate verdict.
        """
        return self.grant is not None and self.grant.covers_calls


async def viewer_access(principal: Principal) -> ViewerAccess:
    """The one read both gates share: what an administrator said, else what Bitrix24 proved.

    An administrator costs no read at all - they already see the whole mirror, and neither
    a grant nor a census could say anything but something narrower, which `crm_scope` would
    refuse to apply anyway.

    A grant short-circuits the census on purpose. Merging the two would make "why can this
    person see that?" unanswerable; taking the grant alone leaves exactly one answer, and
    `portal_events` holds who gave it.
    """
    if principal.access == _ALL:
        return ViewerAccess()
    # `get_principal` already read it, next to the `portals` row: one read per request,
    # shared by the CRM gates here and by `calls_repo.scope_filter`.
    if principal.grant is not None:
        return ViewerAccess(grant=principal.grant)
    if not settings.crm_scope_enabled:
        return ViewerAccess()
    return ViewerAccess(scope=await load_scope(principal.portal_id, principal.user_id))


def mirror_readable(portal: Portal) -> bool:
    """The CRM mirror holds live data for this portal: syncing, not opted out, not being purged.

    The reports have `serves_mirror` for a whole report; this is the weaker test for reading a
    few mirrored links on the side (a tab's leads, a call's converted deal).
    """
    return portal.crm_mode != "off" and portal.crm_opt_out_at is None and not portal.crm_purge_pending


def simple_crm(portal: Portal) -> bool:
    """True when the portal runs Bitrix24 Simple CRM: no leads, every new lead a deal at once.

    Read from `portals.capabilities`, which the dictionary lane refreshes from
    `crm.settings.mode.get` every pass, because portals do switch between the two modes.
    Unknown - not read yet, or unanswered - is Classic, which is what the reports did before.
    """
    mode = (portal.capabilities or {}).get(crm_dict.CRM_MODE_CAPABILITY)
    return mode == crm_dict.CRM_MODE_SIMPLE


#: Leads one contact or company tab matches on. A card with more is not what the tab is for,
#: and the keys become an `IN` list: the same bound `crm_activity_cap` puts on activity ids.
_LINKED_LEAD_CAP: Final[int] = 250


async def linked_lead_ids(portal_id: int, entity_type: str, entity_id: int) -> list[int]:
    """The leads the CRM mirror links to a contact or company, newest first.

    A call made while the client was still a lead keeps `CRM_ENTITY_TYPE = LEAD` forever, so
    a contact or company tab only reaches it through the lead. Read from the mirror, not on
    the opener's token: the tab's context row is shared between viewers, and keys that
    depended on who opened the card would make the tab's numbers depend on it too. Empty
    when the portal does not mirror CRM - the tab then matches as it always did.
    """
    if entity_type == "CONTACT":
        link = CrmItem.contact_ids.contains([entity_id])
    elif entity_type == "COMPANY":
        link = CrmItem.company_id == entity_id
    else:
        return []
    async with tenant_txn(portal_id) as session:
        rows = (
            await session.execute(
                select(CrmItem.id)
                .where(
                    CrmItem.portal_id == portal_id,
                    CrmItem.entity_type_id == LEAD,
                    CrmItem.deleted_at.is_(None),
                    link,
                )
                .order_by(CrmItem.id.desc())
                .limit(_LINKED_LEAD_CAP)
            )
        ).scalars()
        return [int(value) for value in rows]


def serves_mirror(
    portal: Portal, principal: Principal, access: ViewerAccess | None = None
) -> bool:
    """True when this viewer's Deals and Sources pages read Postgres rather than Bitrix24.

    Three conditions, all of them required. The portal was promoted (`crm_mode = mirror`,
    which `tools/crm_mode.py` refuses before the deal history is loaded); its administrator
    has not turned CRM analytics off; and the viewer is one `crm_scope` can answer for.

    A grant or a census satisfies the third condition and ONLY the third: neither can open
    a portal that was never promoted, and neither can reopen one whose administrator turned
    CRM analytics off. Nor do they reach a `denied` viewer - `denied` means the statistics
    probe said this person may see no telephony at all (§4.7), which is a decision about
    the whole app and not about how much CRM they get.
    """
    if portal.crm_opt_out_at is not None or portal.crm_mode != _MIRROR_MODE:
        return False
    if principal.access == _ALL:
        return True
    return principal.access == _OWN and access is not None and access.widens_crm


def crm_scope(
    principal: Principal, access: ViewerAccess | None = None
) -> ColumnElement[bool] | None:
    """The CRM row predicate for this viewer, or None when the whole mirror is visible.

    * `all` (administrators) -> None, decision 28: an administrator sees every record.
    * `denied` -> 403, the backstop `calls_repo.scope_filter` also is. A grant does not
      reach here: a viewer who may see no telephony sees no CRM page either (§4.7).
    * `own` WITH a grant -> what the grant says, and nothing merged into it. An
      administrator decided this, `portal_events` records who and when, and that is the
      whole answer to "why can this person see that?".
    * `own` with a census -> the cells Bitrix24 itself proved that viewer may read, asked
      with their own token (`services/crm_scope.py`).
    * `own` with neither -> 409 `crm_mirror_unavailable`, and the SPA reads Bitrix24 live on
      that viewer's own token. Refusing is what keeps a salesperson from being served the
      administrator's view by default.

    The order matters: `denied` is tested before the grant, so a grant left behind on a
    viewer whose rights were later removed cannot resurrect them.
    """
    if principal.access == _ALL:
        return None
    if principal.access == _DENIED:
        raise PrincipalError("no_stats_permission", 403)
    if access is not None and access.grant is not None and access.grant.covers_crm:
        return grant_scope(principal.portal_id, access.grant)
    if access is not None and access.scope is not None:
        return scope_predicate(access.scope)
    raise PrincipalError(MIRROR_UNAVAILABLE, 409)


def report_gate() -> asyncio.Semaphore:
    """Mirror reports running at once in this process, both pages together.

    Postgres, not Bitrix24, is what this protects: a 366-day aggregate over a large portal is
    a real query, and a dozen of them started by one impatient click-through would queue
    every dashboard behind them. Built lazily for the reason `deal_stats._gate` gives.
    """
    global _global_gate
    if _global_gate is None:
        _global_gate = asyncio.Semaphore(settings.crm_report_concurrency)
    return _global_gate


def reset_report_gate() -> None:
    """Drop the gate (tests running on a fresh loop)."""
    global _global_gate
    _global_gate = None


def _period(column: Any, filters: CallFilters) -> ColumnElement[bool]:
    """Half-open, as the live filters are: `>= start`, `< end`."""
    return and_(column >= filters.start_utc, column < filters.end_utc)


def _live(portal_id: int, entity_type_ids: Sequence[int]) -> list[ColumnElement[bool]]:
    return [
        CrmItem.portal_id == portal_id,
        CrmItem.entity_type_id.in_(tuple(entity_type_ids)),
        CrmItem.deleted_at.is_(None),
    ]


# --- /deals --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageCount:
    """Deals sharing a funnel, an operator, a stage and an outcome."""

    category_id: int | None
    assigned_by_id: int | None
    stage_id: str
    semantic: str
    deals: int
    #: The smallest deal id in the group. The live read discovers columns in id order, and
    #: folding the groups in this order discovers them in the same one.
    first_id: int


async def deal_dictionary(portal_id: int) -> tuple[list[Funnel], dict[int, tuple[Stage, ...]]]:
    """The portal's funnels and their stages, ordered as `parse_funnels`/`parse_stages` order.

    A funnel or stage the dictionary lane has stopped seeing (`missing_since`) is left out,
    as it is absent from a live `crm.category.list` answer.
    """
    async with tenant_txn(portal_id) as session:
        funnel_rows = (
            await session.execute(
                select(CrmFunnel.category_id, CrmFunnel.name, CrmFunnel.sort, CrmFunnel.is_default)
                .where(
                    CrmFunnel.portal_id == portal_id,
                    CrmFunnel.entity_type_id == DEAL,
                    CrmFunnel.missing_since.is_(None),
                )
            )
        ).all()
        stage_rows = (
            await session.execute(
                select(
                    CrmStage.category_id,
                    CrmStage.status_id,
                    CrmStage.name,
                    CrmStage.sort,
                    CrmStage.semantic,
                ).where(
                    CrmStage.portal_id == portal_id,
                    CrmStage.entity_type_id == DEAL,
                    CrmStage.missing_since.is_(None),
                )
            )
        ).all()

    funnels = sorted(
        (
            Funnel(
                id=int(row.category_id),
                name=row.name,
                sort=int(row.sort),
                is_default=bool(row.is_default),
            )
            for row in funnel_rows
        ),
        key=lambda funnel: (funnel.sort, funnel.id),
    )
    stages: dict[int, list[Stage]] = {funnel.id: [] for funnel in funnels}
    for row in stage_rows:
        bucket = stages.get(int(row.category_id))
        if bucket is None:
            continue
        bucket.append(
            Stage(
                key=stage_key(int(row.category_id), row.status_id),
                category_id=int(row.category_id),
                status_id=row.status_id,
                name=row.name,
                semantic=normalise_semantic(row.semantic),
                sort=int(row.sort),
                known=True,
            )
        )
    return funnels, {
        category_id: tuple(sorted(items, key=lambda stage: (stage.sort, stage.status_id)))
        for category_id, items in stages.items()
    }


async def deal_stage_counts(
    portal_id: int,
    filters: CallFilters,
    *,
    scope: ColumnElement[bool] | None,
    employees: Sequence[int],
) -> list[StageCount]:
    """Owner decision 3 in SQL: deals CREATED in the period, counted - and no others.

    A deal modified or closed in the period but created before it does not count, exactly as
    the live read's `period_filter` does not select it.

    The outcome repeats the live read's rule, including the quirk G0 Q3 keeps until cutover:
    a deal that says won or lost is believed; one that says anything else takes its stage's
    outcome from the dictionary, when the dictionary knows the stage.

    The bucket expressions are projected once in an inner query and grouped by the
    projection, for the reason `stats.py` gives: a bound parameter in the select list and
    the same one in `GROUP BY` are two different trees to Postgres.
    """
    semantic = case(
        (CrmItem.stage_semantic.in_(_WON_OR_LOST), CrmItem.stage_semantic),
        (CrmStage.semantic.in_(_WON_OR_LOST), CrmStage.semantic),
        else_=literal(_IN_PROGRESS),
    )
    terms = [*_live(portal_id, (DEAL,)), _period(CrmItem.created_time, filters)]
    if scope is not None:
        terms.append(scope)
    if employees:
        terms.append(CrmItem.assigned_by_id.in_(tuple(employees)))

    inner = (
        select(
            CrmItem.id.label("id"),
            CrmItem.category_id.label("category_id"),
            CrmItem.assigned_by_id.label("assigned_by_id"),
            func.coalesce(CrmItem.stage_id, "").label("stage_id"),
            semantic.label("semantic"),
        )
        .select_from(CrmItem)
        .outerjoin(
            CrmStage,
            and_(
                CrmStage.portal_id == CrmItem.portal_id,
                CrmStage.entity_type_id == DEAL,
                CrmStage.category_id == CrmItem.category_id,
                CrmStage.status_id == CrmItem.stage_id,
                CrmStage.missing_since.is_(None),
            ),
        )
        .where(*terms)
        .subquery()
    )
    first_id = func.min(inner.c.id)
    statement = (
        select(
            inner.c.category_id,
            inner.c.assigned_by_id,
            inner.c.stage_id,
            inner.c.semantic,
            func.count().label("deals"),
            first_id.label("first_id"),
        )
        .group_by(inner.c.category_id, inner.c.assigned_by_id, inner.c.stage_id, inner.c.semantic)
        .order_by(first_id)
    )
    async with tenant_txn(portal_id) as session:
        rows = (await session.execute(statement)).all()
    return [
        StageCount(
            category_id=row.category_id,
            assigned_by_id=row.assigned_by_id,
            stage_id=row.stage_id,
            semantic=row.semantic,
            deals=int(row.deals),
            first_id=int(row.first_id),
        )
        for row in rows
    ]


# --- /utm ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UtmCount:
    """Records of one entity sharing five tags and an outcome."""

    entity_type_id: int
    utm: tuple[str, ...]
    semantic: str
    records: int
    #: Deals carrying a lead link (`lead_id > 0`).
    from_lead: int
    #: Deals carrying an amount; the rest are `rows_without_amount`.
    amount_rows: int
    #: The sum of those amounts, each already rounded half-up to cents when it was stored.
    amount: Decimal
    currencies: tuple[str, ...]


def _utm_terms(
    portal_id: int,
    filters: CallFilters,
    *,
    entity_type_ids: Sequence[int],
    scope: ColumnElement[bool] | None,
    employees: Sequence[int],
) -> list[ColumnElement[bool]]:
    """Decision 3 of §4.13: creation time only, one flat leg."""
    terms = [*_live(portal_id, entity_type_ids), _period(CrmItem.created_time, filters)]
    if scope is not None:
        terms.append(scope)
    if employees:
        terms.append(CrmItem.assigned_by_id.in_(tuple(employees)))
    return terms


async def utm_counts(
    portal_id: int,
    filters: CallFilters,
    *,
    entity_type_ids: Sequence[int],
    scope: ColumnElement[bool] | None,
    employees: Sequence[int],
) -> list[UtmCount]:
    """The five-tag accumulator of `utm_stats._fold`, computed by Postgres."""
    terms = _utm_terms(
        portal_id, filters, entity_type_ids=entity_type_ids, scope=scope, employees=employees
    )
    semantic = case(
        (CrmItem.stage_semantic.in_(_WON_OR_LOST), CrmItem.stage_semantic),
        else_=literal(_IN_PROGRESS),
    )
    inner = (
        select(
            CrmItem.entity_type_id.label("entity_type_id"),
            *[func.coalesce(getattr(CrmItem, name), "").label(name) for name in DIMENSIONS],
            semantic.label("semantic"),
            CrmItem.lead_id.label("lead_id"),
            CrmItem.opportunity.label("opportunity"),
            CrmItem.currency_id.label("currency_id"),
        )
        .where(*terms)
        .subquery()
    )
    keys = [inner.c.entity_type_id, *[inner.c[name] for name in DIMENSIONS], inner.c.semantic]
    priced = inner.c.opportunity.is_not(None)
    statement = (
        select(
            *keys,
            func.count().label("records"),
            func.count().filter(inner.c.lead_id > 0).label("from_lead"),
            func.count(inner.c.opportunity).label("amount_rows"),
            func.coalesce(func.sum(inner.c.opportunity), 0).label("amount"),
            func.array_agg(inner.c.currency_id.distinct())
            .filter(and_(priced, inner.c.currency_id.is_not(None)))
            .label("currencies"),
        )
        .group_by(*keys)
        .order_by(*keys)
    )
    async with tenant_txn(portal_id) as session:
        rows = (await session.execute(statement)).all()
    return [
        UtmCount(
            entity_type_id=int(row.entity_type_id),
            utm=tuple(getattr(row, name) for name in DIMENSIONS),
            semantic=row.semantic,
            records=int(row.records),
            from_lead=int(row.from_lead),
            amount_rows=int(row.amount_rows),
            amount=Decimal(row.amount),
            currencies=tuple(row.currencies or ()),
        )
        for row in rows
    ]


async def utm_days(
    portal_id: int,
    filters: CallFilters,
    *,
    entity_type_ids: Sequence[int],
    scope: ColumnElement[bool] | None,
    employees: Sequence[int],
) -> dict[dt.date, dict[int, int]]:
    """Records per local day of creation, in the viewer's zone, per entity type."""
    terms = _utm_terms(
        portal_id, filters, entity_type_ids=entity_type_ids, scope=scope, employees=employees
    )
    inner = (
        select(
            CrmItem.entity_type_id.label("entity_type_id"),
            cast(func.timezone(filters.tz_name, CrmItem.created_time), Date).label("day"),
        )
        .where(*terms)
        .subquery()
    )
    statement = select(inner.c.day, inner.c.entity_type_id, func.count().label("records")).group_by(
        inner.c.day, inner.c.entity_type_id
    )
    async with tenant_txn(portal_id) as session:
        rows = (await session.execute(statement)).all()
    days: dict[dt.date, dict[int, int]] = {}
    for row in rows:
        days.setdefault(row.day, {})[int(row.entity_type_id)] = int(row.records)
    return days


# --- coverage --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    """How much of the portal the mirror holds, and how fresh it is (§4.14 constraint 4)."""

    deals_available: bool
    deal_reason: str
    leads_available: bool
    lead_reason: str
    history_complete: bool
    progress_pct: int
    #: The oldest of the sweeps' last completed passes: every edit made before this moment
    #: has reached the mirror. None until each sweep has finished once.
    data_as_of: dt.datetime | None

    def wire(self, *, now: dt.datetime) -> dict[str, Any]:
        """The three keys a mirror report carries beside the live read's body."""
        stale: dict[str, Any] | None = None
        if self.data_as_of is None:
            stale = {"since": None, "reason": "never_synced"}
        elif (now - self.data_as_of).total_seconds() > settings.crm_stale_after_sec:
            stale = {"since": self.data_as_of.isoformat(), "reason": "sync_delayed"}
        return {
            "data_as_of": self.data_as_of.isoformat() if self.data_as_of else None,
            "coverage": {
                # The window lane that would pin this to a date has not shipped; the
                # backfill reads newest ids first, so progress is what can be stated.
                "window_from": None,
                "history_complete": self.history_complete,
                "progress_pct": self.progress_pct,
            },
            "stale": stale,
        }


@dataclass(frozen=True)
class _LaneState:
    status: str
    block_reason: str | None
    progress_done: int
    progress_total: int | None
    last_clean_at: dt.datetime | None


async def coverage(portal_id: int, *, leads: bool = True) -> Coverage:
    """Read the lanes once and state what a report built from the mirror can promise.

    `leads=False` for a report that reads no leads - the Deals page, or the Sources page on a
    Simple-CRM portal: an unfinished or lagging lead lane says nothing about its numbers, and
    must not put "history is still loading" or "stale" over a deals-only table.
    `leads_available` and `lead_reason` still describe the lead lanes either way.
    """
    async with control_txn() as session:
        rows = (
            await session.execute(
                select(
                    CrmLane.lane,
                    CrmLane.status,
                    CrmLane.block_reason,
                    CrmLane.progress_done,
                    CrmLane.progress_total,
                    CrmLane.last_clean_at,
                ).where(CrmLane.portal_id == portal_id)
            )
        ).all()
    lanes = {
        row.lane: _LaneState(
            status=row.status,
            block_reason=row.block_reason,
            progress_done=int(row.progress_done or 0),
            progress_total=row.progress_total,
            last_clean_at=row.last_clean_at,
        )
        for row in rows
    }

    def readable(name: str) -> tuple[bool, str]:
        lane = lanes.get(name)
        if lane is None:
            return False, "not_started"
        if lane.block_reason:
            return False, lane.block_reason
        return True, ""

    deals_available, deal_reason = readable(crm_lanes.DEAL_BACKFILL)
    leads_available, lead_reason = readable(crm_lanes.LEAD_BACKFILL)
    if leads_available:
        leads_available, lead_reason = readable(crm_lanes.LEAD_SWEEP)

    counted = leads and leads_available
    backfills = [lanes[crm_lanes.DEAL_BACKFILL]] if deals_available else []
    if counted:
        backfills.append(lanes[crm_lanes.LEAD_BACKFILL])
    history_complete = bool(backfills) and all(lane.status == crm_lanes.DONE for lane in backfills)

    if history_complete:
        progress_pct = 100
    else:
        done = sum(lane.progress_done for lane in backfills)
        total = sum(lane.progress_total or 0 for lane in backfills)
        # Never 100 while a backfill is still running: the page would promise a full history
        # it does not have.
        progress_pct = min(99, (100 * done) // total) if total > 0 else 0

    sweeps = [crm_lanes.DEAL_SWEEP] + ([crm_lanes.LEAD_SWEEP] if counted else [])
    cleaned = [lanes[name].last_clean_at if name in lanes else None for name in sweeps]
    data_as_of = None if any(moment is None for moment in cleaned) else min(
        moment for moment in cleaned if moment is not None
    )

    return Coverage(
        deals_available=deals_available,
        deal_reason=deal_reason,
        leads_available=leads_available,
        lead_reason=lead_reason,
        history_complete=history_complete,
        progress_pct=progress_pct,
        data_as_of=data_as_of,
    )
