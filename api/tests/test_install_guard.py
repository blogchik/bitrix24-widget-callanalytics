"""§4.3 - the four ways `POST /install/` must refuse to write, and the one way it must.

These are the design review's blocker cases. Each of them is a path where the handler
holds an *unproven* payload (§4.1: "every field of a Bitrix24 POST is untrusted until
proven") and the only correct behaviour is to render a state page and leave the database
exactly as it was. A partial write on any of them is a tenant takeover or a data-loss
bug, not a cosmetic one:

1. **Empty `REFRESH_ID` on an existing portal.** Nothing about that POST is proven -
   `member_id` is public (decision 1), so the body could come from anyone. Writing even
   `domain` from it would let a stranger rewrite a live tenant's CSP origin.
2. **A refresh whose response `member_id` differs from the posted one.** The exchange
   proved *a* portal, just not this one.
3. **A non-admin installer.** Decision 4: a non-admin credential becomes the worker's
   token and silently caches only that user's calls.
4. **A reinstall of an active portal must not reset the sync cursors** (§4.3 step 4).
   Bitrix24 re-opens the install URL on every version update; resetting there re-imports
   the whole history and races the running worker (review finding on §4.3).

Everything is driven through the real FastAPI app over `httpx.ASGITransport`, so the
routing, the form parsing and the response headers are the real ones. The Bitrix24 side
is the shared fake, and the assertion in cases 1-3 is a **full row snapshot** compared
before and after - not a spot check of the columns we happened to think of.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.oauth import reset_exchange_rate_limit
from app.handlers.render import STATE_KINDS
from app.i18n import LOCALES, has_message, t
from app.main import create_app
from tests.fixtures.bitrix import (
    CLIENT_ENDPOINT,
    DOMAIN,
    FRESH_ACCESS,
    Err,
    FakeBitrix,
    delete_portal,
    install_form,
    install_query,
    new_member_id,
    patch_httpx,
    portal_snapshot,
    portal_sync_snapshot,
    seed_portal,
    token_response,
)

# NOTE: no module-level `pytest.mark.asyncio` - `asyncio_mode = "auto"` marks the async
# tests, and the two synchronous guard tests at the bottom must stay synchronous.

#: Accepted spellings of a state kind. `render_state(request, kind, ...)` takes the kind
#: as a slug and `render.py` owns the canonical set, so the lookup below resolves an
#: alias against THAT set rather than hard-coding a name this file cannot see change.
#: §4.3 step 3 names the non-admin state only in prose ("administrators only"), which is
#: why more than one spelling is accepted for it.
_STATE_ALIASES: dict[str, tuple[str, ...]] = {
    "unsupported_portal": ("unsupported_portal",),
    "bad_request": ("bad_request",),
    "administrators_only": ("admin_only", "administrators_only", "admins_only", "not_admin"),
}


def resolve_kind(kind: str) -> str:
    """Map a test's name for a state onto the slug `render.py` actually defines."""
    for alias in _STATE_ALIASES[kind]:
        if alias in STATE_KINDS:
            return alias
    pytest.fail(
        f"render.py defines none of {_STATE_ALIASES[kind]} in STATE_KINDS; "
        f"add the spelling it uses to _STATE_ALIASES[{kind!r}]."
    )


def assert_state(response: httpx.Response, kind: str) -> None:
    """Assert the response really is the rendered state page for `kind` (§4.10).

    The page is matched by its **translated title** rather than by a marker attribute:
    `state.html` is a moderator-facing page and has no business carrying a test hook,
    while the title is what §4.11 says each path must end in. Reading it through
    `app.i18n.t` also means this assertion is exercising the single message source of
    §8, not a copy of the copy.
    """
    slug = resolve_kind(kind)
    key = f"state.{slug}.title"
    if not has_message(key):
        pytest.fail(
            f"the shared message catalogue (§8) has no {key!r}, so render_state() falls "
            "back to the generic error copy and NO state page is distinguishable. Either "
            "the key is missing from web/messages/<locale>.json, or the api image does "
            "not carry the bundle (§8: 'the api image copies messages/ and "
            "src/i18n/locales.json at build time') - app/i18n.py looks in "
            "<image root>/i18n/ and <repo root>/web/."
        )
    # Matched in ANY shipped locale: which language the page rendered in is a separate
    # question (§8's fallback map), and these tests deliberately vary the posted LANG.
    titles = {t(locale, key) for locale in LOCALES}
    assert any(title in response.text for title in titles), (
        f"expected the {slug!r} state page (title, in some locale, one of {sorted(titles)}); "
        "got a different page."
    )
    # §4.10: rendered per portal and per user - never cached, and never offered a legacy
    # framing header, which has no origin list and would blank the Bitrix24 iframe.
    assert "no-store" in response.headers.get("cache-control", "")
    assert "x-frame-options" not in {key.lower() for key in response.headers}


@pytest.fixture(autouse=True)
def fresh_exchange_budget() -> None:
    """§4.1 rate-limits unauthenticated refresh exchanges to OAUTH_EXCHANGE_LIMIT per
    `member_id` AND per source IP per 10 minutes. Every test here shares one source IP,
    so without this the sixth install in the module would be refused for a reason that
    has nothing to do with what it is testing."""
    reset_exchange_rate_limit()


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The real app over ASGI.

    An explicit `transport=` keeps this client out of `patch_httpx`'s reach, so the fake
    Bitrix24 and the app under test can be live in the same `with` block.
    """
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


async def post_install(client: httpx.AsyncClient, body: dict[str, str]) -> httpx.Response:
    """One install POST, shaped the way Bitrix24 shapes it (§4.2)."""
    return await client.post(f"/install/?{install_query()}", data=body)


@pytest.fixture()
async def cleanup(app_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """Member ids to purge afterwards, including ones a test expects NOT to be created.

    Depends on `app_engine` so pytest tears this down BEFORE the engine's pool is
    disposed - otherwise the cleanup transaction opens a fresh pool that nothing closes.
    """
    member_ids: list[str] = []
    try:
        yield member_ids
    finally:
        for member_id in member_ids:
            await delete_portal(member_id)


def full_fake(member_id: str) -> FakeBitrix:
    """A Bitrix24 that answers the whole §4.3 install happily."""
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=member_id))
    return fake


# --- 1. empty REFRESH_ID on an existing portal --------------------------------------


async def test_empty_refresh_id_on_an_existing_portal_changes_no_column(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.3 step 2: "Empty REFRESH_ID -> /state/unsupported_portal, no row is created
    or touched."

    The POST below is entirely forgeable: `member_id` is public (decision 1) and there
    is no token in it to prove anything. If the handler updated so much as `domain` or
    `lang` from it, anyone who has ever seen the tenant key could rewrite the CSP origin
    of a live customer.
    """
    seeded = await seed_portal(status="active", high_id=41_200, low_id=8_100)
    cleanup.append(seeded.member_id)
    before_portal = await portal_snapshot(seeded.member_id)
    before_sync = await portal_sync_snapshot(seeded.portal_id)

    fake = FakeBitrix()  # nothing scripted: any REST/OAuth call at all is a failure
    with patch_httpx(fake):
        response = await post_install(
            client,
            install_form(
                member_id=seeded.member_id,
                refresh_id=None,          # the field arrives, empty - the on-premise case
                domain="attacker.example.com",
                lang="en",
                application_token="totallydifferentapplicationtoken00",
            ),
        )

    assert response.status_code < 500
    assert fake.oauth_count == 0, "an empty REFRESH_ID cannot be exchanged, so do not try"
    assert fake.rest_count == 0, "and nothing may be contacted at DOMAIN either (§4.1)"
    assert await portal_snapshot(seeded.member_id) == before_portal
    assert await portal_sync_snapshot(seeded.portal_id) == before_sync
    assert_state(response, "unsupported_portal")


async def test_empty_refresh_id_for_an_unknown_portal_creates_no_row(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The same rule for a portal we have never seen (assumption 20: isolated boxes are
    out of scope in v1 and get a state page, not a half-built tenant)."""
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=member_id, refresh_id=None))

    assert fake.oauth_count == 0
    assert await portal_snapshot(member_id) is None
    assert_state(response, "unsupported_portal")


# --- 2. the refresh proves a different portal ---------------------------------------


async def test_a_refresh_answering_with_another_member_id_writes_nothing(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.3 step 2: "The response `member_id` must equal the POSTed one (else 400)."

    Without this check the exchange proves only that *some* portal exists - and since
    the tokens, `client_endpoint` and `user_id` from that response are what we store,
    the row created would carry another tenant's credential under this tenant's key.
    """
    posted = new_member_id()
    other = new_member_id()
    cleanup.extend([posted, other])

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=other))
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=posted))

    assert response.status_code == 400, "§4.3 step 2 makes this a bad request"
    assert await portal_snapshot(posted) is None, "no row for the posted member_id"
    assert await portal_snapshot(other) is None, "and none for the one the response named"
    # The admin proof of step 3 must never even be attempted on an unproven endpoint.
    assert fake.rest_count == 0
    assert_state(response, "bad_request")


async def test_a_mismatched_refresh_does_not_touch_an_existing_portal(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The same POST against a tenant that already exists: still a full no-op."""
    seeded = await seed_portal(status="active", high_id=999, low_id=100)
    cleanup.append(seeded.member_id)
    before_portal = await portal_snapshot(seeded.member_id)
    before_sync = await portal_sync_snapshot(seeded.portal_id)

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=new_member_id()))
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=seeded.member_id))

    assert response.status_code == 400
    assert await portal_snapshot(seeded.member_id) == before_portal
    assert await portal_sync_snapshot(seeded.portal_id) == before_sync


# --- 3. the installer is not an administrator ---------------------------------------


async def test_a_non_admin_installer_writes_nothing_and_renders_http_200(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.3 step 3: "`user.admin=false` -> render "administrators only" (HTTP 200) and
    write nothing."

    Decision 4 is why: the token stored at install becomes the sync worker's credential.
    A regular employee's token returns only that employee's calls, so the dashboard
    would look plausible and be permanently, silently wrong. HTTP 200 because the frame
    must show the explanation, not a browser error page (§4.11).
    """
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = full_fake(member_id)
    fake.on("user.admin", False)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=member_id))

    assert response.status_code == 200, "the moderator must see the explanation in-frame"
    assert await portal_snapshot(member_id) is None, "a non-admin creates no tenant at all"
    # The proof batch had to run to learn the answer, but nothing may follow it: no
    # placement.bind, no event.bind, no second exchange (§4.3 step 3 is ONE batch).
    assert fake.oauth_count == 1
    assert fake.rest_count == 1
    assert_state(response, "administrators_only")


async def test_a_non_admin_reinstall_leaves_an_existing_portal_untouched(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The takeover shape: an employee re-runs the install URL on a healthy tenant. The
    stored admin credential must survive it byte for byte."""
    seeded = await seed_portal(status="active", high_id=77_000, low_id=1_000)
    cleanup.append(seeded.member_id)
    before_portal = await portal_snapshot(seeded.member_id)
    before_sync = await portal_sync_snapshot(seeded.portal_id)

    fake = full_fake(seeded.member_id)
    fake.on("user.admin", False)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=seeded.member_id))

    assert response.status_code == 200
    after_portal = await portal_snapshot(seeded.member_id)
    assert after_portal == before_portal, "a non-admin may not rewrite ANY portals column"
    assert await portal_sync_snapshot(seeded.portal_id) == before_sync
    assert_state(response, "administrators_only")


async def test_a_failing_admin_proof_never_stores_the_exchanged_credential(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """`user.admin` erroring is not "assume admin": §4.1 fails closed."""
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = full_fake(member_id)
    fake.on("user.admin", Err("INVALID_CREDENTIALS", "Invalid request credentials"))
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=member_id))

    assert response.status_code < 500
    assert await portal_snapshot(member_id) is None


# --- 4. reinstall must not reset the cursors (§4.3 step 4) --------------------------


async def test_reinstalling_an_active_portal_keeps_the_sync_cursors(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.3 step 4: cursors reset "only when the portal row is new or its previous
    status was `uninstalled`".

    Bitrix24 re-opens the install URL on every version update and any admin can trigger
    it. Resetting there re-imports the entire history - hours of backfill on a 500k-row
    portal, a progress banner the customer already saw once, and a race with the worker
    that is mid-visit (review finding on §4.3).
    """
    seeded = await seed_portal(
        status="active", high_id=512_345, low_id=498_000, backfill_status="running",
        sync_generation=3,
    )
    cleanup.append(seeded.member_id)
    before_sync = await portal_sync_snapshot(seeded.portal_id)
    assert before_sync is not None

    fake = full_fake(seeded.member_id)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=seeded.member_id))

    assert response.status_code == 200
    after_sync = await portal_sync_snapshot(seeded.portal_id)
    assert after_sync is not None
    assert after_sync["high_id"] == 512_345, "the forward cursor must survive a reinstall"
    assert after_sync["low_id"] == 498_000, "and so must the backfill cursor"
    assert after_sync["backfill_status"] == "running"
    # What step 4 DOES change: the fencing token, so an in-flight run cannot write back.
    assert after_sync["sync_generation"] == 4
    assert after_sync["lease_owner"] is None

    # And the credential really was re-seeded, so the test is not passing because the
    # whole install silently no-opped.
    after_portal = await portal_snapshot(seeded.member_id)
    assert after_portal is not None
    assert after_portal["token_status"] == "ok"
    assert after_portal["status"] == "active"
    assert after_portal["install_completed_at"] is not None


async def test_reinstalling_an_uninstalled_portal_does_reset_the_cursors(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The other half of the same rule: after an uninstall the cached rows were purged
    (§4.9 rule 3, §5.9), so keeping the cursors would leave the portal permanently
    missing everything below `low_id` - a dashboard that can never be complete."""
    seeded = await seed_portal(
        status="uninstalled", high_id=512_345, low_id=498_000, backfill_status="running",
        sync_generation=3,
    )
    cleanup.append(seeded.member_id)

    fake = full_fake(seeded.member_id)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=seeded.member_id))

    assert response.status_code == 200
    after_sync = await portal_sync_snapshot(seeded.portal_id)
    assert after_sync is not None
    assert after_sync["high_id"] == 0
    assert after_sync["low_id"] is None
    assert after_sync["backfill_status"] == "pending"
    assert after_sync["sync_generation"] == 4

    after_portal = await portal_snapshot(seeded.member_id)
    assert after_portal is not None
    assert after_portal["status"] == "active", "the tenant is live again"
    assert after_portal["uninstalled_at"] is None


async def test_a_clean_install_stores_the_exchanged_credential_not_the_posted_one(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """The happy path, which the four refusals above would otherwise pass vacuously.

    §4.3 step 2: the refresh response is authoritative. The POSTed `AUTH_ID`/`REFRESH_ID`
    belong to the browsing admin's session; what we keep is the pair the OAuth server
    issued, together with ITS `client_endpoint` - never a base built from `DOMAIN` (§4.1).
    """
    from app.security.crypto import decrypt

    member_id = new_member_id()
    cleanup.append(member_id)

    fake = full_fake(member_id)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=member_id))

    assert response.status_code == 200
    row = await portal_snapshot(member_id)
    assert row is not None
    assert row["client_endpoint"] == CLIENT_ENDPOINT
    assert row["domain"] == DOMAIN, "domain comes from the POST, never from the OAuth response"
    assert row["status"] == "active"
    assert row["token_status"] == "ok"
    assert row["token_admin_verified_at"] is not None
    assert row["token_version"] >= 1
    stored_access = decrypt(
        bytes.fromhex(str(row["access_token_enc"])), member_id=member_id, column="access_token"
    )
    assert stored_access == FRESH_ACCESS
    assert fake.oauth_count == 1, "§4.3 step 2 performs exactly one exchange"


async def test_install_stays_inside_the_round_trip_budget(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.3 step 6: "Three HTTP round trips; no sync work inline."

    Kept separate from the correctness test above so a budget regression cannot be
    mistaken for a credential bug. The budget is not cosmetic: this runs while the
    moderator watches an "Installing..." spinner, every command burns the per-method
    operating-time allowance (§5.6), and the batch endpoint exists precisely so that
    step 3's five probes and step 5's six binds are two requests rather than eleven.
    """
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = full_fake(member_id)
    with patch_httpx(fake):
        response = await post_install(client, install_form(member_id=member_id))

    assert response.status_code == 200
    packed = [sorted(request.commands.values()) for request in fake.of_kind("batch")]
    assert fake.oauth_count == 1
    assert fake.rest_count == 2, (
        "§4.3 wants one proof batch (step 3: user.current, user.admin, app.info, "
        "method.get, placement.get) and one bind batch (step 5: four placement.bind "
        "plus the two event.bind, which step 5 puts in that same batch). "
        f"Observed {fake.rest_count} REST round trips: {packed}"
    )


async def test_an_unparsable_body_renders_bad_request_and_writes_nothing(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.2/§4.11: "Probes endpoints with garbage -> translated bad request, HTTP 400."
    Never a stack trace, never a blank frame, never a row."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_install(
            client, {"member_id": "NOT-A-MEMBER-ID", "DOMAIN": "'; drop table calls"}
        )

    assert response.status_code == 400
    assert fake.oauth_count == 0
    assert "traceback" not in response.text.lower()
    assert_state(response, "bad_request")


async def test_an_event_body_posted_to_the_install_url_is_not_treated_as_an_install(
    client: httpx.AsyncClient, cleanup: list[str]
) -> None:
    """§4.2: "a body carrying `event=` on /install/ is dispatched to the events handler
    BEFORE the placement allowlist runs" - some cabinets deliver lifecycle events there.
    Whatever the events handler decides, it must not be an install: no exchange, no row.
    """
    member_id = new_member_id()
    cleanup.append(member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await client.post(
            "/install/",
            data={
                "event": "ONAPPUNINSTALL",
                "ts": "1780319382",
                "auth[member_id]": member_id,
                "auth[application_token]": "someapplicationtoken0011223344",
                "data[CLEAN]": "1",
            },
        )

    assert response.status_code < 500
    assert fake.oauth_count == 0, "an unknown portal's token-less event is ignored (§4.9 r7)"
    assert await portal_snapshot(member_id) is None


def test_install_form_fixture_is_a_valid_allowlisted_body() -> None:
    """Guards the fixture itself: if `install_form()` drifted out of the §4.2 allowlist
    every test above would be exercising the bad-request branch instead."""
    from app.bitrix.forms import parse_iframe_post

    parsed = parse_iframe_post(install_form(member_id=new_member_id()), {}, install_query())
    assert parsed.refresh_id is not None
    assert parsed.placement == "DEFAULT"
