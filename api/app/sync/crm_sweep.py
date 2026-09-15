"""The `updatedTime` sweep: re-read whatever changed since the last pass (§5.10).

Until change signals exist (milestone M6) this is how an edit reaches the mirror; after them it
is the backstop for a lost signal. One pass is a keyset walk by id over the records whose
`updatedTime` is at or after the watermark minus an overlap:

* **The watermark is the server's clock**, from `time.date_start` of the pass's first page -
  never ours, which may disagree with the portal's by minutes.
* **The next pass starts where this one STARTED**, not where it ended: a record edited during
  the pass with an id the walk had already passed is caught next time, because its new
  `updatedTime` is after the start.
* **The overlap** absorbs a record whose `updatedTime` was stamped a moment before the
  transaction that made it visible committed.
* **The bound sent is not the bound wanted.** A portal reads a datetime filter shifted by its
  token user's zone (S-A.10), so the value sent carries the shift `crm_clock` measured, or goes
  fourteen hours early until one is measured.

`updatedTime` is not a complete change feed (research block (i) B8): some edits may not bump
it. A patrol re-reads values on a cycle for those; the sweep does not pretend otherwise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from app.bitrix.client import BatchResult
from app.bitrix.crm_items import (
    PAGE_SIZE,
    Command,
    ItemRow,
    MirrorDialect,
    keyset_command,
    page_rows,
    parse_item,
    parse_timestamp,
    row_id,
)
from app.bitrix.errors import BitrixError, classify
from app.sync import crm_clock
from app.sync.crm_backfill import DIALECT_ITEM, DIALECT_LEGACY
from app.sync.throttle import merge_time_blocks

__all__ = ["INITIAL_LOOKBACK", "KEY", "SweepCursor", "SweepStep", "apply", "command"]

#: How far the very first pass reaches back. It only has to reach before the backfill
#: started, which begins in the same visit.
INITIAL_LOOKBACK: Final[dt.timedelta] = dt.timedelta(hours=1)

KEY: Final[str] = "sw"

#: Slack for a timestamp Bitrix24 renders to the second while filtering to the microsecond.
_CLOCK_SLACK: Final[dt.timedelta] = dt.timedelta(seconds=1)


@dataclass(frozen=True)
class SweepCursor:
    dialect: str
    #: The current pass reads records updated at or after this, minus the overlap.
    since: dt.datetime
    #: The last id this pass has seen; 0 at the start of a pass.
    after_id: int = 0
    #: The server's clock at the first page of this pass; the next pass's watermark.
    pass_started: dt.datetime | None = None
    #: How far this portal shifts a datetime filter value (`crm_clock`); None until measured.
    shift_seconds: int | None = None
    #: When the shift was last measured; None means measure before the next page.
    calibrated_at: dt.datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "since": self.since.isoformat(),
            "after_id": self.after_id,
            "pass_started": None if self.pass_started is None else self.pass_started.isoformat(),
            "shift_seconds": self.shift_seconds,
            "calibrated_at": None if self.calibrated_at is None else self.calibrated_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], *, now: dt.datetime) -> SweepCursor:
        """The stored cursor; a fresh pass reaching back `INITIAL_LOOKBACK` when there is none."""
        dialect = raw.get("dialect")
        if dialect not in (DIALECT_ITEM, DIALECT_LEGACY):
            dialect = DIALECT_ITEM
        since = parse_timestamp(raw.get("since"))
        if since is None:
            return cls(dialect=dialect, since=now - INITIAL_LOOKBACK)
        try:
            after_id = max(0, int(raw.get("after_id") or 0))
        except (TypeError, ValueError):
            after_id = 0
        shift = raw.get("shift_seconds")
        return cls(
            dialect=dialect,
            since=since,
            after_id=after_id,
            pass_started=parse_timestamp(raw.get("pass_started")),
            shift_seconds=shift if isinstance(shift, int) and not isinstance(shift, bool) else None,
            calibrated_at=parse_timestamp(raw.get("calibrated_at")),
        )


def bound(cursor: SweepCursor, *, overlap: dt.timedelta) -> dt.datetime:
    """The `>=updatedTime` value to SEND, so the portal selects from `since - overlap` or earlier.

    The portal reads the value shifted by its token user's zone (`crm_clock`, S-A.10). A measured
    shift is added back; without one the bound goes `MAX_SHIFT` early, which can only widen the
    pass. Truncated to the second, downwards, for the same reason.
    """
    wanted = cursor.since - overlap
    if cursor.shift_seconds is None:
        wanted -= crm_clock.MAX_SHIFT
    else:
        wanted += dt.timedelta(seconds=cursor.shift_seconds)
    return wanted.replace(microsecond=0)


def command(cursor: SweepCursor, dialect: MirrorDialect, *, overlap: dt.timedelta) -> Command:
    return keyset_command(
        dialect,
        KEY,
        after_id=cursor.after_id,
        updated_since=bound(cursor, overlap=overlap).isoformat(),
    )


@dataclass(frozen=True)
class SweepStep:
    #: The cursor after this page - unchanged on an error or a violation.
    cursor: SweepCursor
    rows: list[ItemRow] = field(default_factory=list)
    rejected: list[tuple[int | None, str]] = field(default_factory=list)
    error: BitrixError | None = None
    #: The build did not honour the filter; the lane must park, not loop.
    violation: str | None = None
    pass_complete: bool = False
    time_block: dict[str, Any] | None = None


def _server_clock(*blocks: Mapping[str, Any] | None) -> dt.datetime | None:
    for block in blocks:
        if isinstance(block, Mapping):
            moment = parse_timestamp(block.get("date_start"))
            if moment is not None:
                return moment
    return None


def apply(
    cursor: SweepCursor,
    dialect: MirrorDialect,
    batch: BatchResult,
    *,
    overlap: dt.timedelta,
    now: dt.datetime,
    utm_max_chars: int,
) -> SweepStep:
    """What one answered sweep page means for the cursor."""
    answered = next((item for item in batch.commands if item.key == KEY), None)
    time_block = merge_time_blocks([batch.time, None if answered is None else answered.time])
    if answered is None:
        error = classify(None, description=f"batch answered no {KEY}")
        return SweepStep(cursor=cursor, error=error, time_block=time_block)
    if answered.error is not None:
        return SweepStep(cursor=cursor, error=answered.error, time_block=time_block)
    page = page_rows(answered.result, dialect)
    if page is None:
        error = classify(None, description=f"{KEY}: unexpected result shape")
        return SweepStep(cursor=cursor, error=error, time_block=time_block)

    rows: list[ItemRow] = []
    rejected: list[tuple[int | None, str]] = []
    for raw in page:
        parsed = parse_item(raw, dialect, utm_max_chars=utm_max_chars)
        if isinstance(parsed, str):
            rejected.append((row_id(raw, dialect), parsed))
        else:
            rows.append(parsed)

    ids = [value for value in (row_id(raw, dialect) for raw in page) if value is not None]
    floor = cursor.since - overlap - _CLOCK_SLACK
    if cursor.shift_seconds is None:
        # Sent MAX_SHIFT early and read back by an unmeasured shift of up to MAX_SHIFT either
        # way: only a row older than both can prove the filter was ignored.
        floor -= 2 * crm_clock.MAX_SHIFT
    previous = cursor.after_id
    for value in ids:
        if value <= previous:
            violation = f"sweep after id {cursor.after_id} returned id {value} following {previous}"
            return SweepStep(cursor=cursor, violation=violation, time_block=time_block)
        previous = value
    for row in rows:
        if row.updated_time is not None and row.updated_time < floor:
            violation = f"sweep since {floor.isoformat()} returned id {row.id} updated earlier"
            return SweepStep(cursor=cursor, violation=violation, time_block=time_block)
    if len(page) >= PAGE_SIZE and not ids:
        # A full page nothing can be positioned after would be read again forever.
        error = classify(None, description=f"{KEY}: a full page without a usable id")
        return SweepStep(cursor=cursor, error=error, time_block=time_block)

    started = cursor.pass_started or _server_clock(answered.time, batch.time) or now
    if len(page) < PAGE_SIZE:
        finished = replace(cursor, since=started, after_id=0, pass_started=None)
        return SweepStep(
            cursor=finished,
            rows=rows,
            rejected=rejected,
            pass_complete=True,
            time_block=time_block,
        )
    advanced = replace(cursor, after_id=ids[-1], pass_started=started)
    return SweepStep(cursor=advanced, rows=rows, rejected=rejected, time_block=time_block)
