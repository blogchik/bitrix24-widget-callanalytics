# Moderation checklist

The §4.11 table of `docs/architecture.md`, turned into a manual test script. Walk it on a
real portal before submitting to Bitrix24, and attach screenshots of each frame.

Every row must end in a rendered, translated page. A blank frame, a raw traceback or an
untranslated string is a moderation rejection, so "nothing happened" is a failure even
when no error was logged.

| # | Action | Entry point | Expected frame | Pass |
|---|---|---|---|---|
| 1 | Install on a clean portal as administrator | `POST /install/` | "Installing…" then Bitrix24 closes the slider; the app appears in the left menu and on the four CRM tabs | ☐ |
| 2 | Open the left-menu item | `POST /app/` `DEFAULT` | Dashboard. While the backfill runs: "Importing history N / M" with a progress bar. With no calls in the period: an explicit empty state, not a blank panel | ☐ |
| 3 | Open a Deal card → the app tab | `CRM_DEAL_DETAIL_TAB` | The deal's calls. Matched through its contacts and company and its call activities, since the statistics API has no DEAL entity type | ☐ |
| 4 | Open a Lead, a Contact and a Company card | the other three tabs | That entity's calls | ☐ |
| 5 | Open as an employee **without** "Call statistics — view" | `POST /app/` | The mandated text: "Your account does not have access to call statistics. Ask your Bitrix24 administrator to grant the 'Call statistics — view' permission." | ☐ |
| 6 | Open as an employee **with** own-calls rights | `POST /app/` | Dashboard filtered to their own calls, with a banner saying so and the employee filter hidden | ☐ |
| 7 | Switch the portal language to English and reopen | any | Every string in English, including the state pages | ☐ |
| 8 | Open `/settings/` as administrator | `POST /settings/` | Sync status, token owner and age, placement bind results, capabilities, last error | ☐ |
| 9 | Open `/settings/` as an employee | `POST /settings/` | "Administrators only", HTTP 200 | ☐ |
| 10 | Uninstall the app | `ONAPPUNINSTALL` | Server-to-server 200; the portal's cached rows are deleted by the worker within a few ticks | ☐ |
| 11 | Reinstall | `POST /install/` | Same as row 1; placements re-bound; history re-imported | ☐ |
| 12 | Install on an on-premise portal | any | Identical UI. REST goes to the portal's own `client_endpoint`; `PROTOCOL=0` is accepted. If the build lacks `voximplant.statistic.get`, the explicit "method missing" state appears instead of a broken widget | ☐ |
| 13 | Open with no portal row (simulate a restored database) | `POST /app/` | Administrator: self-heals and lands on the dashboard. Employee: "not installed yet" | ☐ |
| 14 | POST garbage to `/install/`, `/app/`, `/settings/`, `/events/` | any | A translated "bad request" page with a request id, HTTP 400. Never a traceback | ☐ |
| 14a | Open `https://b24.texnobus.uz/settings` in a plain browser tab | GET | The settings page from the SPA, not JSON. A bare `GET` on a handler path (`/settings/`, `/app/`, `/install/`, `/events/`) instead renders the translated state page, never `{"detail":...}` | ☐ |
| 15 | Resize the browser and switch CRM tabs | any | The frame resizes to its content; no inner scrollbar and no clipped table | ☐ |
| 16 | Play a recording | dashboard or CRM tab | Until the §9 spike is answered, a "has recording" icon and an "open in Bitrix24" link — deliberate, not broken | ☐ |

## Compliance evidence to attach

- **TLS**: `https://b24.texnobus.uz` with a valid certificate; HTTP redirects to HTTPS; HSTS present.
- **REST logs kept ≥ 3 days**: `SELECT min(ts), max(ts), count(*) FROM rest_log;` — retention defaults to 7 days and the configuration refuses anything below 3.
- **No hardcoded cloud domain**: every REST base comes from `client_endpoint`, taken only from an OAuth response. `grep -rn "bitrix24.ru\|bitrix24.com" api/app/` returns nothing that builds a request URL.
- **Batching**: the install proof and the placement binds are one batch each; the sync fetches up to 20 pages per batch.
- **Input validation**: `api/app/bitrix/forms.py` allowlists every field of every Bitrix24-facing endpoint.

## Known v1 simplifications to declare

- Bitrix24's "Call statistics" permission has four levels (own / department / any / none). The app maps them to administrator → all calls, everyone else → their own calls, no permission → the explanatory state. A department-level manager therefore sees only their own calls in v1.
- Fully isolated on-premise portals that cannot reach `oauth.bitrix.info` are not supported: they get an explicit state page and nothing is stored.

### UTM analytics (§4.13)

| Step | Expected |
|---|---|
| Open "Traffic sources" from the page nav | The report renders; no blank frame, no untranslated key |
| Change a UTM filter | The numbers change immediately and the network tab shows NO request |
| Change the period | One request; the previous report stays on screen while it runs |
| Pick a period of 93 days in the calendar | The calendar does not offer it (the cap is geometry, not a 400) |
| Open on a portal with leads turned off | A deals-only report plus one sentence saying so — never `0 → 44` and `—%` |
| Open on a portal whose links carry no tags | Every row under "No tag", plus the sentence explaining the two possible causes |
| Open as a non-admin with own-calls rights | Only that user's own leads and deals, and no employee control |
| Read the conversion column on a tag with more deals than leads | A value above 100 %, with the note above the table explaining it |
| Narrow the browser to 375 px | No horizontal page scroll; the table and the matrix scroll inside their own regions and say so |

### CRM mirror (decisions 26–30, §4.14, §5.10–§5.13)

Walk these on a portal that has been switched to the mirror (`crm_mode = mirror`).

| Step | Expected |
|---|---|
| Install on a clean portal, then call `event.get` | The CRM offline bindings (or, on plans without them, the online `/events/crm/` bindings) and `ONOFFLINEEVENT` are present |
| Publish a new app version, apply it, call `event.get` again | The same bindings are present again after the update |
| Open "Deals" or "Sources" right after install | A "history is still loading" notice with a percentage, never a silently short report |
| Change a deal's stage in Bitrix24 | The report shows the new stage within 5 minutes |
| Delete a deal in Bitrix24 | It disappears from the reports within 5 minutes (offline-events plan) |
| Choose a period of 366 days | The report renders; no 92-day cap and no REST request per period change |
| Open as a non-administrator whose CRM role hides funnel X | Funnel X appears nowhere: not on "Deals", not in the deals leg of "Sources", not in transitions |
| Open as a non-administrator with no right to view leads | The leads leg is unavailable, as it was with the live read |
| POST a forged lead, contact or company tab id the user cannot read | "No access to this CRM item", no entity session |
| As an administrator, open settings and press "Disable CRM analytics" | CRM sync stops, the portal's CRM rows are deleted, and the Deals and Sources pages show the "turned off" state |
| Uninstall the app | Every customer table is empty for the portal, the CRM ones included |

### Known simplifications of the CRM mirror to declare

- Visibility is administrator → all records; everyone else → records assigned to them, inside the funnels Bitrix24 shows them, and leads only when Bitrix24 lets them view leads. A department head or a user with broader CRM rights sees only their own records.
- Amounts are shown in each deal's own currency; Bitrix24 does not return the account-currency amount to list calls.
- A record restored from the Bitrix24 recycle bin gets a new id, so it arrives as a new record without the history it had before deletion.
