"""employees.phone_inner - the internal telephony extension (§7).

WHY this column exists: the employee filter names people, and in a portal with two Ivanovs
a name is not an identifier. The extension is the number the rest of the business already
uses to mean one of them, so the filter shows it in parentheses beside the name.

WHY it costs nothing new to obtain: `UF_PHONE_INNER` is part of the `user_brief` scope the
app already holds, and `bitrix/users.py::fetch_users` calls `user.get` with no field list -
so the value is already in the response the refresher parses today and is simply discarded.
No new scope is requested, which matters: a new scope changes the Marketplace listing and
forces every installed portal to re-consent. `docs/bitrix24-api-research.md` note on the
`user_brief` field list named this exact column as one the employee cache should carry; §3
dropped it, and this revision puts it back.

WHY it is not a contact detail: `user_brief` is defined by Bitrix24 as "user information
without contact details", and EMAIL / PERSONAL_PHONE / WORK_PHONE live in `user_basic`,
which this app does not request and cannot read. An internal extension is a routing number
inside the portal's own directory, visible to every employee in it.

WHY nothing else has to be re-issued: `ADD COLUMN` with no default is a catalogue-only
change in PostgreSQL 11+ (no table rewrite); the §3 `GRANT ... ON employees TO ca_app` is
table-level, so the new column inherits it; and `employees_tenant` is a ROW-level policy
keyed on `app.portal_id`, which is column-agnostic. There is deliberately no CHECK: §3's
rule is that no constraint is placed on a value Bitrix24 controls, and this one is a
free-text user field a portal can put anything into.

Revision ID: 0002_employees_phone_inner
Revises: 0001_baseline
"""

from __future__ import annotations

from alembic import op

revision: str = "0002_employees_phone_inner"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


# varchar(32) rather than the varchar(255) the name columns use: a real extension is two to
# six characters, but the field is free text and portals do put a full number in it, so it
# must not be varchar(8) either. Over-long values are cut by the writer's existing `_trim`,
# never rejected - a cosmetic column must not be able to fail an employee refresh.
ADD_COLUMN = """
ALTER TABLE employees ADD COLUMN phone_inner varchar(32);
"""

COMMENT = """
COMMENT ON COLUMN employees.phone_inner IS
    'UF_PHONE_INNER from user.get (user_brief scope). Internal telephony extension, shown '
    'beside the name in the employee filter. NULL = the portal sets none, or this row has '
    'not been refreshed since the column was added. Not a contact detail: email and '
    'personal phone are user_basic, which this app never requests.';
"""

DROP_COLUMN = """
ALTER TABLE employees DROP COLUMN IF EXISTS phone_inner;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)`.

    `op.execute` on a plain string wraps it in `text()`, which treats `:word` as a bind
    parameter - and the COMMENT body above contains colon-prefixed words. The baseline
    revision does the same thing for the same reason.
    """
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _run(ADD_COLUMN)
    _run(COMMENT)


def downgrade() -> None:
    """Drop the column, which is an exact inverse.

    Deliberately nothing else. The tempting extra - setting `fetched_at = NULL` so the
    refresher backfills every row immediately - is not written here in either direction:
    it would mark every employee of every portal as a placeholder (which is what that NULL
    documents), and it has no inverse at all, because the timestamps it overwrote are gone.
    The column fills itself within `EMPLOYEE_TTL_HOURS` instead.
    """
    _run(DROP_COLUMN)
