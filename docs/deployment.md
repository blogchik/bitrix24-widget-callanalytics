# Deployment

How to put Call Analytics on `b24.texnobus.uz` and keep it there. Written to be followed
top to bottom the first time and used as a reference afterwards.

Read [architecture.md](architecture.md) §2 and §4.10 first if you want to know why the
routing looks the way it does; this document assumes the design and only tells you what
to run.

---

## What you need before you start

| Thing | Why |
| --- | --- |
| A Linux host with Docker Engine and the compose plugin | Runs the four containers |
| Caddy on the host, outside compose | Terminates TLS and routes; see [the snippet](../docker/Caddyfile.snippet) |
| `b24.texnobus.uz` resolving to that host's public address | Caddy cannot obtain a certificate without it |
| Ports 80 and 443 reachable from the internet | Let's Encrypt validates over HTTP |
| `B24_CLIENT_ID` and `B24_CLIENT_SECRET` from the Bitrix24 vendor cabinet | The app cannot exchange a token without them |
| Somewhere off this machine to store one secret | See [Back up the key ring](#back-up-the-key-ring-before-the-first-install) |

The host needs roughly 4 GB of RAM and 20 GB of disk for a few hundred portals. The
database stores call metadata only, never audio: a portal with 500,000 calls costs on
the order of 200 MB.

---

## First deploy

### 1. Get the code onto the host

```bash
git clone <repo> /opt/callanalytics
cd /opt/callanalytics
```

### 2. Build the environment file

```bash
./tools/make-env.sh
```

This writes `.env` with a fresh encryption key ring, a session secret and three
PostgreSQL passwords, and rewrites both `DATABASE_URL`s so their passwords match. It
leaves two values as loud placeholders, because only you have them:

```bash
$EDITOR .env      # set B24_CLIENT_ID and B24_CLIENT_SECRET
```

Confirm `APP_BASE_URL=https://b24.texnobus.uz`. The app builds the placement handler
URLs from it, so a wrong value binds every portal's CRM tabs to the wrong address.

### 3. Back up the key ring before the first install

`TOKEN_ENC_KEYS` decrypts every portal's OAuth and application tokens. A database backup
without it restores rows that nothing can read, and every portal would have to be
re-authorised by an administrator opening the app again.

```bash
grep '^TOKEN_ENC_KEYS=' .env      # copy this into your password manager, now
```

Do this before the first portal installs, not after.

### 4. Start the stack

```bash
export IMAGE_TAG=$(git rev-parse --short HEAD)
make prod-build
make prod-up
```

`make prod-up` waits for every healthcheck, so when it returns the API is answering and
the database is reachable. If it times out, `make prod-logs` says why.

### 5. Apply the schema

```bash
make prod-migrate
```

The roles `ca_owner` and `ca_app` were created by `docker/postgres/init.sql` on the
database's first boot. Migrations run as `ca_owner`; the application runs as `ca_app`,
which cannot bypass row-level security. Verify both:

```bash
docker compose exec -T postgres psql -U postgres -tAc \
  "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname LIKE 'ca_%';"
# ca_owner | f
# ca_app   | f
```

### 6. Configure Caddy

Copy the site block from [`docker/Caddyfile.snippet`](../docker/Caddyfile.snippet) into
`/etc/caddy/Caddyfile`. Read the comments in it before you paste: the trailing slash on
`/settings/` is load-bearing, and `X-Frame-Options` must stay stripped or the app cannot
render inside a Bitrix24 iframe at all.

If Caddy runs on the host rather than on the compose network, change the two
`reverse_proxy` lines from `api:8000` and `web:3000` to `127.0.0.1:8000` and
`127.0.0.1:3000`, which is what compose publishes.

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

### 7. Smoke-test before you tell Bitrix24 about it

```bash
curl -sS https://b24.texnobus.uz/healthz                  # {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' \
     https://b24.texnobus.uz/                              # 200, the SPA
curl -sSI https://b24.texnobus.uz/ | grep -i x-frame       # must print nothing
curl -sS "https://b24.texnobus.uz/settings?DOMAIN=x.bitrix24.kz&PROTOCOL=1" \
  | head -c 40                                             # HTML, never JSON
```

The last one is the check worth keeping: it is the exact path a moderator walks, and
routing it to the wrong upstream produced a raw error inside the iframe once already.

### 8. Point the vendor cabinet at it

Handler `https://b24.texnobus.uz/app/`, install `https://b24.texnobus.uz/install/`,
settings `https://b24.texnobus.uz/settings/`, and — if the cabinet offers the field —
the event handler `https://b24.texnobus.uz/events/`.

Then install on a development portal and walk
[the moderation checklist](moderation-checklist.md) end to end.

---

## Upgrading

```bash
cd /opt/callanalytics
git fetch && git checkout <tag-or-sha>
export IMAGE_TAG=$(git rev-parse --short HEAD)
make prod-build
make prod-migrate      # before the new code runs, so old code never sees a new schema
make prod-up           # recreates only what changed, waits for health
```

Two things make this safe to do during the day:

- The worker holds a lease on each portal it is syncing and is given two minutes to
  finish. A visit cut short costs one re-fetched batch, never data: rows and cursor
  commit in the same transaction.
- The API is stateless. The session token is a signed JWT, so a restart does not log
  anyone out; the browser simply retries.

Run migrations **before** the new containers start. A migration is additive by
convention, so old code tolerates a new column; the reverse is not true.

## Rolling back

Images are tagged, so a rollback is a tag change:

```bash
export IMAGE_TAG=<previous-sha>
make prod-up
```

A schema rollback is a different question and usually the wrong move. `downgrade()`
exists in every revision and is correct, but running it discards whatever the newer
columns hold. Prefer rolling the images back and leaving the schema forward.

---

## Backups

```bash
./tools/backup.sh /var/backups/callanalytics
```

Add it to cron, daily, and keep the output somewhere other than this host:

```cron
17 3 * * * cd /opt/callanalytics && ./tools/backup.sh /var/backups/callanalytics >> /var/log/callanalytics-backup.log 2>&1
```

The script verifies the dump is readable and actually contains the `portals` table
before it rotates old files, because a truncated backup that looks like a backup is
worse than none.

**A dump alone is not a recovery plan.** Restoring needs both the dump and the
`TOKEN_ENC_KEYS` value that was live when it was taken. Store them separately, and test
the restore once on a scratch host:

```bash
gunzip -c callanalytics-<stamp>.sql.gz | docker compose exec -T postgres psql -U postgres -d callanalytics
```

What survives a restore and what does not:

- `portals`, `portal_sync` — irreplaceable. Tokens, cursors, placement state.
- `calls`, `employees`, `crm_contexts` — rebuildable. The worker re-fetches them from
  Bitrix24; the cost is a backfill, not data.
- `rest_log` — retention is seven days anyway.

---

## Operating it

### Where to look when a portal complains

Everything starts from the portal's row:

```bash
docker compose exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT id, domain, status, token_status, purge_pending FROM portals WHERE domain = 'acme.bitrix24.kz';"
```

`token_status` is the one column that explains why sync stopped:

| Value | Meaning | What fixes it |
| --- | --- | --- |
| `ok` | Syncing | — |
| `reauth_required` | The refresh chain is dead or was revoked | An administrator opens the app; it re-seeds automatically |
| `no_stats_permission` | The stored credential lost "Call statistics — view" | Restore the right in Bitrix24, then an administrator opens the app |
| `method_missing` | This build has no `voximplant.statistic.get` | On-premise upgrade; nothing we can do |
| `filter_unsupported` | The build ignores the `ID` filter operator | Reported so it parks instead of looping; needs investigation |

Then the lifecycle audit, which outlives the REST log's retention:

```bash
docker compose exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT created_at, kind, details FROM portal_events WHERE portal_id = 42 ORDER BY id DESC LIMIT 20;"
```

And the REST exchanges themselves, kept seven days with every credential redacted:

```bash
docker compose exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT ts, direction, kind, method, http_status, error_code FROM rest_log WHERE portal_id = 42 ORDER BY id DESC LIMIT 30;"
```

### Reading the container logs

One JSON object per line, correlated by `request_id`, with every token redacted before
serialization:

```bash
make prod-logs                                   # everything
docker compose logs -f worker | grep -v '"level": "DEBUG"'
docker compose logs api | grep '"request_id": "<id>"'
```

### Things that are working as designed

- **A portal shows "authorisation expired" right after you seed it by hand.** The worker
  found a portal with no credential and parked it correctly.
- **The first sync takes an hour or two on a busy portal.** The backfill is deliberately
  paced: every tenant shares one source IP against a 2 requests-per-second limit. The
  dashboard shows recent days within seconds and fills in history behind a progress
  banner.
- **`rejected_rows` is non-zero.** Rows Bitrix24 returned that could not be parsed were
  quarantined so one bad row cannot stall the cursor. A non-zero count is worth
  reporting to us, not worth an incident.

### Things that are not

- **`purge_pending` set for more than a few hours.** Look for `purge_incomplete` in
  `portal_events`.
- **`consecutive_failures` climbing past 10.** The portal is paused for six hours;
  `last_error_code` says why.
- **The worker container restarting.** It should run for weeks. Check its logs for an
  unhandled exception rather than restarting it.

The worker deliberately has no healthcheck: it is a scheduler with nothing to serve, and
a probe that only proved the process exists would be worse than none. To see whether it
is actually working, look at what it writes — `next_run_at` moves on every visit:

```bash
docker compose exec -T postgres psql -U ca_owner -d callanalytics -c   "SELECT portal_id, next_run_at, lease_owner, last_error_code FROM portal_sync ORDER BY next_run_at LIMIT 10;"
```

Every `next_run_at` in the past on an active portal with no lease owner, for more than a
minute, means the tick is not running.

---

## Configuration reference

Every setting is an environment variable, documented inline in
[`.env.example`](../.env.example). The ones worth knowing on day one:

| Variable | Default | Notes |
| --- | --- | --- |
| `RECORDING_MODE` | `off` | `off` until the [playback spike](spike-recording-playback.md) is answered. `redirect` is refused at startup. |
| `REST_LOG_RETENTION_DAYS` | `7` | Bitrix24 moderation requires at least 3; the app refuses to start below that. |
| `GLOBAL_PORTAL_CONCURRENCY` | `4` | Portals synced in parallel. Raising it does not raise the per-portal rate limit. |
| `SYNC_INTERVAL_SEC` | `300` | How often a healthy portal is revisited. |
| `UNINSTALL_GRACE_DAYS` | `30` | A portal whose token chain died and which no administrator has opened is treated as uninstalled and purged. |
| `API_WORKERS` | `2` | uvicorn processes. The worker container is always exactly one. |
| `LOG_LEVEL` | `INFO` | `DEBUG` is noisy but never prints a token. |

Changing any of them means editing `.env` and running `make prod-up`, which recreates
the containers whose environment changed.
