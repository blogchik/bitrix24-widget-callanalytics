"""Baseline schema - verbatim transcription of docs/architecture.md §3.

WHY raw SQL instead of the Alembic DSL: §3 relies on STORED generated columns, partial
and covering (``INCLUDE``) indexes, forced RLS policies, ``COMMENT ON``, triggers and role
grants. The DSL expresses those poorly or not at all, and the approved schema text is the
contract the sync layer, the RLS session helpers and the ORM models are all written against.
Faithfulness to §3 beats idiom here.

WHY this file is frozen: this revision is the schema the owner approved. Any later change -
a new column, a widened type, an extra index - MUST be a NEW Alembic revision. Never edit
this one: portals that already ran it would silently diverge from portals that ran the edit.

Roles (``ca_owner`` / ``ca_app``) and the database itself are created by
``docker/postgres/init.sql`` and run once by ops, not by Alembic (§3 section header), so the
``CREATE ROLE`` / ``CREATE DATABASE`` lines of §3 are deliberately absent below. The grants
that reference ``ca_app`` are guarded by a ``pg_roles`` lookup so a developer running this
migration against a plain local database (no ops bootstrap) still gets the schema.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


# --------------------------------------------------------------------------------------
# §3 - shared updated_at trigger function
# --------------------------------------------------------------------------------------
SET_UPDATED_AT = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;
"""

# --------------------------------------------------------------------------------------
# §3 - portals: tenant registry + portal (installer) credential
# --------------------------------------------------------------------------------------
PORTALS = """
CREATE TABLE portals (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    member_id                varchar(32)  NOT NULL,
    domain                   varchar(255) NOT NULL,
    protocol_https           boolean      NOT NULL DEFAULT true,
    client_endpoint          varchar(512) NOT NULL,
    server_endpoint          varchar(512) NOT NULL DEFAULT 'https://oauth.bitrix.info/rest/',
    status                   varchar(16)  NOT NULL DEFAULT 'active',
    app_status               varchar(4),
    app_version              integer,
    installed_flag           boolean,
    scope                    text         NOT NULL DEFAULT '',
    lang                     varchar(8),
    timezone                 varchar(64)  NOT NULL DEFAULT 'UTC',
    capabilities             jsonb        NOT NULL DEFAULT '{}'::jsonb,
    token_user_id            integer,
    access_token_enc         bytea,
    refresh_token_enc        bytea,
    token_expires_at         timestamptz,
    token_refreshed_at       timestamptz,
    token_admin_verified_at  timestamptz,
    token_version            integer      NOT NULL DEFAULT 0,
    token_status             varchar(24)  NOT NULL DEFAULT 'ok',
    application_token_enc    bytea,
    placements               jsonb        NOT NULL DEFAULT '{}'::jsonb,
    purge_pending            boolean      NOT NULL DEFAULT false,
    purge_bodies             boolean      NOT NULL DEFAULT false,
    last_event_ts            timestamptz,
    installed_at             timestamptz  NOT NULL DEFAULT now(),
    install_completed_at     timestamptz,
    uninstalled_at           timestamptz,
    last_opened_at           timestamptz,
    last_admin_opened_at     timestamptz,
    created_at               timestamptz  NOT NULL DEFAULT now(),
    updated_at               timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT portals_member_id_key UNIQUE (member_id),
    CONSTRAINT portals_member_id_fmt CHECK (member_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT portals_status_chk CHECK (status IN ('active','uninstalled')),
    CONSTRAINT portals_token_status_chk CHECK (token_status IN
        ('ok','reauth_required','no_stats_permission','method_missing','filter_unsupported'))
);
CREATE TRIGGER portals_updated_at BEFORE UPDATE ON portals FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX portals_status_idx ON portals (status) WHERE status = 'active';
CREATE INDEX portals_purge_idx  ON portals (id) WHERE purge_pending;

COMMENT ON TABLE  portals IS 'One row per Bitrix24 portal (tenant). Row is kept forever; uninstall only flips status and wipes API tokens.';
COMMENT ON COLUMN portals.member_id IS 'Tenant key from Bitrix24 (32 hex chars); stable across domain renames. NOT a secret: it authorises nothing on its own.';
COMMENT ON COLUMN portals.domain IS 'Last DOMAIN seen from a verified open; display/CSP only, never a lookup key or REST base.';
COMMENT ON COLUMN portals.client_endpoint IS 'REST base URL. Written ONLY from an OAuth refresh response (on-premise safe). Never derived from DOMAIN.';
COMMENT ON COLUMN portals.server_endpoint IS 'SERVER_ENDPOINT as sent by the portal; refresh URL = its host + /oauth/token/, host must be in OAUTH_HOST_ALLOWLIST.';
COMMENT ON COLUMN portals.app_status IS 'status field from Bitrix24 (F/D/T/P/L/S...). Free-form on purpose.';
COMMENT ON COLUMN portals.installed_flag IS 'app.info INSTALLED as last observed; false => re-render the install page for admins.';
COMMENT ON COLUMN portals.capabilities IS '{"statistic_get": true, "operating_limit_s": 480, "event_bind": {...}, "checked_at": "..."}.';
COMMENT ON COLUMN portals.token_user_id IS 'Bitrix user id owning the stored portal token. Always an administrator (proven by user.admin before the write).';
COMMENT ON COLUMN portals.access_token_enc IS 'ENCRYPTED (AES-GCM envelope, AAD member_id:access_token). Used ONLY by the sync worker.';
COMMENT ON COLUMN portals.refresh_token_enc IS 'ENCRYPTED. Single-use, rotates on every refresh under row lock.';
COMMENT ON COLUMN portals.token_refreshed_at IS 'When the current refresh token was obtained; it dies 180 days later (banner at 150, re-seed offered from 120).';
COMMENT ON COLUMN portals.token_admin_verified_at IS 'Last time user.admin=true was proven for the stored token (at write time and daily by the worker).';
COMMENT ON COLUMN portals.token_version IS 'Bumped on every refresh/re-seed. Single-flight: a caller that saw expired_token re-reads under FOR UPDATE and skips the refresh if this moved.';
COMMENT ON COLUMN portals.token_status IS 'ok | reauth_required | no_stats_permission | method_missing | filter_unsupported (build ignores ID filter operators).';
COMMENT ON COLUMN portals.application_token_enc IS 'ENCRYPTED. Verifies EVERY inbound event (constant-time compare) once stored. Replaced only by an admin-proven ONAPPUPDATE or a verified admin open. Kept after uninstall so late/duplicate events still verify.';
COMMENT ON COLUMN portals.placements IS '{"CRM_DEAL_DETAIL_TAB":{"ok":true,"at":"..."},...} for idempotent re-bind.';
COMMENT ON COLUMN portals.purge_pending IS 'Set by ONAPPUNINSTALL or the inferred-uninstall rule; the worker deletes calls/employees/crm_contexts in chunks under tenant context and clears it. Sync never runs while true.';
COMMENT ON COLUMN portals.purge_bodies IS 'Set together with purge_pending when data[CLEAN]=1: the purge also NULLs rest_log request/response bodies of this portal.';
COMMENT ON COLUMN portals.last_event_ts IS 'Highest ts{} accepted from a lifecycle event; older or duplicate events are 200 no-ops.';
"""

# --------------------------------------------------------------------------------------
# §3 - portal_sync: cursors, throttle state, lease
# --------------------------------------------------------------------------------------
PORTAL_SYNC = """
CREATE TABLE portal_sync (
    portal_id             bigint PRIMARY KEY REFERENCES portals(id) ON DELETE CASCADE,
    sync_generation       integer     NOT NULL DEFAULT 0,
    high_id               bigint      NOT NULL DEFAULT 0,
    low_id                bigint,
    rescan_from_id        bigint,
    backfill_status       varchar(16) NOT NULL DEFAULT 'pending',
    backfill_total        integer,
    backfill_done         integer     NOT NULL DEFAULT 0,
    backfill_started_at   timestamptz,
    backfill_finished_at  timestamptz,
    batch_pages           smallint    NOT NULL DEFAULT 20,
    clean_visits          smallint    NOT NULL DEFAULT 0,
    last_incremental_at   timestamptz,
    last_rescan_at        timestamptz,
    last_recheck_at       timestamptz,
    last_employees_at     timestamptz,
    last_appinfo_at       timestamptz,
    next_run_at           timestamptz NOT NULL DEFAULT now(),
    lease_owner           varchar(64),
    lease_expires_at      timestamptz,
    run_started_at        timestamptz,
    operating_seconds     numeric(8,2),
    operating_reset_at    timestamptz,
    throttle_hits         integer     NOT NULL DEFAULT 0,
    consecutive_failures  integer     NOT NULL DEFAULT 0,
    rejected_rows         integer     NOT NULL DEFAULT 0,
    last_error_code       varchar(64),
    last_error_text       text,
    last_error_at         timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT portal_sync_backfill_chk CHECK (backfill_status IN ('pending','head','running','done','failed')),
    CONSTRAINT portal_sync_batch_pages_chk CHECK (batch_pages BETWEEN 1 AND 50)
);
CREATE TRIGGER portal_sync_updated_at BEFORE UPDATE ON portal_sync FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX portal_sync_due_idx ON portal_sync (next_run_at);

COMMENT ON TABLE  portal_sync IS 'Sync cursors, throttle and lease state, 1:1 with portals. No customer data; no RLS (the tick scans across portals).';
COMMENT ON COLUMN portal_sync.sync_generation IS 'Fencing token. Bumped by install/reinstall/uninstall. Every cursor/upsert transaction updates WHERE lease_owner=:me AND lease_expires_at>now() AND sync_generation=:gen and aborts on 0 rows, so a stale runner can never write.';
COMMENT ON COLUMN portal_sync.high_id IS 'Forward cursor: every statistic row with ID <= high_id that existed at that time has been upserted. Incremental = FILTER {">ID": high_id}, SORT ID ASC. Advances only over an error-free batch prefix.';
COMMENT ON COLUMN portal_sync.low_id IS 'Backward cursor: next backfill fetch = FILTER {"<ID": low_id}, SORT ID DESC. NULL until head_fetch. Backfill done when a fetch returns no rows.';
COMMENT ON COLUMN portal_sync.rescan_from_id IS 'Persisted lower bound of the trailing rescan window: the high_id observed ~RESCAN_WINDOW_HOURS ago, advanced monotonically. Never NULL-derived from an empty window.';
COMMENT ON COLUMN portal_sync.backfill_status IS 'pending -> head -> running -> done | failed. head_fetch runs for pending AND head (idempotent), so a crash between its two steps cannot wedge the portal.';
COMMENT ON COLUMN portal_sync.batch_pages IS 'statistic.get commands per batch. Fixed 20; halved (min 5) on QUERY_LIMIT_EXCEEDED or an operating soft-limit hit; restored to 20 after CLEAN_VISITS_TO_RECOVER clean visits. Never grown beyond 20 in v1.';
COMMENT ON COLUMN portal_sync.run_started_at IS 'Set when sync_portal actually begins. A lease that expires without a started run is a dispatch miss, not a crash, and does not increment consecutive_failures.';
COMMENT ON COLUMN portal_sync.throttle_hits IS 'Rate/operating-limit hits, counted separately from consecutive_failures so a legitimately throttled backfill is never put into the 6 h failure pause.';
COMMENT ON COLUMN portal_sync.rejected_rows IS 'Rows quarantined by the parser (unparsable/oversized). Non-zero is a support signal, never a blocker.';
"""

# --------------------------------------------------------------------------------------
# §3 - calls: metadata cache of voximplant.statistic.get
# --------------------------------------------------------------------------------------
CALLS = """
CREATE TABLE calls (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portal_id            bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    bx_id                bigint       NOT NULL,
    call_id              varchar(255),
    external_call_id     varchar(255),
    call_category        varchar(64),
    call_type            smallint,
    call_start_date      timestamptz  NOT NULL,
    call_duration        integer      NOT NULL DEFAULT 0,
    call_failed_code     varchar(32),
    call_failed_reason   text,
    portal_user_id       integer,
    portal_number        varchar(128),
    phone_number         varchar(128),
    crm_entity_type      varchar(32),
    crm_entity_id        integer,
    crm_activity_id      bigint,
    cost                 numeric(12,4),
    cost_currency        varchar(8),
    call_vote            smallint,
    call_record_url      text,
    record_file_id       bigint,
    record_duration      integer,
    rest_app_id          integer,
    rest_app_name        varchar(255),
    transcript_id        bigint,
    transcript_pending   boolean,
    session_id           bigint,
    redial_attempt       smallint,
    comment              text,
    call_log             text,
    has_record           boolean GENERATED ALWAYS AS
                         (record_file_id IS NOT NULL OR coalesce(call_record_url,'') <> '') STORED,
    result_group         varchar(16) GENERATED ALWAYS AS
                         (CASE call_failed_code WHEN '200' THEN 'answered'
                                                WHEN '304' THEN 'missed'
                                                ELSE 'not_connected' END) STORED,
    record_recheck_count smallint     NOT NULL DEFAULT 0,
    refresh_requested    boolean      NOT NULL DEFAULT false,
    first_seen_at        timestamptz  NOT NULL DEFAULT now(),
    last_synced_at       timestamptz  NOT NULL DEFAULT now(),
    content_changed_at   timestamptz,
    CONSTRAINT calls_portal_bx_id_key UNIQUE (portal_id, bx_id)
);
CREATE INDEX calls_portal_start_idx
    ON calls (portal_id, call_start_date DESC)
    INCLUDE (portal_user_id, call_type, result_group, call_duration, rest_app_id, has_record);
CREATE INDEX calls_portal_user_start_idx
    ON calls (portal_id, portal_user_id, call_start_date DESC);
CREATE INDEX calls_portal_crm_entity_idx
    ON calls (portal_id, crm_entity_type, crm_entity_id, call_start_date DESC)
    WHERE crm_entity_id IS NOT NULL;
CREATE INDEX calls_portal_activity_idx
    ON calls (portal_id, crm_activity_id)
    WHERE crm_activity_id IS NOT NULL;
CREATE INDEX calls_portal_rest_app_idx
    ON calls (portal_id, rest_app_id);
CREATE INDEX calls_portal_recheck_idx
    ON calls (portal_id, call_start_date)
    WHERE record_file_id IS NULL AND coalesce(call_record_url,'') = ''
      AND call_duration > 0 AND record_recheck_count < 2;
CREATE INDEX calls_portal_refresh_idx
    ON calls (portal_id)
    WHERE refresh_requested;

COMMENT ON TABLE  calls IS 'Metadata cache of voximplant.statistic.get rows. Never stores audio. Purged on uninstall. No CHECK constraints on Bitrix-controlled values: one unexpected enum must never stall the sync cursor.';
COMMENT ON COLUMN calls.bx_id IS 'Statistic record ID from Bitrix24 (monotonic per portal). Upsert key with portal_id and the sync cursor.';
COMMENT ON COLUMN calls.call_start_date IS 'Parsed from ISO-8601 with offset; stored UTC. A row without a parsable start date is quarantined, not inserted. Aggregations convert to the viewer tz in SQL.';
COMMENT ON COLUMN calls.call_type IS 'Raw integer as delivered (1..5 documented); unknown codes are stored and displayed as "Other".';
COMMENT ON COLUMN calls.call_failed_code IS 'Raw string: 200, 304, 486, 603, 603-S, 403, 404, 480, 484, 503, 402, 423, OTHER, or anything new.';
COMMENT ON COLUMN calls.crm_entity_type IS 'Raw string as delivered. CONTACT | COMPANY | LEAD are documented; DEAL and dynamic types are stored verbatim if a portal emits them and the deal tab matches them too.';
COMMENT ON COLUMN calls.cost IS 'NUMERIC because Bitrix24 serializes "0.0000" strings.';
COMMENT ON COLUMN calls.call_record_url IS 'Stored with credential-bearing query parameters (auth, token, sig) STRIPPED by the parser; never returned to the browser; excluded from the content-change comparison because it can be per-read volatile.';
COMMENT ON COLUMN calls.rest_app_id IS 'Integration that produced the call; NULL = built-in telephony. Drives the "line / source" filter.';
COMMENT ON COLUMN calls.has_record IS 'Single server-side definition of "has recording" used by the table, the player and the recheck job.';
COMMENT ON COLUMN calls.result_group IS 'Single server-side mapping for the result filter and summary cards; raw code is still shown in the UI.';
COMMENT ON COLUMN calls.record_recheck_count IS 'Budget for the targeted missing-recording recheck (max 2) beyond the 72 h rescan window.';
COMMENT ON COLUMN calls.refresh_requested IS 'Set by POST /api/v1/calls/{id}/refresh (playback got 403/404); the next sync visit re-reads these rows by ID and clears it.';
COMMENT ON COLUMN calls.content_changed_at IS 'Set only when an upsert changed a data column other than call_record_url (IS DISTINCT FROM).';
"""

# --------------------------------------------------------------------------------------
# §3 - employees: per-portal user_brief cache
# --------------------------------------------------------------------------------------
EMPLOYEES = """
CREATE TABLE employees (
    portal_id      bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    bx_user_id     integer      NOT NULL,
    name           varchar(255),
    last_name      varchar(255),
    second_name    varchar(255),
    work_position  varchar(255),
    photo_url      text,
    active         boolean      NOT NULL DEFAULT true,
    found          boolean      NOT NULL DEFAULT true,
    departments    integer[]    NOT NULL DEFAULT '{}',
    fetched_at     timestamptz,
    PRIMARY KEY (portal_id, bx_user_id)
);
CREATE INDEX employees_stale_idx ON employees (portal_id, fetched_at NULLS FIRST);

COMMENT ON TABLE  employees IS 'Per-portal user_brief cache (no contact details by scope design). Includes dismissed users (active=false) because historical calls reference them.';
COMMENT ON COLUMN employees.found IS 'false when user.get returned nothing for this id; retried daily instead of every cycle; UI shows "User #id".';
COMMENT ON COLUMN employees.fetched_at IS 'NULL = placeholder inserted by the call upsert for an unseen PORTAL_USER_ID; refresh job takes NULLs first, then rows older than EMPLOYEE_TTL_HOURS.';
"""

# --------------------------------------------------------------------------------------
# §3 - crm_contexts: resolved CRM entity -> call matching keys
# --------------------------------------------------------------------------------------
CRM_CONTEXTS = """
CREATE TABLE crm_contexts (
    portal_id            bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    entity_type          varchar(16)  NOT NULL,
    entity_id            integer      NOT NULL,
    entity_keys          jsonb        NOT NULL DEFAULT '[]'::jsonb,
    activity_ids         bigint[]     NOT NULL DEFAULT '{}',
    resolved_by_user_id  integer,
    resolved_at          timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (portal_id, entity_type, entity_id),
    CONSTRAINT crm_contexts_type_chk CHECK (entity_type IN ('DEAL','LEAD','CONTACT','COMPANY'))
);
CREATE INDEX crm_contexts_age_idx ON crm_contexts (resolved_at);

COMMENT ON TABLE  crm_contexts IS 'Cache of what a CRM tab must match: entity_keys = [["CONTACT",12],["COMPANY",3]] plus call-activity ids bound to the entity. Written ONLY from a fully successful resolution by the current opener; rows older than 30 days are purged. entity_type is ours (from the PLACEMENT allowlist), so it is constrained.';
COMMENT ON COLUMN crm_contexts.resolved_at IS 'A read is served only from a row resolved not earlier than the JWT was minted; otherwise the SPA re-runs the session exchange, which re-resolves with the current user token.';
COMMENT ON COLUMN crm_contexts.activity_ids IS 'crm.activity.list (OWNER_TYPE_ID of the entity, TYPE_ID=2) ids, capped at CRM_ACTIVITY_CAP (250 = 5 pages x 50, the handler''s page budget).';
"""

# --------------------------------------------------------------------------------------
# §3 - rest_log: moderation requirement (>= 3 days)
# --------------------------------------------------------------------------------------
REST_LOG = """
CREATE TABLE rest_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts              timestamptz  NOT NULL DEFAULT now(),
    portal_id       bigint       REFERENCES portals(id) ON DELETE SET NULL,
    member_id       varchar(32),
    correlation_id  uuid,
    direction       varchar(3)   NOT NULL,
    kind            varchar(16)  NOT NULL,
    method          varchar(128) NOT NULL,
    url             text         NOT NULL,
    token_user_id   integer,
    request         jsonb,
    http_status     smallint,
    error_code      varchar(64),
    response        jsonb,
    response_bytes  integer,
    truncated       boolean      NOT NULL DEFAULT false,
    time_block      jsonb,
    duration_ms     integer,
    CONSTRAINT rest_log_direction_chk CHECK (direction IN ('out','in')),
    CONSTRAINT rest_log_kind_chk CHECK (kind IN ('rest','oauth','event','open','install'))
);
CREATE INDEX rest_log_ts_idx        ON rest_log (ts);
CREATE INDEX rest_log_portal_ts_idx ON rest_log (portal_id, ts DESC);

COMMENT ON TABLE  rest_log IS 'Every outbound REST/OAuth request+response and every inbound Bitrix24 POST, kept >= 3 days (default 7). Written in its own short transaction so a failed work transaction still leaves the exchange logged. Support-only; not exposed to tenants.';
COMMENT ON COLUMN rest_log.portal_id IS 'NULL for calls made before the portal row exists (install-time refresh exchange) and for rejected events of unknown portals; member_id keeps the attribution.';
COMMENT ON COLUMN rest_log.request IS 'Recursively redacted: any key matching /(token|secret|auth|password)/i at any nesting depth, plus auth=/token= parameters inside URL strings.';
COMMENT ON COLUMN rest_log.response IS 'Recursively redacted JSON body up to REST_LOG_BODY_LIMIT bytes (default 256 KiB), then truncated=true with response_bytes recorded. NULLed for a portal that uninstalled with CLEAN=1.';
COMMENT ON COLUMN rest_log.time_block IS 'time{} of the response (or max over result_time for batch) for operating-limit diagnostics.';
"""

# --------------------------------------------------------------------------------------
# §3 - portal_events: lifecycle audit (no tokens, no retention purge)
# --------------------------------------------------------------------------------------
PORTAL_EVENTS = """
CREATE TABLE portal_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portal_id   bigint       NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    kind        varchar(32)  NOT NULL,
    user_id     integer,
    details     jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX portal_events_portal_idx ON portal_events (portal_id, created_at DESC);

COMMENT ON TABLE  portal_events IS 'First thing on-call greps: install | reinstall | admin_open | token_reseeded | refresh_failed | domain_changed | app_update | event_rejected | row_rejected | sync_blocked | sync_unblocked | uninstall | uninstall_inferred | purge_done | purge_incomplete. details never contains tokens.';
"""

# --------------------------------------------------------------------------------------
# §3 - Row-Level Security: structural tenant isolation
# --------------------------------------------------------------------------------------
RLS = """
ALTER TABLE calls        ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls        FORCE  ROW LEVEL SECURITY;
ALTER TABLE employees    ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees    FORCE  ROW LEVEL SECURITY;
ALTER TABLE crm_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_contexts FORCE  ROW LEVEL SECURITY;

CREATE POLICY calls_tenant ON calls
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY employees_tenant ON employees
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
CREATE POLICY crm_contexts_tenant ON crm_contexts
    USING      (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint)
    WITH CHECK (portal_id = NULLIF(current_setting('app.portal_id', true), '')::bigint);
-- app.portal_id unset => predicate NULL => zero rows visible or writable. Fail closed, and SILENTLY:
-- every job that touches these tables MUST run inside tenant_txn(portal_id), which re-issues SET LOCAL
-- for EVERY transaction, and destructive jobs must verify their effect (see purge_portal).
"""

# --------------------------------------------------------------------------------------
# §3 - grants.
# Guarded: ca_owner / ca_app are created by docker/postgres/init.sql (ops, once). A developer
# migrating a plain local database has neither role, and the schema must still land there.
# --------------------------------------------------------------------------------------
GRANTS = """
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO ca_app';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON portals, portal_sync, calls, employees, crm_contexts, rest_log, portal_events TO ca_app';
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ca_app';
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_owner') THEN
            EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ca_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ca_app';
            EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE ca_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ca_app';
        END IF;
    END IF;
END
$do$;
"""

# Reverse dependency order: children before parents, function last (the triggers that use it
# die with their tables). Never used in production - the baseline is forward-only - but a
# wrong downgrade would silently strand a developer's database.
DOWNGRADE = """
DROP TABLE IF EXISTS portal_events;
DROP TABLE IF EXISTS rest_log;
DROP TABLE IF EXISTS crm_contexts;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS calls;
DROP TABLE IF EXISTS portal_sync;
DROP TABLE IF EXISTS portals;
DROP FUNCTION IF EXISTS set_updated_at();
"""


def _run(sql: str) -> None:
    """Execute raw DDL without SQLAlchemy's bind-parameter parsing.

    `op.execute(str)` wraps the string in `text()`, which treats `:name` as a bind
    parameter. Our COMMENT ON bodies legitimately contain colon-prefixed words
    (`lease_owner=:me`, `AAD member_id:access_token`, `{"ok":true}`), so `text()`
    would demand bind values for them and the migration would never run.
    `exec_driver_sql` passes the SQL straight to the driver.
    """
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    """Create the §3 schema. One statement group per section so a failure names the section."""
    _run(SET_UPDATED_AT)
    _run(PORTALS)
    _run(PORTAL_SYNC)
    _run(CALLS)
    _run(EMPLOYEES)
    _run(CRM_CONTEXTS)
    _run(REST_LOG)
    _run(PORTAL_EVENTS)
    _run(RLS)
    _run(GRANTS)


def downgrade() -> None:
    """Drop everything this revision created, children first."""
    _run(DOWNGRADE)
