"""Writes to the `employees` cache (§7).

§7 names three writers, and this module owns two of them:

* **(b) the viewer**, upserted from `user.current` on every `POST /app/` open
  (`upsert_viewer`) - so the person looking at the dashboard has a name in the
  filter list from their very first open, before any sync has run;
* **(a) placeholders**, inserted for `PORTAL_USER_ID`s the call upsert has never
  seen (`upsert_placeholders`), which is what lets `employees_refresh` (§5, the
  third writer, milestone 4) find work with `fetched_at IS NULL` instead of
  running `SELECT DISTINCT portal_user_id FROM calls` every cycle.

Both write under **tenant context**. `employees` carries FORCED row-level security
bound to `app.portal_id` (§3), and RLS fails *silently* closed: an INSERT issued
from `control_txn()` is rejected by the WITH CHECK clause and an UPDATE matches
nothing while reporting success. `upsert_viewer` therefore opens its own
`tenant_txn`; `upsert_placeholders` takes the caller's session because the sync
upsert (§5.5) must insert the placeholders in the SAME transaction as the calls
that reference them - a separate transaction could commit the calls and lose the
placeholders to a crash.

What is deliberately NOT here: reads. §4.7 routes every read of customer data
through `services/calls_repo.py`, where `scope_filter` is applied exactly once.

`tests/test_registry_lint.py` lists this module as the owner of `employees` DML;
a write from anywhere else fails the build.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.db.models import Employee
from app.db.session import tenant_txn
from app.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from app.bitrix.identity import Identity

__all__ = ["upsert_placeholders", "upsert_viewer"]

log = get_logger(__name__)

#: §3 column widths. A longer value would abort the INSERT and, in the open handler,
#: turn a cosmetic cache write into a failed app open - so it is cut, not trusted.
_NAME_MAX: Final[int] = 255

#: One open can only ever describe one user; the sync layer's placeholder batches are
#: what could be large, so only that path is chunked.
_PLACEHOLDER_CHUNK: Final[int] = 1000

_CONFLICT_KEY: Final[tuple[str, str]] = ("portal_id", "bx_user_id")


def _trim(value: str | None, limit: int = _NAME_MAX) -> str | None:
    """Empty -> NULL, over-long -> cut (§3 widths)."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


async def upsert_viewer(portal_id: int, identity: Identity) -> None:
    """Cache the person who just opened the app (§7 writer (b), §4.4 step 4).

    The record comes from the `user.current` command of the open batch, so it is
    exactly the `user_brief` subset §3 stores - no email, no phone: the app never
    requests the scope that would return them.

    Two columns are left alone on conflict, on purpose:

    * `active` - `user.current` does not report it for the caller in every build, and
      overwriting a `false` written by `employees_refresh` would resurrect a dismissed
      employee in the UI (§7 keeps them, greyed, because their calls remain).
    * `departments` - only `user.get` returns `UF_DEPARTMENT`; writing `{}` here would
      erase what the refresher learned.
    * `phone_inner` - the same argument as `departments`, and the one the viewer would
      notice: it is their own extension disappearing from the filter on every app open.
      `user.current` is not documented to report `UF_PHONE_INNER`, and this app has never
      measured that it does, so `employees_refresh` stays its only writer.

    `fetched_at` is set because this row is real data, not a placeholder: §7's refresh
    job takes `NULL` rows first, and a viewer that keeps re-inserting itself as
    "unfetched" would jump that queue on every single open.

    Never raises: a failed cache write must not turn a working open into an error page
    (§4.11). The row is re-created by the next open or by the next employees refresh.
    """
    values: dict[str, Any] = {
        "portal_id": portal_id,
        "bx_user_id": int(identity.user_id),
        "name": _trim(identity.name),
        "last_name": _trim(identity.last_name),
        "second_name": _trim(identity.second_name),
        "work_position": _trim(identity.work_position),
        # Text column: only whitespace-trimmed, never truncated to 255.
        "photo_url": _trim(identity.photo_url, limit=2048),
        "found": True,
        "fetched_at": func.now(),
    }
    updates = {key: value for key, value in values.items() if key not in _CONFLICT_KEY}

    try:
        async with tenant_txn(portal_id) as session:
            await session.execute(
                pg_insert(Employee)
                .values(**values)
                .on_conflict_do_update(index_elements=list(_CONFLICT_KEY), set_=updates)
            )
    except Exception:
        log.warning(
            "employees: viewer upsert failed",
            exc_info=True,
            extra={"portal_id": portal_id, "user_id": identity.user_id},
        )


async def upsert_placeholders(
    session: AsyncSession, portal_id: int, user_ids: Iterable[int]
) -> int:
    """Insert `fetched_at IS NULL` rows for unseen user ids (§7 writer (a)).

    Called by the statistics upsert (§5.5) with every `PORTAL_USER_ID` of a chunk,
    inside the chunk's own `tenant_txn`; that is why the session is a parameter and
    why nothing is committed here.

    `ON CONFLICT DO NOTHING` is the whole point: an id that is already cached - a real
    row from `user.get` or an earlier placeholder - must not be reset to a placeholder,
    which would make `employees_refresh` re-fetch the entire portal on every sync
    visit. Returns how many rows were actually inserted, for the caller's log line.

    Unlike `upsert_viewer` this one **does** propagate a failure: it runs inside the
    caller's transaction, where swallowing an error would leave a half-written chunk
    behind (and, worse, would leave the caller believing the placeholders exist).
    """
    wanted = sorted({int(uid) for uid in user_ids if uid is not None and int(uid) > 0})
    if not wanted:
        return 0

    inserted = 0
    for start in range(0, len(wanted), _PLACEHOLDER_CHUNK):
        chunk = wanted[start : start + _PLACEHOLDER_CHUNK]
        # RETURNING counts what was actually inserted: with ON CONFLICT DO NOTHING the
        # rows that already existed are simply absent from it.
        result = await session.execute(
            pg_insert(Employee)
            .values([{"portal_id": portal_id, "bx_user_id": uid} for uid in chunk])
            .on_conflict_do_nothing(index_elements=list(_CONFLICT_KEY))
            .returning(Employee.bx_user_id)
        )
        inserted += len(result.scalars().all())
    return inserted
