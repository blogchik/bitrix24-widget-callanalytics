"""§5.7 - the three late-update mechanisms.

A statistics row is not immutable. The recording is attached when the file finishes
uploading, the vote and the comment are written by a human minutes or days later, and a
transcript can appear later still. The forward cursor never looks back, so without this
module a call would be cached exactly as it looked the second it ended.

1. **ID-window rescan** (hourly). Re-read `FILTER {">=ID": rescan_from_id}` ascending and
   upsert. The lower bound is the **persisted** `portal_sync.rescan_from_id`
   (`incremental` ages it forward), never `min(bx_id)` of a date window: the design
   review's "[MINOR] rescan lower bound derivation" finding is that a portal with no calls
   over a weekend derives NULL, Bitrix24 ignores `{">=ID": null}` and the hourly rescan
   pages through the entire history every hour until Monday. Skipped outright when the
   bound has caught up with `high_id`.
2. **Recording recheck** (daily). The trailing window only covers 72 h, and real portals
   attach recordings later than that. Candidates come from the `calls_portal_recheck_idx`
   partial index - no recording, non-zero duration, fewer than two rechecks - between 72 h
   and 30 days old, and are re-read by id. `record_recheck_count` is incremented **before**
   the re-read, so a crash costs one budget unit rather than making a call that never had a
   recording cost a request forever.
3. **On-demand refresh**. `POST /api/v1/calls/{id}/refresh` sets `refresh_requested` when
   playback returned 403/404; those rows are re-read first on the next visit and the upsert
   clears the flag (§5.5).

Mechanisms 2 and 3 read by **id list**, which is a different shape from the offset paging
`sync/fetch.py` models: one command per <= 50 ids, no `start=` walk, and - decisively - no
cursor to advance, so §5.2's contiguous-prefix rule has nothing to protect. They therefore
use `client.batch` with the shared `statistic.parse_rows` directly. None of the three ever
writes `high_id` or `low_id`: they re-read rows the cursors have already passed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select, update

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError
from app.bitrix.statistic import PAGE_SIZE, STATISTIC_METHOD, parse_rows, statistic_params
from app.db.models import Call
from app.db.session import tenant_txn
from app.logging import get_logger
from app.sync.fetch import fetch_pages
from app.sync.head_fetch import (
    ORDER_ASC,
    SORT_FIELD,
    Pacer,
    block_on_filter_violation,
    clamp_pages,
    commit_cursor,
    commit_progress,
    dedupe_rows,
    now,
    page_starts,
    prefix_bx_ids,
    raise_if_token_expired,
    should_continue,
)
from app.sync.lease import Fence, fenced_update, heartbeat

__all__ = [
    "MAX_RESCAN_BATCHES",
    "RECHECK_MAX_AGE_DAYS",
    "RECHECK_MAX_COUNT",
    "RECHECK_MIN_AGE_HOURS",
    "RescanOutcome",
    "run_id_window_rescan",
    "run_record_recheck",
    "run_refresh_requested",
]

log = get_logger(__name__)

#: The window rescan is best-effort and must never become the visit. 10 batches of 20
#: commands is 10 000 rows an hour; a portal whose 72 h window is bigger than that has the
#: pass truncated at the newest end and CONTINUES from there on the next hourly run
#: (`_RESUME_FROM`), while the recheck budget covers anything older.
MAX_RESCAN_BATCHES: Final[int] = 10

#: Where a truncated pass stopped, per portal: the id its next pass starts from (§5.7).
#:
#: Without it the walk restarts at `rescan_from_id` every hour, so a portal with more rows
#: in its window than one pass can read re-reads the same oldest 10 000 ids for ever and
#: never reaches the newest end - where every late recording, vote, comment and transcript
#: this pass exists for actually is. The continuation cannot be stored in `rescan_from_id`:
#: §5.7 defines that as the `high_id` of ~72 h ago, `incremental._age_rescan_bound` owns it,
#: and advancing it here would shrink the window so younger calls stopped being re-read at
#: all. `portal_sync` has no column for a pass position, so the hint lives in the worker
#: process; losing it on a restart costs one pass that starts at the floor again and
#: nothing more - this pass moves no cursor and every row it reads the cursors already
#: passed. Cleared as soon as a pass reaches the top of the window, so the next one sweeps
#: from the floor again.
_RESUME_FROM: Final[dict[int, int]] = {}

#: §5.7 item 2, mirroring `calls_portal_recheck_idx` exactly. Younger than 72 h is already
#: covered by the window rescan; older than 30 days is accepted as never-recorded.
RECHECK_MIN_AGE_HOURS: Final[int] = 72
RECHECK_MAX_AGE_DAYS: Final[int] = 30
RECHECK_MAX_COUNT: Final[int] = 2


@dataclass(frozen=True)
class RescanOutcome:
    """What one late-update pass re-read (§5.7)."""

    #: False when the pass had nothing to do (no candidates, or the window is empty).
    ran: bool
    ids_requested: int
    rows_upserted: int
    quarantined: int
    rejected: int
    requests: int
    batches: int
    errors: tuple[BitrixError, ...]
    #: §5.4 filter guard fired; the portal is parked and the visit must end now.
    blocked: bool
    #: §5.6 pacing hook ended the pass early.
    stopped: bool


_IDLE = RescanOutcome(
    ran=False, ids_requested=0, rows_upserted=0, quarantined=0, rejected=0,
    requests=0, batches=0, errors=(), blocked=False, stopped=False,
)


def _chunks(values: Sequence[int], size: int) -> list[list[int]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


async def _read_by_ids(
    client: BitrixClient, ids: Sequence[int], *, commands: int
) -> tuple[list[dict[str, Any]], list[tuple[int | None, str]], list[BitrixError], dict[str, Any] | None]:
    """Re-read up to `commands` x 50 ids in ONE batch (§5.7 items 2 and 3).

    `FILTER {"ID": [...]}` is the documented list form (research note: the docs' own
    example is `{'ID':[1,7]}`), 50 ids per command so no command ever needs a second page.
    Per-command failures are collected rather than raised: there is no cursor to hold back,
    so a failed chunk simply means those ids are re-read on a later visit.
    """
    if not ids:
        return [], [], [], None
    batch = await client.batch(
        [
            (
                f"r{index}",
                STATISTIC_METHOD,
                statistic_params(filter={"ID": chunk}, sort=SORT_FIELD, order=ORDER_ASC),
            )
            for index, chunk in enumerate(_chunks(ids, PAGE_SIZE)[:commands])
        ],
        halt=0,
    )
    rows: list[dict[str, Any]] = []
    rejected: list[tuple[int | None, str]] = []
    errors: list[BitrixError] = []
    for command in batch.commands:
        if command.error is not None:
            errors.append(command.error)
            continue
        parsed = parse_rows(command.result or [])
        rows.extend(parsed.rows)
        rejected.extend(parsed.rejected)
    raise_if_token_expired(errors)
    return dedupe_rows(rows), rejected, errors, batch.time


async def _select_ids(fence: Fence, query: Any) -> list[int]:
    """Run one candidate query for this portal under tenant context (§3).

    `calls` carries FORCED row-level security bound to `app.portal_id`; issued from a
    control transaction this query returns zero rows *silently*, which would look exactly
    like "nothing to re-read" forever.
    """
    async with tenant_txn(fence.portal_id) as session:
        return [int(value) for value in (await session.execute(query)).scalars().all()]


# --------------------------------------------------------------------------- 3. on demand


async def run_refresh_requested(
    fence: Fence, client: BitrixClient, *, batch_pages: int, pace: Pacer | None = None
) -> RescanOutcome:
    """Re-read the rows the SPA flagged after a failed playback (§5.7 item 3).

    Runs first in the visit (§5.9) because it is the only mechanism a user is waiting on.
    The flag is cleared by the upsert (§5.5), so an id that Bitrix24 no longer returns -
    the call was deleted on the portal - stays flagged and is re-read once per visit; that
    is one command, bounded, and it is logged so support can see it.
    """
    commands = clamp_pages(batch_pages)
    ids = await _select_ids(
        fence,
        select(Call.bx_id)
        .where(Call.portal_id == fence.portal_id, Call.refresh_requested.is_(True))
        .order_by(Call.bx_id.desc())
        .limit(commands * PAGE_SIZE),
    )
    if not ids:
        return _IDLE

    rows, rejected, errors, time_block = await _read_by_ids(client, ids, commands=commands)
    upserted, quarantined = await commit_progress(fence, rows, rejected=rejected)
    missing = len(ids) - len(rows)
    if missing > 0:
        log.info(
            "refresh_requested: some ids no longer exist on the portal",
            extra={"portal_id": fence.portal_id, "requested": len(ids), "missing": missing},
        )
    stopped = not await should_continue(pace, time_block)
    return RescanOutcome(
        ran=True, ids_requested=len(ids), rows_upserted=upserted, quarantined=quarantined,
        rejected=len(rejected), requests=1, batches=1, errors=tuple(errors),
        blocked=False, stopped=stopped,
    )


# ------------------------------------------------------------------------ 1. ID window


async def run_id_window_rescan(
    fence: Fence,
    client: BitrixClient,
    *,
    rescan_from_id: int | None,
    high_id: int,
    batch_pages: int,
    max_batches: int = MAX_RESCAN_BATCHES,
    pace: Pacer | None = None,
) -> RescanOutcome:
    """Re-read `FILTER {">=ID": rescan_from_id}` ascending and upsert (§5.7 item 1).

    Skipped when the bound is unknown (head_fetch has not run) or has caught up with
    `high_id` - there is then nothing between them, and sending `{">=ID": null}` would be
    the full-history re-read this bound exists to prevent.

    Writes no `high_id` / `low_id`: this walks rows the cursors have already passed, and a
    cursor moved from here would skip everything the window does not cover.

    A window bigger than one pass's budget (`max_batches` x `batch_pages` x 50 rows) is
    walked across several hourly passes: the pass resumes above the last id the previous
    one re-read (`_RESUME_FROM`) and starts over at the floor once it reaches the top.
    """
    if rescan_from_id is None or int(rescan_from_id) >= int(high_id):
        return _IDLE

    floor = int(rescan_from_id)
    ceiling = int(high_id)
    resume = _RESUME_FROM.get(fence.portal_id)
    if resume is not None and not floor < resume <= ceiling:
        # The window moved out from under the hint (the floor aged past it, or a reinstall
        # reset the cursors): sweep from the persisted floor rather than from an id that
        # belonged to another window.
        del _RESUME_FROM[fence.portal_id]
        resume = None
    start_id = floor if resume is None else resume

    limit = clamp_pages(batch_pages)
    offset = 0
    # The highest id this pass may claim to have re-read: `prefix_bx_ids` applies §5.2's
    # contiguous-prefix rule, so a failed command in the middle is re-read next pass
    # instead of being stepped over, and a page whose rows were all quarantined in an
    # otherwise clean batch still advances (§5.5 - a rejected row may not pin anything).
    reached: int | None = None
    completed = False
    used = 0
    requests = 0
    rows_upserted = 0
    quarantined = 0
    rejected = 0
    errors: list[BitrixError] = []
    blocked = False
    stopped = False

    for _ in range(max(1, int(max_batches))):
        used += 1
        outcome = await fetch_pages(
            client,
            filter={">=ID": start_id},
            sort=SORT_FIELD,
            order=ORDER_ASC,
            starts=page_starts(limit, first=offset),
            # No `guard=`: `fetch_pages` derives the `>=ID` assertion from the filter it
            # was given (§5.4), and a hand-written duplicate could only ever diverge.
        )
        requests += 1
        raise_if_token_expired(outcome.errors)
        if outcome.filter_violation is not None:
            await block_on_filter_violation(
                fence, violation=outcome.filter_violation, step="rescan"
            )
            blocked = True
            break

        rejected += len(outcome.rejected)
        crossed = prefix_bx_ids(outcome)
        if crossed:
            reached = max(crossed) if reached is None else max(reached, max(crossed))
        added, quarantined_now = await commit_progress(
            fence, outcome.rows, rejected=outcome.rejected
        )
        rows_upserted += added
        quarantined += quarantined_now
        if outcome.extra_rows:
            added, quarantined_now = await commit_progress(fence, outcome.extra_rows)
            rows_upserted += added
            quarantined += quarantined_now

        await heartbeat(fence)

        if outcome.errors:
            errors.extend(outcome.errors)
            break
        if not outcome.has_next:
            completed = True
            break
        # ASC ordering means rows created during the pass append at the END of the
        # selection, so these offsets cannot drift under us between requests.
        offset += limit * PAGE_SIZE
        if not await should_continue(pace, outcome.time_block):
            stopped = True
            break

    if not blocked:
        if completed or reached is None:
            # The walk reached the top of the window (or learned nothing it may cross):
            # the next pass starts a fresh sweep at the persisted floor.
            _RESUME_FROM.pop(fence.portal_id, None)
        else:
            # Truncated by the batch budget, a per-command error or the pacer: the next
            # hourly pass continues above the last id this one actually re-read, instead
            # of spending its whole budget on the same oldest rows again (§5.7).
            _RESUME_FROM[fence.portal_id] = reached + 1
        # Stamped even when the pass was truncated: the window is re-read every
        # RESCAN_INTERVAL_SEC anyway, and not stamping would make the next visit repeat it
        # immediately - once per sync interval instead of once per hour.
        await commit_cursor(fence, {"last_rescan_at": now()})

    log.info(
        "rescan window",
        extra={
            "portal_id": fence.portal_id,
            "from_id": start_id,
            "floor_id": floor,
            "reached_id": reached,
            "completed": completed,
            "batches": used,
            "rows": rows_upserted,
        },
    )
    return RescanOutcome(
        ran=True, ids_requested=0, rows_upserted=rows_upserted, quarantined=quarantined,
        rejected=rejected, requests=requests, batches=used, errors=tuple(errors),
        blocked=blocked, stopped=stopped,
    )


# -------------------------------------------------------------------- 2. record recheck


async def run_record_recheck(
    fence: Fence, client: BitrixClient, *, batch_pages: int, pace: Pacer | None = None
) -> RescanOutcome:
    """Re-read calls that still have no recording, at most twice each (§5.7 item 2).

    The candidate predicate is written to match `calls_portal_recheck_idx` term for term so
    the daily pass is an index scan on a portal with hundreds of thousands of rows.

    `record_recheck_count` is incremented in its own fenced transaction **before** the
    re-read. The order matters: a crash between the increment and the upsert costs one
    budget unit, while the reverse order would let a crash-looping worker spend a request
    per call per visit forever on calls that were simply never recorded.
    """
    commands = clamp_pages(batch_pages)
    stamp = now()
    ids = await _select_ids(
        fence,
        select(Call.bx_id)
        .where(
            Call.portal_id == fence.portal_id,
            Call.record_file_id.is_(None),
            func.coalesce(Call.call_record_url, "") == "",
            Call.call_duration > 0,
            Call.record_recheck_count < RECHECK_MAX_COUNT,
            Call.call_start_date < stamp - dt.timedelta(hours=RECHECK_MIN_AGE_HOURS),
            Call.call_start_date > stamp - dt.timedelta(days=RECHECK_MAX_AGE_DAYS),
        )
        # Newest first: a recording attached late is far likelier on a recent call, and the
        # oldest candidates age out of the 30-day window on their own.
        .order_by(Call.call_start_date.desc())
        .limit(commands * PAGE_SIZE),
    )
    if not ids:
        await commit_cursor(fence, {"last_recheck_at": stamp})
        return _IDLE

    async with tenant_txn(fence.portal_id) as session:
        # The one `calls` write outside `sync/upsert.py` (§2), and it has to be: this
        # counter is the budget that stops the recheck from running forever, so it is
        # spent in the same transaction that records the pass. The upsert never touches
        # it - it only writes columns the parser produced.
        await session.execute(
            update(Call)
            .where(Call.portal_id == fence.portal_id, Call.bx_id.in_(ids))
            .values(record_recheck_count=Call.record_recheck_count + 1)
        )
        await fenced_update(session, fence, {"last_recheck_at": stamp})

    rows, rejected, errors, time_block = await _read_by_ids(client, ids, commands=commands)
    upserted, quarantined = await commit_progress(fence, rows, rejected=rejected)
    log.info(
        "record recheck",
        extra={
            "portal_id": fence.portal_id,
            "requested": len(ids),
            "returned": len(rows),
            "errors": len(errors),
        },
    )
    stopped = not await should_continue(pace, time_block)
    return RescanOutcome(
        ran=True, ids_requested=len(ids), rows_upserted=upserted, quarantined=quarantined,
        rejected=len(rejected), requests=1, batches=1, errors=tuple(errors),
        blocked=False, stopped=stopped,
    )
