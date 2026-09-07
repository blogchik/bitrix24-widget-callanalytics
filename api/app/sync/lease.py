"""Portal leases and the write fence (§5.9, §3 `portal_sync` comments, decision 13).

WHY a fence at all. `portal_sync` is the only durable record of "what has been
imported"; the rows it describes live in `calls`. Two things can make a running
worker's view of that pair obsolete *while it is mid-flight*:

* **Another runner took over.** The lease expired (a 5-minute wall clock the run
  outlived, a paused container, a network partition) and the tick handed the portal
  to a second process. Both would now advance the same cursor from different states.
* **The tenant was purged or reinstalled.** `mark_uninstalled()` and
  `store_portal_credential()` bump `sync_generation` (services/portals.py). A run that
  fetched 1 000 rows before the uninstall must not be allowed to re-insert them after
  the purge job emptied the tables — that is brief rule 7 violated by a race.

Decision 13 answers both with one rule: **every transaction that writes a cursor or
customer rows ends in an `UPDATE portal_sync ... WHERE lease_owner = :me AND
lease_expires_at > now() AND sync_generation = :gen`, and zero affected rows aborts
the whole transaction.** Because Postgres holds the row lock from that UPDATE until
commit, an uninstall that bumps the generation either lands before us (we match zero
rows and roll back) or after us (it waits, then bumps). There is no interleaving in
which both succeed.

The counterpart of the fence is the *charging* rule in `acquire_leases`: an expired
lease with `run_started_at` set is a run that died and is charged a failure; an
expired lease **without** it is a dispatch miss (the tick leased a portal and the
process stopped before `sync_portal` began). Charging the second case would push a
perfectly healthy portal towards the 6-hour failure pause purely because a container
was restarted at the wrong moment (§3 `run_started_at`).
"""

from __future__ import annotations

import datetime as dt
import os
import socket
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import case, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func, text

from app.bitrix.errors import BitrixError, OperationTimeLimit, QueryLimitExceeded
from app.config import settings
from app.db.models import Portal, PortalSync
from app.db.session import control_txn
from app.logging import get_logger

__all__ = [
    "LEASE_SECONDS",
    "WORKER_ID",
    "Fence",
    "FenceLost",
    "acquire_leases",
    "fenced_update",
    "heartbeat",
    "release_lease",
]

log = get_logger(__name__)

#: §5.9: `lease_expires_at = now() + 5 min`. Every REST call carries a 120 s httpx
#: timeout (decision 13), so even a fully hung request cannot outlive one lease
#: without the heartbeat having had several chances to extend it first.
LEASE_SECONDS: Final[int] = 300

#: §5.6: "after 10 consecutive failures the portal pauses 6 h ... never a hot loop".
FAILURES_BEFORE_PAUSE: Final[int] = 10
FAILURE_PAUSE_SECONDS: Final[int] = 6 * 3600

#: `portal_sync.lease_owner` is varchar(64) (§3); a long hostname must not abort the
#: lease UPDATE with a string-truncation error, which would stall every portal at once.
_OWNER_MAX: Final[int] = 64

#: Evaluated by Postgres, not by us: worker clocks may drift from each other, and the
#: lease predicate (`lease_expires_at > now()`) is compared against the *database*
#: clock. The interval is a module constant, never caller input.
_LEASE_DEADLINE: Final = text(f"now() + interval '{LEASE_SECONDS} seconds'")

#: Identifies this process in `lease_owner`. Host + pid is enough: two workers on one
#: host differ by pid, two hosts differ by name, and a restarted process gets a new pid
#: so it can never be mistaken for the run that died holding the lease.
WORKER_ID: Final[str] = f"{socket.gethostname()}:{os.getpid()}"[:_OWNER_MAX]


class FenceLost(Exception):
    """The fenced UPDATE matched no row: this run no longer owns the portal.

    Not an error to retry — the lease moved, or the tenant was uninstalled or
    reinstalled underneath us. The only correct reaction is to abandon the
    transaction (which rolls back whatever rows it had written) and stop the run.
    """


@dataclass(frozen=True)
class Fence:
    """The three values every fenced write is checked against (decision 13).

    Captured once, when the lease is taken, and never refreshed inside a run: the
    whole point is that a *stale* triple stops matching.
    """

    portal_id: int
    owner: str
    generation: int


def _sanitise_owner(owner: str) -> str:
    return owner[:_OWNER_MAX]


async def fenced_update(session: AsyncSession, fence: Fence, values: dict[str, Any]) -> None:
    """`UPDATE portal_sync SET <values>` under the §5.9 fence; `FenceLost` on 0 rows.

    Takes the caller's session on purpose. §5.3/§5.5 require the cursor move and the
    rows it describes to commit **together**; opening a transaction here would split
    them and reintroduce exactly the "rows committed, cursor lost" hole the design
    closes. The caller's transaction is aborted by the raised exception, so a lost
    fence discards the rows too.

    An empty `values` still issues the statement: even a write that has no cursor to
    move must prove it still owns the portal before it commits customer rows.
    """
    payload: dict[str, Any] = dict(values) or {"updated_at": func.now()}
    result = await session.execute(
        update(PortalSync)
        .where(
            PortalSync.portal_id == fence.portal_id,
            PortalSync.lease_owner == fence.owner,
            PortalSync.lease_expires_at > func.now(),
            PortalSync.sync_generation == fence.generation,
        )
        .values(**payload)
        .execution_options(synchronize_session=False)
    )
    if cast("CursorResult[Any]", result).rowcount != 1:
        raise FenceLost(
            f"portal {fence.portal_id}: lease/generation moved "
            f"(owner={fence.owner!r}, generation={fence.generation})"
        )


async def heartbeat(fence: Fence) -> None:
    """Extend `lease_expires_at` by another `LEASE_SECONDS` (§5.9, after every batch).

    Runs in its OWN transaction so a long batch does not hold a `portal_sync` row lock
    for the whole fetch; the fence predicate makes that safe, because a runner that has
    already lost the lease cannot extend it.

    **Raises `FenceLost`, and callers must let it propagate.** A heartbeat is the only
    moment a stale runner can discover that it was replaced *before* it spends more of
    the portal's operating-time budget; swallowing it would keep a zombie run fetching
    pages whose rows can never be committed.
    """
    async with control_txn() as session:
        await fenced_update(session, fence, {"lease_expires_at": _LEASE_DEADLINE})


async def acquire_leases(limit: int, owner: str) -> list[Fence]:
    """Lease up to `limit` due portals for `owner` (§5.9 tick, step (a)).

    The selection is the §5.9 predicate verbatim. Three of its clauses are on
    `portals`, not `portal_sync`, and each one is a hard gate rather than an
    optimisation:

    * `status='active'` — an uninstalled tenant has no data to sync and may be
      mid-purge;
    * `token_status='ok'` — the terminal states of §5.8 (`reauth_required`,
      `no_stats_permission`, `filter_unsupported`, `method_missing`) are exactly the
      calls that are guaranteed to fail, and retrying them would burn the portal's
      shared operating-time budget every 15 seconds;
    * `NOT purge_pending` — "sync never runs while true" (§3): a run started here
      would race the purge and re-insert rows it has just deleted.

    `FOR UPDATE OF portal_sync SKIP LOCKED` is what makes several ticks (or several
    worker containers) safe without a distributed lock: a row another tick is already
    leasing is skipped, never waited on, so one slow tick cannot stall the others.

    `run_started_at = NULL` is set as part of the lease so the *next* expiry of this
    lease can tell a crashed run from a dispatch miss (§3).
    """
    if limit <= 0:
        return []
    owner = _sanitise_owner(owner)

    async with control_txn() as session:
        rows = (
            (
                await session.execute(
                    select(
                        PortalSync.portal_id,
                        PortalSync.sync_generation,
                        PortalSync.run_started_at,
                        PortalSync.lease_expires_at,
                    )
                    .join(Portal, Portal.id == PortalSync.portal_id)
                    .where(
                        Portal.status == "active",
                        Portal.token_status == "ok",  # noqa: S105 - a state enum (§3)
                        Portal.purge_pending.is_(False),
                        PortalSync.next_run_at <= func.now(),
                        or_(
                            PortalSync.lease_expires_at.is_(None),
                            PortalSync.lease_expires_at < func.now(),
                        ),
                    )
                    .order_by(PortalSync.next_run_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=PortalSync)
                )
            )
            .all()
        )
        if not rows:
            return []

        portal_ids = [int(row.portal_id) for row in rows]
        # A lease that expired while a run was in progress is a crashed run: the
        # process died between `run_started_at` and `release_lease`. A lease that
        # expired with `run_started_at IS NULL` was never dispatched (§3) and is
        # re-leased free of charge - charging it would eventually push a healthy
        # portal into the 6 h pause every time a container restarts.
        crashed = [
            int(row.portal_id)
            for row in rows
            if row.run_started_at is not None and row.lease_expires_at is not None
        ]

        await session.execute(
            update(PortalSync)
            .where(PortalSync.portal_id.in_(portal_ids))
            .values(
                lease_owner=owner,
                lease_expires_at=_LEASE_DEADLINE,
                run_started_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if crashed:
            await session.execute(
                update(PortalSync)
                .where(PortalSync.portal_id.in_(crashed))
                .values(
                    consecutive_failures=PortalSync.consecutive_failures + 1,
                    last_error_code="lease_expired",
                    last_error_text="the previous run did not release its lease",
                    last_error_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            log.warning(
                "sync: re-leasing portals whose run crashed",
                extra={"portal_ids": crashed, "owner": owner},
            )

    return [
        Fence(portal_id=int(row.portal_id), owner=owner, generation=int(row.sync_generation))
        for row in rows
    ]


def _is_throttle(error: BitrixError | None) -> bool:
    """§5.6: "Throttling is not failure".

    A 429/503 says the *shared* budget is exhausted, not that this portal is broken.
    Counting it as a failure would drop a legitimately busy backfill into the 6 h pause
    and stall the import it was making progress on. Branching on the type, never on the
    error string (errors.py owns that mapping).
    """
    return isinstance(error, (QueryLimitExceeded, OperationTimeLimit))


async def release_lease(
    fence: Fence,
    *,
    next_run_at: dt.datetime | None = None,
    error: BitrixError | None = None,
) -> None:
    """Clear the lease and schedule the next visit (§5.9 "On exit").

    `next_run_at` is ALWAYS written, whatever happened. Rule: every retry path in this
    codebase ends in a timestamp the tick reads, never in a loop inside the worker -
    so a failing portal costs one visit per interval instead of one per event loop
    iteration (§5.6, brief "never a hot loop").

    Failure accounting, all of it here because this is the single exit of a run:

    * success - `consecutive_failures` back to 0 and the last error cleared, so the
      settings page stops showing an error the portal has recovered from;
    * throttling - `throttle_hits + 1` only (§5.6);
    * anything else - `consecutive_failures + 1`, and once that reaches
      `FAILURES_BEFORE_PAUSE` the portal is parked for `FAILURE_PAUSE_SECONDS`
      regardless of what the caller asked for.

    The write is fenced like every other, but a lost fence is **logged, not raised**:
    it means the lease is already someone else's (or the tenant was reinstalled), and
    there is nothing of ours left to release. Raising here would replace a clean end of
    run with a spurious error on the way out.
    """
    now = dt.datetime.now(dt.UTC)
    due = next_run_at or now + dt.timedelta(seconds=settings.sync_interval_sec)
    paused = now + dt.timedelta(seconds=FAILURE_PAUSE_SECONDS)

    values: dict[str, Any] = {
        "lease_owner": None,
        "lease_expires_at": None,
        "run_started_at": None,
        "next_run_at": due,
    }
    if error is None:
        values["consecutive_failures"] = 0
        values["last_error_code"] = None
        values["last_error_text"] = None
        values["last_error_at"] = None
    else:
        values["last_error_code"] = (error.code or type(error).__name__)[:64]
        # `str(error)` renders code/status/description only - BitrixError keeps the
        # response body out of it on purpose (§6), so this cannot leak a token.
        values["last_error_text"] = str(error)
        values["last_error_at"] = func.now()
        if _is_throttle(error):
            values["throttle_hits"] = PortalSync.throttle_hits + 1
        else:
            values["consecutive_failures"] = PortalSync.consecutive_failures + 1
            values["next_run_at"] = case(
                (
                    PortalSync.consecutive_failures + 1 >= FAILURES_BEFORE_PAUSE,
                    paused,
                ),
                else_=due,
            )

    try:
        async with control_txn() as session:
            await fenced_update(session, fence, values)
    except FenceLost:
        log.warning(
            "sync: lease released by someone else before this run finished",
            extra={"portal_id": fence.portal_id, "owner": fence.owner},
        )
