# Contributing

Thank you for looking. This is a small project with one production instance serving real
companies' telephony data, and that fact shapes most of what follows.

**If you have found a security problem, stop and read [SECURITY.md](SECURITY.md).** Do not
open an issue, a pull request or a discussion for it.

## Where to put things

| You have | Put it in |
| --- | --- |
| A question | [Discussions — Q&A](https://github.com/blogchik/bitrix24-widget-callanalytics/discussions/categories/q-a) |
| An idea, or a "why does it work like that" | [Discussions — Ideas](https://github.com/blogchik/bitrix24-widget-callanalytics/discussions/categories/ideas) |
| A reproducible bug | An issue |
| A vulnerability | [Private advisory](https://github.com/blogchik/bitrix24-widget-callanalytics/security/advisories/new), never a public thread |
| Code | A pull request against `dev` |

## The branches

`dev` is the default branch and where all work happens. `main` is the deployment branch:
merging into it publishes container images and offers them to the production host, so it
is protected — pull request required, linear history, every CI check green.

Branch from `dev`, open the pull request against `dev`. Promoting `dev` to `main` is a
separate, deliberate pull request that a maintainer opens when a batch is ready to ship.

## Running it

```bash
cp .env.example .env          # or ./tools/make-env.sh
docker compose up -d postgres
docker compose run --rm api python -m alembic upgrade head
docker compose up -d api worker web
```

Opening `http://127.0.0.1:3000/` renders "open this app from Bitrix24", which is correct
rather than broken — the app is only reachable through Bitrix24's POST handshake.
[`tools/README.md`](tools/README.md) explains how to seed a demo portal so you can see a
dashboard with data in it, and how to drive it with the UI audit harness.

## What CI will run, and how to run it first

```bash
docker compose --profile test run --rm test          # 403 tests, real PostgreSQL 16
docker compose run --rm api sh -c "ruff check . && mypy app"
cd web && npx tsc --noEmit && npm run build && npm audit --audit-level=high
node tools/check-i18n.mjs                            # catalogues agree
./tools/check-compose.sh                             # production render is still sane
```

For anything visual, also run the audit at four viewport widths —
[`tools/README.md`](tools/README.md) has the invocation. Zero overflow, zero clipped text
and zero targets under the floor is the standard, and it is measured rather than judged.

## What makes a change easy to accept

- **A reason, not just a diff.** The comments in this codebase say *why* far more often
  than *what*, because the *what* is already on the next line. Match that.
- **Say what you verified.** "Tests pass" is CI's job. "I opened it in a real portal on a
  phone" is not, and it is what the pull request template asks for.
- **Do not widen the surface without saying so.** A new REST scope changes the Marketplace
  listing and forces every installed portal to re-consent. More requests per sync visit
  eat into a documented budget. A new dependency is a new thing to trust.
- **Tenant isolation is not a convention here, it is a mechanism.** If your change touches
  `tenant_txn`, `scope_filter`, RLS or the token columns, expect the review to be slow and
  detailed. `docs/architecture.md` §3 and §4.7 are the ground truth.
- **Keep the two message catalogues in step.** `web/messages/ru.json` and `en.json` must
  carry the same keys and the same ICU placeholders; CI fails otherwise.

## What is likely to be declined

- A framework major upgrade bundled with anything else.
- A new chart colour. `web/src/lib/viz.ts` holds a palette validated against colour-vision
  deficiency; the hexes are specification values, not preferences.
- Re-adding `X-Frame-Options`. It has no origin granularity and would break every portal;
  `Content-Security-Policy: frame-ancestors` is the control, per request.
- Anything that makes the app write to Bitrix24 without a clear reason. It reads
  `voximplant.statistic.get` and presents it, and that boundary is most of its safety
  argument.
