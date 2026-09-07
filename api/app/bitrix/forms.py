"""Allowlist parsing of every Bitrix24-facing POST body (§4.2).

Nothing downstream re-validates these fields, so this module is the trust boundary of
§4.1: "every field of a Bitrix24 POST is untrusted until proven". Two consequences
shape the code:

* A pydantic `ValidationError` must never escape. The handlers turn
  `FormValidationError` into the translated `state.html` "bad request" page (HTTP 400,
  with a request id); a raw stack trace or an English pydantic dump in the iframe is a
  moderation rejection, so every model is built inside a guarded try/except.
* Error messages carry the field name and a stable reason slug only, never the
  offending value - these strings reach logs and the rendered page.
"""

import json
import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

__all__ = [
    "FormValidationError",
    "IframePost",
    "EventPost",
    "is_event_body",
    "parse_iframe_post",
    "parse_event_post",
    "expand_bracket_keys",
    "PLACEMENTS",
    "CRM_PLACEMENTS",
    "MAX_BODY_BYTES",
    "MAX_PLACEMENT_OPTIONS_BYTES",
]


class FormValidationError(Exception):
    """One allowlist violation, rendered as the translated bad-request state (§4.2).

    The value that failed is deliberately absent from the message: it can be an
    attacker-supplied string that would otherwise land in the log and in the page.
    """

    field: str
    reason: str

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


# --- §4.2 allowlist constants -------------------------------------------------------

MAX_BODY_BYTES: Final[int] = 64 * 1024
MAX_PLACEMENT_OPTIONS_BYTES: Final[int] = 4 * 1024

# Bounds that §4.2 does not spell out. They exist only so a crafted body cannot force
# unbounded work before the size checks can help; the real portals send ~15 keys.
MAX_FORM_KEYS: Final[int] = 512
MAX_KEY_LENGTH: Final[int] = 256
MAX_BRACKET_DEPTH: Final[int] = 8
MAX_SCOPES: Final[int] = 64
MAX_RAW_QUERY_BYTES: Final[int] = 4 * 1024

PLACEMENTS: Final[frozenset[str]] = frozenset(
    {
        "DEFAULT",
        "LEFT_MENU",
        "CRM_DEAL_DETAIL_TAB",
        "CRM_LEAD_DETAIL_TAB",
        "CRM_CONTACT_DETAIL_TAB",
        "CRM_COMPANY_DETAIL_TAB",
    }
)
#: The placements whose PLACEMENT_OPTIONS must carry a numeric entity `ID` (§4.4).
CRM_PLACEMENTS: Final[frozenset[str]] = frozenset(
    p for p in PLACEMENTS if p.startswith("CRM_")
)

_MEMBER_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
_LANG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}$")
_AUTH_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{16,512}$")
_APPLICATION_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9]{8,128}$")
_APP_SID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HOST_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_STATUS_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_SCOPE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_EVENT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.]{1,64}$")
#: RFC 3986 query characters minus the four that would need escaping when the string is
#: interpolated into `handoff.html` (§4.4 step 8). A real Bitrix24 query string is
#: percent-encoded, so none of them can legitimately appear.
_RAW_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]*$")
_ASCII_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")
_SCOPE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[ ,]+")
_BRACKET_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\[\]]*)\]")
_BRACKET_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^([^\[\]]+)((?:\[[^\[\]]*\])+)$")

#: Unix seconds upper bound for event `ts` (year 2100). Keeps a crafted 10^300 out of
#: the int column without pretending to validate clock skew (§4.9 rule 1 does that).
_MAX_TS: Final[int] = 4_102_444_800


# --- models -------------------------------------------------------------------------


class IframePost(BaseModel):
    """A validated placement/install POST from the Bitrix24 iframe (§4.3, §4.4, §4.5)."""

    model_config = ConfigDict(frozen=True)

    member_id: str
    domain: str
    protocol_https: bool
    lang: str | None
    app_sid: str | None
    auth_id: str | None
    refresh_id: str | None
    auth_expires: int | None
    application_token: str | None
    server_endpoint: str | None
    application_scope: str
    status: str | None
    placement: str
    placement_options: dict
    scopes: frozenset[str]
    #: Bitrix24's ORIGINAL query string, carried through byte for byte and never
    #: rebuilt from the parsed fields: `handoff.html` must forward `APP_SID` or the
    #: BX24 SDK never initialises and `fitWindow`/`openPath`/`getAuth` stay inert
    #: (§4.4 step 8).
    raw_query: str


class EventPost(BaseModel):
    """A validated lifecycle event body (§4.9), PHP-bracket keys already expanded."""

    model_config = ConfigDict(frozen=True)

    event: str
    ts: int | None
    member_id: str | None
    application_token: str | None
    access_token: str | None
    refresh_token: str | None
    client_endpoint: str | None
    domain: str | None
    scope: str | None
    data: dict


# --- small helpers ------------------------------------------------------------------


def _ci_index(mapping: Mapping[str, str]) -> dict[str, str]:
    """Case-insensitive view of a form/query mapping.

    Bitrix24 mixes cases across cabinets (`member_id` lower, `DOMAIN` upper, and the
    query string repeats some of the body fields), so lookups are normalised once.
    """
    index: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        lowered = key.lower()
        if lowered not in index:  # first occurrence wins, deterministically
            index.setdefault(lowered, value)
    return index


def _clean(value: str | None) -> str | None:
    """Trim and collapse an empty field to None.

    An empty `REFRESH_ID` is a supported state, not a violation: §4.3 step 2 routes it
    to `/state/unsupported_portal` without touching a row, so it must reach the handler
    as None rather than as a regex failure.
    """
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _check_body_size(form: Mapping[str, str], *, field: str = "body") -> None:
    """§4.2 total body <= 64 KB, plus a key-count guard before any per-key work."""
    if len(form) > MAX_FORM_KEYS:
        raise FormValidationError(field, "too_many_fields")
    total = 0
    for key, value in form.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise FormValidationError(field, "non_string_field")
        if len(key) > MAX_KEY_LENGTH:
            raise FormValidationError(field, "field_name_too_long")
        total += len(key.encode("utf-8", "ignore")) + len(value.encode("utf-8", "ignore")) + 2
        if total > MAX_BODY_BYTES:
            raise FormValidationError(field, "body_too_large")


def _match(value: str | None, pattern: re.Pattern[str], field: str) -> str | None:
    if value is None:
        return None
    if not pattern.match(value):
        raise FormValidationError(field, "malformed")
    return value


def _require(value: str | None, field: str) -> str:
    if value is None:
        raise FormValidationError(field, "missing")
    return value


def _validate_hostname(value: str | None, field: str, *, allow_port: bool) -> str | None:
    """RFC hostname with an optional `:port` (§4.2).

    `DOMAIN` is display/CSP data only (§4.1) and it is interpolated into a
    `frame-ancestors` directive, so anything that is not a bare hostname - a scheme, a
    path, a space, a second colon - has to be rejected here rather than escaped there.
    """
    if value is None:
        return None
    host = value.lower()
    if allow_port and ":" in host:
        head, _, port = host.rpartition(":")
        # Anything that is not `host:digits` (a scheme, an IPv6 literal) is not a port
        # at all and must fail as a malformed hostname, not as a bad port number.
        if not _ASCII_DIGITS_RE.match(port) or len(port) > 5:
            raise FormValidationError(field, "malformed")
        if not 1 <= int(port) <= 65535:
            raise FormValidationError(field, "bad_port")
        host = head
        value = f"{host}:{port}"
    else:
        value = host
    if not host or len(host) > 253:
        raise FormValidationError(field, "malformed")
    labels = host.split(".")
    if any(not _HOST_LABEL_RE.match(label) for label in labels):
        raise FormValidationError(field, "malformed")
    return value


def _validate_endpoint(value: str | None, field: str) -> str | None:
    """A REST/OAuth endpoint URL.

    Only the shape is checked; whether the host may actually be used is a separate
    decision - `OAUTH_HOST_ALLOWLIST` for `SERVER_ENDPOINT` (§4.1) and the OAuth
    response for `client_endpoint` (endpoint invariant).
    """
    if value is None:
        return None
    if len(value) > 512:
        raise FormValidationError(field, "too_long")
    scheme, sep, rest = value.partition("://")
    if not sep or scheme.lower() not in {"http", "https"}:
        raise FormValidationError(field, "malformed")
    host = rest.split("/", 1)[0]
    if not host:
        raise FormValidationError(field, "malformed")
    _validate_hostname(host, field, allow_port=True)
    return value


def _parse_int(value: str | None, field: str, *, low: int, high: int) -> int | None:
    if value is None:
        return None
    # ASCII digits only: str.isdigit() also accepts other Unicode decimal forms, which
    # int() would happily convert into a number the portal never sent.
    if not _ASCII_DIGITS_RE.match(value) or len(value) > 20:
        raise FormValidationError(field, "not_an_integer")
    number = int(value)
    if not low <= number <= high:
        raise FormValidationError(field, "out_of_range")
    return number


def _parse_scopes(application_scope: str) -> frozenset[str]:
    """`APPLICATION_SCOPE` splits on `[ ,]` - the docs show both separators (§4.2)."""
    if not application_scope:
        return frozenset()
    parts = [p for p in _SCOPE_SPLIT_RE.split(application_scope) if p]
    if len(parts) > MAX_SCOPES:
        raise FormValidationError("APPLICATION_SCOPE", "too_many_scopes")
    for part in parts:
        if not _SCOPE_RE.match(part):
            raise FormValidationError("APPLICATION_SCOPE", "malformed")
    return frozenset(parts)


def _parse_placement_options(raw: str | None, placement: str) -> dict:
    """`PLACEMENT_OPTIONS`: valid JSON object <= 4 KB, numeric `ID` on a CRM tab (§4.2).

    The numeric check matters beyond hygiene: the id is fed to `crm.deal.get` and to the
    `crm_contexts` key in §4.4 step 7, and it is entirely attacker-chosen (the design
    review's forged-tab scenario), so it is normalised to an int here and the CRM
    access decision is made later against the user's own token.
    """
    if raw is None:
        # A CRM tab with no options cannot name an entity, so it is a violation there
        # and merely an empty dict on the left-menu placements.
        if placement in CRM_PLACEMENTS:
            raise FormValidationError("PLACEMENT_OPTIONS", "crm_id_missing")
        return {}
    if len(raw.encode("utf-8", "ignore")) > MAX_PLACEMENT_OPTIONS_BYTES:
        raise FormValidationError("PLACEMENT_OPTIONS", "too_large")
    try:
        parsed: Any = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise FormValidationError("PLACEMENT_OPTIONS", "invalid_json") from exc
    if not isinstance(parsed, dict):
        raise FormValidationError("PLACEMENT_OPTIONS", "not_an_object")

    options: dict = dict(parsed)
    if placement in CRM_PLACEMENTS:
        entity_id: Any = options.get("ID")
        if isinstance(entity_id, bool) or entity_id is None:
            raise FormValidationError("PLACEMENT_OPTIONS", "crm_id_missing")
        if isinstance(entity_id, str):
            candidate = entity_id.strip()
            if not candidate.isdigit():
                raise FormValidationError("PLACEMENT_OPTIONS", "crm_id_not_numeric")
            entity_id = int(candidate)
        elif not isinstance(entity_id, int):
            raise FormValidationError("PLACEMENT_OPTIONS", "crm_id_not_numeric")
        if not 1 <= entity_id <= 2**63 - 1:
            raise FormValidationError("PLACEMENT_OPTIONS", "crm_id_out_of_range")
        options["ID"] = entity_id  # normalised so the handler never re-parses it
    return options


def _validate_raw_query(raw_query: str) -> str:
    """Guard the string that `handoff.html` forwards verbatim (§4.4 step 8)."""
    if not isinstance(raw_query, str):
        raise FormValidationError("query_string", "malformed")
    value = raw_query[1:] if raw_query.startswith("?") else raw_query
    if len(value.encode("utf-8", "ignore")) > MAX_RAW_QUERY_BYTES:
        raise FormValidationError("query_string", "too_large")
    if not _RAW_QUERY_RE.match(value):
        raise FormValidationError("query_string", "malformed")
    return value


# --- public API ---------------------------------------------------------------------


def is_event_body(form: Mapping[str, str]) -> bool:
    """True when this body is a lifecycle event, whatever endpoint it arrived on.

    §4.2: a body carrying `event=` on `/app/`, `/install/` or `/settings/` is dispatched
    to the events handler BEFORE the placement allowlist runs, because some cabinets
    deliver lifecycle events to the install URL. A predicate, never a raiser: the
    dispatch decision must not be able to fail.
    """
    for key, value in form.items():
        if isinstance(key, str) and key.lower() == "event":
            return isinstance(value, str) and bool(value.strip())
    return False


def expand_bracket_keys(form: Mapping[str, str]) -> dict:
    """PHP-style `auth[member_id]` / `data[FIELDS][ID]` -> nested dicts (§4.2, §4.9).

    Bounded on purpose: an attacker controls the key text, and an uncapped walk over
    `a[0][0][0]...` allocates one dict per segment. Depth and key count are capped, and
    a container always wins over a scalar at the same path so a conflicting pair of
    keys resolves deterministically instead of raising on a body that is merely odd.
    Empty brackets (`a[]`) become successive integer-like string keys - Bitrix24 does
    not use them, and this keeps the return type a plain dict.
    """
    _check_body_size(form)
    root: dict = {}
    for key, value in form.items():
        match = _BRACKET_KEY_RE.match(key)
        if match is None:
            # No brackets, or unbalanced ones: treat the key literally rather than
            # inventing a structure from a malformed name.
            root[key] = value
            continue
        base = match.group(1)
        segments = _BRACKET_SEGMENT_RE.findall(match.group(2))
        if len(segments) + 1 > MAX_BRACKET_DEPTH:
            raise FormValidationError(key[:MAX_KEY_LENGTH], "key_too_deep")

        node: dict = root
        path: list[str] = [base, *segments]
        for segment in path[:-1]:
            name = segment if segment != "" else str(len(node))
            child = node.get(name)
            if not isinstance(child, dict):
                child = {}
                node[name] = child
            node = child
        leaf = path[-1] if path[-1] != "" else str(len(node))
        node[leaf] = value
    return root


def parse_iframe_post(
    form: Mapping[str, str],
    query: Mapping[str, str],
    raw_query: str,
) -> IframePost:
    """Validate a placement/install POST (§4.2 allowlist).

    `query` is a fallback source because Bitrix24 repeats `DOMAIN`/`PROTOCOL`/`LANG`/
    `APP_SID` on the handler URL; `raw_query` is carried through untouched for the
    handoff page. Unknown extra form fields are ignored - cabinets add fields over time
    and rejecting them would break installs for no security gain.
    """
    _check_body_size(form)
    body = _ci_index(form)
    url = _ci_index(query)

    def field(name: str) -> str | None:
        value = _clean(body.get(name.lower()))
        if value is None:
            value = _clean(url.get(name.lower()))
        return value

    member_id = _require(field("member_id"), "member_id")
    if not _MEMBER_ID_RE.match(member_id):
        raise FormValidationError("member_id", "malformed")

    domain = _validate_hostname(field("DOMAIN"), "DOMAIN", allow_port=True)
    if domain is None:
        raise FormValidationError("DOMAIN", "missing")

    protocol_raw = field("PROTOCOL")
    if protocol_raw is None:
        # Absent means the ordinary cloud case; §4.10 only needs the explicit 0 that
        # on-premise portals send to relax the CSP to http.
        protocol_https = True
    elif protocol_raw in {"0", "1"}:
        protocol_https = protocol_raw == "1"
    else:
        raise FormValidationError("PROTOCOL", "malformed")

    lang_raw = field("LANG")
    # Lower-cased before the check so a cabinet sending "RU" is not a 400; the shape
    # itself stays as §4.2 wrote it.
    lang = _match(lang_raw.lower() if lang_raw else None, _LANG_RE, "LANG")

    app_sid = _match(field("APP_SID"), _APP_SID_RE, "APP_SID")
    auth_id = _match(field("AUTH_ID"), _AUTH_TOKEN_RE, "AUTH_ID")
    refresh_id = _match(field("REFRESH_ID"), _AUTH_TOKEN_RE, "REFRESH_ID")
    auth_expires = _parse_int(field("AUTH_EXPIRES"), "AUTH_EXPIRES", low=1, high=86400)
    application_token = _match(
        field("APPLICATION_TOKEN"), _APPLICATION_TOKEN_RE, "APPLICATION_TOKEN"
    )
    server_endpoint = _validate_endpoint(field("SERVER_ENDPOINT"), "SERVER_ENDPOINT")
    status = _match(field("status"), _STATUS_RE, "status")

    application_scope = field("APPLICATION_SCOPE") or ""
    if len(application_scope) > MAX_SCOPES * 65:
        raise FormValidationError("APPLICATION_SCOPE", "too_long")
    scopes = _parse_scopes(application_scope)

    placement = field("PLACEMENT") or "DEFAULT"  # §4.2: absent PLACEMENT means DEFAULT
    if placement not in PLACEMENTS:
        raise FormValidationError("PLACEMENT", "not_allowed")
    placement_options = _parse_placement_options(field("PLACEMENT_OPTIONS"), placement)

    checked_query = _validate_raw_query(raw_query)

    try:
        return IframePost(
            member_id=member_id,
            domain=domain,
            protocol_https=protocol_https,
            lang=lang,
            app_sid=app_sid,
            auth_id=auth_id,
            refresh_id=refresh_id,
            auth_expires=auth_expires,
            application_token=application_token,
            server_endpoint=server_endpoint,
            application_scope=application_scope,
            status=status,
            placement=placement,
            placement_options=placement_options,
            scopes=scopes,
            raw_query=checked_query,
        )
    except ValidationError as exc:  # defence in depth: never leak a pydantic dump
        raise FormValidationError("body", "invalid") from exc


def parse_event_post(form: Mapping[str, str]) -> EventPost:
    """Validate a lifecycle event body (§4.9).

    Only the shape is settled here. Authenticity is decided by the handler in the order
    §4.9 mandates - idempotency by `ts`, then the constant-time `application_token`
    compare, then the admin proof - because none of these fields prove anything on
    their own (§4.1).
    """
    _check_body_size(form)
    expanded = expand_bracket_keys(form)
    flat = _ci_index(form)
    auth_raw = expanded.get("auth")
    auth: dict = auth_raw if isinstance(auth_raw, dict) else {}
    auth_index = _ci_index({k: v for k, v in auth.items() if isinstance(v, str)})

    def field(name: str) -> str | None:
        """`auth[...]` first, then the same name at the top level.

        Older cabinets put `application_token` and `member_id` beside `event` rather
        than inside `auth`, and both spellings appear in the captured flows.
        """
        value = _clean(auth_index.get(name.lower()))
        if value is None:
            value = _clean(flat.get(name.lower()))
        return value

    event = _require(_clean(flat.get("event")), "event")
    if not _EVENT_NAME_RE.match(event):
        raise FormValidationError("event", "malformed")
    # Case is preserved: the lifecycle events arrive upper-case but the settings events
    # of §4.5 are mixed-case (`OnAppSettingsInstall`), so handlers compare
    # case-insensitively rather than relying on a normalisation here.

    ts = _parse_int(_clean(flat.get("ts")), "ts", low=0, high=_MAX_TS)
    member_id = _match(field("member_id"), _MEMBER_ID_RE, "auth[member_id]")
    application_token = _match(
        field("application_token"), _APPLICATION_TOKEN_RE, "auth[application_token]"
    )
    access_token = _match(field("access_token"), _AUTH_TOKEN_RE, "auth[access_token]")
    refresh_token = _match(field("refresh_token"), _AUTH_TOKEN_RE, "auth[refresh_token]")
    client_endpoint = _validate_endpoint(field("client_endpoint"), "auth[client_endpoint]")
    domain = _validate_hostname(field("domain"), "auth[domain]", allow_port=True)

    scope = field("scope")
    if scope is not None:
        _parse_scopes(scope)  # same allowlist as APPLICATION_SCOPE; value kept verbatim

    data_raw = expanded.get("data")
    data: dict = data_raw if isinstance(data_raw, dict) else {}

    try:
        return EventPost(
            event=event,
            ts=ts,
            member_id=member_id,
            application_token=application_token,
            access_token=access_token,
            refresh_token=refresh_token,
            client_endpoint=client_endpoint,
            domain=domain,
            scope=scope,
            data=data,
        )
    except ValidationError as exc:  # defence in depth: never leak a pydantic dump
        raise FormValidationError("body", "invalid") from exc
