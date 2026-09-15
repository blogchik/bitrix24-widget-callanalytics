"""How a portal reads a datetime inside a CRM filter, measured rather than assumed (§5.10).

Measured on production on 2026-09-15 (docs/spike-crm-mirror.md, S-A.10). Bitrix24 renders
`updatedTime` in the server's zone correctly: 1 615 leads created by a call carry the call's
own start time, median difference zero. It reads a datetime FILTER value shifted by the token
user's time zone offset from the server's, whatever offset the value itself carries. On that
portal the installer sits two hours east of the server, so `>=updatedTime: 15:30Z` selected
records updated since 13:30Z.

East of the server the shift only widens a sweep. West of it, it narrows the sweep, and an
edit is skipped with nothing anywhere to show for it. So the sweep does not trust a datetime
filter it has not measured:

* **The measurement** takes one mirrored record whose `updatedTime` is known and asks whether
  it still matches `>=updatedTime` at bounds moved later by whole hours, then by quarter hours
  inside the last hour that matched. The largest bound that still matches is the shift: every
  zone offset in use is a multiple of fifteen minutes, and none is further than fourteen hours
  from another zone's.
* **Nothing half-measured is used.** Answers that are not a clean prefix (every earlier bound
  matched, every later one did not), a record edited while it was being measured, or an error
  all leave the shift unknown.
* **Unknown means wide.** Until a shift is known the sweep sends its bound fourteen hours early,
  which can over-read by a day of edits and can never under-read.

The shift belongs to the token user and to two zones' rules, so it is measured again daily and
whenever a sweep finds rows its measured bound could not have produced.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.crm_items import (
    Command,
    ItemRow,
    MirrorDialect,
    ids_command,
    keyset_command,
    page_rows,
    parse_item,
    row_id,
)
from app.bitrix.errors import BitrixError, classify
from app.config import settings
from app.db.session import tenant_txn
from app.sync.throttle import merge_time_blocks

__all__ = [
    "MAX_SHIFT",
    "Measurement",
    "due",
    "fresh",
    "measure",
    "prefix_length",
    "probe_commands",
    "reference_record",
]

#: The widest shift two zones can have between them that this measures.
MAX_SHIFT: Final[dt.timedelta] = dt.timedelta(hours=14)
_HOUR: Final[dt.timedelta] = dt.timedelta(hours=1)
_QUARTER: Final[dt.timedelta] = dt.timedelta(minutes=15)

#: A shift measured this recently is not measured again when a sweep disagrees with it: the
#: disagreement is then the portal ignoring the filter, and the lane parks.
_FRESH: Final[dt.timedelta] = dt.timedelta(minutes=10)

#: A reference record edited more recently than this is used only when nothing quieter exists:
#: an edit landing between the two batches would move the answer.
_QUIET: Final[dt.timedelta] = dt.timedelta(days=1)

_PROBE_KEY: Final[str] = "clk"
_CURRENT_KEY: Final[str] = "clk_now"

#: `parse_item` wants a tag length; only `updated_time` is read from the answer.
_ANY_TAG_LENGTH: Final[int] = 255


@dataclass(frozen=True)
class Measurement:
    """What one measurement found. `shift` is None unless the answers were clean."""

    shift: dt.timedelta | None
    batches: int
    error: BitrixError | None = None
    reason: str = ""
    time_block: dict[str, Any] | None = None


def due(calibrated_at: dt.datetime | None, *, now: dt.datetime) -> bool:
    """Whether a sweep should measure before its next page."""
    if calibrated_at is None:
        return True
    return (now - calibrated_at).total_seconds() >= settings.crm_clock_interval_sec


def fresh(calibrated_at: dt.datetime | None, *, now: dt.datetime) -> bool:
    """Whether a measurement is recent enough that a disagreeing page means an ignored filter."""
    return calibrated_at is not None and now - calibrated_at < _FRESH


def probe_commands(
    dialect: MirrorDialect, record_id: int, updated: dt.datetime, offsets: Sequence[dt.timedelta]
) -> list[Command]:
    """One command per offset: does this one record match `>=updatedTime` at `updated + offset`?

    The record is pinned by an id range of one rather than `@id`, so the command is exactly the
    keyset shape a sweep sends, datetime filter included.
    """
    return [
        keyset_command(
            dialect,
            f"{_PROBE_KEY}{index}",
            after_id=record_id - 1,
            below_id=record_id + 1,
            updated_since=(updated + offset).isoformat(),
            ids_only=True,
        )
        for index, offset in enumerate(offsets)
    ]


def prefix_length(flags: Sequence[bool]) -> int | None:
    """How many leading answers matched, or None when a later one matched after a miss."""
    count = 0
    for flag in flags:
        if not flag:
            break
        count += 1
    return None if any(flags[count:]) else count


def _matched(
    batch: BatchResult, dialect: MirrorDialect, record_id: int, count: int
) -> tuple[list[bool] | None, BitrixError | None]:
    flags: list[bool] = []
    for index in range(count):
        key = f"{_PROBE_KEY}{index}"
        answer = next((item for item in batch.commands if item.key == key), None)
        if answer is None:
            return None, classify(None, description=f"batch answered no {key}")
        if answer.error is not None:
            return None, answer.error
        rows = page_rows(answer.result, dialect)
        if rows is None:
            return None, classify(None, description=f"{key}: unexpected result shape")
        flags.append(any(row_id(row, dialect) == record_id for row in rows))
    return flags, None


def _current(batch: BatchResult, dialect: MirrorDialect) -> tuple[ItemRow | None, BitrixError | None]:
    answer = next((item for item in batch.commands if item.key == _CURRENT_KEY), None)
    if answer is None:
        return None, classify(None, description=f"batch answered no {_CURRENT_KEY}")
    if answer.error is not None:
        return None, answer.error
    rows = page_rows(answer.result, dialect) or []
    parsed = parse_item(rows[0], dialect, utm_max_chars=_ANY_TAG_LENGTH) if rows else None
    return (parsed if isinstance(parsed, ItemRow) else None), None


async def measure(
    client: BitrixClient, dialect: MirrorDialect, record_id: int, updated: dt.datetime
) -> Measurement:
    """Two batches: whole hours from -14 h to +14 h, then the three quarters after the last match."""
    hours = [_HOUR * step for step in range(-14, 15)]
    first = await client.batch(probe_commands(dialect, record_id, updated, hours), halt=0)
    blocks: list[dict[str, Any] | None] = [first.time]
    flags, error = _matched(first, dialect, record_id, len(hours))
    if error is not None or flags is None:
        return Measurement(None, batches=1, error=error, time_block=merge_time_blocks(blocks))
    matched = prefix_length(flags)
    if matched is None or matched in (0, len(hours)):
        # No hour matched, every hour matched, or a match after a miss: the filter is not a
        # datetime comparison this can measure (an ignored filter matches everything).
        return Measurement(
            None, batches=1, reason="no clean hour boundary", time_block=merge_time_blocks(blocks)
        )

    hour = hours[matched - 1]
    quarters = [hour + _QUARTER * step for step in (1, 2, 3)]
    second = await client.batch(
        [
            *probe_commands(dialect, record_id, updated, quarters),
            ids_command(dialect, _CURRENT_KEY, [record_id]),
        ],
        halt=0,
    )
    blocks.append(second.time)
    time_block = merge_time_blocks(blocks)

    current, error = _current(second, dialect)
    if error is not None:
        return Measurement(None, batches=2, error=error, time_block=time_block)
    if current is None or current.updated_time != updated:
        return Measurement(
            None,
            batches=2,
            reason="the reference record changed while it was measured",
            time_block=time_block,
        )
    flags, error = _matched(second, dialect, record_id, len(quarters))
    if error is not None or flags is None:
        return Measurement(None, batches=2, error=error, time_block=time_block)
    within = prefix_length(flags)
    if within is None:
        return Measurement(None, batches=2, reason="no clean quarter boundary", time_block=time_block)
    return Measurement(hour + _QUARTER * within, batches=2, time_block=time_block)


async def reference_record(
    portal_id: int, entity_type_id: int, *, now: dt.datetime
) -> tuple[int, dt.datetime] | None:
    """A mirrored record to measure against: the latest one quiet for a day, else the latest one."""
    async with tenant_txn(portal_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, updated_time FROM crm_items
                    WHERE portal_id = :pid AND entity_type_id = :entity
                      AND deleted_at IS NULL AND updated_time IS NOT NULL
                    ORDER BY (updated_time < :quiet) DESC, updated_time DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"pid": portal_id, "entity": entity_type_id, "quiet": now - _QUIET},
            )
        ).first()
    return None if row is None else (int(row.id), row.updated_time)
