"""`app/db/tenancy.py` is the one list of customer tables - these tests keep it honest.

Two halves. The consumers (the purge, the fixtures, the DML lint) must read that list and
not keep a copy, because a copy is how a new table ends up isolated but never purged. And
the migrated catalog must match it, which is the same check CI runs as
`python -m app.tools.check_tenancy` - here with negative controls, since a check that
reports nothing on a clean database would also report nothing if it were broken.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models import Base
from app.db.tenancy import (
    CONTROL_PLANE_TABLES,
    NON_PORTAL_LEADING_INDEXES,
    TENANT_TABLES,
    catalog_problems,
    check_tenancy,
)
from app.sync import purge
from tests import conftest, test_registry_lint

_PROBE = "zz_tenancy_probe"


def test_the_lists_are_disjoint_and_name_mapped_tables() -> None:
    assert not set(TENANT_TABLES) & set(CONTROL_PLANE_TABLES)
    for name in (*TENANT_TABLES, *CONTROL_PLANE_TABLES):
        assert name in Base.metadata.tables, f"{name} is registered but has no model"
    for reason in (*CONTROL_PLANE_TABLES.values(), *NON_PORTAL_LEADING_INDEXES.values()):
        assert reason.strip(), "every exemption says why"


def test_every_consumer_reads_the_one_list() -> None:
    """A copy anywhere is a table that is isolated but not purged, or purged but not linted."""
    assert tuple(table.name for table in purge._TENANT_TABLES) == TENANT_TABLES
    assert conftest.TENANT_TABLES is TENANT_TABLES
    assert test_registry_lint.TENANT_TABLES is TENANT_TABLES


async def test_the_migrated_catalog_matches_the_registry(app_engine: AsyncEngine) -> None:
    """The CI gate, run as the runtime role: every rule holds on the real schema."""
    async with app_engine.connect() as conn:
        problems = await check_tenancy(conn)
    assert not problems, "\n".join(problems)


@pytest_asyncio.fixture()
async def owner_conn(owner_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A `ca_owner` connection inside a transaction that is always rolled back.

    The negative controls change the schema; DDL is transactional in Postgres, so nothing
    they do outlives the test.
    """
    async with owner_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()
    await owner_engine.dispose()


async def test_an_unregistered_portal_table_is_reported(owner_conn: AsyncConnection) -> None:
    await owner_conn.execute(text(f"CREATE TABLE {_PROBE} (portal_id bigint NOT NULL, note text)"))
    problems = await catalog_problems(owner_conn)
    assert any(p.startswith(f"{_PROBE}:") and "neither" in p for p in problems), problems


async def test_a_registered_tenant_table_without_its_policy_is_reported(
    owner_conn: AsyncConnection,
) -> None:
    await owner_conn.execute(text(f"CREATE TABLE {_PROBE} (portal_id bigint NOT NULL, note text)"))
    await owner_conn.execute(text(f"CREATE INDEX {_PROBE}_note_idx ON {_PROBE} (note)"))
    problems = "\n".join(
        await catalog_problems(owner_conn, tenant_tables=(*TENANT_TABLES, _PROBE))
    )
    assert f"{_PROBE}: row-level security must be ENABLED and FORCED" in problems
    assert f"{_PROBE}: expected exactly one tenant policy, found 0" in problems
    assert f"index {_PROBE}_note_idx does not lead with portal_id" in problems


async def test_a_policy_bound_to_something_else_is_reported(owner_conn: AsyncConnection) -> None:
    await owner_conn.execute(text(f"CREATE TABLE {_PROBE} (portal_id bigint NOT NULL)"))
    await owner_conn.execute(text(f"ALTER TABLE {_PROBE} ENABLE ROW LEVEL SECURITY"))
    await owner_conn.execute(text(f"ALTER TABLE {_PROBE} FORCE ROW LEVEL SECURITY"))
    await owner_conn.execute(text(f"CREATE POLICY {_PROBE}_open ON {_PROBE} USING (true)"))
    problems = "\n".join(
        await catalog_problems(owner_conn, tenant_tables=(*TENANT_TABLES, _PROBE))
    )
    assert f"{_PROBE}: policy {_PROBE}_open is not bound to app.portal_id" in problems


async def test_rls_on_a_control_plane_table_is_reported(owner_conn: AsyncConnection) -> None:
    """The worker reads the control plane without a tenant context; RLS there reads as empty."""
    await owner_conn.execute(text("ALTER TABLE portal_events ENABLE ROW LEVEL SECURITY"))
    problems = await catalog_problems(owner_conn)
    assert any(p.startswith("portal_events: row-level security") for p in problems), problems


async def test_a_stale_index_exemption_is_reported(owner_conn: AsyncConnection) -> None:
    """An exemption naming an index that is gone would quietly cover the next one to take the name."""
    problems = await catalog_problems(
        owner_conn,
        non_portal_leading_indexes=(*NON_PORTAL_LEADING_INDEXES, "zz_no_such_idx"),
    )
    assert any(p.startswith("zz_no_such_idx: listed in NON_PORTAL_LEADING_INDEXES") for p in problems)


@pytest.mark.parametrize("name", ["calls", "portals"])
async def test_a_registered_table_missing_from_the_database_is_reported(
    owner_conn: AsyncConnection, name: str
) -> None:
    await owner_conn.execute(text(f"ALTER TABLE {name} RENAME TO {_PROBE}"))
    problems = await catalog_problems(owner_conn)
    assert f"{name}: registered in app/db/tenancy.py but not in the database" in problems
