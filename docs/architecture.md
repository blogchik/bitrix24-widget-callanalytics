# Call Analytics (`texnobus.callanalytics`) — Final Design v1 (revision 2)

Bitrix24 Marketplace application: one deployment serves many portals, reads only `voximplant.statistic.get`, presents a telephony dashboard (left-menu page) and a per-entity call list (four CRM detail tabs). This revision folds in the adversarial review: the trust root is tightened (no endpoint may ever be learned from an unproven payload, no portal credential may be written by a non-admin token), the sync cursor can no longer skip or stall on bad data, RLS-bound jobs are forced to run in a tenant transaction, and the session token never touches a response header. The bar remains minimal-but-correct: PostgreSQL is the only stateful component, there is no Redis/Celery/broker, and every piece of state that drives work lives in a table column so a crash never loses work.

---

## 1. Overview & key decisions

1. **Tenant key is `member_id`; every tenant row carries a surrogate `portal_id` (bigint)** — `member_id` is the only external lookup key (stable across domain renames), the bigint keeps every index small and every PK/FK tenant-prefixed. `member_id` is **not a secret**: every vendor installed on the portal and every employee (`BX24.getAuth()`) can read it, so it never authorises anything by itself.
2. **The OAuth refresh exchange at the allowlisted OAuth host is the only trust root, and `client_endpoint` is written *only* from an OAuth refresh response** — never from `DOMAIN`, never from an `app.info` probe against a caller-supplied host. Portals that cannot refresh (fully isolated on-premise boxes with a custom auth provider) are **out of scope in v1** and get an explicit state page; this closes the forged-`DOMAIN` takeover of both new and existing tenants.
3. **User tokens are verified only against the stored `client_endpoint`** — a user `AUTH_ID` from another portal (or a fake portal) fails there, so `member_id` spoofing on `/app/` is impossible.
4. **Any write of the portal sync credential requires a proven administrator**: the token must pass `user.current` + `user.admin = true` at the OAuth-derived `client_endpoint` before it is stored. This holds for `/install/`, self-heal, re-authorize and every lifecycle event; a non-admin token can never become the worker's credential (which would silently cache only that user's calls).
5. **Lifecycle events are verified by `application_token` first**: if a token is stored, constant-time equality is required for every event including `ONAPPINSTALL`/`ONAPPUSERREADY`. `ONAPPUPDATE` is the only event that may replace it, and only when its `auth[access_token]` proves `user.admin=true` at the stored endpoint **and** its `auth[refresh_token]` exchanges to the same `member_id`.
6. **Cookie-free sessions: a stateless HS256 JWT minted per open, handed to the SPA by a tiny inline-script page (`location.replace`), never in a `Location` header** — third-party cookies are unreliable in the Bitrix24 iframe, and a bearer token in a redirect header would be written to the reverse-proxy access log on every open of every tenant. The page forwards Bitrix24's **original query string verbatim** (`DOMAIN`, `PROTOCOL`, `LANG`, `APP_SID`), because the BX24 JS SDK needs `APP_SID` to talk to the parent frame.
7. **Permissions are Bitrix24's answer, resolved at every open: `user.admin` + (for non-admins) one `voximplant.statistic.get` probe with the user's own token → `all | own | denied`** — no role system of our own; `denied` renders the mandated explanatory state, never an empty screen.
8. **Structural isolation: Row-Level Security ENABLED and FORCED on customer-data tables, runtime role `ca_app` is `NOBYPASSRLS`, and every transaction that touches them starts with `SET LOCAL app.portal_id`** — a query that forgets the context returns zero rows instead of another tenant's rows. Because RLS fails *silently* closed, every job that writes or deletes customer rows (purge, upserts, CRM context) must open `tenant_session(portal_id)` **per transaction**, and the purge verifies emptiness before declaring success.
9. **Sync cursor is the statistics record `ID`, never `CALL_START_DATE`; upsert key `(portal_id, bx_id)`** — rows are created at call finish and may carry backdated start dates, so only the monotonic ID captures late-finished calls without overlap windows.
10. **A batch cursor advances only across the longest error-free *prefix* of its commands** — one failed sub-command in a 50-page batch would otherwise leave a permanent 50-row hole that neither the forward nor the backward cursor ever revisits.
11. **Bitrix24 data is never allowed to poison a chunk**: no CHECK constraints on Bitrix-controlled enums, wide string columns, per-row parsing with quarantine (`portal_events(row_rejected)`), in-chunk dedupe by `bx_id`, and a SAVEPOINT retry — a single unexpected value can never stall the cursor forever.
12. **`head_fetch` of the newest 2,500 rows first, then a descending backfill (`<low_id`), with a fixed batch size of 20 pages (halving to 5 under limit errors)** — the moderator's dashboard shows recent days within seconds of install; the `<low_id` window is immutable so offsets in one batch cannot drift. `head_fetch` is idempotent and re-runs from `pending` *or* `head`, so a crash between its two steps cannot wedge the portal.
13. **Worker writes are fenced**: every cursor/upsert transaction updates `portal_sync` `WHERE lease_owner = :me AND lease_expires_at > now() AND sync_generation = :gen` and aborts on zero rows; uninstall/reinstall bump `sync_generation`, so an in-flight run can never re-insert rows after a purge or clobber a newer runner's cursor. All REST calls carry a 120 s timeout, far below the 5-minute lease.
14. **Late updates are handled by an ID-window rescan (72 h, with a persisted `rescan_from_id`) plus a per-row recording recheck budget (max 2) plus an on-demand refresh flag** — recordings/votes/comments are attached after the row exists and Bitrix24 exposes no modified date.
15. **Durable state instead of a job queue: every unit of work is derivable from `portal_sync` / `portals` columns; APScheduler is only a ticker and dispatch is `asyncio.create_task` under a semaphore** — a worker crash loses nothing, and a leased-but-undispatched portal is never charged a phantom failure.
16. **Single-flight token refresh with `token_version` under `SELECT ... FOR UPDATE`; refresh only on `expired_token`, on imminent expiry, when the chain is dead/aged, or on an explicit "Re-authorize"** — never on a routine open and never on a schedule, and rate-limited per `member_id`/IP so an employee cannot script refresh exchanges against our client_id.
17. **Uninstall makes no REST calls; the webhook flips status, wipes API tokens, keeps `application_token`, bumps `sync_generation`, and the worker purges rows in 10k chunks under tenant context; retries older than `install_completed_at` are ignored** — API access is already revoked at that moment, and a stale retry must not wipe a fresh reinstall.
18. **A fallback uninstall path**: if the events URL cannot be registered (or an event is missed), a portal whose token chain is dead and which no admin has opened for `UNINSTALL_GRACE_DAYS` (30) is marked `uninstalled` and purged (`portal_events(uninstall_inferred)`) — brief rule 7 must not depend on an unconfirmed cabinet field. `/app/`, `/install/` and `/settings/` also detect an `event=` body and dispatch it to the events handler.
19. **Tokens encrypted at rest with an AES-256-GCM envelope carrying a key id and AAD = `member_id:column`** — a ciphertext copied between rows or tenants does not decrypt. v1 ships one key; the envelope's `key_id` byte keeps rotation a later background task, not a migration.
20. **`rest_log` records every outbound REST/OAuth call and every inbound Bitrix24 POST, recursively redacted, written in its own short transaction, kept 7 days (configuration refuses < 3)** — the moderation requirement plus the `time{}` block the throttle needs; a failed work transaction must still leave the exchange logged.
21. **Recording playback is credential-safe by construction**: `GET /api/v1/calls/{id}/record?t=<short-lived signed URL token>` (an `<audio src>` cannot send an `Authorization` header); for non-admin viewers the proxy uses the **viewer's own** token so Bitrix24's separate "listen to recordings" right is enforced; `call_record_url` is never returned to the browser, is stored with credential-bearing query parameters stripped, is excluded from change detection, and `RECORDING_MODE=redirect` is forbidden unless the spike proves the URL carries no credential.
22. **Deal tab context is resolved at open time with the *opener's* token and cached in `crm_contexts`; if any CRM command errors the tab renders a "no access to this item" state and no entity JWT is minted** — a cached context resolved by a privileged user must never be served to a user who cannot see the entity.
23. **`GET /app/` self-heals a broken endpoint**: a transport-level failure of the open batch (portal renamed, custom domain connected) triggers one refresh exchange to re-learn `client_endpoint`, then one retry — otherwise a rename would deadlock the tenant permanently.
24. **Every non-happy path is a rendered, translated state page**, and all Bitrix24-facing endpoints use strict input allowlists with a rendered "bad request" page. A scanner sees nothing exploitable and nothing blank.
25. **Capability probe at install (`method.get voximplant.statistic.get`) recorded in `portals.capabilities`, plus a runtime filter-honoured assertion** — on-premise builds lag the cloud; a missing method or an ignored `>ID` operator becomes an explicit terminal portal state instead of a mysterious failure or a hot loop.

---

## 2. Repository file structure

```
.
├── README.md                              # how to run locally, env vars, links to docs/
├── .env.example                           # every env var with a comment; no real values
├── .gitignore
├── Makefile                               # up / down / migrate / test / lint shortcuts
├── docker-compose.yml                     # postgres, api, worker (same image), web; ports bound to 127.0.0.1
├── docker-compose.dev.yml                 # dev overrides: bind mounts, hot reload
├── docker/
│   ├── Caddyfile.snippet                  # site block: TLS, routing, `header -X-Frame-Options`,
│   │                                      # log filter deleting resp_headers>Location and request query strings
│   └── postgres/init.sql                  # creates roles ca_owner / ca_app and the database (run once, not Alembic)
├── docs/
│   ├── architecture.md                    # this document, kept current
│   ├── moderation-checklist.md            # §4.11 table as a manual test script (+ screenshots after milestone 5)
│   ├── bitrix24-flows.md                  # real install/open/event payload samples (tokens redacted)
│   ├── spike-recording-playback.md        # §9 protocol and captured results
│   └── adr/                               # 0001-no-cookies, 0002-id-cursor, 0003-rls, 0004-state-not-queue,
│                                          # 0005-admin-proof-for-credentials, 0006-no-jwt-in-headers
├── api/                                   # Python 3.12 / FastAPI / SQLAlchemy 2 / Alembic / httpx / APScheduler
│   ├── Dockerfile                         # one image; CMD differs for api (uvicorn) and worker (python -m app.worker)
│   ├── pyproject.toml                     # fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic, httpx,
│   │                                      # pydantic-settings, cryptography, pyjwt, apscheduler
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py                         # runs with DATABASE_URL_MIGRATIONS (ca_owner)
│   │   └── versions/0001_baseline.py      # DDL of §3 incl. RLS policies, grants, comments
│   ├── app/
│   │   ├── main.py                        # FastAPI factory, routers, request-id + JSON logging middleware, /healthz
│   │   ├── worker.py                      # worker entrypoint: builds the job backend and blocks
│   │   ├── config.py                      # pydantic-settings; secrets from env only; REST_LOG_RETENTION_DAYS >= 3
│   │   ├── logging.py                     # JSON logs to stdout; recursive redaction filter;
│   │   │                                  # httpx/httpcore loggers pinned to WARNING; no request bodies in handlers
│   │   ├── db/
│   │   │   ├── engine.py                  # async engine on DATABASE_URL (ca_app role)
│   │   │   ├── session.py                 # tenant_txn(portal_id): one transaction with SET LOCAL app.portal_id,
│   │   │   │                              # re-issued for every transaction; control_txn() for portals/portal_sync/rest_log
│   │   │   └── models.py                  # SQLAlchemy 2.x mapped classes mirroring §3
│   │   ├── api/                       # /api/v1: session, dashboard, filters, calls, hours,
│   │   │                              # deals (§4.12), utm (§4.13), record, portal
│   │   ├── bitrix/                    # client, oauth, crm, statistic, users, deals (§4.12),
│   │   │                              # utm (§4.13) - pure command builders, no HTTP
│   │   ├── services/                  # stats, calls_repo, crm_context, employees, portals,
│   │   │                              # deal_stats (§4.12), utm_stats (§4.13)
│   │   ├── security/
│   │   │   ├── crypto.py                  # AES-256-GCM envelope: key_id||nonce||ct||tag, AAD=member_id:column
│   │   │   ├── session_token.py           # JWT HS256 issue/verify (session) + short-lived playback URL token
│   │   │   ├── redact.py                  # recursive key-regex redaction used by rest_log and logging
│   │   │   └── principal.py               # Principal dataclass + get_principal dependency (§4.7)
│   │   ├── bitrix/
│   │   │   ├── forms.py                   # allowlist parsing of iframe POSTs and PHP-bracket event bodies;
│   │   │   │                              # detects an `event=` body on any Bitrix24-facing endpoint
│   │   │   ├── errors.py                  # BitrixError hierarchy keyed by the JSON `error` string (case-insensitive)
│   │   │   ├── oauth.py                   # refresh_token grant at the allowlisted OAuth host; single-flight;
│   │   │   │                              # per-member_id/IP exchange rate limiter
│   │   │   ├── client.py                  # httpx client (120 s timeout): call(), batch(halt=0); refresh-once-then-fail;
│   │   │   │                              # time{} capture; rest_log writer (own transaction); Retry-After
│   │   │   ├── identity.py                # verify_admin_token(): user.current + user.admin at a given endpoint
│   │   │   ├── placements.py              # placement.get / placement.bind / event.bind wrappers with LANG_ALL titles
│   │   │   ├── users.py                   # user.current, user.admin, user.get (@ID chunks, ADMIN_MODE fallback)
│   │   │   ├── statistic.py               # typed parser: "1"→int, "0.0000"→Decimal, ''/null→NULL, Y/N→bool,
│   │   │   │                              # tz-aware dates, record-url credential stripping, per-row failure isolation
│   │   │   └── crm.py                     # crm.deal.get, crm.deal.contact.items.get, crm.activity.list
│   │   ├── handlers/                      # Bitrix24-facing endpoints (form POST → HTML)
│   │   │   ├── install.py                 # POST /install/
│   │   │   ├── open.py                    # POST /app/ and POST /settings/ (one handler; routes on PLACEMENT)
│   │   │   ├── events.py                  # POST /events/ (+ dispatched `event=` bodies from other endpoints)
│   │   │   └── templates/
│   │   │       ├── install.html           # loads //api.bitrix24.com/api/v1/, BX24.init(() => BX24.installFinish())
│   │   │       ├── handoff.html           # inline script: location.replace(target + '#s=' + jwt); no JWT in headers
│   │   │       ├── state.html             # server-rendered states (not_installed, bad_request, method_missing, …)
│   │   │       └── error.html             # last-resort translated error page with request id
│   │   ├── api/                           # JSON API for the SPA, all under /api/v1, all behind get_principal
│   │   │   ├── router.py
│   │   │   ├── session.py                 # POST /session/exchange (fresh BX24.getAuth token → new JWT); GET /me
│   │   │   ├── dashboard.py               # GET /dashboard: summary + per-day + hour×weekday + per-employee (one query)
│   │   │   ├── calls.py                   # GET /calls (50/page); POST /calls/{id}/refresh; POST /calls/{id}/play-url
│   │   │   ├── filters.py                 # GET /filters: employees + lines (rest_app) for the portal
│   │   │   ├── record.py                  # GET /calls/{id}/record?t=<signed>: RECORDING_MODE=off|proxy (§9)
│   │   │   └── portal.py                  # GET /portal/sync-status, POST /portal/reauthorize (admin only)
│   │   ├── services/
│   │   │   ├── portals.py                 # install/upsert/reauthorize/uninstall transitions; credential writes
│   │   │   ├── access.py                  # open-time verification batch → access level (§4.7)
│   │   │   ├── calls_repo.py              # ALL reads of `calls`; scope_filter(principal) applied in exactly one place
│   │   │   ├── stats.py                   # SQL aggregation (GROUPING SETS) in the viewer's timezone
│   │   │   ├── employees.py               # cache reads; placeholder upsert; viewer upsert from user.current
│   │   │   └── crm_context.py             # resolve + cache deal/lead/contact/company → entity keys and activity ids
│   │   ├── sync/
│   │   │   ├── throttle.py                # per-portal pacing, backoff, Retry-After, operating-time guard (floored)
│   │   │   ├── fetch.py                   # one batch of N statistic.get pages; contiguous-prefix cursor rule
│   │   │   ├── upsert.py                  # dedupe, SAVEPOINT chunks, quarantine, ON CONFLICT, content_changed_at
│   │   │   ├── head_fetch.py              # newest 2,500 rows at install, idempotent (§5.2)
│   │   │   ├── incremental.py             # forward cursor (>high_id), probe-then-pack sizing
│   │   │   ├── backfill.py                # backward cursor (<low_id), resumable
│   │   │   ├── rescan.py                  # persisted rescan_from_id, recheck budget, refresh_requested rows
│   │   │   ├── employees_refresh.py       # user.get for placeholder/stale ids
│   │   │   └── purge.py                   # purge_portal_data (tenant txn, verified), purge_rest_log, redact_on_clean
│   │   └── jobs/
│   │       ├── protocol.py                # JobBackend Protocol: schedule_periodic(name, seconds), run_now(name, **kw)
│   │       ├── definitions.py             # tick(), sync_portal(portal_id), purge_portal(portal_id), purge_rest_log()
│   │       ├── apscheduler_backend.py     # v1 backend (AsyncIOScheduler; tick only); dispatch via asyncio + semaphore
│   │       └── __init__.py                # get_backend() from env JOB_BACKEND=apscheduler
│   └── tests/
│       ├── conftest.py                    # test DB with both roles; fixtures for two portals
│       ├── fixtures/statistic_page.json   # verbatim page with string-typed numbers + one unparsable row
│       ├── test_forms.py                  # allowlist validation, bracket-key parsing, scope separators, event= dispatch
│       ├── test_install_guard.py          # install POST for an existing member_id without a valid refresh changes nothing
│       ├── test_events_auth.py            # non-admin token + ONAPPUPDATE → 403, application_token unchanged;
│       │                                  # forged ONAPPUNINSTALL → 403; retry older than install_completed_at → 200 no-op
│       ├── test_client_refresh.py         # expired_token → exactly one refresh → retry → fail loudly
│       ├── test_refresh_singleflight.py   # two concurrent callers, one refresh; two admin opens → zero OAuth calls
│       ├── test_no_token_in_headers.py    # no response header of /app/ contains the JWT
│       ├── test_secret_logging.py         # mocked refresh: no secret/token substring in captured log output
│       ├── test_cursor.py                 # head_fetch/incremental/backfill math; failed page k stops the cursor at k-1;
│       │                                  # crash between head_fetch steps resumes; filter-violation guard
│       ├── test_upsert.py                 # poison row quarantined, cursor advances; duplicate bx_id in chunk; no churn
│       ├── test_fencing.py                # stale lease / bumped generation aborts the write
│       ├── test_isolation.py              # raw SQL under portal A sees only A; no context → zero rows
│       ├── test_purge.py                  # purge under control txn fails the assertion; under tenant txn empties tables
│       ├── test_registry_lint.py          # greps for portals lookups and raw DML on RLS tables outside allowed modules
│       └── test_access.py                 # all/own/denied/scope_error decisions from batch results
└── web/                                   # Next.js App Router + TypeScript + Tailwind (output: standalone)
    ├── Dockerfile
    ├── package.json  next.config.ts  tsconfig.json  tailwind.config.ts  postcss.config.js
    ├── middleware.ts                      # CSP frame-ancestors from validated DOMAIN/PROTOCOL; never X-Frame-Options
    ├── messages/                          # single message source, also copied into the api image at build
    │   ├── ru.json                        # mandatory
    │   ├── en.json
    │   └── (uz.json)                      # add file + one line in src/i18n/locales.json
    └── src/
        ├── i18n/locales.json              # shared: locale list + fallback map (kz→ru, uz→ru, *→en); read by api too
        ├── i18n/config.ts                 # next-intl wiring over locales.json, ICU plurals
        ├── app/
        │   ├── layout.tsx                 # loads BX24 script, IntlProvider, ResizeObserver → BX24.fitWindow()
        │   ├── page.tsx                   # "Open this app from Bitrix24" (no token)
        │   ├── dashboard/page.tsx         # left-menu view
        │   ├── crm/page.tsx               # CRM tab view (table + player only)
        │   ├── settings/page.tsx          # admin sync status, token state, placements, Re-authorize
        │   └── state/[kind]/page.tsx      # denied | not_installed | reauth | retry | scope | method_missing |
        │                                  # crm_no_access | unsupported_portal | error
        ├── components/                    # Filters, SummaryCards, CallsPerDayChart, HourWeekdayHeatmap,
        │                                  # EmployeeBars, CallsTable, InlinePlayer, StateCard, SyncBanner
        └── lib/
            ├── bx24.ts                    # init promise, fitWindow(), getAuth()/refreshAuth(), openPath()
            ├── session.ts                 # read #s= once, replaceState, memory + sessionStorage copy, exchange on 401
            ├── api.ts                     # fetch wrapper: Authorization: Bearer, retry-once via session exchange
            └── format.ts                  # durations, phone display, result-code → label map, direction map
```

Caddy (host, outside compose): `/app/*`, `/install/*`, `/settings/*`, `/events/*`, `/api/*`, `/healthz` → `api:8000`; everything else → `web:3000`. Caddy terminates TLS (Let's Encrypt DV), adds HSTS, strips `X-Frame-Options`, and its access log **deletes `resp_headers>Location` and request query strings** so no token or auth parameter is ever persisted there.

---

## 3. Database schema — PostgreSQL 16

Rules: surrogate `portal_id` on every tenant row; `member_id` unique on `portals` with a format check; customer-data tables (`calls`, `employees`, `crm_contexts`) carry FORCED RLS bound to the transaction-local `app.portal_id`; control-plane tables (`portals`, `portal_sync`, `rest_log`, `portal_events`) have no RLS because the worker tick and support must scan across portals and they hold no call data. **No CHECK constraint is placed on a value Bitrix24 controls** — unknown enum values are stored raw and mapped in the UI; only values we generate are constrained. Encrypted columns are `bytea` and end in `_enc`: envelope `key_id(1) || nonce(12) || ciphertext || tag(16)`, AAD = `member_id || ':' || column_name`.

```sql
-- ===================== roles (docker/postgres/init.sql; run once by ops) =====================
-- ca_owner: owns all objects, runs Alembic (bypasses RLS as owner).
-- ca_app:   runtime role for api + worker; cannot bypass RLS; no DDL.
CREATE ROLE ca_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE ca_app   LOGIN PASSWORD :'app_pw' NOBYPASSRLS;
CREATE DATABASE callanalytics OWNER ca_owner;

-- ===================== 0001_baseline (run as ca_owner) =====================
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ---------- portals: tenant registry + portal (installer) credential ----------
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

-- ---------- portal_sync: cursors, throttle state, lease ----------
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

-- ---------- calls: metadata cache of voximplant.statistic.get ----------
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
COMMENT ON COLUMN calls.result_group IS 'Single server-side mapping for the result filter and summary cards. Three stored values; services/stats.py collapses them once into the two the app speaks (answered / no_answer), and the raw code stays on the row.';
COMMENT ON COLUMN calls.record_recheck_count IS 'Budget for the targeted missing-recording recheck (max 2) beyond the 72 h rescan window.';
COMMENT ON COLUMN calls.refresh_requested IS 'Set by POST /api/v1/calls/{id}/refresh (playback got 403/404); the next sync visit re-reads these rows by ID and clears it.';
COMMENT ON COLUMN calls.content_changed_at IS 'Set only when an upsert changed a data column other than call_record_url (IS DISTINCT FROM).';

-- ---------- employees: per-portal user_brief cache ----------
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
    phone_inner    varchar(32),
    fetched_at     timestamptz,
    PRIMARY KEY (portal_id, bx_user_id)
);
CREATE INDEX employees_stale_idx ON employees (portal_id, fetched_at NULLS FIRST);

COMMENT ON TABLE  employees IS 'Per-portal user_brief cache (no contact details by scope design). Includes dismissed users (active=false) because historical calls reference them.';
COMMENT ON COLUMN employees.phone_inner IS 'UF_PHONE_INNER from user.get (user_brief scope). Internal telephony extension, shown beside the name in the employee filter. NULL = the portal sets none, or the row has not been refreshed since the column was added. Not a contact detail: email and personal phone are user_basic, which this app never requests.';
COMMENT ON COLUMN employees.found IS 'false when user.get returned nothing for this id; retried daily instead of every cycle; UI shows "User #id".';
COMMENT ON COLUMN employees.fetched_at IS 'NULL = placeholder inserted by the call upsert for an unseen PORTAL_USER_ID; refresh job takes NULLs first, then rows older than EMPLOYEE_TTL_HOURS.';

-- ---------- crm_contexts: resolved CRM entity -> call matching keys ----------
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

-- ---------- rest_log: moderation requirement (>= 3 days) ----------
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

-- ---------- portal_events: lifecycle audit (no tokens, no retention purge) ----------
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

-- ---------- Row-Level Security: structural tenant isolation ----------
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

-- ---------- grants ----------
GRANT USAGE ON SCHEMA public TO ca_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON portals, portal_sync, calls, employees, crm_contexts, rest_log, portal_events TO ca_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ca_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ca_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ca_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ca_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ca_app;
```

`alembic_version` is created by Alembic. Seven tables total; nothing else in v1.

### Per-table rationale

- **`portals`** — tenant registry and the single portal credential in one row: minimal, and the `token_version` / `FOR UPDATE` single-flight needs exactly one row to lock. `member_id` has a format CHECK so garbage never becomes a tenant, but it is treated as public data. `client_endpoint` is written only from the OAuth response. `token_admin_verified_at` records that the stored credential belongs to a proven administrator. `token_status` is the one column the settings page and dashboard banner read to explain why sync stopped. `application_token_enc` survives uninstall so late/duplicate lifecycle events still verify. `last_event_ts` makes event delivery idempotent and stops a retried uninstall from wiping a fresh reinstall. `purge_pending`/`purge_bodies` make uninstall cleanup (including the CLEAN=1 body wipe) durable without a queue. The ONAPPUSERREADY system-user credential is **not** stored in v1 (unused, and a stored 180-day token is a liability).
- **`portal_sync`** — everything the worker needs to resume from any crash, plus the two safety columns the review demanded: `sync_generation` (fencing) and `rescan_from_id` (no NULL-derived full rescans). `throttle_hits` separates "the portal is busy" from "the portal is broken". `run_started_at` distinguishes a crashed run from a dispatch miss. Separate from `portals` because it is rewritten after every batch and must never contend with the credential row's lock.
- **`calls`** — the cache. Natural key `(portal_id, bx_id)` is the upsert target; the surrogate `id` is only for the API's opaque row ids. Generated `has_record` and `result_group` make the recording and result mappings identical in filters, summaries and jobs; the API then collapses `result_group` to two outcomes in one place (`answered` / `no_answer`) so the filter and the charts cannot drift apart, while the raw `call_failed_code` stays on the row for the cell's title. Constraints on Bitrix-supplied values were removed and string widths raised so a novel `CALL_TYPE`, a `DEAL` entity type or a long failure code cannot abort a 500-row chunk. The range index is covering so summary/heatmap/per-employee aggregation is an index-only scan.
- **`employees`** — `user_brief` fields only (scope), which includes the internal extension `phone_inner` and excludes email and personal phone. `fetched_at` NULL placeholders are inserted by the call upsert so the refresh job never has to `SELECT DISTINCT` over `calls`; `found=false` stops retrying deleted users every cycle. Dismissed users are kept because their calls remain.
- **`crm_contexts`** — the resolved matching set for a CRM tab, written only by a successful resolution by the *current* opener and read only when at least as fresh as the JWT, so a privileged user's resolution is never replayed for someone who lacks CRM rights.
- **`rest_log`** — the moderation log for both directions with recursive redaction and a body cap; `portal_id` nullable so pre-install calls and rejected events are logged. Retention by batched `DELETE` on the `ts` index.
- **`portal_events`** — small lifecycle audit that outlives `rest_log` retention; the first thing support reads for a misbehaving portal, and the place a quarantined row is recorded. No tokens, ever.

---

## 4. Request & auth flow

### 4.1 Trust model
- Every field of a Bitrix24 POST is untrusted until proven. `member_id` proves nothing on its own; it is proven only by an OAuth refresh exchange. A user's `AUTH_ID` is proven by calling the **stored** `client_endpoint` with it. `DOMAIN` is only ever display/CSP data and is never used to build a REST base.
- The refresh URL is built from the portal-sent `SERVER_ENDPOINT` host **only if** that host is in `OAUTH_HOST_ALLOWLIST` (`oauth.bitrix.info`, `oauth.bitrix24.tech`); otherwise `https://oauth.bitrix.info/oauth/token/`.
- **Credential invariant**: `access_token_enc` / `refresh_token_enc` may be written only after `user.current` + `user.admin = true` succeeded with that token at an OAuth-derived `client_endpoint`. Every path (install, self-heal, re-authorize, events) goes through `services/portals.py::store_portal_credential()`; `tests/test_registry_lint.py` fails the build if any other module writes those columns.
- **Endpoint invariant**: `client_endpoint` may be written only from an OAuth refresh response. There is no code path that derives it from `DOMAIN`. A portal that cannot refresh (empty `REFRESH_ID`, isolated on-premise box with a custom auth provider) renders `/state/unsupported_portal` and no row is created or modified — v1 does not support that configuration.
- `portal_id`, `user_id`, access level and timezone reach the database only from the verified JWT. No API endpoint accepts `member_id`, `portal_id` or `user_id` from the client.
- Refresh exchanges triggered by unauthenticated input (`/install/`, self-heal, unknown-portal events) are rate-limited to `OAUTH_EXCHANGE_LIMIT` (5 per 10 minutes) per `member_id` and per source IP; over the limit the request renders the "please try again" state and logs `portal_events(refresh_failed, reason=rate_limited)`.

### 4.2 Input validation (`bitrix/forms.py`)
Allowlist for all Bitrix24-facing endpoints: `member_id ~ ^[0-9a-f]{32}$`; `DOMAIN` RFC hostname with optional `:port`; `PROTOCOL ∈ {0,1}`; `LANG ~ ^[a-z]{2}$`; `AUTH_ID`/`REFRESH_ID` `[A-Za-z0-9._-]{16,512}`; `AUTH_EXPIRES` integer 1..86400; `APPLICATION_TOKEN` `[A-Za-z0-9]{8,128}`; `APP_SID` `[A-Za-z0-9._-]{1,128}`; `PLACEMENT` in `{DEFAULT, LEFT_MENU, CRM_DEAL_DETAIL_TAB, CRM_LEAD_DETAIL_TAB, CRM_CONTACT_DETAIL_TAB, CRM_COMPANY_DETAIL_TAB}`; `PLACEMENT_OPTIONS` valid JSON ≤ 4 KB with numeric `ID` when a CRM tab; total body ≤ 64 KB. **A body carrying `event=` on `/app/`, `/install/` or `/settings/` is dispatched to the events handler before the placement allowlist runs** (some cabinets deliver lifecycle events to the install URL). Event bodies: PHP-bracket keys (`auth[member_id]`) expanded; scopes split on `[ ,]`. Any violation renders `state.html` "bad request" (HTTP 400, translated, with request id) — never a bare error.

### 4.3 `POST /install/`
1. Parse and validate. Log the inbound POST to `rest_log` (`kind=install`, `direction=in`, redacted; `portal_id` NULL if unknown).
2. **Prove the payload**: `REFRESH_ID` must be present; refresh exchange at the allowlisted OAuth host (`kind=oauth` log row, rate-limited per §4.1). The response `member_id` must equal the POSTed one (else 400 bad request); its `client_endpoint`, `user_id`, `scope`, `status`, `expires_in` are authoritative and the returned token pair is what we store. Empty `REFRESH_ID` → `/state/unsupported_portal`, **no row is created or touched**.
3. **Prove the installer is an administrator** with one `batch` (halt=0) at the new `client_endpoint`: `user.current` (id, `TIME_ZONE`), `user.admin`, `app.info`, `method.get {name: voximplant.statistic.get}`, `placement.get`. `user.admin=false` → render `state.html` "administrators only" (HTTP 200) and **write nothing**. `method.get` false → `capabilities.statistic_get=false`, `token_status='method_missing'`; install still completes so the admin sees the explicit state.
4. In one transaction: upsert `portals` by `member_id` (status `active`, `uninstalled_at=NULL`, encrypted tokens via `store_portal_credential()`, `token_user_id`, `token_admin_verified_at=now()`, `token_version+1`, `token_status='ok'` (or `method_missing`), `application_token_enc` from `APPLICATION_TOKEN`, `scope`, `app_status`, `domain`, `protocol_https`, `client_endpoint`, `server_endpoint`, `timezone`, `lang`, `capabilities`, `install_completed_at=now()`); `portal_sync`: `sync_generation+1`, lease cleared, `next_run_at=now()`, and **cursors are reset only when the portal row is new or its previous status was `uninstalled`** — a re-run of the install URL after a version update must not re-import 500k rows; `portal_events(install|reinstall|app_update)`.
5. `placement.bind` (one `batch`, halt=0) for each of the four `CRM_*_DETAIL_TAB` placements not already present in `placement.get`, `HANDLER=https://b24.texnobus.uz/app/`, `TITLE`/`LANG_ALL` derived from the shared locale list. In the same batch, best-effort `event.bind('ONAPPUNINSTALL')` and `event.bind('ONAPPUPDATE')` to `https://b24.texnobus.uz/events/` (undocumented for lifecycle events; the result is recorded in `capabilities.event_bind` and is never fatal). HTTP 200 with an `error` body counts as failure. Results into `portals.placements`; failures logged, not fatal (re-bind button on the settings page). `LEFT_MENU` is **not** bound: the version-card menu option already opens the handler with `PLACEMENT=DEFAULT`.
6. Return `install.html`: loads `//api.bitrix24.com/api/v1/`, shows a translated "Installing…" line, runs `BX24.init(() => BX24.installFinish())`. Three HTTP round trips; no sync work inline. The worker picks the portal up on the next tick (≤ 15 s) and runs `head_fetch`.

### 4.4 `POST /app/` — one handler, routed by `PLACEMENT`
1. Parse/validate; log inbound (`kind=open`). Look up `portals` by `member_id`.
2. **Unknown or `uninstalled` portal (self-heal)**: `REFRESH_ID` must be present; run the refresh exchange first (rate-limited); only if the response `member_id` matches, run §4.3 steps 3–5 inline (including the `user.admin` proof — a non-admin self-heal renders "ask your administrator to open the app once" and writes nothing). Refresh failure or empty `REFRESH_ID` → `/state/not_installed`. Nothing is ever contacted at `DOMAIN`.
3. `purge_pending=true` on an active portal (reinstall during cleanup) → continue; the dashboard shows the "importing history" banner because sync has not started.
4. One `batch` (halt=0) at the **stored** `client_endpoint` with `auth=AUTH_ID`:
   - `me: user.current` → id, `TIME_ZONE`, name fields (upserted into `employees` under tenant context)
   - `admin: user.admin`
   - `app: app.info` (only when `installed_flag` is not true or once a day)
   - `probe: voximplant.statistic.get {FILTER:{PORTAL_USER_ID:<me.ID>}, SORT:"ID", ORDER:"DESC", start:0}` — **skipped when the user is an administrator** (admins always have full telephony access) and when `capabilities.statistic_get=false`; this keeps shared operating time out of the common path
   - for `CRM_DEAL_DETAIL_TAB`: `deal: crm.deal.get(ID)`, `contacts: crm.deal.contact.items.get(ID)`, `acts: crm.activity.list {filter:{OWNER_TYPE_ID:2, OWNER_ID:ID, TYPE_ID:2}, select:["ID"]}` (max 5 pages of 50 = `CRM_ACTIVITY_CAP` 250)
   - for `CRM_LEAD/CONTACT/COMPANY_DETAIL_TAB`: `acts: crm.activity.list` with the matching `OWNER_TYPE_ID` (1/3/4)

   **Transport-level failure** (DNS/TLS/connect/timeout at the stored endpoint) → one refresh exchange with the POSTed `REFRESH_ID` (`member_id` must match), take `client_endpoint` (and `domain`) from the response, `portal_events(domain_changed)`, then retry the batch **once**. This is the only automatic cure for a renamed portal or a newly connected custom domain. Still failing → `/state/retry`.
   HTTP 401 / `expired_token` / `NO_AUTH_FOUND` / `user_access_error` on the batch → the user's token is not valid at this portal → `/state/retry` ("Please reopen the app"). We never refresh a user's token.
5. Access decision (§4.7): `admin=true` → `all`; probe OK → `own`; probe `ACCESS_DENIED` / `INVALID_CREDENTIALS` → `denied`; probe `insufficient_scope` → `/state/scope`; `QUERY_LIMIT_EXCEEDED`/`OPERATION_TIME_LIMIT` → `/state/retry` with auto-reload after 10 s; `capabilities.statistic_get=false` → `/state/method_missing`.
   **CRM tabs**: if any CRM command returned an error, render `/state/crm_no_access` and mint **no** entity JWT — a cached context must never be served to a user who cannot see the entity.
6. Housekeeping (under the `portals` row lock): `last_opened_at`, `lang` (any user); `domain`/`protocol_https` if changed; `application_token_enc` from `APPLICATION_TOKEN` **only when the opener is an admin** (it changes on version update); `installed_flag` from `app.info` — `false` for an admin → re-render `install.html`; `last_admin_opened_at` for admins.
   **Token re-seed is not routine.** It happens only when the opener is an admin *and* one of: `token_status != 'ok'`; `token_refreshed_at` older than `TOKEN_RESEED_AFTER_DAYS` (120, so the 180-day chain never dies silently); or the admin clicked "Re-authorize" on the settings page. Re-seeding runs the refresh exchange with the POSTed `REFRESH_ID` (`member_id` must match), proves `user.admin` for the resulting token, then `store_portal_credential()`, `token_status='ok'`, `token_version+1`, `next_run_at=now()`, `portal_events(token_reseeded)`. A failed opportunistic re-seed **leaves the existing credential untouched** and only logs `portal_events(refresh_failed)`. At most one exchange per portal per 10 minutes outside the worker's `expired_token` path.
7. Upsert `crm_contexts` (inside `tenant_txn`) for CRM tabs from step 4 results, recording `resolved_by_user_id` and `resolved_at`.
8. Mint the JWT (§4.6) and return **`handoff.html`** (HTTP 200, `Cache-Control: no-store`): an inline script running `location.replace(target)` where `target` = the SPA path + **Bitrix24's original query string verbatim** (`DOMAIN`, `PROTOCOL`, `LANG`, `APP_SID`) + `#s=<jwt>`. The JWT never appears in a response header, so it can never be written to an access log; `APP_SID` is preserved so the BX24 JS SDK can talk to the parent frame (without it `BX24.init` never fires and `fitWindow`/`openPath`/`getAuth` are inert).

| `PLACEMENT` | Target | Notes |
|---|---|---|
| `DEFAULT`, `LEFT_MENU` | `/dashboard?<original query>#s=<jwt>` | left-menu page; both values accepted |
| `CRM_DEAL_DETAIL_TAB` | `/crm?<original query>#s=<jwt>` with `ent={t:"DEAL",id}` | matched via `crm_contexts` + raw `DEAL` rows |
| `CRM_LEAD/CONTACT/COMPANY_DETAIL_TAB` | `/crm?<original query>#s=<jwt>` with `ent={t,id}` | direct entity keys + activity ids |
| any, `acc=denied` | `/state/denied?<original query>` (no JWT) | mandated text |
| CRM command error | `/state/crm_no_access?<original query>` | no entity JWT |
| unknown (fails allowlist) | `state.html` bad request | never blank |

### 4.5 `POST /settings/`
Same handler as `/app/`; lands on `/settings` for admins, "administrators only" state for others. The settings page shows: sync state and backfill progress, token owner and `token_status` with a "Re-authorize" button (`POST /api/v1/portal/reauthorize`, body = the pair from `BX24.getAuth()`, validated by a refresh whose `member_id` must match **and** a `user.admin` proof), placement bind results with "Re-bind", last error, quarantined-row count, `capabilities`. If the body carries `event=OnAppSettingsInstall|Display|Change` the handler logs it and returns a minimal valid JSON form with zero steps — v1 has no user-editable settings.

### 4.6 Session token (bearer, not cookie)
- JWT HS256 signed with `SESSION_SECRET`. Claims: `pid`, `mid`, `sub`, `adm`, `acc` (`all|own|denied`), `tz`, `lang`, `plc`, `ent` (`{t,id}` or null), `iat`, `exp = iat + min(AUTH_EXPIRES, 3600)`. About 300 bytes.
- Delivered only in the URL fragment written by `handoff.html`; the frontend reads `#s=` once, `history.replaceState` removes it, keeps it in memory with a `sessionStorage` copy, and sends `Authorization: Bearer` on every `/api/v1/*` call. The JWT is never a query parameter, never a header value we emit, never logged.
- Expiry inside a long-lived tab: on 401 the SPA calls `BX24.refreshAuth()`/`getAuth()` and POSTs `{access_token, jwt}` to `/api/v1/session/exchange`; the backend verifies the old JWT's signature ignoring `exp` (to learn `pid`, `plc`, `ent`), re-runs the step-4 batch with the fresh token at the stored endpoint (same `sub` required), re-decides access and re-resolves the CRM context, and returns a new JWT. Access is therefore re-evaluated at least hourly. Bodies of `/session/exchange` and `/portal/reauthorize` are excluded from all exception logging.
- **Playback URLs** are a separate, narrower token: `{pid, sub, acc, cid, exp = now+5min}` minted by `POST /api/v1/calls/{id}/play-url` under the normal bearer session and passed as `?t=` on the `<audio>` source (an `<audio src>` cannot send an `Authorization` header). `record.py` verifies `t` only, then re-applies `scope_filter` and the portal-status check.
- Revocation: `get_principal` loads the `portals` row on every request; `status != 'active'` → 401 `portal_inactive`.

### 4.7 Per-request permission resolution
- `get_principal`: verify JWT → load `portals` by `pid`, assert `member_id == mid` and `status='active'` → open the transaction with `SET LOCAL app.portal_id = pid` → `Principal(portal_id, user_id, is_admin, acc, tz, lang, plc, ent)`. Every subsequent transaction in the request re-issues the `SET LOCAL`.
- `acc='denied'`: only `GET /me` answers; every data endpoint returns 403 `{"code":"no_stats_permission"}` and the SPA renders the mandated text plus a link to the portal's own call statistics when obtainable.
- `acc='own'`: `services/calls_repo.py::scope_filter(principal)` appends `portal_user_id = principal.user_id` to every read; the employee filter is hidden and a banner says "You see your own calls only". `acc='all'`: no extra predicate. `scope_filter` is the single place; every read (dashboard, calls, filters, record, refresh) goes through `calls_repo`.
- This collapses Bitrix24's four levels (own / department / any / none) to admin / own / denied, as the brief mandates; documented in `docs/moderation-checklist.md` as a deliberate v1 simplification. The recording-listen permission is separate and is enforced by using the **viewer's own token** for non-admin playback (§9).

### 4.8 CRM tab reads
`GET /api/v1/calls` with `ent` in the JWT loads `crm_contexts(portal_id, ent.t, ent.id)`; the row must have `resolved_at >= JWT.iat` (or `resolved_by_user_id = sub`), otherwise 409 `context_missing` and the SPA runs the session exchange, which re-resolves with the current user's token. Matching: `(crm_entity_type, crm_entity_id) IN entity_keys` **OR** `crm_activity_id = ANY(activity_ids)` **OR** `(crm_entity_type = ent.t AND crm_entity_id = ent.id)` — the last clause covers portals that do emit `DEAL` (or other undocumented types) in the statistics rows. Then `scope_filter`, then period/paging.

### 4.9 `POST /events/` — lifecycle events and verification
Registered in the vendor cabinet as the "Event installation handler URL" (assumption), also reachable through the `event=` dispatch of §4.2, and additionally subscribed best-effort by `event.bind` at install. Parses PHP-bracket forms; logs every inbound event (`kind=event`, recursively redacted — including `data{}` of `ONAPPUSERREADY`, which carries long-lived tokens). Rules, in order:
1. **Idempotency**: an event whose `ts` is not greater than `portals.last_event_ts`, or (for `ONAPPUNINSTALL`) earlier than `install_completed_at`, is a logged 200 no-op. This stops a retried uninstall from wiping a reinstall performed in the meantime.
2. **`application_token` first**: if `application_token_enc` is stored, every event must present an `auth[application_token]` that is constant-time equal to it. Mismatch → 403 + `portal_events(event_rejected)`. This is brief rule 5, applied to `ONAPPINSTALL`, `ONAPPUSERREADY`, `ONAPPUPDATE` and `ONAPPUNINSTALL` alike.
3. **`ONAPPUNINSTALL`** (carries no access token): portal unknown or already `uninstalled` → 200 no-op. Otherwise, after rule 2: in one transaction `status='uninstalled'`, `uninstalled_at`, `access_token_enc=NULL`, `refresh_token_enc=NULL`, `token_status='reauth_required'`, `placements='{}'`, `purge_pending=true`, `purge_bodies = (data[CLEAN]='1')`, `portal_sync` cursors reset, lease cleared and `sync_generation+1` (fencing any in-flight run), `portal_events(uninstall)`. `application_token_enc` is kept. Return 200 immediately; deletion happens in the worker (§5.9). No REST calls: access is already revoked and placements die with the app.
4. **`ONAPPUPDATE`** — the only event that may *replace* `application_token_enc`, and only when the bearer proves elevated identity: `auth[access_token]` must pass `user.current` + `user.admin=true` at the stored `client_endpoint`, and `auth[refresh_token]` must exchange to the same `member_id`. Then store the new `application_token_enc`, `app_version`, `scope`; the credential is re-seeded only if `token_status != 'ok'` (and then only through `store_portal_credential()`, i.e. with the admin proof already in hand). Anything less → 403 + `event_rejected`.
5. **`ONAPPINSTALL` / `ONAPPUSERREADY` on a known portal**: after rule 2 they are no-ops beyond storing `application_token_enc` when none is stored yet (in which case the token must additionally pass the admin proof of rule 4). The system-user credential is logged and discarded — v1 never syncs with it (regular-employee rights would silently truncate the cache).
6. **Token-bearing events on an unknown portal**: authenticate by a refresh exchange with `auth[refresh_token]` (authoritative `member_id`, rate-limited) **and** a `user.admin` proof at the returned `client_endpoint`; only then create the portal row. A non-admin token creates nothing.
7. Events without tokens for an unknown portal → 200, ignored, logged. An optional Caddy IP allowlist from `dl.bitrix24.com/webhook/app-world.json` is documented as defence in depth; the `application_token` compare plus the admin proof is the primary control.

### 4.10 Frame embedding, TLS, headers
`web/middleware.ts` sets `Content-Security-Policy: frame-ancestors 'self' https://<DOMAIN>` (or `http://` when `PROTOCOL=0`) after validating `DOMAIN` as a hostname; without a valid `DOMAIN` it sets `frame-ancestors 'none'` and the page renders "Open this app from Bitrix24". `install.html`/`handoff.html`/`state.html` carry the same header. No `X-Frame-Options` anywhere. API and handoff responses carry `Cache-Control: no-store`. Caddy: automatic TLS, HSTS, HTTP→HTTPS redirect, and an access-log filter that removes `resp_headers>Location` and request query strings. `lib/bx24.ts` injects the SDK once and exposes `ready()`; the layout calls `BX24.fitWindow()` after first paint and on every debounced `ResizeObserver` change; CRM links use `BX24.openPath('/crm/<type>/details/<id>/')`.

### 4.11 Moderator paths (each ends in a rendered, translated state)

| Moderator action | Entry | Result in the frame |
|---|---|---|
| Installs on a clean portal | `POST /install/` | "Installing…" then Bitrix24 closes the slider |
| Opens the left-menu item | `POST /app/` `DEFAULT` | Dashboard; "Importing history 12 300 / 250 000" banner while backfilling; explicit "No calls in this period" when empty |
| Opens a Deal/Lead/Contact/Company card → our tab | `POST /app/` `CRM_*_DETAIL_TAB` | Call table for that entity; empty state if none; "no access to this item" if CRM rights are missing |
| Opens as non-admin without "Call statistics — view" | `POST /app/` | Mandated "ask your administrator" text |
| Opens as non-admin with own-calls right | `POST /app/` | Dashboard filtered to self + banner |
| Uninstalls | `POST /events/` `ONAPPUNINSTALL` | server-to-server; 200; rows purged by the worker |
| Uninstalls with the events URL unavailable | — | Token chain dies; after `UNINSTALL_GRACE_DAYS` the portal is marked uninstalled and purged |
| Reinstalls | `POST /install/` | Idempotent upsert; placements re-bound; cursors kept unless the portal was uninstalled |
| Tests on on-premise (cloud OAuth reachable) | any | Same UI; REST base is `client_endpoint`; `PROTOCOL=0` allowed; `method_missing` state if the build lacks the method |
| Tests on a fully isolated box (no refresh) | any | `/state/unsupported_portal`, explained; nothing written |
| Opens with no portal row (DB restored) | `POST /app/` | Admin: self-heal → dashboard; non-admin: "not installed yet" |
| Opens `/settings/` | `POST /settings/` | Admin status page; non-admin "administrators only" |
| Opens the UTM page on a portal with leads turned off | `POST /app/` `DEFAULT` | Deals-only report, lead columns absent, one sentence saying so |
| Probes endpoints with garbage | any | Translated "bad request" page, HTTP 400 |

---

### 4.12 Deal analytics — `POST /api/v1/deals`, a live CRM read

The third left-menu page. A row is one operator inside one funnel; the columns are that
funnel's own stages, read from the portal. It is the only read in this app that holds no
rows of its own.

**The owner's four constraints** (given 2026-09-12, not re-litigated in code):

1. Stage columns come from the portal (`crm.status.list`); nothing about the reference
   report's stage names is hardcoded, because the app is mass-market.
2. Rows group by funnel (`CATEGORY_ID`).
3. A deal counts if it was **created, modified or closed** inside the period; the operator
   is `ASSIGNED_BY_ID`.
4. **Nothing is stored** — no table, no migration, no sync phase.

Two reference columns cannot be reproduced and are not faked. `Главный оператор` needs the
`department` scope, a vendor-cabinet change that re-lists the app and forces every installed
portal to re-consent; decision 2 replaces it with the funnel heading. `С новых в треш` is a
stage **transition**, and the list methods return only the current stage — the owner chose
to drop it rather than pay a second full scan over `crm.stagehistory.list`.

#### The credential

`principal.access` (`all|own|denied`) is decided from `user.admin` plus a
`voximplant.statistic.get` probe. It is a **telephony** verdict, and `acc='all'` is not
permission to read one deal. So every `crm.*` call on this path runs on the **viewer's own
Bitrix24 token**, posted in the request body — §4.8's doctrine applied verbatim: *we never
model Bitrix24's CRM permissions, we borrow the answer.* `crm.category.list` is documented as
returning only the funnels the caller may see, so the report is correct by construction for
an administrator, a team lead and a salesperson alike.

* The endpoint is a **POST** because a live token must never enter a URL (§6).
* `user.current` rides in the first batch and must return the JWT's `sub`, or **401
  `invalid_session`** — the check `POST /calls/{id}/play-url` already makes.
* An absent token is **409 `viewer_token_required`**, the code the SPA already answers with
  `BX24.getAuth()`. Never 401: that makes `apiFetch` re-mint *our* JWT and re-post the same
  stale Bitrix token.
* `require_data_access` stays on the route and decides exactly one thing: `acc='denied'` gets
  no data endpoint at all (§4.7).
* An `acc != 'all'` viewer has `@assignedById` pinned to themselves **server-side**. Not only
  because a control can be bypassed: operator names come from the `employees` cache, which
  the worker fills under the installer's credential with `ADMIN_MODE: true`.
* `oauth.with_portal_token` must not appear in `api/deals.py` or `services/deal_stats.py`.

#### Two dialects, probed once per portal

| | Primary | Fallback (on `errors.MethodNotFound`) |
|---|---|---|
| List | `crm.item.list` `entityTypeId=2`, `result.items`, camelCase | `crm.deal.list` × 3 selections, bare array, UPPER_CASE |
| Dictionary | `crm.category.list` + `crm.status.list` | `crm.dealcategory.list` + `crm.dealcategory.stage.list` |

`crm.deal.*` is officially discontinued for new development, and — decisively — `logic: "OR"`
filter grouping is documented **only** for `crm.item.list`. Constraint 3 is therefore ONE
paged query on the primary path and three deduped selections on the fallback. Never gate on a
version number; the only detector is the typed error.

**The period filter** (`closed` + `movedTime`, never `CLOSEDATE` — which is a writable
*planned* end date, so a back-dated value would pull years-old deals into a one-week report):

```json
{"0": {"logic": "OR",
       "0": {">=createdTime": "<startISO>", "<createdTime": "<endISO>"},
       "1": {">=updatedTime": "<startISO>", "<updatedTime": "<endISO>"},
       "2": {"=closed": "Y", ">=movedTime": "<startISO>", "<movedTime": "<endISO>"}}}
```

Bounds carry an **explicit offset**: a bare date is read in the *portal's* zone while this app
computes its period in the viewer's, which is the normal case.

**The honour probe.** Only `createdTime` is confirmed by a retrieved doc; the rest are
inferred. An unknown filter key may be **ignored** rather than refused, which silently widens
the union to "created in the period OR everything" — a report that is plausible, larger than
the truth, and wrong with no symptom. `crm.item.fields` proves a name exists; three probe
commands in the **exact nested `logic` shape** prove the filter is applied. A verdict is
cached only when the unfiltered baseline is non-zero: a viewer who can see no deals proves
nothing about the build.

`ENTITY_ID` is `DEAL_STAGE` for funnel 0 and `DEAL_STAGE_<id>` otherwise. **`DEAL_STAGE_0`
returns an empty list with no error** — a silent zero-column funnel, caught only by the unit
test that asserts both branches.

A column is keyed on the **pair** `"<category_id>:<status_id>"`: `STATUS_ID` uniqueness is
documented as limited to its own directory and the default funnel's codes are unprefixed.

#### Round trips, and the refusal ladder

Warm (dictionary cached): **one** batch — `user.current` plus page 0. Cold: **three** —
identity and funnels, then stages and the probe, then the deals. Page batches hold 25
commands, not the 50 a batch allows: Bitrix24 caps one request at 60 s.

**Every `crm.*` call goes through `batch()`, never `call()`** — `call()` writes the whole
response body to `rest_log`, which for this path means customer deal rows (§6).

Every rung **refuses**; none truncates, because a partial Итого row is a number a supervisor
may act on with nothing on screen saying it is partial.

| Rung | Answer |
|---|---|
| Preflight `total > DEAL_SCAN_CAP` | 400 `deal_scan_too_large` {deals, max_deals, days} |
| Projected cost exceeds the remaining deadline | the same 400, **before** spending the pages |
| `DEAL_SCAN_DEADLINE_SEC` (each batch is `wait_for`-bounded) | the same 400 |
| `time.operating` past the soft ratio | 503 `operation_time_limit` + `Retry-After` |
| `DEAL_REPORT_LIMIT` per `(portal, user)` per 10 min | 429 `rate_limited` + `Retry-After` |
| Admission gates full | 503 `retry` + `Retry-After` |

The deadline is validated below 25 s because `apiFetch` aborts at 30 s with a controller no
caller can extend — past that the user gets a generic network error instead of the
explanation this endpoint computed. The operating budget is read adaptively from `time`; a
429 here blocks the method for this app across the **whole portal**, including the CRM tab.

Admission is a per-portal `Semaphore(1)` inside a process-wide `Semaphore(N)`: the leaky
bucket is counted per source IP and every tenant shares one egress address.

#### Response and aggregation

Integers only — `won/total` and `lost/total` are derived in the browser, so no toggle costs a
REST round trip. `totals` deliberately carries **no** `cells`: two funnels' stages are not
comparable, and adding them would invent a number.

* A deal whose stage the dictionary cannot name gets a **synthesised `known: false` column**,
  never a drop: `Σ cells + unknown_stage == total` must hold on every row, and a row whose
  cells do not add up is the one discrepancy a reader can see and cannot explain.
* A funnel the deals mention but the dictionary omits (created since the cache was filled) is
  appended after the known ones.
* The unassigned row is kept and sorts last, as `load_hours` keeps its NULL row.
* `subtotal` is server-computed over every operator, including any past the 200-row cap.
* The page states that every count is a **current-stage snapshot** of deals that touched the
  period — the most likely misreading of the whole report.

#### Caveat

Both caches and both limiters are **process-local**, correct only because v1 runs exactly one
`api` container. A second replica silently doubles every budget and halves both hit rates.

---

### 4.13 UTM analytics — `POST /api/v1/utm`, a live CRM read

The fourth left-menu page, and the only one that answers a marketing question. A row is one
combination of UTM tags; the columns are leads, deals, outcomes and amount. Like §4.12 it
holds no rows of its own.

**The owner's four constraints** (given 2026-09-12, not re-litigated in code):

1. **Live CRM read** — no table, no migration, no sync phase, exactly as §4.12.
2. **One unified funnel** — a row carries leads *and* deals *and* the money, so a reader
   compares channels rather than two separate reports.
3. **Creation date only.** Deliberately DIFFERENT from §4.12's created-or-modified-or-closed.
   A UTM tag records where a record came from, so the only question it can honestly answer
   is "how many arrived from here, in this period".
4. Counts, `OPPORTUNITY`, the won / lost / in-progress split, conversion and average deal.

#### Three things this page does NOT have, and why

* **No dictionary phase.** §4.12 spends its cold path on `crm.category.list` plus one
  `crm.status.list` per funnel because its COLUMNS are portal data. This page's columns are
  UTM values, which arrive on the rows, and the outcome comes off `stageSemanticId`, which
  research block (g) already verified is on the row. So the cold path is **two** round trips
  and the warm path is **one**.
* **No `logic: "OR"`.** Constraint 3 is one flat leg (`>=createdTime` AND `<createdTime`),
  which every list method has always honoured. The consequence is worth stating: the legacy
  dialects cost EXACTLY what the universal ones cost here, so a `MethodNotFound` demotion is
  free — where in §4.12 it triples the scan.
* **No cardinality refusal.** See "The bucket ladder".

`api/app/bitrix/deals.py` is not modified. Its `_select` carries a §6 promise about what the
DEAL report may receive, and this page needs eleven other fields and a second entity; a new
vocabulary got a new module (`api/app/bitrix/utm.py`) that imports the genuinely shared
scalars rather than widening that one.

#### The credential

Identical to §4.12 and for the same reasons: the viewer's own Bitrix24 token, posted in the
body, `user.current` in the first batch or **401 `invalid_session`**, an absent token is
**409 `viewer_token_required`** and never 401, and `require_data_access` decides only that a
user Bitrix24 refused telephony to gets no data endpoint.

`oauth.with_portal_token` must not appear in `api/utm.py` or `services/utm_stats.py`.

An `acc != 'all'` viewer has `@assignedById` pinned server-side on **both entity legs**. The
lead half is the one easy to forget, and forgetting it puts the whole company's leads in the
denominator of that viewer's own conversion rate.

#### Two entities, two dialects, and one optional leg

| | Primary | Fallback (on demotion) |
|---|---|---|
| Leads | `crm.item.list` `entityTypeId=1`, camelCase | `crm.lead.list`, UPPER_CASE, `STATUS_SEMANTIC_ID` |
| Deals | `crm.item.list` `entityTypeId=2`, camelCase | `crm.deal.list`, UPPER_CASE, `STAGE_SEMANTIC_ID` |
| Field map | `crm.item.fields` | `crm.lead.fields` / `crm.deal.fields` |

`crm.item.*` normalises a lead's `STATUS_ID` / `STATUS_SEMANTIC_ID` into the same `stageId` /
`stageSemanticId` a deal uses — the universal field list documents both as common to lead and
deal and lists no `statusId` at all. That is what lets one dialect SHAPE serve both entities;
only the legacy spellings diverge.

**Leads are optional; deals are not.** A portal in simple CRM mode answers `MethodNotFound`
for the lead leg. That DEGRADES the report to deals-only, sets `scan.leads.available=false`
and prints one sentence; it never refuses. `leads.available=false` is deliberately
distinguishable on the wire from `leads.total==0`, because "this portal has no leads module"
and "nobody created a lead last month" are different facts a marketer acts on differently.
Both legs refused is **403 `crm_no_access`** — an empty table is indistinguishable from "you
may see none", which §4.11 forbids.

The degradation is unconditional, not cold-path-only: a WARM report whose cached verdict
still says leads exist drops the lead leg on a structurally-absent page-0 error and evicts
that verdict, so the next report re-probes. Otherwise an administrator turning leads off
would get an hour of `method_missing` where a fresh reader gets a deals-only report.

#### The honour probe, and the unfiltered baseline that had to go

**One** command per entity: a year-2999 selection that must answer zero. The §4.12 probes
for `closed` + `movedTime` are gone because nothing here reads those fields.

There was briefly a second command — an UNFILTERED list, to prove the viewer can read
anything at all, so that a zero from the probe could be told apart from "this viewer reads
nothing". **It shipped, and it took the page down on the first production portal.**
`filter: {}` makes Bitrix24 count the WHOLE lead table and the whole deal table. That cost
is independent of the period, so narrowing to a single day does not reduce it; it repeats on
every retry, because a report that failed cached nothing; and it spends the `crm.item.list`
operating budget the deal page shares. The page answered `operation_time_limit` — rendered
as the §4.11 `retry` state — for as long as the ten-minute window took to drain, and every
press of "try again" refilled it.

The baseline was never needed to DETECT a dropped filter: no record can be dated the year
2999, so a non-zero total is proof on its own. It was only ever evidence about whether the
verdict may be CACHED. That evidence now comes from the scan the report runs anyway — a
selection that returned rows proves this viewer can read records — so the verdict is
remembered after the scan rather than after the probe.

The trade is that a period with nothing in it leaves the portal un-cached and the next open
cold. That is cheap now: the cold path is two field maps and two selections that match
nothing.

The instinct is that one flat leg is safer than a nested group and the probe can go. It is
the opposite. Under §4.12 an ignored `createdTime` widened the union to "created in the
period OR everything" — bad, bounded by the other legs, usually caught by the preflight cap.
Here an ignored `>=createdTime` **deletes the period**: the selection becomes the portal's
entire history. On a large portal that surfaces as a confusing `utm_scan_too_large`; on a
small one it is a **200** — a lifetime report with a one-month range printed above it,
internally consistent in every cell, and wrong. The verdict is cached only when the baseline
is non-zero, for §4.12's reason.

#### The UTM probe, and the gap it cannot close

`crm.item.fields` is asked for both entity types. A missing core name, or none of the five
UTM names, demotes that entity to its legacy dialect — `UTM_SOURCE` is documented on
`crm.lead.fields` / `crm.deal.fields`, and only its camelCase re-exposure is inferred, so
"the universal method has never heard of these" is a reason to use the old method rather than
to give up. A legacy dialect that also declares no UTM leaves that entity with zero
dimensions; when BOTH entities end up there the answer is **409 `utm_unsupported`** with
mandated copy — never a 200 with an empty table, and never a 200 with everything in the
"no tag" bucket, because both are indistinguishable on screen from "nobody used tagged links
this month".

**What cannot be detected:** `crm.item.fields` proves a field EXISTS; it does not prove
`crm.item.list` returns it when named in `select`. There is no honest detector — Bitrix24
omits null keys, so "no row carried the key" is identical to "these rows genuinely have no
tag". So it is REPORTED, not detected: `scan.<entity>.tagged_rows` counts records carrying
any tag, and a zero over a non-zero scan prints one sentence that is true in both worlds.

#### Round trips, and the refusal ladder

Warm: **one** batch — `user.current` plus page 0 of each entity. Cold: **two** (three if a
dialect demotes). Page batches hold 25 commands, not the 50 a batch allows: Bitrix24 caps one
request at 60 s. Every `crm.*` call goes through `batch()`, never `call()` — `call()` writes
the whole response body to `rest_log`, which here means customer lead rows (§6).

| Rung | Answer |
|---|---|
| Preflight `leads + deals > UTM_SCAN_CAP` | 400 `utm_scan_too_large` {leads, deals, total, max_total, days} |
| Projected cost exceeds the remaining deadline | the same 400, **before** spending the pages |
| `UTM_SCAN_DEADLINE_SEC` (each batch is `wait_for`-bounded) | the same 400 |
| `time.operating` past the soft ratio | 503 `operation_time_limit` + `Retry-After` |
| `UTM_REPORT_LIMIT` per `(portal, user)` per 10 min | 429 `rate_limited` + `Retry-After` |
| Admission gates full | 503 `retry` + `Retry-After` |

Both counts are named separately in the refusal because the lever differs: a portal drowning
in leads and one drowning in deals need different advice.

**Every refusal code this page can answer is in `OWN_MESSAGE_CODES`** (`web/src/lib/api.ts`).
A code that is not there renders the generic "something went wrong" state instead of its own
sentence, which for `utm_scan_too_large` means dropping the two counts, the cap and the
period length — the only numbers that turn the refusal into an action. The 503/429 family
deliberately stays on the mandated `retry` copy, because "narrow this" is not the advice
there.

#### The bucket ladder — and why it is not the truncation §4.12 forbids

A `utm_term` that is unique per click makes the combination count enormous. A cardinality
REFUSAL would be wrong: the count is unknowable until the scan that produced it has been paid
for, so the refusal would arrive after the cost it existed to prevent — exactly what this
ladder's own rule forbids ("refuse BEFORE spending the batches that cannot finish"). The data
is already in hand; there is nothing left to refuse. So, after the scan and before
serialisation:

1. **Value truncation** at `UTM_VALUE_MAX_CHARS`, applied BEFORE keying, so two values that
   differ only past the cut merge rather than rendering as two identical rows.
2. **Per-dimension `other`** — everything past the top `UTM_VALUE_CAP` of a dimension is
   rewritten to one reserved bucket.
3. **Dimension collapse** — if the row count is still past `UTM_COMBINATION_CAP`, `utm_term`
   and then `utm_content` leave the composite key. The order is published so the page can say
   in advance which control it will lose.

No lead and no deal is dropped at any rung; every count still lands somewhere; `Σ rows ==
totals` holds **exactly**. This is relabelling, and it is the move `EmployeeBars`' "Other"
row and §4.12's own row cap already make. The rule those two obey — never drop a row a
supervisor might act on — is not violated, because nothing is dropped. Collapsing a dimension
out of the key cannot change any other dimension's marginal: that is a property of a
projection, and `test_utm_aggregate.py` asserts it bit for bit.

#### `facets` — the part that makes the page free to use

Alongside the combination rows the response carries five small per-dimension marginals,
computed **before** bucketing and **before** any filter. They cost at most `5 ×
UTM_VALUE_CAP` rows and they buy three things the combination table cannot:

* the filter option lists, with real counts, in the response already on screen;
* an exact distinct count for a dimension that was later collapsed — a fact rather than an
  apology;
* chart sources that do not re-fold thousands of rows on every keystroke inside a slider.

Consequently **the UTM filters cost no REST at all**. They are not in the SPA's fetch-effect
dependency list: changing one leaves the query string byte-identical and only a `useMemo`
re-runs. Period and employee are what cost a scan, and the page's layout says so. Options
always come from the unfiltered facets, so they never shrink when a filter is applied — the
cross-filter trap where picking one source collapses the medium list and there is no way
back.

`dimensions` is accepted by the API and narrows the FOLD, not the scan (same pages, smaller
response). It is deliberately **not** exposed as a control: it would sit among the controls
this page promises are free while actually costing a round trip. The reader's grouping
control is client-side.

#### Currency

`opportunityAccount` + `accountCurrencyId` — Bitrix24's own conversion into the portal's
accounting currency, computed with the portal's own rates, which is the number the portal's
own CRM reports print. Agreeing with Bitrix24 rather than inventing a second exchange rate is
`crm_context.py`'s standing doctrine.

Three rules make that safe:

* The source is chosen for the WHOLE report — `account` only when EVERY amount-bearing row
  carried the pair, `native` otherwise. Choosing per row would mix two denominations inside
  one column the moment a single record was missing it.
* The account currency is a portal setting, so the observed set must be a singleton. If it is
  not, `amounts.trusted=false` and the page **hides** the amount column, the average-deal
  column and every money figure, and says why. A missing column a sentence explains is
  recoverable; a wrong total a supervisor acts on is not.
* **Money is deals-only.** A lead carries `OPPORTUNITY` too, and it is an estimate a
  salesperson typed; summing it beside deal amounts double-counts every converted lead. It is
  absent from the lead dialect entirely rather than filtered out later.

Amounts cross the wire as decimal STRINGS at scale two, quantised per record on the way in,
so every aggregate is a sum of exact cents and the browser's own column sum agrees digit for
digit. A float round-trip would make a row disagree with its own total in the fifteenth
digit — §4.12's forbidden discrepancy arriving through a channel nobody would suspect.

#### Attribution, and the sentence the page must print

Leads and deals are grouped **each by its own tags**. `leadId` is NOT joined, for three
reasons: a June deal may belong to a March lead, so resolving the join needs a second
unbounded scan whose size is unknown until the deal scan finishes — which destroys the one
property the whole budget ladder rests on; Bitrix24 copies `UTM_*` from lead to deal on
conversion, so the join mostly re-derives a value already on the row; and importing a
March lead into a June report contradicts constraint 3.

The cheap, bounded stand-in is `deals_from_lead`, one extra select field and one counter:
"of the 31 deals here, 22 came from a lead" tells the reader exactly how far to trust that
row's conversion cell.

So conversion is a period-cohort ratio, and **a value above 100 % is normal**. The page says
so above the table, and a row with deals and no leads renders `—` rather than `∞`, `0 %`, or
nothing at all.

#### Response and aggregation

Counts and decimal strings only. Conversion, average deal size and every share are derived in
the browser, so no toggle costs a REST round trip. `combinations[].k` is POSITIONAL against
`dimensions` — five repeated key names over thousands of rows is roughly three times the
bytes, and this ships into a slider on a phone.

The three reserved buckets are **declared** in the response (`buckets`) rather than
hardcoded in the SPA: a portal may genuinely tag a campaign `other`, and `U+0001` prefixes
make the sentinels unreachable as real values.

Invariants the wire guarantees, each asserted in `tests/test_utm_aggregate.py` and again in
`tests/test_utm_api.py`: `won + lost + in_progress == total` on every measure; `Σ
combinations == totals`; `Σ facets[d].values == totals` for every `d`; `deals_from_lead <=
deals`.

#### Caveat

Both the capability cache and both limiters are **process-local**, correct only because v1
runs exactly one `api` container. A second replica silently doubles every budget and halves
the hit rate.

---

## 5. Sync design

### 5.1 Principles
Only `voximplant.statistic.get`, only with the portal (installer, admin-proven) token, only from the worker, inside `tenant_txn(portal_id)` re-issued for every transaction. One runner per portal (lease + `sync_generation` fencing); portals in parallel up to `GLOBAL_PORTAL_CONCURRENCY` (default 4) because all tenants share one source IP. Cursor = the statistics `ID`. Upsert key `(portal_id, bx_id)`. Every unit of progress is committed with the rows it describes; nothing is held only in memory. All REST calls use a 120 s httpx timeout so a hung request can never outlive the 5-minute lease.

### 5.2 Cursors and `head_fetch`
`portal_sync.high_id` walks forward, `low_id` walks backward; the ranges are disjoint by construction.

`head_fetch` runs while `backfill_status IN ('pending','head')` and is idempotent:
1. One call `SORT=ID, ORDER=DESC, start=0` → `M = max ID`, `total` (→ `backfill_total`). Zero rows → `high_id=0`, `backfill_status='done'`.
2. One `batch` of up to `batch_pages` commands `FILTER {"<=ID": M}, SORT=ID DESC, start = 0, 50, …` — the `<=M` set is immutable, so offsets in one HTTP request cannot drift. Page 0 overlaps step 1's rows, so rows are deduped by `bx_id` before insert. Upsert, then `high_id = M`, `low_id = min(ID over the error-free prefix)`, `backfill_done = rows`, `backfill_status='running'`.
   A crash between steps leaves `backfill_status='head'`; the next visit simply re-runs `head_fetch` from step 1.

**Contiguous-prefix rule (all batched fetches).** With `halt=0`, each command's `result_error` is inspected. The cursor may advance only across the longest prefix of commands with no error; rows returned by commands *after* the first failure are still upserted (harmless) but do not move the cursor, so the failed page is re-fetched next visit. For `head_fetch`, any per-command error means "stay in `head`, retry". Without this rule one failed sub-command would leave a permanent 50-row hole that neither cursor ever revisits.

### 5.3 Backfill (resumable)
While `backfill_status='running'`: one `batch` of `batch_pages` commands `FILTER {"<ID": low_id}, SORT=ID DESC, start = k*50` (newest history first). Upsert, advance `low_id` over the error-free prefix, `backfill_done += rows`, commit — rows and cursor in the same fenced transaction, so a crash costs at most one re-upserted batch. Zero rows → `backfill_status='done'`. After `BACKFILL_BATCHES_PER_VISIT` (default 20) batches the run yields so one busy portal cannot starve the others. Progress in the UI = `backfill_done / backfill_total`. Budget: 500k rows at 20 pages/batch ≈ 500 batches; under the operating-time guard the backfill finishes in roughly two hours without touching the request path.

### 5.4 Incremental
Every `SYNC_INTERVAL_SEC` (default 300, also during backfill): **one command first** (`FILTER {">ID": high_id}, SORT=ID ASC, start=0`); its `total`/`next` decide how many further pages are packed into the following batch (`min(ceil(total/50) - 1, batch_pages)`). This stops 49 empty commands per cycle from burning shared operating time on a quiet portal. Upsert, advance `high_id` over the error-free prefix; if the last successful command still had `next`, repeat immediately (respecting pacing) up to `MAX_LOOPS_PER_VISIT` (10).

**Filter-honoured guard.** Every returned row is asserted against the filter it was requested with (`ID > high_id`, `ID < low_id`, `ID <= M`). A violation means the build ignores the operator (possible on lagging on-premise versions) and would otherwise produce a hot loop: the portal is set to `token_status='filter_unsupported'`, `next_run_at='infinity'`, `portal_events(sync_blocked)`, and the settings page explains it.

### 5.5 Upsert (`sync/upsert.py`)
Parser (`bitrix/statistic.py`) coerces string numerics to int/Decimal, `''`/null interchangeably to NULL, `Y/N` to bool, `CALL_START_DATE` with offset to UTC, and strips credential-bearing query parameters (`auth`, `token`, `sig`) from `CALL_RECORD_URL`. **Each row is parsed independently**: a row that fails (missing/unparsable `CALL_START_DATE`, absurd values) is skipped, counted in `portal_sync.rejected_rows` and recorded once as `portal_events(row_rejected, {bx_id, reason})` — never an exception that rolls back a chunk.

Rows are deduped by `bx_id` inside each chunk (a row deleted between two sub-commands can otherwise appear twice and raise "ON CONFLICT DO UPDATE command cannot affect row a second time"). Chunks of 500 rows run inside a `SAVEPOINT`; on any integrity error the chunk is retried row-by-row and the offending row is quarantined, so a single unexpected value can never block the cursor.

`INSERT … ON CONFLICT (portal_id, bx_id) DO UPDATE SET <all data columns>, last_synced_at = now(), refresh_requested = false, content_changed_at = CASE WHEN (old data columns except call_record_url) IS DISTINCT FROM (new …) THEN now() ELSE calls.content_changed_at END`. Excluding `call_record_url` from the comparison prevents an hourly rewrite of every row in the rescan window if the URL turns out to be per-read volatile.

Each chunk also inserts `employees` placeholders (`fetched_at NULL`, `ON CONFLICT DO NOTHING`) for unseen `PORTAL_USER_ID`s. Every one of these statements runs inside `tenant_txn(portal_id)`, and the fencing `UPDATE portal_sync … WHERE lease_owner=:me AND lease_expires_at>now() AND sync_generation=:gen` in the same transaction aborts the write if the lease or generation moved.

### 5.6 Rate limits, backoff, time block (`sync/throttle.py`)
- Pacing: ≥ 500 ms between HTTP requests per portal (2 req/s leaky bucket on non-Enterprise plans; `SYNC_RATE_PER_SEC` configurable). A batch is one request for the intensity bucket.
- Operating time: every response's `time.operating` / `time.operating_reset_at` (batch: max over `result_time`) is stored. Soft limit = `OPERATING_SOFT_RATIO` (0.8) × `capabilities.operating_limit_s` (default 480). Above it → `next_run_at = operating_reset_at + 60 s`, stop the visit.
- `429 OPERATION_TIME_LIMIT`: `next_run_at = operating_reset_at + 60 s`, `batch_pages` halved (min 5). The observed limit is lowered **only** when our own last observed `operating` was within 20 % of the current limit (a 429 can be caused by another app on the same account) and never below `OPERATING_LIMIT_FLOOR` (300 s); it is restored to the default after `CLEAN_VISITS_TO_RECOVER` (5) clean visits, so a foreign 429 cannot throttle a portal permanently.
- `503 QUERY_LIMIT_EXCEEDED` / `OVERLOAD_LIMIT` (HTTP level or per-command): honour `Retry-After` if present, else exponential 2, 4, 8 … 300 s into `next_run_at`, `batch_pages` halved.
- **Throttling is not failure**: 429/503 increment `throttle_hits`, never `consecutive_failures`, so a legitimately busy backfill is never dropped into the 6 h failure pause. `batch_pages` is restored to 20 after `CLEAN_VISITS_TO_RECOVER` clean visits.
- Other transient errors: after 10 consecutive failures the portal pauses 6 h and the settings page shows the last error; never a hot loop.

### 5.7 Late updates (recording URL, votes, comments, transcripts)
1. **ID-window rescan** every `RESCAN_INTERVAL_SEC` (3600): the lower bound is the persisted `rescan_from_id` (the `high_id` observed ~`RESCAN_WINDOW_HOURS` (72) ago, advanced monotonically each incremental run), never derived from `min(bx_id)` of a possibly empty window — a quiet weekend must not turn the hourly rescan into a full-history re-read. Re-read `FILTER {">=ID": rescan_from_id}` ASC, batch-paged, upsert; skipped when `rescan_from_id >= high_id`.
2. **Recording recheck budget** daily: candidates from `calls_portal_recheck_idx` older than 72 h and younger than 30 days (`record_recheck_count < 2`), re-read by `FILTER {"ID": [≤50 ids]}` per command. A call that truly has no recording stops costing requests after two rechecks; this is what covers recordings attached after the rescan window.
3. **On-demand refresh**: `POST /api/v1/calls/{id}/refresh` (called by the SPA when playback returns 403/404, rate-limited per portal) sets `refresh_requested=true`; the next sync visit re-reads those rows first and the upsert clears the flag.

### 5.8 Token handling in the worker (single-flight refresh)
`bitrix/oauth.py::with_portal_token(portal_id, fn)`:
1. Read the credential without a lock; remember `token_version`. If `token_expires_at < now() + 60 s`, go to step 3 first.
2. Call `fn(access_token)`. Anything other than `expired_token` (checked case-insensitively in the JSON `error` first, HTTP status second, and inside `result_error` for batch) is returned.
3. `SELECT … FOR UPDATE` the `portals` row. If `token_version` moved, another process already refreshed: take the stored token, commit, go to 5.
4. Else refresh at the allowlisted OAuth host; sanity-check response `member_id`; write both new tokens, `token_expires_at`, `token_refreshed_at`, `client_endpoint`, `token_version + 1`; commit. The lock is held across one HTTP call bounded by a 15 s timeout.
5. Retry `fn` exactly once with the new token. A second `expired_token` raises; the run records `last_error_*`, `consecutive_failures++`. No loop.

**Transport failures**: after `TRANSPORT_FAILURES_BEFORE_REFRESH` (3) consecutive connect/DNS/TLS failures the worker performs one refresh under the single-flight lock purely to re-learn `client_endpoint` (portal renamed) before backing off.

**Daily admin re-verification**: once a day (with the `app.info` call) the worker runs `user.admin` with the stored token; `false` → `token_status='no_stats_permission'`, `portal_events(sync_blocked)` — the installer was demoted or dismissed and the cache would silently narrow.

Terminal states set `token_status` and `next_run_at='infinity'`: OAuth `invalid_grant` / `NO_AUTH_FOUND` / `PAYMENT_REQUIRED` → `reauth_required`; `ACCESS_DENIED` on `statistic.get` → `no_stats_permission`; `insufficient_scope` → `reauth_required` with `last_error_code` (fail loudly, never retried); `PORTAL_DELETED` → `status='uninstalled'`, `purge_pending=true`. `portal_events(sync_blocked)` on entry, `sync_unblocked` when an admin open re-seeds. A 150-day-old `token_refreshed_at` shows a "re-authorize soon" banner; from 120 days an admin open re-seeds automatically.

**Inferred uninstall**: a portal with `token_status='reauth_required'` whose `last_admin_opened_at` (or `uninstalled_at`) is older than `UNINSTALL_GRACE_DAYS` (30) is set `status='uninstalled'`, `purge_pending=true`, `sync_generation+1`, `portal_events(uninstall_inferred)`. This guarantees brief rule 7 even if the events URL cannot be registered.

### 5.9 Job layer, tick and durability
- `jobs/protocol.py`: `JobBackend` with `schedule_periodic(name, seconds)` and `run_now(name, **kwargs)`. `jobs/definitions.py`: plain async functions — `tick()`, `sync_portal(portal_id)`, `purge_portal(portal_id)`, `purge_rest_log()`, `purge_crm_contexts()`. `apscheduler_backend.py` is the only file importing APScheduler and schedules **only** `tick` (every 15 s) and the daily purges.
- `tick()` (control transaction): (a) `SELECT portal_id … WHERE status='active' AND token_status='ok' AND NOT purge_pending AND next_run_at <= now() AND (lease_expires_at IS NULL OR lease_expires_at < now()) ORDER BY next_run_at LIMIT <free slots> FOR UPDATE SKIP LOCKED`, set `lease_owner`, `lease_expires_at = now() + 5 min`, `run_started_at = NULL`, and dispatch each portal as `asyncio.create_task` under a `Semaphore(GLOBAL_PORTAL_CONCURRENCY)` — never awaited inline, never a shared APScheduler job id (which would silently drop concurrent dispatches). An expired lease **with** `run_started_at` set is a crashed run → `consecutive_failures++`; an expired lease without it is a dispatch miss and is simply re-leased. (b) `SELECT id FROM portals WHERE purge_pending LIMIT 1` → `purge_portal`.
- `sync_portal(portal_id)` sets `run_started_at`, then in order: refresh_requested rows → head_fetch (pending/head) → incremental (if due) → backfill (up to 20 batches) → rescan (if due) → recheck (if due) → employees refresh (if due) → `app.info` + `user.admin` (daily). Heartbeat: `lease_expires_at` extended after every batch. On exit: lease cleared, `next_run_at = now() + SYNC_INTERVAL_SEC` (or `now() + 2 s` while backfilling).
- `purge_portal(portal_id)` runs **inside `tenant_txn(portal_id)`, opened afresh for every chunk** (RLS is transaction-local and fails silently closed): pre-count under tenant context, then `DELETE FROM calls WHERE portal_id = X AND id IN (SELECT id … LIMIT 10000)` per transaction until none, same for `employees` and `crm_contexts`. If the pre-count was > 0 and the first `DELETE` affected 0 rows, abort with `last_error_code='purge_incomplete'`, leave `purge_pending=true` and log `portal_events(purge_incomplete)`. After the loop, a final `SELECT count(*)` under the same context must be 0 before `purge_pending=false` and `portal_events(purge_done)`. When `purge_bodies` is set (uninstall with `data[CLEAN]=1`), the same job NULLs `rest_log.request`/`response` for that `portal_id` (keeping method/status/time rows so the exchange log itself survives).
- Durability without a queue: every job derives its work from columns; a crash at any point is resumed by the next tick from committed state.
- **Celery swap**: add `jobs/celery_backend.py` where beat schedules the same periodic names and a task wraps each definition; set `JOB_BACKEND=celery`. Job functions, tables, cursors, lease and fencing are unchanged.

---

## 6. REST logging & retention
- `bitrix/client.py` writes one `rest_log` row per outbound HTTP request (`kind=rest`/`oauth`) **in its own short transaction** (committed independently of the work transaction, and written on the exception path before re-raising), so a failed upsert still leaves the request/response logged: method, URL without query string, redacted params, HTTP status, `error`, response body as JSONB up to `REST_LOG_BODY_LIMIT` (256 KiB), `time_block`, `duration_ms`, `token_user_id`, `correlation_id`. A batch is one row with all sub-commands in `request` and `result_error`/`result_time` in `response`.
- `handlers/*` write one row per inbound Bitrix24 POST (`direction=in`, `kind=open|install|event`). Rejected events are logged with `portal_id` NULL and `member_id` kept.
- **Redaction is recursive** (`security/redact.py`): every dict/list is walked and any key matching `/(token|secret|auth|password)/i` at any depth is replaced by `[redacted]`, plus `auth=`/`token=`/`client_secret=` parameters inside URL strings. This covers `auth[*]`, `data{}` of `ONAPPUSERREADY`, refresh responses and future payload shapes. The same filter is attached to the root log handler; `httpx` and `httpcore` loggers are pinned to `WARNING` so their INFO request lines (which contain the full OAuth URL with `client_secret` and `refresh_token`) can never reach stdout. The FastAPI exception handler never logs request bodies, and `/api/v1/session/exchange` and `/api/v1/portal/reauthorize` are explicitly excluded from any body capture. `tests/test_secret_logging.py` runs a mocked refresh and asserts no secret substring appears in captured output.
- Retention: `purge_rest_log` daily deletes `ts < now() − REST_LOG_RETENTION_DAYS` (default 7) in batches of 10,000; `config.py` refuses values < 3. `purge_crm_contexts` deletes rows older than 30 days.
- Container stdout JSON logs (request id, method, status, no bodies) rotated by Docker `json-file` are the second trail. Caddy's access log has `resp_headers>Location` and request query strings removed.

---

## 7. Employee cache
- `employees` holds `user_brief` fields (no email or personal phone by scope — those are `user_basic`), `active`, `departments`, `photo_url`, `phone_inner`, `found`.
- `phone_inner` is `UF_PHONE_INNER`, part of `user_brief` and already present in the `user.get` answer the refresher parses (it sends no field list), so showing it in the employee filter costs no new scope and no extra request. It is the portal's own internal extension, not a contact detail.
- Writers (all inside `tenant_txn`): (a) the call upsert inserts placeholders for unseen `PORTAL_USER_ID`s; (b) `/app/` upserts the viewer from `user.current`; (c) `employees_refresh` inside `sync_portal` runs immediately when placeholders exist and otherwise every `EMPLOYEE_TTL_HOURS` (default 4): rows with `fetched_at IS NULL` first, then stale ones, chunked by 50 ids into `user.get {FILTER:{"@ID":[…]}, ADMIN_MODE:true}` commands, up to 50 commands per batch, with the portal token; on an `ADMIN_MODE` error retry without it. Never `ACTIVE=true` — dismissed users own historical calls; the UI greys them with a "dismissed" badge. Ids with no result get `found=false` and are retried daily.
- Readers: `GET /api/v1/filters`, the calls table and the per-employee breakdown join `employees` under RLS; a miss renders "User #id". All three render `phone_inner` in parentheses after the name, through one helper (`web/src/lib/format.ts::withExtension`), so the same person reads the same way in the filter, the table and the chart.

---

## 8. i18n structure
- **One message source.** `web/messages/<locale>.json` holds every string, including the handful used by the server-rendered `install.html`, `handoff.html`, `state.html` and `error.html`; the api image copies `messages/` and `src/i18n/locales.json` at build time and loads them with the same fallback map. `locales.json` (locale list + `kz→ru`, `uz→ru`, `*→en`) is the single shared definition used by the SPA, the handlers and the placement binder.
- Placement `TITLE`/`LANG_ALL` are **derived from the locale list**, so adding a language and re-binding from the settings page updates the CRM tab titles automatically.
- Adding Uzbek = drop `uz.json`, add one entry to `locales.json`, click "Re-bind placements". Frontend uses `next-intl` in non-routing mode with ICU plurals; a CI check fails on missing keys. Number/date formatting via `Intl`; timezone from the JWT `tz`.
- Backend API errors are machine codes (`no_stats_permission`, `portal_inactive`, `context_missing`, `crm_no_access`, …) translated by the SPA.

---

## 9. Recording playback spike plan
Run after milestone 4 (real rows exist), on the dev cloud portal (kz region) and, if available, one on-premise portal; results go to `docs/spike-recording-playback.md`. The mechanism under test is the one that ships: a **short-lived signed playback URL** (`GET /api/v1/calls/{id}/record?t=…`, minted by `POST /api/v1/calls/{id}/play-url`), because an `<audio src>` cannot carry an `Authorization` header and a `fetch`+Blob workaround would destroy Range seeking.
1. **Capture**: from cached rows with `has_record`, list the distribution of (`call_record_url` populated?, `record_file_id` populated?) per `rest_app_id`; record three real `CALL_RECORD_URL` values verbatim **before** the parser strips them (host, path, query, any `auth`/signature/expiry parameter). This decides immediately whether the value is credential-bearing.
2. **Option 1 — direct `<audio>`**: a throwaway page on `b24.texnobus.uz` inside the iframe with `<audio src=CALL_RECORD_URL controls>`; measure plays / CORS or cookie failure / redirect target; repeat after 1 h, after a portal token refresh, from an incognito browser, and for a user without the "listen to recordings" permission. Also `curl -I` with and without `Range`.
3. **Option 2 — backend proxy** (`RECORDING_MODE=proxy`): the signed-URL endpoint streams with `httpx` (streaming, `Range`/`Content-Range`/`Accept-Ranges` forwarded, nothing to disk). **For a non-admin viewer the proxy uses the viewer's own token** (fresh from `BX24.getAuth()`, verified via `user.current`, same `sub` as the session) so Bitrix24's separate "listen to recordings" right is enforced by Bitrix24 itself; the portal (admin) token is used only for admin viewers. Measure: status, seekability, TTFB, behaviour on 403/404 (stale link → on-demand refresh), api container memory while streaming a 10-minute file, cost against the 2 req/s bucket.
4. **Option 3 — `disk.file.get`**: only a `curl` to confirm `insufficient_scope` without the `disk` scope; if 1 and 2 both fail, stop and report for the owner's decision.
5. **Decision matrix** → the production value of `RECORDING_MODE`. **`redirect` is permitted only if step 1 proves the URL carries no credential and Bitrix24 enforces the listener's rights server-side**; if the URL is of the `download.json?auth=<token>` family, `redirect` is forbidden outright (it would hand the portal access token to every viewer) and `call_record_url` is treated as a secret everywhere (never sent to the browser, stripped on parse, redacted in logs). Until decided: `off` → the table shows a "has recording" icon and a `BX24.openPath` link to the call in Bitrix24.

---

## 10. Milestone-1 build order
1. Repo skeleton as in §2, `docker-compose.yml` (postgres, api, worker, web), `docker/postgres/init.sql`, `.env.example` (`B24_CLIENT_ID`, `B24_CLIENT_SECRET`, `OAUTH_HOST_ALLOWLIST`, `OAUTH_EXCHANGE_LIMIT`, `APP_BASE_URL`, `DATABASE_URL`, `DATABASE_URL_MIGRATIONS`, `TOKEN_ENC_KEYS`, `TOKEN_ENC_ACTIVE_KEY_ID`, `SESSION_SECRET`, `SYNC_INTERVAL_SEC`, `RESCAN_INTERVAL_SEC`, `RESCAN_WINDOW_HOURS`, `EMPLOYEE_TTL_HOURS`, `GLOBAL_PORTAL_CONCURRENCY`, `BACKFILL_BATCHES_PER_VISIT`, `OPERATING_SOFT_RATIO`, `OPERATING_LIMIT_FLOOR`, `SYNC_RATE_PER_SEC`, `REST_LOG_RETENTION_DAYS`, `REST_LOG_BODY_LIMIT`, `CRM_ACTIVITY_CAP`, `TOKEN_RESEED_AFTER_DAYS`, `UNINSTALL_GRACE_DAYS`, `MAX_PERIOD_DAYS`, `RECORDING_MODE`, `JOB_BACKEND`, `SCHEDULER_INLINE`, `LOG_LEVEL`), Alembic `0001_baseline`, `tests/test_isolation.py` and `tests/test_purge.py` green.
2. `bitrix/forms.py`, `errors.py`, `client.py`, `oauth.py`, `identity.py`, `security/crypto.py`, `security/redact.py`, `logging.py`; `POST /install/` end to end on the dev portal; `test_client_refresh.py`, `test_refresh_singleflight.py`, `test_install_guard.py`, `test_secret_logging.py` green; refresh proven by forcing `expired_token` and observing exactly one refresh in `rest_log`. **Record in `docs/bitrix24-flows.md` whether consuming the POSTed `REFRESH_ID` invalidates the paired `AUTH_ID` the iframe keeps using.**
3. `POST /app/` + `/settings/` router, JWT + `handoff.html` (original query string forwarded, `APP_SID` preserved), `get_principal`, `web/` layout with BX24 init/fitWindow, `dashboard` and `crm` placeholder views, all `state/[kind]` pages, per-request CSP; verify on the dev portal that `BX24.init` resolves and `BX24.placement.info()` returns the expected placement on **every** placement; `test_no_token_in_headers.py` green.
4. Worker: `tick` + semaphore dispatch, lease + `sync_generation` fencing, `head_fetch`, incremental (probe-then-pack), backfill, throttle, upsert with quarantine, employees refresh; real calls in Postgres; backfill progress on the settings page; `test_cursor.py`, `test_upsert.py`, `test_fencing.py` green.
5. Dashboard API (`/dashboard` as one GROUPING SETS query, `/calls`, `/filters`, `/me`, `/session/exchange`), charts and table with real numbers in the viewer timezone (custom period capped at `MAX_PERIOD_DAYS` = 366), `own`/`denied` states verified with a non-admin user; `crm_contexts` and deal tab matching incl. the raw-`DEAL` clause; `docs/moderation-checklist.md` walked once.
6. `POST /events/` with the full verification ladder (`application_token` first, admin proof for `ONAPPUPDATE`, `ts` idempotency), worker purge with the emptiness assertion, inferred-uninstall rule, reinstall path, `rest_log` retention and CLEAN body wipe; `test_events_auth.py` green.
7. Recording spike (§9), `RECORDING_MODE` decision, `docs/spike-recording-playback.md`.

---

## 11. Assumptions
1. The statistics record `ID` is strictly monotonic with row creation per portal for all telephony providers; both cursors and the immutable `<low_id` window rely on it. A build that ignores the `>ID`/`<ID` operators is detected at runtime and stops the portal with `filter_unsupported` rather than looping.
2. Statistics rows are not deleted in bulk; a rare deletion shifts at most one offset inside a single batch, absorbed by the in-chunk dedupe, the upsert and the rescan.
3. A user access token issued for portal A is rejected when presented at portal B's `client_endpoint`, so verification at the stored endpoint defeats `member_id` spoofing. `member_id` itself is public.
4. Installation is restricted to portal administrators and administrators always have full telephony access, so a credential proven to be admin-owned returns the complete call history; this is re-verified daily.
5. `user.admin=true` implies full "Call statistics" visibility; non-admins with "department" or "any" level are shown their own calls only in v1 (documented simplification). The probe is skipped for admins.
6. `member_id` is always 32 lowercase hex characters and survives portal renames and custom-domain connection; a rename is recovered by the transport-failure refresh path.
7. One refresh exchange at install / self-heal / re-authorize is acceptable under Bitrix24's "do not refresh excessively" rule; routine opens perform none, and unauthenticated exchange triggers are rate-limited.
8. The vendor cabinet allows an "Event installation handler URL" set to `https://b24.texnobus.uz/events/`; if it does not, `event.bind` at install and the `event=` body dispatch may still deliver events, and the inferred-uninstall rule guarantees cleanup either way.
9. `/settings/` is opened like `/app/` (iframe POST with the same fields); the JSON form-builder branch is a fallback.
10. The left-menu item comes from the version-card option and arrives as `PLACEMENT=DEFAULT`; `LEFT_MENU` is not bound but is accepted by the router.
11. The BX24 JS SDK requires the original `APP_SID`/`DOMAIN`/`PROTOCOL`/`LANG` query parameters on the SPA URL; the handoff page forwards them verbatim.
12. Browsers execute the handoff page's `location.replace` (no redirect/fragment assumptions remain).
13. A separate `worker` container (same image) is acceptable despite the brief's three-service list; `SCHEDULER_INLINE=1` collapses it into `api`.
14. Rate limits of the Basic plan apply (2 req/s, burst 50; operating 480 s / 10 min cloud, 420 s on-premise); pacing 500 ms, global concurrency 4, batch 20 pages, adaptive limit floored at 300 s and recovered after clean visits.
15. A 72-hour ID-window rescan plus two targeted rechecks up to 30 days captures late-attached recordings, transcripts, votes and comments; edits beyond that are accepted as missed.
16. `user.current` under `user_brief` returns `TIME_ZONE`; fallback is the installer's timezone, then UTC. Aggregations are computed live in SQL over a covering index; custom periods are capped at 366 days.
17. `user.get` with `ADMIN_MODE=true` under the portal token returns dismissed users when not filtered by `ACTIVE`; if `ADMIN_MODE` errors, plain `user.get` is used.
18. Deal tab = calls linked to the deal's contacts/company, calls whose `CRM_ACTIVITY_ID` is among the deal's call activities (capped at 250 = the handler's 5-page budget), **and** any row a portal happens to emit with `CRM_ENTITY_TYPE='DEAL'`.
19. Disk/volume and backup encryption on the Hetzner host is handled by ops; only OAuth and application tokens are application-encrypted; phone numbers stay plaintext for filtering. `call_record_url` is stripped of credentials and never leaves the server.
20. Fully isolated on-premise boxes with a custom auth provider (no refresh token) are **out of scope in v1**: they get an explicit state page and no database row. Ordinary on-premise portals, which refresh through `oauth.bitrix.info`, are fully supported.
21. Cached calls, employees and crm_contexts are purged on uninstall regardless of `data[CLEAN]`; `CLEAN=1` additionally wipes `rest_log` bodies for that portal. `portals` and `portal_events` are kept forever.
22. `RECORDING_MODE=off` until the spike is answered; `redirect` is available only if the captured URL carries no credential.
23. Bitrix24 gates app access at the product level; the app implements only the admin / own / denied split.
24. The ONAPPUSERREADY system-user credential is deliberately not stored: it carries regular-employee rights, would silently truncate the cache if ever used, and a stored 180-day token is a liability.

---

## 12. Open questions for the owner
1. **Settings URL contract** — iframe POST like `/app/`, or the REST-only `OnAppSettings*` JSON form contract? *Default: iframe POST admin status page; the JSON branch answers a zero-step form and logs the payload in milestone 3.*
2. **Event handler URL** — can the vendor cabinet register `https://b24.texnobus.uz/events/`, and does `ONAPPINSTALL` arrive for an interface app? *Default: register it; `event.bind` and the inferred-uninstall rule cover the case where it cannot be registered.*
3. **Isolated on-premise boxes** — accept that portals without a refresh token are unsupported in v1 (explicit state page, nothing stored)? *Default: yes; supporting them safely requires an ops-pinned endpoint allowlist, which is v1.x work.*
4. **Permission simplification** — accept admin / own / denied? *Default: accept for v1; test whether `statistic.get` silently filters for a Manager-role token and revisit in v1.x.*
5. **Worker container** — fourth compose service or scheduler inside `api`? *Default: separate `worker`; `SCHEDULER_INLINE=1` remains available.*
6. **`disk` scope** — add it if the spike shows only `disk.file.get` yields a playable recording? *Default: no in v1; ship the "open in Bitrix24" link.*
7. **rest_log retention and body cap** — 7 days / 256 KiB, or the strict 3-day minimum? *Default: 7 days, 256 KiB, batched DELETE; bodies wiped on CLEAN=1 uninstall.*
8. **Credential takeover** — re-seed only when dead/aged/explicit (current), or let any admin open take over sync credentials? *Default: current rule; routine opens perform no OAuth exchange at all.*
9. **Timezone for "today" and the heatmap** — viewer's `TIME_ZONE` (current) or one portal-wide timezone? *Default: viewer's, installer's as fallback.*
10. **Backfill UX and period cap** — newest-first backfill (~2 h per 500k rows) with a progress banner, and a 366-day cap on custom periods? *Default: yes.*
11. **Phone number display for non-admins** — masked or full as in Bitrix24? *Default: full.*
12. **Grace period after uninstall** — purge immediately (current) or keep rows for N days for a fast reinstall? *Default: purge immediately; the 30-day `UNINSTALL_GRACE_DAYS` applies only to the *inferred* uninstall path.*
13. **ONVOXIMPLANTCALLEND** — bind it in v1.x for immediate sync instead of 5-minute polling? *Default: polling only in v1.*
14. **LANG values from kz-region portals** — `ru`, `kz` or `en`? *Default: fallback map `kz→ru`, unknown→`en`; confirm in milestone 3.*
15. **Employee filter for `own` users** — hidden with a banner (current) or shown disabled? *Default: hidden.*
16. **Optional source-IP allowlist on `/events/`** — add the `dl.bitrix24.com/webhook/app-world.json` check as defence in depth, accepting that a stale list can reject real events? *Default: documented but off; the `application_token` + admin proof is the control.*
