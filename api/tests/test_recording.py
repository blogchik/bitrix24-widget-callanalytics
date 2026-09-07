"""§4.6 / §9 — the playback grant, the one credential this app puts in a URL.

An `<audio src>` cannot send an `Authorization` header. That single browser fact is why
recording playback does not run on the session bearer token like every other endpoint, and
why §4.6 mints a second, deliberately narrower token instead: `{pid, sub, acc, cid, exp =
now + 5 min}`, handed to the element as `?t=`. A query parameter is the worst place in HTTP
to keep a secret - it lands in browser history, in `Referer`, and in every proxy's access
log, which is exactly why §4.10 has Caddy stripping query strings - so the design's answer
is to make the secret worth almost nothing: five minutes, one call, one portal.

That only holds if the narrowness is enforced rather than declared. The four properties
below are the whole of it, and each is a different way the grant could quietly widen:

* **one call id.** `cid` is in the token and the request also names an id in its path. An
  endpoint that trusts the path has minted a portal-wide recording pass for five minutes,
  and the difference never shows in the happy path because the SPA always mints the grant
  for the row it is about to play.
* **it expires.** No revocation list exists - §4.6 revokes by state, and a token in a URL
  has no state - so `exp` is the only thing that ever ends this grant.
* **one portal.** `pid` is the RLS key; a grant honoured against another tenant's row is a
  cross-tenant read with a valid signature.
* **the two token kinds do not interchange.** Both are HS256 under the same
  `SESSION_SECRET`, so `typ` is the only thing between a 5-minute media grant and an hour
  of full API access. If a playback token were accepted as a session, `?t=` in a URL would
  become an API credential; if a session token were accepted as `?t=`, the session JWT -
  which §4.6 keeps out of headers, out of `Location` and out of query strings for exactly
  this reason - could be replayed from any URL bar.

The order `record.py` checks these in is what lets this file assert them without switching
`RECORDING_MODE`: the grant, the `cid`, the portal and the row are all settled *before* the
mode is consulted, so a refusal here is always about authorisation and the `off` answer is
always about the feature.

Finally, `RECORDING_MODE` itself. The §9 spike has not run, so `off` ships and the endpoint
must answer a machine code the SPA renders as "open this call in Bitrix24" rather than
leaving a player spinning. And `redirect` must be impossible to configure: if the captured
URL is of the `download.json?auth=<token>` family, redirecting hands the portal's access
token to every listener, so decision 22 keeps the value out of the `Literal` in `config.py`
and the container refuses to start rather than starting in that mode.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Final
from unittest import mock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, settings
from app.db.session import tenant_txn
from app.main import create_app
from app.security import session_token
from app.security.session_token import (
    TokenError,
    issue_play_token,
    issue_session,
    verify_play_token,
    verify_session,
)
from tests.fixtures.bitrix import FakeBitrix, SeededPortal, delete_portal, patch_httpx, seed_portal

TASHKENT: Final[str] = "Asia/Tashkent"
USER: Final[int] = 101

#: A recording URL shaped like the one §9 step 1 warns about. It is never fetched in this
#: file - `RECORDING_MODE=off` refuses first - but the row has to look real.
RECORD_URL: Final[str] = "https://portal.bitrix24.test/rest/download.json?FILE_ID=42"


def expired_play_token(*, pid: int, sub: int, acc: str, cid: int) -> str:
    """A grant that was valid ten minutes ago, minted by the real codec.

    The clock is moved for the *mint* rather than for the verify, so the token is
    byte-identical to one this application really issued. A hand-rolled `jwt.encode` here
    would prove that PyJWT checks `exp`, not that our grants ever stop working.
    """
    with mock.patch.object(session_token.time, "time", return_value=time.time() - 600):
        return issue_play_token(pid=pid, sub=sub, acc=acc, cid=cid, ttl_seconds=300)


# --- seeding ---------------------------------------------------------------------------


async def seed_call(portal_id: int, bx_id: int, *, user_id: int = USER) -> int:
    """One call that has a recording; returns the opaque `calls.id` the API addresses."""
    async with tenant_txn(portal_id) as session:
        return int(
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO calls (portal_id, bx_id, call_id, call_type,
                                           call_start_date, call_duration, call_failed_code,
                                           portal_user_id, phone_number, call_record_url)
                        VALUES (:pid, :bx_id, :call_id, 1, :started, 120, '200',
                                :user_id, '+998901112233', :record_url)
                        RETURNING id
                        """
                    ),
                    {
                        "pid": portal_id,
                        "bx_id": bx_id,
                        "call_id": f"rec-{portal_id}-{bx_id}",
                        "started": datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
                        + timedelta(minutes=bx_id),
                        "user_id": user_id,
                        "record_url": RECORD_URL,
                    },
                )
            ).scalar_one()
        )


async def wipe(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for table in ("calls", "employees", "crm_contexts"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal()
    try:
        yield seeded
    finally:
        await wipe(seeded.portal_id)
        await delete_portal(seeded.member_id)


@pytest.fixture()
async def other_portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal()
    try:
        yield seeded
    finally:
        await wipe(seeded.portal_id)
        await delete_portal(seeded.member_id)


def session_for(portal: SeededPortal, *, access: str = "all", is_admin: bool = True) -> str:
    return issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=USER,
        adm=is_admin,
        acc=access,
        tz=TASHKENT,
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )


async def fetch_record(client: httpx.AsyncClient, call_id: int, token: str) -> httpx.Response:
    """The request an `<audio src>` makes: no header at all, the grant in `?t=` (§4.6)."""
    return await client.get(f"/api/v1/calls/{call_id}/record", params={"t": token})


def code_of(response: httpx.Response) -> str | None:
    body = response.json()
    return body.get("code") if isinstance(body, dict) else None


#: What the endpoint answers a *valid* grant while the §9 spike is unresolved. Used as the
#: control throughout: "not refused" has to mean something, and with `RECORDING_MODE=off`
#: this is what an accepted grant looks like.
ACCEPTED: Final[tuple[int, str]] = (409, "recording_disabled")


# --- the codec itself ------------------------------------------------------------------


def test_the_two_token_kinds_are_not_interchangeable() -> None:
    """`typ` is the entire boundary between a 5-minute media grant and an hour of API.

    Proven at the codec because that is where the property lives: both tokens are HS256
    under `SESSION_SECRET`, so every signature check in the application accepts both, and
    only the `typ` comparison after verification tells them apart. If this pair of
    assertions ever fails, no amount of endpoint-level care can restore the separation.
    """
    play = issue_play_token(pid=1, sub=USER, acc="all", cid=99)
    session = issue_session(
        pid=1,
        mid="0" * 32,
        sub=USER,
        adm=True,
        acc="all",
        tz=TASHKENT,
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )

    with pytest.raises(TokenError):
        verify_session(play)
    with pytest.raises(TokenError):
        verify_play_token(session)

    # And `ignore_exp` - the one deliberate weakening in the codebase, for the §4.6
    # exchange - must not become a way in for the other kind either.
    with pytest.raises(TokenError):
        verify_session(play, ignore_exp=True)

    claims = verify_play_token(play)
    assert claims.cid == 99 and claims.pid == 1, "the control: a real grant still verifies"


def test_a_playback_grant_dies_at_its_expiry() -> None:
    """No revocation list exists (§4.6), so `exp` is the only end this grant ever has."""
    with pytest.raises(TokenError):
        verify_play_token(expired_play_token(pid=1, sub=USER, acc="all", cid=99))


# --- the endpoint ----------------------------------------------------------------------


async def test_a_grant_authorises_exactly_the_call_it_was_minted_for(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """`cid` decides which recording, not the path.

    The failure this rules out is not exotic: read the id from the URL, verify `t` for
    signature and expiry only, and every check still "passes" - while the grant has become
    a five-minute pass to every recording in the portal.
    """
    mine = await seed_call(portal.portal_id, 1)
    neighbours = await seed_call(portal.portal_id, 2)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=mine)

    accepted = await fetch_record(client, mine, grant)
    replayed = await fetch_record(client, neighbours, grant)

    assert (accepted.status_code, code_of(accepted)) == ACCEPTED, (
        f"the grant was not accepted for its own call id ({accepted.status_code}: "
        f"{accepted.text[:200]}), so the refusal below proves nothing."
    )
    assert replayed.status_code == 401 and code_of(replayed) == "invalid_grant", (
        f"a grant minted for call {mine} was honoured for call {neighbours} "
        f"({replayed.status_code}: {replayed.text[:200]}). The `cid` claim, not the path, "
        "decides which recording this token opens (§4.6)."
    )


async def test_an_expired_grant_no_longer_plays(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Five minutes is the blast radius of a URL that ends up in logs and browser history."""
    call_id = await seed_call(portal.portal_id, 1)
    fresh = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)
    stale = expired_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    live = await fetch_record(client, call_id, fresh)
    dead = await fetch_record(client, call_id, stale)

    assert (live.status_code, code_of(live)) == ACCEPTED, "the control: a live grant is accepted"
    assert dead.status_code == 401 and code_of(dead) == "invalid_grant", (
        f"an expired playback URL was still honoured ({dead.status_code}). Nothing else "
        "ever ends this grant: §4.6 revokes by state, and a token in a URL has no state."
    )


async def test_a_grant_minted_for_one_portal_is_refused_at_another(
    client: httpx.AsyncClient, portal: SeededPortal, other_portal: SeededPortal
) -> None:
    """`pid` is the RLS key; honouring the path's row instead would be a cross-tenant read.

    The shape matters: both portals are real, both rows exist, and the signature is
    genuine. The only thing wrong is that the grant names tenant A while the call belongs to
    tenant B - which is precisely what an attacker holding a valid grant for their own
    portal has to work with. §3's RLS makes the read return nothing *if* the transaction is
    opened for the grant's `pid`; an endpoint that opened it for the row's portal instead
    would find the row and stream it.
    """
    theirs = await seed_call(other_portal.portal_id, 1)

    wrong_tenant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=theirs)
    right_tenant = issue_play_token(pid=other_portal.portal_id, sub=USER, acc="all", cid=theirs)

    crossed = await fetch_record(client, theirs, wrong_tenant)
    control = await fetch_record(client, theirs, right_tenant)

    assert (control.status_code, code_of(control)) == ACCEPTED, (
        "the same row, addressed by a grant for its own portal, must be reachable - "
        "otherwise the refusal below is just 'nothing works'."
    )
    assert crossed.status_code == 404 and code_of(crossed) == "call_not_found", (
        f"a grant signed for portal {portal.portal_id} reached a recording belonging to "
        f"portal {other_portal.portal_id} ({crossed.status_code}: {crossed.text[:200]})."
    )


async def test_a_session_token_is_not_accepted_as_a_playback_grant(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The wire half of the codec test: `?t=<session jwt>` must not open a recording.

    Accepting it would undo §4.6's central precaution. The session token is kept out of
    headers, out of the redirect `Location` and out of query strings *because* it is an hour
    of full API access; the moment it also works as a media grant it becomes legitimate for
    the SPA to put it in a URL, and the reverse-proxy log finding is back.
    """
    call_id = await seed_call(portal.portal_id, 1)

    response = await fetch_record(client, call_id, session_for(portal))

    assert response.status_code == 401 and code_of(response) == "invalid_grant", (
        f"a session JWT was honoured as a playback grant ({response.status_code})"
    )


async def test_a_playback_grant_is_not_accepted_as_a_session(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """And the other direction, against the endpoint every session starts at.

    `GET /me` is the right target precisely because it is the most permissive endpoint in
    the API - the one a `denied` principal may still call (§4.7). If the narrow, URL-borne
    grant opens even that, it is a session token wearing a smaller name.
    """
    call_id = await seed_call(portal.portal_id, 1)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {grant}"})

    assert response.status_code == 401, (
        f"a five-minute playback grant was accepted as an API session ({response.status_code})"
    )
    assert response.json().get("code") == "invalid_session", (
        "and the refusal must be the ordinary session refusal - a distinct code here would "
        "tell an attacker they are holding a real token of the wrong kind (§4.6)."
    )


async def test_a_missing_or_malformed_grant_is_refused_the_same_way(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """No `?t=` at all is the request a browser makes when the SPA gets the URL wrong.

    It must be the same 401 and the same code as a forged one: an endpoint that treats a
    missing grant as "no authorisation required" is the design-review finding that put the
    signed URL here in the first place.
    """
    call_id = await seed_call(portal.portal_id, 1)

    bare = await client.get(f"/api/v1/calls/{call_id}/record")
    garbage = await fetch_record(client, call_id, "not-a-token")

    for response in (bare, garbage):
        assert response.status_code == 401 and code_of(response) == "invalid_grant", (
            f"{response.request.url} answered {response.status_code}: {response.text[:200]}"
        )


# --- the shipped mode ------------------------------------------------------------------


async def test_recording_mode_off_answers_a_machine_code_not_a_broken_player(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Decision 22: `off` until the spike answers, and `off` has to be *legible*.

    The grant here is entirely valid, so the answer describes the feature rather than the
    caller - and that distinction is the assertion. §8 has the SPA switch on machine codes,
    and this one tells it to render the "has recording, open it in Bitrix24" affordance of
    §9. An `invalid_grant` in this slot would send the SPA off to re-mint a token forever;
    a 404 would say the recording does not exist, when it does; and a 500 or an empty 200
    would leave the moderator with a player that spins.
    """
    assert settings.recording_mode == "off", (
        "this test describes the shipped configuration; RECORDING_MODE is not `off` here"
    )
    call_id = await seed_call(portal.portal_id, 1)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await fetch_record(client, call_id, grant)

    assert fake.rest_count == 0 and fake.oauth_count == 0, (
        "with recording disabled the endpoint still contacted Bitrix24; `off` means the "
        "feature does not run at all, not that it runs and discards the result."
    )
    assert (response.status_code, code_of(response)) == ACCEPTED, (
        f"expected {ACCEPTED} while the §9 spike is unresolved, got {response.status_code}: "
        f"{response.text[:200]}"
    )
    assert "download.json" not in response.text and RECORD_URL not in response.text, (
        "even the refusal must not disclose the upstream recording URL (decision 19)"
    )
    assert "no-store" in response.headers.get("cache-control", "").lower(), (
        "§4.10 marks every API response no-store; a cached playback answer would outlive "
        "the five-minute grant it was produced for."
    )


def test_recording_mode_redirect_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 22 / §9 step 5: `redirect` is not a mode that can be switched on by mistake.

    If the captured `CALL_RECORD_URL` is of the `download.json?auth=<token>` family - which
    §9 step 1 exists to find out - then a redirect hands the portal's access token to every
    listener's browser, with `crm` scope, for the life of that token. That is not a risk to
    be managed by a note in a runbook: the value is absent from the `Literal` in
    `config.py`, so a container configured that way refuses to start.

    Asserted through `Settings()` rather than by reading the annotation, because the
    guarantee is "the process dies", not "the type says so".
    """
    monkeypatch.setenv("RECORDING_MODE", "redirect")

    with pytest.raises(ValidationError) as raised:
        Settings()  # type: ignore[call-arg]  # every other field comes from the environment

    assert "recording_mode" in str(raised.value)

    monkeypatch.setenv("RECORDING_MODE", "proxy")
    assert Settings().recording_mode == "proxy", (  # type: ignore[call-arg]
        "the control: `proxy` is a real mode, so the refusal above is about `redirect` and "
        "not about the environment being unreadable in this test."
    )
