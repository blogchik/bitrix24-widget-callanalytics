"""§4.2 - the allowlist is the trust boundary, so every boundary gets a test.

`bitrix/forms.py` is the only thing standing between a Bitrix24-shaped POST and code
that treats its fields as facts (§4.1). Two properties are asserted throughout:

1. **A rejection is a `FormValidationError`, never a pydantic `ValidationError`.** §4.2
   requires a translated "bad request" page; a leaked pydantic dump would be an English
   stack trace inside the customer's iframe and a moderation rejection. The helper
   `rejects()` asserts the *exact* type, so a model field that starts validating before
   the allowlist does is caught here rather than in production.
2. **A rejection never quotes the offending value.** The message reaches logs and the
   rendered page, and the value is attacker-chosen.

No database, no network: this module is pure input parsing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from app.bitrix.forms import (
    MAX_BODY_BYTES,
    MAX_PLACEMENT_OPTIONS_BYTES,
    FormValidationError,
    expand_bracket_keys,
    is_event_body,
    parse_event_post,
    parse_iframe_post,
)
from tests.fixtures.bitrix import (
    APP_KEY,
    DOMAIN,
    SERVER_ENDPOINT,
    USER_AUTH,
    USER_REFRESH,
    install_form,
    install_query,
)

MEMBER_ID = "a223c6b3710f85df22e9377d6c4f7553"  # the documented sample, 32 lowercase hex


def form(**overrides: str | None) -> dict[str, str]:
    """A valid iframe POST with the named fields replaced (`None` removes the key)."""
    body = install_form(member_id=MEMBER_ID)
    for key, value in overrides.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    return body


def rejects(body: Mapping[str, str], *, field: str, query: Mapping[str, str] | None = None) -> None:
    """Assert the allowlist refuses this body with a clean, value-free error."""
    with pytest.raises(FormValidationError) as excinfo:
        parse_iframe_post(body, query or {}, "")
    error = excinfo.value
    # A pydantic ValidationError must never be what escapes (§4.2). Pydantic's own
    # error is not a FormValidationError, so pytest.raises above already proves it -
    # this second check catches the sloppier "wrap it but keep it as __cause__ and
    # re-raise the wrong type" refactor.
    assert not isinstance(error, ValidationError)
    assert error.field == field, f"expected the error to name {field!r}, got {error.field!r}"
    assert error.reason, "a rejection must carry a stable reason slug"
    for value in body.values():
        if len(value) >= 8:  # short values like "1" or "ru" appear in slugs legitimately
            assert value not in str(error), "the offending value must not reach the message"


# --- member_id: ^[0-9a-f]{32}$ ------------------------------------------------------


def test_member_id_accepts_exactly_32_lowercase_hex() -> None:
    parsed = parse_iframe_post(form(), {}, "")
    assert parsed.member_id == MEMBER_ID


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("31 chars", MEMBER_ID[:-1]),
        ("33 chars", MEMBER_ID + "0"),
        ("uppercase", MEMBER_ID.upper()),
        ("one uppercase char", MEMBER_ID[:-1] + "F"),
        ("non-hex letter", MEMBER_ID[:-1] + "z"),
        ("hex with a dash", MEMBER_ID[:8] + "-" + MEMBER_ID[9:]),
        ("empty", ""),
        ("leading space kept by a naive trim", " " + MEMBER_ID[1:]),
    ],
)
def test_member_id_boundaries_are_rejected(label: str, value: str) -> None:
    """WHY every neighbour of the regex: `member_id` is the tenant lookup key and the
    `portals_member_id_fmt` CHECK would turn a slipped-through value into a 500 (§3)."""
    rejects(form(member_id=value), field="member_id")


def test_member_id_is_required() -> None:
    with pytest.raises(FormValidationError) as excinfo:
        parse_iframe_post(form(member_id=None), {}, "")
    assert excinfo.value.field == "member_id"
    assert excinfo.value.reason == "missing"


# --- PROTOCOL ∈ {0,1} ---------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("0", False)])
def test_protocol_accepts_only_the_two_documented_values(raw: str, expected: bool) -> None:
    """§4.10 turns this into the CSP scheme, so `0` must really mean http."""
    assert parse_iframe_post(form(PROTOCOL=raw), {}, "").protocol_https is expected


@pytest.mark.parametrize("raw", ["2", "-1", "01", "true", "https", "1.0", "10", "0,1"])
def test_protocol_outside_zero_one_is_rejected(raw: str) -> None:
    rejects(form(PROTOCOL=raw), field="PROTOCOL")


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_protocol_absent_or_blank_defaults_to_https(raw: str | None) -> None:
    """Absent is the ordinary cloud case; only an explicit 0 relaxes the CSP (§4.10).
    Blank is treated as absent, not as a violation - a cabinet that sends the key with
    no value must still install."""
    assert parse_iframe_post(form(PROTOCOL=raw), {}, "").protocol_https is True


# --- AUTH_EXPIRES 1..86400 ----------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "3600", "86400"])
def test_auth_expires_inside_the_range_is_kept(raw: str) -> None:
    assert parse_iframe_post(form(AUTH_EXPIRES=raw), {}, "").auth_expires == int(raw)


@pytest.mark.parametrize("raw", ["0", "86401", "-1", "999999999"])
def test_auth_expires_out_of_range_is_rejected(raw: str) -> None:
    """§4.6 clamps the JWT to `min(AUTH_EXPIRES, 3600)`; a 10^9 here would be a
    year-long session if the clamp were ever reordered."""
    rejects(form(AUTH_EXPIRES=raw), field="AUTH_EXPIRES")


@pytest.mark.parametrize("raw", ["3600.0", "1e3", "abc", "٣٦٠٠"])
def test_auth_expires_non_ascii_digits_are_rejected(raw: str) -> None:
    """`str.isdigit()` accepts Arabic-Indic digits and `int()` converts them: the
    allowlist must use an explicit ASCII class or the portal's value is not the
    value we parsed."""
    rejects(form(AUTH_EXPIRES=raw), field="AUTH_EXPIRES")


# --- PLACEMENT and PLACEMENT_OPTIONS ------------------------------------------------


@pytest.mark.parametrize(
    "placement",
    ["DEFAULT", "LEFT_MENU", "CRM_DEAL_DETAIL_TAB", "CRM_LEAD_DETAIL_TAB",
     "CRM_CONTACT_DETAIL_TAB", "CRM_COMPANY_DETAIL_TAB"],
)
def test_every_allowlisted_placement_parses(placement: str) -> None:
    options = '{"ID":"1234"}' if placement.startswith("CRM_") else "{}"
    parsed = parse_iframe_post(form(PLACEMENT=placement, PLACEMENT_OPTIONS=options), {}, "")
    assert parsed.placement == placement


@pytest.mark.parametrize(
    "placement",
    [
        "CRM_QUOTE_DETAIL_TAB",   # a real Bitrix24 placement we did not bind
        "crm_deal_detail_tab",    # case matters: the router branches on the exact value
        "TASK_VIEW_TAB",
        "../DEFAULT",
        "<script>",
        "DEFAULT,LEFT_MENU",
    ],
)
def test_unknown_placement_is_rejected(placement: str) -> None:
    rejects(form(PLACEMENT=placement), field="PLACEMENT")


def test_absent_placement_means_default() -> None:
    """§4.2: the left-menu version-card option opens the handler with no PLACEMENT."""
    assert parse_iframe_post(form(PLACEMENT=None), {}, "").placement == "DEFAULT"


def test_crm_tab_requires_a_numeric_entity_id() -> None:
    """The forged-tab scenario of the design review: `ID` is entirely attacker-chosen
    and is fed to `crm.deal.get` and the `crm_contexts` key (§4.4 step 7)."""
    for bad in ['{"ID":"12; DROP TABLE calls"}', '{"ID":"abc"}', '{"ID":null}',
                '{"ID":true}', '{"ID":{"$gt":0}}', '{"ID":[1]}', '{"ID":""}', "{}"]:
        rejects(
            form(PLACEMENT="CRM_DEAL_DETAIL_TAB", PLACEMENT_OPTIONS=bad),
            field="PLACEMENT_OPTIONS",
        )


def test_crm_tab_id_is_normalised_to_an_int() -> None:
    """Bitrix24 sends the id as a JSON string; nothing downstream should re-parse it."""
    parsed = parse_iframe_post(
        form(PLACEMENT="CRM_LEAD_DETAIL_TAB", PLACEMENT_OPTIONS='{"ID":"4321","URI":"/x"}'), {}, ""
    )
    assert parsed.placement_options["ID"] == 4321
    assert isinstance(parsed.placement_options["ID"], int)


def test_crm_tab_with_no_placement_options_is_rejected() -> None:
    rejects(
        form(PLACEMENT="CRM_COMPANY_DETAIL_TAB", PLACEMENT_OPTIONS=None),
        field="PLACEMENT_OPTIONS",
    )


def test_oversized_placement_options_is_rejected_before_json_parsing() -> None:
    """§4.2 caps PLACEMENT_OPTIONS at 4 KB. The payload below is valid JSON, so a size
    check placed *after* `json.loads` would have already done the allocation."""
    oversized = json.dumps({"ID": 1, "PAD": "x" * (MAX_PLACEMENT_OPTIONS_BYTES + 512)})
    assert len(oversized.encode()) > MAX_PLACEMENT_OPTIONS_BYTES
    rejects(
        form(PLACEMENT="CRM_DEAL_DETAIL_TAB", PLACEMENT_OPTIONS=oversized),
        field="PLACEMENT_OPTIONS",
    )


@pytest.mark.parametrize("raw", ["not json", "[1,2,3]", '"a string"', "123", "null"])
def test_placement_options_must_be_a_json_object(raw: str) -> None:
    rejects(form(PLACEMENT="LEFT_MENU", PLACEMENT_OPTIONS=raw), field="PLACEMENT_OPTIONS")


# --- total body ≤ 64 KB -------------------------------------------------------------


def test_body_over_64kb_is_rejected() -> None:
    """§4.2's total-body cap. Built from few keys with large values so the *size* rule
    fires rather than the key-count guard - the two must be separately provable."""
    body = form()
    for index in range(9):
        body[f"PAD{index}"] = "x" * 8192
    assert sum(len(k) + len(v) for k, v in body.items()) > MAX_BODY_BYTES
    with pytest.raises(FormValidationError) as excinfo:
        parse_iframe_post(body, {}, "")
    assert excinfo.value.reason == "body_too_large"


def test_a_body_just_under_the_cap_still_parses() -> None:
    """The cap must not be so eager that a legitimately chatty cabinet is refused."""
    body = form()
    body["PAD"] = "x" * 32_000
    assert parse_iframe_post(body, {}, "").member_id == MEMBER_ID


def test_a_key_count_bomb_is_refused_rather_than_walked() -> None:
    """5,000 keys is not a body Bitrix24 sends; it is an attempt to make us do 5,000
    regex matches and 5,000 dict inserts before any size rule can help."""
    body = {f"F{index}": "1" for index in range(5000)}
    body.update(form())
    with pytest.raises(FormValidationError) as excinfo:
        parse_iframe_post(body, {}, "")
    assert excinfo.value.reason in {"too_many_fields", "body_too_large"}


# --- APPLICATION_SCOPE --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("crm,telephony,user_brief", {"crm", "telephony", "user_brief"}),
        ("crm telephony user_brief", {"crm", "telephony", "user_brief"}),
        ("crm, telephony,  user_brief", {"crm", "telephony", "user_brief"}),
        ("crm,,telephony", {"crm", "telephony"}),
        ("crm", {"crm"}),
        ("", set()),
    ],
)
def test_application_scope_splits_on_space_and_comma(raw: str, expected: set[str]) -> None:
    """§4.2: the iframe POST uses commas, event payloads use spaces, and at least one
    cabinet mixes them. Both separators are the documented reality, not a guess."""
    assert set(parse_iframe_post(form(APPLICATION_SCOPE=raw), {}, "").scopes) == expected


def test_application_scope_keeps_the_raw_string_too() -> None:
    """`portals.scope` stores what Bitrix24 said, not our normalisation (§3)."""
    parsed = parse_iframe_post(form(APPLICATION_SCOPE="crm telephony"), {}, "")
    assert parsed.application_scope == "crm telephony"


@pytest.mark.parametrize("raw", ["crm;telephony", "crm|telephony", "crm/../etc", "<crm>"])
def test_malformed_scope_names_are_rejected(raw: str) -> None:
    rejects(form(APPLICATION_SCOPE=raw), field="APPLICATION_SCOPE")


def test_a_scope_bomb_is_rejected() -> None:
    rejects(form(APPLICATION_SCOPE=",".join(f"s{i}" for i in range(500))), field="APPLICATION_SCOPE")


# --- PHP bracket expansion (§4.2, §4.9) ---------------------------------------------


def test_bracket_keys_expand_into_nested_dicts() -> None:
    expanded = expand_bracket_keys(
        {
            "event": "ONAPPUPDATE",
            "ts": "1780319382",
            "auth[member_id]": MEMBER_ID,
            "auth[application_token]": APP_KEY,
            "auth[client_endpoint]": "https://portal.bitrix24.test/rest/",
            "data[VERSION]": "3",
            "data[FIELDS][ID]": "778",
            "data[FIELDS][TYPE]": "DEAL",
        }
    )
    assert expanded["event"] == "ONAPPUPDATE"
    assert expanded["auth"]["member_id"] == MEMBER_ID
    assert expanded["auth"]["application_token"] == APP_KEY
    # The nested case is the one that matters: a one-level-only expander silently
    # produces a flat key "FIELDS][ID" and every downstream lookup misses (§4.9).
    assert expanded["data"]["FIELDS"]["ID"] == "778"
    assert expanded["data"]["FIELDS"]["TYPE"] == "DEAL"
    assert expanded["data"]["VERSION"] == "3"


def test_bracket_expansion_leaves_unbracketed_keys_alone() -> None:
    expanded = expand_bracket_keys({"event": "ONAPPUNINSTALL", "ts": "1"})
    assert expanded == {"event": "ONAPPUNINSTALL", "ts": "1"}


def test_unbalanced_brackets_are_treated_literally_not_guessed() -> None:
    """A malformed key must not be used to invent a structure a handler then trusts."""
    expanded = expand_bracket_keys({"auth[member_id": MEMBER_ID, "a]b[": "1"})
    assert expanded["auth[member_id"] == MEMBER_ID
    assert expanded["a]b["] == "1"


def test_a_depth_bomb_is_refused_rather_than_allocated() -> None:
    """`a[0][0][0]...` costs one dict per segment and the key text is attacker-chosen,
    so the walk is capped instead of being allowed to run to completion."""
    deep = "a" + "[x]" * 64
    with pytest.raises(FormValidationError) as excinfo:
        expand_bracket_keys({deep: "1"})
    assert excinfo.value.reason == "key_too_deep"


def test_a_depth_bomb_reaching_parse_event_post_is_also_refused() -> None:
    with pytest.raises(FormValidationError):
        parse_event_post({"event": "ONAPPINSTALL", "a" + "[x]" * 64: "1"})


def test_a_count_bomb_in_bracket_expansion_is_refused() -> None:
    """10,000 sibling keys: bounded by the key-count guard before any per-key work."""
    with pytest.raises(FormValidationError) as excinfo:
        expand_bracket_keys({f"data[{index}]": "1" for index in range(10_000)})
    assert excinfo.value.reason in {"too_many_fields", "body_too_large"}


def test_expansion_is_deterministic_when_a_scalar_and_a_container_collide() -> None:
    """`data=1` together with `data[X]=2` is odd, not hostile: it must resolve the same
    way every time rather than raising on a body a real cabinet might send."""
    expanded = expand_bracket_keys({"data": "1", "data[X]": "2"})
    assert isinstance(expanded["data"], dict)
    assert expanded["data"]["X"] == "2"


# --- is_event_body (§4.2 dispatch) --------------------------------------------------


def test_is_event_body_detects_a_lifecycle_post_on_the_install_url() -> None:
    """Some cabinets deliver lifecycle events to `/install/`; §4.2 dispatches on this
    BEFORE the placement allowlist runs, so it must be true for an event body..."""
    assert is_event_body({"event": "ONAPPUNINSTALL", "auth[member_id]": MEMBER_ID}) is True
    assert is_event_body({"EVENT": "ONAPPINSTALL"}) is True


def test_is_event_body_is_false_for_an_ordinary_iframe_post() -> None:
    """...and false for the ordinary open, or every install would be routed to /events/."""
    assert is_event_body(form()) is False
    assert is_event_body({}) is False
    assert is_event_body({"event": ""}) is False
    assert is_event_body({"event": "   "}) is False


def test_is_event_body_never_raises() -> None:
    """It is a dispatch decision: it must not be able to fail on a hostile body."""
    hostile: dict[Any, Any] = {"event": None, 7: "x", "a" * 5000: "b" * 5000}
    assert is_event_body(hostile) in {True, False}


# --- raw_query survives verbatim (§4.4 step 8) --------------------------------------


def test_raw_query_is_carried_through_byte_for_byte() -> None:
    """`handoff.html` forwards this string; without `APP_SID` the BX24 SDK never
    initialises and `fitWindow`/`openPath`/`getAuth` stay inert (§4.4 step 8)."""
    query = install_query()
    parsed = parse_iframe_post(form(), {}, query)
    assert parsed.raw_query == query
    assert "APP_SID=b8b3a9e1c7d24f0a" in parsed.raw_query


def test_raw_query_keeps_unknown_parameters_and_ordering() -> None:
    """We do not get to decide which parameters the SDK needs, so nothing is dropped
    and nothing is re-ordered - it is forwarded, not rebuilt."""
    query = "DOMAIN=portal.bitrix24.test&PROTOCOL=1&LANG=ru&APP_SID=abc123&FUTURE_FLAG=7"
    assert parse_iframe_post(form(), {}, query).raw_query == query


def test_raw_query_leading_question_mark_is_dropped_but_nothing_else_is() -> None:
    query = install_query()
    assert parse_iframe_post(form(), {}, "?" + query).raw_query == query


@pytest.mark.parametrize(
    "query",
    [
        'DOMAIN=x"onload=alert(1)',
        "APP_SID=<script>",
        "LANG=ru\nSet-Cookie: a=b",
        "A=" + "x" * 8192,
    ],
)
def test_a_query_string_that_could_break_out_of_the_handoff_page_is_rejected(query: str) -> None:
    """The value is interpolated into `handoff.html`; a real Bitrix24 query string is
    percent-encoded, so none of these can be legitimate."""
    with pytest.raises(FormValidationError) as excinfo:
        parse_iframe_post(form(), {}, query)
    assert excinfo.value.field == "query_string"


# --- fields the handler reads from either the body or the URL -----------------------


def test_url_parameters_fill_in_for_a_body_that_omits_them() -> None:
    """Bitrix24 repeats DOMAIN/PROTOCOL/LANG/APP_SID on the handler URL; a cabinet that
    sends them only there must still produce a usable CSP domain (§4.10)."""
    parsed = parse_iframe_post(
        form(DOMAIN=None, LANG=None, APP_SID=None),
        {"DOMAIN": DOMAIN, "LANG": "en", "APP_SID": "sid-1"},
        "",
    )
    assert parsed.domain == DOMAIN
    assert parsed.lang == "en"
    assert parsed.app_sid == "sid-1"


@pytest.mark.parametrize(
    "domain",
    [
        "https://portal.bitrix24.test",   # a scheme is not a hostname
        "portal.bitrix24.test/rest/",     # nor is a path
        "portal.bitrix24.test:0",
        "portal.bitrix24.test:99999",
        "portal.bitrix24.test x",
        "'; frame-ancestors *",
        "",
    ],
)
def test_domain_that_is_not_a_bare_hostname_is_rejected(domain: str) -> None:
    """`DOMAIN` is display/CSP data only (§4.1) and lands in a `frame-ancestors`
    directive, so it is rejected here rather than escaped there."""
    rejects(form(DOMAIN=domain), field="DOMAIN")


def test_on_premise_host_with_a_port_is_accepted() -> None:
    """Ordinary on-premise portals are in scope (assumption 20) and use odd ports."""
    parsed = parse_iframe_post(form(DOMAIN="b24.intranet.local:8443", PROTOCOL="0"), {}, "")
    assert parsed.domain == "b24.intranet.local:8443"
    assert parsed.protocol_https is False


def test_an_empty_refresh_id_reaches_the_handler_as_none() -> None:
    """§4.3 step 2 routes an empty REFRESH_ID to `unsupported_portal` WITHOUT touching a
    row - so it is a supported state, not an allowlist violation."""
    parsed = parse_iframe_post(form(REFRESH_ID=""), {}, "")
    assert parsed.refresh_id is None
    assert parsed.auth_id == USER_AUTH


@pytest.mark.parametrize("token", ["short", "with spaces in it", "tok;en", "a" * 600])
def test_malformed_auth_and_refresh_tokens_are_rejected(token: str) -> None:
    rejects(form(AUTH_ID=token), field="AUTH_ID")
    rejects(form(REFRESH_ID=token), field="REFRESH_ID")


@pytest.mark.parametrize("token", ["short", "has-a-dash", "has.a.dot", "a" * 200])
def test_malformed_application_token_is_rejected(token: str) -> None:
    """`[A-Za-z0-9]{8,128}` (§4.2): this value is constant-time compared against every
    inbound event (§4.9 rule 2), so its shape is pinned rather than inferred."""
    rejects(form(APPLICATION_TOKEN=token), field="APPLICATION_TOKEN")


def test_server_endpoint_shape_is_checked_but_the_host_decision_is_not_made_here() -> None:
    """§4.1 keeps the OAUTH_HOST_ALLOWLIST decision in `oauth.py`; forms only rejects
    values that are not URLs at all, so an attacker-chosen *valid* URL still parses and
    is refused later by the allowlist that can actually see the configuration."""
    parsed = parse_iframe_post(form(SERVER_ENDPOINT="https://evil.example.com/rest/"), {}, "")
    assert parsed.server_endpoint == "https://evil.example.com/rest/"
    rejects(form(SERVER_ENDPOINT="javascript:alert(1)"), field="SERVER_ENDPOINT")
    rejects(form(SERVER_ENDPOINT="oauth.bitrix.info/rest/"), field="SERVER_ENDPOINT")


def test_a_valid_install_body_round_trips_every_field() -> None:
    """The happy path, so the negative tests above cannot be passing vacuously."""
    parsed = parse_iframe_post(form(), {}, install_query())
    assert parsed.member_id == MEMBER_ID
    assert parsed.domain == DOMAIN
    assert parsed.refresh_id == USER_REFRESH
    assert parsed.application_token == APP_KEY
    assert parsed.server_endpoint == SERVER_ENDPOINT
    assert parsed.status == "F"
    assert parsed.lang == "ru"
    assert parsed.auth_expires == 3600
    assert parsed.placement == "DEFAULT"


# --- event bodies (§4.9) ------------------------------------------------------------


def test_event_post_parses_a_documented_onappuninstall() -> None:
    parsed = parse_event_post(
        {
            "event": "ONAPPUNINSTALL",
            "ts": "1780319382",
            "data[LANGUAGE_ID]": "ru",
            "data[CLEAN]": "1",
            "auth[domain]": DOMAIN,
            "auth[client_endpoint]": "https://portal.bitrix24.test/rest/",
            "auth[server_endpoint]": SERVER_ENDPOINT,
            "auth[member_id]": MEMBER_ID,
            "auth[application_token]": APP_KEY,
        }
    )
    assert parsed.event == "ONAPPUNINSTALL"
    assert parsed.ts == 1780319382
    assert parsed.member_id == MEMBER_ID
    assert parsed.application_token == APP_KEY
    assert parsed.data["CLEAN"] == "1"
    # ONAPPUNINSTALL carries no access token: API access is already revoked (§4.9 r3).
    assert parsed.access_token is None


def test_event_post_preserves_mixed_case_event_names() -> None:
    """`OnAppSettingsInstall` (§4.5) and `ONAPPUPDATE` both arrive; normalising here
    would force every handler comparison to guess which spelling it was given."""
    assert parse_event_post({"event": "OnAppSettingsChange"}).event == "OnAppSettingsChange"


def test_event_post_accepts_the_older_flat_spelling() -> None:
    """Some cabinets put member_id/application_token beside `event`, not inside auth."""
    parsed = parse_event_post(
        {"event": "ONAPPINSTALL", "member_id": MEMBER_ID, "application_token": APP_KEY}
    )
    assert parsed.member_id == MEMBER_ID
    assert parsed.application_token == APP_KEY


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"ts": "1"}, "event"),
        ({"event": "ON APP INSTALL"}, "event"),
        ({"event": "A" * 200}, "event"),
        ({"event": "ONAPPINSTALL", "ts": "99999999999999"}, "ts"),
        ({"event": "ONAPPINSTALL", "ts": "-1"}, "ts"),
        ({"event": "ONAPPINSTALL", "auth[member_id]": MEMBER_ID.upper()}, "auth[member_id]"),
        ({"event": "ONAPPINSTALL", "auth[client_endpoint]": "not a url"}, "auth[client_endpoint]"),
    ],
)
def test_malformed_event_bodies_raise_form_validation_error(
    body: dict[str, str], field: str
) -> None:
    with pytest.raises(FormValidationError) as excinfo:
        parse_event_post(body)
    assert not isinstance(excinfo.value, ValidationError)
    assert excinfo.value.field == field


def test_no_parser_entry_point_leaks_a_pydantic_validation_error() -> None:
    """The single most important property of this module (§4.2): a hostile body of any
    shape produces the translated bad-request page, never an English pydantic dump."""
    hostile: list[dict[str, str]] = [
        {},
        {"member_id": "x"},
        {"event": "\x00"},
        {"member_id": MEMBER_ID, "DOMAIN": DOMAIN, "PLACEMENT_OPTIONS": "{" * 200},
        {"member_id": MEMBER_ID, "DOMAIN": DOMAIN, "AUTH_EXPIRES": "9" * 40},
    ]
    for body in hostile:
        for call in (
            lambda b=body: parse_iframe_post(b, {}, ""),
            lambda b=body: parse_event_post(b),
            lambda b=body: expand_bracket_keys(b),
        ):
            try:
                call()
            except FormValidationError:
                pass
            except ValidationError as exc:  # pragma: no cover - the failure this test exists for
                pytest.fail(f"pydantic ValidationError escaped for {body!r}: {exc}")
