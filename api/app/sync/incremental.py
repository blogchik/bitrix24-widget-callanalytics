"""§5.4 - the forward cursor: probe, then pack.

Every `SYNC_INTERVAL_SEC` (and during the backfill too) this asks the portal for
everything above `portal_sync.high_id`, ordered `ID ASC`. Two things make it different
from "issue 20 pages and hope":

* **Probe first.** ONE command (`FILTER {">ID": high_id}, start=0`) is issued, and its
  `total` / `next` decide how many further pages are packed into the following batch
  (`min(ceil(total/50) - 1, batch_pages)`). A quiet portal - which is most portals, most
  of the time - therefore costs one command instead of twenty, and the 19 empty commands
  it would otherwise send do not burn operating time that is shared with every other app
  on that account (§5.6, research note (e): the batch is one request for the intensity
  bucket but each sub-command still counts towards `operating`).
* **The cursor advances only across the error-free prefix** (§5.2). Rows from commands
  after the first failure are upserted and explicitly do NOT move `high_id`, so the failed
  page is re-read next visit rather than becoming a permanent 50-row hole between the two
  cursors.

This module also owns `portal_sync.rescan_from_id` (§5.7): the persisted lower bound of
the trailing rescan window, advanced monotonically to the `high_id` that was already known
~`RESCAN_WINDOW_HOURS` ago. It is a persisted column and not `min(bx_id)` over a date
window on purpose - the design review's "[MINOR] rescan lower bound derivation" finding is
that a portal with no calls over a weekend derives NULL, Bitrix24 ignores `{">=ID": null}`,
and the hourly rescan turns into a full-history re-read every hour until Monday.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError
from app.bitrix.statistic import PAGE_SIZE
from app.config import settings
from app.db.models import Call
from app.db.session import tenant_txn
from app.logging import get_logger
from app.sync.fetch import FetchOutcome, fetch_pages
from app.sync.head_fetch import (
    ORDER_ASC,
    SORT_FIELD,
    Pacer,
    block_on_filter_violation,
    clamp_pages,
    commit_progress,
    now,
    page_starts,
    prefix_bx_ids,
    raise_if_token_expired,
    should_continue,
)
from app.sync.lease import Fence, fenced_update, heartbeat

__all__ = ["MAX_LOOPS_PER_VISIT", "IncrementalOutcome", "run_incremental"]

log = get_logger(__name__)

#: §5.4: when the last successful command still reported `next`, repeat immediately -
#: but never more than this many times in one visit. The bound is what keeps a portal
#: that is producing rows faster than we read them from monopolising a worker slot, and
#: it is a hard stop against any "still has next" answer that never actually advances.
MAX_LOOPS_PER_VISIT: Final[int] = 10


@dataclass(frozen=True)
class IncrementalOutcome:
    """What one incremental visit imported (§5.4)."""

    #: The cursor as persisted at the end of the visit (unchanged if nothing was read).
    high_id: int
    #: The persisted rescan lower bound after the §5.7 ageing step, if it moved.
    rescan_from_id: int | None
    rows_upserted: int
    quarantined: int
    rejected: int
    requests: int
    loops: int
    errors: tuple[BitrixError, ...]
    #: §5.4 filter guard fired; the portal is parked and the visit must end now.
    blocked: bool
    #: §5.6 pacing hook ended the visit early.
    stopped: bool
    #: The portal still reported `next` when the loop budget ran out - the runner should
    #: come back promptly rather than wait a full `SYNC_INTERVAL_SEC`.
    more: bool


def _pages_to_pack(outcome: FetchOutcome, limit: int) -> int:
    """§5.4's sizing rule: `min(ceil(total/50) - 1, batch_pages)`, never negative.

    Zero is a legitimate answer even when the probe reported `next` (a `total` that lags
    by a page, an on-premise build that omits it): the visit then simply loops and probes
    again from the advanced cursor, which costs one command and cannot spin, because the
    loop below refuses to continue unless the cursor actually moved.
    """
    if not outcome.has_next:
        return 0
    total = outcome.total
    if total is None:
        # No denominator: read one batch and let the next probe re-measure.
        return limit
    return max(0, min(math.ceil(total / PAGE_SIZE) - 1, limit))


async def _advance(
    fence: Fence, outcome: FetchOutcome, *, cursor: int
) -> tuple[int, int, int]:
    """Commit one fetch's rows and the cursor they justify, in ONE transaction (§5.5).

    Returns `(new_cursor, rows_upserted, quarantined)`. The new cursor is the maximum id
    of the **error-free prefix** only; rows that came back after the first failed command
    are committed separately, without a cursor, exactly as §5.2 requires.
    """
    ids = prefix_bx_ids(outcome)
    highest = max(ids) if ids else cursor
    rows_upserted = 0
    quarantined = 0

    if outcome.rows or outcome.rejected:
        values: dict[str, Any] = {"last_incremental_at": now()}
        if highest > cursor:
            values["high_id"] = highest
        added, quarantined_now = await commit_progress(
            fence, outcome.rows, cursor_values=values, rejected=outcome.rejected
        )
        rows_upserted += added
        quarantined += quarantined_now
    if outcome.extra_rows:
        # Real data from commands after the first error: safe to store, must not move a
        # cursor - the failed page in between has not been read yet.
        added, quarantined_now = await commit_progress(fence, outcome.extra_rows)
        rows_upserted += added
        quarantined += quarantined_now
    return max(highest, cursor), rows_upserted, quarantined


async def _age_rescan_bound(
    fence: Fence, *, rescan_from_id: int | None, window_hours: int
) -> int | None:
    """Advance `portal_sync.rescan_from_id` to the `high_id` known ~`window_hours` ago.

    The definition is read literally off the data we already store: the greatest `bx_id`
    whose row we had **already seen** before the cutoff (`calls.first_seen_at`). That is
    exactly "the `high_id` observed ~72 h ago", it is monotone (the cutoff only moves
    forward), and the scan is bounded by the rows inserted since the current bound - the
    `bx_id > :bound` predicate keeps it on the `(portal_id, bx_id)` unique index and the
    backfill's rows, which sit far below the bound, are never touched.

    Returns the new bound when it moved, else None. Runs in the same transaction as the
    fenced `last_incremental_at` write so the read and the decision cannot straddle a lost
    lease.
    """
    cutoff = now() - dt.timedelta(hours=max(1, int(window_hours)))
    async with tenant_txn(fence.portal_id) as session:
        query = select(func.max(Call.bx_id)).where(
            Call.portal_id == fence.portal_id, Call.first_seen_at <= cutoff
        )
        if rescan_from_id is not None:
            query = query.where(Call.bx_id > rescan_from_id)
        candidate = (await session.execute(query)).scalar()

        values: dict[str, Any] = {"last_incremental_at": now()}
        moved: int | None = None
        if candidate is not None and (rescan_from_id is None or candidate > rescan_from_id):
            # Monotone by construction; the guard is kept because a NULL-derived or
            # backwards bound is what turns the hourly rescan into a full-history read.
            values["rescan_from_id"] = int(candidate)
            moved = int(candidate)
        await fenced_update(session, fence, values)
    return moved


async def run_incremental(
    fence: Fence,
    client: BitrixClient,
    *,
    high_id: int,
    batch_pages: int,
    rescan_from_id: int | None = None,
    window_hours: int | None = None,
    max_loops: int = MAX_LOOPS_PER_VISIT,
    pace: Pacer | None = None,
) -> IncrementalOutcome:
    """Walk `FILTER {">ID": high_id}` forward until the portal has nothing newer (§5.4).

    `high_id`, `batch_pages` and `rescan_from_id` are passed in rather than read here: the
    runner already holds the `portal_sync` row for its due-ness decisions, and one reader
    per visit keeps the cursor arithmetic in this module honest about what it was given.

    `ExpiredToken` and `FenceLost` propagate (§5.8 must refresh; a lost fence means a newer
    runner owns this portal). Every other Bitrix24 failure is reported in the outcome.
    """
    limit = clamp_pages(batch_pages)
    cursor = int(high_id)
    requests = 0
    loops = 0
    rows_upserted = 0
    quarantined = 0
    rejected = 0
    errors: list[BitrixError] = []
    blocked = False
    stopped = False
    more = False

    for _ in range(max(1, max_loops)):
        loops += 1
        base = cursor  # the filter this whole iteration's offsets are relative to

        probe = await fetch_pages(
            client,
            filter={">ID": base},
            sort=SORT_FIELD,
            order=ORDER_ASC,
            starts=(0,),
            # No `guard=`: `fetch_pages` derives the `>ID` assertion from the filter it
            # was given (§5.4) - the check that turns an ignored operator into a parked
            # portal instead of a hot loop - and a duplicate here could only diverge.
        )
        requests += 1
        raise_if_token_expired(probe.errors)
        if probe.filter_violation is not None:
            await block_on_filter_violation(
                fence, violation=probe.filter_violation, step="incremental"
            )
            blocked = True
            break

        rejected += len(probe.rejected)
        cursor, added, quarantined_now = await _advance(fence, probe, cursor=base)
        rows_upserted += added
        quarantined += quarantined_now

        if probe.errors:
            # The single probe command failed: nothing further can be sized from it, and
            # retrying inside the visit would be a hot loop. Next visit re-reads it.
            errors.extend(probe.errors)
            break
        if not probe.rows and not probe.rejected:
            break  # nothing newer than `high_id`

        pack = _pages_to_pack(probe, limit)
        last = probe
        if pack:
            if not await should_continue(pace, probe.time_block):
                stopped = True
                break
            batch = await fetch_pages(
                client,
                filter={">ID": base},
                sort=SORT_FIELD,
                order=ORDER_ASC,
                # Offsets are relative to the probe's selection, so page 0 is skipped.
                # ASC ordering means rows created during the visit append at the END of
                # the selection and cannot shift these offsets.
                starts=page_starts(pack, first=PAGE_SIZE),
            )
            requests += 1
            raise_if_token_expired(batch.errors)
            if batch.filter_violation is not None:
                await block_on_filter_violation(
                    fence, violation=batch.filter_violation, step="incremental"
                )
                blocked = True
                break
            rejected += len(batch.rejected)
            cursor, added, quarantined_now = await _advance(fence, batch, cursor=cursor)
            rows_upserted += added
            quarantined += quarantined_now
            last = batch
            if batch.errors:
                errors.extend(batch.errors)
                break

        await heartbeat(fence)

        if cursor <= base:
            # The selection said "there is more" but the cursor did not move: without this
            # stop the loop would re-issue the identical request until MAX_LOOPS_PER_VISIT,
            # and a runner that reschedules on `more` would keep doing it forever.
            log.warning(
                "incremental: no forward progress, ending visit",
                extra={"portal_id": fence.portal_id, "high_id": cursor},
            )
            break
        if not last.has_next:
            break
        if not await should_continue(pace, last.time_block):
            stopped = True
            break
    else:
        more = True  # loop budget exhausted while the portal still had pages

    moved: int | None = None
    if not blocked:
        moved = await _age_rescan_bound(
            fence,
            rescan_from_id=rescan_from_id,
            window_hours=window_hours if window_hours is not None else settings.rescan_window_hours,
        )

    if rows_upserted or errors:
        log.info(
            "incremental visit",
            extra={
                "portal_id": fence.portal_id,
                "high_id": cursor,
                "rows": rows_upserted,
                "requests": requests,
                "errors": len(errors),
            },
        )
    return IncrementalOutcome(
        high_id=cursor,
        rescan_from_id=moved if moved is not None else rescan_from_id,
        rows_upserted=rows_upserted,
        quarantined=quarantined,
        rejected=rejected,
        requests=requests,
        loops=loops,
        errors=tuple(errors),
        blocked=blocked,
        stopped=stopped,
        more=more,
    )
