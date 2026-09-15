"""Daily reconciliation: records the mirror has and the portal does not, or the reverse (§5.12).

Sweeps carry edits; neither a sweep nor, reliably, a change signal carries a deletion (a lost
event, a bulk delete that sent none). Reconciliation compares the two sides without reading
every record. It counts ids in ranges on both sides; a range whose counts disagree is split in
two, until it is small enough (`LEAF_SIZE`) to list its ids and compare them one by one.

Two limits of the method, stated rather than hidden:

* equal counts can hide a deletion balanced by a creation inside the same range. The sweep
  stores the new record, and a later run finds the deletion once the counts differ;
* a record the installer cannot read counts on neither side - `unreadable_since` rows are
  excluded locally too - so reconciliation never turns a visibility change into a deletion.

It marks ids dirty and deletes nothing: the dirty refresh confirms every absence with
`crm.item.get` before a tombstone is written (decision 29).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.client import BatchResult
from app.bitrix.crm_items import Command, MirrorDialect, range_count_command
from app.bitrix.errors import BitrixError, classify
from app.db.models import CrmItem
from app.db.session import tenant_txn
from app.services import crm_dirty
from app.sync.crm_backfill import DIALECT_ITEM, DIALECT_LEGACY
from app.sync.crm_lanes import Lane, store_lanes
from app.sync.crm_ranges import plan_ranges
from app.sync.lease import Fence, fenced_update

__all__ = [
    "LEAF_SIZE",
    "ReconcileCursor",
    "after_counts",
    "count_commands",
    "local_counts",
    "local_high_id",
    "local_ids",
    "read_counts",
    "start",
    "store_marks",
]

#: A range this small is listed id by id instead of being split again.
LEAF_SIZE: Final[int] = 2_000

_KEY_PREFIX: Final[str] = "c"


@dataclass(frozen=True)
class ReconcileCursor:
    dialect: str = DIALECT_ITEM
    started: bool = False
    #: `[lo, hi)` ranges whose counts are still to be compared.
    queue: tuple[tuple[int, int], ...] = ()
    #: `(lo, hi, last id listed)` ranges being compared id by id.
    leaves: tuple[tuple[int, int, int], ...] = ()
    #: Ids this run has marked dirty so far.
    marked: int = 0

    @property
    def finished(self) -> bool:
        return self.started and not self.queue and not self.leaves

    def to_json(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "started": self.started,
            "queue": [list(pair) for pair in self.queue],
            "leaves": [list(leaf) for leaf in self.leaves],
            "marked": self.marked,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ReconcileCursor:
        """The stored cursor, or a fresh one - starting over only costs a day's counts."""
        dialect = raw.get("dialect") if raw.get("dialect") in (DIALECT_ITEM, DIALECT_LEGACY) else DIALECT_ITEM
        if not raw.get("started"):
            return cls(dialect=str(dialect))
        try:
            queue = tuple((int(lo), int(hi)) for lo, hi in raw.get("queue") or [])
            leaves = tuple((int(lo), int(hi), int(after)) for lo, hi, after in raw.get("leaves") or [])
            marked = int(raw.get("marked") or 0)
        except (TypeError, ValueError):
            return cls(dialect=str(dialect))
        if any(hi <= lo for lo, hi in queue) or any(not lo - 1 <= after < hi for lo, hi, after in leaves):
            return cls(dialect=str(dialect))
        return cls(dialect=str(dialect), started=True, queue=queue, leaves=leaves, marked=marked)


def start(cursor: ReconcileCursor, high_id: int, *, ranges: int) -> ReconcileCursor:
    """The first comparisons: `[1, high_id]` cut into about `ranges` ranges, newest first."""
    width = max(LEAF_SIZE, -(-max(high_id, 1) // max(1, ranges)))
    queue = tuple((stream.lo, stream.hi) for stream in plan_ranges(high_id, width=width))
    return replace(cursor, started=True, queue=queue, leaves=(), marked=0)


def after_counts(
    cursor: ReconcileCursor,
    compared: Sequence[tuple[int, int]],
    local: Sequence[int],
    remote: Sequence[int],
) -> ReconcileCursor:
    """Drop the ranges that agree, list the small ones that do not, split the rest."""
    queue = list(cursor.queue[len(compared) :])
    leaves = list(cursor.leaves)
    for (lo, hi), mine, theirs in zip(compared, local, remote, strict=True):
        if mine == theirs:
            continue
        if hi - lo <= LEAF_SIZE:
            leaves.append((lo, hi, lo - 1))
        else:
            middle = (lo + hi) // 2
            queue.extend([(lo, middle), (middle, hi)])
    return replace(cursor, queue=tuple(queue), leaves=tuple(leaves))


def count_commands(dialect: MirrorDialect, ranges: Sequence[tuple[int, int]]) -> list[Command]:
    return [
        range_count_command(dialect, f"{_KEY_PREFIX}{index}", lo=lo, hi=hi)
        for index, (lo, hi) in enumerate(ranges)
    ]


def read_counts(
    batch: BatchResult, commands: Sequence[Command]
) -> tuple[list[int] | None, list[BitrixError]]:
    """Each command's `total`, or None and the errors when any count is missing.

    All or nothing: a count that did not come back is not zero, and comparing a range against
    an invented zero would mark every local id in it as deleted.
    """
    by_key = {command.key: command for command in batch.commands}
    totals: list[int] = []
    errors: list[BitrixError] = []
    for key, _method, _params in commands:
        answered = by_key.get(key)
        if answered is None:
            errors.append(classify(None, description=f"batch answered no {key}"))
        elif answered.error is not None:
            errors.append(answered.error)
        elif answered.total is None:
            errors.append(classify(None, description=f"{key}: no total"))
        else:
            totals.append(int(answered.total))
    return (None, errors) if errors else (totals, [])


async def local_high_id(session: AsyncSession, entity_type_id: int) -> int:
    """The newest id the mirror holds alive; ids above it are the sweep's, not ours."""
    value = (
        await session.execute(
            select(func.max(CrmItem.id)).where(
                CrmItem.entity_type_id == entity_type_id, CrmItem.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    return int(value or 0)


_LOCAL_COUNTS: Final = text(
    """
    SELECT r.idx, count(i.id) AS n
      FROM unnest(CAST(:los AS bigint[]), CAST(:his AS bigint[])) WITH ORDINALITY AS r(lo, hi, idx)
      LEFT JOIN crm_items i
             ON i.entity_type_id = :entity
            AND i.id >= r.lo AND i.id < r.hi
            AND i.deleted_at IS NULL AND i.unreadable_since IS NULL
     GROUP BY r.idx
     ORDER BY r.idx
    """
)


async def local_counts(
    session: AsyncSession, entity_type_id: int, ranges: Sequence[tuple[int, int]]
) -> list[int]:
    """Live, readable ids per range, in one statement, under the caller's tenant context."""
    if not ranges:
        return []
    rows = (
        await session.execute(
            _LOCAL_COUNTS,
            {
                "los": [lo for lo, _ in ranges],
                "his": [hi for _, hi in ranges],
                "entity": entity_type_id,
            },
        )
    ).all()
    return [int(row.n) for row in rows]


async def local_ids(
    session: AsyncSession, entity_type_id: int, *, after_id: int, upto: int
) -> set[int]:
    """Live, readable ids in `(after_id, upto]`."""
    if upto <= after_id:
        return set()
    rows = await session.execute(
        select(CrmItem.id).where(
            CrmItem.entity_type_id == entity_type_id,
            CrmItem.id > after_id,
            CrmItem.id <= upto,
            CrmItem.deleted_at.is_(None),
            CrmItem.unreadable_since.is_(None),
        )
    )
    return {int(value) for value in rows.scalars()}


async def store_marks(
    fence: Fence,
    entity_type_id: int,
    *,
    local_only: set[int],
    remote_only: set[int],
    now: dt.datetime,
    lanes: Sequence[Lane],
) -> None:
    """Queue what this batch found and move the lane, behind the fence."""
    async with tenant_txn(fence.portal_id) as session:
        if local_only:
            await crm_dirty.mark(
                session,
                fence.portal_id,
                entity_type_id,
                local_only,
                reasons=crm_dirty.REASON_RECONCILE_LOCAL,
                now=now,
            )
        if remote_only:
            await crm_dirty.mark(
                session,
                fence.portal_id,
                entity_type_id,
                remote_only,
                reasons=crm_dirty.REASON_RECONCILE_REMOTE,
                now=now,
            )
        if lanes:
            await store_lanes(session, fence.portal_id, lanes)
        await fenced_update(session, fence, {})
