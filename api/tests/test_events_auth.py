"""§4.9 — the lifecycle-event verification ladder, told as the attack it exists to stop.

`POST /events/` is the only endpoint in this application that a stranger can reach with a
body that flips a tenant off and schedules the destruction of its data, and the only
thing standing in front of it is a ladder of checks whose *order* is the security
property. This file walks that ladder as one narrative rather than as a list of unit
cases, because the finding the reviewers wrote up is a two-step chain and neither step is
alarming on its own:

    step 1  an employee (or anyone who learned the public `member_id`) posts ONAPPUPDATE
            carrying an `application_token` of their own choosing, hoping the handler
            treats "the event told me the token" as "the token is now this";
    step 2  they post ONAPPUNINSTALL presenting that same value, and the portal - with its
            calls, its employees and its cached CRM contexts - is marked uninstalled and
            queued for purge.

Rule 2 of §4.9 ("`application_token` first", constant-time equality against the *stored*
value) breaks the chain at step 1, and rule 4 makes ONAPPUPDATE the single event that may
replace that stored value and only behind a proven administrator. So the assertions here
are almost always about what did **not** change: `application_token_enc` byte for byte,
`portals.status`, the customer rows. A handler that answers 403 while having already
written the attacker's token would pass a status-code-only test and lose the portal on the
next request.

The other three shapes in here are the ones that cost data rather than leak it:

* rule 1 idempotency - a *retried* uninstall whose `ts` predates `install_completed_at` is
  a 200 no-op, because Bitrix24 retries webhooks and the portal may have been reinstalled
  in between; without this rule the retry wipes the reinstall;
* rule 3's "no REST calls" - access is already revoked at uninstall, so a round trip there
  is a guaranteed-failing call inside a webhook that must answer fast (it is asserted as a
  *count*, since the only way to keep a "never" true is to count it);
* rule 6 - a token-bearing event for a portal we have never seen may create a tenant only
  when the refresh exchange proves the `member_id` **and** `user.admin` proves the token is
  an administrator's; either half alone creates nothing.

Everything runs through the real FastAPI app over `httpx.ASGITransport` against the shared
Bitrix24 fake, so "makes no REST call" and "made exactly one exchange" are observations of
the wire, not of a mock's call list.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from app.main import create_app
from app.security.crypto import decrypt
from tests.fixtures.bitrix import (
    APP_KEY,
    CLIENT_ENDPOINT,
    DOMAIN,
    FRESH_ACCESS,
    FRESH_REFRESH,
    SEED_ACCESS,
    SEED_REFRESH,
    Err,
    FakeBitrix,
    RecordedRequest,
    SeededPortal,
    delete_portal,
    new_member_id,
    patch_httpx,
    portal_snapshot,
    portal_sync_snapshot,
    seed_portal,
    token_response,
    user_current,
)

#: §4.9 is registered in the vendor cabinet as the "Event installation handler URL".
EVENTS_PATH: Final[str] = "/events/"

#: A value the attacker chose. Shaped to pass §4.2's `APPLICATION_TOKEN` allowlist
#: (`[A-Za-z0-9]{8,128}`) on purpose: a body rejected by the form parser would prove
#: nothing about the ladder that runs after it.
ATTACKER_KEY: Final[str] = "attackerkey99887766554433221100ffeeddccbb"

#: What a genuine version update rotates the application token to (§4.9 rule 4).
ROTATED_KEY: Final[str] = "rotatedapplicationkey00112233445566778899"

#: `auth[access_token]` / `auth[refresh_token]` of the event body. Distinct from the
#: portal's stored pair so a handler that "proved" the stored credential instead of the
#: presented one is visible in the fake's request log.
EVENT_ACCESS: Final[str] = "eventaccess.a1b2c3d4e5f60718293a4b5c6d7e8f90"
EVENT_REFRESH: Final[str] = "eventrefresh.0f9e8d7c6b5a49382716f5e4d3c2b1a0"


# --- payloads -------------------------------------------------------------------------


def event_form(
    event: str,
    *,
    member_id: str,
    application_token: str | None = APP_KEY,
    ts: int | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    data: dict[str, str] | None = None,
    client_endpoint: str = CLIENT_ENDPOINT,
    domain: str = DOMAIN,
    scope: str = "crm,telephony,user_brief,placement",
) -> dict[str, str]:
    """One lifecycle event in the PHP-bracket form encoding Bitrix24 actually posts (§4.2).

    `ts` defaults to "now" because rule 1 compares it against `portals.last_event_ts` and
    `install_completed_at`; the tests that care about staleness pass it explicitly.
    """
    body: dict[str, str] = {
        "event": event,
        "ts": str(int(datetime.now(tz=UTC).timestamp()) if ts is None else ts),
        "auth[member_id]": member_id,
        "auth[client_endpoint]": client_endpoint,
        "auth[domain]": domain,
        "auth[scope]": scope,
    }
    if application_token is not None:
        body["auth[application_token]"] = application_token
    if access_token is not None:
        body["auth[access_token]"] = access_token
    if refresh_token is not None:
        body["auth[refresh_token]"] = refresh_token
    for key, value in (data or {}).items():
        body[f"data[{key}]"] = value
    return body


# --- database helpers -----------------------------------------------------------------


async def seed_calls(portal_id: int, count: int = 3) -> None:
    """A handful of customer rows, so "the portal survived" can be checked as *data*.

    Under `tenant_txn`: `calls` carries FORCED RLS (§3) and an insert from a control
    transaction is silently rejected by the WITH CHECK, which would make every
    "rows intact" assertion below trivially true.
    """
    started = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    async with tenant_txn(portal_id) as session:
        for offset in range(count):
            await session.execute(
                text(
                    """
                    INSERT INTO calls (portal_id, bx_id, call_id, call_type, call_start_date,
                                       call_duration, call_failed_code, portal_user_id,
                                       phone_number)
                    VALUES (:pid, :bx_id, :call_id, 1, :started, 60, '200', 101, :phone)
                    """
                ),
                {
                    "pid": portal_id,
                    "bx_id": 9000 + offset,
                    "call_id": f"event-call-{offset}",
                    "started": started + timedelta(minutes=offset),
                    "phone": f"+9989011100{offset:02d}",
                },
            )


async def count_calls(portal_id: int) -> int:
    async with tenant_txn(portal_id) as session:
        return int((await session.execute(text("SELECT count(*) FROM calls"))).scalar_one())


async def wipe_calls(portal_id: int) -> None:
    """Explicit teardown under tenant context - `delete_portal` cannot reach RLS tables."""
    async with tenant_txn(portal_id) as session:
        for table in ("calls", "employees", "crm_contexts"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


async def set_install_completed(portal_id: int, when: datetime) -> None:
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET install_completed_at = :when WHERE id = :pid"),
            {"pid": portal_id, "when": when},
        )


async def insert_rest_log(member_id: str, portal_id: int, *, count: int = 2) -> None:
    """Moderation rows with bodies, so the CLEAN=1 wipe has something to blank (§6)."""
    async with control_txn() as session:
        for index in range(count):
            await session.execute(
                text(
                    """
                    INSERT INTO rest_log (portal_id, member_id, direction, kind, method, url,
                                          request, response, http_status)
                    VALUES (:pid, :mid, 'out', 'rest', 'voximplant.statistic.get', :url,
                            CAST(:request AS jsonb), CAST(:response AS jsonb), 200)
                    """
                ),
                {
                    "pid": portal_id,
                    "mid": member_id,
                    "url": f"{CLIENT_ENDPOINT}voximplant.statistic.get",
                    "request": json.dumps({"FILTER": {">ID": index}}),
                    "response": json.dumps({"result": [{"ID": str(index), "PHONE_NUMBER": "+9989"}]}),
                },
            )


async def rest_log_bodies(portal_id: int) -> list[tuple[Any, Any]]:
    async with control_txn() as session:
        rows = (
            await session.execute(
                text("SELECT request, response FROM rest_log WHERE portal_id = :pid ORDER BY id"),
                {"pid": portal_id},
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


async def event_kinds(portal_id: int) -> list[str]:
    async with control_txn() as session:
        rows = (
            await session.execute(
                text("SELECT kind FROM portal_events WHERE portal_id = :pid ORDER BY id"),
                {"pid": portal_id},
            )
        ).scalars()
    return list(rows)


# --- fixtures -------------------------------------------------------------------------


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    """An installed, active tenant that already holds an `application_token` and rows."""
    seeded = await seed_portal(application_key=APP_KEY)
    await seed_calls(seeded.portal_id)
    try:
        yield seeded
    finally:
        await wipe_calls(seeded.portal_id)
        await delete_portal(seeded.member_id)


@pytest.fixture()
async def strangers(app_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """Member ids used by the unknown-portal cases, including ones that must NOT appear."""
    member_ids: list[str] = []
    try:
        yield member_ids
    finally:
        for member_id in member_ids:
            await delete_portal(member_id)


async def post_event(client: httpx.AsyncClient, body: dict[str, str]) -> httpx.Response:
    return await client.post(EVENTS_PATH, data=body)


# --- 1 + 2. the two-step attack -------------------------------------------------------


async def test_a_forged_application_token_is_refused_and_never_replaces_the_stored_one(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.9 rules 2 and 4, as the chain the review found.

    Step 1 is an ONAPPUPDATE from a non-administrator carrying `ATTACKER_KEY`. Two
    independent rules refuse it - the presented token is not the stored one (rule 2), and
    ONAPPUPDATE may replace that token only behind `user.admin=true` (rule 4) - and the
    test insists on *both* outcomes: 403 **and** `application_token_enc` unchanged, byte
    for byte. A handler that answers 403 after writing the value would hand step 2 the
    key it needs, and no status code would show it.

    Step 2 then plays the attacker's value back as an ONAPPUNINSTALL. The portal must
    still be `active`, its cursors untouched and its calls still on disk: the whole point
    of rule 2 is that the uninstall path - the one that schedules data destruction - is
    unreachable without the token we actually stored.
    """
    before = await portal_snapshot(portal.member_id)
    assert before is not None
    calls_before = await count_calls(portal.portal_id)
    assert calls_before > 0, "the fixture must seed rows, or 'rows intact' proves nothing"

    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=portal.member_id))
    fake.on("user.admin", False)  # the event's access token belongs to a regular employee

    with patch_httpx(fake):
        update = await post_event(
            client,
            event_form(
                "ONAPPUPDATE",
                member_id=portal.member_id,
                application_token=ATTACKER_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "4"},
            ),
        )

    assert update.status_code == 403, (
        "§4.9 rule 2: an event that does not present the STORED application_token is "
        "rejected outright, whatever else it carries."
    )
    after_update = await portal_snapshot(portal.member_id)
    assert after_update is not None
    assert after_update["application_token_enc"] == before["application_token_enc"], (
        "the forged token was written to the row. The 403 is cosmetic: the next event "
        "presenting ATTACKER_KEY now passes rule 2."
    )
    assert after_update["status"] == "active"

    # --- step 2: the value the attacker tried to install, used for real ----------------
    with patch_httpx(fake):
        uninstall = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=portal.member_id,
                application_token=ATTACKER_KEY,
                data={"VERSION": "4", "CLEAN": "0"},
            ),
        )

    assert uninstall.status_code == 403
    after_uninstall = await portal_snapshot(portal.member_id)
    assert after_uninstall is not None
    assert after_uninstall["status"] == "active", "a forged token must not be able to uninstall"
    assert after_uninstall["purge_pending"] is False, "and must not queue the tenant for purge"
    assert after_uninstall["access_token_enc"] == before["access_token_enc"]
    assert after_uninstall["refresh_token_enc"] == before["refresh_token_enc"]
    assert await count_calls(portal.portal_id) == calls_before, (
        "the customer's calls were touched by a request that was supposed to be rejected"
    )
    assert "event_rejected" in await event_kinds(portal.portal_id), (
        "§4.9 rules 2/4 require portal_events(event_rejected); support has no other trail "
        "for an attack that leaves the database unchanged by design."
    )


# --- 3. the genuine uninstall ----------------------------------------------------------


async def test_a_genuine_uninstall_flips_the_portal_keeps_the_token_and_makes_no_rest_call(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.9 rule 3, column by column.

    The state transition is the contract the worker's purge (§5.9) reads: `purge_pending`
    is what schedules deletion, and `sync_generation+1` is what fences a run that is in
    flight right now - without the bump, that runner's next cursor UPDATE would re-insert
    rows behind the purge.

    `application_token_enc` is deliberately **kept**: Bitrix24 retries webhooks, and the
    duplicate uninstall must still be verifiable (rule 2) rather than sailing past an
    empty compare.

    And no REST call, asserted as a count. The design says it plainly - access is revoked
    at uninstall, so a `placement.unbind` would be a guaranteed-failing round trip inside
    a webhook that must return 200 fast - and a "never" is only kept by counting it.
    """
    before = await portal_snapshot(portal.member_id)
    sync_before = await portal_sync_snapshot(portal.portal_id)
    assert before is not None and sync_before is not None
    calls_before = await count_calls(portal.portal_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=portal.member_id,
                application_token=APP_KEY,
                data={"VERSION": "3", "CLEAN": "0"},
            ),
        )

    assert response.status_code == 200, "Bitrix24 must see a fast, successful acknowledgement"
    assert fake.rest_count == 0 and fake.oauth_count == 0, (
        f"{fake.rest_count} REST and {fake.oauth_count} OAuth round trips during an "
        "uninstall: the app's access is already revoked, so every one of them is a "
        "guaranteed failure inside a webhook that must answer immediately (§4.9 rule 3)."
    )

    after = await portal_snapshot(portal.member_id)
    sync_after = await portal_sync_snapshot(portal.portal_id)
    assert after is not None and sync_after is not None
    assert after["status"] == "uninstalled"
    assert after["uninstalled_at"] is not None
    assert after["access_token_enc"] is None and after["refresh_token_enc"] is None
    assert after["token_status"] == "reauth_required"
    assert after["placements"] == {}
    assert after["purge_pending"] is True, "the worker has no other signal to purge on (§5.9)"
    assert after["purge_bodies"] is False, "CLEAN=0 keeps the moderation bodies (§6)"
    assert after["application_token_enc"] == before["application_token_enc"], (
        "§4.9 rule 3 keeps the application token so a RETRIED uninstall is still verifiable"
    )
    assert int(sync_after["sync_generation"]) > int(sync_before["sync_generation"]), (
        "without the generation bump an in-flight sync run re-inserts rows behind the purge"
    )
    assert sync_after["lease_owner"] is None
    assert await count_calls(portal.portal_id) == calls_before, (
        "the handler must return 200 immediately; deletion is the worker's job (§5.9), and "
        "doing it inline would put a 500k-row DELETE inside a webhook timeout."
    )
    assert "uninstall" in await event_kinds(portal.portal_id)


# --- 4. the retried uninstall that arrives after a reinstall ---------------------------


async def test_an_uninstall_older_than_the_install_is_a_no_op_and_spares_the_reinstall(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.9 rule 1: "`ONAPPUNINSTALL` earlier than `install_completed_at` is a logged 200 no-op".

    The scenario is ordinary rather than exotic: the moderator uninstalls, our events URL
    is briefly unreachable, they reinstall, and Bitrix24's retry of the *first* webhook
    lands afterwards. The body is perfectly authentic - it presents the stored
    `application_token`, so rules 2 and 3 would happily wipe the tenant that was just
    rebuilt. Only the `ts` comparison can tell the two apart.

    200 rather than 403 on purpose: this is a duplicate, not an attack, and answering an
    error would make Bitrix24 retry it again.
    """
    reinstalled_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    await set_install_completed(portal.portal_id, reinstalled_at)
    stale_ts = int((reinstalled_at - timedelta(days=2)).timestamp())
    calls_before = await count_calls(portal.portal_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=portal.member_id,
                application_token=APP_KEY,
                ts=stale_ts,
                data={"VERSION": "3", "CLEAN": "1"},
            ),
        )

    assert response.status_code == 200, "a retry is a duplicate, not an error to re-deliver"
    after = await portal_snapshot(portal.member_id)
    assert after is not None
    assert after["status"] == "active", (
        "a webhook retry from before the reinstall uninstalled the portal that had just "
        "been reinstalled - §4.9 rule 1 exists for exactly this."
    )
    assert after["purge_pending"] is False
    assert after["purge_bodies"] is False, "and CLEAN=1 on a no-op must not arm the body wipe"
    assert after["access_token_enc"] is not None, "the fresh credential must survive"
    assert await count_calls(portal.portal_id) == calls_before


# --- 5. the genuine version update -----------------------------------------------------


async def test_an_admin_proven_app_update_does_replace_the_application_token(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.9 rule 4 - the control for test 1, and the reason it cannot simply be "never".

    Without this case the safe implementation is "ONAPPUPDATE never writes anything",
    which passes every refusal test in this file and silently breaks the app the day
    Bitrix24 rotates the application token: every later event fails rule 2 and the
    uninstall webhook stops working, taking brief rule 7 with it.

    Two proofs, both required: `auth[access_token]` must pass `user.current` + `user.admin`
    at the **stored** `client_endpoint` (never at `DOMAIN`, §4.1), and `auth[refresh_token]`
    must exchange to the *same* `member_id`. Anything less is the 403 of test 1.
    """
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=portal.member_id))  # exchanges to the same tenant
    fake.on("user.admin", True)

    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUPDATE",
                member_id=portal.member_id,
                application_token=ROTATED_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "5"},
            ),
        )

    assert response.status_code == 200
    after = await portal_snapshot(portal.member_id)
    assert after is not None
    stored = decrypt(
        bytes.fromhex(str(after["application_token_enc"])),
        member_id=portal.member_id,
        column="application_token",
    )
    assert stored == ROTATED_KEY, (
        "a version update proven by an administrator is the ONE path allowed to replace "
        "the application token (§4.9 rule 4); refusing it wedges every later event."
    )
    assert after["status"] == "active"
    assert fake.oauth_count == 1, (
        "exactly one exchange proves the member_id; a second would be the 'refreshing too "
        "often' behaviour Bitrix24 blocks applications for (§4.1, decision 11)."
    )
    assert fake.rest_count >= 1, (
        "no REST round trip happened, so `user.current` + `user.admin` were never asked: "
        "the token was accepted on the strength of the body alone (§4.9 rule 4)."
    )


# --- 6. token-bearing events for a portal we have never seen ---------------------------


async def test_an_unknown_portal_is_created_only_when_both_proofs_succeed(
    client: httpx.AsyncClient, strangers: list[str]
) -> None:
    """§4.9 rule 6: the exchange AND the `user.admin` proof, or no tenant at all.

    `member_id` is public (§4.1 / decision 1), so a token-bearing event for an unknown
    portal is an offer, not evidence. Two things have to be true before a row exists:
    the refresh token really belongs to that `member_id` (only the OAuth server can say
    so), and it belongs to an administrator - because whatever is stored here becomes the
    worker's credential forever, and a regular employee's token would cache a silently
    truncated slice of the portal's calls (§4.1 credential invariant, decision 4).

    All three branches live in one test so the "created" case is compared against the two
    refusals under identical conditions; three separate tests would let a handler that
    never creates anything pass two of them.
    """
    failed_exchange, non_admin, genuine = (new_member_id() for _ in range(3))
    strangers.extend([failed_exchange, non_admin, genuine])

    # (a) the refresh token does not exchange: nothing proves the member_id at all.
    fake = FakeBitrix()
    fake.on_oauth(Err("invalid_grant", "unknown refresh token"))
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPINSTALL",
                member_id=failed_exchange,
                application_token=ATTACKER_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "1"},
            ),
        )
    assert response.status_code < 500
    assert await portal_snapshot(failed_exchange) is None, (
        "a tenant was conjured from a form post whose refresh token proved nothing (§4.9 rule 6)"
    )

    # (b) the exchange succeeds, but the token belongs to a regular employee.
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=non_admin))
    fake.on("user.admin", False)
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPINSTALL",
                member_id=non_admin,
                application_token=ATTACKER_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "1"},
            ),
        )
    assert response.status_code < 500
    assert await portal_snapshot(non_admin) is None, (
        "a proven-but-non-admin token became a tenant. Its credential would be the sync "
        "worker's forever, and every dashboard in that portal would be quietly incomplete."
    )

    # (c) both proofs succeed: this, and only this, is an install.
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=genuine))
    fake.on("user.admin", True)
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPINSTALL",
                member_id=genuine,
                application_token=APP_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "1"},
            ),
        )
    assert response.status_code == 200
    created = await portal_snapshot(genuine)
    assert created is not None, (
        "the control case: if this does not create a portal, the two refusals above are "
        "passed by a handler that simply never writes."
    )
    assert created["status"] == "active"
    assert created["client_endpoint"] == CLIENT_ENDPOINT, (
        "§4.1 endpoint invariant: the REST base comes from the OAuth response, never DOMAIN"
    )
    assert created["application_token_enc"] is not None


# --- 7. CLEAN=1 arms the body wipe, and the worker performs it -------------------------


async def test_clean_uninstall_arms_purge_bodies_and_the_worker_blanks_rest_log_bodies(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.9 rule 3 + §5.9 + decision 21: what `data[CLEAN]=1` actually costs the tenant.

    Two halves that are only correct together. The handler must record the request
    (`purge_bodies`) and return, because the webhook may not run a 500k-row DELETE; the
    worker must then honour it. And the wipe is deliberately partial: the `rest_log`
    *rows* stay - method, URL, status, timing are the moderation trail §6 promises - while
    `request` and `response`, the parts that carried the customer's payloads, go.

    Asserting the rows survive matters as much as asserting the bodies are gone: a purge
    implemented as `DELETE FROM rest_log` would satisfy "the data is removed" and destroy
    the evidence that the app behaved correctly.
    """
    from app.sync.purge import purge_portal_data

    await insert_rest_log(portal.member_id, portal.portal_id, count=2)
    bodies_before = await rest_log_bodies(portal.portal_id)
    assert bodies_before and all(req is not None for req, _ in bodies_before), (
        "the fixture must write real bodies, or 'the bodies were blanked' is vacuous"
    )

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=portal.member_id,
                application_token=APP_KEY,
                data={"VERSION": "3", "CLEAN": "1"},
            ),
        )
    assert response.status_code == 200

    armed = await portal_snapshot(portal.member_id)
    assert armed is not None
    assert armed["purge_bodies"] is True, "data[CLEAN]=1 must reach portals.purge_bodies"
    assert armed["purge_pending"] is True

    outcome = await purge_portal_data(portal.portal_id)
    assert outcome.verified_empty, (
        "the purge must PROVE the tenant tables are empty before clearing the flag (§5.9)"
    )
    assert await count_calls(portal.portal_id) == 0

    bodies_after = await rest_log_bodies(portal.portal_id)
    assert len(bodies_after) == len(bodies_before), (
        "the moderation rows themselves must survive: CLEAN removes the customer's "
        "payloads, not the evidence of what the application did (§6)."
    )
    assert all(request is None and response_body is None for request, response_body in bodies_after), (
        "rest_log bodies survived a CLEAN=1 uninstall (decision 21). Note SQL NULL, not the "
        "JSON value `null`: the latter still stores the row's shape and re-matches IS NOT NULL."
    )
    assert outcome.bodies_redacted >= len(bodies_before)

    settled = await portal_snapshot(portal.member_id)
    assert settled is not None
    assert settled["purge_pending"] is False and settled["purge_bodies"] is False
    assert settled["application_token_enc"] is not None, (
        "even after a CLEAN purge the application token stays: it is what verifies the "
        "duplicate uninstall webhooks that are still coming (§4.9 rule 3)."
    )


# --- the seeded credential is never what an event authenticates against ----------------


async def test_no_event_is_authenticated_with_the_portals_own_stored_tokens(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.1: an event is proven by what it PRESENTS, never by what we already hold.

    A tempting shortcut in rule 4 is to run the admin proof with the portal's stored
    access token - it is guaranteed to be an administrator's, so the check always passes
    and the event is always accepted. That turns the proof into a formality: any body
    naming a `member_id` would then be able to replace the application token.

    The fake records the `auth=` parameter of every REST call, so the shortcut is directly
    observable on the wire.
    """
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=portal.member_id))
    fake.on("user.admin", True)

    with patch_httpx(fake):
        await post_event(
            client,
            event_form(
                "ONAPPUPDATE",
                member_id=portal.member_id,
                application_token=ROTATED_KEY,
                access_token=EVENT_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "5"},
            ),
        )

    used = {record.access_token for record in fake.of_kind("rest") + fake.of_kind("batch")}
    used |= {record.access_token for record in fake.of_kind("oauth")}
    assert SEED_ACCESS not in used, (
        "the admin proof ran with the PORTAL's stored access token instead of the token "
        "the event presented - the proof then proves nothing about the sender (§4.9 rule 4)."
    )
    presented = {
        record.params.get("refresh_token") for record in fake.of_kind("oauth")
    }
    assert SEED_REFRESH not in presented, (
        "the exchange used the stored refresh token, so it would succeed for any body "
        "naming this member_id - and it burns the portal's own refresh chain (§4.1)."
    )


# --- 8. the uninstall nothing can authenticate ------------------------------------------

#: A portal administrator's own one-hour bearer, as it reaches the handler in
#: `auth[access_token]`. Kept apart from `EVENT_ACCESS` because test 9 needs a body whose
#: two `auth[...]` halves belong to *different* people.
ADMIN_ACCESS: Final[str] = "adminaccess.3f8e1d7c5b9a2064e8f1c3a5d7b9e0f2"
#: A regular employee's pair, the kind anyone can read out of `BX24.getAuth()`.
EMPLOYEE_ACCESS: Final[str] = "employeeaccess.6a4c2e0f8d1b3957ae2c4068d1f3b5a7"
EMPLOYEE_REFRESH: Final[str] = "employeerefresh.b7d5f3a1c9e70826d4b2f0a8c6e4d2b0"

#: Who `user.current` answers with, so "whose credential got stored" is visible in the row.
ADMIN_USER_ID: Final[int] = 42
EMPLOYEE_USER_ID: Final[int] = 101


@pytest.fixture()
async def tokenless_portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    """An active tenant that holds NO `application_token`, with rows to lose.

    Not an exotic state: §4.9 rule 5 exists precisely for "a portal installed through
    `/install/` by a cabinet that sends no `APPLICATION_TOKEN` in the iframe POST", and
    `store_portal_credential` writes the column only when the value is present. §11
    assumption 8 also allows `ONAPPINSTALL` never to arrive for an app with an interface,
    so the portal can stay in this state indefinitely.
    """
    seeded = await seed_portal(application_key=None)
    await seed_calls(seeded.portal_id)
    try:
        yield seeded
    finally:
        await wipe_calls(seeded.portal_id)
        await delete_portal(seeded.member_id)


async def test_an_uninstall_is_refused_when_the_portal_has_no_stored_application_token(
    client: httpx.AsyncClient, tokenless_portal: SeededPortal
) -> None:
    """§4.9 rule 2 has to answer when there is nothing to compare against.

    The compare is conditioned on a stored token, and `ONAPPUNINSTALL` is the one event
    that by design carries no credential at all - no access token, no refresh token. So on
    a portal whose `application_token_enc` is NULL the body below proves *nothing*: its
    only content is a `member_id`, which §4.1 declares public (every vendor installed on
    that portal has it, and any employee can read it from `BX24.getAuth()`).

    Obeying it costs the tenant everything the uninstall transition costs: both API tokens
    NULLed, `purge_pending` armed, the cursors reset, and a worker that then deletes every
    cached call. So the answer is 403, and the genuine uninstall of such a portal is picked
    up by §5.8's inferred-uninstall sweep after `UNINSTALL_GRACE_DAYS` - the fallback §4.11
    already names for "uninstalls with the events URL unavailable".
    """
    before = await portal_snapshot(tokenless_portal.member_id)
    sync_before = await portal_sync_snapshot(tokenless_portal.portal_id)
    assert before is not None and sync_before is not None
    assert before["application_token_enc"] is None, (
        "the fixture must produce the NULL-token state, or this test proves nothing"
    )
    calls_before = await count_calls(tokenless_portal.portal_id)
    assert calls_before > 0

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=tokenless_portal.member_id,
                application_token=None,
                data={"VERSION": "3", "CLEAN": "1"},
            ),
        )

    assert response.status_code == 403, (
        "an ONAPPUNINSTALL that presented nothing but the public `member_id` was obeyed. "
        "Anyone who knows it can now wipe this tenant's cache (§4.9 rule 2)."
    )
    after = await portal_snapshot(tokenless_portal.member_id)
    sync_after = await portal_sync_snapshot(tokenless_portal.portal_id)
    assert after is not None and sync_after is not None
    assert after["status"] == "active"
    assert after["uninstalled_at"] is None
    assert after["purge_pending"] is False, "and must not queue the tenant for purge"
    assert after["purge_bodies"] is False
    assert after["access_token_enc"] == before["access_token_enc"]
    assert after["refresh_token_enc"] == before["refresh_token_enc"]
    assert after["token_status"] == before["token_status"]
    assert after["placements"] == before["placements"]
    assert int(sync_after["sync_generation"]) == int(sync_before["sync_generation"]), (
        "a generation bump alone breaks the tenant: any in-flight sync run is fenced off"
    )
    assert await count_calls(tokenless_portal.portal_id) == calls_before
    assert "event_rejected" in await event_kinds(tokenless_portal.portal_id), (
        "support needs the audit row: a refused uninstall leaves the database otherwise "
        "unchanged, so `portal_events` is the only trace that someone tried."
    )
    assert fake.rest_count == 0 and fake.oauth_count == 0, (
        "refusing must stay as cheap as obeying - an unauthenticated body may not spend a "
        "round trip, or the refusal becomes the amplifier (§4.9 rule 3)."
    )


async def test_an_uninstall_for_an_already_uninstalled_tokenless_portal_is_still_a_no_op(
    client: httpx.AsyncClient, strangers: list[str]
) -> None:
    """The control for the refusal above: rule 3's 200 no-op must survive it.

    Bitrix24 retries webhooks, and a 4xx is what makes it retry. A portal that is already
    `uninstalled` has nothing left to protect, so answering 403 there would only buy an
    endless redelivery loop for a message whose effect is already in place.
    """
    gone = await seed_portal(application_key=None, status="uninstalled")
    strangers.append(gone.member_id)

    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUNINSTALL",
                member_id=gone.member_id,
                application_token=None,
                data={"VERSION": "3"},
            ),
        )

    assert response.status_code == 200, (
        "a duplicate uninstall of an already-uninstalled portal must stay a 200 no-op "
        "(§4.9 rule 3); a 4xx makes Bitrix24 redeliver it forever."
    )


# --- 9. the credential that was never itself proven -------------------------------------


def _admin_only(*admin_tokens: str) -> tuple[Any, Any]:
    """Script `user.current` + `user.admin` so the answer depends on WHICH token asked.

    The fake scripts per method name, and every entry may be a callable over the recorded
    request - so this is how "is an administrator" becomes a property of a token rather
    than of the test. Without it a handler that proves one token and stores another is
    indistinguishable from one that proves the token it stores.
    """
    admins = frozenset(admin_tokens)

    def current(record: RecordedRequest) -> dict[str, Any]:
        is_admin = record.access_token in admins
        return user_current(user_id=ADMIN_USER_ID if is_admin else EMPLOYEE_USER_ID)

    def admin(record: RecordedRequest) -> bool:
        return record.access_token in admins

    return current, admin


async def test_an_app_update_reseeds_only_a_credential_whose_own_admin_standing_was_proven(
    client: httpx.AsyncClient, strangers: list[str]
) -> None:
    """§4.1: "...succeeded **with that token**", the half `open.py` spells out.

    `ONAPPUPDATE` presents two independent strings - `auth[access_token]` and
    `auth[refresh_token]` - and nothing in the protocol binds them to the same person. The
    handler proves the first and stores what the *second* exchanges into, so unless the
    exchanged pair is itself asked `user.admin`, a non-admin refresh chain paired with a
    borrowed administrator bearer becomes the sync worker's credential: the portal keeps
    working, and every dashboard on it silently shows that one employee's calls.

    (a) is that mix, on a portal whose `token_status != 'ok'` - the only state in which
    rule 4 re-seeds. (b) is the control, because "never re-seed" would pass (a) on its own
    and would leave a portal with a dead credential no `ONAPPUPDATE` can repair.
    """
    # --- (a) an administrator's bearer, an employee's refresh chain --------------------
    mixed = await seed_portal(token_status="reauth_required")
    strangers.append(mixed.member_id)
    before = await portal_snapshot(mixed.member_id)
    assert before is not None

    fake = FakeBitrix()
    fake.on_oauth(
        token_response(
            member_id=mixed.member_id,
            access_token=EMPLOYEE_ACCESS,
            refresh_token=EMPLOYEE_REFRESH,
            user_id=EMPLOYEE_USER_ID,
        )
    )
    current, admin = _admin_only(ADMIN_ACCESS)
    fake.on("user.current", current).on("user.admin", admin)

    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUPDATE",
                member_id=mixed.member_id,
                application_token=ATTACKER_KEY,
                access_token=ADMIN_ACCESS,
                refresh_token=EMPLOYEE_REFRESH,
                data={"VERSION": "9"},
            ),
        )

    assert EMPLOYEE_ACCESS in {
        record.access_token for record in fake.of_kind("batch") + fake.of_kind("rest")
    }, (
        "the token the handler was about to STORE was never itself asked `user.admin`. "
        "§4.1 admits only a credential that has answered that question for itself."
    )
    assert response.status_code == 403, (
        "an event whose two auth halves belong to two different people was accepted "
        "(§4.9 rule 4: anything less than both proofs is 403 + event_rejected)."
    )
    after = await portal_snapshot(mixed.member_id)
    assert after is not None
    stored_access = decrypt(
        bytes.fromhex(str(after["access_token_enc"])),
        member_id=mixed.member_id,
        column="access_token",
    )
    assert stored_access == SEED_ACCESS, (
        "the employee's access token is now the portal's sync credential: the worker will "
        "cache only the calls that one user can see, with no error anywhere."
    )
    assert after["refresh_token_enc"] == before["refresh_token_enc"]
    assert after["token_status"] == "reauth_required", (
        "a credential that failed its own admin proof must not be marked healthy"
    )
    assert after["application_token_enc"] == before["application_token_enc"], (
        "and the refused event must not leave the attacker holding the key to rule 2 - "
        "the application token write happens on the same accepted-event path."
    )
    assert "event_rejected" in await event_kinds(mixed.portal_id)

    # --- (b) the control: one administrator, both halves ------------------------------
    genuine = await seed_portal(token_status="reauth_required")
    strangers.append(genuine.member_id)

    fake = FakeBitrix()
    fake.on_oauth(
        token_response(
            member_id=genuine.member_id,
            access_token=FRESH_ACCESS,
            refresh_token=FRESH_REFRESH,
            user_id=ADMIN_USER_ID,
        )
    )
    current, admin = _admin_only(ADMIN_ACCESS, FRESH_ACCESS)
    fake.on("user.current", current).on("user.admin", admin)

    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPUPDATE",
                member_id=genuine.member_id,
                application_token=ROTATED_KEY,
                access_token=ADMIN_ACCESS,
                refresh_token=EVENT_REFRESH,
                data={"VERSION": "9"},
            ),
        )

    assert response.status_code == 200, (
        "a genuine version update from an administrator must still repair a broken "
        "credential (§4.9 rule 4); refusing it strands the portal for good."
    )
    repaired = await portal_snapshot(genuine.member_id)
    assert repaired is not None
    assert (
        decrypt(
            bytes.fromhex(str(repaired["access_token_enc"])),
            member_id=genuine.member_id,
            column="access_token",
        )
        == FRESH_ACCESS
    )
    assert repaired["token_status"] == "ok"
    assert repaired["token_user_id"] == ADMIN_USER_ID


async def test_an_unknown_portal_stores_only_the_exchanged_token_it_proved(
    client: httpx.AsyncClient, strangers: list[str]
) -> None:
    """§4.9 rule 6 + §4.1, the same seam on the path that CREATES a tenant.

    Here the whole portal row is conjured from the event, so the credential it starts life
    with is the exchange's - and that is the one `user.admin` has to be asked about. The
    body pairs an administrator's bearer with an employee's refresh chain again; the
    difference is that a wrong answer creates a tenant whose sync credential belongs to a
    regular employee from its very first backfill.
    """
    stranger = new_member_id()
    strangers.append(stranger)

    fake = FakeBitrix()
    fake.on_oauth(
        token_response(
            member_id=stranger,
            access_token=EMPLOYEE_ACCESS,
            refresh_token=EMPLOYEE_REFRESH,
            user_id=EMPLOYEE_USER_ID,
        )
    )
    current, admin = _admin_only(ADMIN_ACCESS)
    fake.on("user.current", current).on("user.admin", admin)

    with patch_httpx(fake):
        response = await post_event(
            client,
            event_form(
                "ONAPPINSTALL",
                member_id=stranger,
                application_token=ATTACKER_KEY,
                access_token=ADMIN_ACCESS,
                refresh_token=EMPLOYEE_REFRESH,
                data={"VERSION": "1"},
            ),
        )

    assert response.status_code < 500
    assert await portal_snapshot(stranger) is None, (
        "a tenant was created around a credential that never answered `user.admin` for "
        "itself (§4.1). Its worker would cache one employee's slice of the portal forever."
    )
