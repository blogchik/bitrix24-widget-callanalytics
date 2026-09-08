"""The two transaction scopes. There is no third way to reach the database.

§1 decision 8: `calls`, `employees` and `crm_contexts` carry ENABLED + FORCED
row-level security bound to the transaction-local GUC `app.portal_id`, and the
runtime role is NOBYPASSRLS. The policy predicate is
`portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint`, so
with the GUC unset the predicate is NULL and the table appears **empty**: a
SELECT returns zero rows, an INSERT is rejected by WITH CHECK, an UPDATE or
DELETE silently matches nothing. That failure is silent by design — it can never
leak another tenant's rows — which is exactly why it must be impossible to
forget, hence `tenant_txn` being the only supported entry point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import session_factory

# `true` = set_config's is_local flag: the value dies with the transaction, so a
# pooled connection can never carry one portal's context into another's work.
_SET_TENANT = text("SELECT set_config('app.portal_id', :pid, true)")


@asynccontextmanager
async def control_txn() -> AsyncIterator[AsyncSession]:
    """One transaction with NO tenant context.

    For the control-plane tables only: `portals`, `portal_sync`, `rest_log` and
    `portal_events` (§3 — they carry no call data and the worker tick and
    support must scan across portals, so they have no RLS).

    Touching `calls` / `employees` / `crm_contexts` here sees zero rows because
    RLS fails closed — that is intentional, not a bug to work around. §5.10's
    purge asserts emptiness for exactly this reason: a DELETE issued from a
    control transaction reports success having deleted nothing.
    """
    async with session_factory() as session, session.begin():
        yield session


@asynccontextmanager
async def tenant_txn(portal_id: int) -> AsyncIterator[AsyncSession]:
    """One transaction scoped to a single portal; the ONLY way to touch customer data.

    Issues `SET LOCAL app.portal_id` as its first statement so the §3 policies
    resolve to this tenant. Because `SET LOCAL` is transaction-scoped, this must
    be re-entered for **every** transaction — a commit clears the GUC, so a
    second unit of work inside one `async with` block is not covered and would
    silently see an empty database (§1 decision 8).

    The id is bound as a parameter, never interpolated: `set_config` takes text,
    and a formatted GUC assignment would be an injection point on a value that
    reaches us, indirectly, from a Bitrix24 payload.
    """
    pid = int(portal_id)  # reject anything that is not an integer id before it reaches SQL
    async with session_factory() as session, session.begin():
        await session.execute(_SET_TENANT, {"pid": str(pid)})
        yield session
