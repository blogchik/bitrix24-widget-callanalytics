# Developer tools

Not part of the running app. None of these scripts is imported by anything in `api/` or
`web/`, and `playwright` is deliberately **not** a dependency of `web/package.json` so it
never lands in the production image.

## `seed-demo-portal.py`

Fills a local database with one portal and 900 calls across three employees so the
dashboard has something to draw. Run it against the compose stack:

```bash
docker compose up -d api
docker compose exec -T api python - < tools/seed-demo-portal.py
```

It prints a `PORTAL_ID` and two one-hour session tokens:

| Output | Opens | Why it exists |
| --- | --- | --- |
| `TOKEN` | `/dashboard`, `/settings` | The left-menu placement. |
| `CRM_TOKEN` | `/crm` | The CRM tab reads its entity from the JWT `ent` claim, never from the query string (§4.4 step 5), so the left-menu token cannot open it. |

Roughly two thirds of the calls are attached to one of four fixed CRM cards, and about a
fifth carry a recording. Both are there to be *rendered*, not merely stored: the call
table hides its CRM column and its recording column whenever every loaded row is empty for
them, so a seed without those rows can only ever exercise the hidden half of that rule —
which is exactly how two undersized touch targets survived a full audit pass.

The recording URLs point at `example.invalid`. `RECORDING_MODE` is `off` in every
environment this script runs in, so nothing fetches them, and they must not resemble a real
provider link.

Note that the seeded portal has no credential, so the worker will park it as
`reauth_required` on its next tick and the dashboard will show the "authorisation expired"
banner — that is the design working, not a fault in the seed.

Each run **deletes and recreates** the portal, so every token minted by an earlier run
stops working. If a token has simply expired and the data is still wanted, mint a new one
rather than re-seeding.

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

## `ui-audit.mjs`

The same page-loading trick as the screenshot script, at four viewport widths, reporting
what a screenshot cannot show:

- horizontal overflow of the document and of every element — skipping anything a
  deliberate horizontal scroller holds, because that content is reachable
- text clipped by its own box, and boxes shorter than their content
- interactive targets under the floor, which **follows the pointer**: 375 and 768 run with
  `hasTouch`, so `(pointer: coarse)` matches and the floor is 44px; 1024 and 1440 run with
  a mouse and a 30px floor. A 32px control with clear separation is correct on a desktop
  and only looks like a defect to a harness that cannot tell a finger from a cursor.
- console errors and failed requests, excluding the aborts React StrictMode's double
  effect produces under `next dev`

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml                -f docker-compose.tunnel.yml -f docker-compose.audit.yml up -d caddy api web
docker compose exec -T api python - < tools/seed-demo-portal.py   # TOKEN, CRM_TOKEN

cd web
npm i -D playwright && npx playwright install chromium   # once, not committed
cp ../tools/ui-audit.mjs _audit.mjs                      # playwright resolves from web/
node _audit.mjs "<TOKEN>" <outDir> /dashboard ru
rm _audit.mjs
```

`docker-compose.audit.yml` is what makes the result trustworthy, and it does two things.
It publishes **our** Caddy on `127.0.0.1:8080`, so the SPA and the API answer on one origin
exactly as they do in production — running `next dev` on `:3000` beside the API on `:8000`
is two origins, and rewriting requests in the harness to paper over that produces findings
the real app does not have. And it runs `web` from the `build` stage with the source
bind-mounted, because the production `runner` stage bakes the source into the image: an
audit against it silently measures the *previous* version of the app, however many times
you edit a component.

One `pageerror: Unable to initialize Bitrix24 JS library!` is expected on every run — the
BX24 SDK is fetched from `api.bitrix24.com` and has no parent frame to talk to.
