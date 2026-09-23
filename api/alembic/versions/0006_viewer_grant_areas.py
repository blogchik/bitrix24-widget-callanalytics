"""viewer_grants - a grant says which PARTS of the app it opens, and stops claiming to be CRM-only.

WHY: `crm_viewer_grants` (0005) widened one thing, the CRM mirror. An administrator who gave
somebody "the whole portal" reasonably read that as the whole widget, opened the Summary page
and found the same eighteen calls as before - because telephony is scoped somewhere else
entirely, by Bitrix24's own `voximplant.statistic.get` verdict, which the app honours.

So a grant now names its areas. Two booleans rather than one flag, because the owner asked
for both ends: "all rights" for one person, and the ability to give a narrower grant to the
next one.

WHY the defaults are what they are: `covers_crm` defaults TRUE and `covers_calls` FALSE, so
every row written under 0005 keeps meaning exactly what it meant when it was written. A
migration must never widen an access decision somebody already made.

WHY the rename: a table called `crm_viewer_grants` that also hands out telephony access is a
name that lies, and the next person to read the purge list would believe it. `viewer_grants`
is what it is. The constraints, the policy and the primary key travel with it, because a
renamed table carrying the old names in `pg_constraint` is the same lie one level down.

WHAT THIS DOES NOT DO: a grant still cannot make anyone an administrator. The settings page
is gated on the JWT's `adm` claim, which comes from Bitrix24's `user.admin` and nothing here
can touch. That is deliberate and is asserted in the test suite.

Revision ID: 0006_viewer_grant_areas
Revises: 0005_crm_viewer_scopes
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_viewer_grant_areas"
down_revision: str | None = "0005_crm_viewer_scopes"
branch_labels: str | None = None
depends_on: str | None = None


RENAME = """
ALTER TABLE crm_viewer_grants RENAME TO viewer_grants;
ALTER TABLE viewer_grants RENAME CONSTRAINT crm_viewer_grants_kind_chk TO viewer_grants_kind_chk;
ALTER TABLE viewer_grants
    RENAME CONSTRAINT crm_viewer_grants_departments_chk TO viewer_grants_departments_chk;
ALTER INDEX crm_viewer_grants_pkey RENAME TO viewer_grants_pkey;
ALTER POLICY crm_viewer_grants_tenant ON viewer_grants RENAME TO viewer_grants_tenant;
"""

AREAS = """
ALTER TABLE viewer_grants
    ADD COLUMN covers_crm   boolean NOT NULL DEFAULT true,
    ADD COLUMN covers_calls boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT viewer_grants_areas_chk CHECK (covers_crm OR covers_calls);

COMMENT ON TABLE viewer_grants IS
    'Administrator decisions about who may see more of this app than Bitrix24 shows them. '
    'NOT evidence and NOT rebuildable - deliberately separate from crm_viewer_scopes so that '
    '"why can this person see that?" always has one answer. Every write is audited to '
    'portal_events. A grant never confers administrator rights. FORCED RLS.';
COMMENT ON COLUMN viewer_grants.covers_crm IS
    'Opens the Deals and Sources reports: the CRM mirror, within the kind below.';
COMMENT ON COLUMN viewer_grants.covers_calls IS
    'Opens the Summary, By hour and call list pages. A wider disclosure than covers_crm: a '
    'call row carries the customer phone number, and Bitrix24 told us this viewer may see '
    'only their own. Granting it is an explicit override of that answer.';
"""

GRANTS = """
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON viewer_grants TO ca_app';
    END IF;
END
$do$;
"""

DOWNGRADE = """
ALTER TABLE viewer_grants
    DROP CONSTRAINT IF EXISTS viewer_grants_areas_chk,
    DROP COLUMN IF EXISTS covers_calls,
    DROP COLUMN IF EXISTS covers_crm;
ALTER POLICY viewer_grants_tenant ON viewer_grants RENAME TO crm_viewer_grants_tenant;
ALTER INDEX viewer_grants_pkey RENAME TO crm_viewer_grants_pkey;
ALTER TABLE viewer_grants
    RENAME CONSTRAINT viewer_grants_departments_chk TO crm_viewer_grants_departments_chk;
ALTER TABLE viewer_grants RENAME CONSTRAINT viewer_grants_kind_chk TO crm_viewer_grants_kind_chk;
ALTER TABLE viewer_grants RENAME TO crm_viewer_grants;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)` (see 0001 for why)."""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    """One statement group per concern so a failure names it."""
    _run(RENAME)
    _run(AREAS)
    _run(GRANTS)


def downgrade() -> None:
    """Back to 0005's shape exactly, including the names.

    Dropping the two columns loses which areas each grant opened; what comes back is a row
    that the 0005 code reads as CRM-only, which is what it was before this revision.
    """
    _run(DOWNGRADE)
