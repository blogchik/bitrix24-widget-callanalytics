"""Record ids waiting for a re-read (`crm_dirty`): the queue every CRM change source shares (§5.11-§5.12).

A change signal (milestone M6), a reconciliation mismatch and a patrol finding all say one thing -
"read this record again" - and none of them carries a value: the stored row always comes from
the re-read. So the queue holds ids, why they were marked, and `seq`.

`seq` answers the race every such queue has: a mark that arrives while a refresh is reading the
record. A refresh deletes a row only where `seq` still equals the value it read, so the later
mark survives and the record is read once more.

Every function takes the caller's `tenant_txn` session: `crm_dirty` carries FORCED row-level
security, and a mark written outside tenant context would be refused (§3).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CrmDirty

__all__ = [
    "ATTEMPTS_BEFORE_HOLD",
    "REASON_RECONCILE_LOCAL",
    "REASON_RECONCILE_REMOTE",
    "REASON_SIGNAL",
    "REASON_SIGNAL_DELETE",
    "DirtyId",
    "consume",
    "defer",
    "due",
    "hold",
    "mark",
]

#: The mirror has the record and a listing of the portal did not.
REASON_RECONCILE_LOCAL: Final[int] = 1
#: The portal listed the record and the mirror does not have it.
REASON_RECONCILE_REMOTE: Final[int] = 2
#: A change event named the record (M6).
REASON_SIGNAL: Final[int] = 4
#: A delete event named the record (M6). Still only a mark: `crm.item.get` decides.
REASON_SIGNAL_DELETE: Final[int] = 8

#: An id that cannot be resolved after this many passes waits a day instead of an hour.
ATTEMPTS_BEFORE_HOLD: Final[int] = 10
_RETRY_BASE_SECONDS: Final[float] = 60.0
_RETRY_MAX_SECONDS: Final[float] = 3_600.0
_HOLD_SECONDS: Final[float] = 86_400.0
_ATTEMPTS_MAX: Final[int] = 32_000

_CHUNK: Final[int] = 1_000


@dataclass(frozen=True)
class DirtyId:
    """One queued id, with the `seq` its refresh must match to consume it."""

    entity_type_id: int
    id: int
    seq: int
    reasons: int = 0
    attempts: int = 0


def _chunks[T](values: Sequence[T], size: int) -> list[Sequence[T]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


async def mark(
    session: AsyncSession,
    portal_id: int,
    entity_type_id: int,
    ids: Iterable[int],
    *,
    reasons: int,
    now: dt.datetime,
) -> int:
    """Queue ids for a re-read, or bump the `seq` of ids already queued. Returns how many."""
    wanted = sorted({int(value) for value in ids if int(value) > 0})
    for chunk in _chunks(wanted, _CHUNK):
        statement = pg_insert(CrmDirty).values(
            [
                {
                    "portal_id": portal_id,
                    "entity_type_id": entity_type_id,
                    "id": item_id,
                    "reasons": reasons,
                    "first_marked_at": now,
                    "not_before": now,
                }
                for item_id in chunk
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["portal_id", "entity_type_id", "id"],
                set_={
                    "reasons": CrmDirty.reasons.op("|")(statement.excluded.reasons),
                    "seq": CrmDirty.seq + 1,
                    "not_before": func.least(CrmDirty.not_before, statement.excluded.not_before),
                },
            )
        )
    return len(wanted)


async def due(
    session: AsyncSession,
    portal_id: int,
    entity_type_id: int,
    *,
    now: dt.datetime,
    limit: int,
) -> list[DirtyId]:
    """The oldest ids ready for a re-read: past `not_before` and not on hold."""
    rows = (
        await session.execute(
            select(
                CrmDirty.entity_type_id,
                CrmDirty.id,
                CrmDirty.seq,
                CrmDirty.reasons,
                CrmDirty.attempts,
            )
            .where(
                CrmDirty.portal_id == portal_id,
                CrmDirty.entity_type_id == entity_type_id,
                CrmDirty.not_before <= now,
                or_(CrmDirty.held_until.is_(None), CrmDirty.held_until <= now),
            )
            .order_by(CrmDirty.first_marked_at, CrmDirty.id)
            .limit(limit)
        )
    ).all()
    return [
        DirtyId(
            entity_type_id=int(row.entity_type_id),
            id=int(row.id),
            seq=int(row.seq),
            reasons=int(row.reasons),
            attempts=int(row.attempts),
        )
        for row in rows
    ]


async def consume(session: AsyncSession, portal_id: int, items: Iterable[DirtyId]) -> int:
    """Delete the marks a refresh served - only where `seq` is still the one it read."""
    keys = [(item.entity_type_id, item.id, item.seq) for item in items]
    removed = 0
    for chunk in _chunks(keys, _CHUNK):
        result = await session.execute(
            delete(CrmDirty)
            .where(
                CrmDirty.portal_id == portal_id,
                tuple_(CrmDirty.entity_type_id, CrmDirty.id, CrmDirty.seq).in_(list(chunk)),
            )
            .execution_options(synchronize_session=False)
        )
        removed += int(cast("CursorResult[Any]", result).rowcount or 0)
    return removed


async def defer(
    session: AsyncSession, portal_id: int, items: Iterable[DirtyId], *, now: dt.datetime
) -> None:
    """Try again later: 60 s doubling to an hour, and a day once an id has failed ten times."""
    groups: dict[tuple[dt.datetime, dt.datetime | None], list[tuple[int, int]]] = {}
    for item in items:
        delay = min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * 2.0 ** min(item.attempts, 12))
        held = (
            now + dt.timedelta(seconds=_HOLD_SECONDS)
            if item.attempts + 1 >= ATTEMPTS_BEFORE_HOLD
            else None
        )
        groups.setdefault((now + dt.timedelta(seconds=delay), held), []).append(
            (item.entity_type_id, item.id)
        )
    for (not_before, held), keys in groups.items():
        values: dict[str, Any] = {
            "attempts": func.least(CrmDirty.attempts + 1, _ATTEMPTS_MAX),
            "not_before": not_before,
        }
        if held is not None:
            values["held_until"] = held
        for chunk in _chunks(keys, _CHUNK):
            await session.execute(
                update(CrmDirty)
                .where(
                    CrmDirty.portal_id == portal_id,
                    tuple_(CrmDirty.entity_type_id, CrmDirty.id).in_(list(chunk)),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )


async def hold(
    session: AsyncSession, portal_id: int, items: Iterable[DirtyId], *, until: dt.datetime
) -> None:
    """Keep ids queued but untouched until `until` - a guard said "not now", not "never"."""
    keys = [(item.entity_type_id, item.id) for item in items]
    for chunk in _chunks(keys, _CHUNK):
        await session.execute(
            update(CrmDirty)
            .where(
                CrmDirty.portal_id == portal_id,
                tuple_(CrmDirty.entity_type_id, CrmDirty.id).in_(list(chunk)),
            )
            .values(held_until=until)
            .execution_options(synchronize_session=False)
        )
