"""The CRM mirror's lanes: which exist, when each is due, and what one run leaves behind (§5.10).

A lane is one kind of CRM work for one portal - the dictionary, a backfill, a sweep - with its
own schedule, cursor and failure count in `crm_lanes`. Lanes exist so a failure stays where it
happened: a lead list refused for rights backs off alone, the deal sweep keeps its five
minutes, and neither touches `portal_sync`, whose failure counter parks the call sync (§5.6).

Everything except the database helpers at the bottom is pure: a `Lane` in, a `Lane` out.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.errors import BitrixError
from app.db.models import CrmLane

__all__ = [
    "ACTIVE",
    "DEAL_BACKFILL",
    "DEAL_SWEEP",
    "DICT",
    "DONE",
    "FAILURES_BEFORE_PAUSE",
    "LANES",
    "LEAD_BACKFILL",
    "LEAD_SWEEP",
    "PARKED",
    "PENDING",
    "UNAVAILABLE_RETRY_SECONDS",
    "Lane",
    "delete_lanes",
    "ensure_lanes",
    "failed",
    "load_lanes",
    "next_wake",
    "parked",
    "resting",
    "store_lanes",
    "succeeded",
    "unavailable",
]

DICT: Final[str] = "dict"
DEAL_SWEEP: Final[str] = "deal.sweep"
LEAD_SWEEP: Final[str] = "lead.sweep"
DEAL_BACKFILL: Final[str] = "deal.backfill"
LEAD_BACKFILL: Final[str] = "lead.backfill"

#: Every lane a CRM-enabled portal has, in the order a visit runs them: names first, then
#: what changed, then history.
LANES: Final[tuple[str, ...]] = (DICT, DEAL_SWEEP, LEAD_SWEEP, DEAL_BACKFILL, LEAD_BACKFILL)

PENDING: Final[str] = "pending"
ACTIVE: Final[str] = "active"
DONE: Final[str] = "done"
PARKED: Final[str] = "parked"

#: The same escalation §5.6 gives a failing portal - 60, 120, 240 ... 3600 s, then six hours
#: after ten in a row - applied to one lane.
FAILURE_BASE_SECONDS: Final[float] = 60.0
FAILURE_MAX_SECONDS: Final[float] = 3_600.0
FAILURES_BEFORE_PAUSE: Final[int] = 10
FAILURE_PAUSE_SECONDS: Final[float] = 6 * 3_600.0

#: A lane refused for a reason only the portal can change - CRM switched off, leads disabled,
#: a build without the method - is asked again once a day instead of backing off as a fault.
UNAVAILABLE_RETRY_SECONDS: Final[float] = 86_400.0

_CODE_MAX: Final[int] = 64


@dataclass(frozen=True)
class Lane:
    """One `crm_lanes` row."""

    name: str
    due_at: dt.datetime
    status: str = PENDING
    cursor: Mapping[str, Any] = field(default_factory=dict)
    progress_done: int = 0
    progress_total: int | None = None
    last_clean_at: dt.datetime | None = None
    failures: int = 0
    paused_until: dt.datetime | None = None
    block_reason: str | None = None
    last_error_code: str | None = None
    last_error_at: dt.datetime | None = None

    def runnable(self, now: dt.datetime) -> bool:
        """Due, not backing off, and not finished or parked."""
        if self.status in (DONE, PARKED):
            return False
        if self.paused_until is not None and self.paused_until > now:
            return False
        return self.due_at <= now

    def wakes_at(self) -> dt.datetime | None:
        """When this lane next wants a visit; None when it never will by itself."""
        if self.status in (DONE, PARKED):
            return None
        if self.paused_until is not None and self.paused_until > self.due_at:
            return self.paused_until
        return self.due_at


def _code(error: BitrixError | None, reason: str | None) -> str:
    if reason:
        return reason[:_CODE_MAX]
    if error is None:
        return "unknown"
    return (error.code or type(error).__name__)[:_CODE_MAX]


def succeeded(
    lane: Lane,
    *,
    now: dt.datetime,
    due_at: dt.datetime,
    cursor: Mapping[str, Any] | None = None,
    status: str = ACTIVE,
    progress_done: int | None = None,
    progress_total: int | None = None,
) -> Lane:
    """A run that finished without an error: the failure streak and any back-off end."""
    return replace(
        lane,
        status=status,
        due_at=due_at,
        cursor=lane.cursor if cursor is None else dict(cursor),
        progress_done=lane.progress_done if progress_done is None else progress_done,
        progress_total=lane.progress_total if progress_total is None else progress_total,
        last_clean_at=now,
        failures=0,
        paused_until=None,
        block_reason=None,
        last_error_code=None,
        last_error_at=None,
    )


def failed(
    lane: Lane,
    error: BitrixError | None,
    *,
    now: dt.datetime,
    reason: str | None = None,
    cursor: Mapping[str, Any] | None = None,
) -> Lane:
    """A run that hit an error: back off this lane, escalating, and pause after ten."""
    failures = lane.failures + 1
    if failures >= FAILURES_BEFORE_PAUSE:
        delay = FAILURE_PAUSE_SECONDS
    else:
        delay = min(FAILURE_MAX_SECONDS, FAILURE_BASE_SECONDS * 2.0 ** (failures - 1))
    code = _code(error, reason)
    return replace(
        lane,
        status=ACTIVE,
        cursor=lane.cursor if cursor is None else dict(cursor),
        failures=failures,
        paused_until=now + dt.timedelta(seconds=delay),
        last_error_code=code,
        last_error_at=now,
    )


def unavailable(
    lane: Lane,
    reason: str,
    *,
    now: dt.datetime,
    cursor: Mapping[str, Any] | None = None,
) -> Lane:
    """The portal refuses this work for a reason of its own: ask again tomorrow.

    Not counted as a failure - nothing is broken - and `block_reason` says why, so the
    settings page can name it instead of showing an error.
    """
    code = reason[:_CODE_MAX]
    return replace(
        lane,
        status=ACTIVE,
        cursor=lane.cursor if cursor is None else dict(cursor),
        paused_until=now + dt.timedelta(seconds=UNAVAILABLE_RETRY_SECONDS),
        block_reason=code,
        last_error_code=code,
        last_error_at=now,
    )


def resting(lane: Lane, until: dt.datetime) -> Lane:
    """Wait for an operating-time budget to refill: not a failure, no reason, only a time."""
    return replace(lane, paused_until=until)


def parked(lane: Lane, reason: str, *, now: dt.datetime) -> Lane:
    """Stop for good - only for a portal whose answers cannot be trusted (a filter ignored)."""
    code = reason[:_CODE_MAX]
    return replace(lane, status=PARKED, block_reason=code, last_error_code=code, last_error_at=now)


def next_wake(lanes: Iterable[Lane]) -> dt.datetime | None:
    """The earliest moment any lane wants a visit."""
    moments = [moment for lane in lanes if (moment := lane.wakes_at()) is not None]
    return min(moments) if moments else None


# --------------------------------------------------------------------------- database


def _from_row(row: CrmLane) -> Lane:
    return Lane(
        name=row.lane,
        due_at=row.due_at,
        status=row.status,
        cursor=dict(row.cursor or {}),
        progress_done=int(row.progress_done or 0),
        progress_total=None if row.progress_total is None else int(row.progress_total),
        last_clean_at=row.last_clean_at,
        failures=int(row.failures or 0),
        paused_until=row.paused_until,
        block_reason=row.block_reason,
        last_error_code=row.last_error_code,
        last_error_at=row.last_error_at,
    )


async def load_lanes(session: AsyncSession, portal_id: int) -> dict[str, Lane]:
    rows = (await session.execute(select(CrmLane).where(CrmLane.portal_id == portal_id))).scalars()
    return {row.lane: _from_row(row) for row in rows}


async def ensure_lanes(session: AsyncSession, portal_id: int, now: dt.datetime) -> dict[str, Lane]:
    """Every lane of `LANES`, created due now where missing, in the caller's transaction."""
    await session.execute(
        pg_insert(CrmLane)
        .values([{"portal_id": portal_id, "lane": name, "due_at": now} for name in LANES])
        .on_conflict_do_nothing(index_elements=["portal_id", "lane"])
    )
    return await load_lanes(session, portal_id)


_STORED: Final[tuple[str, ...]] = (
    "status",
    "due_at",
    "cursor",
    "progress_done",
    "progress_total",
    "last_clean_at",
    "failures",
    "paused_until",
    "block_reason",
    "last_error_code",
    "last_error_at",
)


async def store_lanes(session: AsyncSession, portal_id: int, lanes: Iterable[Lane]) -> None:
    """Upsert the given lanes in the caller's (fenced) transaction."""
    rows = [
        {
            "portal_id": portal_id,
            "lane": lane.name,
            "status": lane.status,
            "due_at": lane.due_at,
            "cursor": dict(lane.cursor),
            "progress_done": lane.progress_done,
            "progress_total": lane.progress_total,
            "last_clean_at": lane.last_clean_at,
            "failures": lane.failures,
            "paused_until": lane.paused_until,
            "block_reason": lane.block_reason,
            "last_error_code": lane.last_error_code,
            "last_error_at": lane.last_error_at,
        }
        for lane in lanes
    ]
    if not rows:
        return
    statement = pg_insert(CrmLane).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["portal_id", "lane"],
            set_={column: statement.excluded[column] for column in _STORED},
        )
    )


async def delete_lanes(session: AsyncSession, portal_id: int) -> None:
    """Forget every lane of a portal: its CRM history starts again from nothing."""
    await session.execute(delete(CrmLane).where(CrmLane.portal_id == portal_id))
