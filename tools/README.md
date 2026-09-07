# Developer tools

Not part of the running app. Neither script is imported by anything in `api/` or `web/`,
and `playwright` is deliberately **not** a dependency of `web/package.json` so it never
lands in the production image.

## `seed-demo-portal.py`

Fills a local database with one portal and 900 calls across three employees so the
dashboard has something to draw. Run it against the compose stack:

```bash
docker compose up -d api
docker compose exec -T api python - < tools/seed-demo-portal.py
```

It prints a `PORTAL_ID` and a one-hour session token. Note that the seeded portal has no
credential, so the worker will park it as `reauth_required` on its next tick and the
dashboard will show the "authorisation expired" banner — that is the design working, not
a fault in the seed.

## `screenshot-dashboard.mjs`

Loads a page the way Bitrix24 does — session token in the URL fragment,
`DOMAIN`/`PROTOCOL`/`LANG`/`APP_SID` in the query — and reports the visible text, the
number of chart marks and table rows, and every console error, then writes a full-page
screenshot. This is how to check a chart actually renders: a palette validator checks
colour, not layout, so collisions and overflow are only found by looking.

```bash
cd web
npm i -D playwright && npx playwright install chromium   # once, not committed
node ../tools/screenshot-dashboard.mjs "<TOKEN>" ../dashboard.png /dashboard ru
```

Two console errors are expected outside a real Bitrix24 iframe: the BX24 JS SDK is
fetched from `api.bitrix24.com` and has no parent frame to talk to.
