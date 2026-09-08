# Security Policy

Call Analytics (`texnobus.callanalytics`) is a Bitrix24 Marketplace app: **one deployment
serves many portals**, and each portal's call history, employee list and OAuth credentials
live in the same PostgreSQL database as everybody else's. That single fact decides how we
rank every report. The worst outcomes, in order, are:

1. one portal reading, writing or deleting another portal's rows;
2. a portal's OAuth credential or `application_token` leaving the system;
3. a user seeing calls that Bitrix24 would not have shown them.

Everything below follows from that. The design and its rationale are in
[docs/architecture.md](docs/architecture.md); section numbers referenced here point into it.

---

## Supported versions

The project has no tagged releases. Two branches carry everything:

| Branch | What it is | Supported |
| --- | --- | --- |
| `main` | The deployment branch. Its head is what runs at `https://b24.texnobus.uz`. | ✅ |
| `dev` | Where work happens. Reaches `main` only through a reviewed pull request with green CI. | ✅ — report against it, but the fix ships from `main` |
| Any older commit, fork, or modified deployment | — | ❌ |

A fix lands on `dev`, is promoted to `main` by pull request, and is deployed from there by
[the CD pipeline](.github/workflows/cd.yml) — never by hand, except as break-glass. There
are no backports and no LTS branch.

Because a deployment is a commit sha, "the version you tested" is the sha you tested. That
is the single most useful thing you can put in a report.

---

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a security problem.**

Preferred channel — GitHub private vulnerability reporting:

> **[Report a vulnerability](https://github.com/blogchik/bitrix24-widget-callanalytics/security/advisories/new)**
> (repository → *Security* tab → *Report a vulnerability*)

It is private, it threads, and it turns into a published advisory with credit when the fix
ships. If you cannot use it, email **ops@texnobus.uz** with `SECURITY` in the subject.

### What to include

A report we can act on in one pass:

- what an attacker gets — read another portal's calls, mint a session, recover a secret;
- the exact request or steps, including the endpoint (`/install/`, `/app/`, `/settings/`,
  `/events/`, `/api/v1/...`) and the commit you tested;
- a minimal proof of concept — a `curl`, a diff, a failing test;
- whether you needed an installed portal, an admin token, or nothing at all;
- your assessment of severity, and anything that limits exploitability.

**Never paste a live credential into a report.** Redact `access_token`, `refresh_token`,
`application_token`, `AUTH_ID`, `REFRESH_ID`, `SESSION_SECRET` and `TOKEN_ENC_KEYS` values.
Say that you have one and we will arrange a channel.

### What to expect from us

| Stage | Target |
| --- | --- |
| Acknowledgement that a human read it | 3 business days |
| Triage: accepted / needs-info / out of scope, with severity | 10 calendar days |
| Status update while it is open | every 14 days |
| Fix for a tenant-isolation or credential issue | as fast as we can cut it, aiming for 7 days |
| Fix for everything else | next release cycle |

Disclosure is coordinated. We publish a GitHub advisory once the fix is deployed, and we
credit you by the name or handle you choose unless you ask us not to. If a fix is taking
longer than 90 days from your report, you are free to publish; we would appreciate a
heads-up first.

This is a small project. These are honest targets, not a contractual SLA.

---

## Rules of engagement

This section matters more here than in a single-tenant app, because the production instance
holds other companies' telephony data.

**Test against your own copy.** The whole stack runs locally:

```bash
cp .env.example .env          # then fill in the marked values
docker compose up -d postgres
docker compose run --rm api python -m alembic upgrade head
docker compose up -d api worker web
```

`tools/README.md` explains how to seed a demo portal, so you never need real data to
reproduce anything.

**Do not:**

- test against `https://b24.texnobus.uz`, or any deployment you do not own;
- install or exercise the app on a Bitrix24 portal that is not yours;
- access, modify, exfiltrate or retain data belonging to anyone else — stop as soon as you
  have proof, and tell us what you touched;
- run volumetric, denial-of-service, brute-force or automated-scanner traffic against a live
  deployment;
- social-engineer Texnobus staff, our hosting providers, or Bitrix24 support;
- attack the underlying infrastructure (the host, Cloudflare, the Caddy instance in front of
  it) — that belongs to the providers, not to this project.

If you keep to this and report privately, we will treat your research as authorised, we will
not report you and we will not pursue you. We cannot waive anyone else's rights — notably
Bitrix24's or a portal owner's — which is exactly why staying inside your own copy is what
keeps that promise meaningful.

There is no bug bounty. We pay in credit and in a fast fix.

---

## In scope

Ranked by how seriously we take it.

1. **Tenant isolation bypass.** Any path that reads, writes or deletes rows for a portal
   other than the caller's. `calls`, `employees` and `crm_contexts` carry *forced* row-level
   security bound to a transaction-local setting, and the runtime role `ca_app` cannot bypass
   it (§3) — so this usually means a query that escapes `tenant_txn`, a job that forgets to
   re-issue `SET LOCAL app.portal_id`, or a read that skips `services/calls_repo.py`.
2. **Credential compromise.** Recovering an OAuth access/refresh token or an
   `application_token` from the database, the logs or a response; writing `access_token_enc`
   / `refresh_token_enc` without going through `store_portal_credential()` and its
   `user.admin` proof; recovering `TOKEN_ENC_KEYS`, `SESSION_SECRET` or `B24_CLIENT_SECRET`;
   making `client_endpoint` derive from the portal-supplied `DOMAIN` instead of an OAuth
   refresh response (§4.1).
3. **Session token forgery or escalation.** Minting or altering the HS256 session JWT;
   raising `acc` from `own` to `all`, or `adm` from false to true; attaching an `ent` claim
   for a CRM entity the viewer cannot see; replaying an expired token through
   `/api/v1/session/exchange`; abusing the five-minute playback token (§4.6, §4.7).
4. **Secret leakage into a log.** Anything the recursive redaction in
   `api/app/security/redact.py` misses, so that a token reaches stdout, a `rest_log` row or
   Caddy's access log (§6). `api/tests/test_secret_logging.py` is the regression test — a
   report that makes it fail is in scope automatically.
5. **Lifecycle-event authentication bypass.** Getting `POST /events/` to act on an
   `ONAPPUNINSTALL` or `ONAPPUPDATE` without a constant-time-equal `application_token`, or
   creating a portal row without the refresh exchange plus `user.admin` proof (§4.9).
6. **Frame-embedding escape.** Making the per-request
   `Content-Security-Policy: frame-ancestors` accept an origin that is not the validated
   `DOMAIN`, so the app renders inside an attacker's page (§4.10).
7. **The ordinary catalogue**, when you can show impact: SQL injection, SSRF (particularly
   anything that widens `OAUTH_HOST_ALLOWLIST` or reaches an internal address), RCE,
   deserialisation, path traversal, authentication bypass on `/api/v1/*`, stored or
   reflected XSS in the SPA or in the server-rendered `install.html` / `handoff.html` /
   `state.html`, and dependency vulnerabilities with a reachable path in this code.

Reports about the deployment materials — `docker/`, `docker-compose*.yml`,
`docker/Caddyfile.snippet`, `tools/make-env.sh` — are in scope when a documented setup,
followed as written, ends up insecure.

---

## Out of scope

- **Bitrix24 itself.** Its REST API, OAuth server, iframe host and permission model belong
  to Bitrix24; report those to them. We are in scope only where *our* handling of their data
  is wrong.
- **The absence of `X-Frame-Options`.** Deliberate and load-bearing: the app renders inside
  each portal's own iframe, `X-Frame-Options` has no origin granularity and would break every
  portal, so it is stripped at the proxy and `frame-ancestors` is the only control (§4.10).
  Re-adding it is a bug, not a fix.
- **"These endpoints have no authentication."** `/install/`, `/app/`, `/settings/` and
  `/events/` are POST targets for Bitrix24 and must accept anonymous requests. That is only a
  finding if you can make one of them return data, write a row or store a credential — which
  is items 1, 2 and 5 above.
- **Rate limiting and availability.** OAuth exchanges triggered by unauthenticated input are
  limited to 5 per 10 minutes per `member_id` and per source IP (§4.1); beyond that, request
  flooding, resource exhaustion and traffic amplification are not accepted.
- **Findings that presuppose a compromise already lost**: host root, a PostgreSQL superuser,
  read access to `.env`, or a stolen `TOKEN_ENC_KEYS`. Encryption at rest protects a database
  dump, not a live box.
- Missing security headers, TLS or cipher-suite preferences, and cookie flags (the app sets
  no cookies) with no demonstrated impact.
- Scanner or SAST output pasted without a proof of concept, version-banner fingerprinting,
  and best-practice advice unattached to an attack.
- Self-XSS, clickjacking on a page that changes no state, missing SPF/DMARC on domains we do
  not send mail from, and content spoofing in a field only the reporter sees.
- The developer tooling under `tools/` and the `test` / `dev` compose profiles: they are not
  built into the production image and not deployed.
- Anything only reproducible on a fork, a modified deployment, or an unsupported commit.

---

## If you find an exposed credential

Report it before you do anything else, and do not use it. If it is ours, this is how it gets
rotated:

| Secret | Rotation |
| --- | --- |
| `B24_CLIENT_SECRET` | Bitrix24 vendor cabinet, then redeploy |
| `TOKEN_ENC_KEYS` | add a new key id to the ring, point `TOKEN_ENC_ACTIVE_KEY_ID` at it, keep the old id until re-encryption completes |
| `SESSION_SECRET` | replace and redeploy; every live session JWT is invalidated and the SPA re-handshakes |
| Portal OAuth tokens | an admin re-authorises from the settings page; `token_version` increments and in-flight work is fenced |
| `application_token` | replaced on the portal's next `ONAPPUPDATE`, or by a reinstall |

`.env` is git-ignored and no secret is committed. If you believe one reached the repository
history, treat it as live and report it — a rotation is cheap and a public token is not.

---

## What the project already does

So you know what is covered before you spend time on it:

- **Structural tenant isolation**, not conventional: forced RLS on the tenant tables, a
  runtime role without `BYPASSRLS`, and a CI step that asserts all three properties against
  the database the migrations actually produced — a migration that drops one fails the build.
- **A credential invariant enforced by a lint test**: `api/tests/test_registry_lint.py` fails
  the build if any module other than `services/portals.py::store_portal_credential()` writes
  the token columns.
- **Recursive log redaction**, with `api/tests/test_secret_logging.py` running a mocked
  refresh and scanning captured output for every secret as a substring; `httpx` and
  `httpcore` loggers are pinned to `WARNING` so their request URLs never reach stdout.
- **Envelope encryption at rest** for OAuth and application tokens: a key-ringed AES-256-GCM
  envelope in `api/app/security/crypto.py`.
- **CI on every pull request** ([.github/workflows/ci.yml](.github/workflows/ci.yml)): it
  builds the API image, applies the migrations, asserts the RLS invariants against the
  database those migrations actually produced, runs the migrations down to base and back
  up so the rollback path is exercised rather than assumed, runs the suite against a real
  PostgreSQL 16, lints and type-checks both halves, and fails on a high-severity `npm
  audit` finding. It runs with a read-only `GITHUB_TOKEN`.
- **A pull request cannot introduce a known-vulnerable dependency**: `dependency-review`
  fails it at the diff, before it is merged and before Dependabot would have noticed.
- **CodeQL** ([.github/workflows/codeql.yml](.github/workflows/codeql.yml)) over Python,
  TypeScript and the workflows themselves, with the `security-extended` query pack, on a
  schedule as well as on changes — a query published next month should find a bug written
  last month.
- **Every published image is scanned** with Trivy before it can be deployed, and the
  findings are reported to code scanning rather than to a log nobody reads.
- **Secret scanning with push protection is on**, so a token in a commit is rejected at
  `git push` rather than discovered afterwards.
- **Every action is pinned to a commit sha**, not to a tag. A tag is mutable and an action
  is code that runs with this repository checked out.
- **A log trail designed to be safe**: request query strings and `Location` response headers
  are dropped from the proxy's access log, the session JWT travels only in a URL fragment,
  and `/api/v1/session/exchange` and `/api/v1/portal/reauthorize` bodies are excluded from
  all exception logging.

---

## How code reaches production

Worth knowing, because "can you make the deployment do something it should not" is a real
question about a real attack surface, and the answer is bounded deliberately.

- **`main` is the only branch that deploys, and nothing pushes to it directly.** It is a
  protected branch: pull request required, every CI check green, conversations resolved,
  no force push, no deletion. Promotions are merged rather than squashed or rebased, so
  every deployed sha remains a commit you can check out and read.
- **Images are built on the GitHub runner, never on the host.** The production box is
  shared with five unrelated projects; a build there costs both its cores. The host only
  pulls a tag.
- **A deployment waits for a human.** The `production` environment carries a required
  reviewer and is restricted to the `main` branch, so publishing an image and putting it
  in front of live portals are two decisions, not one.
- **The deploy key has no shell.** It is pinned with `command=` to a script installed
  outside the repository checkout, and accepts exactly `deploy <40-hex-sha>`, `rollback`
  and `status`. Anything else exits 64. A stolen key can move production between two
  commits that already exist in this repository ~ which is bad ~ but cannot read `.env`,
  cannot reach PostgreSQL and cannot touch a neighbouring project.
- **The four deployment secrets are environment secrets**, not repository secrets: a
  workflow that does not use the `production` environment cannot read them, and the
  environment cannot be used from a branch other than `main`.

If you find a way around any of those, it is in scope under item 2 (credential
compromise) or item 7, and we would very much like to hear about it.

## Operators running their own copy

[docs/deployment.md](docs/deployment.md) is the reference. The parts that are security
decisions rather than preferences:

- Generate `TOKEN_ENC_KEYS` and `SESSION_SECRET` as fresh random values per deployment;
  `config.py` validates their shape at import and `.env.example` documents the format.
- Publish no container ports. Ingress is a reverse proxy in front of `api:8000` and
  `web:3000`; PostgreSQL is never exposed.
- Keep the access-log filter from `docker/Caddyfile.snippet` intact. Its two deletions are
  what stop session tokens and Bitrix24's `AUTH_ID` from being written to disk.
- Leave `REST_LOG_RETENTION_DAYS` at or above the default; `config.py` refuses values below
  3, and the daily purge is what keeps redacted request bodies from accumulating.
- Back up the database *and* the key ring, separately. A dump without `TOKEN_ENC_KEYS`
  restores nothing usable — which is the point — and losing the ring is unrecoverable.
