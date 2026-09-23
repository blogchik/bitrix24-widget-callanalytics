"""`services/crm_grants.py`: the one control that may show a viewer more than Bitrix24 does.

Every assertion here is about a decision, not a calculation. A grant is the only thing in the
product that can widen what a person sees beyond the portal's own CRM permissions, so the
tests that matter are the ones that pin *who* may write one, *what* it may say, and which
viewers it must not reach.

The two-portal fixture already seeds a `portal` grant for `user_ids[1]` (every tenant table
carries a row so `test_isolation` has something to walk), so a test about "a viewer with no
grant" uses an id the fixture never touches.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import CrmItem, Portal
from app.db.session import control_txn, tenant_txn
from app.api.portal import _require_admin
from app.security.principal import Principal, PrincipalError
from app.services import crm_grants, crm_repo
from tests.conftest import TwoPortals

pytestmark = pytest.mark.asyncio

#: An id `_seed_portal` never assigns: `user_ids` are `index * 1000 + 1` and `+ 2`.
UNGRANTED: int = 4242
GRANTEE: int = 4343


def _access(grant: crm_grants.ViewerGrant) -> crm_repo.ViewerAccess:
    """What the routes hand the gates: at most one of grant and census, never both."""
    return crm_repo.ViewerAccess(grant=grant)


def _principal(portal_id: int, user_id: int, *, access: str, admin: bool = False) -> Principal:
    return Principal(
        portal_id=portal_id,
        member_id="0" * 32,
        user_id=user_id,
        is_admin=admin,
        access=access,
        timezone="UTC",
        lang="ru",
        placement="DEFAULT",
        entity=None,
        issued_at=0,
    )


async def _portal_row(portal_id: int) -> Portal:
    async with control_txn() as session:
        return (await session.execute(select(Portal).where(Portal.id == portal_id))).scalar_one()


async def _promote(portal_id: int) -> None:
    """The mirror only answers a promoted portal; a grant must not be able to change that."""
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET crm_mode = 'mirror' WHERE id = :pid"), {"pid": portal_id}
        )


async def _events(portal_id: int, kind: str) -> int:
    async with control_txn() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM portal_events WHERE portal_id = :pid AND kind = :k"
                    ),
                    {"pid": portal_id, "k": kind},
                )
            ).scalar_one()
        )


# --- who may write one -------------------------------------------------------------------


async def test_an_administrator_cannot_grant_themselves(two_portals: TwoPortals) -> None:
    """A self-grant can only be a mistake, and it would make the audit trail lie."""
    a = two_portals.a
    with pytest.raises(ValueError, match="themselves"):
        await crm_grants.set_grant(
            a.portal_id, a.user_ids[0], kind="portal", granted_by=a.user_ids[0]
        )


@pytest.mark.parametrize(
    ("kind", "departments", "match"),
    [
        ("departments", [], "at least one department"),
        ("portal", [5], "names no departments"),
        ("everything", [], "unknown grant kind"),
    ],
)
async def test_a_grant_that_contradicts_itself_is_refused(
    two_portals: TwoPortals, kind: str, departments: list[int], match: str
) -> None:
    """Refused at the single writer, so the CHECK constraints stay a backstop and not a 500."""
    a = two_portals.a
    with pytest.raises(ValueError, match=match):
        await crm_grants.set_grant(
            a.portal_id,
            GRANTEE,
            kind=kind,
            department_ids=departments,
            granted_by=a.user_ids[0],
        )


async def test_setting_and_clearing_a_grant_is_audited(two_portals: TwoPortals) -> None:
    """`portal_events` is what answers "who decided this, and when?" a year later."""
    a = two_portals.a
    before = await _events(a.portal_id, "crm_grant_set")

    grant = await crm_grants.set_grant(
        a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0], note="  covers Q4  "
    )
    assert grant.kind == "portal"
    assert grant.granted_by == a.user_ids[0], "the administrator is accountable, not the viewer"
    assert grant.note == "covers Q4", "the note is trimmed, not echoed raw"
    assert await _events(a.portal_id, "crm_grant_set") == before + 1

    assert await crm_grants.clear_grant(a.portal_id, GRANTEE, cleared_by=a.user_ids[0]) is True
    assert await crm_grants.load_grant(a.portal_id, GRANTEE) is None
    assert await _events(a.portal_id, "crm_grant_cleared") == 1
    # Clearing what is not there is not an error, and must not write a second audit row.
    assert await crm_grants.clear_grant(a.portal_id, GRANTEE, cleared_by=a.user_ids[0]) is False
    assert await _events(a.portal_id, "crm_grant_cleared") == 1


async def test_a_grant_never_crosses_a_portal(two_portals: TwoPortals) -> None:
    """The table is FORCED-RLS; this is the proof that the read honours it."""
    a, b = two_portals.a, two_portals.b
    await crm_grants.set_grant(a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0])
    assert await crm_grants.load_grant(a.portal_id, GRANTEE) is not None
    assert await crm_grants.load_grant(b.portal_id, GRANTEE) is None
    assert [g.user_id for g in await crm_grants.list_grants(b.portal_id)] == [b.user_ids[1]]


async def test_a_grant_never_makes_anybody_an_administrator(two_portals: TwoPortals) -> None:
    """The owner's condition, in one assertion: "everything except settings".

    A grant is about data. The settings page - and every endpoint that writes a grant - is
    gated on the JWT's `adm` claim, which comes from Bitrix24's `user.admin`, and nothing on
    the grant path reaches it. If this ever passes for a granted viewer, a person an
    administrator meant to show reports to can hand out grants of their own.
    """
    a = two_portals.a
    grant = await crm_grants.set_grant(
        a.portal_id,
        GRANTEE,
        kind="portal",
        covers_crm=True,
        covers_calls=True,
        granted_by=a.user_ids[0],
    )
    assert grant.covers_crm and grant.covers_calls, "the widest grant there is"

    viewer = _principal(a.portal_id, GRANTEE, access="own")
    assert viewer.is_admin is False
    with pytest.raises(PrincipalError) as refusal:
        await _require_admin(viewer)
    assert refusal.value.http_status == 403


# --- which viewers it reaches ------------------------------------------------------------


async def test_a_granted_employee_reads_the_mirror_and_an_ungranted_one_does_not(
    two_portals: TwoPortals,
) -> None:
    a = two_portals.a
    await _promote(a.portal_id)
    portal = await _portal_row(a.portal_id)

    granted = _principal(a.portal_id, GRANTEE, access="own")
    grant = await crm_grants.set_grant(
        a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0]
    )
    assert crm_repo.serves_mirror(portal, granted, _access(grant)) is True
    assert crm_repo.crm_scope(granted, _access(grant)) is None, "a portal grant is the whole mirror"

    ungranted = _principal(a.portal_id, UNGRANTED, access="own")
    assert crm_repo.serves_mirror(portal, ungranted, crm_repo.ViewerAccess()) is False
    with pytest.raises(PrincipalError) as refusal:
        crm_repo.crm_scope(ungranted, crm_repo.ViewerAccess())
    assert refusal.value.code == crm_repo.MIRROR_UNAVAILABLE


async def test_a_denied_viewer_is_refused_even_holding_a_grant(two_portals: TwoPortals) -> None:
    """`denied` is a decision about the whole app (§4.7), not about how much CRM to show."""
    a = two_portals.a
    await _promote(a.portal_id)
    portal = await _portal_row(a.portal_id)
    grant = await crm_grants.set_grant(
        a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0]
    )
    denied = _principal(a.portal_id, GRANTEE, access="denied")

    assert crm_repo.serves_mirror(portal, denied, _access(grant)) is False
    with pytest.raises(PrincipalError) as refusal:
        crm_repo.crm_scope(denied, _access(grant))
    assert refusal.value.http_status == 403


async def test_a_grant_cannot_open_a_portal_that_was_never_promoted(
    two_portals: TwoPortals,
) -> None:
    """A grant relaxes one condition. It cannot conjure a mirror that is not there."""
    a = two_portals.a
    portal = await _portal_row(a.portal_id)  # still crm_mode = 'sync'
    grant = await crm_grants.set_grant(
        a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0]
    )
    assert portal.crm_mode != "mirror"
    viewer = _principal(a.portal_id, GRANTEE, access="own")
    assert crm_repo.serves_mirror(portal, viewer, _access(grant)) is False


async def test_crm_analytics_off_beats_a_grant(two_portals: TwoPortals) -> None:
    a = two_portals.a
    await _promote(a.portal_id)
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portals SET crm_mode = 'off', crm_opt_out_at = now() WHERE id = :pid"
            ),
            {"pid": a.portal_id},
        )
    portal = await _portal_row(a.portal_id)
    grant = await crm_grants.set_grant(
        a.portal_id, GRANTEE, kind="portal", granted_by=a.user_ids[0]
    )
    viewer = _principal(a.portal_id, GRANTEE, access="own")
    assert crm_repo.serves_mirror(portal, viewer, _access(grant)) is False


# --- what a department grant selects -----------------------------------------------------


async def test_a_department_grant_selects_that_department_and_nothing_else(
    app_engine: AsyncEngine, two_portals: TwoPortals
) -> None:
    """Membership is read at query time from `employees.departments`, not frozen into the row.

    The seeded items alternate deal and lead and are assigned to `user_ids[0]` and
    `user_ids[1]`; putting only the first in department 12 must select only their records.
    """
    a = two_portals.a
    async with tenant_txn(a.portal_id) as session:
        await session.execute(
            text("UPDATE employees SET departments = :d WHERE portal_id = :pid AND bx_user_id = :u"),
            {"d": [12], "pid": a.portal_id, "u": a.user_ids[0]},
        )
        await session.execute(
            text("UPDATE employees SET departments = :d WHERE portal_id = :pid AND bx_user_id = :u"),
            {"d": [13], "pid": a.portal_id, "u": a.user_ids[1]},
        )

    grant = await crm_grants.set_grant(
        a.portal_id,
        GRANTEE,
        kind="departments",
        department_ids=[12],
        granted_by=a.user_ids[0],
    )
    predicate = crm_grants.grant_scope(a.portal_id, grant)
    assert predicate is not None, "a department grant is never the whole mirror"

    async with tenant_txn(a.portal_id) as session:
        selected = set(
            (
                await session.execute(
                    select(CrmItem.assigned_by_id).where(
                        CrmItem.portal_id == a.portal_id, predicate
                    )
                )
            )
            .scalars()
            .all()
        )
    assert selected == {a.user_ids[0]}, "department 13's records must not be selected"


async def test_a_department_nobody_belongs_to_selects_nothing(
    app_engine: AsyncEngine, two_portals: TwoPortals
) -> None:
    """Fail closed: an empty membership must select no rows, never every row."""
    a = two_portals.a
    grant = await crm_grants.set_grant(
        a.portal_id,
        GRANTEE,
        kind="departments",
        department_ids=[9999],
        granted_by=a.user_ids[0],
    )
    predicate = crm_grants.grant_scope(a.portal_id, grant)

    async with tenant_txn(a.portal_id) as session:
        count = (
            await session.execute(
                select(func.count()).select_from(CrmItem).where(
                    CrmItem.portal_id == a.portal_id, predicate
                )
            )
        ).scalar_one()
    assert count == 0
