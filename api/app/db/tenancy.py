"""§3 - the one list of which tables hold customer data, and the check that the database agrees.

Tenancy used to be pinned in four places: `sync/purge.py`, `tests/conftest.py`,
`tests/test_registry_lint.py` and a `count(*) = 3` in CI. A new customer table had to touch
all four, and the one most likely to be forgotten fails silently: a table missing from the
purge list is simply never deleted on uninstall. This module is now the only list, and
`check_tenancy` is how CI proves the migrated catalog matches it
(`python -m app.tools.check_tenancy`).

The rules the catalog must satisfy:

1. every table with a `portal_id` column is named in exactly one of `TENANT_TABLES` or
   `CONTROL_PLANE_TABLES`;
2. a tenant table has row-level security ENABLED and FORCED, and exactly one policy,
   `FOR ALL`, bound to `app.portal_id` with the same predicate as every other tenant table;
3. no other table has row-level security or a policy - the worker reads the control plane
   without a tenant context, and a policy there reads as empty rather than failing;
4. every index on a tenant table leads with `portal_id`, unless `NON_PORTAL_LEADING_INDEXES`
   names it and says why;
5. the runtime role cannot bypass row-level security.

Nothing here builds an engine: importing the registry must stay free of side effects
(`app/db/__init__.py`), so the lint and the purge can read the names without a database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "CONTROL_PLANE_TABLES",
    "NON_PORTAL_LEADING_INDEXES",
    "RUNTIME_ROLE",
    "TENANT_TABLES",
    "catalog_problems",
    "check_tenancy",
    "role_problems",
]

#: The FORCED-RLS customer tables, in the fixed order the purge deletes and audits them.
TENANT_TABLES: Final[tuple[str, ...]] = ("calls", "employees", "crm_contexts")

#: Every other table that names a portal, with the reason it is not RLS-bound. A new table
#: with a `portal_id` column belongs in exactly one of these two lists.
CONTROL_PLANE_TABLES: Final[Mapping[str, str]] = {
    "portals": "the tenant registry itself; the lease query must see every portal",
    "portal_sync": "one lease and cursor row per portal, read by the worker before a tenant is chosen",
    "rest_log": "the request log (§6): its own retention and CLEAN redaction, not the tenant purge",
    "portal_events": "the lifecycle audit trail support reads across portals",
    "sync_method_budgets": "operating-time numbers per (portal, method), read before a tenant is chosen",
}

#: Indexes on a tenant table that deliberately do not lead with `portal_id`.
NON_PORTAL_LEADING_INDEXES: Final[Mapping[str, str]] = {
    "calls_pkey": "the surrogate identity key; tenant reads go through (portal_id, bx_id)",
    "crm_contexts_age_idx": (
        "the 30-day retention sweep (§6) selects by resolved_at, portal by portal under "
        "tenant_txn, so the policy still bounds every scan"
    ),
}

#: The role `DATABASE_URL` connects as in every environment (§3).
RUNTIME_ROLE: Final[str] = "ca_app"

_SCHEMA: Final[str] = "public"

#: The GUC every tenant policy binds to (`db/session.py::tenant_txn`).
_TENANT_GUC: Final[str] = "app.portal_id"


async def catalog_problems(
    conn: AsyncConnection,
    *,
    tenant_tables: Sequence[str] = TENANT_TABLES,
    control_plane_tables: Sequence[str] = tuple(CONTROL_PLANE_TABLES),
    non_portal_leading_indexes: Sequence[str] = tuple(NON_PORTAL_LEADING_INDEXES),
) -> list[str]:
    """Rules 1-4 against the connected database; an empty list means the catalog agrees.

    The keyword arguments exist for the negative controls in
    `tests/test_tenant_table_registry.py`; production callers use the defaults.
    """
    tenant = tuple(tenant_tables)
    control = tuple(control_plane_tables)
    problems = [
        f"{name}: listed as both a tenant and a control-plane table"
        for name in sorted(set(tenant) & set(control))
    ]

    tables = {
        row.relname: row
        for row in (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                           EXISTS (
                               SELECT 1 FROM pg_attribute a
                                WHERE a.attrelid = c.oid AND a.attname = 'portal_id'
                                  AND a.attnum > 0 AND NOT a.attisdropped
                           ) AS has_portal_id
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')
                    """
                ),
                {"schema": _SCHEMA},
            )
        ).all()
    }

    for name in (*tenant, *control):
        if name not in tables:
            problems.append(f"{name}: registered in app/db/tenancy.py but not in the database")
    for name, row in sorted(tables.items()):
        if row.has_portal_id and name not in tenant and name not in control:
            problems.append(
                f"{name}: has a portal_id column but is in neither TENANT_TABLES nor "
                "CONTROL_PLANE_TABLES (app/db/tenancy.py)"
            )

    policies: dict[str, list[tuple[str, str, str | None, str | None]]] = {}
    for row in (
        await conn.execute(
            text(
                "SELECT tablename, policyname, cmd, qual, with_check "
                "FROM pg_policies WHERE schemaname = :schema"
            ),
            {"schema": _SCHEMA},
        )
    ).all():
        policies.setdefault(row.tablename, []).append(
            (row.policyname, row.cmd, row.qual, row.with_check)
        )

    canonical: str | None = None
    for name in tenant:
        table_row = tables.get(name)
        if table_row is None:
            continue
        if not (table_row.relrowsecurity and table_row.relforcerowsecurity):
            problems.append(f"{name}: row-level security must be ENABLED and FORCED")
        own = policies.get(name, [])
        if len(own) != 1:
            problems.append(f"{name}: expected exactly one tenant policy, found {len(own)}")
            continue
        policy_name, cmd, qual, with_check = own[0]
        if cmd != "ALL":
            problems.append(f"{name}: policy {policy_name} must be FOR ALL, not {cmd}")
        # FOR ALL without WITH CHECK applies USING to writes too, so NULL is equivalent.
        if qual is None or (with_check is not None and with_check != qual):
            problems.append(
                f"{name}: policy {policy_name} must use one predicate for USING and WITH CHECK"
            )
        elif _TENANT_GUC not in qual:
            problems.append(f"{name}: policy {policy_name} is not bound to {_TENANT_GUC}")
        elif canonical is None:
            canonical = qual
        elif qual != canonical:
            problems.append(
                f"{name}: policy {policy_name} differs from the other tenant tables' predicate"
            )

    for name, row in sorted(tables.items()):
        if name in tenant:
            continue
        if row.relrowsecurity or row.relforcerowsecurity or policies.get(name):
            problems.append(
                f"{name}: row-level security on a table outside TENANT_TABLES reads as empty "
                "without a tenant context"
            )

    if tenant:
        allowed = set(non_portal_leading_indexes)
        seen: set[str] = set()
        for index_row in (
            await conn.execute(
                text(
                    """
                    SELECT t.relname AS table_name, i.relname AS index_name,
                           a.attname AS leading_column
                      FROM pg_index x
                      JOIN pg_class i ON i.oid = x.indexrelid
                      JOIN pg_class t ON t.oid = x.indrelid
                      JOIN pg_namespace n ON n.oid = t.relnamespace
                      LEFT JOIN pg_attribute a
                             ON a.attrelid = t.oid AND a.attnum = x.indkey[0]
                     WHERE n.nspname = :schema AND t.relname::text = ANY(CAST(:tables AS text[]))
                     ORDER BY t.relname, i.relname
                    """
                ),
                {"schema": _SCHEMA, "tables": list(tenant)},
            )
        ).all():
            seen.add(index_row.index_name)
            if index_row.leading_column != "portal_id" and index_row.index_name not in allowed:
                problems.append(
                    f"{index_row.table_name}: index {index_row.index_name} does not lead with "
                    "portal_id (add it to NON_PORTAL_LEADING_INDEXES with the reason if deliberate)"
                )
        # An exemption for an index that no longer exists would silently cover the next
        # index to take its name.
        problems.extend(
            f"{name}: listed in NON_PORTAL_LEADING_INDEXES but no such index on a tenant table"
            for name in sorted(allowed - seen)
        )

    return problems


async def role_problems(conn: AsyncConnection, *, runtime_role: str = RUNTIME_ROLE) -> list[str]:
    """Rule 5, for the connected role and for the runtime role when it exists.

    Both, because a deployment that pointed `DATABASE_URL` at a bypassing role would make
    every policy check above pass while the application read across tenants.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles "
                "WHERE rolname = current_user OR rolname = :runtime"
            ),
            {"runtime": runtime_role},
        )
    ).all()
    return [
        f"role {row.rolname} can bypass row-level security"
        for row in rows
        if row.rolbypassrls or row.rolsuper
    ]


async def check_tenancy(conn: AsyncConnection) -> list[str]:
    """Every rule, in order; what `python -m app.tools.check_tenancy` runs in CI."""
    return [*await catalog_problems(conn), *await role_problems(conn)]
