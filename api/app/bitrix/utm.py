"""The CRM commands the UTM report packs into its batches (§4.13).

WHY this module is separate from `deals.py`, which also reads a pipeline: that module's own
docblock is a carefully argued statement about ONE page's command vocabulary, and its
`_select` carries the sentence "the five fields the report reads, and not one more" as a §6
promise about what customer content the deal report is allowed to receive. This page needs
eleven different fields, a second entity type and a money column. Bolting them onto that
dialect would make every claim in that file conditional, and would widen what `/deals`
itself asks Bitrix24 for - which is the one thing its comment promises it will not do.

So: a new vocabulary gets a new module. What IS shared is imported rather than copied -
`as_int`, `normalise_semantic` and `parse_deal_rows` are entity-agnostic already, and every
one of them is public in `deals.py::__all__`.

Everything here is a **pure function**. No HTTP, no database, no clock. The caller owns the
batch, for the reason `crm.py` states: §5.6 budgets one batch per round trip.

---------------------------------------------------------------------------------------
**THREE ways this page is simpler than §4.12, and all three follow from one owner decision.**

Decision 3 is *creation date only*. `/deals` counts a deal that was created OR modified OR
closed in the period, which is expressible in one query only through `logic: "OR"`, which is
documented only for `crm.item.list`. That single fact is why §4.12 has a primary dialect, a
three-times-more-expensive fallback, and a probe that must prove nested `logic` grouping
survives. Here the filter is `>=created` AND `<created` - two flat keys that every list
method has always honoured - so:

* the legacy dialects cost exactly what the universal ones cost, and demotion is free;
* `page_key`'s `stream` machinery is unnecessary: one selection per entity, never three;
* the honour probe halves.

The fourth simplification is not about the filter: this page has **no dictionary phase**.
`/deals` spends its cold path on `crm.category.list` plus one `crm.status.list` per funnel
because its COLUMNS are portal data. This page's columns are UTM values, which arrive on the
rows themselves, and won/lost comes off `stageSemanticId`, which research block (g) already
verified is a read-only field on the row. So there is no `Funnel`, no `Stage`, no dictionary
cache and no dictionary TTL anywhere in §4.13.
---------------------------------------------------------------------------------------

**What the honour probe is still for, and why halving it did not make it optional.** The
instinct is that one flat leg is safer than a nested group, so the probe can go. It is the
opposite. Under §4.12's design an ignored `createdTime` widened the union to "created in the
period OR everything" - bad, but bounded by the other legs and usually caught by the
preflight cap. Under THIS design an ignored `>=createdTime` deletes the period outright: the
selection becomes every lead the portal has ever had. On a large portal that surfaces as a
confusing `utm_scan_too_large`; on a small one it is a **200** - a lifetime report with a
one-month date range printed above it, internally consistent in every cell, and wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.bitrix.deals import (
    CRM_DEAL_LIST,
    CRM_ITEM_FIELDS,
    CRM_ITEM_LIST,
    DEAL_ENTITY_TYPE_ID,
    DEAL_PAGE_SIZE,
    ME_KEY,
    as_int,
    me_command,
    normalise_semantic,
    parse_deal_rows,
)

__all__ = [
    "BUCKET_COLLAPSED",
    "BUCKET_NONE",
    "BUCKET_OTHER",
    "CRM_DEAL_FIELDS",
    "CRM_LEAD_FIELDS",
    "CRM_LEAD_LIST",
    "DEAL_ITEM",
    "DEAL_LEGACY",
    "DIMENSIONS",
    "KIND_DEAL",
    "KIND_LEAD",
    "LEAD_ENTITY_TYPE_ID",
    "LEAD_ITEM",
    "LEAD_LEGACY",
    "ME_KEY",
    "PAGE_SIZE",
    "EntityDialect",
    "core_names",
    "fields_command",
    "fields_key",
    "honour_key",
    "honour_probe_commands",
    "honour_verdict",
    "legacy_of",
    "list_page_commands",
    "me_command",
    "page_key",
    "parse_rows",
    "period_filter",
    "read_assigned",
    "read_id",
    "read_lead_id",
    "read_money",
    "read_semantic",
    "utm_names",
    "utm_values",
]

# --- methods -------------------------------------------------------------------------

CRM_LEAD_LIST: Final[str] = "crm.lead.list"
CRM_LEAD_FIELDS: Final[str] = "crm.lead.fields"
CRM_DEAL_FIELDS: Final[str] = "crm.deal.fields"

#: `crm.item.*`'s entity type for a lead. Not configurable: it is Bitrix24's own id.
LEAD_ENTITY_TYPE_ID: Final[int] = 1

#: Re-exported so this module's callers never import the deal page's spelling of a number
#: that is a property of the CRM list contract rather than of either report.
PAGE_SIZE: Final[int] = DEAL_PAGE_SIZE

KIND_LEAD: Final[str] = "lead"
KIND_DEAL: Final[str] = "deal"

#: The report's own vocabulary for the five tags, in the fixed order `combinations[].k` is
#: positional against. Never a Bitrix24 spelling: the dialect is the only place those live.
DIMENSIONS: Final[tuple[str, ...]] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
)

# --- reserved buckets -------------------------------------------------------------------
# DECLARED in the response rather than hardcoded in the SPA, because a real `utm_term` could
# literally be the string `other`. `U+0001` is legal JSON, and a tag carrying it could not
# have survived a URL query string into a Bitrix24 field without being visible garbage - so
# the collision is not merely unlikely, it is unreachable.

#: No tag at all: the key was absent, `null`, or whitespace. Rendered by the page as a
#: translated label, NEVER as an empty cell - an empty cell reads as a rendering bug.
BUCKET_NONE: Final[str] = ""

#: Every value past `UTM_VALUE_CAP` for one dimension, folded together. Not a dropped row:
#: the count still lands here, so `sum(rows) == totals` holds exactly.
BUCKET_OTHER: Final[str] = "\u0001other"

#: A dimension lifted out of the composite key entirely because the row count was still past
#: `UTM_COMBINATION_CAP` after bucketing. Its own facet stays exact.
BUCKET_COLLAPSED: Final[str] = "\u0001collapsed"

#: A date far enough ahead that no real record can be at or past it. Deliberately not
#: derived from the clock: a `now()` in a builder would make the probe's own request
#: non-reproducible in a test.
_NEVER_ISO: Final[str] = "2999-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class EntityDialect:
    """One REST spelling of "a lead" or "a deal", so the difference is data, not branches.

    `name` is echoed in the response's `scan` block and nothing outside this module branches
    on it. It is a support affordance, for the reason `deals.py::Dialect` gives: a question
    about a portal answering strange numbers is answerable without a repro when the answer
    says which dialect produced them.

    The `None` fields are not optional decoration - each one is a statement:

    * `lead_id` is deals-only because a lead has no lead.
    * `opportunity` / `currency_id` / `account_*` are **deals-only by decision, not by
      availability**. A lead does carry `OPPORTUNITY`, and summing it beside deal amounts
      would double-count every converted lead. §6 says ask for nothing we will not use, so
      the lead dialects do not name them at all.
    """

    name: str
    kind: str
    method: str
    #: `None` on the legacy dialects, whose method already names the entity.
    entity_type_id: int | None
    id: str
    semantic: str
    assigned_by_id: str
    created: str
    lead_id: str | None
    opportunity: str | None
    currency_id: str | None
    account_opportunity: str | None
    account_currency_id: str | None
    #: `DIMENSIONS` member -> this dialect's field spelling.
    utm: Mapping[str, str]
    #: True when the method takes `entityTypeId` and wraps its rows in `result.items`.
    universal: bool


_ITEM_UTM: Final[Mapping[str, str]] = {
    "utm_source": "utmSource",
    "utm_medium": "utmMedium",
    "utm_campaign": "utmCampaign",
    "utm_content": "utmContent",
    "utm_term": "utmTerm",
}

_LEGACY_UTM: Final[Mapping[str, str]] = {
    "utm_source": "UTM_SOURCE",
    "utm_medium": "UTM_MEDIUM",
    "utm_campaign": "UTM_CAMPAIGN",
    "utm_content": "UTM_CONTENT",
    "utm_term": "UTM_TERM",
}

#: `crm.item.*` normalises a LEAD's `STATUS_ID` / `STATUS_SEMANTIC_ID` into the same
#: `stageId` / `stageSemanticId` a deal uses - the universal field list documents `stageId`
#: and `stageSemanticId` as common to both and lists no `statusId` at all. That is what makes
#: one dialect SHAPE serve both entities here; only the legacy spellings diverge.
LEAD_ITEM: Final[EntityDialect] = EntityDialect(
    name="item-lead",
    kind=KIND_LEAD,
    method=CRM_ITEM_LIST,
    entity_type_id=LEAD_ENTITY_TYPE_ID,
    id="id",
    semantic="stageSemanticId",
    assigned_by_id="assignedById",
    created="createdTime",
    lead_id=None,
    opportunity=None,
    currency_id=None,
    account_opportunity=None,
    account_currency_id=None,
    utm=_ITEM_UTM,
    universal=True,
)

DEAL_ITEM: Final[EntityDialect] = EntityDialect(
    name="item-deal",
    kind=KIND_DEAL,
    method=CRM_ITEM_LIST,
    entity_type_id=DEAL_ENTITY_TYPE_ID,
    id="id",
    semantic="stageSemanticId",
    assigned_by_id="assignedById",
    created="createdTime",
    lead_id="leadId",
    opportunity="opportunity",
    currency_id="currencyId",
    account_opportunity="opportunityAccount",
    account_currency_id="accountCurrencyId",
    utm=_ITEM_UTM,
    universal=True,
)

#: The legacy pair. `crm.lead.list` keeps `STATUS_*` where `crm.deal.list` says `STAGE_*` -
#: the one place the two entities genuinely disagree, and the whole reason this is a table of
#: constants rather than one dialect with an entity id swapped in.
LEAD_LEGACY: Final[EntityDialect] = EntityDialect(
    name="lead",
    kind=KIND_LEAD,
    method=CRM_LEAD_LIST,
    entity_type_id=None,
    id="ID",
    semantic="STATUS_SEMANTIC_ID",
    assigned_by_id="ASSIGNED_BY_ID",
    created="DATE_CREATE",
    lead_id=None,
    opportunity=None,
    currency_id=None,
    account_opportunity=None,
    account_currency_id=None,
    utm=_LEGACY_UTM,
    universal=False,
)

DEAL_LEGACY: Final[EntityDialect] = EntityDialect(
    name="deal",
    kind=KIND_DEAL,
    method=CRM_DEAL_LIST,
    entity_type_id=None,
    id="ID",
    semantic="STAGE_SEMANTIC_ID",
    assigned_by_id="ASSIGNED_BY_ID",
    created="DATE_CREATE",
    lead_id="LEAD_ID",
    opportunity="OPPORTUNITY",
    currency_id="CURRENCY_ID",
    account_opportunity="OPPORTUNITY_ACCOUNT",
    account_currency_id="ACCOUNT_CURRENCY_ID",
    utm=_LEGACY_UTM,
    universal=False,
)

_LEGACY: Final[Mapping[str, EntityDialect]] = {
    KIND_LEAD: LEAD_LEGACY,
    KIND_DEAL: DEAL_LEGACY,
}


def legacy_of(dialect: EntityDialect) -> EntityDialect:
    """The UPPER_CASE dialect for the same entity - the demotion target.

    Demotion here is cheap in a way it is not in §4.12: with a single flat period leg the
    legacy method answers the same question in the same number of pages, so the only thing a
    demotion costs is the field spellings.
    """
    return _LEGACY[dialect.kind]


# --- batch keys ----------------------------------------------------------------------
# Spelled so a `rest_log` row reads against §4.13 with no translation table, and all inside
# the client's `^[A-Za-z0-9_.\-]{1,32}$` key pattern. Deliberately NOT `deals.py::page_key`:
# its `stream` argument and its `start == 0 and stream == 0 -> "pre"` special case exist for
# a three-selection union this page does not have, and reusing it would put an unexplained
# `pre` in the middle of a two-entity batch.


def _check_kind(kind: str) -> str:
    if kind not in (KIND_LEAD, KIND_DEAL):
        raise ValueError(f"unknown entity kind {kind!r}")
    return kind


def page_key(kind: str, start: int) -> str:
    """Batch key for one page of one entity: `l0`, `l50`, `d0`, `d50`...

    The entity letter is what keeps the two streams apart. Both start at offset 0 in the same
    batch, and two commands under one key is something `BitrixClient.batch` rejects outright
    - but only after the caller has already built the report wrong in their head.
    """
    _check_kind(kind)
    if start < 0 or start % PAGE_SIZE:
        raise ValueError(f"page start must be a non-negative multiple of {PAGE_SIZE}")
    return f"{kind[0]}{start}"


def fields_key(kind: str) -> str:
    """Batch key for one entity's `crm.item.fields` probe."""
    return f"fl{_check_kind(kind)[0]}"


def honour_key(kind: str) -> str:
    """The probe key for one entity."""
    return f"h{_check_kind(kind)[0]}"


# --- selection and filter ----------------------------------------------------------------


def core_names(dialect: EntityDialect) -> tuple[str, ...]:
    """The field names without which this dialect cannot produce a row at all.

    Checked through `crm.item.fields` before the universal dialect is believed: only
    `createdTime` is confirmed verbatim by a retrieved doc (research block (g)); the rest are
    inferred from the same documented camelCase mapping, and an inferred name that a build
    does not have is exactly what the probe exists to catch.
    """
    return (dialect.id, dialect.created, dialect.semantic, dialect.assigned_by_id)


def utm_names(dialect: EntityDialect) -> tuple[str, ...]:
    """This dialect's five UTM spellings, in `DIMENSIONS` order."""
    return tuple(dialect.utm[name] for name in DIMENSIONS)


def _select(dialect: EntityDialect) -> list[str]:
    """Every field this report reads for this entity, and not one more.

    Explicit for cost and for §6 alike. Omitting `select` returns every field including every
    `UF_*`, and a lead's name, phone, comment and custom fields are customer content this
    report has no reason to receive, let alone log - the same argument `crm.py` makes for
    `crm.activity.list` and `deals.py` makes for its own five.

    What is deliberately ABSENT, and why:

    * `title`, and every contact or company binding - a UTM report names no people.
    * `stageId` / `STATUS_ID` - the report reads the SEMANTIC (`S`/`F`/`P`), never the stage
      itself, so the stage directory this page does not fetch is one it also does not need.
    * `categoryId` - funnels are §4.12's axis, not this one's.
    * a lead's `OPPORTUNITY` - see `EntityDialect`. It is an estimate a salesperson typed,
      and summing it beside deal amounts double-counts every converted lead.

    The money pair is named TWICE on purpose: `opportunityAccount` is the number the portal
    converted itself, and `opportunity` + `currencyId` is both the fallback for a build with
    no account pair and the guard that detects a multi-currency portal (§4.13).
    """
    names = [dialect.id, dialect.created, dialect.semantic, dialect.assigned_by_id]
    for optional in (
        dialect.lead_id,
        dialect.account_opportunity,
        dialect.account_currency_id,
        dialect.opportunity,
        dialect.currency_id,
    ):
        if optional is not None:
            names.append(optional)
    names.extend(utm_names(dialect))
    return names


def period_filter(dialect: EntityDialect, *, start_iso: str, end_iso: str) -> dict[str, Any]:
    """The period as ONE flat, half-open leg. No `logic`, no nesting, no second selection.

    Both bounds carry an explicit offset (the caller's `_iso`), never a bare date: a bare
    date is read in the PORTAL's timezone while this app computes its period in the viewer's,
    and those differ in the normal case rather than the exotic one.

    Half-open (`>=start`, `<end`) so a record created at midnight on the last day belongs to
    exactly one period, in exactly the way `CallFilters.predicates` already splits days.
    """
    return {f">={dialect.created}": start_iso, f"<{dialect.created}": end_iso}


def _list_params(
    dialect: EntityDialect,
    *,
    filter_: dict[str, Any],
    start: int,
    assigned_to: Sequence[int] = (),
) -> dict[str, Any]:
    """One page request, in this dialect's spelling.

    `order` by id ascending is what makes offset paging as stable as it can be. Unlike §4.12
    the selection here filters on CREATION time, which no edit can change, so a record cannot
    move between pages mid-scan at all: the drift that forced §4.12 to argue for a possible
    one-row undercount does not exist on this page. The ascending order is kept anyway,
    because a stable key costs nothing and a future filter change would reintroduce the
    problem silently.
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
    if dialect.entity_type_id is not None:
        out["entityTypeId"] = dialect.entity_type_id
    return out


def list_page_commands(
    dialect: EntityDialect,
    *,
    filter_: dict[str, Any],
    starts: Sequence[int],
    assigned_to: Sequence[int] = (),
) -> list[tuple[str, str, dict[str, Any]]]:
    """Page requests for the given offsets, ready to pack into one batch.

    Speculative packing is the idiom `crm.py::activity_page_commands` and `deals.py` already
    use: a page past the end of the selection answers an empty list rather than an error, so
    a batch may ask for more than exists without a second round trip to find out how much.
    """
    return [
        (
            page_key(dialect.kind, start),
            dialect.method,
            _list_params(dialect, filter_=filter_, start=start, assigned_to=assigned_to),
        )
        for start in starts
    ]


_LEGACY_FIELDS: Final[Mapping[str, str]] = {
    KIND_LEAD: CRM_LEAD_FIELDS,
    KIND_DEAL: CRM_DEAL_FIELDS,
}


def fields_command(dialect: EntityDialect) -> tuple[str, str, dict[str, Any]]:
    """The field-map command for this dialect - the name check it must pass before belief.

    Defined for BOTH families, and the legacy half is not redundant. `UTM_SOURCE` and its
    four siblings are documented on `crm.lead.fields` and `crm.deal.fields`; whether
    `crm.item.*` re-exposes them camelCased is not documented anywhere. So the universal
    probe answers "does this build know the inferred spelling", and the legacy probe answers
    the question that actually decides whether this page can exist at all: does this portal
    store UTM tags in a form any method will return.
    """
    if dialect.entity_type_id is not None:
        return (
            fields_key(dialect.kind),
            CRM_ITEM_FIELDS,
            {"entityTypeId": dialect.entity_type_id},
        )
    return (fields_key(dialect.kind), _LEGACY_FIELDS[dialect.kind], {})


def honour_probe_commands(dialect: EntityDialect) -> list[tuple[str, str, dict[str, Any]]]:
    """ONE question whose answer is known, in the EXACT shape production sends.

    It asks for records created at or past the year 2999. No record can be: an honoured
    filter answers zero, and any non-zero total means the key was DROPPED - which on this
    page does not widen the selection, it deletes the period. See the module docblock.

    ---------------------------------------------------------------------------------
    **There used to be a second, UNFILTERED command here, and removing it was a bug fix
    rather than a tidy-up.**

    Its job was to prove the viewer can see anything at all, so that a zero from the probe
    could be told apart from "this viewer reads nothing". That is a real question - but it
    is a question about whether the verdict may be CACHED, never about whether the filter
    was honoured, because a dropped key is proved by a non-zero total on its own.

    The cost of asking it that way was not small. `filter_={}` makes Bitrix24 count the
    WHOLE lead table and the whole deal table, on every cold report, for a number the
    report then uses only to decide a cache write. It is independent of the period, so
    narrowing to a single day does not make it cheaper; it repeats on every retry, because
    a report that failed never cached anything; and it spends the `crm.item.list` operating
    budget that the deal page shares. On a production portal that is how a page ends up
    permanently answering `operation_time_limit`.

    The caller now takes the same evidence from the scan it was going to run anyway: if the
    period selection returned any rows at all, this viewer can see records, so a zero here
    means the filter held. Same verdict, same safety, two fewer full-table counts.
    ---------------------------------------------------------------------------------
    """
    return [
        (
            honour_key(dialect.kind),
            dialect.method,
            _list_params(dialect, filter_={f">={dialect.created}": _NEVER_ISO}, start=0),
        ),
    ]


def honour_verdict(*, future: int | None) -> bool | None:
    """`False` demote to the legacy dialect, `True` no evidence the filter was dropped.

    `None` means the probe itself did not answer and the caller must decide on other
    grounds - never that the filter is fine.

    `True` is deliberately NOT "conclusive". A viewer who can read nothing answers zero to
    every filter, so a zero here is only evidence when something else shows they can read
    something; the caller gets that from the period scan and uses it to decide whether the
    verdict may be cached. Keeping the two apart is what let the unfiltered baseline go.
    """
    if future is None:
        return None
    return future == 0


# --- parsers ----------------------------------------------------------------------------


def parse_rows(result: Any, *, universal: bool) -> Sequence[Mapping[str, Any]]:
    """The rows of one page, unwrapped for whichever list method answered.

    Delegates to `deals.py::parse_deal_rows`, which is already entity-agnostic: it branches
    only on `universal` (`{"items": [...]}` versus a bare array) and drops a row that is not
    a mapping rather than raising, so one malformed row from a portal can never become a 500
    for the whole report.
    """
    return parse_deal_rows(result, universal=universal)


def _field(row: Mapping[str, Any], name: str | None) -> Any:
    """One field, tolerating a dialect that does not carry it."""
    return None if name is None else row.get(name)


def _text(value: Any) -> str:
    """A portal string, or `""`. Never `None`, so the aggregator needs no guard."""
    return value.strip() if isinstance(value, str) else ""


def read_id(row: Mapping[str, Any], dialect: EntityDialect) -> int | None:
    """The record's own id, for de-duplication across speculative pages."""
    return as_int(row.get(dialect.id))


def read_semantic(row: Mapping[str, Any], dialect: EntityDialect) -> str:
    """`S` / `F` / `P` for this row.

    `normalise_semantic` folds every spelling of "in progress" - `null`, `""`, `"P"` and
    anything a future build invents - into `P`. That direction of defaulting is deliberate
    and inherited verbatim: an unrecognised value must never be counted as won or lost,
    because those two feed the only rollups a reader treats as an outcome.
    """
    return normalise_semantic(row.get(dialect.semantic))


def read_assigned(row: Mapping[str, Any], dialect: EntityDialect) -> int | None:
    """The responsible user id, or `None` when the record belongs to nobody."""
    return as_int(row.get(dialect.assigned_by_id))


def read_lead_id(row: Mapping[str, Any], dialect: EntityDialect) -> int | None:
    """The lead this deal was converted from, or `None`.

    Feeds `deals_from_lead` - the cheap, bounded stand-in for the `leadId` join §4.13
    rejects. It tells a reader how much of a row's conversion cell came from a tracked lead
    and how much from a deal somebody typed by hand.
    """
    value = as_int(_field(row, dialect.lead_id))
    return value if value is not None and value > 0 else None


def _decimal(raw: Any) -> Decimal | None:
    """A money field as `Decimal`, or `None` when this row carries no usable number.

    `Decimal(str(value))` and never `Decimal(float)`: `OPPORTUNITY` is declared a `double`,
    and summing hundreds of binary floats then serialising the result makes the browser's own
    column sum disagree with the total in the fifteenth digit. §4.12 forbids a row that does
    not add up to its own total; arriving at one through float arithmetic is the same defect
    through a channel nobody would think to look at.

    An unparseable or non-finite amount is treated as absent rather than raised on: a report
    must not 500 because one portal has one strange row.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


@dataclass(frozen=True)
class Money:
    """Both denominations of one row's amount, kept apart until the report picks one.

    WHY both rather than a preference. `opportunityAccount` is Bitrix24's own conversion into
    the portal's account currency and is the number to use - but it is not documented for
    leads or deals (only for SPAs), so a build may not have it. Choosing per ROW would mix
    two denominations inside one column the moment a single record was missing the pair, and
    nothing on screen would say which rows were which. So each row contributes to BOTH sums,
    and §4.13's guard picks ONE source for the whole report: `account` when every amount
    bearing row had the pair, `native` otherwise. Then the currency set for the chosen source
    must be a singleton, or the page hides the money entirely.
    """

    account: Decimal | None
    account_currency: str
    native: Decimal | None
    native_currency: str


def read_money(row: Mapping[str, Any], dialect: EntityDialect) -> Money:
    """This row's amount in both denominations. All-`None` on a lead, which names neither."""
    return Money(
        account=_decimal(_field(row, dialect.account_opportunity)),
        account_currency=_text(_field(row, dialect.account_currency_id)),
        native=_decimal(_field(row, dialect.opportunity)),
        native_currency=_text(_field(row, dialect.currency_id)),
    )


def utm_values(row: Mapping[str, Any], dialect: EntityDialect, *, max_chars: int) -> tuple[str, ...]:
    """This row's five tags, normalised, in `DIMENSIONS` order.

    Three normalisations, each of them a decision:

    * **Absent, `null` and whitespace all become `BUCKET_NONE`.** They are the same business
      fact - this record carries no tag - and splitting them would put an untagged record in
      two different rows depending on which of three indistinguishable things the portal
      stored.
    * **Truncated to `max_chars` BEFORE keying**, so two values that differ only past the cut
      merge into one row rather than producing two rows the page renders identically. The cut
      is stated in `scan.value_max_chars`.
    * **Case and spelling are preserved.** `Google` and `google` are two rows. Folding them
      would require choosing which spelling to display, which is a guess about what the
      marketer meant; v1 states the behaviour on the page instead of guessing.
    """
    out: list[str] = []
    for name in DIMENSIONS:
        text = _text(row.get(dialect.utm[name]))
        out.append(text[:max_chars] if len(text) > max_chars else text)
    return tuple(out)
