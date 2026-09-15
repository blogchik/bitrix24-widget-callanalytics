"""One pass over the dirty queue: re-read, confirm what is missing, and only then delete (§5.12).

The pass is three reads and two guards:

1. **`@id` re-read** of up to 2 500 queued ids. A returned row is stored; an id a clean command
   did not return is only a candidate - a list omits a deleted record and an unreadable one
   alike (research block (i) C2).
2. **`crm.item.get`** for every candidate, 50 per batch. NOT_FOUND is a deletion;
   ACCESS_DENIED is a narrowed credential; anything else waits for the next pass.
3. **The guards** before a tombstone, both from decision 29. The installer's administrator
   rights must have been proven within 26 hours - a demoted installer's `crm.item.get` answers
   NOT_FOUND for records it merely cannot see on some builds. And one pass may not delete more
   than 50 records or 2 % of the mirror, whichever is larger; past that the candidates are held
   for an hour and a `crm_delete_held` event says why, because a mass deletion is far more often
   a permissions change than a customer emptying their CRM.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.bitrix.client import BitrixClient
from app.bitrix.crm_items import MAX_IDS_PER_COMMAND, ItemRow, MirrorDialect
from app.bitrix.errors import BitrixError
from app.db.session import tenant_txn
from app.services import crm_dirty
from app.services.crm_dirty import DirtyId
from app.services.portals import record_event
from app.sync.crm_deletes import live_count, mark_unreadable, tombstone
from app.sync.crm_fetch import confirm_absent, fetch_by_ids
from app.sync.crm_lanes import Lane, store_lanes
from app.sync.lease import Fence, fenced_update

__all__ = [
    "ADMIN_FRESHNESS",
    "DELETE_FLOOR",
    "DELETE_RATIO",
    "MAX_IDS_PER_PASS",
    "RefreshOutcome",
    "refresh",
    "store_refresh",
]

#: 50 `@id` commands of 50: one batch.
MAX_IDS_PER_PASS: Final[int] = 2_500

#: Decision 29: the installer's administrator rights, proven this recently, or no tombstone.
ADMIN_FRESHNESS: Final[dt.timedelta] = dt.timedelta(hours=26)

#: One pass deletes at most max(DELETE_FLOOR, DELETE_RATIO x live records) before holding.
DELETE_FLOOR: Final[int] = 50
DELETE_RATIO: Final[float] = 0.02

_HOLD: Final[dt.timedelta] = dt.timedelta(hours=1)

Pace = Callable[[dict[str, Any] | None], Awaitable[bool]]


@dataclass
class RefreshOutcome:
    """What one pass learned, sorted by what each id's answer lets the mirror do."""

    read_at: dt.datetime
    rows: list[ItemRow] = field(default_factory=list)
    rejected: list[tuple[int | None, str]] = field(default_factory=list)
    #: Returned by the re-read: consumed with the upsert of their rows.
    present: list[DirtyId] = field(default_factory=list)
    #: NOT_FOUND from `crm.item.get`.
    deleted: list[DirtyId] = field(default_factory=list)
    #: ACCESS_DENIED from `crm.item.get`.
    unreadable: list[DirtyId] = field(default_factory=list)
    #: Nothing conclusive: deferred with back-off.
    retry: list[DirtyId] = field(default_factory=list)
    errors: list[BitrixError] = field(default_factory=list)
    batches: int = 0
    time_block: dict[str, Any] | None = None


async def refresh(
    client: BitrixClient,
    dialect: MirrorDialect,
    items: Sequence[DirtyId],
    *,
    utm_max_chars: int,
    pace: Pace,
    max_batches: int,
    now: dt.datetime,
) -> RefreshOutcome:
    """Re-read `items` and confirm the absent ones, within `max_batches` requests."""
    out = RefreshOutcome(read_at=now)
    by_id = {item.id: item for item in items[:MAX_IDS_PER_PASS]}
    if not by_id or max_batches <= 0:
        return out

    fetched = await fetch_by_ids(client, dialect, list(by_id), utm_max_chars=utm_max_chars)
    out.batches = 1
    out.time_block = fetched.time_block
    out.rows = list(fetched.rows)
    out.rejected = list(fetched.rejected)
    out.errors.extend(fetched.errors)

    # A row the parser refused still proves the record exists; it is consumed, and the
    # refusal is audited by the upsert, rather than re-read forever.
    returned = {row.id for row in fetched.rows} | {
        item_id for item_id, _ in fetched.rejected if item_id is not None
    }
    out.present = [by_id[item_id] for item_id in sorted(returned) if item_id in by_id]
    out.retry = [by_id[item_id] for item_id in sorted(fetched.unresolved) if item_id in by_id]

    missing = sorted(item_id for item_id in fetched.missing if item_id not in returned)
    for start in range(0, len(missing), MAX_IDS_PER_COMMAND):
        chunk = missing[start : start + MAX_IDS_PER_COMMAND]
        if out.batches >= max_batches or not await pace(out.time_block):
            out.retry.extend(by_id[item_id] for item_id in missing[start:])
            break
        confirmed = await confirm_absent(client, dialect.entity_type_id, chunk)
        out.batches += 1
        out.time_block = confirmed.time_block
        out.deleted.extend(by_id[item_id] for item_id in sorted(confirmed.not_found))
        out.unreadable.extend(by_id[item_id] for item_id in sorted(confirmed.access_denied))
        # `present` here means the list omitted what `crm.item.get` still returns: nothing to
        # conclude, so it is read again next pass rather than guessed at.
        out.retry.extend(
            by_id[item_id] for item_id in sorted(confirmed.present | set(confirmed.unresolved))
        )
        out.errors.extend(confirmed.unresolved.values())
    return out


async def store_refresh(
    fence: Fence,
    entity_type_id: int,
    outcome: RefreshOutcome,
    *,
    allow_deletes: bool,
    now: dt.datetime,
    lanes: Sequence[Lane] = (),
) -> bool:
    """Tombstone behind the guards, mark the unreadable, defer the rest. True when deletes were held.

    The re-read rows are not stored here: `crm_upsert.upsert_items(consume=outcome.present)` does
    that, in its own transaction, before this runs.
    """
    portal_id = fence.portal_id
    held = False
    async with tenant_txn(portal_id) as session:
        if outcome.deleted:
            live = await live_count(session, portal_id, entity_type_id)
            ceiling = max(DELETE_FLOOR, int(DELETE_RATIO * live))
            if not allow_deletes or len(outcome.deleted) > ceiling:
                held = True
                await crm_dirty.hold(session, portal_id, outcome.deleted, until=now + _HOLD)
                await record_event(
                    session,
                    portal_id,
                    "crm_delete_held",
                    details={
                        "entity_type_id": entity_type_id,
                        "confirmed": len(outcome.deleted),
                        "ceiling": ceiling,
                        "admin_verified": allow_deletes,
                    },
                )
            else:
                await tombstone(
                    session,
                    portal_id,
                    entity_type_id,
                    [item.id for item in outcome.deleted],
                    read_at=outcome.read_at,
                )
                await crm_dirty.consume(session, portal_id, outcome.deleted)
        if outcome.unreadable:
            await mark_unreadable(
                session, portal_id, entity_type_id, [item.id for item in outcome.unreadable], now=now
            )
            await crm_dirty.consume(session, portal_id, outcome.unreadable)
        if outcome.retry:
            await crm_dirty.defer(session, portal_id, outcome.retry, now=now)
        if lanes:
            await store_lanes(session, portal_id, lanes)
        await fenced_update(session, fence, {})
    return held
