"""The `crm_items` upsert: rows, their assignees and the lane cursors in ONE fenced transaction (§5.10).

The defences are `sync/upsert.py`'s, for its reason - one odd value from Bitrix24 must never
stall a lane: dedupe inside the chunk, chunks in a SAVEPOINT, a row-by-row retry that
quarantines the offender, and rows plus cursor committed together behind the fence.

A mirror adds one rule: **a version is never overwritten by an older read.** Each row carries
`read_at`, the worker's clock just before the request that produced it, and the conflict
update applies only when the stored row is not newer. A backfill page read before a deal was
edited therefore cannot undo the edit a sweep has already stored.

The two tombstones behave differently on purpose (decision 29). `not_found` is proof the record
is gone, so no read resurrects it; a record restored from the recycle bin comes back under a
new id. `evicted` only means the installer could not read it for a month, so a successful read
restores it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import and_, case, or_, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.bitrix.crm_items import ItemRow
from app.bitrix.utm import DIMENSIONS
from app.db.models import CRM_ITEM_DATA_COLUMNS, CrmItem
from app.db.session import tenant_txn
from app.logging import get_logger
from app.services.employees import upsert_placeholders
from app.services.portals import record_event
from app.sync.crm_lanes import Lane, store_lanes
from app.sync.lease import Fence, fenced_update
from app.sync.upsert import row_fault

__all__ = ["CHUNK_SIZE", "PROVEN_DELETED", "ItemsWritten", "item_values", "upsert_items"]

log = get_logger(__name__)

#: 500 x 27 bind parameters stays far below Postgres' 65 535 ceiling.
CHUNK_SIZE: Final[int] = 500

#: One visit's rejected rows are audited individually up to this many, then summarised.
_MAX_REJECT_EVENTS: Final[int] = 20

_KEY: Final[tuple[str, str, str]] = ("portal_id", "entity_type_id", "id")

#: `crm_items.delete_reason` of a deletion `crm.item.get` proved (decision 29). Our own column
#: value, not a Bitrix24 error string, however alike the two are spelled.
PROVEN_DELETED: Final[str] = "not_found"

#: `(entity type id, record id or None, reason)` - a row the parser or the database refused.
Rejection = tuple[int, int | None, str]


@dataclass(frozen=True)
class ItemsWritten:
    #: Rows that reached `crm_items` or were refused by the version guard - the database does
    #: not say which without a RETURNING that would buy nothing.
    written: int
    quarantined: int


def item_values(portal_id: int, row: ItemRow, read_at: dt.datetime) -> dict[str, Any]:
    """One `ItemRow` as `crm_items` columns."""
    return {
        "portal_id": portal_id,
        "entity_type_id": row.entity_type_id,
        "id": row.id,
        "category_id": row.category_id,
        "stage_id": row.stage_id or None,
        "stage_semantic": row.stage_semantic or None,
        "assigned_by_id": row.assigned_by_id,
        "created_time": row.created_time,
        "updated_time": row.updated_time,
        "moved_time": row.moved_time,
        "closed": row.closed,
        "opportunity": row.opportunity,
        "currency_id": row.currency_id or None,
        "lead_id": row.lead_id,
        "contact_ids": list(row.contact_ids),
        "company_id": row.company_id,
        # An empty tag is kept as "", the `/utm` report's own "no tag" bucket.
        **dict(zip(DIMENSIONS, row.utm, strict=True)),
        "read_at": read_at,
    }


def _insert(chunk: Sequence[dict[str, Any]]) -> Any:
    statement = pg_insert(CrmItem).values(list(chunk))
    excluded = statement.excluded
    table = CrmItem.__table__
    changed = tuple_(*[table.c[name] for name in CRM_ITEM_DATA_COLUMNS]).is_distinct_from(
        tuple_(*[excluded[name] for name in CRM_ITEM_DATA_COLUMNS])
    )
    updates: dict[str, Any] = {name: excluded[name] for name in CRM_ITEM_DATA_COLUMNS}
    updates.update(
        read_at=excluded.read_at,
        synced_at=func.now(),
        content_changed_at=case((changed, func.now()), else_=table.c.content_changed_at),
        deleted_at=None,
        delete_reason=None,
        unreadable_since=None,
    )
    return statement.on_conflict_do_update(
        index_elements=list(_KEY),
        set_=updates,
        where=and_(
            table.c.read_at <= excluded.read_at,
            or_(table.c.delete_reason.is_(None), table.c.delete_reason != PROVEN_DELETED),
        ),
    )


def _reason(exc: Exception) -> str:
    """Class and SQLSTATE only: the driver's message quotes the value (`sync/upsert.py`)."""
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    return f"{name}:{state}" if state else name


async def _write_chunk(
    session: AsyncSession, chunk: Sequence[dict[str, Any]]
) -> tuple[int, list[Rejection]]:
    savepoint = await session.begin_nested()
    try:
        await session.execute(_insert(chunk))
    except DBAPIError as exc:
        # `sync/upsert.py::row_fault`: only a row's own value is quarantined, never an outage.
        if not row_fault(exc):
            raise
        await savepoint.rollback()
    else:
        await savepoint.commit()
        return len(chunk), []

    written = 0
    rejected: list[Rejection] = []
    for row in chunk:
        row_savepoint = await session.begin_nested()
        try:
            await session.execute(_insert([row]))
        except DBAPIError as exc:
            if not row_fault(exc):
                raise
            await row_savepoint.rollback()
            rejected.append((int(row["entity_type_id"]), int(row["id"]), _reason(exc)))
        else:
            await row_savepoint.commit()
            written += 1
    return written, rejected


async def _record_rejections(
    session: AsyncSession, portal_id: int, rejected: Sequence[Rejection]
) -> None:
    for entity_type_id, item_id, reason in rejected[:_MAX_REJECT_EVENTS]:
        await record_event(
            session,
            portal_id,
            "row_rejected",
            details={"entity_type_id": entity_type_id, "id": item_id, "reason": reason},
        )
    overflow = len(rejected) - _MAX_REJECT_EVENTS
    if overflow > 0:
        await record_event(
            session,
            portal_id,
            "row_rejected",
            details={"suppressed": overflow, "reason": "reject_event_cap"},
        )


async def upsert_items(
    fence: Fence,
    rows: Sequence[ItemRow],
    *,
    read_at: dt.datetime,
    lanes: Sequence[Lane] = (),
    rejected: Sequence[Rejection] = (),
) -> ItemsWritten:
    """Upsert `rows`, placeholder their assignees, store `lanes`, all behind the fence.

    Raises `FenceLost` when the lease or `sync_generation` moved; nothing is committed then -
    which is what keeps a run that outlived an uninstall or an opt-out from re-inserting rows
    the purge has just deleted.
    """
    portal_id = fence.portal_id
    prepared: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        # Later duplicates win: two copies of one id in a batch are two reads of one record.
        prepared[(row.entity_type_id, row.id)] = item_values(portal_id, row, read_at)
    values = list(prepared.values())

    written = 0
    all_rejects: list[Rejection] = list(rejected)
    quarantined: set[tuple[int, int]] = set()
    async with tenant_txn(portal_id) as session:
        for start in range(0, len(values), CHUNK_SIZE):
            chunk = values[start : start + CHUNK_SIZE]
            chunk_written, chunk_rejects = await _write_chunk(session, chunk)
            written += chunk_written
            all_rejects.extend(chunk_rejects)
            quarantined.update((entity, item_id) for entity, item_id, _ in chunk_rejects if item_id)

        if written:
            # §7 writer (a): an assignee nobody has named yet becomes a placeholder the employee
            # refresh resolves, so the Deals report can print a name instead of "User #id".
            await upsert_placeholders(
                session,
                portal_id,
                (
                    value["assigned_by_id"]
                    for key, value in prepared.items()
                    if value["assigned_by_id"] and key not in quarantined
                ),
            )
        if all_rejects:
            await _record_rejections(session, portal_id, all_rejects)
        if lanes:
            await store_lanes(session, portal_id, lanes)
        await fenced_update(session, fence, {})

    if all_rejects:
        log.warning(
            "crm upsert: rows quarantined",
            extra={"portal_id": portal_id, "count": len(all_rejects), "written": written},
        )
    return ItemsWritten(written=written, quarantined=len(all_rejects))
