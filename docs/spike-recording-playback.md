# Recording playback spike

**Status: run 2026-09-08 against a real portal. Partially answered.**
The REST and network half is settled and recorded under [Results](#results). Two questions
remain open: playback from inside the Bitrix24 iframe in a real browser, and the behaviour of
a portal using **built-in** Bitrix24 telephony, for which this portal supplied no data.
`RECORDING_MODE` stays `off` until the browser half is walked.

This is the protocol of `docs/architecture.md` §9, turned into a runnable checklist.
Fill in the Results section as you go and record raw output, not conclusions.

## Why this cannot be answered from the desk

The app is served from `b24.texnobus.uz` while recordings live on the customer's
`*.bitrix24.*` domain. Whether a browser will play that cross-origin audio inside a
third-party iframe depends on the response headers Bitrix24 actually sends, on whether
`CALL_RECORD_URL` carries a credential, and on how each browser's third-party cookie
rules treat the portal session. None of that is documented, and all of it decides
whether the app needs the `disk` scope — a listing change and a re-consent for every
installed portal, which is the owner's decision, not a coding one.

## Prerequisites

- A Bitrix24 portal in the kz region with the app installed and at least a few dozen
  calls **with recordings**, synced (`backfill_status = 'done'`).
- Two Bitrix24 accounts on that portal: one administrator, and one ordinary employee
  who has "Call statistics — view" but **not** "Call recording — listen". The second
  account is the whole point of step 3b.
- Chrome, Firefox and Safari. Safari's ITP is the most likely thing to break option 1.

## Step 1 — what is actually in the URL

Run before anything else: it can settle the question on its own.

```sql
-- distribution of what we cached, per integration
SELECT rest_app_id,
       rest_app_name,
       count(*)                                                   AS calls,
       count(*) FILTER (WHERE record_file_id IS NOT NULL)         AS with_file_id,
       count(*) FILTER (WHERE coalesce(call_record_url,'') <> '') AS with_url,
       count(*) FILTER (WHERE has_record)                         AS has_record
  FROM calls
 WHERE portal_id = :pid
 GROUP BY rest_app_id, rest_app_name
 ORDER BY calls DESC;
```

Then capture three URLs **verbatim, before the parser strips them**. The parser removes
`auth`, `token`, `sig` and `access_token` query parameters on the way into the database
(§5.5), so the cached value cannot answer this question. Read them straight from
Bitrix24 instead:

```bash
curl -sS "$CLIENT_ENDPOINT/voximplant.statistic.get" \
     -d "auth=$PORTAL_ACCESS_TOKEN" \
     -d 'FILTER[>ID]=0' -d 'SORT=ID' -d 'ORDER=DESC' -d 'start=0' \
  | python -m json.tool | grep -i record
```

Record for each: host, path, and every query parameter name (values redacted).

**This is the decision point.** If the URL is of the `download.json?auth=<token>` family
it carries the portal access token. In that case `RECORDING_MODE=redirect` is forbidden
outright — it would hand a portal-wide credential to every viewer — and `call_record_url`
is treated as a secret everywhere: never sent to the browser, stripped on parse, redacted
in logs. The app already behaves this way; the spike only confirms it must stay that way.

## Step 2 — option 1: point `<audio>` straight at the URL

A throwaway page served from `b24.texnobus.uz`, opened **inside** the Bitrix24 iframe:

```html
<audio src="<CALL_RECORD_URL>" controls preload="metadata"></audio>
```

Test each cell and write down the exact failure, not "did not work":

| Case | Chrome | Firefox | Safari |
|---|---|---|---|
| Fresh portal session, admin | | | |
| Same URL one hour later | | | |
| After a portal token refresh | | | |
| Incognito / no Bitrix24 session | | | |
| Employee without "listen to recordings" | | | |

Also, from a shell:

```bash
curl -I "<CALL_RECORD_URL>"
curl -I -H 'Range: bytes=0-1023' "<CALL_RECORD_URL>"
```

Record: status, `Accept-Ranges`, `Content-Type`, `Content-Length`, any redirect target,
and the CORS headers. An option-1 pass requires playback **and** seeking **and** the
no-permission employee being refused by Bitrix24 rather than served.

## Step 3 — option 2: the backend proxy

Set `RECORDING_MODE=proxy` on a staging deployment. The endpoint already exists
(`GET /api/v1/calls/{id}/record?t=…`, minted by `POST /api/v1/calls/{id}/play-url`),
streams with httpx, forwards `Range` / `Content-Range` / `Accept-Ranges`, and writes
nothing to disk.

**3a — admin viewer.** Play, seek to the middle, seek backwards. Measure time to first
byte and watch `docker stats` on the api container while streaming a ten-minute file.
Memory must stay flat: a rise means the stream is being buffered rather than piped.

**3b — employee without "listen to recordings".** This is the case that decides whether
the proxy is safe to ship. For a non-admin viewer the proxy uses **the viewer's own
token**, so Bitrix24 itself should refuse. If it instead serves the audio, the proxy
would let any employee listen to every recording in the portal, and option 2 fails on
permissions regardless of how well it streams.

**3c — stale link.** Delete a recording in Bitrix24, then play that row. Expect 403/404
from the source, the SPA calling `POST /calls/{id}/refresh` once, and the row being
re-read on the next sync visit (§5.7).

**3d — cost.** Count the requests a playback session adds against the 2 requests per
second budget of §5.6. A player that re-requests on every seek could starve the sync.

## Step 4 — option 3: confirm the scope wall

One call, only to prove the alternative really does need the scope we did not request:

```bash
curl -sS "$CLIENT_ENDPOINT/disk.file.get" \
     -d "auth=$PORTAL_ACCESS_TOKEN" -d "id=$RECORD_FILE_ID"
```

Expect `insufficient_scope`. Record the exact error.

## Step 5 — the decision

| Outcome | `RECORDING_MODE` | Consequence |
|---|---|---|
| Option 1 plays, seeks, and refuses the unpermitted employee | `off` + direct `<audio>` | No proxy, no bandwidth cost, no scope change |
| Option 1 fails, option 2 passes 3a-3d | `proxy` | Our bandwidth; permissions enforced by Bitrix24 via the viewer's token |
| Step 1 shows the URL carries no credential **and** Bitrix24 enforces the listener's rights server-side | `redirect` allowed | Otherwise forbidden |
| Only `disk.file.get` works | **stop** | Adding the `disk` scope is a product decision: it changes the Marketplace listing and forces re-consent on every installed portal |

## Results

Run 2026-09-08 on the first real portal (`member_id` 9aa34dc6…, licence `kz_pro100`), from the
production deployment, using the app's own admin-proven token. 7 275 calls cached, 3 209 with a
recording.

### The finding that changes the design

**The recordings are not on a `*.bitrix24.*` host at all.** Every one of the 3 209 lives on the
telephony provider:

| Line | Calls | With a recording | Recording host |
| --- | --- | --- | --- |
| Телефония Сипуни (`rest_app`) | 7 266 | 3 209 | `sipuni.com` |
| Built-in Bitrix24 telephony | 9 | 0 | — |

The URL is `https://sipuni.com/api/crm/record?id=<17>&hash=<32>&user=<6>`. §9 was written around
the assumption that the audio sits on the customer's own Bitrix24 domain and that the viewer's
portal session is what unlocks it. For this portal, and for any portal running an external
provider, that assumption is simply wrong — and `REST_APP_ID` already tells us which case we are in.

### Step 1 — what is in the URL

No Bitrix24 credential. The parameters are `id`, `hash` and `user`; nothing matching
`auth|token|sig|key|secret`. So the specific danger §9 guarded against — handing a portal access
token to every viewer — does not arise for this provider.

What is there instead is a **32-character `hash` that is the only gate**. A request with the hash
mutated returns 500; a request with the correct hash returns the audio to anyone, from anywhere,
with no session and no cookie. The URL is a bearer capability with no expiry we can observe.

Two consequences follow, and both matter more than the original worry:

1. **Bitrix24's "Call recording: listen" permission is not enforced on this path at all.**
   Sipuni has never heard of it. §4.7 and §9 both assume that using the viewer's own token makes
   Bitrix24 enforce that right; against an external provider there is no Bitrix24 request in the
   chain to enforce anything. Whatever we ship, *our* `scope_filter` is the only access control.
2. **The stored `call_record_url` is a live credential.** The parser strips `auth`, `token`, `sig`
   and `access_token`; `hash` is not in that list, and must not be, because stripping it would
   make the URL useless for the proxy. So `calls.call_record_url` is a table of permanent
   listen-links. The design already forbids returning it to the browser and redacts it in logs;
   that rule is now load-bearing rather than precautionary.

### Step 2 — can a browser play it directly

Probed from the production server, which has no relationship to the portal and carried no
Bitrix24 session:

```
HEAD                  -> 200
  content-type:  audio/mpeg
  content-length: 504000
  accept-ranges: bytes
  cache-control: no-cache
GET Range: bytes=0-1023 -> 206
  content-range: bytes 0-1023/504000
HEAD with a mutated hash -> 500
```

Correct media type, byte ranges honoured, so seeking works. No `Access-Control-Allow-Origin`
header, which does not block a plain `<audio src>` (media elements load cross-origin without CORS
unless the page needs to read the samples). **Technically option 1 would play.**

### Step 4 — the scope wall

`disk.file.get` with the portal token returns `insufficient_scope`, as expected without the
`disk` scope. Confirmed, and moot for this provider: the audio never touches Bitrix24 Disk.

### Decision

**`RECORDING_MODE=proxy`** for external-provider portals, and the reason is not capability but
containment.

Option 1 works technically and costs us no bandwidth, but it puts a permanent, unauthenticated
listen-link into the browser, where it survives in devtools, history, a copied link and a shared
screenshot. Since the provider enforces nothing, that link is the whole security boundary for a
customer's recorded phone calls. The proxy keeps it server-side and makes our own `scope_filter`
the gate — which, per the finding above, is the only gate that exists on this path anyway.

`redirect` is refused for the same reason, notwithstanding that the URL carries no Bitrix24 token:
redirecting hands the browser the same permanent capability.

### Still open

1. **Playback inside the Bitrix24 iframe, in a real browser.** Everything above was measured with
   an HTTP client. Chrome, Firefox and Safari each need one pass through the real portal, and
   Safari's third-party rules are the likely source of any surprise. This is the reason
   `RECORDING_MODE` stays `off`.
2. **Built-in Bitrix24 telephony.** This portal's nine built-in calls have no recordings, so the
   original §9 question — is the Bitrix24 record URL credential-bearing, and does it need a portal
   session — is still unanswered. A portal on built-in telephony has to answer it.
3. **Whether `RECORDING_MODE` should be per-provider rather than per-deployment.** `REST_APP_ID` is
   already stored per call, so the app can tell the two cases apart at playback time. If the
   built-in case turns out to need a different mechanism, one global switch will not do. That is a
   design change, not a configuration change, and should be decided once question 2 is answered.
