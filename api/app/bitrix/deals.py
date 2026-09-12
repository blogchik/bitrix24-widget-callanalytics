"""The CRM commands the deal report packs into its batches (§4.12).

WHY this module is separate from `crm.py`: that one serves the *detail tab* - it answers
"which cached calls belong to this card" and never reads a pipeline. This one reads the
pipeline itself, for a page that has no entity at all. Sharing a module would put two
unrelated command vocabularies behind one import and make the tab's careful §4.8 rules look
like they apply here, which they do not.

Everything here is a **pure function**. No HTTP, no database, no clock. The caller owns the
batch, exactly as `crm.py` states it: §5.6 budgets one batch per round trip, and a client of
our own here would turn one request into two and spend the portal's shared operating-time
budget twice.

---------------------------------------------------------------------------------------
**FOUR parameter-spelling families are now in play**, and mixing them is silent breakage
rather than an error:

* `voximplant.*` - UPPER_CASE `FILTER` / `SORT` / `ORDER` (see `statistic.py`).
* `crm.deal.*`, `crm.status.*` - lower-case `filter` / `select` / `order` / `start`
  wrappers with UPPER_CASE field names inside (`crm.py:142-153` already says this).
* `crm.item.*` - the same lower-case wrappers, but **camelCase field names** and a bare
  camelCase `entityTypeId` scalar beside them.
* `crm.category.list` - a bare `{"entityTypeId": 2}` and nothing else: no filter, no order.

`Dialect` exists so that difference is data rather than four copies of the same builder.
---------------------------------------------------------------------------------------

**Why two dialects at all.** `crm.deal.*` is officially discontinued for new development in
favour of `crm.item.*` with `entityTypeId = 2`, and - decisively for this page - `logic: "OR"`
filter grouping is documented ONLY for `crm.item.list`. Owner decision 3 asks for deals whose
creation OR modification OR closing falls in the period, which `crm.item.list` answers in ONE
paged query and `crm.deal.list` cannot answer at all: its filter keys are strictly AND-ed, so
the same question costs three independent paged scans deduped by id. The fallback exists only
for builds that answer `ERROR_METHOD_NOT_FOUND`, and it is entered by the typed
`errors.MethodNotFound` - never by a version number, which no response carries.

**Why `closed` + `movedTime` and not `CLOSEDATE`.** `CLOSEDATE` reads like "when the deal
closed" and is not that: `crm.deal.fields` declares it `{"type": "date", "isReadOnly": false}`
- the deal's *declared* end of its date range, pre-filled at creation and editable by anyone
who can edit the deal. A back-dated value would pull years-old deals into a one-week report
and a forward-dated one would hide a deal that closed yesterday. The read-only pair the
platform maintains is `closed` ("Y"/"N") together with `movedTime`, the moment the deal last
changed stage - which for a closed deal is when it was closed.

**Why the honour probe.** Of the field names this module sends, only `createdTime` is
confirmed by a retrieved doc; `updatedTime`, `movedTime`, `closed` and `stageSemanticId` are
inferred from the documented camelCase mapping. That would be a tolerable risk if an unknown
filter key were an error - but Bitrix24 may instead **ignore** it, and an ignored key inside
the OR group silently widens the union to "created in the period OR everything", producing a
report that is plausible, larger than the truth, and wrong with no symptom anywhere.
`honour_probe_commands` therefore asks the server three questions whose answers are known in
advance, in the EXACT nested shape production uses, so that a build which drops the field name
*or* the `logic` grouping is demoted to the `deal` dialect instead of being believed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "CATEGORIES_KEY",
    "CRM_CATEGORY_LIST",
    "CRM_DEALCATEGORY_LIST",
    "CRM_DEALCATEGORY_STAGE_LIST",
    "CRM_DEAL_LIST",
    "CRM_ITEM_FIELDS",
    "CRM_ITEM_LIST",
    "CRM_STATUS_LIST",
    "DEAL_DIALECT",
    "DEAL_ENTITY_TYPE_ID",
    "DEAL_PAGE_SIZE",
    "FIELDS_KEY",
    "HONOUR_KEYS",
    "ITEM_DIALECT",
    "ME_KEY",
    "PREFLIGHT_KEY",
    "SEMANTIC_FAILURE",
    "SEMANTIC_PROGRESS",
    "SEMANTIC_SUCCESS",
    "USER_CURRENT",
    "Dialect",
    "Funnel",
    "Stage",
    "as_int",
    "category_commands",
    "field_names_present",
    "fields_command",
    "honour_probe_commands",
    "honour_verdict",
    "list_page_commands",
    "me_command",
    "normalise_semantic",
    "page_key",
    "parse_deal_rows",
    "parse_funnels",
    "parse_stages",
    "period_leg_filters",
    "period_or_filter",
    "stage_entity_id",
    "stage_key",
    "status_commands",
    "status_key",
]

# --- methods -------------------------------------------------------------------------

CRM_ITEM_LIST: Final[str] = "crm.item.list"
CRM_ITEM_FIELDS: Final[str] = "crm.item.fields"
CRM_DEAL_LIST: Final[str] = "crm.deal.list"
CRM_CATEGORY_LIST: Final[str] = "crm.category.list"
CRM_STATUS_LIST: Final[str] = "crm.status.list"
CRM_DEALCATEGORY_LIST: Final[str] = "crm.dealcategory.list"
CRM_DEALCATEGORY_STAGE_LIST: Final[str] = "crm.dealcategory.stage.list"
USER_CURRENT: Final[str] = "user.current"

#: `crm.item.*`'s entity type for a deal. Not configurable: it is Bitrix24's own id.
DEAL_ENTITY_TYPE_ID: Final[int] = 2

#: Fixed CRM list page size. Documented as "always static - 50 records", with no
#: `limit`/`pageSize` parameter anywhere in the list contract, so the number of pages a
#: report costs is a function of the selection and nothing the app can tune.
DEAL_PAGE_SIZE: Final[int] = 50

#: Deal-stage `SEMANTICS` / `stageSemanticId`. The docs disagree with themselves about
#: whether "in progress" is JSON `null` (every `crm.status.list` example) or the empty
#: string (`crm.status.add`), so `normalise_semantic` folds both into `P`.
SEMANTIC_SUCCESS: Final[str] = "S"
SEMANTIC_FAILURE: Final[str] = "F"
SEMANTIC_PROGRESS: Final[str] = "P"

# --- batch keys ----------------------------------------------------------------------
# Spelled so a `rest_log` row can be read against §4.12 without a translation table, and
# all inside the client's `^[A-Za-z0-9_.\-]{1,32}$` key pattern.

ME_KEY: Final[str] = "me"
CATEGORIES_KEY: Final[str] = "cats"
FIELDS_KEY: Final[str] = "flds"
PREFLIGHT_KEY: Final[str] = "pre"
HONOUR_KEYS: Final[tuple[str, str, str]] = ("hp0", "hp1", "hp2")

#: A date far enough ahead that no real deal can be at or past it, used by the honour
#: probe. Deliberately not derived from the clock: `Date.now()` in a builder would make the
#: probe's own request non-reproducible in a test.
_NEVER_ISO: Final[str] = "2999-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class Dialect:
    """One REST spelling of "a deal", so the difference is data rather than two builders.

    `name` is echoed in the response's `scan` block. It is a support affordance and nothing
    branches on it outside this module: a question about a portal answering strange numbers
    is answerable without a repro if the answer says which dialect produced them.
    """

    name: str
    method: str
    id: str
    category_id: str
    stage_id: str
    assigned_by_id: str
    semantic: str
    created: str
    updated: str
    moved: str
    closed: str
    #: True when the method takes `entityTypeId` and wraps its rows in `result.items`.
    universal: bool


ITEM_DIALECT: Final[Dialect] = Dialect(
    name="item",
    method=CRM_ITEM_LIST,
    id="id",
    category_id="categoryId",
    stage_id="stageId",
    assigned_by_id="assignedById",
    semantic="stageSemanticId",
    created="createdTime",
    updated="updatedTime",
    moved="movedTime",
    closed="closed",
    universal=True,
)

DEAL_DIALECT: Final[Dialect] = Dialect(
    name="deal",
    method=CRM_DEAL_LIST,
    id="ID",
    category_id="CATEGORY_ID",
    stage_id="STAGE_ID",
    assigned_by_id="ASSIGNED_BY_ID",
    semantic="STAGE_SEMANTIC_ID",
    created="DATE_CREATE",
    updated="DATE_MODIFY",
    moved="MOVED_TIME",
    closed="CLOSED",
    universal=False,
)


@dataclass(frozen=True)
class Funnel:
    """One deal pipeline, normalised out of whichever dictionary method answered."""

    id: int
    name: str
    sort: int
    is_default: bool


@dataclass(frozen=True)
class Stage:
    """One stage of one funnel - a column of the report.

    `key` is OURS, not Bitrix24's. `STATUS_ID` uniqueness is documented as limited to its
    own directory, and the default funnel's stages are unprefixed (`NEW`), so keying a
    column on `status_id` alone would merge two funnels' stages the day a portal creates a
    second directory with the same code. The `C<n>:` prefix that non-default funnels carry
    is never parsed and never assumed present: it is Bitrix24's business, not ours.
    """

    key: str
    category_id: int
    status_id: str
    name: str
    semantic: str
    sort: int
    known: bool


# --- scalar normalisers ---------------------------------------------------------------


def as_int(value: Any) -> int | None:
    """Numeric id from `"17"`, `17` or `17.0`; anything else is not an id.

    The same function `crm.py` carries, for the same reason: Bitrix24 serialises
    `ASSIGNED_BY_ID` as the string `"1"` in one response and the integer `1` in another, and
    a raw comparison would split one operator into two rows of the report.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
            return int(text)
    return None


def _field(raw: Mapping[str, Any], *names: str) -> Any:
    """First present key out of several spellings; builds differ in case."""
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _text(value: Any) -> str:
    """A portal string, or `""`. Never `None`, so the aggregator needs no guard."""
    return value.strip() if isinstance(value, str) else ""


def _rows(result: Any) -> Sequence[Any]:
    """A CRM list result as a sequence; PHP renders an empty one as `[]` and, rarely, `{}`."""
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        return result
    if isinstance(result, Mapping):
        return list(result.values())
    return ()


def normalise_semantic(value: Any) -> str:
    """`S` / `F` / `P`, accepting every spelling of "in progress" the docs use.

    Only `S` and `F` are recognised; everything else - `null`, `""`, `"P"`, and any value a
    future build invents - is in-progress. That direction of defaulting is deliberate: an
    unrecognised value must not be counted as a won or a lost deal, because those two feed
    the only two rollups a supervisor reads as an outcome.
    """
    text = value.strip().upper() if isinstance(value, str) else ""
    if text == SEMANTIC_SUCCESS:
        return SEMANTIC_SUCCESS
    if text == SEMANTIC_FAILURE:
        return SEMANTIC_FAILURE
    return SEMANTIC_PROGRESS


# --- keys ------------------------------------------------------------------------------


def stage_entity_id(category_id: int) -> str:
    """`crm.status.list`'s `ENTITY_ID` for one funnel.

    The default funnel is `DEAL_STAGE` with **no numeric part**. `DEAL_STAGE_0` is not a
    typo Bitrix24 rejects - it returns an EMPTY LIST WITH NO ERROR, so the bug it causes is
    a funnel that renders with zero columns and produces no exception, no log line and no
    symptom anywhere at runtime. `tests/test_deal_aggregate.py` asserts both branches, and
    that assertion is the only thing that will ever catch a regression here.
    """
    return f"DEAL_STAGE_{category_id}" if category_id > 0 else "DEAL_STAGE"


def status_key(category_id: int) -> str:
    """Batch key for one funnel's stage list - and the carrier of its category id.

    `crm.status.list` rows report `CATEGORY_ID` as a STRING for a numbered funnel and as
    `null` for the default one, so the id can never be read back off the row. The command
    key is what remembers which funnel was asked about.
    """
    return f"st{category_id}"


def page_key(start: int, stream: int = 0) -> str:
    """Batch key for one deal page: `pre`, `p0x50`, `p1x0`...

    `stream` exists for the `deal` dialect alone. That method has no OR, so owner decision
    3 becomes three independent paged selections in the same batches, and three pages that
    all start at 0 would otherwise collide on one key - which `BitrixClient.batch` rejects
    outright rather than silently overwriting, but only after the report was already built
    wrong in the caller's head.

    Stream 0's first page keeps the bare `pre` key it was requested under in the preflight
    batch, so the common single-stream report reads in `rest_log` exactly as §4.12 spells it.
    """
    if start < 0 or start % DEAL_PAGE_SIZE:
        raise ValueError(f"deal page start must be a non-negative multiple of {DEAL_PAGE_SIZE}")
    if stream < 0:
        raise ValueError("deal page stream must be non-negative")
    if start == 0 and stream == 0:
        return PREFLIGHT_KEY
    return f"p{stream}x{start}"


def stage_key(category_id: int, status_id: str) -> str:
    """The composite column key, `"<category_id>:<status_id>"`. See `Stage.key`."""
    return f"{category_id}:{status_id}"


# --- dictionary commands ---------------------------------------------------------------


def me_command() -> tuple[str, str, dict[str, Any]]:
    """`user.current`, the identity proof for the posted viewer token.

    It rides in the first batch rather than costing a round trip of its own, and the caller
    refuses the request unless the id it returns equals the JWT's `sub` - byte-identical to
    what `POST /calls/{id}/play-url` already does with a posted token (`calls.py:610-622`).
    Without it, a token belonging to somebody else would answer somebody else's report.
    """
    return (ME_KEY, USER_CURRENT, {})


def fields_command() -> tuple[str, str, dict[str, Any]]:
    """`crm.item.fields` - what turns the camelCase spellings from a guess into a fact.

    One nested command inside a batch that is being sent anyway. It proves a field NAME
    exists; `honour_probe_commands` proves the filter is actually APPLIED. Neither is
    sufficient alone: a name can exist and still be ignored inside a `logic` group on a
    build that does not implement grouping.
    """
    return (FIELDS_KEY, CRM_ITEM_FIELDS, {"entityTypeId": DEAL_ENTITY_TYPE_ID})


def category_commands(*, universal: bool) -> list[tuple[str, str, dict[str, Any]]]:
    """The funnel list, in whichever dictionary dialect this portal answers.

    `crm.category.list` is the current method and returns `result.categories[]` in
    camelCase, including the default funnel as a real row with `id: 0` and `isDefault: "Y"`
    - nothing needs synthesising. `crm.dealcategory.list` is the frozen predecessor: a flat
    `result[]` in UPPER_CASE with no `isDefault`, where the default funnel is simply `ID 0`.

    Both are filtered by the CALLER's rights, which is the whole reason this runs on the
    viewer's token: an empty list means "you may see no funnels", not "this portal has none".
    """
    if universal:
        return [(CATEGORIES_KEY, CRM_CATEGORY_LIST, {"entityTypeId": DEAL_ENTITY_TYPE_ID})]
    return [(CATEGORIES_KEY, CRM_DEALCATEGORY_LIST, {})]


def status_commands(
    category_ids: Sequence[int], *, universal: bool
) -> list[tuple[str, str, dict[str, Any]]]:
    """One stage-list command per funnel - the fan-out that cannot be collapsed.

    `crm.status.list` documents that `ENTITY_ID` must be a string and that arrays are not
    supported, and states plainly that each pipeline requires a separate call. So the cost
    is `K` commands for `K` funnels, and the only lever is the batch (one HTTP request for
    up to 50 of them) and the dictionary cache in front of it.

    `order: {"SORT": "ASC"}` is not decoration: it is what makes the report's columns appear
    in the same left-to-right order as the portal's own kanban, which is the order the
    person reading this page already has in their head.
    """
    if universal:
        return [
            (
                status_key(cid),
                CRM_STATUS_LIST,
                {"order": {"SORT": "ASC"}, "filter": {"ENTITY_ID": stage_entity_id(cid)}},
            )
            for cid in category_ids
        ]
    return [(status_key(cid), CRM_DEALCATEGORY_STAGE_LIST, {"id": cid}) for cid in category_ids]


# --- list commands ---------------------------------------------------------------------


def _leg_created(dialect: Dialect, start_iso: str, end_iso: str) -> dict[str, Any]:
    return {f">={dialect.created}": start_iso, f"<{dialect.created}": end_iso}


def _leg_updated(dialect: Dialect, start_iso: str, end_iso: str) -> dict[str, Any]:
    return {f">={dialect.updated}": start_iso, f"<{dialect.updated}": end_iso}


def _leg_closed(dialect: Dialect, start_iso: str, end_iso: str) -> dict[str, Any]:
    """Deals that were CLOSED in the window - `closed = "Y"` plus `movedTime` in range.

    Not `CLOSEDATE`; see the module docblock. `movedTime` is the moment the deal last
    changed stage, and for a deal that is closed now, that is when it was closed.
    """
    return {
        f"={dialect.closed}": "Y",
        f">={dialect.moved}": start_iso,
        f"<{dialect.moved}": end_iso,
    }


def period_or_filter(dialect: Dialect, *, start_iso: str, end_iso: str) -> dict[str, Any]:
    """Owner decision 3 as ONE filter: created OR modified OR closed inside the window.

    The nested numeric-keyed group carrying a `logic` member is Bitrix24's own documented
    shape for exactly this question, and it exists only on `crm.item.list`. Bounds are
    half-open (`>=start`, `<end`) rather than closed, which is how the official example
    writes it and which sidesteps the whole second-versus-millisecond boundary argument.

    The client flattens this to `filter[0][logic]` and `filter[0][0][>=createdTime]` at
    depth 3, well inside `_MAX_PARAM_DEPTH`, so no client change is needed.
    """
    return {
        "0": {
            "logic": "OR",
            "0": _leg_created(dialect, start_iso, end_iso),
            "1": _leg_updated(dialect, start_iso, end_iso),
            "2": _leg_closed(dialect, start_iso, end_iso),
        }
    }


def period_leg_filters(
    dialect: Dialect, *, start_iso: str, end_iso: str
) -> tuple[dict[str, Any], ...]:
    """The same union as three independent filters - the `crm.deal.list` fallback.

    Every key of a `crm.deal.list` filter is AND-ed and there is no documented OR, so the
    union costs three paged scans whose results are deduped by id. Roughly three times the
    requests and three times the operating time, which is exactly why the `item` dialect is
    the primary path and this one is entered only on `MethodNotFound`.
    """
    return (
        _leg_created(dialect, start_iso, end_iso),
        _leg_updated(dialect, start_iso, end_iso),
        _leg_closed(dialect, start_iso, end_iso),
    )


def _select(dialect: Dialect) -> list[str]:
    """The five fields the report reads, and not one more.

    Explicit for cost and for §6 alike: omitting `select` returns every field including
    every `UF_*`, and a deal's title, comments and custom fields are customer content this
    report has no reason to receive, let alone log. The same argument `crm.py` already makes
    for `crm.activity.list`.
    """
    return [
        dialect.id,
        dialect.category_id,
        dialect.stage_id,
        dialect.assigned_by_id,
        dialect.semantic,
    ]


def _list_params(
    dialect: Dialect,
    *,
    filter_: dict[str, Any],
    start: int,
    assigned_to: Sequence[int] = (),
) -> dict[str, Any]:
    """One page request, in this dialect's spelling.

    `order` by id ascending is what makes offset paging as stable as it can be here: the
    selection includes modification time, so a deal edited mid-scan can shift later pages,
    and a stable ascending key turns that into a possible one-row undercount rather than a
    duplicate.
    """
    params: dict[str, Any] = dict(filter_)
    if assigned_to:
        params[f"@{dialect.assigned_by_id}"] = list(assigned_to)
    out: dict[str, Any] = {
        "filter": params,
        "select": _select(dialect),
        "order": {dialect.id: "ASC"},
        "start": start,
    }
    if dialect.universal:
        out["entityTypeId"] = DEAL_ENTITY_TYPE_ID
    return out


def list_page_commands(
    dialect: Dialect,
    *,
    filter_: dict[str, Any],
    starts: Sequence[int],
    assigned_to: Sequence[int] = (),
    stream: int = 0,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Page requests for the given offsets, ready to pack into one batch.

    Speculative packing is the same idiom `crm.py::activity_page_commands` uses: a page past
    the end of the selection answers an empty list rather than an error, so a batch may ask
    for more than exists without paying for a second round trip to find out.
    """
    return [
        (
            page_key(start, stream),
            dialect.method,
            _list_params(dialect, filter_=filter_, start=start, assigned_to=assigned_to),
        )
        for start in starts
    ]


def honour_probe_commands(dialect: Dialect) -> list[tuple[str, str, dict[str, Any]]]:
    """Three questions whose answers are known, in the EXACT shape production sends.

    `hp0` is unfiltered and establishes that this viewer can see anything at all - without
    it a zero from `hp1`/`hp2` proves nothing, because a viewer with no readable deals
    returns zero for every filter.

    `hp1` and `hp2` are **nested `logic: "OR"` groups**, not flat filters, and that is the
    point. Two failures are possible and independent: the build may not know the field name,
    or it may not implement `logic` grouping and treat `filter["0"]` as one more unknown
    key. A flat probe detects only the first, and the second is the more dangerous - it
    discards the entire union and answers as if no date filter had been sent at all.

    Both ask for deals at or past the year 2999, so an honoured filter answers zero and any
    non-zero total means something in the filter was dropped.
    """
    never = _NEVER_ISO
    return [
        (HONOUR_KEYS[0], dialect.method, _list_params(dialect, filter_={}, start=0)),
        (
            HONOUR_KEYS[1],
            dialect.method,
            _list_params(
                dialect,
                filter_={
                    "0": {
                        "logic": "OR",
                        "0": {f">={dialect.created}": never},
                        "1": {f">={dialect.updated}": never},
                    }
                },
                start=0,
            ),
        ),
        (
            HONOUR_KEYS[2],
            dialect.method,
            _list_params(
                dialect,
                filter_={
                    "0": {
                        "logic": "OR",
                        "0": {f"={dialect.closed}": "Y", f">={dialect.moved}": never},
                    }
                },
                start=0,
            ),
        ),
    ]


def honour_verdict(
    *, baseline: int | None, future_dates: int | None, future_closed: int | None
) -> bool | None:
    """`True` honoured, `False` demote to the fallback dialect, `None` inconclusive.

    `None` is returned when the baseline is zero or unknown, and the caller MUST NOT cache
    it. A viewer whose CRM rights are "own deals only" and who owns nothing gets a zero
    baseline; recording that as "honoured" would pin the verdict for the whole portal for an
    hour on a measurement that tested nothing, and every later viewer would be served from a
    cache that never saw a single deal.
    """
    if baseline is None or baseline <= 0:
        return None
    if future_dates is None or future_closed is None:
        return None
    return future_dates == 0 and future_closed == 0


# --- parsers ----------------------------------------------------------------------------


def field_names_present(fields_result: Any, names: Sequence[str]) -> set[str]:
    """Which of `names` `crm.item.fields` actually declares.

    The result is a map of field name to metadata. A name missing from it is a name this
    build does not have, which is one of the two ways the union can silently widen.
    """
    if not isinstance(fields_result, Mapping):
        return set()
    # Two shapes are documented for this method across builds: the field map at the top
    # level, and the same map nested under `fields`. Accepting both costs one branch and
    # saves a whole dialect demotion on a portal that answers the other one.
    declared = fields_result.get("fields")
    source: Mapping[str, Any] = declared if isinstance(declared, Mapping) else fields_result
    return {name for name in names if name in source}


def parse_funnels(result: Any, *, universal: bool) -> list[Funnel]:
    """The portal's funnels, normalised across both dictionary dialects.

    `crm.category.list` wraps its rows in `result.categories` and spells them camelCase;
    `crm.dealcategory.list` answers a flat array in UPPER_CASE and carries no `isDefault`,
    so there the default funnel is the one whose id is 0. Both spellings are accepted on
    both paths rather than branched on, because a build that mixes them is a build this app
    would otherwise render blank.
    """
    wrapped = universal and isinstance(result, Mapping)
    rows: Sequence[Any] = _rows(result.get("categories")) if wrapped else _rows(result)

    funnels: list[Funnel] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identifier = as_int(_field(row, "id", "ID"))
        if identifier is None or identifier < 0 or identifier in seen:
            continue
        seen.add(identifier)
        flag = _text(_field(row, "isDefault", "IS_DEFAULT")).upper()
        funnels.append(
            Funnel(
                id=identifier,
                name=_text(_field(row, "name", "NAME")),
                sort=as_int(_field(row, "sort", "SORT")) or 0,
                is_default=flag == "Y" if flag else identifier == 0,
            )
        )
    funnels.sort(key=lambda funnel: (funnel.sort, funnel.id))
    return funnels


def parse_stages(result: Any, *, category_id: int) -> list[Stage]:
    """One funnel's stages, in the order the portal's own kanban shows them.

    `category_id` is passed in rather than read off the rows: `CATEGORY_ID` comes back as a
    STRING for a numbered funnel and as `null` for the default one, so the row cannot be
    trusted to name the funnel it belongs to. The batch key carried it instead.

    `SORT` arrives as a string of an integer and is cast defensively; rows that are already
    in order are left in it, so a portal whose SORT values collide keeps its response order
    rather than being re-sorted by a name nobody sorted by.
    """
    stages: list[Stage] = []
    seen: set[str] = set()
    for index, row in enumerate(_rows(result)):
        if not isinstance(row, Mapping):
            continue
        status_id = _text(_field(row, "STATUS_ID", "statusId", "status_id"))
        if status_id in seen:
            continue
        seen.add(status_id)
        stages.append(
            Stage(
                key=stage_key(category_id, status_id),
                category_id=category_id,
                status_id=status_id,
                name=_text(_field(row, "NAME", "name")),
                semantic=normalise_semantic(_field(row, "SEMANTICS", "semantics")),
                sort=as_int(_field(row, "SORT", "sort")) or index,
                known=True,
            )
        )
    stages.sort(key=lambda stage: (stage.sort, stage.status_id))
    return stages


def parse_deal_rows(result: Any, *, universal: bool) -> Sequence[Mapping[str, Any]]:
    """The deal rows of one page, unwrapped for whichever list method answered.

    `crm.item.list` answers `{"items": [...]}`; `crm.deal.list` answers the bare array. A
    row that is not a mapping is dropped rather than raised on: this parser must never turn
    one malformed row from a portal into a 500 for the whole report.
    """
    rows = _rows(result.get("items")) if universal and isinstance(result, Mapping) else _rows(result)
    return [row for row in rows if isinstance(row, Mapping)]
