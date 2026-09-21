"""The CRM mirror's REST vocabulary: dialects, the select allowlist, commands and parsers.

Everything here is a pure function over `(key, method, params)` triples and response values
- no HTTP, no database - for the reason `bitrix/crm.py` and `bitrix/deals.py` give: the
caller owns the batch, and a builder that cannot be tested without a portal ends up tested
on one.

The facts it is built on were measured on a real portal, not assumed
(docs/spike-crm-mirror.md, S-A):

* `crm.item.list` keysets with `order {id: ASC}`, `filter {">id": n}` and `start: -1`. The
  answer is `result.items`, at most 50 rows, and `total`/`next` are null. The legacy
  `crm.deal.list` / `crm.lead.list` behave the same with UPPERCASE keys and a bare list.
* `>=updatedTime` (legacy `>=DATE_MODIFY`) and `@id` are honoured.
* `opportunityAccount` / `accountCurrencyId` are never returned, so money is native only.
* `contactIds` arrives as a list and `closed` as the string `Y`/`N`.

§6's rule carries over unchanged, now as owner decision D-3: a select names exactly the
fields the mirror stores - never a title, a person's name, a phone, an e-mail, a comment,
`fm` or a custom field. The dialects are checked against that at import, so widening an
allowlist fails before the module can be used.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from app.bitrix import utm as utm_dialects
from app.bitrix.deals import as_int, normalise_semantic

__all__ = [
    "CRM_DEAL_LIST",
    "CRM_ITEM_GET",
    "CRM_ITEM_LIST",
    "CRM_LEAD_LIST",
    "DEAL_ITEM",
    "DEAL_LEGACY",
    "DIALECTS",
    "ENTITY_DEAL",
    "ENTITY_LEAD",
    "LEAD_ITEM",
    "LEAD_LEGACY",
    "MAX_IDS_PER_COMMAND",
    "PAGE_SIZE",
    "Command",
    "ItemRow",
    "MirrorDialect",
    "census_command",
    "census_key",
    "census_proven",
    "get_command",
    "high_id_command",
    "ids_command",
    "keyset_command",
    "page_rows",
    "parse_flag",
    "parse_item",
    "parse_money",
    "parse_timestamp",
    "range_count_command",
    "row_id",
]

CRM_ITEM_LIST: Final[str] = "crm.item.list"
CRM_ITEM_GET: Final[str] = "crm.item.get"
CRM_DEAL_LIST: Final[str] = "crm.deal.list"
CRM_LEAD_LIST: Final[str] = "crm.lead.list"

ENTITY_LEAD: Final[int] = 1
ENTITY_DEAL: Final[int] = 2

#: Fixed by Bitrix24 for every list method; a page shorter than this is the last one.
PAGE_SIZE: Final[int] = 50

#: `@id` values per command. A longer array still answers at most one 50-row page.
MAX_IDS_PER_COMMAND: Final[int] = 50

Command = tuple[str, str, dict[str, Any]]


@dataclass(frozen=True)
class MirrorDialect:
    """One REST spelling of "a deal" or "a lead", keyed by the mirror's column names.

    `wire` maps a `crm_items` column to this dialect's field. A column the entity does not
    have is simply absent: a lead has no funnel, no `closed` flag, no amount and no lead.
    """

    name: str
    entity_type_id: int
    method: str
    #: True when the method takes `entityTypeId` and wraps its rows in `result.items`.
    universal: bool
    wire: Mapping[str, str]
    #: The single-contact spelling read when the list spelling is absent or empty.
    contact_fallback: str | None
    #: The `/utm` dialect of the same entity, so tag normalisation is shared exactly and the
    #: mirror cannot bucket a tag differently from the live report it replaces.
    utm: utm_dialects.EntityDialect

    @property
    def id_field(self) -> str:
        return self.wire["id"]

    @property
    def select(self) -> list[str]:
        """Every field this dialect stores, and nothing else (D-3)."""
        fields = [*self.wire.values()]
        if self.contact_fallback is not None:
            fields.append(self.contact_fallback)
        return list(dict.fromkeys(fields))

    def op(self, operator: str, column: str) -> str:
        """A filter key: `op(">", "id")` is `>id` here and `>ID` on a legacy dialect."""
        return f"{operator}{self.wire[column]}"

    def base_params(self) -> dict[str, Any]:
        return {"entityTypeId": self.entity_type_id} if self.universal else {}


_ITEM_COMMON: Final[Mapping[str, str]] = {
    "id": "id",
    "stage_id": "stageId",
    "stage_semantic": "stageSemanticId",
    "assigned_by_id": "assignedById",
    "created_time": "createdTime",
    "updated_time": "updatedTime",
    "moved_time": "movedTime",
    "contact_ids": "contactIds",
    "company_id": "companyId",
}
_ITEM_UTM: Final[Mapping[str, str]] = {
    "utm_source": "utmSource",
    "utm_medium": "utmMedium",
    "utm_campaign": "utmCampaign",
    "utm_content": "utmContent",
    "utm_term": "utmTerm",
}
_LEGACY_COMMON: Final[Mapping[str, str]] = {
    "id": "ID",
    "assigned_by_id": "ASSIGNED_BY_ID",
    "created_time": "DATE_CREATE",
    "updated_time": "DATE_MODIFY",
    "moved_time": "MOVED_TIME",
    "company_id": "COMPANY_ID",
}
_LEGACY_UTM: Final[Mapping[str, str]] = {
    "utm_source": "UTM_SOURCE",
    "utm_medium": "UTM_MEDIUM",
    "utm_campaign": "UTM_CAMPAIGN",
    "utm_content": "UTM_CONTENT",
    "utm_term": "UTM_TERM",
}

DEAL_ITEM: Final[MirrorDialect] = MirrorDialect(
    name="item-deal",
    entity_type_id=ENTITY_DEAL,
    method=CRM_ITEM_LIST,
    universal=True,
    wire={
        **_ITEM_COMMON,
        "category_id": "categoryId",
        "closed": "closed",
        "opportunity": "opportunity",
        "currency_id": "currencyId",
        "lead_id": "leadId",
        **_ITEM_UTM,
    },
    contact_fallback=None,
    utm=utm_dialects.DEAL_ITEM,
)

#: `crm.item.*` folds a lead's STATUS_* into the same stageId / stageSemanticId a deal uses.
#: S-A found both `contactId` and `contactIds` on lead rows; the list wins when present.
LEAD_ITEM: Final[MirrorDialect] = MirrorDialect(
    name="item-lead",
    entity_type_id=ENTITY_LEAD,
    method=CRM_ITEM_LIST,
    universal=True,
    wire={**_ITEM_COMMON, **_ITEM_UTM},
    contact_fallback="contactId",
    utm=utm_dialects.LEAD_ITEM,
)

#: The legacy deal carries one `CONTACT_ID`; the full set needs `crm.deal.contact.items.get`,
#: which the mirror asks for only where a tab needs it.
DEAL_LEGACY: Final[MirrorDialect] = MirrorDialect(
    name="deal",
    entity_type_id=ENTITY_DEAL,
    method=CRM_DEAL_LIST,
    universal=False,
    wire={
        **_LEGACY_COMMON,
        "category_id": "CATEGORY_ID",
        "stage_id": "STAGE_ID",
        "stage_semantic": "STAGE_SEMANTIC_ID",
        "closed": "CLOSED",
        "opportunity": "OPPORTUNITY",
        "currency_id": "CURRENCY_ID",
        "lead_id": "LEAD_ID",
        **_LEGACY_UTM,
    },
    contact_fallback="CONTACT_ID",
    utm=utm_dialects.DEAL_LEGACY,
)

LEAD_LEGACY: Final[MirrorDialect] = MirrorDialect(
    name="lead",
    entity_type_id=ENTITY_LEAD,
    method=CRM_LEAD_LIST,
    universal=False,
    wire={
        **_LEGACY_COMMON,
        "stage_id": "STATUS_ID",
        "stage_semantic": "STATUS_SEMANTIC_ID",
        **_LEGACY_UTM,
    },
    contact_fallback="CONTACT_ID",
    utm=utm_dialects.LEAD_LEGACY,
)

DIALECTS: Final[tuple[MirrorDialect, ...]] = (DEAL_ITEM, LEAD_ITEM, DEAL_LEGACY, LEAD_LEGACY)

#: Fields no mirror select may ever name (D-3): customer content, people, and `*`.
_FORBIDDEN_FIELD: Final[re.Pattern[str]] = re.compile(
    r"(?i)^(\*|uf_.*|title|name|last_?name|second_?name|comments?|fm|phone|email|web|im|"
    r"address.*|source_?description|status_?description|observers?)$"
)


def _check_minimal(dialect: MirrorDialect) -> None:
    offending = [field for field in dialect.select if _FORBIDDEN_FIELD.match(field)]
    if offending:
        raise ValueError(f"{dialect.name} selects fields D-3 forbids: {offending}")


def _check_census(dialect: MirrorDialect) -> None:
    """The viewer census must be spellable on every dialect, and honest about funnels.

    `census_proven` compares a returned row field-by-field against what was filtered on, so
    a missing wire entry there is not a failed filter - it is a comparison against `None`
    that would pass for any row. Asserting the spellings at import is how that stays a
    deploy-time error instead of a silently widened scope on one portal's dialect.
    """
    for column in ("id", "assigned_by_id"):
        if column not in dialect.wire:
            raise ValueError(f"{dialect.name} cannot be censused: no {column} field")
    has_category = "category_id" in dialect.wire
    if (dialect.entity_type_id == ENTITY_DEAL) != has_category:
        raise ValueError(
            f"{dialect.name}: a deal dialect needs category_id and a lead dialect must not "
            "have one - crm_items.category_id is NULL for every lead"
        )


for _dialect in DIALECTS:
    _check_minimal(_dialect)
    _check_census(_dialect)


# --- commands ---------------------------------------------------------------------------


def _list_command(
    dialect: MirrorDialect,
    key: str,
    *,
    select: Sequence[str],
    filter: Mapping[str, Any],
    descending: bool = False,
    count: bool = False,
) -> Command:
    params: dict[str, Any] = {
        **dialect.base_params(),
        "select": list(select),
        "order": {dialect.id_field: "DESC" if descending else "ASC"},
        "filter": dict(filter),
        # `-1` switches the COUNT off (49.9 s -> 0.1 s on 2.4M rows, research block (g)).
        "start": 0 if count else -1,
    }
    return (key, dialect.method, params)


def keyset_command(
    dialect: MirrorDialect,
    key: str,
    *,
    after_id: int,
    below_id: int | None = None,
    updated_since: str | None = None,
    ids_only: bool = False,
) -> Command:
    """One keyset page: ids above `after_id` (and below `below_id`), ascending, no count.

    `updated_since` is an ISO timestamp with an explicit offset; the sweep passes the server
    clock it last read, never ours.
    """
    filter: dict[str, Any] = {dialect.op(">", "id"): int(after_id)}
    if below_id is not None:
        if below_id <= after_id + 1:
            raise ValueError("an id range must contain at least one id")
        filter[dialect.op("<", "id")] = int(below_id)
    if updated_since is not None:
        filter[dialect.op(">=", "updated_time")] = updated_since
    select = [dialect.id_field] if ids_only else dialect.select
    return _list_command(dialect, key, select=select, filter=filter)


def high_id_command(dialect: MirrorDialect, key: str) -> Command:
    """The newest page, ids only: its first row is the table's highest id.

    Unfiltered but `start: -1`, so no COUNT runs - the one shape of an unfiltered list that
    is cheap (S-A.1). Never send it with `start: 0`.
    """
    return _list_command(dialect, key, select=[dialect.id_field], filter={}, descending=True)


def ids_command(dialect: MirrorDialect, key: str, ids: Sequence[int]) -> Command:
    """The full stored select for up to 50 known ids."""
    values = sorted({int(value) for value in ids})
    if not values:
        raise ValueError("ids_command requires at least one id")
    if len(values) > MAX_IDS_PER_COMMAND:
        raise ValueError(f"ids_command accepts at most {MAX_IDS_PER_COMMAND} ids")
    return _list_command(dialect, key, select=dialect.select, filter={dialect.op("@", "id"): values})


def range_count_command(dialect: MirrorDialect, key: str, *, lo: int, hi: int) -> Command:
    """`total` for ids in `[lo, hi)` - the reconciliation probe. Bounded, so the COUNT is too."""
    if hi <= lo:
        raise ValueError("a count range must contain at least one id")
    filter = {dialect.op(">=", "id"): int(lo), dialect.op("<", "id"): int(hi)}
    return _list_command(dialect, key, select=[dialect.id_field], filter=filter, count=True)


def get_command(entity_type_id: int, key: str, item_id: int) -> Command:
    """`crm.item.get`: the only call whose `NOT_FOUND` proves a deletion (S-A.7)."""
    return (key, CRM_ITEM_GET, {"entityTypeId": int(entity_type_id), "id": int(item_id)})


def census_key(dialect: MirrorDialect, *, assigned_by_id: int, category_id: int | None) -> str:
    """The batch key of one census cell.

    `client.batch` caps a key at 32 characters of `[A-Za-z0-9_.-]` and raises on a duplicate,
    so the spelling lives here next to the command it names rather than in the caller. `n`
    stands in for "no funnel" and cannot collide with funnel 0, which is a real category id
    on some portals even though the lead pipeline is stored as one.
    """
    funnel = "n" if category_id is None else str(int(category_id))
    return f"{'d' if dialect.entity_type_id == ENTITY_DEAL else 'l'}.{funnel}.{int(assigned_by_id)}"


def census_command(
    dialect: MirrorDialect,
    key: str,
    *,
    assigned_by_id: int,
    category_id: int | None = None,
) -> Command:
    """One cell of the viewer census: "may you see anything assigned to U in funnel C?"

    Sent with the VIEWER's own token, never the installer credential, because the question is
    about that viewer's CRM rights and Bitrix24 is the only thing that knows them - it exposes
    no permission data to read instead (`crm.role.list` and every sibling answer
    ERROR_METHOD_NOT_FOUND).

    `start: -1` for the reason `high_id_command` gives: the answer needed is "is there at
    least one row", and a COUNT of a permission-filtered selection is the expensive way to
    learn it. Nothing downstream may read `total` from a census answer.

    The select is the three fields `census_proven` echoes back, and only those: the census
    must not become a way to read record content it has no D-3 mandate for.
    """
    if category_id is not None and "category_id" not in dialect.wire:
        # Both lead dialects land here: a lead has no funnel, and `crm_items.category_id` is
        # NULL for every lead row. Asking anyway would send `filter[None]` or a KeyError.
        raise ValueError(f"{dialect.name} has no category_id to filter on")
    filter: dict[str, Any] = {dialect.wire["assigned_by_id"]: int(assigned_by_id)}
    select = [dialect.id_field, dialect.wire["assigned_by_id"]]
    if category_id is not None:
        filter[dialect.wire["category_id"]] = int(category_id)
        select.append(dialect.wire["category_id"])
    return _list_command(dialect, key, select=select, filter=filter)


# --- parsing ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemRow:
    """One mirrored record in `crm_items` column terms, normalised and typed."""

    entity_type_id: int
    id: int
    category_id: int | None
    stage_id: str
    stage_semantic: str
    assigned_by_id: int | None
    created_time: dt.datetime | None
    updated_time: dt.datetime | None
    moved_time: dt.datetime | None
    closed: bool | None
    opportunity: Decimal | None
    currency_id: str
    lead_id: int | None
    contact_ids: tuple[int, ...]
    company_id: int | None
    #: `utm_source .. utm_term`, normalised exactly as `/utm` normalises them.
    utm: tuple[str, ...]


def page_rows(result: Any, dialect: MirrorDialect) -> list[Any] | None:
    """The rows of one list answer, or `None` when the shape is unusable.

    `None` truncates progress exactly like an error does. Reading an unexpected shape as
    "zero rows" would make it indistinguishable from the end of a range, which is how a
    cursor skips a page for good.
    """
    if dialect.universal:
        if isinstance(result, Mapping) and isinstance(result.get("items"), list):
            return list(result["items"])
        return None
    if isinstance(result, list):
        return list(result)
    if isinstance(result, Mapping):
        # PHP renders a non-sequential array as an object; its values are the rows.
        return list(result.values())
    return None


def census_proven(
    result: Any,
    dialect: MirrorDialect,
    *,
    assigned_by_id: int,
    category_id: int | None = None,
) -> bool | None:
    """Did Bitrix24 answer the question `census_command` asked? True / False / unusable.

    * `True`  - at least one row came back and EVERY row echoes the assignee (and funnel)
                that was filtered on.
    * `False` - an honest zero, or a row that does not match. Not proven, and that is a
                normal answer: a viewer with no rights in this cell sees nothing in it.
    * `None`  - the shape is unusable. `page_rows` returns `None` rather than `[]` precisely
                so a broken answer cannot pass for an empty one, and the caller must treat
                this as "this entity could not be censused" rather than as a narrow scope.

    The echo is the whole safety property. An unknown filter key may be IGNORED rather than
    refused (research block (g)), and a command whose filter was dropped returns the viewer's
    newest readable rows - which would "prove" a cell nobody granted. The per-row test is the
    same one `sync/crm_fetch.py` applies to a stray id: one row that was not asked for
    neuters the whole command.
    """
    rows = page_rows(result, dialect)
    if rows is None:
        return None
    if not rows:
        return False
    for raw in rows:
        if not isinstance(raw, Mapping):
            return None
        if as_int(raw.get(dialect.wire["assigned_by_id"])) != int(assigned_by_id):
            return False
        if category_id is not None and as_int(raw.get(dialect.wire["category_id"])) != int(
            category_id
        ):
            return False
    return True


def row_id(raw: Any, dialect: MirrorDialect) -> int | None:
    """A row's own positive id, or None."""
    if not isinstance(raw, Mapping):
        return None
    value = as_int(raw.get(dialect.id_field))
    return value if value is not None and value > 0 else None


def parse_timestamp(value: Any) -> dt.datetime | None:
    """An offset-bearing ISO timestamp, or None.

    A value without an offset is refused rather than read in some zone: S-A found the
    server clock at `+03:00` for a `.kz` portal, so no zone can be assumed safely.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_flag(value: Any) -> bool | None:
    """`Y`/`N` (S-A.5), a real boolean, or None when the build said something else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().upper()
        if text == "Y":
            return True
        if text == "N":
            return False
    return None


_CENT: Final[Decimal] = Decimal("0.01")


def parse_money(value: Any) -> Decimal | None:
    """An amount rounded half-up to cents, the way `/utm` rounds each record, or None.

    Through `str`, never `float`: summing binary floats makes a column disagree with its
    own total in the fifteenth digit.
    """
    if value is None or isinstance(value, bool):
        return None
    text = value.strip() if isinstance(value, str) else str(value)
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP) if amount.is_finite() else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive(value: Any) -> int | None:
    number = as_int(value)
    return number if number is not None and number > 0 else None


def _contact_ids(listed: Any, single: Any) -> tuple[int, ...]:
    found: list[int] = []
    if isinstance(listed, (list, tuple)):
        found = [number for number in (_positive(item) for item in listed) if number is not None]
    if not found:
        number = _positive(single)
        found = [number] if number is not None else []
    return tuple(dict.fromkeys(found))


def parse_item(raw: Any, dialect: MirrorDialect, *, utm_max_chars: int) -> ItemRow | str:
    """One row as an `ItemRow`, or the reason it was refused.

    Only a row without a usable id is refused: nothing else can be keyed. An unreadable
    value in any other field becomes that field's empty value, because a mirror that drops
    a record for one odd timestamp is less correct than one that stores it.
    """
    if not isinstance(raw, Mapping):
        return "row is not an object"
    item_id = row_id(raw, dialect)
    if item_id is None:
        return "missing or non-positive id"

    def field(column: str) -> Any:
        wire = dialect.wire.get(column)
        return None if wire is None else raw.get(wire)

    single_contact = raw.get(dialect.contact_fallback) if dialect.contact_fallback else None
    return ItemRow(
        entity_type_id=dialect.entity_type_id,
        id=item_id,
        category_id=as_int(field("category_id")) if "category_id" in dialect.wire else None,
        stage_id=_text(field("stage_id")),
        stage_semantic=normalise_semantic(field("stage_semantic")),
        assigned_by_id=_positive(field("assigned_by_id")),
        created_time=parse_timestamp(field("created_time")),
        updated_time=parse_timestamp(field("updated_time")),
        moved_time=parse_timestamp(field("moved_time")),
        closed=parse_flag(field("closed")) if "closed" in dialect.wire else None,
        opportunity=parse_money(field("opportunity")) if "opportunity" in dialect.wire else None,
        currency_id=_text(field("currency_id")),
        lead_id=_positive(field("lead_id")),
        contact_ids=_contact_ids(field("contact_ids"), single_contact),
        company_id=_positive(field("company_id")),
        utm=utm_dialects.utm_values(raw, dialect.utm, max_chars=utm_max_chars),
    )
