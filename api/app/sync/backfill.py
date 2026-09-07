"""§5.3 - the backward cursor: import the history below `low_id`, resumably.

While `backfill_status='running'` each visit issues batches of `batch_pages` commands
`FILTER {"<ID": low_id}, SORT=ID, ORDER=DESC, start = k*50` - newest history first, so the
months a moderator is most likely to look at arrive first. After every batch the rows and
the new `low_id` are committed **together** (§5.5), which is what makes a crash cost at
most one re-upserted batch instead of an unknown hole.

Three rules carry the safety of this module:

* **Contiguous prefix** (§5.2). `low_id` may only move across the longest run of commands
  with no error. Rows from later commands are still upserted - they are real data and the
  upsert is idempotent - but they must not move the cursor, because the failed page between
  them has not been read: `backfill` continues strictly below `low_id` and `incremental`
  strictly above `high_id`, so a page skipped here is a permanent 50-row hole.
* **Forward progress or stop.** Every batch must produce a strictly smaller `low_id`. If it
  does not, the visit ends: repeating the same request is a hot loop against an API shared
  by every tenant, and §5.1 has no place where a retry runs without a `next_run_at`.
* **Yield after `BACKFILL_BATCHES_PER_VISIT` batches** so one 500k-row portal cannot starve
  the other three worker slots (§5.3). The runner sees `more=True` and comes back in ~2 s.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError
from app.config import settings
from app.logging import get_logger
from app.sync.fetch import fetch_pages
from app.sync.head_fetch import (
    ORDER_DESC,
    SORT_FIELD,
    Pacer,
    block_on_filter_violation,
    clamp_pages,
    commit_cursor,
    commit_progress,
    now,
    page_starts,
    prefix_bx_ids,
    raise_if_token_expired,
    should_continue,
)
from app.sync.lease import Fence, heartbeat

__all__ = ["BackfillOutcome", "run_backfill"]

log = get_logger(__name__)


@dataclass(frozen=True)
class BackfillOutcome:
    """What one backfill visit imported (§5.3)."""

    #: The backward cursor as persisted at the end of the visit.
    low_id: int | None
    #: `'running'` while there is history left, `'done'` once a fetch found none.
    backfill_status: str
    #: `portal_sync.backfill_done` as persisted - the numerator of the UI progress bar.
    backfill_done: int
    batches: int
    rows_upserted: int
    quarantined: int
    rejected: int
    requests: int
    errors: tuple[BitrixError, ...]
    #: §5.4 filter guard fired; the portal is parked and the visit must end now.
    blocked: bool
    #: §5.6 pacing hook ended the visit early.
    stopped: bool
    #: History remains and the runner should come back promptly (§5.9: `now() + 2 s`).
    more: bool


async def run_backfill(
    fence: Fence,
    client: BitrixClient,
    *,
    low_id: int | None,
    batch_pages: int,
    backfill_done: int = 0,
    batches: int | None = None,
    pace: Pacer | None = None,
) -> BackfillOutcome:
    """Import history below `low_id` for at most `batches` batches (§5.3).

    `low_id is None` means `head_fetch` has not run yet; the visit does nothing rather
    than guess a starting point, because `FILTER {"<ID": null}` is ignored by Bitrix24 and
    would re-read the entire history on every visit.

    `ExpiredToken` and `FenceLost` propagate (§5.8 refresh; a newer runner owns the
    portal). Every other Bitrix24 failure is reported in the outcome for §5.6 to weigh.
    """
    limit = clamp_pages(batch_pages)
    budget = batches if batches is not None else settings.backfill_batches_per_visit
    cursor = None if low_id is None else int(low_id)
    imported = int(backfill_done)
    status = "running"
    used = 0
    requests = 0
    rows_upserted = 0
    quarantined = 0
    rejected = 0
    errors: list[BitrixError] = []
    blocked = False
    stopped = False
    more = False

    if cursor is None:
        log.warning(
            "backfill asked to run without a low_id; head_fetch has not completed",
            extra={"portal_id": fence.portal_id},
        )
        return BackfillOutcome(
            low_id=None, backfill_status=status, backfill_done=imported, batches=0,
            rows_upserted=0, quarantined=0, rejected=0, requests=0, errors=(),
            blocked=False, stopped=False, more=False,
        )

    for _ in range(max(1, int(budget))):
        used += 1
        floor = cursor
        outcome = await fetch_pages(
            client,
            filter={"<ID": floor},
            sort=SORT_FIELD,
            order=ORDER_DESC,
            starts=page_starts(limit),
            # No `guard=`: `fetch_pages` derives the `<ID` assertion from the filter it
            # was given (§5.4), and a hand-written duplicate could only ever diverge.
        )
        requests += 1
        raise_if_token_expired(outcome.errors)
        if outcome.filter_violation is not None:
            await block_on_filter_violation(
                fence, violation=outcome.filter_violation, step="backfill"
            )
            blocked = True
            break

        rejected += len(outcome.rejected)
        ids = prefix_bx_ids(outcome)

        if not ids:
            if outcome.prefix_len == 0:
                # The very first command failed: nothing is known about this page, so the
                # cursor stays put and the next visit re-reads it.
                errors.extend(outcome.errors)
                break
            # An error-free page with no rows at all: the history below `low_id` is
            # exhausted. This is the ONLY way the backfill finishes (§5.3).
            status = "done"
            await commit_cursor(
                fence, {"backfill_status": "done", "backfill_finished_at": now()}
            )
            log.info(
                "backfill complete",
                extra={"portal_id": fence.portal_id, "low_id": cursor, "rows": imported},
            )
            break

        lowest = min(ids)
        if lowest >= floor:
            # `<ID` was honoured (the guard passed) yet nothing below the cursor came back.
            # Advancing would skip rows and repeating would spin, so the visit ends here.
            log.warning(
                "backfill: no backward progress, ending visit",
                extra={"portal_id": fence.portal_id, "low_id": floor},
            )
            errors.extend(outcome.errors)
            break

        imported += len(ids)
        # No `next` on the last prefix command means this selection is exhausted: there is
        # provably nothing below `lowest`, so the backfill is finished without spending a
        # further request on an empty page.
        exhausted = not outcome.has_next and not outcome.errors
        cursor_values: dict[str, Any] = {"low_id": lowest, "backfill_done": imported}
        if exhausted:
            status = "done"
            cursor_values["backfill_status"] = "done"
            cursor_values["backfill_finished_at"] = now()

        added, quarantined_now = await commit_progress(
            fence, outcome.rows, cursor_values=cursor_values, rejected=outcome.rejected
        )
        rows_upserted += added
        quarantined += quarantined_now
        if outcome.extra_rows:
            # Rows from commands after the first failure: stored, never cursor-moving.
            added, quarantined_now = await commit_progress(fence, outcome.extra_rows)
            rows_upserted += added
            quarantined += quarantined_now
        cursor = lowest

        await heartbeat(fence)

        if outcome.errors:
            # The prefix advanced as far as it legally could; the failed page is re-read
            # next visit rather than immediately, so a failing page cannot become a loop.
            errors.extend(outcome.errors)
            break
        if exhausted:
            log.info(
                "backfill complete",
                extra={"portal_id": fence.portal_id, "low_id": cursor, "rows": imported},
            )
            break
        if not await should_continue(pace, outcome.time_block):
            stopped = True
            break
    else:
        more = status == "running"  # yielded on the per-visit batch budget (§5.3)

    if used:
        log.info(
            "backfill visit",
            extra={
                "portal_id": fence.portal_id,
                "batches": used,
                "rows": rows_upserted,
                "low_id": cursor,
                "status": status,
            },
        )
    return BackfillOutcome(
        low_id=cursor,
        backfill_status=status,
        backfill_done=imported,
        batches=used,
        rows_upserted=rows_upserted,
        quarantined=quarantined,
        rejected=rejected,
        requests=requests,
        errors=tuple(errors),
        blocked=blocked,
        stopped=stopped,
        more=more,
    )
