"""§5.2 - the two-step `head_fetch` and the cursor primitives every step shares.

`head_fetch` is what turns an installed portal into a syncing one: it learns the newest
statistics `ID` (`M`), imports the newest page window around it and hands `incremental`
a forward cursor and `backfill` a backward cursor that are **disjoint by construction**
(`high_id = M`, `low_id = min(ID)` of the head window).

Why it is written as two requests that may be repeated at any time:

* Step 1 (`SORT=ID, ORDER=DESC, start=0`) is the only way to learn `M` and `total`.
* Step 2 pages the **immutable** set `ID <= M`, so offsets inside that one HTTP request
  cannot drift while new calls arrive - which is exactly what a plain `start=` walk over
  an unfiltered selection would suffer from.
* Nothing between the two steps writes `high_id`. A crash, an OOM kill or a per-command
  error leaves `backfill_status='head'`, and §5.2 runs `head_fetch` for `pending` **and**
  `head`, so the next visit simply starts again at step 1. The design review's "[MAJOR]
  head_fetch state machine resumability" finding is that a portal which persisted `head`
  and `high_id=M` but never ran step 2 is wedged forever: `head_fetch` no longer runs (not
  `pending`), `backfill` does not run (not `running`), and `incremental` only ever sees
  calls made after the install.

**Any per-command error in step 2 means "stay in `head`, retry"** (§5.2 contiguous-prefix
rule). The rows that did come back are still upserted - they are real data and the upsert
is idempotent - but no cursor is written, so the failed page is re-read next visit instead
of becoming a permanent 50-row hole that neither cursor ever revisits.

This module also owns the primitives the other cursor-writing steps (`incremental`,
`backfill`, `rescan`) import, because each of them is a rule that must have exactly ONE
definition in the sync layer:

* `commit_cursor` - the fenced, tenant-scoped cursor write for progress that carries no
  rows (§3 `sync_generation`, §5.5).
* `prefix_bx_ids` - which ids a cursor may legally cross (§5.2, §5.5).
* `raise_if_token_expired` - the one place a batch's per-command `expired_token` is turned
  back into a raise, without which §5.8's single-flight refresh never fires.
* `block_on_filter_violation` - §5.4's filter-honoured guard.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.bitrix.client import MAX_BATCH_COMMANDS, BitrixClient
from app.bitrix.errors import BitrixError, ExpiredToken
from app.bitrix.statistic import PAGE_SIZE
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger
from app.services.portals import record_event, set_token_status
from app.sync.fetch import FetchOutcome, fetch_pages
from app.sync.lease import Fence, fenced_update
from app.sync.upsert import upsert_calls

__all__ = [
    "ORDER_ASC",
    "ORDER_DESC",
    "SORT_FIELD",
    "HeadFetchOutcome",
    "Pacer",
    "block_on_filter_violation",
    "clamp_pages",
    "commit_cursor",
    "commit_progress",
    "dedupe_rows",
    "now",
    "page_starts",
    "prefix_bx_ids",
    "raise_if_token_expired",
    "run_head_fetch",
    "should_continue",
]

log = get_logger(__name__)

#: `voximplant.statistic.get` is sorted and filtered on the internal record id only
#: (research note: rows are created at finish time and `CALL_START_DATE` can be backdated,
#: so a date cursor is unsafe - §5.1 "Cursor = the statistics `ID`").
SORT_FIELD: Final[str] = "ID"
ORDER_ASC: Final[str] = "ASC"
ORDER_DESC: Final[str] = "DESC"

#: Awaited between two HTTP requests of one visit with the `time{}` block of the response
#: that just came back. It is `sync/throttle.py`'s hook (§5.6): it applies the >= 500 ms
#: pacing and returns **False** when the visit must stop now - operating-time soft limit
#: reached, `Retry-After` seen, lease nearly expired. Optional so the steps stay testable
#: without a throttle, but a runner that omits it gives up the mid-visit budget guard.
type Pacer = Callable[[dict[str, Any] | None], Awaitable[bool]]


def now() -> dt.datetime:
    """UTC timestamp for cursor columns.

    Deliberately Python-side rather than `func.now()`: these values are handed to
    `lease.fenced_update` / `upsert.upsert_calls` as plain bind parameters, and a SQL
    expression would constrain how those two modules are allowed to build their UPDATE.
    """
    return dt.datetime.now(dt.UTC)


def clamp_pages(batch_pages: int) -> int:
    """Commands per batch, inside Bitrix24's hard ceiling (§5.6, research note (e)).

    `portal_sync.batch_pages` is 1..50 by CHECK and is halved by the throttle, but a
    caller that passes a stale or hand-edited value must not turn into an HTTP 400
    `ERROR_BATCH_LENGTH_EXCEEDED` that costs a request against the shared bucket.
    """
    return max(1, min(int(batch_pages), MAX_BATCH_COMMANDS))


def page_starts(pages: int, *, first: int = 0) -> tuple[int, ...]:
    """`start=` offsets of consecutive 50-row pages (`start = (N-1) * 50`, verified)."""
    return tuple(first + index * PAGE_SIZE for index in range(max(0, pages)))


def _bx_id(row: dict[str, Any]) -> int | None:
    """The parsed row's cursor value, or None for a shape that cannot be a cursor."""
    value = row.get("bx_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated `bx_id`s, keeping the LAST occurrence (§5.2, §5.5).

    Step 1 and step 2's page 0 describe the same rows, and the two sets reach the upsert
    together. `ON CONFLICT DO UPDATE` cannot touch the same row twice in one statement
    ("command cannot affect row a second time"), and §5.5's per-chunk dedupe cannot help
    here because 500-row chunks can split the duplicate pair across two statements. The
    last occurrence wins because step 2 was read later.
    """
    unique: dict[int, dict[str, Any]] = {}
    for row in rows:
        key = _bx_id(row)
        if key is not None:
            unique[key] = row
    return list(unique.values())


def prefix_bx_ids(outcome: FetchOutcome) -> list[int]:
    """Every id the cursor may legally cross for this fetch (§5.2 + §5.5).

    Two rules meet here:

    * **Contiguous prefix** - only commands before the first error are trustworthy, so
      only `outcome.rows` (which `fetch_pages` already restricts to that prefix) counts.
    * **A rejected row must never pin a cursor** - §5.5 quarantines unparsable rows
      instead of failing the chunk, and a page whose rows all failed to parse would
      otherwise leave the cursor with nothing to move to, re-reading that page every visit
      forever. Rejected ids are therefore included, but *only* when the whole batch was
      error-free: `FetchOutcome.rejected` is flat, so with a failed command in the middle a
      rejected id might come from a command AFTER the failure, and crossing it would skip
      pages that were never read.
    """
    ids = [value for row in outcome.rows if (value := _bx_id(row)) is not None]
    if outcome.prefix_len >= outcome.command_count and not outcome.errors:
        ids.extend(bx_id for bx_id, _reason in outcome.rejected if bx_id is not None)
    return ids


def raise_if_token_expired(errors: Sequence[BitrixError]) -> None:
    """Re-raise the first `expired_token` found among per-command errors (§5.8).

    `client.batch(halt=0)` reports sub-command failures as VALUES, never as exceptions -
    which is what §5.2's prefix rule needs. But `oauth.with_portal_token` only refreshes
    when `fn` **raises** `ExpiredToken`: a step that quietly folded one into its outcome
    would spend the rest of the visit issuing requests with a dead token, and a portal
    whose stored `token_expires_at` is wrong (clock skew, a token revoked early) would
    never refresh at all. Raising costs nothing - everything committed so far stays
    committed and the single retry resumes from the advanced cursor.
    """
    for error in errors:
        if isinstance(error, ExpiredToken):
            raise error


async def should_continue(pace: Pacer | None, time_block: dict[str, Any] | None) -> bool:
    """Apply the §5.6 pacing / operating-budget hook between two requests of one visit."""
    if pace is None:
        return True
    return bool(await pace(time_block))


async def commit_cursor(fence: Fence, values: dict[str, Any]) -> None:
    """Write cursor-only progress in ONE fenced transaction (§5.5, §3).

    Used for progress that carries no rows - "this portal has no statistics at all", "the
    backfill reached the end", "the rescan ran". Progress that DOES carry rows goes
    through `upsert.upsert_calls`, which commits rows and cursor together so a crash can
    never leave a cursor ahead of the rows it claims to describe.

    `tenant_txn` even though `portal_sync` carries no RLS: every transaction in the sync
    layer is opened the same way, so no reviewer has to decide per call site whether
    tenant context was needed. `FenceLost` from `fenced_update` propagates - a newer runner
    or an uninstall took this portal over and this run must stop immediately.
    """
    async with tenant_txn(fence.portal_id) as session:
        await fenced_update(session, fence, values)


async def commit_progress(
    fence: Fence,
    rows: Sequence[dict[str, Any]],
    *,
    cursor_values: dict[str, Any] | None = None,
    rejected: Sequence[tuple[int | None, str]] = (),
) -> tuple[int, int]:
    """Commit rows, quarantine accounting and the cursor they justify together (§5.5).

    One call to `upsert.upsert_calls`, which puts all three in ONE fenced tenant
    transaction, so a crash can never leave the cursor ahead of the rows it describes.

    An EMPTY row list with a cursor is a real case, not a degenerate one: a page whose 50
    rows were all quarantined by the parser must still move the cursor over them (§5.5 -
    "a single unexpected value can never block the cursor"), and `upsert_calls` issues its
    fenced UPDATE unconditionally, so that works. The call is skipped only when there is
    nothing at all to record, to avoid an empty transaction per idle visit.
    """
    if not rows and not rejected and not cursor_values:
        return 0, 0
    result = await upsert_calls(fence, rows, cursor_values=cursor_values, rejected=rejected)
    return result.inserted_or_updated, result.quarantined


async def block_on_filter_violation(fence: Fence, *, violation: str, step: str) -> None:
    """§5.4 filter-honoured guard: park the portal instead of hot-looping.

    A build that ignores `FILTER[>ID]` answers every request with the same rows. The
    cursor then never advances while every response still looks like a success - a hot
    loop against a rate-limited API shared by every tenant. `next_run_at='infinity'` plus
    `token_status='filter_unsupported'` stops it dead and the settings page explains it.

    Written through `services/portals.py` in a control transaction and deliberately NOT
    fenced: this is a terminal safety stop, and even a runner that has just lost its lease
    is right about what it observed. An admin open that re-seeds the portal clears it.
    """
    async with control_txn() as session:
        await set_token_status(
            session,
            fence.portal_id,
            "filter_unsupported",
            block_sync=True,
            last_error_code="filter_unsupported",
            last_error_text=f"{step}: {violation}"[:2000],
        )
        await record_event(
            session,
            fence.portal_id,
            "sync_blocked",
            details={"reason": "filter_unsupported", "step": step, "detail": violation},
        )
    log.error(
        "sync blocked: the portal ignored an ID filter",
        extra={"portal_id": fence.portal_id, "step": step, "violation": violation},
    )


@dataclass(frozen=True)
class HeadFetchOutcome:
    """What one `head_fetch` visit changed (§5.2)."""

    #: The `backfill_status` this visit persisted, or None when it wrote no status at all
    #: (step 1 failed) - the portal stays `pending`/`head` and simply retries.
    backfill_status: str | None
    #: Written only when the whole two-step sequence succeeded; None means "unchanged".
    high_id: int | None
    low_id: int | None
    backfill_total: int | None
    rows_upserted: int
    quarantined: int
    rejected: int
    requests: int
    errors: tuple[BitrixError, ...]
    #: §5.4 guard fired; the portal is parked and the visit must end now.
    blocked: bool
    #: The §5.6 pacing hook ended the visit early; the step itself stays resumable.
    stopped: bool

    @property
    def completed(self) -> bool:
        """True once the portal left `pending`/`head` - the cursors now exist."""
        return self.backfill_status in ("running", "done")


def _empty_portal(outcome: FetchOutcome) -> bool:
    """No statistics at all - as opposed to "50 rows arrived and none of them parsed".

    The difference is the whole install: treating an all-rejected first page as an empty
    portal would set `backfill_status='done'` with the entire history never fetched, and
    nothing in the system would ever look at that portal again.
    """
    return not outcome.rows and not outcome.rejected and not (outcome.total or 0)


async def run_head_fetch(
    fence: Fence,
    client: BitrixClient,
    *,
    batch_pages: int,
    pace: Pacer | None = None,
) -> HeadFetchOutcome:
    """Run §5.2's idempotent head fetch for a portal in `pending` or `head`.

    Bitrix24 failures are returned, not raised: §5.6/§5.8 back-off and the terminal
    `token_status` decisions belong to the runner, which sees `outcome.errors`. Two
    exceptions must never be swallowed and are therefore allowed to propagate:
    `ExpiredToken` (so §5.8 refreshes once and retries) and `FenceLost` (a newer runner
    owns this portal).
    """
    pages = clamp_pages(batch_pages)
    requests = 0

    # ---- step 1: M and total. One command, no filter, newest first. -----------------
    head = await fetch_pages(
        client,
        filter={},
        sort=SORT_FIELD,
        order=ORDER_DESC,
        starts=(0,),
        guard=None,
    )
    requests += 1
    raise_if_token_expired(head.errors)
    if head.filter_violation is not None:
        await block_on_filter_violation(fence, violation=head.filter_violation, step="head_fetch")
        return HeadFetchOutcome(
            backfill_status=None, high_id=None, low_id=None, backfill_total=None,
            rows_upserted=0, quarantined=0, rejected=0, requests=requests,
            errors=tuple(head.errors), blocked=True, stopped=False,
        )
    if head.errors or head.prefix_len == 0:
        # Nothing is known yet, so nothing is written: the portal stays pending/head and
        # the next visit repeats step 1 (§5.2 idempotency).
        return HeadFetchOutcome(
            backfill_status=None, high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=0, quarantined=0, rejected=0, requests=requests,
            errors=tuple(head.errors), blocked=False, stopped=False,
        )

    if _empty_portal(head):
        # §5.2: zero rows -> high_id = 0, backfill_status = 'done'. `incremental` then
        # walks forward from 0 as soon as this portal makes its first call.
        stamp = now()
        await commit_cursor(
            fence,
            {
                "high_id": 0,
                "low_id": None,
                "backfill_status": "done",
                "backfill_total": 0,
                "backfill_done": 0,
                "backfill_started_at": stamp,
                "backfill_finished_at": stamp,
            },
        )
        log.info("head_fetch: portal has no statistics", extra={"portal_id": fence.portal_id})
        return HeadFetchOutcome(
            backfill_status="done", high_id=0, low_id=None, backfill_total=0,
            rows_upserted=0, quarantined=0, rejected=0, requests=requests,
            errors=(), blocked=False, stopped=False,
        )

    head_ids = prefix_bx_ids(head)
    if not head_ids:
        # Rows (or a non-zero `total`) exist but not one usable id came back. Retry -
        # never "done", which would abandon the whole history.
        log.warning(
            "head_fetch: first page carried no usable ID",
            extra={"portal_id": fence.portal_id, "rejected": len(head.rejected)},
        )
        return HeadFetchOutcome(
            backfill_status=None, high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=0, quarantined=0, rejected=len(head.rejected), requests=requests,
            errors=tuple(head.errors), blocked=False, stopped=False,
        )
    max_id = max(head_ids)

    # The `head` marker (never `high_id`) plus the denominator for the UI progress bar.
    # Persisting it here is what makes a crash before step 2 resumable rather than a
    # wedge: §5.2 runs this function for `pending` AND `head`.
    marker: dict[str, Any] = {"backfill_status": "head", "backfill_started_at": now()}
    if head.total is not None:
        marker["backfill_total"] = head.total
    await commit_cursor(fence, marker)

    if not await should_continue(pace, head.time_block):
        return HeadFetchOutcome(
            backfill_status="head", high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=0, quarantined=0, rejected=len(head.rejected), requests=requests,
            errors=(), blocked=False, stopped=True,
        )

    # ---- step 2: the immutable `ID <= M` window, paged inside ONE request. ----------
    window = await fetch_pages(
        client,
        filter={"<=ID": max_id},
        sort=SORT_FIELD,
        order=ORDER_DESC,
        starts=page_starts(pages),
        # No `guard=`: `fetch_pages` derives the `<=ID` assertion from the filter it was
        # given (§5.4), and a hand-written duplicate could only ever diverge from it.
    )
    requests += 1
    raise_if_token_expired(window.errors)
    if window.filter_violation is not None:
        await block_on_filter_violation(
            fence, violation=window.filter_violation, step="head_fetch"
        )
        return HeadFetchOutcome(
            backfill_status="head", high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=0, quarantined=0, rejected=0, requests=requests,
            errors=tuple(window.errors), blocked=True, stopped=False,
        )

    rows = dedupe_rows([*head.rows, *window.rows, *window.extra_rows])
    all_rejected = [*head.rejected, *window.rejected]

    if window.errors:
        # §5.2: any per-command error means "stay in head, retry". The rows are upserted
        # anyway (idempotent, and they are real data), but NO cursor is written, so the
        # failed page is re-read next visit instead of becoming a permanent hole.
        rows_upserted, quarantined = await commit_progress(fence, rows, rejected=all_rejected)
        log.info(
            "head_fetch: per-command error, staying in head",
            extra={
                "portal_id": fence.portal_id,
                "errors": len(window.errors),
                "prefix_len": window.prefix_len,
            },
        )
        return HeadFetchOutcome(
            backfill_status="head", high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=rows_upserted, quarantined=quarantined, rejected=len(all_rejected),
            requests=requests, errors=tuple(window.errors), blocked=False, stopped=False,
        )

    window_ids = prefix_bx_ids(window)
    if not window_ids:
        # `ID <= M` came back empty although step 1 saw M a moment ago: the selection moved
        # under us (rows purged on the portal side). Retry from step 1 rather than invent a
        # `low_id` that would make `backfill` walk below rows that were never imported.
        log.warning(
            "head_fetch: window page 0 returned nothing, staying in head",
            extra={"portal_id": fence.portal_id, "max_id": max_id},
        )
        return HeadFetchOutcome(
            backfill_status="head", high_id=None, low_id=None, backfill_total=head.total,
            rows_upserted=0, quarantined=0, rejected=len(all_rejected), requests=requests,
            errors=(), blocked=False, stopped=False,
        )
    low_id = min(window_ids)

    # The `ID <= M` selection is exhausted when its last page reported no `next`: there is
    # provably nothing below `low_id`, so the backfill has no work at all and one whole
    # request per small portal is saved.
    exhausted = not window.has_next
    cursor: dict[str, Any] = {
        "high_id": max_id,
        "low_id": low_id,
        "backfill_status": "done" if exhausted else "running",
        "backfill_done": len(rows),
        # §5.7: the rescan's persisted lower bound starts at the bottom of the head window
        # - a bounded, never-NULL value. `incremental` ages it forward from here.
        "rescan_from_id": low_id,
    }
    if head.total is not None:
        cursor["backfill_total"] = head.total
    if exhausted:
        cursor["backfill_finished_at"] = now()

    upserted, quarantined = await commit_progress(
        fence, rows, cursor_values=cursor, rejected=all_rejected
    )
    log.info(
        "head_fetch complete",
        extra={
            "portal_id": fence.portal_id,
            "high_id": max_id,
            "low_id": low_id,
            "rows": upserted,
            "backfill_status": cursor["backfill_status"],
        },
    )
    return HeadFetchOutcome(
        backfill_status=str(cursor["backfill_status"]),
        high_id=max_id,
        low_id=low_id,
        backfill_total=head.total,
        rows_upserted=upserted,
        quarantined=quarantined,
        rejected=len(all_rejected),
        requests=requests,
        errors=(),
        blocked=False,
        stopped=False,
    )
