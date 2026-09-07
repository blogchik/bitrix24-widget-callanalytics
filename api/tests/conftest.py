"""Test fixtures against the compose Postgres.

WHY two engines: the isolation and purge tests are only meaningful if the
application really is the NOBYPASSRLS `ca_app` role of §3. `app_engine` is
deliberately the *same* engine object `tenant_txn` / `control_txn` use, so a test
cannot accidentally prove isolation on a connection the production code never
takes. `owner_engine` (DATABASE_URL_MIGRATIONS, `ca_owner`) exists to run Alembic
and to inspect the catalog — note that §3 puts FORCE ROW LEVEL SECURITY on the
customer tables, so the owner is subject to the policies too and is not a way to
peek across tenants.

WHY the app engine's pool is disposed after every test: pytest-asyncio hands each
test its own event loop by default, and a pooled asyncpg connection bound to a
closed loop fails in ways that look like isolation bugs. Disposing keeps the suite
correct whatever loop scope the installed plugin version chooses.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.db.engine import dispose_engine
from app.db.engine import engine as _runtime_engine
from app.db.session import control_txn, tenant_txn

API_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The three tables that carry FORCED RLS (§3). Everything else is control plane.
TENANT_TABLES: Final[tuple[str, ...]] = ("calls", "employees", "crm_contexts")


def _as_async_url(url: str) -> str:
    """Force the asyncpg driver.

    DATABASE_URL_MIGRATIONS is consumed by Alembic, which may well be configured with
    a sync driver; the tests need the same database through an async engine.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    base = scheme.split("+", 1)[0]
    return f"{base}+asyncpg{sep}{rest}"


def _probe(url: str) -> str | None:
    """Return a human reason the database is unusable, or None when it answers."""

    async def _connect() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_connect())
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot test here"
        return f"{type(exc).__name__}: {exc}"
    return None


# pytest-asyncio <0.23 expects a suite to override `event_loop` when it wants control
# of the loop; 0.23+ deprecates the override and 1.0 dropped the fixture. Define it
# only where it is still the supported mechanism so no version warns about it. Nothing
# in this suite depends on it — every fixture below is either sync or function-scoped.
if hasattr(getattr(pytest_asyncio, "plugin", None), "event_loop"):

    @pytest.fixture(scope="session")
    def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
        loop = asyncio.new_event_loop()
        try:
            yield loop
        finally:
            loop.close()


@pytest.fixture(scope="session", autouse=True)
def _database_available() -> None:
    """Skip the whole suite, loudly, when the compose Postgres is not there."""
    for label, url in (
        ("DATABASE_URL_MIGRATIONS", _as_async_url(str(settings.database_url_migrations))),
        ("DATABASE_URL", str(settings.database_url)),
    ):
        reason = _probe(url)
        if reason is not None:
            pytest.skip(
                f"database unreachable via {label} ({reason}). "
                "Start it with `docker compose up -d postgres` and re-run."
            )


@pytest.fixture(scope="session")
def _migrated(_database_available: None) -> None:
    """`alembic upgrade head` once per session, as `ca_owner`.

    Run as a subprocess rather than through `alembic.command`: alembic/env.py owns its
    own event loop for the async engine, and driving that from inside a test session's
    loop is exactly the kind of nesting that hangs.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", "-c", str(API_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=str(API_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed:\n" + proc.stdout + proc.stderr,
            pytrace=False,
        )


@pytest.fixture()
def owner_engine(_migrated: None) -> AsyncEngine:
    """`ca_owner` engine for catalog inspection. NullPool: nothing survives a test."""
    return create_async_engine(
        _as_async_url(str(settings.database_url_migrations)),
        poolclass=NullPool,
    )


@pytest_asyncio.fixture()
async def app_engine(_migrated: None) -> AsyncIterator[AsyncEngine]:
    """The runtime `ca_app` engine — the one `tenant_txn`/`control_txn` actually use."""
    yield _runtime_engine
    await dispose_engine()


@dataclass(frozen=True)
class PortalFixture:
    """One seeded tenant: the control row plus a handful of RLS-protected rows."""

    portal_id: int
    member_id: str
    user_ids: tuple[int, ...]
    bx_ids: tuple[int, ...]
    entity_ids: tuple[int, ...]

    @property
    def calls(self) -> int:
        return len(self.bx_ids)

    @property
    def employees(self) -> int:
        return len(self.user_ids)

    @property
    def crm_contexts(self) -> int:
        return len(self.entity_ids)


@dataclass(frozen=True)
class TwoPortals:
    a: PortalFixture
    b: PortalFixture


_CALL_CODES: Final[tuple[str, ...]] = ("200", "304", "603", "200")


async def _seed_portal(index: int) -> PortalFixture:
    member_id = uuid.uuid4().hex  # matches portals_member_id_fmt: 32 lowercase hex
    domain = f"portal{index}.bitrix24.test"
    async with control_txn() as session:
        portal_id = int(
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO portals (member_id, domain, client_endpoint, lang, timezone)
                        VALUES (:member_id, :domain, :client_endpoint, 'en', 'UTC')
                        RETURNING id
                        """
                    ),
                    {
                        "member_id": member_id,
                        "domain": domain,
                        "client_endpoint": f"https://{domain}/rest/",
                    },
                )
            ).scalar_one()
        )
        await session.execute(
            text("INSERT INTO portal_sync (portal_id) VALUES (:pid)"),
            {"pid": portal_id},
        )

    base = index * 1000
    user_ids = (base + 1, base + 2)
    bx_ids = tuple(base + 10 + n for n in range(len(_CALL_CODES)))
    entity_ids = (base + 100, base + 200)
    start = datetime.now(UTC) - timedelta(days=1)

    # Customer rows: only reachable under tenant context (§3 policies).
    async with tenant_txn(portal_id) as session:
        for offset, (bx_id, code) in enumerate(zip(bx_ids, _CALL_CODES, strict=True)):
            await session.execute(
                text(
                    """
                    INSERT INTO calls (portal_id, bx_id, call_id, call_type, call_start_date,
                                       call_duration, call_failed_code, portal_user_id,
                                       phone_number, portal_number)
                    VALUES (:pid, :bx_id, :call_id, :call_type, :started,
                            :duration, :code, :user_id, :phone, :line)
                    """
                ),
                {
                    "pid": portal_id,
                    "bx_id": bx_id,
                    "call_id": f"call-{portal_id}-{bx_id}",
                    "call_type": 1 + (offset % 2),
                    "started": start + timedelta(minutes=offset),
                    "duration": 30 * (offset + 1),
                    "code": code,
                    "user_id": user_ids[offset % len(user_ids)],
                    "phone": f"+9989000{index}{offset:02d}",
                    "line": f"line-{index}",
                },
            )
        for position, user_id in enumerate(user_ids):
            await session.execute(
                text(
                    """
                    INSERT INTO employees (portal_id, bx_user_id, name, last_name,
                                           work_position, active, found, fetched_at)
                    VALUES (:pid, :uid, :name, :last, 'Operator', true, true, now())
                    """
                ),
                {
                    "pid": portal_id,
                    "uid": user_id,
                    "name": f"User{position}",
                    "last": f"Portal{index}",
                },
            )
        for position, entity_id in enumerate(entity_ids):
            await session.execute(
                text(
                    """
                    INSERT INTO crm_contexts (portal_id, entity_type, entity_id, entity_keys,
                                              activity_ids, resolved_by_user_id)
                    VALUES (:pid, :etype, :eid, CAST(:keys AS jsonb),
                            CAST(:acts AS bigint[]), :uid)
                    """
                ),
                {
                    "pid": portal_id,
                    "etype": ("DEAL", "LEAD")[position % 2],
                    "eid": entity_id,
                    "keys": f'[["CONTACT", {entity_id}]]',
                    # A Python list, not a '{1,2}' literal: asyncpg encodes arrays
                    # from a sized iterable and rejects the text form outright.
                    "acts": [entity_id + 1],
                    "uid": user_ids[0],
                },
            )

    return PortalFixture(
        portal_id=portal_id,
        member_id=member_id,
        user_ids=user_ids,
        bx_ids=bx_ids,
        entity_ids=entity_ids,
    )


async def _drop_portal(portal_id: int) -> None:
    """Tear down explicitly under tenant context.

    Not by letting `ON DELETE CASCADE` do it: whether a cascade is exempt from RLS is
    a Postgres implementation detail, and a test suite that leaves rows behind on a
    version that is not exempt would poison the next run silently.
    """
    async with tenant_txn(portal_id) as session:
        for table in TENANT_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )
    async with control_txn() as session:
        await session.execute(
            text("DELETE FROM portal_sync WHERE portal_id = :pid"), {"pid": portal_id}
        )
        await session.execute(text("DELETE FROM portals WHERE id = :pid"), {"pid": portal_id})


@pytest_asyncio.fixture()
async def two_portals(app_engine: AsyncEngine) -> AsyncIterator[TwoPortals]:
    """Two fully seeded tenants — the minimum needed to prove a leak would be visible."""
    portal_a = await _seed_portal(1)
    portal_b = await _seed_portal(2)
    try:
        yield TwoPortals(a=portal_a, b=portal_b)
    finally:
        await _drop_portal(portal_a.portal_id)
        await _drop_portal(portal_b.portal_id)
