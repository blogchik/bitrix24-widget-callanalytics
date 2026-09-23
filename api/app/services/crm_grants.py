"""Administrator decisions about who may read the CRM mirror (0005, §4.14).

WHY this exists next to `crm_repo.crm_scope` rather than inside it: a grant is not evidence.
Everything else that decides what a viewer sees is derived from Bitrix24 - `user.admin` at
open, the statistics probe, and (later) the per-viewer census. A grant is a person's
decision, it is stored because Bitrix24 has nowhere to store it, and **it may be wider than
Bitrix24's own CRM permissions**. Bitrix24 exposes no CRM roles to read instead
(`crm.role.list` and every sibling answer ERROR_METHOD_NOT_FOUND on a real portal), so an
administrator who wants a supervisor to see the whole funnel has no other way to say so.

Three rules follow from that, and they are the whole module:

1. **A grant REPLACES the derived scope, it never merges with it.** Merging would make
   "why can this person see that?" unanswerable; replacing leaves exactly one answer, and
   `portal_events` holds who gave it and when.
2. **Only an administrator may write one, and every write is audited.** `granted_by` is the
   administrator, never the viewer, so the row says who is accountable.
3. **A grant relaxes one condition and no others.** The portal must still be promoted to
   `crm_mode = 'mirror'` and CRM analytics must still be on; a grant cannot conjure a mirror
   that is not there.

The department form resolves through `employees.departments`, which the employee refresh
already keeps up to date, rather than through a stored copy of the department tree: a person
moved between departments in Bitrix24 must change what they can see at the next refresh,
not at the next time somebody remembers to edit a grant.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Call, CrmItem, Employee, ViewerGrantRow
from app.db.session import tenant_txn
from app.logging import get_logger
from app.services.portals import record_event

__all__ = [
    "GRANT_KINDS",
    "KIND_DEPARTMENTS",
    "KIND_PORTAL",
    "MAX_DEPARTMENTS",
    "MAX_NOTE_CHARS",
    "ViewerGrant",
    "calls_scope",
    "clear_grant",
    "grant_scope",
    "list_grants",
    "load_grant",
    "set_grant",
]

_log = get_logger(__name__)

KIND_PORTAL: Final[str] = "portal"
KIND_DEPARTMENTS: Final[str] = "departments"
#: The CHECK constraint 0005 writes is the authority; this is the same list for validation.
GRANT_KINDS: Final[tuple[str, ...]] = (KIND_PORTAL, KIND_DEPARTMENTS)

#: A grant naming more departments than a portal has is a mistake, not a use case. The cap
#: keeps a hostile body from turning one row into an unbounded `&&` against every employee.
MAX_DEPARTMENTS: Final[int] = 64
#: `note` is free text an administrator writes for the next administrator. Capped because it
#: is rendered back on the settings page and stored forever.
MAX_NOTE_CHARS: Final[int] = 500


@dataclass(frozen=True)
class ViewerGrant:
    """One administrator decision, as the settings page and the predicate both read it."""

    user_id: int
    kind: str
    department_ids: tuple[int, ...]
    #: The Deals and Sources reports.
    covers_crm: bool
    #: The Summary, By hour and call list pages. A wider disclosure: a call row carries the
    #: customer's phone number, and Bitrix24 said this viewer may see only their own.
    covers_calls: bool
    granted_by: int
    granted_at: dt.datetime
    note: str

    def as_json(self) -> dict[str, Any]:
        """The wire form. `granted_by` travels so the page can name who is accountable."""
        return {
            "user_id": self.user_id,
            "kind": self.kind,
            "department_ids": list(self.department_ids),
            "covers_crm": self.covers_crm,
            "covers_calls": self.covers_calls,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat(),
            "note": self.note,
        }


def _row(row: ViewerGrantRow) -> ViewerGrant:
    return ViewerGrant(
        user_id=row.user_id,
        kind=row.kind,
        department_ids=tuple(row.department_ids or ()),
        covers_crm=bool(row.covers_crm),
        covers_calls=bool(row.covers_calls),
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        note=row.note,
    )


async def load_grant(portal_id: int, user_id: int) -> ViewerGrant | None:
    """This viewer's grant, or None. One primary-key read under tenant context.

    `viewer_grants` carries FORCED row-level security, so a read outside `tenant_txn`
    returns nothing at all rather than failing - which would read as "no grant" and quietly
    narrow a viewer instead of raising. Every caller here opens the context.
    """
    async with tenant_txn(portal_id) as session:
        row = (
            await session.execute(
                select(ViewerGrantRow).where(
                    ViewerGrantRow.portal_id == portal_id,
                    ViewerGrantRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
    return None if row is None else _row(row)


async def list_grants(portal_id: int) -> list[ViewerGrant]:
    """Every grant on this portal, newest decision first - the settings page's table."""
    async with tenant_txn(portal_id) as session:
        rows = (
            (
                await session.execute(
                    select(ViewerGrantRow)
                    .where(ViewerGrantRow.portal_id == portal_id)
                    .order_by(ViewerGrantRow.granted_at.desc(), ViewerGrantRow.user_id)
                )
            )
            .scalars()
            .all()
        )
    return [_row(row) for row in rows]


async def set_grant(
    portal_id: int,
    user_id: int,
    *,
    kind: str,
    department_ids: Sequence[int] = (),
    covers_crm: bool = True,
    covers_calls: bool = False,
    granted_by: int,
    note: str = "",
) -> ViewerGrant:
    """Create or replace one viewer's grant, and audit the decision.

    Validation is here rather than at the route because this is the single writer: the CHECK
    constraints in 0005 are the backstop, and a `ValueError` at this layer is a 400 the route
    renders, not a 500 from the driver.
    """
    if kind not in GRANT_KINDS:
        raise ValueError(f"unknown grant kind: {kind!r}")
    departments = sorted({int(value) for value in department_ids})
    if kind == KIND_DEPARTMENTS and not departments:
        raise ValueError("a departments grant names at least one department")
    if kind == KIND_PORTAL and departments:
        raise ValueError("a portal-wide grant names no departments")
    if len(departments) > MAX_DEPARTMENTS:
        raise ValueError(f"a grant names at most {MAX_DEPARTMENTS} departments")
    if not covers_crm and not covers_calls:
        # A grant that opens nothing is not a narrower grant, it is a deleted one, and
        # `clear_grant` is how a caller says that.
        raise ValueError("a grant opens at least one area")
    if user_id == granted_by:
        # An administrator already sees the whole mirror; a self-grant can only be a mistake
        # or an attempt to make the audit trail say something it does not mean.
        raise ValueError("an administrator cannot grant themselves")
    text = note.strip()[:MAX_NOTE_CHARS]

    values = {
        "portal_id": portal_id,
        "user_id": user_id,
        "kind": kind,
        "department_ids": departments,
        "covers_crm": bool(covers_crm),
        "covers_calls": bool(covers_calls),
        "granted_by": granted_by,
        "note": text,
    }
    async with tenant_txn(portal_id) as session:
        await session.execute(
            pg_insert(ViewerGrantRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["portal_id", "user_id"],
                # `granted_at` is refreshed on purpose: a replaced grant is a new decision by
                # whoever made it, and the page orders by it.
                set_={
                    "kind": kind,
                    "department_ids": departments,
                    "covers_crm": bool(covers_crm),
                    "covers_calls": bool(covers_calls),
                    "granted_by": granted_by,
                    "note": text,
                    "granted_at": func.now(),
                },
            )
        )
        await record_event(
            session,
            portal_id,
            "crm_grant_set",
            user_id=granted_by,
            details={
                "subject": user_id,
                "kind": kind,
                "departments": departments,
                "crm": bool(covers_crm),
                "calls": bool(covers_calls),
            },
        )
        row = (
            await session.execute(
                select(ViewerGrantRow).where(
                    ViewerGrantRow.portal_id == portal_id,
                    ViewerGrantRow.user_id == user_id,
                )
            )
        ).scalar_one()
        grant = _row(row)
    _log.info(
        "crm_grants: grant set",
        # The subject and the kind, never the note: it is free text an administrator wrote.
        extra={
            "portal_id": portal_id,
            "subject": user_id,
            "kind": kind,
            "crm": bool(covers_crm),
            "calls": bool(covers_calls),
        },
    )
    return grant


async def clear_grant(portal_id: int, user_id: int, *, cleared_by: int) -> bool:
    """Remove one viewer's grant. True when a row went, False when there was none.

    Removing a grant does not narrow the viewer to nothing: it returns them to whatever the
    derived scope says, which today is the live read.
    """
    async with tenant_txn(portal_id) as session:
        # `RETURNING` rather than `rowcount`: the async `Result` does not carry one, and the
        # id coming back is a stronger statement anyway - the row that went is named, not
        # merely counted, which is what the audit row below claims.
        removed = (
            await session.execute(
                delete(ViewerGrantRow)
                .where(
                    ViewerGrantRow.portal_id == portal_id,
                    ViewerGrantRow.user_id == user_id,
                )
                .returning(ViewerGrantRow.user_id)
            )
        ).scalar_one_or_none() is not None
        if removed:
            await record_event(
                session,
                portal_id,
                "crm_grant_cleared",
                user_id=cleared_by,
                details={"subject": user_id},
            )
    if removed:
        _log.info(
            "crm_grants: grant cleared", extra={"portal_id": portal_id, "subject": user_id}
        )
    return removed


def _department_members(portal_id: int, grant: ViewerGrant) -> Any:
    """The user ids of the departments this grant names, as a subquery.

    A subquery rather than a resolved list, and that is the point: membership is read at
    query time from `employees.departments`, so a person who moved department in Bitrix24
    stops seeing the old one's records as soon as the employee refresh notices, without
    anybody editing the grant. An empty membership makes the `IN` match nothing, which
    fails closed.
    """
    return (
        select(Employee.bx_user_id)
        .where(
            Employee.portal_id == portal_id,
            Employee.departments.overlap(list(grant.department_ids)),
        )
        .scalar_subquery()
    )


def calls_scope(portal_id: int, grant: ViewerGrant) -> ColumnElement[bool] | None:
    """The `calls` row predicate this grant produces, or None for "every call".

    The telephony half of a grant, and the one an administrator should think hardest about:
    `calls_repo.scope_filter` otherwise pins a non-administrator to their own rows because
    Bitrix24's statistics probe said so, and a call row carries the customer's phone number.
    Opening it overrides Bitrix24's answer rather than reading it.

    A NULL `portal_user_id` - a statistic row with no user - is in nobody's department and
    so is not selected by the department form, exactly as `scope_filter`'s own `own`
    predicate excludes it.
    """
    if grant.kind == KIND_PORTAL:
        return None
    return Call.portal_user_id.in_(_department_members(portal_id, grant))


def grant_scope(portal_id: int, grant: ViewerGrant) -> ColumnElement[bool] | None:
    """The CRM row predicate this grant produces, or None for "the whole mirror".

    `None` means exactly what it means everywhere else in `crm_repo`: no extra term. It is
    returned only for `KIND_PORTAL`, which is the one grant that says so.

    The department form goes through `_department_members`, which says why it is a subquery.
    A NULL `assigned_by_id` is in no subquery either, which is also correct: an unassigned
    record is nobody's department.
    """
    if grant.kind == KIND_PORTAL:
        return None
    return CrmItem.assigned_by_id.in_(_department_members(portal_id, grant))
