"""Data destruction: tenant purge and the two retention jobs (§5.9, §6, brief rule 7).

This module deletes the data an uninstalled customer is entitled to have removed, and
it is the one place in the codebase where **"the statement succeeded" is not evidence
that anything happened**.

`calls`, `employees` and `crm_contexts` carry ENABLED + FORCED row-level security bound
to the transaction-local GUC `app.portal_id`, and the runtime role is `NOBYPASSRLS`
(§3, decision 8). With the GUC unset the policy predicate is NULL, so a `DELETE`
matches zero rows **and reports success**. A purge written as the obvious loop -
"delete a chunk until a chunk comes back empty" - therefore finishes instantly from a
control transaction, clears `purge_pending` and writes `portal_events(purge_done)`
while every row is still on disk: brief rule 7 violated, with the audit trail claiming
the opposite. `tests/test_purge.py` locks that trap in; this implementation answers it
with three rules that together make silence impossible:

1. every count and every delete runs inside `tenant_txn(portal_id)`, **opened afresh
   for each chunk** (`SET LOCAL` dies with its transaction, so one long `async with`
   would cover only the first statement);
2. a **pre-count per table** under that context, and a table whose pre-count was > 0
   whose first `DELETE` affected 0 rows aborts as `purge_incomplete` - never as
   "already empty";
3. a **final count**, under the same context, that must be 0 before `purge_pending`
   is cleared.

Chunking is by `ctid` and 10 000 rows (§5.9): `ctid` works identically for the
surrogate-key table and the two composite-key ones, RLS applies to the sub-select as
well, and a bounded delete keeps the transaction (and its locks, and the WAL it
generates) small enough that a purge of a 500 k-row tenant cannot block the api.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

from sqlalchemy import ColumnElement, Table, and_, delete, literal_column, null, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql import Select, func
from sqlalchemy.sql.selectable import NamedFromClause

from app.config import settings
from app.db.models import Call, CrmContext, Employee, Portal, PortalSync, RestLog
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger
from app.services.portals import record_event

__all__ = [
    "CHUNK_ROWS",
    "PurgeOutcome",
    "purge_crm_contexts",
    "purge_portal_data",
    "purge_rest_log",
    "redact_on_clean",
]

log = get_logger(__name__)

#: §5.9: "DELETE ... LIMIT 10000 per transaction until none".
CHUNK_ROWS: Final[int] = 10_000

#: §5.7 does not apply here; §6 fixes 30 days for the resolved-CRM cache.
CRM_CONTEXT_MAX_AGE_DAYS: Final[int] = 30

#: A `purge_incomplete` leaves `purge_pending = true`, and §5.9's tick picks purge work
#: with `SELECT id FROM portals WHERE purge_pending LIMIT 1` on every visit. Without a
#: cooldown, a purge that cannot make progress (a lock it never wins, a policy
#: misconfiguration) would be retried every tick forever - a hot loop against our own
#: database. The retry stays automatic, it is just paced.
INCOMPLETE_RETRY_SECONDS: Final[int] = 3600

#: The three FORCED-RLS tables of §3, in a fixed order so the audit counts are
#: comparable between runs.
_TENANT_TABLES: Final[tuple[Table, ...]] = (
    cast("Table", Call.__table__),
    cast("Table", Employee.__table__),
    cast("Table", CrmContext.__table__),
)

#: Guard against an unbounded loop if a delete keeps reporting rows it never removes.
#: 10 000 x 10 000 = 100 M rows is far beyond any plausible tenant; hitting it is a bug
#: report, not a state to keep spinning in.
_MAX_CHUNKS: Final[int] = 10_000

_PURGE_INCOMPLETE: Final[str] = "purge_incomplete"


@dataclass(frozen=True)
class PurgeOutcome:
    """What one `purge_portal_data` visit did - the input to §5.9's audit event."""

    portal_id: int
    #: Rows visible under tenant context before any delete, per table name.
    pre_counts: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)
    #: Rows still visible under tenant context afterwards. Must be all-zero to finish.
    final_counts: dict[str, int] = field(default_factory=dict)
    #: True when the purge could not prove it emptied the tenant (§5.9).
    incomplete: bool = False
    #: True when this visit did nothing because a recent visit already failed.
    skipped: bool = False
    #: Bodies of `rest_log` rows blanked by `purge_bodies` (§5.9, data[CLEAN]=1).
    bodies_redacted: int = 0

    @property
    def verified_empty(self) -> bool:
        """True only when a final count was actually taken and came back zero.

        A skipped visit counted nothing, so it must never answer "empty" - that is the
        same false reassurance the whole module exists to prevent.
        """
        return not self.incomplete and not self.skipped and not any(self.final_counts.values())

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def _chunk_delete(
    table: Table,
    portal_id: int,
    *,
    extra: Callable[[NamedFromClause], ColumnElement[bool]] | None = None,
) -> Any:
    """`DELETE FROM t WHERE t.ctid IN (SELECT ctid FROM t AS a WHERE ... LIMIT n)`.

    The sub-select is written against an explicit alias so SQLAlchemy cannot decide to
    correlate it with the DELETE target and drop its FROM clause - which would turn the
    bounded delete into an unbounded one.

    `ctid` rather than the primary key: `employees` and `crm_contexts` have composite
    keys, and a single expression that works for all three tables is one fewer place
    for a per-table variant to go wrong.

    `extra` is a callable, not a ready expression, so an additional predicate is built
    against *this* alias object; a second `table.alias("purge_victim")` created by the
    caller would render a duplicate name into the same FROM clause.
    """
    victim = table.alias("purge_victim")
    predicate: ColumnElement[bool] = victim.c.portal_id == portal_id
    if extra is not None:
        predicate = and_(predicate, extra(victim))
    inner: Select[Any] = (
        select(literal_column("purge_victim.ctid"))
        .select_from(victim)
        .where(predicate)
        .limit(CHUNK_ROWS)
    )
    return delete(table).where(literal_column(f"{table.name}.ctid").in_(inner))


async def _count(table: Table, portal_id: int) -> int:
    """Count under THIS tenant's context - the only honest count of its rows (§5.9)."""
    async with tenant_txn(portal_id) as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(table).where(table.c.portal_id == portal_id)
                )
            ).scalar_one()
        )


async def _recently_failed(portal_id: int) -> bool:
    """True while a `purge_incomplete` from the last hour should still be left alone."""
    async with control_txn() as session:
        row = (
            await session.execute(
                select(PortalSync.last_error_code, PortalSync.last_error_at).where(
                    PortalSync.portal_id == portal_id
                )
            )
        ).one_or_none()
    if row is None or row.last_error_code != _PURGE_INCOMPLETE or row.last_error_at is None:
        return False
    age = dt.datetime.now(dt.UTC) - row.last_error_at
    return bool(age.total_seconds() < INCOMPLETE_RETRY_SECONDS)


async def purge_portal_data(portal_id: int, *, force: bool = False) -> PurgeOutcome:
    """Delete every customer row of one tenant and PROVE it (§5.9, brief rule 7).

    `force=True` skips the `purge_incomplete` cooldown; it exists for an operator
    re-running a purge by hand after fixing whatever blocked it, never for the tick.

    On success: `portals.purge_pending = false`, `purge_bodies = false`, and
    `portal_events(purge_done)` with the counts. On failure: both flags are left
    exactly as they were, `portal_sync.last_error_code = 'purge_incomplete'`, and
    `portal_events(purge_incomplete)` - so the next visit retries from committed state,
    which is the whole durability model of §5.9 (no queue, work derived from columns).

    NOT deleted here: the `portals` row itself (§3 - "kept forever; uninstall only flips
    status and wipes API tokens"), `portal_events` (the audit outlives the data) and
    `rest_log` rows (retention job below; only their bodies go, and only on
    `purge_bodies`).
    """
    if not force and await _recently_failed(portal_id):
        log.warning(
            "purge: skipping, a recent attempt reported purge_incomplete",
            extra={"portal_id": portal_id},
        )
        return PurgeOutcome(portal_id=portal_id, skipped=True)

    async with control_txn() as session:
        purge_bodies = bool(
            (
                await session.execute(select(Portal.purge_bodies).where(Portal.id == portal_id))
            ).scalar_one_or_none()
        )

    pre_counts: dict[str, int] = {}
    deleted: dict[str, int] = {}
    incomplete = False

    for table in _TENANT_TABLES:
        pre_counts[table.name] = await _count(table, portal_id)
        removed = 0
        first_chunk: int | None = None
        for _ in range(_MAX_CHUNKS):
            # A FRESH transaction per chunk: `SET LOCAL app.portal_id` dies with the
            # transaction that issued it, so reusing one session across commits would
            # leave every chunk after the first running with no tenant context - and
            # deleting nothing, silently (§3 decision 8).
            async with tenant_txn(portal_id) as session:
                result = await session.execute(_chunk_delete(table, portal_id))
            rows = int(cast("CursorResult[Any]", result).rowcount or 0)
            if first_chunk is None:
                first_chunk = rows
            removed += rows
            if rows == 0:
                break
        else:
            log.error(
                "purge: chunk limit reached, giving up this visit",
                extra={"portal_id": portal_id, "table": table.name, "deleted": removed},
            )
            incomplete = True

        deleted[table.name] = removed
        if pre_counts[table.name] > 0 and not first_chunk:
            # The §5.9 assertion: rows were visible a moment ago and the delete matched
            # none of them. Something took our tenant context away; reporting "done"
            # here is the exact failure this whole module is shaped to prevent.
            log.error(
                "purge: rows exist but the first DELETE matched nothing",
                extra={
                    "portal_id": portal_id,
                    "table": table.name,
                    "pre_count": pre_counts[table.name],
                },
            )
            incomplete = True

    final_counts = {table.name: await _count(table, portal_id) for table in _TENANT_TABLES}
    if any(final_counts.values()):
        incomplete = True

    bodies_redacted = 0
    if purge_bodies and not incomplete:
        # Only once the rows are provably gone: blanking the moderation bodies of a
        # tenant whose data survived would destroy evidence and fix nothing (§6).
        bodies_redacted = await redact_on_clean(portal_id)

    outcome = PurgeOutcome(
        portal_id=portal_id,
        pre_counts=pre_counts,
        deleted=deleted,
        final_counts=final_counts,
        incomplete=incomplete,
        bodies_redacted=bodies_redacted,
    )
    await _finish(outcome)
    return outcome


async def _finish(outcome: PurgeOutcome) -> None:
    """Write the audit event and flip (or refuse to flip) `purge_pending` (§5.9)."""
    details: dict[str, Any] = {
        "pre_counts": outcome.pre_counts,
        "deleted": outcome.deleted,
        "final_counts": outcome.final_counts,
        "bodies_redacted": outcome.bodies_redacted,
    }
    async with control_txn() as session:
        if outcome.incomplete:
            await session.execute(
                update(PortalSync)
                .where(PortalSync.portal_id == outcome.portal_id)
                .values(
                    last_error_code=_PURGE_INCOMPLETE,
                    last_error_text="purge could not prove the tenant tables are empty",
                    last_error_at=func.now(),
                )
            )
            await record_event(session, outcome.portal_id, _PURGE_INCOMPLETE, details=details)
            return

        await session.execute(
            update(Portal)
            .where(Portal.id == outcome.portal_id)
            .values(purge_pending=False, purge_bodies=False)
        )
        await session.execute(
            update(PortalSync)
            .where(PortalSync.portal_id == outcome.portal_id)
            .values(last_error_code=None, last_error_text=None, last_error_at=None)
        )
        await record_event(session, outcome.portal_id, "purge_done", details=details)

    log.info(
        "purge: finished",
        extra={
            "portal_id": outcome.portal_id,
            "deleted": outcome.total_deleted,
            "incomplete": outcome.incomplete,
        },
    )


async def redact_on_clean(portal_id: int) -> int:
    """NULL `rest_log.request`/`response` for one portal, keeping the rows (§5.9, §6).

    `data[CLEAN]=1` at uninstall means "remove this portal's data". The exchange log
    itself is a moderation artefact - method, URL, status, timing - and stays; only the
    bodies, which are the part that carried the customer's payloads, go. Chunked like
    the deletes so a portal with a week of traffic cannot produce one enormous UPDATE.

    `rest_log` is a control-plane table (no RLS, §3), so this runs in `control_txn` -
    and unlike the deletes above, a zero row count here really does mean "nothing left
    to blank".
    """
    blanked = 0
    for _ in range(_MAX_CHUNKS):
        async with control_txn() as session:
            victims = (
                select(RestLog.id)
                .where(
                    RestLog.portal_id == portal_id,
                    or_(RestLog.request.is_not(None), RestLog.response.is_not(None)),
                )
                .limit(CHUNK_ROWS)
            )
            result = await session.execute(
                update(RestLog)
                .where(RestLog.id.in_(victims))
                # `null()`, never Python None: SQLAlchemy's JSON types default to
                # `none_as_null=False`, so `request=None` writes the JSON value `null`
                # into the JSONB column instead of SQL NULL. The body would survive as
                # `'null'::jsonb`, the `IS NOT NULL` predicate would keep matching it,
                # and this loop would spin until its guard - a data-retention bug and a
                # hot loop in one line.
                .values(request=null(), response=null())
                .execution_options(synchronize_session=False)
            )
        rows = int(cast("CursorResult[Any]", result).rowcount or 0)
        blanked += rows
        if rows == 0:
            break
    return blanked


async def purge_rest_log() -> int:
    """Daily retention: drop `rest_log` rows older than the configured window (§6).

    `config.py` refuses a retention below 3 days, so this can only ever delete rows the
    moderation floor no longer requires. Ordered by `ts` so `rest_log_ts_idx` drives
    the sub-select; batched at 10 000 so the daily job never holds a long transaction
    against the table the api writes on every single REST call.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.rest_log_retention_days)
    removed = 0
    for _ in range(_MAX_CHUNKS):
        async with control_txn() as session:
            victims = (
                select(RestLog.id)
                .where(RestLog.ts < cutoff)
                .order_by(RestLog.ts)
                .limit(CHUNK_ROWS)
            )
            result = await session.execute(delete(RestLog).where(RestLog.id.in_(victims)))
        rows = int(cast("CursorResult[Any]", result).rowcount or 0)
        removed += rows
        if rows == 0:
            break
    if removed:
        log.info("rest_log purge", extra={"deleted": removed, "cutoff": cutoff.isoformat()})
    return removed


async def _portal_ids() -> Sequence[int]:
    async with control_txn() as session:
        return [
            int(pid)
            for pid in (await session.execute(select(Portal.id).order_by(Portal.id)))
            .scalars()
            .all()
        ]


async def purge_crm_contexts() -> int:
    """Daily retention: drop `crm_contexts` rows older than 30 days (§6).

    WHY this iterates portals instead of issuing one global DELETE: `crm_contexts` is
    RLS-bound, so there is no transaction from which "all portals' old rows" is even
    visible. A global statement would delete nothing and report success - the same trap
    `purge_portal_data` exists to close. One short transaction per portal per chunk is
    the price of the guarantee, and `crm_contexts_age_idx` keeps each one cheap.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=CRM_CONTEXT_MAX_AGE_DAYS)
    table = cast("Table", CrmContext.__table__)
    removed = 0
    for portal_id in await _portal_ids():
        for _ in range(_MAX_CHUNKS):
            async with tenant_txn(portal_id) as session:
                result = await session.execute(
                    _chunk_delete(
                        table,
                        portal_id,
                        extra=lambda victim: victim.c.resolved_at < cutoff,
                    )
                )
            rows = int(cast("CursorResult[Any]", result).rowcount or 0)
            removed += rows
            if rows == 0:
                break
    if removed:
        log.info("crm_contexts purge", extra={"deleted": removed, "cutoff": cutoff.isoformat()})
    return removed
