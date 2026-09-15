# Spike: CRM mirror — what a real portal answers

Live-portal measurements for the CRM mirror (the Postgres copy of deals, leads and stage
history that replaces request-time CRM reads). Each block below is one spike from the plan;
every "Implication" line names the design decision the measurement settles.

Portals are anonymised. Record ids are replaced by scale. No field value was printed or
stored while measuring: the probes report key presence, row counts, ordering, error
classes and `time` blocks only.

## S-A — extraction (2026-09-15)

**Portal A:** Bitrix24 cloud, `.kz` zone, Professional licence. About 4.6k deals (ids up to
~12k), leads with ids up to ~10k, about 30k deal stage-history rows.

**Method:** one read-only script run inside the production api container, using the
installer credential through `oauth.with_portal_token` and `BitrixClient.batch`. It sent 4
batches of 6-9 commands, paced 1.5 s apart. Every list call was `start:-1` or a bounded id
range.

**Budget:** `crm.item.list` operating time went from ~0.2 s to ~4.8 s in its 10-minute
window; the configured limit is 480 s. The legacy `crm.deal.list`/`crm.lead.list` and
`crm.stagehistory.list` reported `operating: 0` throughout.

| # | Question | Answer on Portal A | Implication |
|---|---|---|---|
| S-A.1 | `crm.item.list` keyset: `order {id:ASC}`, `filter {">id": n}`, `start:-1` | Works for deals and leads. 50 rows, strictly ascending. Page 1 starts after page 0's last id with no overlap. `order {id:DESC}, start:-1` returns the newest 50. With `start:-1` both `total` and `next` are **`null`** (not `0`). | The keyset is the extraction path. Never read `total`/`next` from a `start:-1` answer. The high id comes from one DESC page. |
| S-A.2 | Legacy `crm.deal.list` / `crm.lead.list` keyset with UPPERCASE keys | Works. The result is a bare list, `total: 0`. | The legacy dialect can mirror; `legacy_unsupported` is not needed on this build. |
| S-A.3 | `>=updatedTime` filter; the three-leg OR; `order {updatedTime, id}` | `>=updatedTime` is honoured on deals and leads: every row is at or after the bound, and a year-2999 bound returns 0 rows. The three-leg OR (`createdTime` ∨ `updatedTime` ∨ `closed=Y ∧ movedTime`) is honoured: every row matches a leg. `order {updatedTime:ASC, id:ASC}` is accepted and sorted. Legacy `>=DATE_MODIFY` is honoured. | The sweep and the window bootstrap can filter on `updatedTime` as planned. An `(updatedTime, id)` keyset is available as well as the id keyset. |
| S-A.4 | `@id` with 50 ids | Returns all 50. | The dirty refresh reads 50 ids per command. |
| S-A.5 | Does the plan's select allowlist come back? | **Deals:** every allowlisted key is carried, **except `opportunityAccount` and `accountCurrencyId`, which are never returned**. `contactIds` is a list, `closed` is the string `Y`/`N`, `categoryId`/`assignedById`/`leadId`/`companyId`/`opportunity` are integers. The UTM keys are carried; on this portal they are empty. **Leads:** every allowlisted key is carried, `contactId` and `contactIds` both, and `movedTime` too. | Store native `opportunity` + `currencyId` only; `/utm`'s "account currency" branch never fires from the mirror. `contactIds` makes the per-deal `crm.deal.contact.items.get` unnecessary. A UTM key's presence proves `select` returns UTM, which settles the open question in research block (h). |
| S-A.6 | Operating time per command | `time.operating` is the **running accumulator for the method** in its window, not the cost of the command: it rises through a batch and repeats when a command is served from cache. The per-command delta is ~0.2–0.4 s for a 50-row page, id-only or full select alike; leads cost a little more than deals. A 9-command batch took ~2 s wall time. | Budget tracking reads the accumulator as the budget state directly. Cost per page is the delta between consecutive commands of the same method. At ~0.3 s a page, a 100k-deal/300k-lead portal backfills in roughly 40 min of operating time. |
| S-A.7 | Not-found vs access-denied | `crm.item.get` on a missing id → error code **`NOT_FOUND`** ("Элемент не найден"). Legacy `crm.deal.get` → **no error code**, description "Not found". Access-denied was **not measured**: it needs a non-admin token. | `errors.py` needs a `NotFound` class for `NOT_FOUND`. Legacy gets cannot tell not-found from other failures by code, so tombstones on legacy portals must come from `crm.item.get` or be held. The access-denied half stays open (S-C.3). |
| S-A.8 | `crm.stagehistory.list` | `result.items`, UPPERCASE keys, `start:-1` works (`total: null`). `>ID` keyset ascending and `order {ID:DESC}` both work. `@OWNER_ID` filters exactly (10 owners → 36 rows, all matching). Lead rows carry `STATUS_ID`/`STATUS_SEMANTIC_ID`. `CATEGORY_ID` is carried (`0` for the default funnel). | The history lane, owner repair and the trailing window work as planned, with no fallback needed. |
| S-A.9 | `time.date_start` | Present on the batch `time` and on every successful command's `result_time`, as `+03:00` server time. Absent on errored commands. | The sweep watermark uses the server clock from the response. Parse with the offset; never assume the portal's zone. |
| C6 | `$result[...][items][49][id]` chaining inside one batch | A full page resolves exactly like the keyset (same first ids as page 1). A **short** page makes the next command fail with `INVALID_ARG_VALUE` ("field '>ID' has invalid value"): an error, not a silently broadened result. | Chained keyset pages in one batch are safe under the contiguous-prefix rule. The planner may chain instead of issuing disjoint range streams. `INVALID_ARG_VALUE` after a short page means "end of data", not a failure. |
| S-C.4 | Range count: `select [id]`, `start:0`, `>=id`/`<id` | The 2k-id range returned `total: 974` for ~0.4 s. The whole-table range (~12k ids) returned `total: 4623` for ~0.3 s. | Count bisection for reconciliation is cheap at this size. Re-measure on a large portal before relying on it above ~100k rows. |

**Still open after S-A:**
- the access-denied error code (needs a non-admin token; S-C.3);
- behaviour on a portal with non-empty UTM values and multi-currency deals;
- costs on a portal two orders of magnitude larger.
