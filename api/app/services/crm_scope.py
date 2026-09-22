"""What Bitrix24 itself proves one viewer may read from the CRM mirror (0005, §4.14).

Bitrix24 exposes no CRM permission data over REST. Probed against a real portal:
`crm.role.list`, `crm.role.relation.list`, `crm.permissions.get`, `crm.permission.get` and
`crm.settings.permissions.get` all answer ERROR_METHOD_NOT_FOUND. So the only way to learn
what a person may see is to ask Bitrix24 about records **with that person's own token**, and
what this module stores is the answers rather than the rules behind them.

**The unit is a (funnel, assignee) cell**, and the grid is the PORTAL's, not the viewer's.
That is the whole reason a department head works: the census asks "may you see anything in
funnel C assigned to U?" for every U the portal has records for, so a supervisor whose
Bitrix24 role covers their team proves their team's cells and gets a report with their team
in it. A census restricted to the viewer's own records could only ever answer "yourself",
which is not what "each employee sees what Bitrix24 shows them" means.

Measured on the production portal: 29 deal cells plus 18 lead assignees is 47 commands, two
batches, 8.8 seconds - against 49 seconds to enumerate every visible id instead.

**The row echo is the safety property.** An unknown filter key may be IGNORED rather than
refused (docs/bitrix24-api-research.md:121), and a command whose filter was dropped comes
back full of the viewer's newest readable rows - which would "prove" a cell nobody granted.
`census_proven` therefore requires every returned row to carry back the assignee and funnel
that were filtered on, and answers `None` rather than `False` when the shape is unusable, so
a broken answer can never pass for an honest empty one.

**The sentinel is the second half of that.** One command per entity filters on an assignee
id no portal issues. Any row back means the filter is not being applied at all, and the
whole entity is marked `unprovable` - that viewer stays on the live read rather than being
served a scope measured with a broken ruler.

What this CANNOT prove, stated because the code should not pretend otherwise:

* A cell proves Bitrix24 showed the viewer **at least one** record in it; the predicate then
  serves them **all** of that cell. The known gap is a record that is "available to
  everyone" - no dialect in this codebase carries that flag, so a cell can be proven by an
  open record the role grid did not grant.
* The mirror's `assigned_by_id` and `category_id` are as of the last sync, and `updatedTime`
  is not a complete change feed (`sync/crm_sweep.py`). A record that moved since is judged
  by where it was.

Both are bounded by the TTL and are why `crm_scope_enabled` ships off.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import and_, desc, false, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from app.bitrix.client import MAX_BATCH_COMMANDS, BitrixClient
from app.bitrix.crm_items import (
    DEAL_ITEM,
    DEAL_LEGACY,
    ENTITY_DEAL,
    ENTITY_LEAD,
    LEAD_ITEM,
    LEAD_LEGACY,
    Command,
    MirrorDialect,
    census_command,
    census_key,
    census_proven,
    page_rows,
)
from app.config import settings
from app.db.models import CrmItem, CrmLane, CrmViewerScope, PortalSync
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger
from app.sync import crm_backfill, crm_lanes

__all__ = [
    "SENTINEL_USER_ID",
    "VERDICT_EMPTY",
    "VERDICT_OK",
    "VERDICT_UNPROVABLE",
    "ViewerScope",
    "as_json",
    "load_scope",
    "run_census",
    "scope_predicate",
    "serves_scope",
]

_log = get_logger(__name__)

VERDICT_OK: Final[str] = "ok"
VERDICT_EMPTY: Final[str] = "empty"
VERDICT_UNPROVABLE: Final[str] = "unprovable"

#: An assignee id no Bitrix24 portal issues (max int32). A command filtered on it must come
#: back empty; anything else means the filter was not applied.
SENTINEL_USER_ID: Final[int] = 2_147_483_647

_DEAL: Final[str] = "deal"
_LEAD: Final[str] = "lead"


@dataclass(frozen=True)
class ViewerScope:
    """One census, as the predicate and the freshness rule both read it."""

    user_id: int
    resolved_at: dt.datetime
    sync_generation: int
    deal_verdict: str
    lead_verdict: str
    deal_reason: str
    lead_reason: str
    #: `((category_id, assignee_id), ...)`, proven only. A positive allow-list.
    deal_cells: tuple[tuple[int, int], ...]
    lead_assignees: tuple[int, ...]
    truncated: bool
    commands: int

    @property
    def measured_nothing(self) -> bool:
        """Both entities failed their check, so this census can stand on nothing.

        `empty` is NOT a failure: a portal with no records to ask about has answered, and
        an empty report is the correct answer. Only `unprovable` on both sides means the
        ruler itself was broken, and `load_scope` refuses such a row.
        """
        return (
            self.deal_verdict == VERDICT_UNPROVABLE
            and self.lead_verdict == VERDICT_UNPROVABLE
        )


def _dialect(entity: str, name: str) -> MirrorDialect:
    legacy = name == crm_backfill.DIALECT_LEGACY
    if entity == _DEAL:
        return DEAL_LEGACY if legacy else DEAL_ITEM
    return LEAD_LEGACY if legacy else LEAD_ITEM


async def _learned_dialects(portal_id: int) -> dict[str, MirrorDialect]:
    """The dialect each entity's backfill already learned - never a guess.

    A census taken on one spelling says nothing about another: `assignedById` and
    `ASSIGNED_BY_ID` are different filters, and asking with the wrong one is exactly the
    ignored-key case the echo exists to catch. Read from `crm_lanes` the way the worker
    reads it (`jobs/definitions.py::_entity_dialect`).
    """
    lanes = {_DEAL: crm_lanes.DEAL_BACKFILL, _LEAD: crm_lanes.LEAD_BACKFILL}
    async with control_txn() as session:
        found = (
            await session.execute(
                select(CrmLane.lane, CrmLane.cursor).where(
                    CrmLane.portal_id == portal_id,
                    CrmLane.lane.in_(tuple(lanes.values())),
                )
            )
        ).all()
    rows: dict[str, Any] = {str(row[0]): row[1] for row in found}
    out: dict[str, MirrorDialect] = {}
    for entity, lane in lanes.items():
        cursor = rows.get(lane)
        name = (
            crm_backfill.BackfillCursor.from_json(cursor).dialect
            if cursor is not None
            else crm_backfill.DIALECT_ITEM
        )
        out[entity] = _dialect(entity, name)
    return out


async def _candidate_grid(portal_id: int) -> tuple[list[tuple[int, int]], list[int], bool]:
    """The portal's `(funnel, assignee)` cells and lead assignees, most recently touched first.

    THE PORTAL's grid, not the viewer's: the census asks whether this viewer may see each
    cell, and a supervisor proves their team's cells only because their team's cells are
    asked about at all.

    `unreadable_since` rows are left out. A record whose current funnel and stage even the
    installer credential can no longer read must not become a cell, and must not be served
    to a scoped viewer either (`scope_predicate` excludes them too).
    """
    cap = settings.crm_scope_max_cells
    touched = func.max(func.coalesce(CrmItem.updated_time, CrmItem.created_time))
    base = (
        CrmItem.portal_id == portal_id,
        CrmItem.deleted_at.is_(None),
        CrmItem.unreadable_since.is_(None),
        CrmItem.assigned_by_id.is_not(None),
    )
    async with tenant_txn(portal_id) as session:
        deal_rows = (
            await session.execute(
                select(CrmItem.category_id, CrmItem.assigned_by_id)
                .where(*base, CrmItem.entity_type_id == ENTITY_DEAL, CrmItem.category_id.is_not(None))
                .group_by(CrmItem.category_id, CrmItem.assigned_by_id)
                .order_by(desc(touched))
                .limit(cap + 1)
            )
        ).all()
        lead_rows = (
            await session.execute(
                select(CrmItem.assigned_by_id)
                .where(*base, CrmItem.entity_type_id == ENTITY_LEAD)
                .group_by(CrmItem.assigned_by_id)
                .order_by(desc(touched))
                .limit(cap + 1)
            )
        ).all()
    truncated = len(deal_rows) > cap or len(lead_rows) > cap
    cells = [(int(row[0]), int(row[1])) for row in deal_rows[:cap]]
    leads = [int(row[0]) for row in lead_rows[:cap]]
    return cells, leads, truncated


def _sentinel_key(entity: str) -> str:
    return f"s.{entity}"


def _commands(
    cells: Sequence[tuple[int, int]],
    leads: Sequence[int],
    dialects: dict[str, MirrorDialect],
) -> list[Command]:
    """Every cell plus one sentinel per entity, in the order they will be read back."""
    out: list[Command] = []
    deal = dialects[_DEAL]
    lead = dialects[_LEAD]
    if cells:
        out.append(
            census_command(
                deal,
                _sentinel_key(_DEAL),
                assigned_by_id=SENTINEL_USER_ID,
                category_id=cells[0][0],
            )
        )
        out.extend(
            census_command(
                deal,
                census_key(deal, assigned_by_id=uid, category_id=cat),
                assigned_by_id=uid,
                category_id=cat,
            )
            for cat, uid in cells
        )
    if leads:
        out.append(
            census_command(lead, _sentinel_key(_LEAD), assigned_by_id=SENTINEL_USER_ID)
        )
        out.extend(
            census_command(
                lead, census_key(lead, assigned_by_id=uid, category_id=None), assigned_by_id=uid
            )
            for uid in leads
        )
    return out


async def run_census(
    portal_id: int,
    user_id: int,
    *,
    access_token: str,
    client_endpoint: str,
) -> ViewerScope:
    """Ask Bitrix24, with THIS viewer's token, which cells they may read. Then store it.

    Never raises for a Bitrix24 answer: a failure is a verdict, and a viewer whose census
    could not be trusted belongs on the live read rather than on a guess. Only a database
    failure propagates.
    """
    dialects = await _learned_dialects(portal_id)
    cells, leads, truncated = await _candidate_grid(portal_id)
    commands = _commands(cells, leads, dialects)

    proven_cells: list[tuple[int, int]] = []
    proven_leads: list[int] = []
    deal_reason = ""
    lead_reason = ""
    deal_broken = False
    lead_broken = False
    sent = 0

    if commands:
        size = min(settings.crm_scope_commands_per_batch, MAX_BATCH_COMMANDS)
        client = BitrixClient(
            endpoint=client_endpoint, access_token=access_token, portal_id=portal_id
        )
        try:
            for start in range(0, len(commands), size):
                chunk = commands[start : start + size]
                try:
                    batch = await client.batch(chunk, halt=0)
                # Broad on purpose: a Bitrix24 failure is a VERDICT here, never a raise.
                # A viewer whose census could not be taken belongs on the live read, and a
                # transport error that escaped would take their report down instead.
                except Exception as exc:
                    deal_broken = lead_broken = True
                    deal_reason = lead_reason = f"batch:{type(exc).__name__}"
                    break
                sent += len(chunk)
                for key, _, params in chunk:
                    entity = _DEAL if key.startswith(("d.", "s.deal")) else _LEAD
                    dialect = dialects[entity]
                    result = batch.get(key)
                    if batch.error(key) is not None:
                        # One refused command narrows this viewer; it does not condemn the
                        # entity, because a portal may legitimately refuse one funnel.
                        continue
                    if key == _sentinel_key(entity):
                        rows = page_rows(result, dialect)
                        if rows is None or rows:
                            if entity == _DEAL:
                                deal_broken, deal_reason = True, "sentinel"
                            else:
                                lead_broken, lead_reason = True, "sentinel"
                        continue
                    filter_ = params["filter"]
                    assignee = int(filter_[dialect.wire["assigned_by_id"]])
                    category = (
                        int(filter_[dialect.wire["category_id"]])
                        if entity == _DEAL
                        else None
                    )
                    verdict = census_proven(
                        result, dialect, assigned_by_id=assignee, category_id=category
                    )
                    if verdict is None:
                        if entity == _DEAL:
                            deal_broken, deal_reason = True, "shape"
                        else:
                            lead_broken, lead_reason = True, "shape"
                    elif verdict and category is not None:
                        proven_cells.append((category, assignee))
                    elif verdict:
                        proven_leads.append(assignee)
        finally:
            await client.aclose()

    deal_verdict = (
        VERDICT_UNPROVABLE
        if deal_broken
        else VERDICT_EMPTY
        if not cells
        else VERDICT_OK
    )
    lead_verdict = (
        VERDICT_UNPROVABLE
        if lead_broken
        else VERDICT_EMPTY
        if not leads
        else VERDICT_OK
    )
    if deal_broken:
        proven_cells = []
    if lead_broken:
        proven_leads = []

    scope = await _store(
        portal_id,
        user_id,
        dialects=dialects,
        deal_verdict=deal_verdict,
        lead_verdict=lead_verdict,
        deal_reason=deal_reason,
        lead_reason=lead_reason,
        deal_cells=proven_cells,
        lead_assignees=proven_leads,
        truncated=truncated,
        commands=sent,
    )
    _log.info(
        "crm_scope: census done",
        extra={
            "portal_id": portal_id,
            "subject": user_id,
            "commands": sent,
            "deal_verdict": deal_verdict,
            "lead_verdict": lead_verdict,
            "deal_cells": len(proven_cells),
            "lead_assignees": len(proven_leads),
        },
    )
    return scope


async def _store(
    portal_id: int,
    user_id: int,
    *,
    dialects: dict[str, MirrorDialect],
    deal_verdict: str,
    lead_verdict: str,
    deal_reason: str,
    lead_reason: str,
    deal_cells: Sequence[tuple[int, int]],
    lead_assignees: Sequence[int],
    truncated: bool,
    commands: int,
) -> ViewerScope:
    async with control_txn() as session:
        generation = int(
            (
                await session.execute(
                    select(PortalSync.sync_generation).where(PortalSync.portal_id == portal_id)
                )
            ).scalar_one_or_none()
            or 0
        )
    values = {
        "portal_id": portal_id,
        "user_id": user_id,
        "sync_generation": generation,
        "dialects": {name: dialect.name for name, dialect in dialects.items()},
        "deal_verdict": deal_verdict,
        "lead_verdict": lead_verdict,
        "deal_reason": deal_reason[:40],
        "lead_reason": lead_reason[:40],
        "deal_cells": [[cat, uid] for cat, uid in deal_cells],
        "lead_assignees": list(lead_assignees),
        "truncated": truncated,
        "commands": min(commands, 32_767),
    }
    async with tenant_txn(portal_id) as session:
        await session.execute(
            pg_insert(CrmViewerScope)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["portal_id", "user_id"],
                set_={**{k: v for k, v in values.items() if k not in ("portal_id", "user_id")},
                      "resolved_at": func.now()},
            )
        )
        row = (
            await session.execute(
                select(CrmViewerScope).where(
                    CrmViewerScope.portal_id == portal_id, CrmViewerScope.user_id == user_id
                )
            )
        ).scalar_one()
        return _row(row)


def _row(row: CrmViewerScope) -> ViewerScope:
    return ViewerScope(
        user_id=row.user_id,
        resolved_at=row.resolved_at,
        sync_generation=row.sync_generation,
        deal_verdict=row.deal_verdict,
        lead_verdict=row.lead_verdict,
        deal_reason=row.deal_reason,
        lead_reason=row.lead_reason,
        deal_cells=tuple(
            (int(pair[0]), int(pair[1])) for pair in (row.deal_cells or []) if len(pair) == 2
        ),
        lead_assignees=tuple(int(value) for value in (row.lead_assignees or [])),
        truncated=bool(row.truncated),
        commands=int(row.commands),
    )


async def load_scope(portal_id: int, user_id: int, *, now: dt.datetime | None = None) -> ViewerScope | None:
    """This viewer's census, or None when there is none, it is stale, or it is orphaned.

    Three fences, and each answers a different way of being wrong:

    * the TTL, because Bitrix24 can change a person's rights without telling us;
    * `sync_generation`, because a reinstall resets the mirror and a proof taken under the
      previous install describes records that no longer exist;
    * `unprovable` on both entities, because that census measured nothing it can stand on.
    """
    async with tenant_txn(portal_id) as session:
        row = (
            await session.execute(
                select(CrmViewerScope).where(
                    CrmViewerScope.portal_id == portal_id, CrmViewerScope.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    scope = _row(row)
    async with control_txn() as session:
        generation = int(
            (
                await session.execute(
                    select(PortalSync.sync_generation).where(PortalSync.portal_id == portal_id)
                )
            ).scalar_one_or_none()
            or 0
        )
    if scope.sync_generation != generation:
        return None
    moment = now or dt.datetime.now(dt.UTC)
    if (moment - scope.resolved_at).total_seconds() > settings.crm_scope_ttl_sec:
        return None
    if scope.measured_nothing:
        return None
    return scope


def serves_scope(scope: ViewerScope | None) -> bool:
    """True when this census can answer a report at all."""
    return scope is not None


def scope_predicate(scope: ViewerScope) -> ColumnElement[bool]:
    """The CRM row predicate this census produces. Never None: a census is never everything.

    The entity-type term and the scope term are ONE term, not two. `utm_counts` reads leads
    and deals in a single statement, so a flat predicate appended after `entity_type_id IN
    (1, 2)` would apply the deal grid to leads and the lead grid to deals.

    An empty proven set is `false()` spelled out, never an omitted term and never an empty
    `IN`: an omitted term means everything, and that is the one mistake this module cannot
    be allowed to make.
    """
    deal_term: ColumnElement[bool] = false()
    if scope.deal_verdict == VERDICT_OK and scope.deal_cells:
        assignees = sorted({uid for _, uid in scope.deal_cells})
        deal_term = and_(
            # Redundant and deliberate: `crm_items_assignee_idx` leads
            # `(portal_id, entity_type_id, assigned_by_id, ...)` and a row-wise tuple IN
            # cannot use it. This conjunct can, and narrows before the tuple test runs.
            CrmItem.assigned_by_id.in_(assignees),
            tuple_(CrmItem.category_id, CrmItem.assigned_by_id).in_(
                [(cat, uid) for cat, uid in scope.deal_cells]
            ),
        )
    lead_term: ColumnElement[bool] = false()
    if scope.lead_verdict == VERDICT_OK and scope.lead_assignees:
        lead_term = CrmItem.assigned_by_id.in_(sorted(set(scope.lead_assignees)))
    return and_(
        # A row the installer credential can no longer read is served to nobody scoped: the
        # census could not have asked about it, and the mirror's copy of its funnel is stale
        # by definition.
        CrmItem.unreadable_since.is_(None),
        or_(
            and_(CrmItem.entity_type_id == ENTITY_DEAL, deal_term),
            and_(CrmItem.entity_type_id == ENTITY_LEAD, lead_term),
        ),
    )


def as_json(scope: ViewerScope | None) -> dict[str, Any]:
    """What `/me` tells the SPA about this viewer's census."""
    if scope is None:
        return {"state": "none"}
    return {
        "state": "ready",
        "deal_verdict": scope.deal_verdict,
        "lead_verdict": scope.lead_verdict,
        "truncated": scope.truncated,
        "resolved_at": scope.resolved_at.isoformat(),
    }
