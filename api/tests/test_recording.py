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

import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from unittest import mock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api import record as record_api
from app.config import Settings, settings
from app.db.session import tenant_txn
from app.logging import setup_logging
from app.main import create_app
from app.security import session_token
from app.security.session_token import (
    TokenError,
    issue_play_token,
    issue_session,
    verify_play_token,
    verify_session,
)
from tests.fixtures.bitrix import (
    DOMAIN,
    SEED_ACCESS,
    Err,
    FakeBitrix,
    SeededPortal,
    delete_portal,
    patch_httpx,
    seed_portal,
)

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


async def seed_call(
    portal_id: int,
    bx_id: int,
    *,
    user_id: int = USER,
    activity_id: int | None = None,
    record_file_id: int | None = None,
) -> int:
    """One call that has a recording; returns the opaque `calls.id` the API addresses.

    `activity_id` / `record_file_id` default to NULL, which is the row every test above
    describes: a portal whose recordings were never attached to a CRM activity. A row
    that carries both is the row §9's measurement is about - on the live portal all 3 209
    recorded calls have both - and is what the Bitrix24-copy tests at the bottom seed.
    """
    async with tenant_txn(portal_id) as session:
        return int(
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO calls (portal_id, bx_id, call_id, call_type,
                                           call_start_date, call_duration, call_failed_code,
                                           portal_user_id, phone_number, call_record_url,
                                           crm_activity_id, record_file_id)
                        VALUES (:pid, :bx_id, :call_id, 1, :started, 120, '200',
                                :user_id, '+998901112233', :record_url,
                                :activity_id, :record_file_id)
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
                        "activity_id": activity_id,
                        "record_file_id": record_file_id,
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


async def fetch_record(
    client: httpx.AsyncClient,
    call_id: int,
    token: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """The request an `<audio src>` makes: no header at all, the grant in `?t=` (§4.6).

    `headers` exists for the playback tests only, and carries exactly one thing: the
    `Range` a real player sends. §9's measurement is *about* that header - an open-ended
    `Range: bytes=0-` is the shape the provider stalls on and the shape Bitrix24's own
    copy answers in 1.15 s - so a proxy test that never sends one is not testing playback.
    """
    return await client.get(
        f"/api/v1/calls/{call_id}/record", params={"t": token}, headers=headers or {}
    )


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


# =========================================================================================
# The Bitrix24-hosted copy of the recording (§9, and the measurement that replaced it)
# =========================================================================================
#
# §9 step 1 assumed one upstream: `calls.call_record_url`, whatever the portal's telephony
# provider put there. On the live portal that is `sipuni.com`, and our server cannot pull
# it - every request shape a real player makes (open-ended `Range: bytes=0-`, no Range, or
# any range >= 64 KiB) returns headers and then no body at all. The same request from an
# ordinary client network succeeds, so it is per-IP shaping against our datacentre egress
# rather than anything about the URL; either way the conclusion is operational: do not
# build on our server pulling from the provider.
#
# Bitrix24 keeps its own copy. Marketplace telephony integrations are required to call
# `telephony.externalCall.attachRecord`, which is why `RECORD_FILE_ID` is populated on
# these rows, and that copy is reachable with the `crm` scope this app already holds:
#
#     crm.activity.get {id: <crm_activity_id>}
#       -> FILES: [{id: <file id>, url: ".../crm_show_file.php?fileId=..&auth=<token>"}]
#
# Measured twice on the live portal, from the same container that cannot reach the
# provider: `FILES[0].id` equals `calls.record_file_id` exactly; `Range: bytes=0-` answers
# 206 with the whole 3 962 880-byte body in 1.15 s; a bare GET answers 200 `audio/mpeg`;
# a mid-file range answers 206 with a correct `Content-Range` in 0.26 s. The URL is
# reusable and needs no cookie, and the `auth` parameter is the *whole* gate - strip or
# mutate it, or mismatch `ownerId`, and the portal answers an HTML login page.
#
# What is documented is that `FILES` is an activity field (`crm.activity.fields` types it
# `diskfile`, and the `crm.activity.list` reference shows this URL shape verbatim). What
# is NOT documented is that a telephony recording lands there and that the endpoint serves
# `audio/mpeg` with byte ranges. Both are measured, neither is promised - which is the
# entire reason the fallback below is a test and not a comment.

#: `crm.activity.get` -> `FILES[].url`. Spelled as Bitrix24 spells it; `ownerTypeId=6` is
#: the activity owner type, and a mismatched `ownerId` is answered with HTML, not audio.
SHOW_FILE_PATH: Final[str] = "/bitrix/tools/crm_show_file.php"

#: The provider link's path, i.e. what `call_record_url` resolves to in this file. Kept
#: next to the one above because "which of these two did the endpoint open" is the
#: assertion of four tests below.
PROVIDER_PATH: Final[str] = "/rest/download.json"

#: `calls.crm_activity_id` and `calls.record_file_id` of the seeded row. Deliberately
#: different numbers: "the resolver passed the activity id where a file id belongs" is a
#: plausible bug, and two equal values would hide it.
ACTIVITY_ID: Final[int] = 776655
FILE_ID: Final[int] = 4242

#: A body that is neither JSON nor text, so "the client got the file" is unambiguous and
#: an accidental error envelope can never compare equal to it.
AUDIO: Final[bytes] = b"ID3\x03\x00\x00\x00" + bytes(range(256)) * 16

#: Sentinel for "the key is not in the payload at all", which is a different shape from
#: "the key is there and null" - both of which `FILES` has to survive.
_ABSENT: Final[object] = object()


def show_file_url(file_id: int, *, owner_id: int = ACTIVITY_ID, auth: str = SEED_ACCESS) -> str:
    """One `FILES[].url`, shape for shape as measured on the live portal.

    `auth` is the access token that made the REST call, and it is the whole gate. That
    makes this string a live credential with the portal's `crm` scope behind it, which is
    why the two tests at the end of this file treat it exactly like `SEED_ACCESS` itself.
    """
    return (
        f"https://{DOMAIN}{SHOW_FILE_PATH}"
        f"?fileId={file_id}&ownerTypeId=6&ownerId={owner_id}&auth={auth}"
    )


def file_entry(file_id: int, *, as_text: bool = False) -> dict[str, Any]:
    """One entry of `FILES`. `as_text` is Bitrix24's other spelling of the same id.

    Ids arrive as strings on most builds and as integers on some
    (docs/bitrix24-api-research.md, and `crm.py::_as_int` exists for the same reason).
    `record_file_id` is a `bigint` in our schema, so the comparison is across types on
    every real portal - `"4242" == 4242` is False in Python and would resolve nothing.
    """
    return {
        "id": str(file_id) if as_text else file_id,
        "name": f"record-{file_id}.mp3",
        "url": show_file_url(file_id),
    }


def activity(files: Any = _ABSENT) -> dict[str, Any]:
    """The `crm.activity.get` result, carrying only the fields this app looks at."""
    payload: dict[str, Any] = {
        "ID": str(ACTIVITY_ID),
        "TYPE_ID": "2",
        "OWNER_ID": "1",
        "OWNER_TYPE_ID": "2",
        "PROVIDER_ID": "VOXIMPLANT_CALL",
    }
    if files is not _ABSENT:
        payload["FILES"] = files
    return payload


def resolver() -> Any:
    """`crm.resolve_recording_url`, imported late and on purpose.

    A module-level import would turn "the resolver does not exist yet" into a collection
    error for the whole file, and the eight authorisation tests above have nothing to do
    with this change - they must keep running, and keep passing, either way.
    """
    from app.bitrix.crm import resolve_recording_url

    return resolve_recording_url


@dataclass
class StubActivityClient:
    """The narrowest thing that satisfies what the resolver needs of a `BitrixClient`.

    Not `FakeBitrix` plus a real client: these three tests are about one pure decision -
    given this payload and this `record_file_id`, which URL - and routing them through
    HTTP, OAuth and `rest_log` would make a failure here mean a dozen things. The endpoint
    tests below use the real client against the real fake, so the wiring is covered once.
    """

    result: Any = None
    error: Exception | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def asked_for(self) -> set[str]:
        """Every parameter value the resolver sent, stringified for a type-blind compare."""
        return {str(value) for _, params in self.calls for value in params.values()}


# --- the resolver ----------------------------------------------------------------------


async def test_the_resolver_picks_the_file_the_row_names() -> None:
    """`FILES` is a list; `record_file_id` says which entry of it, in either spelling.

    Measured on the live portal: `FILES[0].id` equals `calls.record_file_id` exactly. That
    an activity usually carries one attachment is what makes "just take the first one" look
    correct, and it is why the id has to be the rule instead - a call whose activity also
    holds a note attachment, or a portal that attached a re-recorded file second, would
    otherwise play the wrong bytes.

    Both id spellings are run because `record_file_id` is a `bigint` on our side: against a
    build that serialises ids as strings, an `==` on the raw values matches nothing at all,
    and the symptom is not an error - it is silent fallback to the provider URL.
    """
    for as_text in (False, True):
        spelling = "strings" if as_text else "integers"
        client = StubActivityClient(
            result=activity(
                [
                    file_entry(4241, as_text=as_text),
                    file_entry(FILE_ID, as_text=as_text),
                    file_entry(4243, as_text=as_text),
                ]
            )
        )

        url = await resolver()(client, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)

        assert url == show_file_url(FILE_ID), (
            f"with ids serialised as {spelling}, the resolver returned {url!r} instead of "
            f"the entry whose id is {FILE_ID}. `FILES[0]` is not the answer - the row's "
            "`record_file_id` is (§9 results)."
        )
        assert [method for method, _ in client.calls] == ["crm.activity.get"], (
            "the copy is reached through `crm.activity.get` under the `crm` scope this app "
            f"already holds; this asked for {[m for m, _ in client.calls]}"
        )
        assert str(ACTIVITY_ID) in client.asked_for, (
            "the activity asked for must be the one the row names - `crm_activity_id` - "
            f"and nothing in {client.calls[0][1]!r} is {ACTIVITY_ID}"
        )


async def test_the_resolver_refuses_to_guess_which_recording_to_play() -> None:
    """With no `record_file_id` and several attachments the answer is None, not `FILES[0]`.

    The failure this prevents is not a 500 and not an empty player: it is **playing one
    customer's recording to a moderator who asked for another**. That is a privacy incident
    that looks exactly like success - audio plays, the duration is plausible, nobody has any
    reason to doubt it - and it is discovered, if ever, by the person on the recording.

    Falling back to the stored `call_record_url` is always available and always correct, so
    guessing buys nothing. Where there is exactly one attachment there is nothing to guess:
    one file on the activity of one call is that call's recording, and refusing it would
    strand every row whose `RECORD_FILE_ID` we never captured.
    """
    ambiguous = StubActivityClient(
        result=activity([file_entry(4241), file_entry(FILE_ID), file_entry(4243)])
    )
    assert await resolver()(ambiguous, activity_id=ACTIVITY_ID, record_file_id=None) is None, (
        "three attachments and nothing naming which one is a recording this app must not "
        "pick; the endpoint falls back to `call_record_url` instead."
    )

    lone = StubActivityClient(result=activity([file_entry(FILE_ID)]))
    assert await resolver()(lone, activity_id=ACTIVITY_ID, record_file_id=None) == show_file_url(
        FILE_ID
    ), "one attachment leaves nothing to guess, and refusing it would strand real rows"


async def test_the_resolver_survives_every_shape_files_comes_back_in() -> None:
    """`FILES` is typed `diskfile`, which is not a promise about JSON.

    None of this is hypothetical PHP-lawyering: `crm.py::_rows` already exists because a
    Bitrix24 list result arrives as `[]` on most builds and as an object keyed by index on
    some, and the whole point of this path is that the payload is *undocumented for this
    use*. A resolver that raises on an unexpected shape turns a portal-side surprise into a
    500 on a playback that had a perfectly good fallback URL sitting in the row.
    """
    for label, files in (
        ("FILES absent", _ABSENT),
        ("FILES null", None),
        ("FILES empty list", []),
        ("FILES empty object", {}),
        ("FILES not a collection", "no"),
        ("an entry that is not a mapping", ["nonsense", 5]),
        ("an entry with an id but no url", [{"id": FILE_ID}]),
        ("an entry with a url but no id", [{"url": show_file_url(FILE_ID)}]),
        ("only other files", [file_entry(4241), file_entry(4243)]),
    ):
        client = StubActivityClient(result=activity() if files is _ABSENT else activity(files))
        answer = await resolver()(client, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)
        assert answer is None, f"{label}: expected None, got {answer!r}"

    # PHP's other spelling of a list, with the wanted file in it: still resolvable.
    indexed = StubActivityClient(result=activity({"0": file_entry(4241), "1": file_entry(FILE_ID)}))
    assert await resolver()(
        indexed, activity_id=ACTIVITY_ID, record_file_id=FILE_ID
    ) == show_file_url(FILE_ID), (
        "an object keyed by index is the same list Bitrix24 sends as an array elsewhere; "
        "reading only one of the two spellings would lose the copy on those portals"
    )

    # And the result itself, not just `FILES`, may be nothing at all.
    for empty in (None, [], {}, "", 0):
        assert (
            await resolver()(
                StubActivityClient(result=empty), activity_id=ACTIVITY_ID, record_file_id=FILE_ID
            )
            is None
        ), f"a {empty!r} result must resolve to None, not raise"


# --- the endpoint, against both upstreams -----------------------------------------------


def _is_media(url: httpx.URL) -> bool:
    """True for either URL a playback could open: Bitrix24's copy, or the provider link."""
    return url.path.endswith(SHOW_FILE_PATH) or url.path.endswith(PROVIDER_PATH)


class RecordingBitrix(FakeBitrix):
    """`FakeBitrix` plus the one thing it knows nothing about: a URL that answers audio.

    A playback opens exactly one non-REST URL, and *which* one - the copy Bitrix24 kept or
    the provider link in `call_record_url` - is the whole assertion of the tests below. So
    media fetches are captured apart from `requests`: they are not REST round trips and
    must not move `rest_count`, which the `off`-mode test above asserts is zero.

    The Range behaviour is the measured one (§9 results): `Range: bytes=0-` answers 206
    with the whole body, a bare GET answers 200 `audio/mpeg`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.media: list[httpx.Request] = []

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if not _is_media(request.url):
            return await super()._handle(request)
        self.media.append(request)
        headers = {"content-type": "audio/mpeg", "accept-ranges": "bytes"}
        if request.headers.get("range"):
            headers["content-range"] = f"bytes 0-{len(AUDIO) - 1}/{len(AUDIO)}"
            return httpx.Response(206, content=AUDIO, headers=headers)
        return httpx.Response(200, content=AUDIO, headers=headers)

    @property
    def opened(self) -> httpx.URL:
        """The single URL this playback fetched. Loud when it was not exactly one."""
        assert len(self.media) == 1, (
            f"expected exactly one upstream media fetch, saw {len(self.media)}: "
            f"{[str(r.url.path) for r in self.media]}"
        )
        return self.media[0].url


@pytest.fixture()
def proxy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the endpoint in `proxy` for one test, without touching the shipped default.

    `Settings` is frozen, so the module-level name `record.py` resolves at request time is
    swapped for a copy instead of being mutated. Nothing else about the endpoint moves: the
    5-minute `?t=` grant, the portal-status recheck, the `scope_filter` re-application and
    the admin/viewer token choice are all settled *before* the mode is consulted, which is
    exactly what lets `off` and `proxy` share this file's fixtures.
    """
    monkeypatch.setattr(
        record_api, "settings", settings.model_copy(update={"recording_mode": "proxy"})
    )


async def test_the_endpoint_streams_the_copy_bitrix24_kept(
    client: httpx.AsyncClient, portal: SeededPortal, proxy_mode: None
) -> None:
    """With an activity id on the row, the proxy opens the portal's copy - not the provider.

    This is the whole change. Both upstreams answer in this test, and the provider link is
    the one the row has always carried, so nothing *fails* if the endpoint ignores the
    resolver - it just streams from the URL that, on the one real portal, returns headers
    and then nothing at all. Preferring the copy is therefore not an optimisation: it is the
    difference between playback working and playback timing out.
    """
    call_id = await seed_call(portal.portal_id, 1, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    fake = RecordingBitrix().on("crm.activity.get", activity([file_entry(FILE_ID)]))
    with patch_httpx(fake):
        response = await fetch_record(client, call_id, grant, headers={"Range": "bytes=0-"})

    assert response.status_code == 206, (
        f"an open-ended Range must come back 206 with the body ({response.status_code}: "
        f"{response.text[:200]})"
    )
    assert response.content == AUDIO, "the client got something other than the file"
    assert fake.opened.path == SHOW_FILE_PATH, (
        f"the proxy opened {fake.opened.path!r}. With `crm_activity_id` on the row the "
        "upstream is the copy Bitrix24 kept (§9 results); the provider URL is the fallback, "
        "not the first choice."
    )
    asked = fake.calls_to("crm.activity.get")
    assert len(asked) == 1, f"expected one crm.activity.get, saw {len(asked)}"
    assert str(ACTIVITY_ID) in set(asked[0].params.values()), (
        f"the activity asked for is not the row's `crm_activity_id`: {asked[0].params!r}"
    )
    assert fake.media[0].headers.get("range") == "bytes=0-", (
        "the browser's Range must reach the copy unchanged - seeking a forty-minute "
        "recording is the reason this endpoint streams instead of downloading (§9 step 3)"
    )


async def test_a_portal_that_never_attached_its_recordings_still_plays(
    client: httpx.AsyncClient, portal: SeededPortal, proxy_mode: None
) -> None:
    """When the copy cannot be resolved, `call_record_url` is still streamed.

    Nothing about `FILES` is promised. `crm.activity.fields` types it `diskfile` and the
    `crm.activity.list` reference shows the URL shape, but that a *telephony recording*
    lands there is measured, not documented - so it can be absent on a portal whose
    integration never called `telephony.externalCall.attachRecord`, and the REST call can
    fail outright for a viewer who may not read that activity. Both were working playbacks
    before this change, and a change that only works on portals shaped like ours is a
    regression for every other one.

    Two failures, because they arrive on different code paths: an answer carrying no usable
    file (the resolver returns None) and an answer that never comes (it raises).
    """
    call_id = await seed_call(portal.portal_id, 1, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    unattached = RecordingBitrix().on("crm.activity.get", activity([]))
    with patch_httpx(unattached):
        no_copy = await fetch_record(client, call_id, grant, headers={"Range": "bytes=0-"})

    refused = RecordingBitrix().on("crm.activity.get", Err("access_denied"))
    with patch_httpx(refused):
        raised = await fetch_record(client, call_id, grant, headers={"Range": "bytes=0-"})

    for label, response, fake in (
        ("the activity carries no file", no_copy, unattached),
        ("crm.activity.get failed", raised, refused),
    ):
        assert response.status_code == 206, (
            f"{label}: playback answered {response.status_code} ({response.text[:200]}) "
            "instead of falling back to the URL the row has always carried"
        )
        assert response.content == AUDIO, f"{label}: the client did not get the file"
        assert fake.opened.path == PROVIDER_PATH, (
            f"{label}: the proxy opened {fake.opened.path!r} rather than the stored "
            "`call_record_url`"
        )


async def test_the_portal_token_never_reaches_the_browser(
    client: httpx.AsyncClient, portal: SeededPortal, proxy_mode: None
) -> None:
    """The client sees audio bytes and our own headers - never the upstream URL.

    The resolved URL is a bearer credential in disguise: `auth` is the entire gate, it is
    the portal's access token with `crm` scope, and it works from anywhere with no cookie
    and no session. Handing it to the browser would be decision 22's forbidden `redirect`
    arriving by accident - through a `Location`, a diagnostic header, or an error body that
    quotes what it tried to open. §3 decision 21 and §4.6 say the same thing: the upstream
    URL is read into a local, used once, and dropped.

    The scan covers the whole response, headers included, and looks for the token this test
    planted *and* whatever token the app actually presented - so it does not quietly stop
    proving anything if §4.7's admin/viewer token choice is ever revisited.
    """
    call_id = await seed_call(portal.portal_id, 1, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    fake = RecordingBitrix().on("crm.activity.get", activity([file_entry(FILE_ID)]))
    with patch_httpx(fake):
        response = await fetch_record(client, call_id, grant, headers={"Range": "bytes=0-"})

    assert response.status_code == 206, f"{response.status_code}: {response.text[:200]}"
    presented = {r.access_token for r in fake.calls_to("crm.activity.get") if r.access_token}
    assert presented, (
        "the resolver made no authenticated REST call, so a scan for the token it used "
        "would prove nothing"
    )

    exposed = "\n".join(
        [
            str(response.url),
            *(f"{name}: {value}" for name, value in response.headers.items()),
            response.content.decode("latin-1"),
        ]
    )
    for secret in {SEED_ACCESS, *presented}:
        assert secret not in exposed, (
            "the portal access token reached the browser in the playback response - with "
            "`crm` scope, for the life of that token (decision 22 / §9 step 5)"
        )
    for marker in ("crm_show_file.php", "auth=", show_file_url(FILE_ID)):
        assert marker not in exposed, (
            f"{marker!r} reached the browser. The upstream URL is not something a media "
            "proxy relays - it is the credential (§3 decision 21)."
        )


@pytest.fixture()
def raw_log_records() -> Iterator[list[logging.LogRecord]]:
    """Every record the process produces, captured BEFORE `RedactingFilter` sees it.

    The opposite of `test_secret_logging`'s fixture, deliberately. That one reads the
    production formatter's output, because its question is "what does the deployed process
    print". The question here is narrower and stricter: a live listen-link must never be
    handed to the logging machinery at all. Checking only the second layer would accept
    `_log.info(..., extra={"url": url})`, which survives every formatter change, every new
    `extra` key, and every hurried widening of the redaction regex.

    `setup_logging()` runs first because it is what pins `httpx` and `httpcore` to WARNING;
    at INFO they log the full request URL, which on this path *is* the credential.
    """
    setup_logging()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = _Capture()
    # Ahead of the production handler: `RedactingFilter` mutates the record in place, so a
    # handler added after it would only ever see already-tidied text.
    root.handlers.insert(0, handler)
    previous = root.level
    root.setLevel(logging.DEBUG)  # the chattiest possible run
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def rendered(record: logging.LogRecord) -> str:
    """One log record as a searchable string: the message plus every structured field."""
    fields = {key: value for key, value in record.__dict__.items() if key not in ("msg", "args")}
    return f"{record.getMessage()} {json.dumps(fields, default=str, ensure_ascii=False)}"


async def test_a_resolved_listen_link_never_reaches_a_log_line(
    client: httpx.AsyncClient,
    portal: SeededPortal,
    proxy_mode: None,
    raw_log_records: list[logging.LogRecord],
) -> None:
    """§6: no token may reach a log - and this URL is a token with a hostname attached.

    Container stdout is the second moderation trail (§6) and is kept for as long as the
    operator keeps it, which is the opposite of the five-minute blast radius §4.6 designed
    the `?t=` grant around. One `extra={"url": ...}` on the "upstream stopped mid-stream"
    branch would put a working, reusable, portal-scoped listen-link into that trail - which
    is why the existing code already logs `type(exc).__name__` rather than `str(exc)`:
    httpx embeds the request URL in its messages.
    """
    call_id = await seed_call(portal.portal_id, 1, activity_id=ACTIVITY_ID, record_file_id=FILE_ID)
    grant = issue_play_token(pid=portal.portal_id, sub=USER, acc="all", cid=call_id)

    fake = RecordingBitrix().on("crm.activity.get", activity([file_entry(FILE_ID)]))
    with patch_httpx(fake):
        response = await fetch_record(client, call_id, grant, headers={"Range": "bytes=0-"})

    assert response.status_code == 206, f"{response.status_code}: {response.text[:200]}"
    logged = [rendered(entry) for entry in raw_log_records]
    assert logged, (
        "no log records were captured at all - every assertion below would pass on a "
        "fixture that had silently stopped working (the request middleware logs one line "
        "per request, so there is always at least that one)"
    )

    haystack = "\n".join(logged)
    presented = {r.access_token for r in fake.calls_to("crm.activity.get") if r.access_token}
    for needle in ("crm_show_file.php", "auth=", SEED_ACCESS, *presented):
        assert needle not in haystack, (
            f"{needle!r} was written to a log record during playback. The resolved URL "
            "carries a live access token and is not loggable in any form (§6)."
        )
