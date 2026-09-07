"""The `calls` upsert: one fenced tenant transaction per call (§5.5, §5.3, §5.9).

Everything here exists to protect one invariant: **a single unexpected value from
Bitrix24 must never stall the sync cursor** (decision 11). `calls` deliberately has no
CHECK constraints on Bitrix-controlled values (§3), but the column types are still
real - a 300-character `PHONE_NUMBER`, a missing `CALL_START_DATE`, a duplicate `ID`
returned by two sub-commands of one batch - and any of those aborts a multi-row INSERT.
Without the machinery below, that abort would roll back the cursor as well, the next
visit would re-fetch the same page, and the portal would be wedged on one poison row
forever, silently, for as long as the row exists.

The four defences, in the order they apply:

1. **Dedupe by `bx_id` inside the chunk.** A row deleted between two sub-commands of
   the same batch shifts the offsets and returns twice; Postgres answers a repeated
   conflict key in ONE statement with "ON CONFLICT DO UPDATE command cannot affect row
   a second time" - a hard abort of a perfectly ordinary situation.
2. **Chunks of 500 inside a SAVEPOINT.** The savepoint is what makes step 3 possible at
   all: without it, the first integrity error would poison the whole transaction and
   there would be nothing left to retry into.
3. **Row-by-row retry on an integrity/data error**, quarantining the offender as
   `portal_events(row_rejected, {bx_id, reason})` and counting it in
   `portal_sync.rejected_rows` (§3: "Non-zero is a support signal, never a blocker").
4. **Rows and cursor in ONE transaction, closed by the fence** (§5.3, decision 13). A
   crash therefore costs at most one re-upserted batch, and a run that lost its lease
   or whose tenant was purged/reinstalled has its rows rolled back with the cursor.

`content_changed_at` compares old against new over the data columns **except
`call_record_url`** (§5.5). If that URL turns out to be per-read volatile - it carries
`auth`/`token`/`sig` parameters the parser strips, and there is no promise the rest is
stable - including it would mark every row in the 72 h rescan window as changed every
hour, which is both a pointless write amplification and a "what changed?" signal that
means nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import case, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import Call, PortalSync
from app.db.session import tenant_txn
from app.logging import get_logger
from app.services.employees import upsert_placeholders
from app.services.portals import record_event
from app.sync.lease import Fence, fenced_update

__all__ = ["CHUNK_SIZE", "DATA_COLUMNS", "UpsertResult", "upsert_calls"]

log = get_logger(__name__)

#: §5.5. Small enough that one poison row costs a cheap row-by-row retry, large enough
#: that 500 x ~28 bind parameters stays far below Postgres' 65 535 parameter ceiling.
CHUNK_SIZE: Final[int] = 500

#: Columns of `calls` that are NOT payload: the identity, the two GENERATED columns
#: (Postgres owns them - §3), and our own bookkeeping. `record_recheck_count` is in
#: here because it is the recording-recheck budget (§5.7), not something a re-read may
#: reset; `first_seen_at` because "when we first saw it" must survive every re-upsert.
_NON_DATA_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "portal_id",
        "bx_id",
        "has_record",
        "result_group",
        "record_recheck_count",
        "refresh_requested",
        "first_seen_at",
        "last_synced_at",
        "content_changed_at",
    }
)

#: Derived from the model rather than typed out, so a column added to §3 is upserted
#: without a second edit here - the failure mode of a hand-kept list is a column that
#: silently stops being written.
DATA_COLUMNS: Final[tuple[str, ...]] = tuple(
    column.name for column in Call.__table__.columns if column.name not in _NON_DATA_COLUMNS
)

#: §5.5 / §3 `call_record_url`: excluded from the change comparison only, still written.
_VOLATILE_COLUMNS: Final[frozenset[str]] = frozenset({"call_record_url"})

_COMPARED_COLUMNS: Final[tuple[str, ...]] = tuple(
    name for name in DATA_COLUMNS if name not in _VOLATILE_COLUMNS
)

_ACCEPTED_KEYS: Final[frozenset[str]] = frozenset(DATA_COLUMNS) | {"bx_id"}

#: One `portal_events` row per rejected row is the design (§5.5), but a portal whose
#: whole history is unparsable must not turn one visit into 100 000 audit rows. Beyond
#: this many, a single summary event is written instead.
_MAX_REJECT_EVENTS: Final[int] = 50


@dataclass(frozen=True)
class UpsertResult:
    """What one `upsert_calls` call did, for the caller's cursor maths and logging."""

    #: Rows that reached `calls` (inserted or updated - Postgres does not tell us which
    #: without a RETURNING round trip that would buy nothing).
    inserted_or_updated: int
    #: Rows added to `portal_sync.rejected_rows` by this call: the parser's rejects
    #: passed in plus the ones the database refused.
    quarantined: int
    #: Highest / lowest `bx_id` **seen**, including quarantined rows. Deliberately not
    #: "highest written": §5.5 requires a rejected row to be skipped, not to block the
    #: cursor, so a cursor derived from these still steps over the poison row.
    max_bx_id: int | None
    min_bx_id: int | None


def _reason(exc: Exception) -> str:
    """A short, payload-free reason for `portal_events(row_rejected)`.

    Only the exception class and the SQLSTATE: the driver's message quotes the offending
    value, and §3 is explicit that `portal_events.details` never carries payloads (the
    table outlives `rest_log` retention and is not redacted on read).
    """
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    return f"{name}:{state}" if state else name


def _prepare(
    rows: Sequence[dict[str, Any]], portal_id: int
) -> tuple[list[dict[str, Any]], list[tuple[int | None, str]]]:
    """Normalise, validate the key and dedupe by `bx_id` (§5.5 defence 1).

    Every returned mapping carries the *same* key set, because one multi-row INSERT has
    one column list; a row that simply omitted a field would otherwise shift the whole
    VALUES clause. A missing field therefore means SQL NULL, which is the correct read:
    `statistic.get` returns the full record on every read, so a field that disappeared
    really is gone.

    Later duplicates win: two copies of one `bx_id` in a batch are two reads of the same
    record, and the later page is the later read.
    """
    deduped: dict[int, dict[str, Any]] = {}
    rejected: list[tuple[int | None, str]] = []
    unknown_keys: set[str] = set()

    for raw in rows:
        try:
            bx_id = int(raw["bx_id"])
        except (KeyError, TypeError, ValueError):
            # No usable upsert key: nothing to conflict on, nothing to quarantine by id.
            rejected.append((None, "missing_or_non_integer_bx_id"))
            continue
        if bx_id <= 0:
            rejected.append((bx_id, "non_positive_bx_id"))
            continue
        unknown_keys |= set(raw) - _ACCEPTED_KEYS
        values: dict[str, Any] = {"portal_id": portal_id, "bx_id": bx_id}
        for name in DATA_COLUMNS:
            values[name] = raw.get(name)
        deduped[bx_id] = values

    if unknown_keys:
        # A parser change that outran this module. Loud, but not fatal: dropping an
        # unknown key is strictly better than aborting the batch it arrived in.
        log.warning(
            "upsert: dropping keys that are not `calls` columns",
            extra={"portal_id": portal_id, "keys": sorted(unknown_keys)},
        )
    return list(deduped.values()), rejected


def _insert(chunk: Sequence[dict[str, Any]]) -> Any:
    """`INSERT ... ON CONFLICT (portal_id, bx_id) DO UPDATE ...` for one chunk (§5.5).

    `content_changed_at` is a row-wise `IS DISTINCT FROM` over the compared columns, so
    NULL-to-NULL counts as "unchanged" (a plain `<>` would evaluate to NULL there and
    quietly never fire). `refresh_requested` is cleared on every write because the
    on-demand refresh flag of §5.7 has, by definition, just been served.
    """
    statement = pg_insert(Call).values(list(chunk))
    excluded = statement.excluded

    changed = tuple_(*[Call.__table__.c[name] for name in _COMPARED_COLUMNS]).is_distinct_from(
        tuple_(*[excluded[name] for name in _COMPARED_COLUMNS])
    )
    updates: dict[str, Any] = {name: excluded[name] for name in DATA_COLUMNS}
    updates["last_synced_at"] = func.now()
    updates["refresh_requested"] = False
    updates["content_changed_at"] = case((changed, func.now()), else_=Call.content_changed_at)

    return statement.on_conflict_do_update(
        index_elements=["portal_id", "bx_id"], set_=updates
    )


async def _write_chunk(
    session: AsyncSession, chunk: Sequence[dict[str, Any]]
) -> tuple[int, list[tuple[int | None, str]]]:
    """One chunk in a SAVEPOINT; on a value the database refuses, retry row by row.

    Only `IntegrityError` and `DataError` trigger the retry, never `DBAPIError` at
    large: an `OperationalError` (connection gone, deadlock, statement timeout) is not
    the fault of any particular row, and retrying 500 rows individually against a dead
    connection would quarantine the entire chunk - turning a transient outage into
    permanent data loss.
    """
    savepoint = await session.begin_nested()
    try:
        await session.execute(_insert(chunk))
    except (IntegrityError, DataError):
        await savepoint.rollback()
    else:
        await savepoint.commit()
        return len(chunk), []

    written = 0
    rejected: list[tuple[int | None, str]] = []
    for row in chunk:
        row_savepoint = await session.begin_nested()
        try:
            await session.execute(_insert([row]))
        except (IntegrityError, DataError) as exc:
            await row_savepoint.rollback()
            rejected.append((int(row["bx_id"]), _reason(exc)))
        else:
            await row_savepoint.commit()
            written += 1
    return written, rejected


async def _record_rejections(
    session: AsyncSession, portal_id: int, rejected: Sequence[tuple[int | None, str]]
) -> None:
    """`portal_events(row_rejected, {bx_id, reason})`, capped (§5.5).

    Written in the caller's transaction but OUTSIDE the failed savepoints, which is the
    only place they survive: an event added inside the savepoint that rolled back would
    disappear with it, leaving `rejected_rows` incremented and no audit trail at all.
    """
    for bx_id, reason in rejected[:_MAX_REJECT_EVENTS]:
        await record_event(
            session, portal_id, "row_rejected", details={"bx_id": bx_id, "reason": reason}
        )
    overflow = len(rejected) - _MAX_REJECT_EVENTS
    if overflow > 0:
        await record_event(
            session,
            portal_id,
            "row_rejected",
            details={"suppressed": overflow, "reason": "reject_event_cap"},
        )


def _extent(
    rows: Sequence[dict[str, Any]], rejected: Iterable[tuple[int | None, str]]
) -> tuple[int | None, int | None]:
    seen = [int(row["bx_id"]) for row in rows]
    seen += [int(bx_id) for bx_id, _ in rejected if bx_id is not None]
    if not seen:
        return None, None
    return max(seen), min(seen)


async def upsert_calls(
    fence: Fence,
    rows: Sequence[dict[str, Any]],
    *,
    cursor_values: dict[str, Any] | None = None,
    rejected: Sequence[tuple[int | None, str]] = (),
) -> UpsertResult:
    """Upsert `rows` and commit `cursor_values` in ONE fenced tenant transaction (§5.3).

    `rejected` carries the parser's per-row failures (§5.5): they never reached the
    database, but they must still be counted and audited, and - crucially - the cursor
    must still step over them, or the next visit re-fetches the same unparsable page
    forever.

    Order inside the transaction is deliberate: rows, then the employee placeholders
    they reference (§7 writer (a) - same transaction, so a crash cannot commit calls
    whose users the refresher will never look for), then the audit events, and the
    fence **last**. Fencing last means a run that lost its lease still pays for the
    work, but the rows it wrote are rolled back with it - and that ordering is what
    lets the caller pass a `cursor_values` computed from the rows themselves.

    Raises `FenceLost` (from `fenced_update`) when the lease or `sync_generation` moved.
    Nothing is committed in that case, which is the entire point of decision 13.
    """
    portal_id = fence.portal_id
    prepared, key_rejects = _prepare(rows, portal_id)
    all_rejects: list[tuple[int | None, str]] = [*rejected, *key_rejects]

    written = 0
    quarantined_ids: set[int] = set()
    # §3 decision 8: re-entered for THIS transaction. RLS is transaction-local and
    # fails silently closed, so a chunk written outside this block would insert nothing
    # and report success.
    async with tenant_txn(portal_id) as session:
        for start in range(0, len(prepared), CHUNK_SIZE):
            chunk = prepared[start : start + CHUNK_SIZE]
            chunk_written, chunk_rejects = await _write_chunk(session, chunk)
            written += chunk_written
            all_rejects.extend(chunk_rejects)
            quarantined_ids.update(bx_id for bx_id, _ in chunk_rejects if bx_id is not None)

        if written:
            # Only for rows that actually landed: a placeholder for a user id that came
            # from a rejected row would send `employees_refresh` after an id no stored
            # call references (§7).
            await upsert_placeholders(
                session,
                portal_id,
                (
                    row["portal_user_id"]
                    for row in prepared
                    if row.get("portal_user_id") and row["bx_id"] not in quarantined_ids
                ),
            )
        if all_rejects:
            await _record_rejections(session, portal_id, all_rejects)

        values: dict[str, Any] = dict(cursor_values or {})
        if all_rejects:
            values["rejected_rows"] = PortalSync.rejected_rows + len(all_rejects)
        await fenced_update(session, fence, values)

    if all_rejects:
        log.warning(
            "upsert: rows quarantined",
            extra={"portal_id": portal_id, "count": len(all_rejects), "written": written},
        )

    max_bx_id, min_bx_id = _extent(prepared, all_rejects)
    return UpsertResult(
        inserted_or_updated=written,
        quarantined=len(all_rejects),
        max_bx_id=max_bx_id,
        min_bx_id=min_bx_id,
    )
