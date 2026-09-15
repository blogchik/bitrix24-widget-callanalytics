"""What a confirmed absence does to the mirror: tombstones and unreadable marks (§5.12, decision 29).

Only two answers of `crm.item.get` change a row here, and they change it differently:

* **NOT_FOUND** is proof the record is gone. Every data column is NULLed at once (the privacy
  policy's "values of a deleted record are removed immediately"), the id stays 35 days, and no
  later read resurrects it - a record restored from the recycle bin comes back under a new id.
  The tombstone is an upsert, so an id the mirror never stored is remembered too, and an older
  backfill page cannot import it afterwards.
* **ACCESS_DENIED** proves nothing about the record, only about the credential. The row keeps
  its values and gains `unreadable_since`; the daily retention hides it after 7 days and evicts
  it after 30 (`sync/purge.py::purge_crm_retention`).

Both run inside the caller's fenced tenant transaction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any, Final, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CRM_ITEM_DATA_COLUMNS, CrmItem
from app.sync.crm_upsert import PROVEN_DELETED

__all__ = ["live_count", "mark_unreadable", "tombstone"]

_CHUNK: Final[int] = 1_000


async def tombstone(
    session: AsyncSession,
    portal_id: int,
    entity_type_id: int,
    ids: Iterable[int],
    *,
    read_at: dt.datetime,
) -> int:
    """NULL every value of records `crm.item.get` proved deleted; keep only their ids."""
    wanted = sorted({int(value) for value in ids if int(value) > 0})
    table = CrmItem.__table__
    written = 0
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        statement = pg_insert(CrmItem).values(
            [
                {
                    "portal_id": portal_id,
                    "entity_type_id": entity_type_id,
                    "id": item_id,
                    "read_at": read_at,
                    "deleted_at": read_at,
                    "delete_reason": PROVEN_DELETED,
                }
                for item_id in chunk
            ]
        )
        excluded = statement.excluded
        updates: dict[str, Any] = dict.fromkeys(CRM_ITEM_DATA_COLUMNS)
        updates.update(
            read_at=excluded.read_at,
            deleted_at=excluded.deleted_at,
            delete_reason=excluded.delete_reason,
            synced_at=func.now(),
            content_changed_at=func.now(),
            unreadable_since=None,
        )
        result = await session.execute(
            statement.on_conflict_do_update(
                index_elements=["portal_id", "entity_type_id", "id"],
                set_=updates,
                where=and_(
                    table.c.read_at <= excluded.read_at,
                    or_(table.c.delete_reason.is_(None), table.c.delete_reason != PROVEN_DELETED),
                ),
            )
        )
        written += int(cast("CursorResult[Any]", result).rowcount or 0)
    return written


async def mark_unreadable(
    session: AsyncSession,
    portal_id: int,
    entity_type_id: int,
    ids: Iterable[int],
    *,
    now: dt.datetime,
) -> int:
    """Stamp `unreadable_since` on live rows the credential was refused, once."""
    wanted = sorted({int(value) for value in ids if int(value) > 0})
    marked = 0
    for start in range(0, len(wanted), _CHUNK):
        result = await session.execute(
            update(CrmItem)
            .where(
                CrmItem.portal_id == portal_id,
                CrmItem.entity_type_id == entity_type_id,
                CrmItem.id.in_(wanted[start : start + _CHUNK]),
                CrmItem.deleted_at.is_(None),
            )
            .values(unreadable_since=func.coalesce(CrmItem.unreadable_since, now))
            .execution_options(synchronize_session=False)
        )
        marked += int(cast("CursorResult[Any]", result).rowcount or 0)
    return marked


async def live_count(session: AsyncSession, portal_id: int, entity_type_id: int) -> int:
    """Records of one entity the mirror currently holds - the base of the delete-ratio guard."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(CrmItem)
                .where(
                    CrmItem.portal_id == portal_id,
                    CrmItem.entity_type_id == entity_type_id,
                    CrmItem.deleted_at.is_(None),
                )
            )
        ).scalar_one()
    )
