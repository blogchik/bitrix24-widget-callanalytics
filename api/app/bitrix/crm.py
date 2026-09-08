"""The CRM commands a detail-tab open packs into its batch (§4.4 step 4, §4.8).

WHY this module exists: `voximplant.statistic.get` documents `CRM_ENTITY_TYPE` as
**CONTACT, COMPANY or LEAD only** - there is no `DEAL`
(docs/bitrix24-api-research.md, block (c), correction 1). So the `CRM_DEAL_DETAIL_TAB`
placement cannot be served by filtering cached rows on the entity type. A deal is reached
instead through the things a call row *can* name:

* the deal's own contacts and company (`crm.deal.contact.items.get`, `crm.deal.get`), and
* the ids of the deal's call activities (`crm.activity.list`), which land in
  `calls.crm_activity_id`.

Both are resolved at open time **with the opener's own `AUTH_ID`**, so Bitrix24 - not this
app - evaluates the CRM read permission on that deal. That is the whole point: we never
model CRM rights, we borrow the answer. §4.4 step 5 turns any error on these commands into
the `crm_no_access` state with **no** entity JWT, and §4.8 refuses to serve a cached
context that a *different*, possibly more privileged, user resolved.

Everything in the open-time path is a **pure function that returns
`(key, method, params)` triples**. No HTTP happens there, because the caller owns the
batch: §4.4 budgets one batch for the whole open (`user.current`, `user.admin`, `app.info`,
the access probe and these), and a client of our own there would turn one round trip into
two and spend the portal's shared operating-time budget twice (§5.6).

`resolve_recording_url` is the one exception, and it belongs to a different path: §4.6's
playback endpoint has no batch to join and one thing to ask. It is documented at its own
definition.

Paging: `crm.activity.list` answers 50 rows per page, and §3 caps the stored set at
`CRM_ACTIVITY_CAP` (250 = 5 pages x 50, "the handler's page budget"). `deal_context_commands`
/ `entity_activity_commands` emit page 0; `activity_page_commands` emits the follow-up
pages so the caller can pack them into the same batch; `parse_activity_ids` reads whichever
pages came back and stops at the cap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.bitrix.client import BatchResult, BitrixClient
from app.config import settings
from app.logging import get_logger

__all__ = [
    "ACTIVITY_KEY",
    "ACTIVITY_PAGE_SIZE",
    "ACTIVITY_TYPE_CALL",
    "CRM_ACTIVITY_GET",
    "CRM_ACTIVITY_LIST",
    "CRM_DEAL_CONTACT_ITEMS_GET",
    "CRM_DEAL_GET",
    "DEAL_CONTACTS_KEY",
    "DEAL_KEY",
    "ENTITY_TYPES",
    "OWNER_TYPE_IDS",
    "PLACEMENT_ENTITY_TYPES",
    "activity_page_commands",
    "activity_page_key",
    "activity_page_keys",
    "context_command_keys",
    "deal_context_commands",
    "entity_activity_commands",
    "entity_type_for_placement",
    "parse_activity_ids",
    "parse_deal_entity_keys",
    "present_activity_keys",
    "resolve_recording_url",
]

CRM_DEAL_GET: Final[str] = "crm.deal.get"
CRM_DEAL_CONTACT_ITEMS_GET: Final[str] = "crm.deal.contact.items.get"
CRM_ACTIVITY_LIST: Final[str] = "crm.activity.list"
CRM_ACTIVITY_GET: Final[str] = "crm.activity.get"

_log = get_logger(__name__)

#: `crm.activity.list` `filter.OWNER_TYPE_ID`. These are Bitrix24's CRM owner-type ids,
#: not our `entity_type` strings; the mapping is the whole reason a deal tab and a lead
#: tab can share one command builder (§4.4 step 4).
OWNER_TYPE_IDS: dict[str, int] = {"LEAD": 1, "DEAL": 2, "CONTACT": 3, "COMPANY": 4}

#: The four entity types `crm_contexts_type_chk` allows (§3). They are OURS - derived from
#: the `PLACEMENT` allowlist of §4.2, never from a Bitrix payload - which is why the
#: database is allowed to CHECK them.
ENTITY_TYPES: Final[tuple[str, ...]] = ("DEAL", "LEAD", "CONTACT", "COMPANY")

#: `PLACEMENT` -> `entity_type`. `DEFAULT` / `LEFT_MENU` are absent on purpose: they carry
#: no entity and route to the dashboard (§4.4 routing table).
PLACEMENT_ENTITY_TYPES: dict[str, str] = {
    f"CRM_{entity_type}_DETAIL_TAB": entity_type for entity_type in ENTITY_TYPES
}

#: `crm.activity.list` `filter.TYPE_ID`: 2 is the call activity. Telephony writes one per
#: call and reports its id back as `CRM_ACTIVITY_ID` on the statistics row (§4.8), which is
#: the join that makes deal matching possible at all.
ACTIVITY_TYPE_CALL: Final[int] = 2

#: Fixed CRM list page size. Not configurable: it is Bitrix24's, and the §3 cap is
#: expressed as a number of these pages.
ACTIVITY_PAGE_SIZE: Final[int] = 50

#: Batch command keys. Spelled exactly as §4.4 step 4 names them so a `rest_log` row can be
#: read against the design without a translation table. They satisfy the client's key
#: pattern (`^[A-Za-z0-9_.\\-]{1,32}$`).
DEAL_KEY: Final[str] = "deal"
DEAL_CONTACTS_KEY: Final[str] = "contacts"
ACTIVITY_KEY: Final[str] = "acts"


def _entity_id(entity_id: int) -> int:
    """A CRM id is a positive integer and nothing else.

    The value is attacker-chosen: it arrives in `PLACEMENT_OPTIONS.ID` from a POST anyone
    can forge (§4.2, the review's forged-tab scenario). `forms.py` already normalised it to
    an int; re-checking here costs nothing and keeps this module safe to call directly.
    """
    number = int(entity_id)
    if number <= 0:
        raise ValueError(f"CRM entity id must be positive, got {entity_id!r}")
    return number


def _owner_type_id(entity_type: str) -> int:
    """`entity_type` -> `OWNER_TYPE_ID`; an unknown type is a programming error.

    Raising rather than defaulting is deliberate: a silent fallback to, say, LEAD would
    resolve a context for the *wrong* entity and cache it under the right key - a cross-
    entity data leak produced by a typo.
    """
    try:
        return OWNER_TYPE_IDS[entity_type]
    except KeyError:
        raise ValueError(f"unknown CRM entity type: {entity_type!r}") from None


def entity_type_for_placement(placement: str) -> str | None:
    """`CRM_DEAL_DETAIL_TAB` -> `"DEAL"`; None for the placements that carry no entity."""
    return PLACEMENT_ENTITY_TYPES.get(placement)


def _activity_filter(entity_type: str, entity_id: int) -> dict[str, Any]:
    """§4.4 step 4: `{OWNER_TYPE_ID, OWNER_ID, TYPE_ID: 2}`.

    Lower-case `filter` / `select` / `start` is the `crm.*` convention (the telephony
    methods of §5 use the upper-case `FILTER` / `SORT` spelling instead - the two families
    genuinely differ, so this is not a typo to "fix").
    """
    return {
        "OWNER_TYPE_ID": _owner_type_id(entity_type),
        "OWNER_ID": _entity_id(entity_id),
        "TYPE_ID": ACTIVITY_TYPE_CALL,
    }


def activity_page_key(start: int) -> str:
    """Batch key for the activity page starting at `start`: `acts`, `acts50`, `acts100`...

    Page 0 keeps the bare `acts` key of §4.4 step 4 so the common single-page open reads
    exactly as the design specifies it.
    """
    offset = int(start)
    if offset < 0 or offset % ACTIVITY_PAGE_SIZE:
        raise ValueError(f"activity page start must be a multiple of {ACTIVITY_PAGE_SIZE}")
    return ACTIVITY_KEY if offset == 0 else f"{ACTIVITY_KEY}{offset}"


def _cap(cap: int | None) -> int:
    """`CRM_ACTIVITY_CAP` unless the caller overrides it (tests, and only tests)."""
    return settings.crm_activity_cap if cap is None else int(cap)


def _page_starts(cap: int | None) -> list[int]:
    """Offsets of every page the cap allows: `[0, 50, 100, 150, 200]` for 250.

    A cap that is not a whole number of pages still gets the page that contains it; the
    parser is what enforces the exact ceiling, so the last page is simply read short.
    """
    limit = _cap(cap)
    pages = -(-limit // ACTIVITY_PAGE_SIZE)  # ceil, so a cap of 10 still fetches one page
    return [index * ACTIVITY_PAGE_SIZE for index in range(pages)]


def activity_page_keys(*, cap: int | None = None) -> tuple[str, ...]:
    """Every activity page key the cap permits, in page order."""
    return tuple(activity_page_key(start) for start in _page_starts(cap))


def entity_activity_commands(
    entity_type: str, entity_id: int
) -> list[tuple[str, str, dict[str, Any]]]:
    """Page 0 of the entity's call activities - the one CRM command a LEAD/CONTACT/COMPANY
    tab needs (§4.4 step 4).

    `OWNER_TYPE_ID` is 1 / 3 / 4 for LEAD / CONTACT / COMPANY. `DEAL` (2) is accepted as
    well because `deal_context_commands` builds on this function; a deal tab must not issue
    a *different* activity query than the other three, or the two code paths would drift.

    `select: ["ID"]` on purpose: the ids are the only thing §4.8 matches on, and an activity
    record otherwise carries subject lines and comments - customer content this app has no
    reason to receive, let alone log (§6 logs the request, and a `select` is a request).
    """
    return [
        (
            ACTIVITY_KEY,
            CRM_ACTIVITY_LIST,
            {"filter": _activity_filter(entity_type, entity_id), "select": ["ID"]},
        )
    ]


def deal_context_commands(entity_id: int) -> list[tuple[str, str, dict[str, Any]]]:
    """The three CRM commands of a deal tab open (§4.4 step 4).

    `crm.deal.get` for `COMPANY_ID` (and the legacy single `CONTACT_ID`),
    `crm.deal.contact.items.get` for the full contact set, and page 0 of the deal's call
    activities. All three run with the opener's token, so a user who may not read this deal
    gets an error on them and §4.4 step 5 renders `crm_no_access` instead of the tab.
    """
    number = _entity_id(entity_id)
    return [
        (DEAL_KEY, CRM_DEAL_GET, {"id": number}),
        (DEAL_CONTACTS_KEY, CRM_DEAL_CONTACT_ITEMS_GET, {"id": number}),
        *entity_activity_commands("DEAL", number),
    ]


def activity_page_commands(
    entity_type: str,
    entity_id: int,
    *,
    total: int | None = None,
    cap: int | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """The FOLLOW-UP activity pages (start 50, 100, ...), for packing into a batch.

    Page 0 already comes from `entity_activity_commands` / `deal_context_commands`; this
    function never re-emits it, so the two can be concatenated without a duplicate key
    (which `BitrixClient.batch` rejects outright).

    Two ways to use it, both of which the design permits:

    * `total=None` - pack every page the cap allows into the SAME open-time batch,
      speculatively. Costs no extra HTTP round trip, and a page past the end of the
      selection comes back as an empty list, not an error.
    * `total=<result_total of page 0>` - after a batch, ask for exactly the pages that
      exist. Cheaper on the shared operating-time budget when most entities have well
      under 50 calls, at the price of a second batch.

    The cap is `CRM_ACTIVITY_CAP` (§3): 250 ids is what a tab can usefully match on, and an
    unbounded `ANY(activity_ids)` array is a query plan nobody sizes for.
    """
    starts = _page_starts(cap)[1:]
    if total is not None:
        starts = [start for start in starts if start < int(total)]
    return [
        (
            activity_page_key(start),
            CRM_ACTIVITY_LIST,
            {
                "filter": _activity_filter(entity_type, entity_id),
                "select": ["ID"],
                "start": start,
            },
        )
        for start in starts
    ]


def context_command_keys(entity_type: str) -> tuple[str, ...]:
    """The batch keys that MUST be present and error-free for a resolution to count.

    §4.4 step 5: "if any CRM command returned an error, render `/state/crm_no_access` and
    mint **no** entity JWT". A missing key counts as a failure too - a context resolved
    from commands that were never sent would be an empty, permanently wrong cache row.
    """
    if entity_type == "DEAL":
        return (DEAL_KEY, DEAL_CONTACTS_KEY, ACTIVITY_KEY)
    _owner_type_id(entity_type)  # rejects anything outside the four entity types
    return (ACTIVITY_KEY,)


def present_activity_keys(batch: BatchResult, *, cap: int | None = None) -> tuple[str, ...]:
    """Activity page keys this batch actually carries, in page order.

    The caller chooses how many pages to send (see `activity_page_commands`), so both the
    error check and the parser have to discover what was sent rather than assume it.
    """
    sent = {command.key for command in batch.commands}
    return tuple(key for key in activity_page_keys(cap=cap) if key in sent)


def _as_int(value: Any) -> int | None:
    """Numeric id from `"17"`, `17` or `17.0`; anything else is not an id.

    Bitrix24 returns CRM ids as strings on most builds and as integers on some; both
    spellings mean the same row (docs/bitrix24-api-research.md).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _field(raw: Mapping[str, Any], *names: str) -> Any:
    """First present key out of several spellings (builds differ in case)."""
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _rows(result: Any) -> Sequence[Any]:
    """A CRM list result is a list; PHP renders an empty one as `[]` and, rarely, as `{}`."""
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return result
    if isinstance(result, Mapping):
        return list(result.values())
    return ()


def parse_activity_ids(batch: BatchResult, *, cap: int | None = None) -> list[int]:
    """Every `crm.activity.list` id in the batch, deduped, in page order, stopped at the cap.

    Deduping is not cosmetic: pages are plain offsets over a selection that can shift
    between them, so the same activity may appear twice and another may be missed. A
    duplicate id in `activity_ids` would only widen an `ANY(...)` match, but a duplicate in
    the stored array is noise every later reader has to think about.

    Errored pages are skipped here rather than raised: whether a partial set is acceptable
    is a policy decision, and §4.4 step 5 makes it in `services/crm_context.py` (it is not -
    any error means `crm_no_access`). This function is only the reader.
    """
    limit = _cap(cap)
    seen: set[int] = set()
    ids: list[int] = []
    for key in present_activity_keys(batch, cap=cap):
        for row in _rows(batch.get(key)):
            identifier = _as_int(_field(row, "ID", "id") if isinstance(row, Mapping) else row)
            if identifier is None or identifier in seen:
                continue
            seen.add(identifier)
            ids.append(identifier)
            if len(ids) >= limit:
                return ids
    return ids


def parse_deal_entity_keys(
    deal: Any, contacts: Any, *, cap: int | None = None
) -> list[list[Any]]:
    """A deal's contacts and company as `[["CONTACT", 12], ["COMPANY", 3]]` (§3, §4.8).

    The deal itself is deliberately NOT in the list. `CRM_ENTITY_TYPE` has no `DEAL` value,
    so a `["DEAL", id]` key could never match a cached row through this path; §4.8's third
    matching clause (`crm_entity_type = ent.t AND crm_entity_id = ent.id`) is what covers
    the portals that do emit an undocumented `DEAL` anyway.

    Contacts come from `crm.deal.contact.items.get`, which returns the full N:N set;
    `crm.deal.get`'s own `CONTACT_ID` is folded in as well because older builds keep the
    primary contact only there. Order is contacts first, then the company, so the jsonb
    reads the way §3's COMMENT ON writes it.
    """
    limit = _cap(cap)
    seen: set[tuple[str, int]] = set()
    keys: list[list[Any]] = []

    def add(entity_type: str, value: Any) -> None:
        identifier = _as_int(value)
        if identifier is None or identifier <= 0 or len(keys) >= limit:
            return
        pair = (entity_type, identifier)
        if pair in seen:
            return
        seen.add(pair)
        keys.append([entity_type, identifier])

    for row in _rows(contacts):
        if isinstance(row, Mapping):
            add("CONTACT", _field(row, "CONTACT_ID", "contact_id", "ID", "id"))
        else:
            add("CONTACT", row)

    if isinstance(deal, Mapping):
        add("CONTACT", _field(deal, "CONTACT_ID", "contact_id"))
        add("COMPANY", _field(deal, "COMPANY_ID", "company_id"))

    return keys


# --- the recording a call activity carries (§4.6, §9) --------------------------------


def _file_entries(files: Any) -> Sequence[Any]:
    """A `FILES` value as a sequence of entries, in whichever shape it arrived.

    `crm.activity.get` types `FILES` as `diskfile` (`crm.activity.fields`), and on the live
    portal it comes back as a JSON list. Three other shapes are handled anyway, because
    Bitrix24's PHP serialiser is what decides and it is not consistent across builds or
    across empty/non-empty results: the field may be **absent** or **null** (no attachment),
    a **list**, or a **dict keyed by index** (`{"0": {...}}` - how PHP renders an array
    whose keys are not a clean 0..n range). A bare single entry is accepted too. Anything
    else yields no entries at all, which the caller turns into `None`; a shape we do not
    recognise must degrade to "no Bitrix-hosted URL, use the stored one", never to a 500 on
    a playback request.
    """
    if isinstance(files, Mapping) and _field(files, "url", "URL") is not None:
        return (files,)
    return _rows(files)


async def resolve_recording_url(
    client: BitrixClient, *, activity_id: int, record_file_id: int | None
) -> str | None:
    """The Bitrix24-hosted URL of one call's recording, or None if it cannot be named.

    WHY this exists at all (§9, docs/spike-recording-playback.md): `calls.call_record_url`
    points at the telephony provider, and on the live portal our server cannot pull from it
    - every request shape a real player makes returns headers and then no body, while the
    same request from an ordinary client network succeeds. Bitrix24 keeps its own copy,
    because a Marketplace telephony integration is required to call
    `telephony.externalCall.attachRecord` (which is why `RECORD_FILE_ID` is populated), and
    `crm.activity.get` hands that copy over as a `FILES` entry:

        FILES: [ {id: <file id>, url: ".../bitrix/tools/crm_show_file.php?fileId=...
                                        &ownerTypeId=6&ownerId=<activity id>&auth=<token>"} ]

    Measured twice from the container that cannot reach the provider: `Range: bytes=0-`
    returns 206 and the whole 3,962,880-byte body in 1.15 s, a mid-file range returns 206
    with a correct `Content-Range` in 0.26 s, and no request stalls. The endpoint needs no
    cookie and no portal session - the `auth` parameter is the whole gate. `crm.activity.get`
    is scope `crm`, which the app already holds, so this costs no new scope and no new
    install prompt.

    What is measured is not the same as what is promised: `FILES` is a documented activity
    field, but neither "a telephony recording lands there" nor "that endpoint serves
    audio/mpeg with byte ranges" is documented anywhere. The caller therefore keeps the
    stored-URL path wired as a fallback, and this function returns `None` rather than
    raising whenever it cannot name a file with confidence.

    Matching rules:

    * With a `record_file_id` (all 3,209 recorded calls on the first portal have one), the
      entry whose id equals it is the recording. Ids are compared **numerically**: Bitrix24
      serialises numbers as strings in some responses and as integers in others
      (docs/bitrix24-api-research.md), and `"42" != 42` would silently return None forever.
    * Without one, a **single** file in `FILES` is taken as the recording, and several files
      yield `None`. An activity can carry ordinary attachments; guessing which of them is
      the recording would eventually play a customer some other document.

    Raises `BitrixError` (the client's typed failure) rather than swallowing it, so the
    caller can tell "this portal does not attach recordings" from "the REST call failed"
    and log them differently. It never raises for a payload shape.

    The returned URL is NEVER logged: the embedded `auth` is a live access token (§6, §3
    decision 21). The file id and the activity id are logged instead - they name the same
    thing to a support engineer and carry no credential.
    """
    number = _as_int(activity_id)
    if number is None or number <= 0:
        # A row whose `crm_activity_id` is not a usable id: nothing to ask about, and a
        # raise here would turn one bad row into a 500 on a playback request.
        return None

    result = await client.call(CRM_ACTIVITY_GET, {"id": number})
    if not isinstance(result, Mapping):
        return None

    wanted = _as_int(record_file_id) if record_file_id is not None else None
    candidates: list[tuple[int | None, str]] = []
    for entry in _file_entries(_field(result, "FILES", "files")):
        if not isinstance(entry, Mapping):
            continue
        raw_url = _field(entry, "url", "URL")
        if not isinstance(raw_url, str) or not raw_url.strip():
            continue
        candidates.append((_as_int(_field(entry, "id", "ID")), raw_url.strip()))

    if wanted is not None:
        for file_id, url in candidates:
            if file_id == wanted:
                _log.info(
                    "crm: recording resolved from activity",
                    extra={"activity_id": number, "file_id": wanted},
                )
                return url
        _log.info(
            "crm: recording file not attached to activity",
            extra={"activity_id": number, "file_id": wanted, "files": len(candidates)},
        )
        return None

    if len(candidates) == 1:
        file_id, url = candidates[0]
        _log.info(
            "crm: recording resolved from the activity's only file",
            extra={"activity_id": number, "file_id": file_id},
        )
        return url

    _log.info(
        "crm: activity carries no unambiguous recording",
        extra={"activity_id": number, "files": len(candidates)},
    )
    return None
