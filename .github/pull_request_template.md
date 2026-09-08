<!--
Base branch: `dev` for everything.

`main` is the deployment branch: merging into it publishes images and offers them to the
production host, so a PR targeting `main` should be a promotion of `dev` and nothing else.
-->

## What this changes

<!-- One paragraph. What behaviour is different afterwards, and for whom. -->

## Why

<!-- The problem, not the patch. If it fixes something that was measured, give the number. -->

## How it was verified

<!-- Delete what does not apply. CI runs the first four; the rest only a human can do. -->

- [ ] `docker compose --profile test run --rm test` — API suite
- [ ] `npx tsc --noEmit` and `npm run build` in `web/`
- [ ] `./tools/check-i18n.mjs` and `./tools/check-compose.sh`
- [ ] `ruff check . && mypy app`
- [ ] UI audit at 375 / 768 / 1024 / 1440 (`tools/ui-audit.mjs`) — for anything visual
- [ ] Looked at it in a real Bitrix24 iframe — for anything the moderator will see

## Risk

<!--
Say the true thing here, including "none". The ones worth naming:

- a migration (is `downgrade()` real? does old code tolerate the new schema?)
- a change to tenant isolation, RLS, `tenant_txn`, or `scope_filter`
- anything touching tokens, `redact.py`, or what reaches a log
- a new scope, a new REST method, or more requests per sync visit
- a change to `docker-compose*.yml`, the Caddyfiles, or the deploy path
-->

## Rollback

<!--
For a PR that will be promoted to `main`: how do we undo this if it is wrong in production?
"Images are tagged; `rollback` on the host" is the usual and correct answer. If a migration
makes that untrue, say so here — that is exactly the case where nobody wants to work it out
during the incident.
-->
