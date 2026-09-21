"""crm_viewer_scopes - what each non-administrator may be shown from the CRM mirror (§4.14).

WHY this exists at all: until now the mirror answered administrators only
(`services/crm_repo.py::serves_mirror` required `access == 'all'`), and every other employee
was sent back to the live REST read - the one with the 92-day cap, the 6,000-deal cap and the
18-second scan deadline. The owner asked for the mirror to serve everyone, each person seeing
what Bitrix24 shows them.

WHY a census and not a permission copy: Bitrix24 REST exposes no CRM permission data at all.
Probed directly against the production portal on 2026-09-21, `crm.role.list`,
`crm.role.relation.list`, `crm.permissions.get`, `crm.permission.get` and
`crm.settings.permissions.get` every one answered ERROR_METHOD_NOT_FOUND. There is no role, no
role assignment and no permission matrix to mirror. The only authority on what a viewer may see
is Bitrix24 asked with that viewer's own token, so this table stores the ANSWERS to that
question rather than the rules behind it.

WHY the unit is a (funnel, assignee) cell: it is the grid the Deals report groups by, and it is
small. Measured on the production portal: 29 deal cells plus 18 lead assignees, 47 commands, two
batches, 8.8 seconds - against 49 seconds to enumerate every visible id instead. A cell is
proven when a `crm.item.list` under the viewer's own token returns at least one row AND every
returned row echoes back the assignee and funnel that were filtered on; Bitrix24 may ignore an
unknown filter key rather than refuse it (docs/bitrix24-api-research.md:121), so the echo is
what turns "we asked" into "it answered the question we asked".

TWO TABLES, and the split is the point:

* `crm_viewer_scopes` is EVIDENCE. Every row in it was derived from Bitrix24's own answer to
  the viewer's own token. It is cache: delete it and the next open rebuilds it. It therefore
  lives in `CRM_TENANT_TABLES`, so turning CRM analytics off erases it with the rest of the
  mirror.
* `crm_viewer_grants` is a DECISION an administrator made, and it can be WIDER than Bitrix24 -
  that is what it is for. It is not evidence, it is not rebuildable, and conflating the two in
  one table would make "why can this person see that?" unanswerable. It is a tenant table
  (an uninstall removes it) but NOT a CRM table: turning CRM analytics off and on again must
  not silently forget an administrator's decision. Every write is audited to `portal_events`.

Revision ID: 0005_crm_viewer_scopes
Revises: 0004_crm_mirror
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_crm_viewer_scopes"
down_revision: str | None = "0004_crm_mirror"
branch_labels: str | None = None
depends_on: str | None = None


VIEWER_SCOPES = """
CREATE TABLE crm_viewer_scopes (
    portal_id       bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    user_id         integer     NOT NULL,
    resolved_at     timestamptz NOT NULL DEFAULT now(),
    sync_generation integer     NOT NULL,
    dialects        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    deal_verdict    varchar(12) NOT NULL,
    lead_verdict    varchar(12) NOT NULL,
    deal_reason     varchar(40) NOT NULL DEFAULT '',
    lead_reason     varchar(40) NOT NULL DEFAULT '',
    deal_cells      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    lead_assignees  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    truncated       boolean     NOT NULL DEFAULT false,
    commands        smallint    NOT NULL DEFAULT 0,
    PRIMARY KEY (portal_id, user_id),
    CONSTRAINT crm_viewer_scopes_deal_verdict_chk
        CHECK (deal_verdict IN ('ok', 'empty', 'unprovable')),
    CONSTRAINT crm_viewer_scopes_lead_verdict_chk
        CHECK (lead_verdict IN ('ok', 'empty', 'unprovable'))
);

COMMENT ON TABLE crm_viewer_scopes IS
    'One row per (portal, viewer): which (funnel, assignee) cells of the CRM mirror Bitrix24 '
    'proved this viewer may read, measured with the viewer own token. Cache, not configuration: '
    'deleting a row costs one census. FORCED RLS.';
COMMENT ON COLUMN crm_viewer_scopes.sync_generation IS
    'portal_sync.sync_generation when the census ran. A reinstall bumps it, which orphans every '
    'row here rather than letting a proof outlive the install it was taken under.';
COMMENT ON COLUMN crm_viewer_scopes.dialects IS
    'Which mirror dialect answered each entity, e.g. {"deal": "item-deal", "lead": "lead"}. A '
    'census taken on one dialect says nothing about another spelling of the same filter.';
COMMENT ON COLUMN crm_viewer_scopes.deal_verdict IS
    'ok: at least one cell proven. empty: the viewer has no candidate cells at all, which is a '
    'real answer and not a failure. unprovable: the census could not be trusted (see '
    'deal_reason) and this viewer must stay on the live read.';
COMMENT ON COLUMN crm_viewer_scopes.deal_cells IS
    'The PROVEN cells, as [[category_id, assignee_id], ...]. A positive allow-list: a funnel '
    'created after the census is simply absent until the next one. Never a deny-list.';
COMMENT ON COLUMN crm_viewer_scopes.lead_assignees IS
    'The PROVEN lead assignees. Leads carry no category: crm_items.category_id is NULL for every '
    'lead, and neither lead dialect has a category_id wire field.';
COMMENT ON COLUMN crm_viewer_scopes.truncated IS
    'The candidate grid was larger than the per-census command budget and was cut to the most '
    'recently touched cells. The viewer sees less than Bitrix24 would show them, never more.';
"""

VIEWER_GRANTS = """
CREATE TABLE crm_viewer_grants (
    portal_id      bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    user_id        integer     NOT NULL,
    kind           varchar(16) NOT NULL,
    department_ids integer[]   NOT NULL DEFAULT '{}',
    granted_by     integer     NOT NULL,
    granted_at     timestamptz NOT NULL DEFAULT now(),
    note           text        NOT NULL DEFAULT '',
    PRIMARY KEY (portal_id, user_id),
    CONSTRAINT crm_viewer_grants_kind_chk CHECK (kind IN ('portal', 'departments')),
    CONSTRAINT crm_viewer_grants_departments_chk
        CHECK (kind <> 'departments' OR cardinality(department_ids) > 0)
);

COMMENT ON TABLE crm_viewer_grants IS
    'Administrator decisions about who may read more of the CRM mirror than Bitrix24 itself '
    'would show them. NOT evidence and NOT rebuildable - deliberately a separate table from '
    'crm_viewer_scopes so that "why can this person see that?" always has one answer. Every '
    'write is audited to portal_events. FORCED RLS.';
COMMENT ON COLUMN crm_viewer_grants.kind IS
    'portal: every record the mirror holds. departments: records whose assignee sits in one of '
    'department_ids, resolved through employees.departments.';
COMMENT ON COLUMN crm_viewer_grants.granted_by IS
    'Bitrix24 user id of the administrator who made the decision. Never the viewer.';
COMMENT ON COLUMN crm_viewer_grants.note IS
    'Free text the administrator wrote when granting, shown back on the settings page so the '
    'reason survives the person who had it.';
"""

RLS = """
ALTER TABLE crm_viewer_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_viewer_scopes FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_viewer_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_viewer_grants FORCE  ROW LEVEL SECURITY;

CREATE POLICY crm_viewer_scopes_tenant ON crm_viewer_scopes
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY crm_viewer_grants_tenant ON crm_viewer_grants
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
"""

# Default privileges already cover tables ca_owner creates, but 0001 and 0004 grant
# explicitly and a developer database migrated as another role would otherwise have none.
GRANTS = """
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON crm_viewer_scopes, crm_viewer_grants '
             || 'TO ca_app';
    END IF;
END
$do$;
"""

DOWNGRADE = """
DROP TABLE IF EXISTS crm_viewer_grants;
DROP TABLE IF EXISTS crm_viewer_scopes;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)` (see 0001 for why)."""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    """One statement group per table so a failure names it."""
    _run(VIEWER_SCOPES)
    _run(VIEWER_GRANTS)
    _run(RLS)
    _run(GRANTS)


def downgrade() -> None:
    """Drop both tables.

    The census is cache and costs one rebuild. The grants are a real loss: they are
    administrator decisions that exist nowhere else, which is why the settings page warns
    before an operator is ever pointed at a downgrade.
    """
    _run(DOWNGRADE)
