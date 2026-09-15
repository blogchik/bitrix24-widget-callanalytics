"""Newest-first id ranges walked by keyset: the plan the CRM backfill and patrol follow.

A CRM table is cut into disjoint id ranges `[lo, hi)`, newest first. Inside a range the walk
is the keyset S-A measured (docs/spike-crm-mirror.md): ids above the last one seen,
ascending, `start: -1`, 50 rows. A range is done when a page comes back short.

Two rules from `sync/fetch.py` carry over, in the shape independent ranges need:

1. **A cursor moves only over rows it saw.** A command that errored or answered an unusable
   shape leaves its own range exactly where it was. Unlike the single call-history cursor,
   it does not stop the other ranges in the batch: their pages are independent, so their
   progress leaves no hole. `coverage_floor` is what turns "every range down to here is
   done" into a number a page can show.
2. **The filter-honoured guard.** Every returned id must lie in its range and above its
   cursor, strictly ascending. A build that ignored the operator would otherwise hand back
   the same first page forever. One violation neuters the whole batch - no range moves -
   and is reported so the lane parks instead of looping.

Everything here is pure and immutable: `apply_range_batch` returns the new streams, and the
caller persists them in the same transaction as the rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from app.bitrix.client import MAX_BATCH_COMMANDS, BatchResult
from app.bitrix.crm_items import (
    PAGE_SIZE,
    Command,
    ItemRow,
    MirrorDialect,
    keyset_command,
    page_rows,
    parse_item,
    row_id,
)
from app.bitrix.errors import BitrixError, classify
from app.sync.throttle import merge_time_blocks

__all__ = [
    "RangeBatchOutcome",
    "RangeStream",
    "apply_range_batch",
    "coverage_floor",
    "plan_ranges",
    "range_commands",
]

_KEY_PREFIX: Final[str] = "r"


@dataclass(frozen=True)
class RangeStream:
    """One id range and how far into it the walk has got."""

    lo: int
    hi: int
    #: The last id seen; `lo - 1` before the first page.
    cursor: int
    done: bool = False

    def __post_init__(self) -> None:
        if self.hi <= self.lo:
            raise ValueError(f"empty range [{self.lo}, {self.hi})")
        if not self.lo - 1 <= self.cursor < self.hi:
            raise ValueError(f"cursor {self.cursor} outside [{self.lo - 1}, {self.hi})")


def plan_ranges(high_id: int, *, width: int, floor: int = 1) -> list[RangeStream]:
    """Disjoint ranges covering `[floor, high_id]`, newest first."""
    if width <= 0:
        raise ValueError("range width must be positive")
    streams: list[RangeStream] = []
    hi = high_id + 1
    while hi > floor:
        lo = max(floor, hi - width)
        streams.append(RangeStream(lo=lo, hi=hi, cursor=lo - 1))
        hi = lo
    return streams


def coverage_floor(streams: Sequence[RangeStream]) -> int | None:
    """The lowest id such that every range from the newest down to it is done.

    `None` while the newest range is still open. Streams must be in `plan_ranges` order.
    """
    floor: int | None = None
    for stream in streams:
        if not stream.done:
            break
        floor = stream.lo
    return floor


def _key(index: int) -> str:
    return f"{_KEY_PREFIX}{index}"


def range_commands(
    dialect: MirrorDialect,
    streams: Sequence[RangeStream],
    *,
    max_commands: int,
    ids_only: bool = False,
) -> list[tuple[int, Command]]:
    """One keyset page for each open range, newest first, up to `max_commands`.

    Returned as `(stream index, command)` pairs, which `apply_range_batch` needs back.
    """
    if not 1 <= max_commands <= MAX_BATCH_COMMANDS:
        raise ValueError(f"max_commands must be within 1..{MAX_BATCH_COMMANDS}")
    planned: list[tuple[int, Command]] = []
    for index, stream in enumerate(streams):
        if stream.done:
            continue
        planned.append(
            (
                index,
                keyset_command(
                    dialect,
                    _key(index),
                    after_id=stream.cursor,
                    below_id=stream.hi,
                    ids_only=ids_only,
                ),
            )
        )
        if len(planned) == max_commands:
            break
    return planned


@dataclass(frozen=True)
class RangeBatchOutcome:
    """What one batch of range pages tells the caller."""

    #: The streams after this batch - unchanged when `filter_violation` is set.
    streams: tuple[RangeStream, ...]
    #: Parsed rows from every command that answered cleanly.
    rows: list[ItemRow] = field(default_factory=list)
    #: `(id or None, reason)` for rows the parser refused.
    rejected: list[tuple[int | None, str]] = field(default_factory=list)
    #: `(stream index, error)` for commands that errored or answered an unusable shape.
    errors: list[tuple[int, BitrixError]] = field(default_factory=list)
    filter_violation: str | None = None
    #: The batch and per-command `time` blocks folded as §5.6 folds them.
    time_block: dict[str, Any] | None = None

    @property
    def clean(self) -> bool:
        return not self.errors and self.filter_violation is None


def _violation(ids: Sequence[int], stream: RangeStream) -> str | None:
    previous = stream.cursor
    for value in ids:
        if value <= previous or value < stream.lo or value >= stream.hi:
            return (
                f"range [{stream.lo}, {stream.hi}) after id {stream.cursor} returned id {value}"
                f" following {previous}"
            )
        previous = value
    return None


def apply_range_batch(
    dialect: MirrorDialect,
    streams: Sequence[RangeStream],
    sent: Sequence[tuple[int, Command]],
    batch: BatchResult,
    *,
    utm_max_chars: int,
) -> RangeBatchOutcome:
    """Parse one answered batch, apply the guard, and advance the ranges that answered.

    `sent` is exactly what `range_commands` returned for this batch.
    """
    by_key = {command.key: command for command in batch.commands}
    updated = list(streams)
    rows: list[ItemRow] = []
    rejected: list[tuple[int | None, str]] = []
    errors: list[tuple[int, BitrixError]] = []
    violation: str | None = None

    for index, (key, _method, _params) in sent:
        stream = streams[index]
        command = by_key.get(key)
        if command is None:
            errors.append((index, classify(None, description=f"batch answered no {key}")))
            continue
        if command.error is not None:
            errors.append((index, command.error))
            continue
        page = page_rows(command.result, dialect)
        if page is None:
            reason = f"{key}: unexpected result shape {type(command.result).__name__}"
            errors.append((index, classify(None, description=reason)))
            continue

        ids = [value for value in (row_id(raw, dialect) for raw in page) if value is not None]
        for raw in page:
            parsed = parse_item(raw, dialect, utm_max_chars=utm_max_chars)
            if isinstance(parsed, str):
                rejected.append((row_id(raw, dialect), parsed))
            else:
                rows.append(parsed)
        found = _violation(ids, stream)
        if found is not None:
            # This page's ids cannot position a cursor at all; the batch is neutered below.
            violation = violation or found
            continue
        cursor = ids[-1] if ids else stream.cursor
        updated[index] = replace(stream, cursor=cursor, done=len(page) < PAGE_SIZE)

    time_block = merge_time_blocks([batch.time, *(command.time for command in batch.commands)])
    if violation is not None:
        return RangeBatchOutcome(
            streams=tuple(streams),
            rows=rows,
            rejected=rejected,
            errors=errors,
            filter_violation=violation,
            time_block=time_block,
        )
    return RangeBatchOutcome(
        streams=tuple(updated),
        rows=rows,
        rejected=rejected,
        errors=errors,
        time_block=time_block,
    )
