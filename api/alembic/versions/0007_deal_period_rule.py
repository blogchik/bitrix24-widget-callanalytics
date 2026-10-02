"""portals.deal_period_rule - the stages whose deals count in a period by more than their creation.

WHY: owner decision 3 (§4.12) changed on 2026-10-02. A deal still belongs to the period it was
CREATED in. For the stages an administrator names here it ALSO belongs to the period in which it
moved into that stage (`movedTime`) or was modified (`updatedTime`). The owner's words: not only
by creation time, but by the time it changed as well.

WHY it is data and not code: the stages are the portal's own (`"16:C16:WON"`, `"16:C16:UC_…"`),
and §4.12 constraint 1 keeps stage names out of the app. A name hardcoded here would stop working
the day an administrator renames the stage, and silently.

WHY JSONB on `portals` and not a table: one small object per portal, read with the `portals` row
every report already loads, written by one function (`services/portals.set_deal_period_rule`) and
audited to `portal_events`. `{}` means "no stages": creation time alone, which is what every
portal does today - so this revision changes no report until an administrator saves a rule.

WHY the CHECK knows only the type: the keys are validated by that one writer against the portal's
current stage dictionary. A constraint that knew them would have to know every portal's stages.

No GRANT: `ca_app` holds table-level privileges on `portals` (0001), which cover a new column.

Revision ID: 0007_deal_period_rule
Revises: 0006_viewer_grant_areas
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_deal_period_rule"
down_revision: str | None = "0006_viewer_grant_areas"
branch_labels: str | None = None
depends_on: str | None = None


UPGRADE = """
ALTER TABLE portals
    ADD COLUMN deal_period_rule jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD CONSTRAINT portals_deal_period_rule_chk CHECK (jsonb_typeof(deal_period_rule) = 'object');

COMMENT ON COLUMN portals.deal_period_rule IS
    'Owner decision 3 (§4.12): {"stage_keys": ["<category_id>:<status_id>", ...]}. A deal on one '
    'of these stages also counts in the period it moved into the stage or was modified in; '
    'every other deal counts in the period it was created in. {} = creation time alone. '
    'Written only by services/portals.set_deal_period_rule, audited to portal_events.';
"""

DOWNGRADE = """
ALTER TABLE portals
    DROP CONSTRAINT IF EXISTS portals_deal_period_rule_chk,
    DROP COLUMN IF EXISTS deal_period_rule;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)` (see 0001 for why)."""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _run(UPGRADE)


def downgrade() -> None:
    """Back to 0006. Every portal returns to creation time alone; the rules are lost."""
    _run(DOWNGRADE)
