"""§4.4 — `POST /app/` and `POST /settings/`, restricted to the branches that carry risk.

The happy path of an open is boring: one batch, one page. What is not boring is what
the handler does when the payload is *unproven*, when the network is broken, or when
the opener is not who the stored credential belongs to. Those are the branches here,
and each of them exists because getting it wrong is expensive rather than ugly:

1. **Unknown portal, no `REFRESH_ID`** — the only thing the POST proves is that someone
   knows a `member_id`, which is public (decision 1). §4.4 step 2 renders a state page
   and writes nothing; a row created here would be a tenant conjured out of a form.
2. **Self-heal by a non-admin** — the self-heal path writes the *portal* credential, and
   decision 4 says a regular employee's token silently caches only that employee's
   calls. So the admin proof of §4.3 step 3 runs inside the self-heal too.
3. **Transport failure at the stored endpoint** — the one automatic cure for a renamed
   portal (§4.4 step 4). Exactly one exchange and exactly one retry: a loop here is an
   OAuth hammer, and Bitrix24 blocks applications for excessive refresh.
4. **A routine open by a healthy admin performs zero exchanges** (§4.4 step 6). This is
   the review finding that "a refresh exchange on every open by the installing admin"
   gets an app blocked. It is asserted as a *count*, because the only way to keep this
   true is to count.
5. **Placement routing** — six placements, three targets, and one of them is
   administrators-only. A placement that lands on the wrong page is invisible in code
   review and instantly visible to a moderator.
6. **A CRM tab whose CRM command errored** — the [MINOR] review finding: without this
   branch, a user with no rights to a deal gets a JWT for it and is served the context
   a privileged colleague resolved yesterday.

Everything runs through the real FastAPI app over `httpx.ASGITransport` against the
shared Bitrix24 fake, and "writes nothing" is asserted as a full row snapshot.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn
from app.handlers.render import STATE_KINDS
from app.i18n import LOCALES, has_message, t
from app.main import create_app
from app.security.session_token import SessionClaims, verify_session
from tests.fixtures.bitrix import (
    CLIENT_ENDPOINT,
    Err,
    FakeBitrix,
    delete_portal,
    install_form,
    install_query,
    new_member_id,
    patch_httpx,
    portal_snapshot,
    seed_portal,
    token_response,
)

#: Where a renamed portal moves to. Deliberately a different host from `CLIENT_ENDPOINT`
#: so "the retry went to the new endpoint" is observable, not assumed.
RENAMED_ENDPOINT: Final[str] = "https://renamed.bitrix24.test/rest/"

CRM_PLACEMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("CRM_DEAL_DETAIL_TAB", "DEAL"),
    ("CRM_LEAD_DETAIL_TAB", "LEAD"),
    ("CRM_CONTACT_DETAIL_TAB", "CONTACT"),
    ("CRM_COMPANY_DETAIL_TAB", "COMPANY"),
)

ENTITY_ID: Final[int] = 5


# --- helpers -------------------------------------------------------------------------


def assert_state(response: httpx.Response, *kinds: str) -> None:
    """Assert the request ended in one of the `kinds` states (§4.11).

    §4.4 delivers a state two different ways, and both are correct:

    * **server-rendered** `state.html`, for everything decided before the SPA can be
      trusted with the request at all (bad request, not installed, admin only, retry);
    * **a handoff to the SPA's own `/state/<kind>` route with no token**, which is what
      the §4.4 step 8 table specifies for `denied` and `crm_no_access` - those pages
      belong inside the app shell (the denied copy links to the portal's own call
      statistics) but must carry no session.

    A server-rendered page is matched by its translated title in ANY shipped locale,
    through `app.i18n.t`, so the assertion exercises the single message source of §8
    rather than a copy of the copy. Several kinds are accepted where §4.4 and the §4.11
    table name the same situation differently (the non-admin self-heal is "ask your
    administrator to open the app once", which is verbatim `state.not_installed`).
    """
    titles: set[str] = set()
    for kind in kinds:
        assert kind in STATE_KINDS, f"render.py does not define the {kind!r} state"
        key = f"state.{kind}.title"
        assert has_message(key), (
            f"the shared catalogue (§8) has no {key!r}, so render_state() falls back to "
            "the generic error copy and no state page is distinguishable."
        )
        titles.update(t(locale, key) for locale in LOCALES)

    rendered = any(title in response.text for title in titles)
    routed = any(f"/state/{kind}" in response.text for kind in kinds)
    assert rendered or routed, (
        f"expected one of the {list(kinds)} states, either server-rendered or as a "
        "token-less handoff to /state/<kind>; got a different page."
    )
    assert "no-store" in response.headers.get("cache-control", "").lower()
    assert "x-frame-options" not in {key.lower() for key in response.headers}


def assert_no_session_token(response: httpx.Response) -> None:
    """A state page must not carry a session token (§4.4 step 5: "mints no entity JWT")."""
    assert "#s=" not in response.text, "a state page must never hand out a session token"


def handoff_token(response: httpx.Response) -> SessionClaims:
    """Read and verify the JWT out of a handoff page (§4.4 step 8)."""
    assert response.status_code == 200, f"expected a handoff page, got {response.status_code}"
    marker = "#s="
    assert marker in response.text, (
        "no `#s=` fragment assignment in the body: §4.4 step 8 delivers the session "
        "token in the URL fragment and nowhere else."
    )
    match = re.search(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", response.text)
    assert match is not None, "the handoff page carries no JWT"
    return verify_session(match.group(0))


def assert_target(response: httpx.Response, expected: str, *forbidden: str) -> None:
    """The SPA path the handoff redirects to (§4.4 step 8 table)."""
    assert expected in response.text, f"the handoff target is not {expected!r}"
    for other in forbidden:
        assert other not in response.text, (
            f"the handoff target names {other!r} as well as {expected!r}"
        )


def crm_answers(fake: FakeBitrix, *, activity_error: Err | None = None) -> FakeBitrix:
    """Realistic CRM answers for the §4.4 step 4 commands of a CRM tab."""
    fake.on(
        "crm.deal.get",
        {"ID": str(ENTITY_ID), "TITLE": "Deal", "CONTACT_ID": "12", "COMPANY_ID": "3"},
    )
    fake.on("crm.deal.contact.items.get", [{"CONTACT_ID": "12"}])
    fake.on(
        "crm.activity.list",
        activity_error if activity_error is not None else [{"ID": "1001"}, {"ID": "1002"}],
    )
    return fake


def crm_body(member_id: str, placement: str, *, entity_id: int = ENTITY_ID) -> dict[str, str]:
    """A CRM tab open: §4.2 requires a numeric `ID` in `PLACEMENT_OPTIONS`."""
    body = install_form(member_id=member_id, placement=placement)
    body["PLACEMENT_OPTIONS"] = json.dumps({"ID": str(entity_id)})
    return body


async def portal_event_kinds(portal_id: int) -> list[str]:
    async with control_txn() as session:
        rows = (
            await session.execute(
                text("SELECT kind FROM portal_events WHERE portal_id = :pid ORDER BY id"),
                {"pid": portal_id},
            )
        ).scalars()
    return list(rows)


def boom(_: Any) -> Any:
    """A DNS/TLS/connect failure at the stored endpoint (§4.4 step 4)."""
    raise httpx.ConnectError("simulated: the portal was renamed")


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


@pytest.fixture()
async def cleanup(app_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """Member ids to purge, including ones a test expects NOT to have been created."""
    member_ids: list[str] = []
    try:
        yield member_ids
    finally:
        for member_id in member_ids:
            await delete_portal(member_id)


async def open_app(
    client: httpx.AsyncClient, body: dict[str, str], *, path: str = "/app/"
) -> httpx.Response:
    return await client.post(f"{path}?{install_query()}", data=body)


# --- 1. an unknown portal with no REFRESH_ID ----------------------------------------


async def test_an_unknown_portal_without_a_refresh_id_renders_not_installed_and_writes_nothing(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.4 step 2: "Refresh failure or empty `REFRESH_ID` → `/state/not_installed`."

    There is nothing here to prove anything with: no stored credential to call, and no
    refresh token to exchange. `DOMAIN` is display data only (§4.1), so contacting it
    would be inventing trust; creating a row would be inventing a tenant.
    """
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = FakeBitrix()  # nothing scripted: ANY call is a failure
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=member_id, refresh_id=None))

    assert response.status_code < 500
    assert fake.oauth_count == 0, "an empty REFRESH_ID cannot be exchanged, so do not try"
    assert fake.rest_count == 0, "and nothing may be contacted at DOMAIN (§4.1)"
    assert await portal_snapshot(member_id) is None, "no row may be conjured from a form post"
    assert_state(response, "not_installed")
    assert_no_session_token(response)


# --- 2. a self-heal attempted by a non-administrator --------------------------------


async def test_a_non_admin_self_heal_writes_nothing(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.4 step 2: the self-heal runs §4.3 steps 3-5 "including the `user.admin` proof -
    a non-admin self-heal renders 'ask your administrator to open the app once' and
    writes nothing".

    Decision 4 is the reason: whatever credential this path stores becomes the sync
    worker's token forever. A regular employee's token returns only that employee's
    calls, so every dashboard in the portal would be quietly, permanently incomplete -
    and the moderator would see plausible numbers rather than an error.
    """
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=member_id))
    fake.on("user.admin", False)
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=member_id))

    assert response.status_code == 200, "the moderator must read the explanation in-frame"
    assert await portal_snapshot(member_id) is None, "a non-admin creates no tenant at all"
    assert fake.oauth_count == 1, "one exchange proves the member_id; there is no second one"
    assert fake.rest_count == 1, "and exactly one proof batch follows it - nothing more"
    # §4.11 calls this "not installed yet" for the DB-restored case and §4.4 calls it
    # "ask your administrator", which is verbatim the body of state.not_installed.
    assert_state(response, "not_installed", "admin_only")
    assert_no_session_token(response)


async def test_a_self_heal_by_an_admin_rebuilds_the_portal(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The control for the refusal above: the same POST from an administrator must
    actually rebuild the tenant, or test 2 would pass on a handler that never
    self-heals at all (§4.11, "Opens with no portal row (DB restored)")."""
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=member_id))
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=member_id))

    assert response.status_code == 200
    row = await portal_snapshot(member_id)
    assert row is not None, "an admin self-heal must recreate the portal (§4.4 step 2)"
    assert row["status"] == "active"
    assert row["client_endpoint"] == CLIENT_ENDPOINT, "from the OAuth response, never DOMAIN"
    assert fake.oauth_count == 1


# --- 3. the stored endpoint stopped resolving ---------------------------------------


async def test_a_transport_failure_triggers_one_exchange_one_retry_and_relearns_the_endpoint(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.4 step 4: "Transport-level failure ... → one refresh exchange ..., take
    `client_endpoint` (and `domain`) from the response, `portal_events(domain_changed)`,
    then retry the batch **once**."

    This is the only automatic cure for a renamed portal or a newly connected custom
    domain, and it is also the easiest place in the app to write an infinite loop: the
    retry can fail the same way. The assertions are therefore counts - one exchange,
    two batch attempts - not just "it worked in the end".
    """
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=seeded.member_id, client_endpoint=RENAMED_ENDPOINT))
    fake.on("batch", boom, None)  # first batch dies in transport, the rest answer normally
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))

    assert fake.oauth_count == 1, (
        "exactly one refresh exchange (§4.4 step 4); more than one is the OAuth hammer "
        "Bitrix24 blocks applications for."
    )
    batches = fake.of_kind("batch")
    dead = [b for b in batches if str(b.url).startswith(CLIENT_ENDPOINT)]
    moved = [b for b in batches if str(b.url).startswith(RENAMED_ENDPOINT)]
    assert len(dead) == 1, (
        f"the unreachable endpoint must be tried once, never in a loop; saw {len(dead)} "
        "attempts against it"
    )
    assert moved, "the retry must go to the endpoint the exchange returned"
    assert set(moved[0].commands) == set(dead[0].commands), (
        "the retry must be the SAME batch (§4.4 step 4 retries it once), not a narrower "
        f"one: {sorted(moved[0].commands)} vs {sorted(dead[0].commands)}"
    )
    # A third batch is legitimate and only that: §4.4 step 6 proves `user.admin` for the
    # freshly exchanged token before `store_portal_credential()` may persist the move.
    assert len(batches) <= 3, f"one attempt, one retry, one admin proof; saw {len(batches)}"

    row = await portal_snapshot(seeded.member_id)
    assert row is not None
    assert row["client_endpoint"] == RENAMED_ENDPOINT, "the new base must be persisted"
    assert "domain_changed" in await portal_event_kinds(seeded.portal_id), (
        "§4.4 step 4 records the move in portal_events - it is the first thing support "
        "looks at when a portal stops syncing"
    )
    handoff_token(response)  # and the user still gets their app


async def test_a_transport_failure_that_survives_the_retry_renders_retry(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """"Still failing → `/state/retry`" - never a blank frame, never a stack trace
    (§4.11), and still exactly one exchange."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=seeded.member_id, client_endpoint=RENAMED_ENDPOINT))
    fake.on("batch", boom)  # the last entry repeats: every attempt dies
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))

    assert response.status_code < 500
    assert fake.oauth_count == 1, "one exchange, even when the retry also fails"
    assert len(fake.of_kind("batch")) == 2, "and no attempt beyond the single documented retry"
    assert_state(response, "retry")
    assert_no_session_token(response)


# --- 4. the routine open (§4.4 step 6) ----------------------------------------------


async def test_a_routine_admin_open_performs_no_oauth_exchange_at_all(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.4 step 6: "**Token re-seed is not routine.**"

    The design review lists "a refresh exchange on every open by the installing admin"
    as a defect to fix before milestone 1 ends, because Bitrix24 blocks applications
    that refresh on a schedule - and the installing admin opening the app is a
    schedule. The portal here is healthy in every way step 6 tests: `token_status='ok'`
    and `token_refreshed_at` one day old, far inside TOKEN_RESEED_AFTER_DAYS.
    """
    seeded = await seed_portal(status="active", token_status="ok")
    cleanup.append(seeded.member_id)
    before = await portal_snapshot(seeded.member_id)
    assert before is not None

    fake = FakeBitrix()
    fake.on_oauth(Err("invalid_grant", "this exchange must never happen"))
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))

    assert fake.oauth_count == 0, (
        "a healthy portal opened by an admin must not refresh anything (§4.4 step 6)"
    )
    assert fake.rest_count == 1, "one batch at the stored endpoint is the whole round trip budget"

    after = await portal_snapshot(seeded.member_id)
    assert after is not None
    assert after["access_token_enc"] == before["access_token_enc"], "the credential is untouched"
    assert after["refresh_token_enc"] == before["refresh_token_enc"]
    assert after["token_version"] == before["token_version"], "no re-seed means no version bump"
    assert after["last_opened_at"] is not None, "housekeeping still runs (§4.4 step 6)"

    claims = handoff_token(response)
    assert claims.acc == "all", "an administrator always has full telephony access (§4.7)"
    assert claims.adm is True


async def test_a_non_admin_open_probes_once_and_lands_on_own(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.4 steps 4-5 for the other kind of opener: the probe runs, it answers, and the
    JWT records `own` - the level the whole `scope_filter` story depends on."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    fake.on("user.admin", False)
    fake.on("voximplant.statistic.get", [])
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))

    assert fake.oauth_count == 0, "a non-admin open re-seeds nothing either (§4.4 step 6)"
    claims = handoff_token(response)
    assert claims.acc == "own"
    assert claims.adm is False


async def test_a_probe_that_is_refused_renders_the_mandated_no_access_state(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.11: "Opens as non-admin without 'Call statistics - view' → mandated 'ask your
    administrator' text". The Marketplace review requires that sentence, so the open
    must end in the `denied` state rather than an empty dashboard."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    fake.on("user.admin", False)
    fake.on("voximplant.statistic.get", Err("ACCESS_DENIED", "Access denied"))
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))

    assert response.status_code == 200
    # §4.4's routing table sends this to `/state/denied` with no JWT; §4.7 describes a
    # `acc='denied'` principal that `GET /me` still answers. Both readings are honest,
    # and the invariant that matters is the same either way: this user must never be
    # handed a token that says `all` or `own`.
    if "#s=" in response.text:
        assert handoff_token(response).acc == "denied"
    else:
        assert_state(response, "denied")


# --- 5. placement routing (§4.4 step 8 table) ---------------------------------------


@pytest.mark.parametrize("placement", ["DEFAULT", "LEFT_MENU"])
async def test_the_menu_placements_route_to_the_dashboard(
    client: httpx.AsyncClient, cleanup: list[str], placement: str
) -> None:
    """"`DEFAULT`, `LEFT_MENU` → `/dashboard?<original query>#s=<jwt>`; both values
    accepted." `LEFT_MENU` is never bound by us (§4.3 step 5), but the version card's
    menu entry opens the handler with either value depending on the cabinet."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await open_app(
            client, install_form(member_id=seeded.member_id, placement=placement)
        )

    claims = handoff_token(response)
    # §4.6 records the placement in `plc`; §4.4 accepts either spelling for this page,
    # so a handler that normalises LEFT_MENU to DEFAULT is equally correct.
    assert claims.plc in {placement, "DEFAULT"}, "the JWT records the placement (§4.6)"
    assert claims.ent is None, "a menu placement has no CRM entity"
    assert_target(response, "/dashboard", "/crm", "/settings")


@pytest.mark.parametrize(("placement", "entity_type"), CRM_PLACEMENTS)
async def test_every_crm_tab_routes_to_the_crm_view_with_its_entity(
    client: httpx.AsyncClient, cleanup: list[str], placement: str, entity_type: str
) -> None:
    """"`CRM_*_DETAIL_TAB` → `/crm?<original query>#s=<jwt>` with `ent={t,id}`."

    The entity reaches the SPA only inside the signed token: §4.1 forbids any endpoint
    from accepting an entity id from the client, because the id is what selects the
    cached `crm_contexts` row.
    """
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = crm_answers(FakeBitrix())
    with patch_httpx(fake):
        response = await open_app(client, crm_body(seeded.member_id, placement))

    claims = handoff_token(response)
    assert claims.plc == placement
    assert claims.ent is not None, "a CRM tab must mint an entity JWT (§4.4 step 8)"
    assert claims.ent["t"] == entity_type
    assert int(claims.ent["id"]) == ENTITY_ID
    assert_target(response, "/crm", "/dashboard", "/settings")


async def test_settings_lands_on_the_settings_page_for_an_admin(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.5: "Same handler as `/app/`; lands on `/settings` for admins"."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await open_app(
            client, install_form(member_id=seeded.member_id), path="/settings/"
        )

    handoff_token(response)
    assert_target(response, "/settings", "/dashboard", "/crm")


async def test_settings_refuses_a_non_admin_with_the_administrators_only_state(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.5 / §4.11: ""administrators only" state for others".

    The settings page shows the token owner, the sync state and a Re-authorize button
    that re-seeds the portal credential - so it is an admin surface, and the refusal is
    a rendered page rather than a 403 because the frame must explain itself.
    """
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    fake.on("user.admin", False)
    with patch_httpx(fake):
        response = await open_app(
            client, install_form(member_id=seeded.member_id), path="/settings/"
        )

    assert response.status_code == 200
    assert_state(response, "admin_only")
    assert_no_session_token(response)


async def test_an_unknown_placement_is_a_bad_request_not_a_blank_frame(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.2 / §4.4 table: "unknown (fails allowlist) → state.html bad request"."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await open_app(
            client, install_form(member_id=seeded.member_id, placement="CRM_INVOICE_DETAIL_TAB")
        )

    assert response.status_code == 400
    assert fake.rest_count == 0, "an unallowed placement is rejected before any REST call"
    assert_state(response, "bad_request")
    assert_no_session_token(response)


# --- 6. a CRM tab the opener may not see --------------------------------------------


@pytest.mark.parametrize(("placement", "entity_type"), CRM_PLACEMENTS)
async def test_a_failed_crm_command_renders_crm_no_access_and_mints_no_entity_jwt(
    client: httpx.AsyncClient, cleanup: list[str], placement: str, entity_type: str
) -> None:
    """§4.4 step 5: "if any CRM command returned an error, render `/state/crm_no_access`
    and mint **no** entity JWT - a cached context must never be served to a user who
    cannot see the entity."

    The review's [MINOR] finding spells out the leak: `crm_contexts` is keyed per
    portal, not per user, so a manager with no rights to deal 5 who still receives
    `ent={DEAL,5}` is served the contact ids and activity ids a privileged colleague
    resolved yesterday. `scope_filter` keeps the *calls* to their own, so what leaks is
    the contact-to-deal association - which is exactly the thing a CRM permission
    protects.
    """
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    fake = crm_answers(FakeBitrix(), activity_error=Err("ACCESS_DENIED", "Access denied"))
    with patch_httpx(fake):
        response = await open_app(client, crm_body(seeded.member_id, placement))

    assert response.status_code == 200, "the frame explains itself (§4.11)"
    assert_state(response, "crm_no_access")
    assert_no_session_token(response)
    assert "eyJ" not in response.text, (
        "no token of any kind may be minted for an entity the opener cannot read"
    )


async def test_a_crm_tab_without_a_numeric_id_is_a_bad_request(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.2: `PLACEMENT_OPTIONS` must be "valid JSON ≤ 4 KB with numeric `ID` when a CRM
    tab". The id becomes the key of a cached row, so a non-numeric one is not a value to
    coerce - it is a request to refuse."""
    seeded = await seed_portal(status="active")
    cleanup.append(seeded.member_id)

    body = install_form(member_id=seeded.member_id, placement="CRM_DEAL_DETAIL_TAB")
    body["PLACEMENT_OPTIONS"] = json.dumps({"ID": "not-a-number"})

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await open_app(client, body)

    assert response.status_code == 400
    assert fake.rest_count == 0
    assert_state(response, "bad_request")


# --- 7. the open must not starve the worker's daily `user.admin` --------------------


async def portal_row(portal_id: int) -> dict[str, Any]:
    async with control_txn() as session:
        row = (
            await session.execute(
                text(
                    "SELECT p.token_admin_verified_at, s.last_appinfo_at "
                    "FROM portals p JOIN portal_sync s ON s.portal_id = p.id "
                    "WHERE p.id = :pid"
                ),
                {"pid": portal_id},
            )
        ).mappings().one()
    return dict(row)


async def test_a_daily_admin_open_still_leaves_the_worker_its_user_admin_reverification(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§5.8 "Daily admin re-verification", and why it must not share a clock with §4.4.

    The worker re-runs `user.admin` on the STORED credential once a day because that is
    the only thing that notices the installer being demoted or dismissed: the token keeps
    answering HTTP 200 and `voximplant.statistic.get` quietly narrows to that one
    person's calls - nothing fails, nothing is logged, and the dashboard just shows less
    than the truth (architecture.md:260, :685).

    The `/app/` open also runs `app.info` once a day (§4.4 step 4) and stamps
    `portal_sync.last_appinfo_at`. That call is made with the *opener's* token and proves
    nothing about the stored credential, so it must not be able to satisfy the worker's
    gate. On a portal opened during working hours it otherwise does, every day.
    """
    from app.jobs.definitions import sync_portal
    from app.sync.lease import WORKER_ID, acquire_leases

    seeded = await seed_portal(status="active", token_status="ok")
    cleanup.append(seeded.member_id)
    async with control_txn() as session:
        # A portal that has been installed for a while: `installed_flag` is true, so the
        # open's `app.info` is the once-a-day one, and both daily jobs are due.
        await session.execute(
            text(
                "UPDATE portals SET installed_flag = true, "
                "token_admin_verified_at = now() - interval '25 hours' WHERE id = :pid"
            ),
            {"pid": seeded.portal_id},
        )
        await session.execute(
            text(
                "UPDATE portal_sync SET last_appinfo_at = now() - interval '25 hours', "
                "next_run_at = now() - interval '1 second' WHERE portal_id = :pid"
            ),
            {"pid": seeded.portal_id},
        )
    before = await portal_row(seeded.portal_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await open_app(client, install_form(member_id=seeded.member_id))
        handoff_token(response)  # a real, successful admin open

        opened = await portal_row(seeded.portal_id)
        assert opened["last_appinfo_at"] > before["last_appinfo_at"], (
            "this test is only meaningful if the open really ran `app.info` and stamped "
            "the shared column (§4.4 step 4)"
        )
        assert opened["token_admin_verified_at"] == before["token_admin_verified_at"], (
            "an open proves nothing about the stored credential"
        )

        leased = [f for f in await acquire_leases(8, WORKER_ID) if f.portal_id == seeded.portal_id]
        assert leased, "the portal was not due for a lease; the visit would be a no-op"
        fake.reset()  # from here on, only what the WORKER did
        await sync_portal(seeded.portal_id)

    admin_probes = [
        record
        for record in fake.requests
        if any("user.admin" in command for command in record.commands.values())
        or (record.rest_method or "").lower() == "user.admin"
    ]
    assert admin_probes, (
        "the visit ran no `user.admin` at all: the open's `app.info` timestamp satisfied "
        "the worker's daily gate, so a demoted installer would never be noticed (§5.8)"
    )
    assert admin_probes[0].access_token == seeded.access, (
        "the re-verification must use the STORED credential - that is the token whose "
        "admin standing decides what the cache contains"
    )

    after = await portal_row(seeded.portal_id)
    assert after["token_admin_verified_at"] > before["token_admin_verified_at"], (
        "a successful re-verification records itself; `/portal/sync-status` shows this "
        "value and a frozen one is exactly the silent failure §5.8 exists to prevent"
    )
