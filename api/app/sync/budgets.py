"""Operating-time budgets per Bitrix24 method, for every method except the call statistics.

Bitrix24 accounts operating time per METHOD per application, and `time.operating` is that
method's own running accumulator (docs/spike-crm-mirror.md, S-A.6). `portal_sync` has one
pair of `operating_*` columns, and the worker used to fold every method it called into
them. Two consequences, both silent:

* a slow `user.get` could park the portal's call sync;
* a statistics soft limit ended the whole visit, skipping the employee refresh and the
  daily admin re-verification, which spend budgets of their own.

A `sync_method_budgets` row is one (portal, method): the accumulator last seen, when it
resets, and until when the worker leaves that method alone. Every decision goes through
`sync/throttle.py`'s pure functions via a `ThrottleState` view, so the soft ratio, the 429
attribution rule and the five-clean-visit recovery are the same for every method (§5.6).

`voximplant.statistic.get` stays in `portal_sync`, because that row also schedules the
portal; moving it belongs with the CRM lanes that will share the schedule.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.errors import BitrixError
from app.config import settings
from app.db.models import SyncMethodBudget
from app.sync import throttle

__all__ = [
    "MethodBudget",
    "load_budgets",
    "observe",
    "on_clean_visit",
    "on_operation_time_limit",
    "store_budgets",
]

_METHOD_MAX: Final[int] = 64
_SMALLINT_MAX: Final[int] = 32_767


@dataclass(frozen=True)
class MethodBudget:
    """One method's budget state for one portal - a row of `sync_method_budgets`."""

    method: str
    operating_seconds: Decimal | None = None
    operating_reset_at: dt.datetime | None = None
    blocked_until: dt.datetime | None = None
    #: The learned limit; `None` means `throttle.DEFAULT_OPERATING_LIMIT_S`.
    limit_s: float | None = None
    throttle_hits: int = 0
    clean_visits: int = 0

    def state(self) -> throttle.ThrottleState:
        """The `ThrottleState` view the pure decisions in `sync/throttle.py` take."""
        limit = throttle.DEFAULT_OPERATING_LIMIT_S if self.limit_s is None else self.limit_s
        limit = max(float(settings.operating_limit_floor), min(limit, throttle.MAX_OPERATING_LIMIT_S))
        return throttle.ThrottleState(
            clean_visits=self.clean_visits,
            throttle_hits=self.throttle_hits,
            operating_seconds=self.operating_seconds,
            operating_reset_at=self.operating_reset_at,
            operating_limit_s=limit,
        )

    def blocked(self, now: dt.datetime) -> bool:
        return self.blocked_until is not None and self.blocked_until > now


def observe(
    budget: MethodBudget, block: Mapping[str, Any] | None, *, now: dt.datetime | None = None
) -> tuple[MethodBudget, bool]:
    """Record one `time{}` block; `True` when the method has reached its soft limit."""
    decision = throttle.observe_time_block(budget.state(), block, now=now)
    updates = decision.updates
    updated = replace(
        budget,
        operating_seconds=updates.get("operating_seconds", budget.operating_seconds),
        operating_reset_at=updates.get("operating_reset_at", budget.operating_reset_at),
    )
    if not decision.stop_visit:
        return updated, False
    return (
        replace(
            updated,
            blocked_until=updates["next_run_at"],
            throttle_hits=int(updates["throttle_hits"]),
            clean_visits=0,
        ),
        True,
    )


def on_operation_time_limit(
    budget: MethodBudget, error: BitrixError | None, *, now: dt.datetime | None = None
) -> MethodBudget:
    """A 429 on this method: it waits for its baskets, and only it (§5.6)."""
    decision = throttle.on_operation_time_limit(budget.state(), error, now=now)
    updates = decision.updates
    return replace(
        budget,
        blocked_until=updates["next_run_at"],
        operating_reset_at=updates.get("operating_reset_at", budget.operating_reset_at),
        throttle_hits=int(updates["throttle_hits"]),
        clean_visits=0,
        limit_s=budget.limit_s if decision.operating_limit_s is None else decision.operating_limit_s,
    )


def on_clean_visit(budget: MethodBudget) -> MethodBudget:
    """A visit used this method without a stop: count towards restoring the default limit."""
    decision = throttle.on_clean_visit(budget.state())
    visits = int(decision.updates.get("clean_visits", budget.clean_visits))
    # A restored limit comes back as the default, which this table spells as NULL.
    limit = None if decision.operating_limit_s is not None else budget.limit_s
    return replace(budget, clean_visits=visits, limit_s=limit)


async def load_budgets(session: AsyncSession, portal_id: int) -> dict[str, MethodBudget]:
    rows = (
        await session.execute(
            select(SyncMethodBudget).where(SyncMethodBudget.portal_id == portal_id)
        )
    ).scalars()
    return {
        row.method: MethodBudget(
            method=row.method,
            operating_seconds=row.operating_seconds,
            operating_reset_at=row.operating_reset_at,
            blocked_until=row.blocked_until,
            limit_s=None if row.limit_s is None else float(row.limit_s),
            throttle_hits=int(row.throttle_hits),
            clean_visits=int(row.clean_visits),
        )
        for row in rows
    }


async def store_budgets(
    session: AsyncSession, portal_id: int, budgets: Iterable[MethodBudget]
) -> None:
    """Upsert the given rows in the caller's (fenced) transaction."""
    rows = [
        {
            "portal_id": portal_id,
            "method": budget.method[:_METHOD_MAX],
            "operating_seconds": budget.operating_seconds,
            "operating_reset_at": budget.operating_reset_at,
            "blocked_until": budget.blocked_until,
            "limit_s": None if budget.limit_s is None else Decimal(str(round(budget.limit_s, 2))),
            "throttle_hits": budget.throttle_hits,
            "clean_visits": min(budget.clean_visits, _SMALLINT_MAX),
        }
        for budget in budgets
    ]
    if not rows:
        return
    statement = pg_insert(SyncMethodBudget).values(rows)
    columns = (
        "operating_seconds",
        "operating_reset_at",
        "blocked_until",
        "limit_s",
        "throttle_hits",
        "clean_visits",
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["portal_id", "method"],
            set_={column: statement.excluded[column] for column in columns},
        )
    )
