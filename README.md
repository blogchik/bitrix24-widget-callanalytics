# Call Analytics — Bitrix24 Marketplace app

`texnobus.callanalytics` — a single deployment that serves many Bitrix24 portals and shows
each of them their own telephony activity: a dashboard on the left-menu page and a call list
on the Deal / Lead / Contact / Company detail tabs.

The app is **not** a telephony provider. It registers no calls, uploads no recordings and
connects no PBX. It reads `voximplant.statistic.get` and presents it.

## Status

Deployed and serving a real portal at `https://b24.texnobus.uz`. Milestone 1 is built and
verified end to end; the UI was rebuilt on a control kit of its own and measured at four
viewport widths; and the project now ships through CI/CD rather than by hand.

| Step | State |
| --- | --- |
| Repo skeleton, Docker Compose, Alembic baseline | done |
| Install flow with tokens stored and refresh proven | done |
| Placement routing: left menu and four CRM tabs render distinct views | done |
| Incremental sync pulling calls into PostgreSQL | done |
| Dashboard rendering real numbers with the permission states | done |
| Lifecycle events, purge, retention | done |
| Deployed to production behind a Cloudflare Tunnel | done |
| Custom UI kit: select, input, date range, audio player, responsive layout | done |
| CI/CD: `dev` for work, `main` for deployment, images from GHCR | done |
| UTM analytics: leads and deals by advertising tag, read live from CRM | done |
| Recording playback in a real browser, inside a real portal | open — `docs/spike-recording-playback.md` |
| Bitrix24 Marketplace moderation | not started — `docs/moderation-checklist.md` |

530 tests pass against a real PostgreSQL 16.

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
- **`web/`** — Next.js 15 App Router, TypeScript, Tailwind, `next-intl` 4, rendered inside
  the Bitrix24 iframe. Every control it needs and cannot get from the platform — select,
  text input, segmented control, date range, audio transport — is in
  `web/src/components/ui/`, sharing one height, one radius and one motion vocabulary, all
  of it expressed as CSS custom properties so `prefers-reduced-motion` is honoured in one
  place rather than in twenty.
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

## How it is developed and shipped

Two branches, and the difference between them is the whole workflow.

| Branch | What it is | What may push to it |
| --- | --- | --- |
| `dev` | Where the work happens. The default branch: a clone lands here and a new pull request targets it. | Anyone with write access, directly. It cannot be force-pushed or deleted. |
| `main` | The deployment branch. What is on it is what is in production, or about to be. | Nothing directly. A pull request from `dev`, with every CI check green. |

A change therefore travels: `dev` — pull request — CI — `main` — images published — a
human approves the deployment — the host pulls. Nothing skips a step, and the last two
are separate on purpose: publishing an image is cheap and reversible, and putting it in
front of other companies' telephony data is neither.

A promotion is merged with a **merge commit**, not squashed and not rebased. Both of
those rewrite commit shas, and a sha is the unit of deployment here: `deploy <sha>` and
`rollback` name commits, and `/var/lib/callanalytics/current` records one. A history where
the sha that was deployed cannot be checked out is a history that cannot answer the only
question that matters during an incident.

### What CI checks

[`.github/workflows/ci.yml`](.github/workflows/ci.yml), four jobs in parallel so a
TypeScript error and a failing migration are not the same red X:

- **Invariants** — three checks that were being done by eye, each of which had already
  been got wrong once:
  [`check-i18n.mjs`](tools/check-i18n.mjs) (the catalogues carry the same keys *and* the
  same ICU placeholders in every language),
  [`check-compose.sh`](tools/check-compose.sh) (the production render publishes no port,
  mounts no source tree over an image, carries a real `IMAGE_TAG`, and still splits `api`
  from `web`), and
  [`check-actions.sh`](tools/check-actions.sh) (every action is pinned to a commit sha and
  none runs on a Node version GitHub has deprecated).
- **API** — builds the image, applies the migrations, asserts the three tenancy
  properties they must produce against the database they actually produced, runs the
  migrations *down to base and back up* because `downgrade()` is the rollback path and is
  otherwise never executed, runs the suite against a real PostgreSQL 16, then `ruff` and
  `mypy`.
- **Web** — `npm ci`, `tsc --noEmit`, `next build`, and `npm audit` as a gate rather than
  a report.
- **Dependency review** — on pull requests, blocks one that introduces a vulnerable or
  strong-copyleft dependency, which is the cheapest moment to say no.

[CodeQL](.github/workflows/codeql.yml) runs alongside on a schedule as well as on
changes: a query published next month should find a bug written last month, and nothing
else here would ever look again.

### How a deployment happens

[`.github/workflows/cd.yml`](.github/workflows/cd.yml). Merging to `main` builds both
images **on the runner**, tags them with the full commit sha, pushes them to GHCR and
scans them with Trivy. Nothing is built on the production host: it is a shared box with
two cores and five other projects, three of them PostgreSQL, and a build there is felt by
all of them.

The deploy job then waits for a human. Approving it runs exactly one command over SSH,
and the key on the host can run exactly three:

```
deploy <40-hex-sha>    pull that tag, migrate, restart, verify from inside
rollback               go back to the previously recorded tag; no build, no migration
status                 what is running, and what rollback would return to
```

That is enforced by `command=` in `authorized_keys`, so the key has no shell, no pty and
no port forwarding. The script it is pinned to
([`deploy/ci-deploy.sh`](deploy/ci-deploy.sh)) is installed *outside* the checkout, as
`/usr/local/bin/callanalytics-deploy`, because a deployment checks the tree out at the
requested commit — a copy living inside that tree would rewrite the very thing the forced
command points at, and the restriction would hold only until the first deployment.

After the restart the workflow smoke-tests the public URL from outside, through
Cloudflare and the tunnel, the way a portal reaches it. A failure rolls back to the
previous tag automatically.

Deploying by hand is still possible — [docs/deployment.md](docs/deployment.md) documents
it as the break-glass path — but it is no longer how anything normally ships.

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

## Licence

MIT — see [LICENSE](LICENSE). Use it, fork it, run your own copy; the only thing asked is
that the notice travels with it.

The Bitrix24 Marketplace listing for `texnobus.callanalytics` is a separate matter: the
licence covers this source, not the published app or the instance at `b24.texnobus.uz`.

## Configuration

Every setting is an environment variable and every one is documented in `.env.example`.
No secret is ever committed: `client_id` and `client_secret` come from the environment,
OAuth and application tokens are encrypted at rest with a key-ringed AES-256-GCM envelope,
and the redaction filter that guards logs and the REST audit trail is exercised by a test
that scans for every secret as a substring.
