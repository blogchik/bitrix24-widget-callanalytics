"""`voximplant.statistic.get`: the parameter shape and the row parser (§5.5).

WHY this module exists at all, and why it is pure
-------------------------------------------------
Two different kinds of mistake are fatal to this system, and both are prevented here
rather than at the call sites:

1. **A filter that does not filter.** The cursor modules (§5.2-§5.4, §5.7) drive every
   fetch through `FILTER[>ID]` / `FILTER[<ID]` / `FILTER[<=ID]`. A typo in an operator or
   a `None` value does not fail - Bitrix24 answers HTTP 200 with *unfiltered* rows, the
   cursor advances over data it never read, and the hole is permanent. `statistic_params`
   is therefore the single place that spells those keys, and it *raises* on a filter key
   or value that could silently widen the request. It is a pure function so `fetch.py`,
   the backfill, the rescan and the recheck cannot drift apart on the spelling.

2. **One poison row stalling a portal forever.** §5.5 and the design review's
   "sync/upsert: poison rows stall the cursor forever" finding require that **each row is
   parsed independently**: a row that cannot be understood is skipped and reported, never
   raised. If parsing 500 rows could raise, the chunk rolls back, the cursor never moves,
   and the portal re-fetches the same poison page every visit until someone notices.
   `parse_rows` therefore has no failure mode: it always returns a `ParseOutcome`.

What Bitrix24 actually sends (verified, docs/bitrix24-api-research.md block (a))
-------------------------------------------------------------------------------
* Numbers arrive as **strings**: `"ID": "1"`, `"CALL_DURATION": "0"`, `"CALL_VOTE": "5"`,
  `"COST": "0.0000"`, `"SESSION_ID": "3841557776"`.
* `RECORD_FILE_ID` arrives as a **bare integer** (`9079`) or `null` - a different shape in
  the same response.
* `CALL_RECORD_URL` may be `""` **or** `null` even on a row that has a recording
  (`RECORD_FILE_ID` set); `has_record` in §3 is defined over both columns for that reason.
* `CALL_FAILED_CODE` is a string with values such as `603-S` and `OTHER`; `CALL_TYPE` is
  documented as 1..5 but the column has no CHECK (§3) because the list is not closed.
* `CRM_ENTITY_TYPE` is documented as CONTACT/COMPANY/LEAD only, yet portals emit `DEAL`.
  Unknown values of both are stored **raw**; the UI maps what it recognises.
* `CALL_START_DATE` is ISO-8601 **with an offset** reflecting the portal's server tz.

Storage decisions taken here (each one is a WHY, not a preference)
------------------------------------------------------------------
* `cost` is `Decimal`, never `float`: `0.1 + 0.2` money is how a per-line cost report
  stops reconciling against the portal's own telephony bill.
* `call_start_date` is rejected when it is missing, unparsable, **naive** or absurd.
  Defaulting it (to `now()`, to UTC-assumed) would put a real call in the wrong hour of the
  wrong day in every chart, silently and undetectably. A rejected row is visible in
  `portal_sync.rejected_rows`; a fabricated date is visible nowhere.
* `call_record_url` is stored with credential-bearing query parameters removed (§5.5,
  decision 21). The closest documented analogue of a Bitrix24 file link is
  `download.json?auth=<access token>&token=disk|...`; storing that verbatim would put the
  installing administrator's live portal token into 500k rows and into every backup.
* Oversized strings are **truncated to the column width, never dropped**. A 300-character
  `CALL_ID` must not cost us the row's duration, user, time and cost - the values every
  chart is built from. See `DEGRADED_PREFIX` for how a truncation that changes what a
  value *means* is still reported.

Nothing in this file touches the database, the network or the clock beyond one `now()`
used for the future-date sanity bound: `sync/fetch.py` calls it, `sync/upsert.py` writes
what it returns.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "COLUMNS",
    "CREDENTIAL_QUERY_PARAMS",
    "DEGRADED_PREFIX",
    "FILTER_OPERATORS",
    "MAX_FUTURE_SLACK",
    "MIN_START_DATE",
    "PAGE_SIZE",
    "STATISTIC_FIELDS",
    "STATISTIC_METHOD",
    "ParseOutcome",
    "parse_rows",
    "statistic_params",
]

#: The one telephony method this application calls (§5.1). Scope `telephony`.
STATISTIC_METHOD: Final[str] = "voximplant.statistic.get"

#: Page size is **fixed by Bitrix24** at 50 rows; `start` is an offset, so a page is
#: `start = (N-1) * 50` (verified). Every `start` this module accepts is a multiple of it.
PAGE_SIZE: Final[int] = 50


# --------------------------------------------------------------------------- the columns

#: The `calls` data columns this parser produces, in §3's declaration order.
#:
#: WHY it is exported: `sync/upsert.py` builds one `INSERT ... ON CONFLICT DO UPDATE` whose
#: column list and `EXCLUDED` comparison must match these keys exactly. Deriving the list
#: from `rows[0].keys()` would make the statement depend on whichever row came first;
#: **every** dict `parse_rows` returns carries every key here, `None` where the portal sent
#: nothing, so the statement is stable even for an empty page.
COLUMNS: Final[tuple[str, ...]] = (
    "bx_id",
    "call_id",
    "external_call_id",
    "call_category",
    "call_type",
    "call_start_date",
    "call_duration",
    "call_failed_code",
    "call_failed_reason",
    "portal_user_id",
    "portal_number",
    "phone_number",
    "crm_entity_type",
    "crm_entity_id",
    "crm_activity_id",
    "cost",
    "cost_currency",
    "call_vote",
    "call_record_url",
    "record_file_id",
    "record_duration",
    "rest_app_id",
    "rest_app_name",
    "transcript_id",
    "transcript_pending",
    "session_id",
    "redial_attempt",
    "comment",
    "call_log",
)


# --------------------------------------------------------------------- rejection reasons

#: Prefix marking a report entry that did **not** cost us the row.
#:
#: `ParseOutcome.rejected` carries two different things and the caller must be able to tell
#: them apart:
#:
#: * a plain reason (`"start_date_naive"`) - the row was **dropped**; its `bx_id` is *not*
#:   in `ParseOutcome.rows`;
#: * a `degraded:` reason (`"degraded:call_id_truncated"`) - the row **was kept** and is in
#:   `rows`; one field lost fidelity.
#:
#: WHY report the second kind at all: §5.5 counts rejections into
#: `portal_sync.rejected_rows`, which is the only support signal that a portal is emitting
#: values this build does not model. A truncation that changes what a value *means* - an
#: identifier that no longer identifies, a currency code that is no longer that currency -
#: is exactly such a signal. Truncation of a purely descriptive field (`CALL_CATEGORY`,
#: `PORTAL_NUMBER`, `PHONE_NUMBER`, `REST_APP_NAME`) is not reported: the shortened value
#: still says the same thing, and a per-row report on a systematically long field would
#: flood `portal_events` for no decision anybody would take.
DEGRADED_PREFIX: Final[str] = "degraded:"


# ----------------------------------------------------------------- credential scrubbing

#: Query parameter names stripped from `CALL_RECORD_URL` before storage (§5.5, decision
#: 21). The design names `auth`, `token` and `sig`; the task adds `access_token` and this
#: module adds the remaining spellings Bitrix24 is known to use for the same secret.
#:
#: WHY erring wide is correct here: dropping a parameter we did not need makes a cached URL
#: unusable and the playback path re-reads the row (§5.7 point 3 exists for exactly that);
#: keeping one we should have dropped puts a live portal token in the database, in every
#: backup and - if `RECORDING_MODE=redirect` is ever enabled - in a `Location` header sent
#: to an ordinary employee's browser. The two costs are not comparable.
CREDENTIAL_QUERY_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "auth",
        "auth_id",
        "access_token",
        "token",
        "refresh_token",
        "sig",
        "signature",
        "sessid",
        "application_token",
    }
)


# ------------------------------------------------------------------- date sanity bounds

#: Lower sanity bound for `CALL_START_DATE`. Bitrix24 telephony did not exist before this,
#: so a row dated earlier is a zero/epoch placeholder, not a call. Keeping it would pin the
#: "oldest call" of every portal to 1970 and stretch every date axis in the UI.
MIN_START_DATE: Final[dt.datetime] = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)

#: Upper sanity bound, as slack over `now()`. `telephony.externalCall.register` accepts a
#: caller-supplied `CALL_START_DATE`, so a broken integration can post the year 3000; such
#: a row would sit at the top of "recent calls" forever. A week of slack absorbs both clock
#: skew between us and the portal and a legitimately mis-set portal timezone.
MAX_FUTURE_SLACK: Final[dt.timedelta] = dt.timedelta(days=7)


# -------------------------------------------------------------------- parameter grammar

#: Every documented field of the method - the set that may appear in `FILTER` and `SORT`
#: (verified: "sorting allowed by all filter fields except CALL_TYPE").
STATISTIC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "ID",
        "CALL_ID",
        "EXTERNAL_CALL_ID",
        "CALL_CATEGORY",
        "PORTAL_USER_ID",
        "PORTAL_NUMBER",
        "PHONE_NUMBER",
        "CALL_TYPE",
        "CALL_DURATION",
        "CALL_START_DATE",
        "CALL_LOG",
        "CALL_RECORD_URL",
        "CALL_VOTE",
        "COST",
        "COST_CURRENCY",
        "CALL_FAILED_CODE",
        "CALL_FAILED_REASON",
        "CRM_ENTITY_TYPE",
        "CRM_ENTITY_ID",
        "CRM_ACTIVITY_ID",
        "REST_APP_ID",
        "REST_APP_NAME",
        "TRANSCRIPT_ID",
        "TRANSCRIPT_PENDING",
        "SESSION_ID",
        "REDIAL_ATTEMPT",
        "COMMENT",
        "RECORD_DURATION",
        "RECORD_FILE_ID",
    }
)

#: Documented `FILTER` operator prefixes, longest first so `!><` wins over `!` and `>=`
#: over `>`. `@` / `!@` are the IN / NOT IN spelling the rest of the codebase already uses
#: (`user.get {"FILTER": {"@ID": [...]}}`) and §5.7's recheck needs.
FILTER_OPERATORS: Final[tuple[str, ...]] = (
    "!><",
    "!%",
    "!@",
    "!=",
    "><",
    ">=",
    "<=",
    "!",
    ">",
    "<",
    "=",
    "%",
    "?",
    "@",
)

_ORDERS: Final[frozenset[str]] = frozenset({"ASC", "DESC"})


# ------------------------------------------------------------------------ column limits

_INT16_MIN: Final[int] = -(2**15)
_INT16_MAX: Final[int] = 2**15 - 1
_INT32_MIN: Final[int] = -(2**31)
_INT32_MAX: Final[int] = 2**31 - 1
_INT64_MIN: Final[int] = -(2**63)
_INT64_MAX: Final[int] = 2**63 - 1

#: `numeric(12,4)` holds at most eight digits before the point.
_COST_LIMIT: Final[Decimal] = Decimal(10) ** 8
_COST_QUANTUM: Final[Decimal] = Decimal("0.0001")

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"y", "yes", "true", "1"})
_FALSE_STRINGS: Final[frozenset[str]] = frozenset({"n", "no", "false", "0"})


@dataclass(frozen=True)
class ParseOutcome:
    """Everything one page of raw rows produced (§5.5).

    `rows` are ready for the upsert - keyed by database column name, every dict carrying
    every key in `COLUMNS`. `rejected` is a report, not an error: see `DEGRADED_PREFIX` for
    the two kinds of entry it holds.
    """

    rows: list[dict[str, Any]]
    rejected: list[tuple[int | None, str]]


# --------------------------------------------------------------------------- primitives


def _field(raw: Mapping[str, Any], name: str) -> Any:
    """One field, accepting the lower-case spelling some on-premise builds return."""
    if name in raw:
        return raw[name]
    return raw.get(name.lower())


def _present(value: Any) -> bool:
    """True when the portal actually sent something.

    `''` and `null` are interchangeable "absent" per §5.5, so neither counts as present;
    a value that is absent can never be *degraded*, it is simply NULL.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _text(value: Any) -> str | None:
    """Any scalar as trimmed text; `''` -> None. Never a dict's or list's `repr`."""
    if value is None or isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return None
    if isinstance(value, bool):
        # A bool here means the build sent something we do not model; `"True"` in a
        # varchar column would be worse than NULL.
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _clip(value: str | None, width: int) -> tuple[str | None, bool]:
    """Truncate to the §3 column width. Returns `(value, was_truncated)`."""
    if value is None or len(value) <= width:
        return value, False
    return value[:width], True


def _number(value: Any, low: int, high: int) -> tuple[int | None, bool]:
    """A Bitrix24 numeric (usually a string) as a bounded int. Returns `(value, degraded)`.

    `degraded` is True when the portal sent something non-empty that we could not store:
    garbage, or a value outside the column's range. Both become NULL - a `smallint` column
    handed 1_000_000 would raise at INSERT time, which would push the whole 500-row chunk
    into the slow row-by-row retry path of §5.5 for a value we do not even need.
    """
    parsed = _to_int(value)
    if parsed is None or not (low <= parsed <= high):
        return None, _present(value)
    return parsed, False


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            # Decimal rather than int() so `"3"`, `"3.0"` and `"+3"` all read the same way
            # and `"3.5"` is refused instead of silently floored.
            number = Decimal(text)
        except (DecimalException, ValueError):
            return None
        if not number.is_finite() or number != number.to_integral_value():
            return None
        return int(number)
    return None


def _cost(value: Any) -> tuple[Decimal | None, bool]:
    """`COST` as `Decimal`, quantised to `numeric(12,4)`. Returns `(value, degraded)`.

    Decimal and not float: this column is money, and §3 chose `numeric(12,4)` precisely
    because Bitrix24 serialises `"0.0000"`.
    """
    if not _present(value):
        return None, False
    if isinstance(value, bool):
        return None, True
    try:
        number = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except (DecimalException, ValueError):
        return None, True
    # `Decimal("NaN")` is a *valid* Postgres numeric and would poison every SUM over cost.
    if not number.is_finite() or abs(number) >= _COST_LIMIT:
        return None, True
    try:
        return number.quantize(_COST_QUANTUM), False
    except DecimalException:  # pragma: no cover - unreachable given the range check above
        return None, True


def _yes_no(value: Any) -> tuple[bool | None, bool]:
    """`TRANSCRIPT_PENDING` and friends: `Y`/`N` (or a JSON bool) -> bool, else NULL."""
    if not _present(value):
        return None, False
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)):
        return bool(value), False
    token = str(value).strip().lower()
    if token in _TRUE_STRINGS:
        return True, False
    if token in _FALSE_STRINGS:
        return False, False
    return None, True


def _start_date(value: Any) -> tuple[dt.datetime | None, str | None]:
    """`CALL_START_DATE` -> tz-aware UTC, or `(None, reason)`.

    Four distinct reasons, because they mean four different things to whoever reads
    `portal_sync.last_error_text`: the field is missing, the string is not ISO-8601, the
    string carries **no offset**, or the instant is absurd.

    Naive is refused rather than assumed-UTC on purpose. Assuming UTC on a portal whose
    server runs at +03:00 shifts every call three hours - across day boundaries, into the
    wrong bucket of the "hour x weekday" chart - and nothing in the product would ever look
    wrong enough to investigate. A rejected row, by contrast, increments a counter the
    settings page shows.
    """
    if not _present(value) or not isinstance(value, str):
        return None, "start_date_missing"
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None, "start_date_unparsable"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "start_date_naive"
    moment = parsed.astimezone(dt.UTC)
    if moment < MIN_START_DATE or moment > dt.datetime.now(dt.UTC) + MAX_FUTURE_SLACK:
        return None, "start_date_out_of_range"
    return moment, None


def _record_url(value: Any) -> str | None:
    """`CALL_RECORD_URL` with its credentials removed (§5.5, decision 21).

    Removed: any query parameter named in `CREDENTIAL_QUERY_PARAMS`, and the `user:pass@`
    userinfo of the authority. Surviving parameters keep their **original bytes** - the
    query is split on `&` and filtered rather than parsed and re-encoded - so the stored
    URL is still the portal's URL minus the secret, and the recording proxy of §9 can use
    it without guessing at re-encoding.

    `''` and `null` are both NULL: §3's `has_record` is defined over `RECORD_FILE_ID` too
    precisely because a row with a recording can arrive with an empty URL.
    """
    raw = _text(value)
    if raw is None:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        # Not a URL we can take apart - and therefore not one we can prove is credential
        # free. §5.7 point 3 re-reads the row on demand; a NULL here costs nothing else.
        return None

    kept = [
        piece
        for piece in parts.query.split("&")
        if piece and piece.split("=", 1)[0].strip().lower() not in CREDENTIAL_QUERY_PARAMS
    ]
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]

    cleaned = urlunsplit((parts.scheme, netloc, parts.path, "&".join(kept), parts.fragment))
    return cleaned or None


# ------------------------------------------------------------------------- the row parser


def _parse_row(raw: Any) -> tuple[dict[str, Any] | None, int | None, list[str]]:
    """One raw row -> `(row or None, bx_id or None, reasons)`.

    Never raises. The two columns §3 declares NOT NULL - `bx_id` and `call_start_date` -
    are the only ones that can cost the row; everything else degrades to NULL, because a
    call whose `CALL_VOTE` we cannot read is still a call that happened.
    """
    reasons: list[str] = []
    if not isinstance(raw, Mapping):
        return None, None, ["not_a_record"]

    bx_id = _to_int(_field(raw, "ID"))
    if bx_id is None:
        return None, None, ["missing_id"]
    if not (1 <= bx_id <= _INT64_MAX):
        # The upsert key and the sync cursor are both this value; a 0 or a negative id
        # would make `high_id` meaningless for every later visit.
        return None, None, ["id_out_of_range"]

    start_date, date_reason = _start_date(_field(raw, "CALL_START_DATE"))
    if start_date is None:
        return None, bx_id, [date_reason or "start_date_missing"]

    def degrade(flag: bool, what: str) -> None:
        if flag:
            reasons.append(f"{DEGRADED_PREFIX}{what}")

    # Identity-bearing strings: a truncation changes what the value *means*, so it is
    # reported. The row is still kept - see DEGRADED_PREFIX.
    call_id, cut = _clip(_text(_field(raw, "CALL_ID")), 255)
    degrade(cut, "call_id_truncated")
    external_call_id, cut = _clip(_text(_field(raw, "EXTERNAL_CALL_ID")), 255)
    degrade(cut, "external_call_id_truncated")
    call_failed_code, cut = _clip(_text(_field(raw, "CALL_FAILED_CODE")), 32)
    degrade(cut, "call_failed_code_truncated")
    crm_entity_type, cut = _clip(_text(_field(raw, "CRM_ENTITY_TYPE")), 32)
    degrade(cut, "crm_entity_type_truncated")
    cost_currency, cut = _clip(_text(_field(raw, "COST_CURRENCY")), 8)
    degrade(cut, "cost_currency_truncated")

    # Descriptive strings: silently clipped (see DEGRADED_PREFIX for why).
    call_category, _ = _clip(_text(_field(raw, "CALL_CATEGORY")), 64)
    portal_number, _ = _clip(_text(_field(raw, "PORTAL_NUMBER")), 128)
    phone_number, _ = _clip(_text(_field(raw, "PHONE_NUMBER")), 128)
    rest_app_name, _ = _clip(_text(_field(raw, "REST_APP_NAME")), 255)

    call_type, bad = _number(_field(raw, "CALL_TYPE"), _INT16_MIN, _INT16_MAX)
    degrade(bad, "call_type_dropped")
    # NOT NULL DEFAULT 0 in §3: an unreadable duration is 0, but it is reported, because
    # a portal whose durations are all unreadable would otherwise show 0 average talk time
    # with no explanation anywhere.
    duration, bad = _number(_field(raw, "CALL_DURATION"), 0, _INT32_MAX)
    degrade(bad, "call_duration_dropped")
    portal_user_id, bad = _number(_field(raw, "PORTAL_USER_ID"), 0, _INT32_MAX)
    degrade(bad, "portal_user_id_dropped")
    crm_entity_id, bad = _number(_field(raw, "CRM_ENTITY_ID"), _INT32_MIN, _INT32_MAX)
    degrade(bad, "crm_entity_id_dropped")
    crm_activity_id, bad = _number(_field(raw, "CRM_ACTIVITY_ID"), _INT64_MIN, _INT64_MAX)
    degrade(bad, "crm_activity_id_dropped")
    call_vote, bad = _number(_field(raw, "CALL_VOTE"), _INT16_MIN, _INT16_MAX)
    degrade(bad, "call_vote_dropped")
    record_file_id, bad = _number(_field(raw, "RECORD_FILE_ID"), _INT64_MIN, _INT64_MAX)
    degrade(bad, "record_file_id_dropped")
    record_duration, bad = _number(_field(raw, "RECORD_DURATION"), _INT32_MIN, _INT32_MAX)
    degrade(bad, "record_duration_dropped")
    rest_app_id, bad = _number(_field(raw, "REST_APP_ID"), _INT32_MIN, _INT32_MAX)
    degrade(bad, "rest_app_id_dropped")
    transcript_id, bad = _number(_field(raw, "TRANSCRIPT_ID"), _INT64_MIN, _INT64_MAX)
    degrade(bad, "transcript_id_dropped")
    session_id, bad = _number(_field(raw, "SESSION_ID"), _INT64_MIN, _INT64_MAX)
    degrade(bad, "session_id_dropped")
    redial_attempt, bad = _number(_field(raw, "REDIAL_ATTEMPT"), _INT16_MIN, _INT16_MAX)
    degrade(bad, "redial_attempt_dropped")

    cost, bad = _cost(_field(raw, "COST"))
    degrade(bad, "cost_dropped")
    transcript_pending, bad = _yes_no(_field(raw, "TRANSCRIPT_PENDING"))
    degrade(bad, "transcript_pending_dropped")

    row: dict[str, Any] = {
        "bx_id": bx_id,
        "call_id": call_id,
        "external_call_id": external_call_id,
        "call_category": call_category,
        # Stored raw: §3 puts no CHECK on a Bitrix-controlled value, and an undocumented
        # code must reach the UI as "Other" rather than stall the cursor.
        "call_type": call_type,
        "call_start_date": start_date,
        "call_duration": 0 if duration is None else duration,
        "call_failed_code": call_failed_code,
        "call_failed_reason": _text(_field(raw, "CALL_FAILED_REASON")),
        "portal_user_id": portal_user_id,
        "portal_number": portal_number,
        "phone_number": phone_number,
        # `DEAL` is undocumented but real on some portals; stored verbatim (§3).
        "crm_entity_type": crm_entity_type,
        "crm_entity_id": crm_entity_id,
        "crm_activity_id": crm_activity_id,
        "cost": cost,
        "cost_currency": cost_currency,
        "call_vote": call_vote,
        "call_record_url": _record_url(_field(raw, "CALL_RECORD_URL")),
        "record_file_id": record_file_id,
        "record_duration": record_duration,
        "rest_app_id": rest_app_id,
        "rest_app_name": rest_app_name,
        "transcript_id": transcript_id,
        "transcript_pending": transcript_pending,
        "session_id": session_id,
        "redial_attempt": redial_attempt,
        # `text` columns in §3: no width, so nothing to truncate and nothing to report.
        "comment": _text(_field(raw, "COMMENT")),
        "call_log": _text(_field(raw, "CALL_LOG")),
    }
    return row, bx_id, reasons


def parse_rows(raw: Sequence[Any]) -> ParseOutcome:
    """A page of `voximplant.statistic.get` rows -> upsert-ready dicts + a report (§5.5).

    **This function cannot raise on portal data.** Each row is parsed independently, so one
    poison row costs one row and not the 500-row chunk, the transaction, and the cursor
    (design review: "sync/upsert: poison rows stall the cursor forever"). Anything the
    parser refuses is reported in `rejected` as `(bx_id or None, reason)` and counted by the
    caller into `portal_sync.rejected_rows`.

    Order is preserved and rows are **not** de-duplicated here: §5.5 dedupes by `bx_id`
    inside each upsert chunk, because the duplicate this protects against comes from two
    *different* batch commands (a row deleted between two sub-requests shifts the offsets),
    which this function never sees together.

    A `TypeError`/`AttributeError` from a caller passing something that is not a sequence is
    a bug in our code, not portal data, and is deliberately not caught.
    """
    rows: list[dict[str, Any]] = []
    rejected: list[tuple[int | None, str]] = []
    for item in raw:
        row, bx_id, reasons = _parse_row(item)
        if row is not None:
            rows.append(row)
        rejected.extend((bx_id, reason) for reason in reasons)
    return ParseOutcome(rows=rows, rejected=rejected)


# ---------------------------------------------------------------------------- parameters


def _check_filter(filter: dict[str, Any]) -> None:
    """Refuse a filter that Bitrix24 would answer with *more* rows than we asked for.

    Three ways a filter silently stops filtering, all of which produce HTTP 200:

    * a misspelled operator or field (`">Id"`, `">ID "`), which PHP reads as an unknown key
      and ignores;
    * a `None` value - `php_query_pairs` drops NULLs (matching `http_build_query`), so
      `{">ID": None}` is sent as no filter at all;
    * an empty list, which encodes to nothing for the same reason.

    Every one of them makes the cursor advance over rows it never read, which is the one
    failure this design cannot detect afterwards. So they raise here, at the call site, in
    the worker's own process - `ValueError`, because it is our bug and not the portal's.
    """
    for key, value in filter.items():
        if not isinstance(key, str):
            raise ValueError(f"filter key must be a string, got {key!r}")
        field = key
        for operator in FILTER_OPERATORS:
            if key.startswith(operator):
                field = key[len(operator) :]
                break
        if field not in STATISTIC_FIELDS:
            raise ValueError(
                f"filter key {key!r} does not name a voximplant.statistic.get field; "
                "a key Bitrix24 does not recognise is ignored and the request returns "
                "UNFILTERED rows"
            )
        if value is None:
            raise ValueError(f"filter {key!r} is None - it would be dropped from the request")
        if isinstance(value, (list, tuple, set, frozenset)) and not value:
            raise ValueError(f"filter {key!r} is empty - it would be dropped from the request")


def statistic_params(
    *,
    # Shadows the builtin on purpose: the milestone contract fixes this name, and the
    # Bitrix24 parameter it becomes is literally `FILTER`.
    filter: dict[str, Any],
    sort: str = "ID",
    order: str = "ASC",
    start: int = 0,
) -> dict[str, Any]:
    """The params dict for one `voximplant.statistic.get` page (§5.2-§5.4, §5.7).

    The client's PHP-array encoder turns `{"FILTER": {">ID": 5}}` into `FILTER[>ID]=5`;
    this function owns the spelling of everything above that encoder so the head fetch, the
    incremental run, the backfill, the rescan and the recheck cannot drift apart on an
    operator - which they would, since each of them is a different module writing the same
    three keys.

    Raises `ValueError` on anything that would widen the request (see `_check_filter`), on
    a `SORT` Bitrix24 cannot sort by (`CALL_TYPE` is documented as the one exception), and
    on a `start` that is not a page boundary: `start` is an **offset**, pages are fixed at
    `PAGE_SIZE`, and a non-multiple offset would overlap one page and skip another.
    """
    _check_filter(filter)

    sort_field = sort.strip().upper()
    if sort_field not in STATISTIC_FIELDS:
        raise ValueError(f"SORT {sort!r} is not a voximplant.statistic.get field")
    if sort_field == "CALL_TYPE":
        raise ValueError("voximplant.statistic.get cannot sort by CALL_TYPE")

    order_value = order.strip().upper()
    if order_value not in _ORDERS:
        raise ValueError(f"ORDER must be ASC or DESC, got {order!r}")

    if start < 0 or start % PAGE_SIZE:
        raise ValueError(f"start must be a non-negative multiple of {PAGE_SIZE}, got {start}")

    # `start` is always sent, including 0: the params dict is logged into `rest_log`, and a
    # key that appears only sometimes makes two identical requests look different there.
    return {"FILTER": dict(filter), "SORT": sort_field, "ORDER": order_value, "start": start}
