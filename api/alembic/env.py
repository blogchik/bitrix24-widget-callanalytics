"""Alembic environment.

Deliberately synchronous. §3 splits the two database identities: the runtime role
``ca_app`` is ``NOBYPASSRLS`` and holds no DDL rights, so migrations must connect as
``ca_owner`` - the role that owns every object and therefore may create tables,
policies and grants. That is exactly what ``settings.database_url_migrations``
points at (the ``postgresql+psycopg`` sync URL), which is why no async engine and
no ``asyncio`` plumbing appear here.

``metadata.create_all`` is never called: the migration, not the ORM, owns the
schema (generated columns, FORCED RLS policies and COMMENTs of §3 have no ORM
representation). ``app.db.models`` is imported only so ``--autogenerate`` has
something to diff against.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL never lives in alembic.ini - it carries a password (§3).
config.set_main_option("sqlalchemy.url", settings.database_url_migrations)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a connection (``alembic upgrade head --sql``).

    Kept working so ops can review the DDL of a release before it touches a
    production portal database.
    """
    context.configure(
        url=settings.database_url_migrations,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection as ``ca_owner``."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
