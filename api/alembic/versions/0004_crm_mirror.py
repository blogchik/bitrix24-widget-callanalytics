"""crm_mirror - the tables the CRM mirror stores deals, leads and their dictionaries in (§4.14).

WHY a mirror at all: owner decisions D-1..D-9 (docs/architecture.md decision 26) replace the
live `/deals` and `/utm` reads with a Postgres copy the worker keeps in sync, so no report
depends on request-time REST, the per-method operating limit or a 92-day cap.

WHY these columns and no others: D-3. `crm_items` holds exactly the fields the privacy policy
lists (section 2, revision of 15 September 2026) and `bitrix/crm_items.py` selects - never a
title, a person, a phone, a comment or a custom field. A new column here is a privacy-policy
change first.

What is tenant data and what is not, which `app/db/tenancy.py` records and
`python -m app.tools.check_tenancy` enforces:

* `crm_items`, `crm_funnels`, `crm_stages`, `crm_dirty` hold per-record CRM ids and values, so
  they carry FORCED row-level security with the same policy as `calls`, are purged on
  uninstall, and every index leads with `portal_id`.
* `crm_lanes` holds one cursor row per (portal, lane): id bounds, timestamps and counters, never
  a record's values. The worker reads it before any tenant context exists, so no RLS.
* `app_flags` names no portal: fleet-wide switches, starting with the CRM mirror kill switch.

WHY `portals.crm_mode` defaults to 'sync': the owner decided on 2026-09-15 that storage starts
on every install as soon as the code ships, without the 14-day wait D-7 first described. This
revision ships alone, one release ahead of the code that reads it (decision 30), and the code
before it ignores the column.

Revision ID: 0004_crm_mirror
Revises: 0003_sync_method_budgets
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_crm_mirror"
down_revision: str | None = "0003_sync_method_budgets"
branch_labels: str | None = None
depends_on: str | None = None


PORTALS = """
ALTER TABLE portals
    ADD COLUMN crm_mode                 varchar(8)  NOT NULL DEFAULT 'sync',
    ADD COLUMN crm_opt_out_at           timestamptz,
    ADD COLUMN crm_opt_out_by           integer,
    ADD COLUMN crm_notice_dismissed_by  integer[]   NOT NULL DEFAULT '{}',
    ADD COLUMN crm_purge_pending        boolean     NOT NULL DEFAULT false,
    ADD CONSTRAINT portals_crm_mode_chk CHECK (crm_mode IN ('off','sync','shadow','mirror')),
    ADD CONSTRAINT portals_crm_opt_out_chk CHECK (crm_opt_out_at IS NULL OR crm_mode = 'off');
CREATE INDEX portals_crm_purge_idx ON portals (id) WHERE crm_purge_pending;

COMMENT ON COLUMN portals.crm_mode IS
    'off | sync | shadow | mirror (§4.14). off: no CRM REST and no CRM rows. sync: the worker '
    'mirrors, pages read live. shadow: pages read live and are compared with the mirror. mirror: '
    'pages read Postgres. Moved only by services/crm_state.py.';
COMMENT ON COLUMN portals.crm_opt_out_at IS
    'When an administrator turned CRM analytics off. While set, crm_mode stays off (CHECK) and the '
    'Deals and Sources pages are unavailable, live reads included.';
COMMENT ON COLUMN portals.crm_opt_out_by IS 'Bitrix24 user id of the administrator who turned it off.';
COMMENT ON COLUMN portals.crm_notice_dismissed_by IS
    'Administrators who dismissed the informational CRM notice banner. It gates nothing.';
COMMENT ON COLUMN portals.crm_purge_pending IS
    'Set when CRM analytics is turned off: the worker deletes this portal''s CRM tenant rows '
    '(crm_items, crm_funnels, crm_stages, crm_dirty) under tenant context and clears it. The '
    'whole-portal purge_pending covers them on uninstall.';
"""

CRM_LANES = """
CREATE TABLE crm_lanes (
    portal_id        bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    lane             varchar(32) NOT NULL,
    status           varchar(16) NOT NULL DEFAULT 'pending',
    due_at           timestamptz NOT NULL DEFAULT now(),
    cursor           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    progress_done    bigint      NOT NULL DEFAULT 0,
    progress_total   bigint,
    last_clean_at    timestamptz,
    failures         integer     NOT NULL DEFAULT 0,
    paused_until     timestamptz,
    block_reason     varchar(64),
    last_error_code  varchar(64),
    last_error_at    timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portal_id, lane),
    CONSTRAINT crm_lanes_status_chk CHECK (status IN ('pending','active','done','parked'))
);
CREATE TRIGGER crm_lanes_updated_at BEFORE UPDATE ON crm_lanes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE crm_lanes IS
    'One CRM mirror lane per portal (dict, deal.backfill, lead.sweep, ...), each with its own '
    'schedule, cursor and failure state inside the one portal lease (§5.10). Control plane: id '
    'bounds, server timestamps and counters only, never a record''s values.';
COMMENT ON COLUMN crm_lanes.status IS
    'pending (never run) | active (has work) | done (a one-off lane finished) | parked '
    '(block_reason says why; only a person or a reset moves it).';
COMMENT ON COLUMN crm_lanes.cursor IS
    'Lane-specific resume state: id ranges for a backfill, the server-clock watermark for a '
    'sweep. Replaced whole in the same fenced transaction as the rows it describes.';
COMMENT ON COLUMN crm_lanes.failures IS
    'Consecutive failed runs of this lane. Backs off this lane alone; never the portal.';
COMMENT ON COLUMN crm_lanes.paused_until IS 'Failure back-off: the lane is skipped until then.';
"""

APP_FLAGS = """
CREATE TABLE app_flags (
    name        varchar(64) PRIMARY KEY,
    enabled     boolean     NOT NULL,
    note        text        NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER app_flags_updated_at BEFORE UPDATE ON app_flags
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
INSERT INTO app_flags (name, enabled, note) VALUES
    ('crm_mirror', true,
     'Kill switch: false stops every CRM REST call the worker makes, fleet-wide, at the next visit. '
     'Stored rows stay; pages keep serving what they served.');

COMMENT ON TABLE app_flags IS
    'Fleet-wide switches an operator flips with one UPDATE, without a deploy. Names no portal.';
"""

CRM_ITEMS = """
CREATE TABLE crm_items (
    portal_id           bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    entity_type_id      smallint     NOT NULL,
    id                  bigint       NOT NULL,
    category_id         integer,
    stage_id            varchar(128),
    stage_semantic      varchar(1),
    assigned_by_id      integer,
    created_time        timestamptz,
    updated_time        timestamptz,
    moved_time          timestamptz,
    closed              boolean,
    opportunity         numeric,
    currency_id         varchar(16),
    lead_id             bigint,
    contact_ids         bigint[],
    company_id          bigint,
    utm_source          text,
    utm_medium          text,
    utm_campaign        text,
    utm_content         text,
    utm_term            text,
    read_at             timestamptz  NOT NULL,
    synced_at           timestamptz  NOT NULL DEFAULT now(),
    content_changed_at  timestamptz,
    deleted_at          timestamptz,
    delete_reason       varchar(16),
    unreadable_since    timestamptz,
    PRIMARY KEY (portal_id, entity_type_id, id),
    CONSTRAINT crm_items_entity_chk CHECK (entity_type_id IN (1, 2)),
    CONSTRAINT crm_items_delete_reason_chk CHECK (delete_reason IN ('not_found','evicted')),
    CONSTRAINT crm_items_tombstone_chk CHECK (
        (deleted_at IS NULL AND delete_reason IS NULL)
        OR (deleted_at IS NOT NULL AND delete_reason IS NOT NULL
            AND category_id IS NULL AND stage_id IS NULL AND stage_semantic IS NULL
            AND assigned_by_id IS NULL AND created_time IS NULL AND updated_time IS NULL
            AND moved_time IS NULL AND closed IS NULL AND opportunity IS NULL
            AND currency_id IS NULL AND lead_id IS NULL AND contact_ids IS NULL
            AND company_id IS NULL AND utm_source IS NULL AND utm_medium IS NULL
            AND utm_campaign IS NULL AND utm_content IS NULL AND utm_term IS NULL)
    )
);
CREATE INDEX crm_items_deal_created_idx
    ON crm_items (portal_id, created_time)
    INCLUDE (category_id, assigned_by_id, stage_id, stage_semantic)
    WHERE entity_type_id = 2 AND deleted_at IS NULL;
CREATE INDEX crm_items_deal_updated_idx
    ON crm_items (portal_id, updated_time)
    WHERE entity_type_id = 2 AND deleted_at IS NULL;
CREATE INDEX crm_items_deal_closed_idx
    ON crm_items (portal_id, moved_time)
    WHERE entity_type_id = 2 AND closed AND deleted_at IS NULL;
CREATE INDEX crm_items_lead_created_idx
    ON crm_items (portal_id, created_time)
    WHERE entity_type_id = 1 AND deleted_at IS NULL;
CREATE INDEX crm_items_assignee_idx
    ON crm_items (portal_id, entity_type_id, assigned_by_id, created_time)
    WHERE deleted_at IS NULL;
CREATE INDEX crm_items_company_idx
    ON crm_items (portal_id, company_id)
    WHERE company_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX crm_items_tombstone_idx
    ON crm_items (portal_id, deleted_at)
    WHERE deleted_at IS NOT NULL;
CREATE INDEX crm_items_unreadable_idx
    ON crm_items (portal_id, unreadable_since)
    WHERE unreadable_since IS NOT NULL;

COMMENT ON TABLE crm_items IS
    'The CRM mirror: one row per deal (entity_type_id 2) or lead (1), D-3 fields only. FORCED RLS, '
    'purged on uninstall and on CRM opt-out. No CHECK on a Bitrix-controlled value: one odd stage '
    'id must never stall a lane.';
COMMENT ON COLUMN crm_items.entity_type_id IS 'Bitrix24''s own entityTypeId: 1 lead, 2 deal.';
COMMENT ON COLUMN crm_items.stage_semantic IS
    'S | F | P after bitrix/deals.py::normalise_semantic; anything unrecognised is P.';
COMMENT ON COLUMN crm_items.opportunity IS
    'The amount in the record''s own currency, rounded half-up to cents. Unconstrained numeric: a '
    'portal''s absurd amount must not abort a chunk. Account-currency amounts are not returned to '
    'list calls (docs/spike-crm-mirror.md), so none is stored.';
COMMENT ON COLUMN crm_items.utm_source IS
    'Normalised exactly as the /utm report normalises tags (bitrix/utm.py::utm_values): '
    'trimmed, empty for none, cut to UTM_VALUE_MAX_CHARS.';
COMMENT ON COLUMN crm_items.read_at IS
    'The worker''s clock just before the request that produced this version. An upsert writes '
    'only when its read_at is not older, so a slow page can never overwrite a newer read.';
COMMENT ON COLUMN crm_items.content_changed_at IS
    'Set only when an upsert changed a data column (IS DISTINCT FROM).';
COMMENT ON COLUMN crm_items.deleted_at IS
    'Tombstone. not_found: crm.item.get proved the deletion (decision 29) - never resurrected, '
    'row removed after 35 days. evicted: unreadable too long - a later successful read restores '
    'it. Every data column is NULL while set (crm_items_tombstone_chk).';
COMMENT ON COLUMN crm_items.unreadable_since IS
    'First time the installer credential was refused this record. Hidden after 7 days, evicted '
    'after 30 (§5.12).';
"""

CRM_FUNNELS = """
CREATE TABLE crm_funnels (
    portal_id       bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    entity_type_id  smallint    NOT NULL,
    category_id     integer     NOT NULL,
    name            text        NOT NULL DEFAULT '',
    sort            integer     NOT NULL DEFAULT 0,
    is_default      boolean     NOT NULL DEFAULT false,
    seen_at         timestamptz NOT NULL DEFAULT now(),
    missing_since   timestamptz,
    PRIMARY KEY (portal_id, entity_type_id, category_id),
    CONSTRAINT crm_funnels_entity_chk CHECK (entity_type_id IN (1, 2))
);

COMMENT ON TABLE crm_funnels IS
    'Deal funnels (and the single lead pipeline as category 0) with their names, read with the '
    'installer credential (G0 Q1: names are stored). FORCED RLS.';
COMMENT ON COLUMN crm_funnels.missing_since IS
    'First dictionary read that no longer listed this funnel. Removed after two such reads 24 h '
    'apart, so one truncated answer cannot erase a funnel.';
"""

CRM_STAGES = """
CREATE TABLE crm_stages (
    portal_id       bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    entity_type_id  smallint     NOT NULL,
    category_id     integer      NOT NULL,
    status_id       varchar(128) NOT NULL,
    name            text         NOT NULL DEFAULT '',
    sort            integer      NOT NULL DEFAULT 0,
    semantic        varchar(1)   NOT NULL DEFAULT 'P',
    seen_at         timestamptz  NOT NULL DEFAULT now(),
    missing_since   timestamptz,
    PRIMARY KEY (portal_id, entity_type_id, category_id, status_id),
    CONSTRAINT crm_stages_entity_chk CHECK (entity_type_id IN (1, 2))
);

COMMENT ON TABLE crm_stages IS
    'Stages per funnel, keyed by the (category_id, status_id) pair because a status id is unique '
    'only inside its own directory (bitrix/deals.py::Stage). FORCED RLS.';
"""

CRM_DIRTY = """
CREATE TABLE crm_dirty (
    portal_id        bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    entity_type_id   smallint    NOT NULL,
    id               bigint      NOT NULL,
    reasons          integer     NOT NULL DEFAULT 0,
    seq              bigint      NOT NULL DEFAULT 1,
    first_marked_at  timestamptz NOT NULL DEFAULT now(),
    not_before       timestamptz NOT NULL DEFAULT now(),
    held_until       timestamptz,
    attempts         smallint    NOT NULL DEFAULT 0,
    PRIMARY KEY (portal_id, entity_type_id, id),
    CONSTRAINT crm_dirty_entity_chk CHECK (entity_type_id IN (1, 2, 3, 4))
);
CREATE INDEX crm_dirty_due_idx ON crm_dirty (portal_id, not_before);

COMMENT ON TABLE crm_dirty IS
    'Record ids to re-read (a change signal, a reconciliation mismatch). Only ids and reasons - '
    'the stored values always come from a re-read. FORCED RLS.';
COMMENT ON COLUMN crm_dirty.seq IS
    'Bumped on every mark. A refresh deletes the row only WHERE seq = the value it read, so a mark '
    'that arrives mid-refresh survives for the next pass.';
COMMENT ON COLUMN crm_dirty.reasons IS 'Bit set of why the id was marked (services/crm_dirty.py).';
"""

RLS = """
ALTER TABLE crm_items   ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_items   FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_funnels ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_funnels FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_stages  ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_stages  FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_dirty   ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_dirty   FORCE  ROW LEVEL SECURITY;

CREATE POLICY crm_items_tenant ON crm_items
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY crm_funnels_tenant ON crm_funnels
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY crm_stages_tenant ON crm_stages
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY crm_dirty_tenant ON crm_dirty
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
"""

# Default privileges already cover tables ca_owner creates, but the baseline grants
# explicitly and a developer database migrated as another role would otherwise have none.
GRANTS = """
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON crm_lanes, app_flags, crm_items, '
             || 'crm_funnels, crm_stages, crm_dirty TO ca_app';
    END IF;
END
$do$;
"""

DOWNGRADE = """
DROP TABLE IF EXISTS crm_dirty;
DROP TABLE IF EXISTS crm_stages;
DROP TABLE IF EXISTS crm_funnels;
DROP TABLE IF EXISTS crm_items;
DROP TABLE IF EXISTS app_flags;
DROP TABLE IF EXISTS crm_lanes;
DROP INDEX IF EXISTS portals_crm_purge_idx;
ALTER TABLE portals
    DROP CONSTRAINT IF EXISTS portals_crm_opt_out_chk,
    DROP CONSTRAINT IF EXISTS portals_crm_mode_chk,
    DROP COLUMN IF EXISTS crm_purge_pending,
    DROP COLUMN IF EXISTS crm_notice_dismissed_by,
    DROP COLUMN IF EXISTS crm_opt_out_by,
    DROP COLUMN IF EXISTS crm_opt_out_at,
    DROP COLUMN IF EXISTS crm_mode;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)` (see 0001 for why)."""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    """One statement group per table so a failure names it."""
    _run(PORTALS)
    _run(CRM_LANES)
    _run(APP_FLAGS)
    _run(CRM_ITEMS)
    _run(CRM_FUNNELS)
    _run(CRM_STAGES)
    _run(CRM_DIRTY)
    _run(RLS)
    _run(GRANTS)


def downgrade() -> None:
    """Drop everything this revision created. Discards the mirror; it is rebuildable."""
    _run(DOWNGRADE)
