"""Single async engine for the runtime role `ca_app`.

`ca_app` is NOBYPASSRLS (§3), so every connection handed out here is subject to
the tenant policies; that is the whole point of not giving the application the
owner role. One engine per process keeps the pool bounded — §5.9 dispatches
portal syncs with `asyncio.create_task` under a semaphore, and a per-task engine
would multiply Postgres connections by the concurrency limit.

The engine is built at import time from `settings` only (no connection is
opened until first use), which keeps the "no side effects at import" rule
intact: `pool_pre_ping` absorbs the connections a restarted Postgres drops.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine: AsyncEngine = create_async_engine(
    str(settings.database_url),
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
    echo=False,
)

# expire_on_commit=False: §5 code reads ORM attributes after the transaction
# scope closes, and a lazy refresh outside the greenlet context would raise
# MissingGreenlet rather than emit SQL.
session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=True,
)


async def dispose_engine() -> None:
    """Close the pool on shutdown (FastAPI lifespan / worker exit).

    Docker stops containers with SIGTERM; without this, sockets are torn down by
    the kernel and Postgres logs a connection reset for every pooled connection.
    """
    await engine.dispose()
