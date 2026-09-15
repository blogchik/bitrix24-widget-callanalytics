"""The CRM backfill's cursor: newest-first id ranges, planned a few at a time (§5.10).

`sync/crm_ranges.py` walks ranges; this module decides which ranges exist and what "done"
means, in a cursor small enough for one `crm_lanes` row. Only the ranges being walked are
stored:

* every id at or above `covered` has been read;
* every id below `next_hi` has not been planned yet;
* `open` is the window in between, newest first.

A portal with ten million deals therefore stores twenty ranges, not five hundred, and a crash
costs at most the pages of one batch, which the next visit reads again.

The head is read once, when the lane starts: records created after it are not the backfill's
business, because the `updatedTime` sweep - which starts looking before the backfill does -
sees every record created or changed from then on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from app.bitrix.crm_items import PAGE_SIZE, MirrorDialect, page_rows, row_id
from app.sync.crm_ranges import RangeStream

__all__ = [
    "DIALECT_ITEM",
    "DIALECT_LEGACY",
    "BackfillCursor",
    "headed",
    "plan",
    "read_high_id",
    "settle",
]

#: `crm.item.list`, or the frozen `crm.deal.list` / `crm.lead.list` on a build without it.
DIALECT_ITEM: Final[str] = "item"
DIALECT_LEGACY: Final[str] = "legacy"
_DIALECTS: Final[frozenset[str]] = frozenset({DIALECT_ITEM, DIALECT_LEGACY})


@dataclass(frozen=True)
class BackfillCursor:
    dialect: str = DIALECT_ITEM
    #: The newest id when the lane started; None until the head has been read.
    high_id: int | None = None
    width: int = 0
    next_hi: int = 0
    covered: int = 0
    open: tuple[RangeStream, ...] = ()

    @property
    def headed(self) -> bool:
        return self.high_id is not None

    @property
    def finished(self) -> bool:
        return self.high_id is not None and self.next_hi <= 1 and not self.open

    @property
    def progress(self) -> tuple[int, int | None]:
        """`(ids covered, newest id)` - what `crm_lanes.progress_*` shows."""
        if self.high_id is None:
            return 0, None
        return max(0, self.high_id + 1 - self.covered), self.high_id

    def to_json(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "high_id": self.high_id,
            "width": self.width,
            "next_hi": self.next_hi,
            "covered": self.covered,
            "open": [[stream.lo, stream.hi, stream.cursor, stream.done] for stream in self.open],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> BackfillCursor:
        """The stored cursor, or a fresh one when it cannot be trusted.

        Starting over is always safe - the upsert is idempotent and versioned - so a cursor
        that was edited by hand or written by an older build restarts the walk instead of
        raising in every visit.
        """
        dialect = raw.get("dialect") if isinstance(raw.get("dialect"), str) else DIALECT_ITEM
        if dialect not in _DIALECTS:
            dialect = DIALECT_ITEM
        high_id = raw.get("high_id")
        if high_id is None:
            return cls(dialect=dialect)
        try:
            streams = tuple(
                RangeStream(lo=int(lo), hi=int(hi), cursor=int(cursor), done=bool(done))
                for lo, hi, cursor, done in raw.get("open") or []
            )
            restored = cls(
                dialect=dialect,
                high_id=int(high_id),
                width=int(raw["width"]),
                next_hi=int(raw["next_hi"]),
                covered=int(raw["covered"]),
                open=streams,
            )
        except (KeyError, TypeError, ValueError):
            return cls(dialect=dialect)
        if restored.width <= 0 or restored.high_id is None or restored.high_id < 0:
            return cls(dialect=dialect)
        return restored


def headed(cursor: BackfillCursor, high_id: int, *, commands: int, max_width: int) -> BackfillCursor:
    """The cursor once the newest id is known.

    The width aims at `commands` ranges over the whole table, so a small portal is read in
    parallel from the first batch, and is capped so a large one still covers its newest
    records first rather than walking one enormous range.
    """
    width = max(PAGE_SIZE, min(max_width, -(-max(high_id, 1) // max(1, commands))))
    return replace(
        cursor,
        high_id=high_id,
        width=width,
        next_hi=high_id + 1,
        covered=high_id + 1,
        open=(),
    )


def plan(cursor: BackfillCursor, *, streams: int) -> BackfillCursor:
    """Open ranges below the planned ones until `streams` are open or nothing is left."""
    opened = list(cursor.open)
    next_hi = cursor.next_hi
    while len(opened) < streams and next_hi > 1:
        lo = max(1, next_hi - cursor.width)
        opened.append(RangeStream(lo=lo, hi=next_hi, cursor=lo - 1))
        next_hi = lo
    return replace(cursor, open=tuple(opened), next_hi=next_hi)


def settle(cursor: BackfillCursor, streams: Sequence[RangeStream]) -> BackfillCursor:
    """Take a batch's streams back; the leading done ranges move `covered` down.

    A done range behind one still open stays in `open`: `covered` is a contiguous claim, and
    closing a gap it does not have would tell a report that history is loaded when it is not.
    """
    remaining = list(streams)
    covered = cursor.covered
    while remaining and remaining[0].done:
        covered = remaining[0].lo
        remaining.pop(0)
    return replace(cursor, open=tuple(remaining), covered=covered)


def read_high_id(result: Any, dialect: MirrorDialect) -> int | None:
    """The newest id from a `high_id_command` answer; 0 for an empty table, None when unusable."""
    page = page_rows(result, dialect)
    if page is None:
        return None
    if not page:
        return 0
    ids = [value for value in (row_id(raw, dialect) for raw in page) if value is not None]
    return max(ids) if ids else None
