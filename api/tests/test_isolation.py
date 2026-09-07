"""§10 step 1 — structural proof that tenant isolation is the database's job.

Every assertion here is raw SQL on purpose. The ORM, the repositories and
`scope_filter` are all layers that a future refactor can bypass; the §3 policies
are not. If these tests pass, a forgotten `WHERE portal_id = …` cannot leak a
tenant, and a job that forgets `tenant_txn` writes nothing rather than writing to
the wrong tenant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from tests.conftest import TENANT_TABLES, TwoPortals

pytestmark = pytest.mark.asyncio


async def test_tenant_context_sees_only_its_own_rows(two_portals: TwoPortals) -> None:
    """1. Under `tenant_txn(A)` the three customer tables contain only A."""
    a, b = two_portals.a, two_portals.b

    async with tenant_txn(a.portal_id) as session:
        for table in TENANT_TABLES:
            portal_ids = set(
                (
                    await session.execute(
                        text(f"SELECT DISTINCT portal_id FROM {table}")  # noqa: S608
                    )
                )
                .scalars()
                .all()
            )
            assert portal_ids == {a.portal_id}, f"{table} leaked rows of another portal"

        assert (
            await session.execute(text("SELECT count(*) FROM calls"))
        ).scalar_one() == a.calls
        assert (
            await session.execute(text("SELECT count(*) FROM employees"))
        ).scalar_one() == a.employees
        assert (
            await session.execute(text("SELECT count(*) FROM crm_contexts"))
        ).scalar_one() == a.crm_contexts

        # An explicit predicate for B is not a way around the policy either.
        assert (
            await session.execute(
                text("SELECT count(*) FROM calls WHERE portal_id = :pid"),
                {"pid": b.portal_id},
            )
        ).scalar_one() == 0


async def test_control_context_sees_zero_rows_not_an_error(two_portals: TwoPortals) -> None:
    """2. With no tenant context the tables read as empty — silently. Fail closed."""
    async with control_txn() as session:
        for table in TENANT_TABLES:
            count = (
                await session.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            ).scalar_one()
            assert count == 0, f"{table} was readable without a tenant context"

        # The control plane itself stays fully visible: that asymmetry is the design.
        visible = (
            await session.execute(
                text("SELECT count(*) FROM portals WHERE id IN (:x, :y)"),
                {"x": two_portals.a.portal_id, "y": two_portals.b.portal_id},
            )
        ).scalar_one()
        assert visible == 2


async def test_insert_for_another_portal_is_rejected(two_portals: TwoPortals) -> None:
    """3. WITH CHECK: A cannot write a row stamped with B's id, however it tries."""
    a, b = two_portals.a, two_portals.b

    with pytest.raises(DBAPIError) as excinfo:
        async with tenant_txn(a.portal_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO calls (portal_id, bx_id, call_start_date, call_duration)
                    VALUES (:pid, 999999, now(), 1)
                    """
                ),
                {"pid": b.portal_id},
            )
    assert "row-level security" in str(excinfo.value).lower()

    async with tenant_txn(b.portal_id) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM calls"))
        ).scalar_one() == b.calls


async def test_update_and_delete_cannot_reach_another_portal(two_portals: TwoPortals) -> None:
    """4. Cross-tenant UPDATE/DELETE match nothing — no error, no rows, B untouched."""
    a, b = two_portals.a, two_portals.b

    async with tenant_txn(a.portal_id) as session:
        updated = await session.execute(
            text("UPDATE calls SET call_duration = 99999 WHERE portal_id = :pid"),
            {"pid": b.portal_id},
        )
        deleted = await session.execute(
            text("DELETE FROM calls WHERE portal_id = :pid"), {"pid": b.portal_id}
        )
        emp_deleted = await session.execute(
            text("DELETE FROM employees WHERE portal_id = :pid"), {"pid": b.portal_id}
        )
        ctx_deleted = await session.execute(
            text("DELETE FROM crm_contexts WHERE portal_id = :pid"), {"pid": b.portal_id}
        )
        assert updated.rowcount == 0
        assert deleted.rowcount == 0
        assert emp_deleted.rowcount == 0
        assert ctx_deleted.rowcount == 0

    async with tenant_txn(b.portal_id) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM calls"))
        ).scalar_one() == b.calls
        assert (
            await session.execute(text("SELECT count(*) FROM employees"))
        ).scalar_one() == b.employees
        assert (
            await session.execute(text("SELECT count(*) FROM crm_contexts"))
        ).scalar_one() == b.crm_contexts
        assert (
            await session.execute(
                text("SELECT count(*) FROM calls WHERE call_duration = 99999")
            )
        ).scalar_one() == 0

    # A is intact too: a rejected cross-tenant statement must not be a self-inflicted wipe.
    async with tenant_txn(a.portal_id) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM calls"))
        ).scalar_one() == a.calls


async def test_runtime_role_cannot_bypass_rls(
    app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    """5. The whole chapter rests on this: the runtime login is NOBYPASSRLS (§3).

    Asserted for the role the application actually connects as, not for the literal
    name `ca_app`, because a deployment that points DATABASE_URL at a bypassing role
    would make every test above pass while the production database leaked.
    """
    async with app_engine.connect() as conn:
        app_role, bypasses = (
            await conn.execute(
                text(
                    """
                    SELECT r.rolname, r.rolbypassrls
                    FROM pg_roles r
                    WHERE r.rolname = current_user
                    """
                )
            )
        ).one()
        assert bypasses is False, f"runtime role {app_role!r} can bypass RLS"
        assert (
            await conn.execute(text("SELECT usesuper FROM pg_user WHERE usename = current_user"))
        ).scalar_one() is False

    # And the ops-provisioned role really is the one §3 names, when it exists.
    async with owner_engine.connect() as conn:
        ca_app_bypass = (
            await conn.execute(
                text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'ca_app'")
            )
        ).scalar_one_or_none()
    if ca_app_bypass is not None:
        assert ca_app_bypass is False, "ca_app was created without NOBYPASSRLS"
