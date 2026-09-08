# Call Analytics — Bitrix24 Marketplace app

`texnobus.callanalytics` — a single deployment that serves many Bitrix24 portals and shows
each of them their own telephony activity: a dashboard on the left-menu page and a call list
on the Deal / Lead / Contact / Company detail tabs.

The app is **not** a telephony provider. It registers no calls, uploads no recordings and
connects no PBX. It reads `voximplant.statistic.get` and presents it.

## Status

Milestone 1 is built and verified. The recording-playback spike is the one open item: it
needs a real portal with recorded calls, so `RECORDING_MODE` stays `off` and the player
offers an "open in Bitrix24" link instead.

| Step | State |
| --- | --- |
| Repo skeleton, Docker Compose, Alembic baseline | done |
| Install flow with tokens stored and refresh proven | done |
| Placement routing: left menu and four CRM tabs render distinct views | done |
| Incremental sync pulling calls into PostgreSQL | done |
| Dashboard rendering real numbers with the permission states | done |
| Lifecycle events, purge, retention | done |
| Recording playback spike | needs a real portal — see `docs/spike-recording-playback.md` |

396 tests pass against a real PostgreSQL 16.

## Running it locally

```bash
cp .env.example .env          # then fill in the marked values
docker compose up -d postgres
docker compose run --rm api python -m alembic upgrade head
docker compose up -d api worker web
curl http://127.0.0.1:8000/healthz
```

`make test` runs the suite; `make migrate` applies migrations; `make logs` tails everything.
`tools/README.md` explains how to seed a demo portal and screenshot the dashboard.
To put it on a server, follow [docs/deployment.md](docs/deployment.md) — written for the
shared host it actually runs on (Cloudflare Tunnel ingress, no published ports, raw
`docker compose` because there is no `make` there), and covering upgrades, rollback,
backups and what to look at when a portal complains.

The app itself is only reachable through Bitrix24, which POSTs to `/install/`, `/app/` and
`/settings/`. Opening `http://127.0.0.1:3000/` directly renders "open this app from Bitrix24",
which is the correct answer rather than an error.

## How it is put together

- **`api/`** — Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL 16, httpx,
  APScheduler. One image serves both the web API and the sync worker.
- **`web/`** — Next.js App Router, TypeScript, Tailwind, `next-intl`, rendered inside the
  Bitrix24 iframe.
- **PostgreSQL is the only stateful component.** No Redis, no broker: every unit of work the
  worker does is derivable from columns, so a crash resumes from committed state.

Three properties are worth knowing before reading the code:

1. **Tenant isolation is structural, not conventional.** `calls`, `employees` and
   `crm_contexts` carry forced row-level security bound to a transaction-local setting, and
   the runtime database role cannot bypass it. A query that forgets its tenant context
   returns zero rows rather than another portal's. Because that failure is silent, every job
   that writes or deletes must open `tenant_txn` per transaction and verify its effect.
2. **The sync cursor is the statistics record id, never the call start date.** Rows are
   created when a call finishes and can carry backdated start dates, so a date cursor skips
   calls. A batch's cursor advances only across the longest error-free prefix of its
   sub-commands, or one failed page would orphan fifty rows forever.
3. **Bitrix24's answer is the permission model.** The app asks `user.admin` and, for
   everyone else, probes the statistics method with the viewer's own token. Administrators
   see the portal's calls, everyone else sees their own, and a user with no rights gets an
   explanation rather than an empty screen.

## Documents

| Document | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | The design: decisions, file structure, full DDL, auth and request flows, sync, logging, i18n, spike plan |
| [docs/architecture-appendix.md](docs/architecture-appendix.md) | Assumptions, open questions, and the disposition of every review finding |
| [docs/bitrix24-api-research.md](docs/bitrix24-api-research.md) | Verified facts from the official documentation, including where they contradict the original brief |
| [docs/design-review-findings.md](docs/design-review-findings.md) | The adversarial review that shaped the design: 38 issues with failure scenarios |
| [docs/moderation-checklist.md](docs/moderation-checklist.md) | The manual test script to walk before submitting to Bitrix24 |
| [docs/spike-recording-playback.md](docs/spike-recording-playback.md) | The playback protocol, ready to run on a real portal |
| [docs/deployment.md](docs/deployment.md) | First deploy, upgrade, rollback, backups, and the operator's reference |

## Configuration

Every setting is an environment variable and every one is documented in `.env.example`.
No secret is ever committed: `client_id` and `client_secret` come from the environment,
OAuth and application tokens are encrypted at rest with a key-ringed AES-256-GCM envelope,
and the redaction filter that guards logs and the REST audit trail is exercised by a test
that scans for every secret as a substring.
