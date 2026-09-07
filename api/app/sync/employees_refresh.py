"""§7 - refresh the per-portal `employees` cache from `user.get`.

The dashboard, the filter list and the calls table all render a name for a
`PORTAL_USER_ID`. §7 names three writers of that cache; this is the third: the sync
visit's refresher, which resolves the placeholder rows the call upsert inserted
(`fetched_at IS NULL`) and re-reads the ones that have gone stale.

Order and shape are exactly §7's:

* **placeholders first, then stale rows**, which is what the `employees_stale_idx`
  (`portal_id, fetched_at NULLS FIRST`) index is ordered for. A brand-new employee whose
  calls just arrived gets a name on the next visit instead of waiting behind a full-cache
  refresh.
* **50 ids per `user.get` command**, batched by `bitrix/users.py::fetch_users` - one HTTP
  request per 2 500 ids, with the `ADMIN_MODE` retry that module owns.
* **`ACTIVE` is never filtered.** Dismissed employees own historical calls and must still
  resolve to a name; the flag is stored and the UI greys them. Filtering here would make
  every call of a departed employee render as "User #id" forever.
* **Ids `user.get` did not return get `found=false` and are retried daily**, not every
  cycle - a deleted user must not cost a command every four hours forever.

Everything is written inside ONE `tenant_txn` together with the fenced `last_employees_at`
stamp: `employees` carries FORCED row-level security bound to `app.portal_id` (§3) and
fails *silently* closed, so a write issued without tenant context would report success
having stored nothing, and the cache would look permanently empty.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError, ExpiredToken
from app.bitrix.users import ID_CHUNK, MAX_COMMANDS_PER_BATCH, fetch_users
from app.config import settings
from app.db.models import Employee
from app.db.session import tenant_txn
from app.logging import get_logger
from app.sync.head_fetch import now
from app.sync.lease import Fence, fenced_update

__all__ = [
    "MAX_IDS_PER_VISIT",
    "NOT_FOUND_RETRY_HOURS",
    "EmployeesRefreshOutcome",
    "run_employees_refresh",
]

log = get_logger(__name__)

#: One HTTP request per 50 commands x 50 ids (§7). Capping the visit here keeps the
#: refresher bounded on a 3 000-employee portal; the remainder is picked up next visit,
#: oldest `fetched_at` first, so the queue drains rather than starving its own tail.
MAX_IDS_PER_VISIT: Final[int] = ID_CHUNK * MAX_COMMANDS_PER_BATCH

#: §7: "Ids with no result get `found=false` and are retried daily instead of every cycle."
NOT_FOUND_RETRY_HOURS: Final[int] = 24

#: §3 column widths. An over-long value would abort the whole statement, so it is cut.
_NAME_MAX: Final[int] = 255
_URL_MAX: Final[int] = 2048

#: Rows per INSERT ... ON CONFLICT statement; keeps one statement's parameter count sane.
_WRITE_CHUNK: Final[int] = 500

_CONFLICT_KEY: Final[tuple[str, str]] = ("portal_id", "bx_user_id")


@dataclass(frozen=True)
class EmployeesRefreshOutcome:
    """What one employee-cache refresh resolved (§7)."""

    #: False when there were no placeholders and nothing was stale.
    ran: bool
    requested: int
    resolved: int
    #: Ids `user.get` returned nothing for; stored as `found=false`, retried daily.
    missing: int
    errors: tuple[BitrixError, ...]


_IDLE = EmployeesRefreshOutcome(
    ran=False, requested=0, resolved=0, missing=0, errors=()
)


def _trim(value: Any, limit: int = _NAME_MAX) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


async def _candidates(fence: Fence, *, ttl_hours: int, limit: int) -> list[int]:
    """Placeholders first, then stale rows (§7), under tenant context (§3).

    One query, ordered `fetched_at NULLS FIRST`, so the placeholder priority is the index
    order rather than two round trips. `found=false` rows use the daily threshold instead
    of the TTL: they are deleted or invisible users, and re-asking every four hours buys
    nothing but requests.
    """
    stamp = now()
    stale_at = stamp - dt.timedelta(hours=max(1, int(ttl_hours)))
    retry_at = stamp - dt.timedelta(hours=NOT_FOUND_RETRY_HOURS)
    query = (
        select(Employee.bx_user_id)
        .where(
            Employee.portal_id == fence.portal_id,
            or_(
                Employee.fetched_at.is_(None),
                and_(Employee.found.is_(True), Employee.fetched_at < stale_at),
                and_(Employee.found.is_(False), Employee.fetched_at < retry_at),
            ),
        )
        .order_by(Employee.fetched_at.asc().nulls_first())
        .limit(limit)
    )
    async with tenant_txn(fence.portal_id) as session:
        return [int(value) for value in (await session.execute(query)).scalars().all()]


async def _store(
    fence: Fence,
    *,
    records: Sequence[dict[str, Any]],
    missing: Sequence[int],
    stamp: dt.datetime,
) -> None:
    """Persist the refresh and the `last_employees_at` stamp in ONE fenced transaction.

    Together on purpose: the stamp is what decides when this job runs again, and a stamp
    committed without its rows would silence the refresher for another TTL while the cache
    still held placeholders. `FenceLost` propagates - a newer runner owns this portal.
    """
    async with tenant_txn(fence.portal_id) as session:
        for start in range(0, len(records), _WRITE_CHUNK):
            chunk = records[start : start + _WRITE_CHUNK]
            statement = pg_insert(Employee).values(list(chunk))
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=list(_CONFLICT_KEY),
                    set_={
                        "name": statement.excluded.name,
                        "last_name": statement.excluded.last_name,
                        "second_name": statement.excluded.second_name,
                        "work_position": statement.excluded.work_position,
                        "photo_url": statement.excluded.photo_url,
                        "active": statement.excluded.active,
                        "departments": statement.excluded.departments,
                        "found": statement.excluded.found,
                        "fetched_at": statement.excluded.fetched_at,
                    },
                )
            )
        if missing:
            # Only the two flag columns: a name learned earlier is kept, because a user who
            # has become invisible to us still owns historical calls that must render.
            await session.execute(
                update(Employee)
                .where(
                    Employee.portal_id == fence.portal_id,
                    Employee.bx_user_id.in_(list(missing)),
                )
                .values(found=False, fetched_at=stamp)
            )
        await fenced_update(session, fence, {"last_employees_at": stamp})


async def run_employees_refresh(
    fence: Fence,
    client: BitrixClient,
    *,
    ttl_hours: int | None = None,
    limit: int = MAX_IDS_PER_VISIT,
) -> EmployeesRefreshOutcome:
    """Resolve placeholder and stale `employees` rows for one portal (§7).

    `ExpiredToken` and `FenceLost` propagate (§5.8 refreshes once and retries; a lost fence
    means a newer runner owns this portal). Any other Bitrix24 failure is returned in the
    outcome: a portal whose `user.get` is refused still has perfectly good call data, and
    the visit must not be aborted over a cache of display names.
    """
    ids = await _candidates(
        fence,
        ttl_hours=ttl_hours if ttl_hours is not None else settings.employee_ttl_hours,
        limit=max(1, min(int(limit), MAX_IDS_PER_VISIT)),
    )
    if not ids:
        return _IDLE

    try:
        resolved = await fetch_users(client, ids)
    except ExpiredToken:
        raise
    except BitrixError as error:
        log.warning(
            "employees refresh failed",
            extra={"portal_id": fence.portal_id, "requested": len(ids), "error": error.code},
        )
        return EmployeesRefreshOutcome(
            ran=True, requested=len(ids), resolved=0, missing=0, errors=(error,)
        )

    stamp = now()
    wanted = set(ids)
    records = [
        {
            "portal_id": fence.portal_id,
            "bx_user_id": user_id,
            "name": _trim(record.get("name")),
            "last_name": _trim(record.get("last_name")),
            "second_name": _trim(record.get("second_name")),
            "work_position": _trim(record.get("work_position")),
            "photo_url": _trim(record.get("photo_url"), _URL_MAX),
            # Stored, never used to filter the request (§7): dismissed employees own
            # historical calls and the UI greys them with a "dismissed" badge.
            "active": bool(record.get("active", True)),
            "departments": list(record.get("departments") or []),
            "found": True,
            "fetched_at": stamp,
        }
        for user_id, record in sorted(resolved.items())
        # `user.get` can answer with ids we did not ask for on some builds; storing those
        # would fill the cache with users no call of this portal references.
        if user_id in wanted
    ]
    missing = [user_id for user_id in ids if user_id not in resolved]

    await _store(fence, records=records, missing=missing, stamp=stamp)
    log.info(
        "employees refreshed",
        extra={
            "portal_id": fence.portal_id,
            "requested": len(ids),
            "resolved": len(records),
            "missing": len(missing),
        },
    )
    return EmployeesRefreshOutcome(
        ran=True,
        requested=len(ids),
        resolved=len(records),
        missing=len(missing),
        errors=(),
    )
