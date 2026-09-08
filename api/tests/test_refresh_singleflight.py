"""§5.8 step 3 - two callers, one refresh. Against a real PostgreSQL, on purpose.

The refresh token is **single use** (research note (b)): the OAuth server rotates it on
every exchange and invalidates the previous value. So two workers that both see
`expired_token` at the same moment and both refresh do not merely waste a request - the
loser writes a token pair that the winner's exchange already invalidated, and the portal
loses sync until an admin re-authorizes. §5.8 step 3 prevents that with
`SELECT ... FOR UPDATE` on the `portals` row plus a `token_version` re-check.

That mechanism is *entirely* database behaviour: a mocked session, or an in-process
`asyncio.Lock`, would pass this test while the production path raced. So this module
uses the `app_engine` fixture - the same engine `control_txn` uses, owned by the
NOBYPASSRLS `ca_app` role - and forces genuine overlap with a slow OAuth transport.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError
from app.bitrix.oauth import with_portal_token
from app.db.session import control_txn
from app.security.crypto import decrypt
from tests.fixtures.bitrix import (
    CLIENT_ENDPOINT,
    FRESH_ACCESS,
    FRESH_REFRESH,
    SEED_ACCESS,
    SEED_REFRESH,
    Err,
    FakeBitrix,
    SeededPortal,
    delete_portal,
    new_member_id,
    patch_httpx,
    seed_portal,
    token_response,
)

pytestmark = pytest.mark.asyncio

#: Long enough that the second caller is provably still inside `with_portal_token` when
#: the first one is mid-exchange, short enough not to slow the suite down. §5.8 bounds
#: the real exchange at 15 s and holds the row lock across it, so this is in scale.
OAUTH_LATENCY_SEC = 0.35


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal(token_version=7)
    try:
        yield seeded
    finally:
        await delete_portal(seeded.member_id)


async def read_credential(portal_id: int, member_id: str) -> dict[str, object]:
    """Decrypt the stored pair the way the worker would (AAD `member_id:column`)."""
    async with control_txn() as session:
        row = (
            await session.execute(
                text(
                    "SELECT token_version, access_token_enc, refresh_token_enc, "
                    "client_endpoint, token_refreshed_at, token_expires_at "
                    "FROM portals WHERE id = :pid"
                ),
                {"pid": portal_id},
            )
        ).mappings().one()
    return {
        "token_version": row["token_version"],
        "access": decrypt(row["access_token_enc"], member_id=member_id, column="access_token"),
        "refresh": decrypt(row["refresh_token_enc"], member_id=member_id, column="refresh_token"),
        "client_endpoint": row["client_endpoint"],
        "token_refreshed_at": row["token_refreshed_at"],
        "token_expires_at": row["token_expires_at"],
    }


async def test_two_concurrent_callers_perform_exactly_one_refresh(
    portal: SeededPortal,
) -> None:
    """§5.8 step 3: the loser observes the bumped `token_version` and skips the exchange.

    Both callers see `expired_token` on their first attempt. One takes the row lock and
    refreshes; the other blocks on `FOR UPDATE`, wakes with `token_version` moved, and
    retries with the STORED token rather than burning the (now invalid) refresh token.
    """
    fake = FakeBitrix()
    fake.oauth_latency = OAUTH_LATENCY_SEC
    fake.on_oauth(token_response(member_id=portal.member_id))
    # Every REST attempt made with the seeded (expired) token fails; anything made with
    # a token the OAuth server issued succeeds. Scripting it this way - rather than
    # "fail the first N calls" - means the test cannot accidentally pass because the
    # callers happened to interleave in a convenient order.
    fake.on(
        "user.admin",
        lambda record: True if record.access_token == FRESH_ACCESS else Err("expired_token"),
    )

    tokens_seen: dict[str, list[str]] = {"a": [], "b": []}

    def caller(name: str) -> Callable[[str], Awaitable[bool]]:
        async def work(access_token: str) -> bool:
            tokens_seen[name].append(access_token)
            async with BitrixClient(
                endpoint=CLIENT_ENDPOINT, access_token=access_token,
                portal_id=portal.portal_id, member_id=portal.member_id,
            ) as client:
                result = await client.call("user.admin")
            return bool(result)

        return work

    with patch_httpx(fake):
        results = await asyncio.gather(
            with_portal_token(portal.portal_id, caller("a")),
            with_portal_token(portal.portal_id, caller("b")),
        )

    assert results == [True, True], "both callers must complete their work"
    assert fake.oauth_count == 1, (
        "the refresh token is single use: a second exchange would invalidate the pair "
        "the winner just stored (§5.8 step 3)"
    )

    # Each caller: one attempt with the old token, one retry with the new one. The
    # loser's retry token proves it read the row again instead of reusing its stale copy.
    for name in ("a", "b"):
        assert tokens_seen[name][0] == SEED_ACCESS, f"{name} started from the stored token"
        assert tokens_seen[name][-1] == FRESH_ACCESS, f"{name} retried with the refreshed token"
        assert len(tokens_seen[name]) == 2, f"caller {name} attempted more than twice (§5.8 step 5)"

    stored = await read_credential(portal.portal_id, portal.member_id)
    assert stored["token_version"] == 8, "bumped exactly once, by the winner only"
    assert stored["access"] == FRESH_ACCESS
    assert stored["refresh"] == FRESH_REFRESH
    assert stored["token_refreshed_at"] is not None


async def test_four_concurrent_callers_still_perform_exactly_one_refresh(
    portal: SeededPortal,
) -> None:
    """The lock must serialise, not merely narrow the window: with four callers a
    "check then refresh" implementation without `FOR UPDATE` reliably fires twice."""
    fake = FakeBitrix()
    fake.oauth_latency = OAUTH_LATENCY_SEC
    fake.on_oauth(token_response(member_id=portal.member_id))
    fake.on(
        "user.admin",
        lambda record: True if record.access_token == FRESH_ACCESS else Err("expired_token"),
    )

    async def work(access_token: str) -> bool:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return bool(await client.call("user.admin"))

    with patch_httpx(fake):
        results = await asyncio.gather(
            *(with_portal_token(portal.portal_id, work) for _ in range(4))
        )

    assert results == [True] * 4
    assert fake.oauth_count == 1
    assert (await read_credential(portal.portal_id, portal.member_id))["token_version"] == 8


async def test_a_caller_whose_refresh_fails_leaves_the_credential_untouched(
    portal: SeededPortal,
) -> None:
    """§4.4 step 6: "A failed opportunistic re-seed leaves the existing credential
    untouched." A half-written pair here would strand the portal with a token nobody
    holds the matching refresh for."""
    fake = FakeBitrix()
    fake.on_oauth(Err("invalid_grant", "The passed refresh token is not valid", status=400))
    fake.on("user.admin", Err("expired_token"))

    async def work(access_token: str) -> bool:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return bool(await client.call("user.admin"))

    # `invalid_grant` classifies to InvalidGrant (errors.py), which is a BitrixError;
    # §5.8 makes it terminal (`token_status='reauth_required'`) rather than retryable.
    with patch_httpx(fake), pytest.raises(BitrixError):
        await with_portal_token(portal.portal_id, work)

    stored = await read_credential(portal.portal_id, portal.member_id)
    assert stored["token_version"] == 7, "nothing was written"
    assert stored["access"] == SEED_ACCESS
    assert stored["refresh"] == SEED_REFRESH


async def test_a_refresh_returning_a_different_member_id_writes_nothing(
    portal: SeededPortal,
) -> None:
    """§5.8 step 4 sanity-checks the response `member_id`.

    The credential is keyed by tenant and the ciphertext's AAD is `member_id:column`
    (decision 19); storing another portal's token here would produce a row that cannot
    even be decrypted, and - worse - would point this tenant's sync at that portal.
    """
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=new_member_id()))
    fake.on("user.admin", Err("expired_token"))

    async def work(access_token: str) -> bool:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return bool(await client.call("user.admin"))

    # The design does not pin an exception type for this case - only that nothing is
    # written - so the assertion that carries the weight is the one on the stored row.
    with patch_httpx(fake), pytest.raises(Exception):  # noqa: B017
        await with_portal_token(portal.portal_id, work)

    stored = await read_credential(portal.portal_id, portal.member_id)
    assert stored["token_version"] == 7
    assert stored["access"] == SEED_ACCESS
    assert stored["client_endpoint"] == CLIENT_ENDPOINT


async def test_two_different_portals_refresh_independently(app_engine: AsyncEngine) -> None:
    """The lock is per portal row, not global: one tenant's refresh must not serialise
    every other tenant's worker (§5.9 dispatches GLOBAL_PORTAL_CONCURRENCY at a time)."""
    first = await seed_portal(refresh=SEED_REFRESH + "aa")
    second = await seed_portal(refresh=SEED_REFRESH + "bb")
    # The refresh grant carries no member_id (grant_type/client_id/client_secret/
    # refresh_token only - research note (b)), so the fake answers by refresh token,
    # exactly as the real OAuth server has to.
    by_refresh = {first.refresh: first.member_id, second.refresh: second.member_id}
    try:
        fake = FakeBitrix()
        fake.oauth_latency = OAUTH_LATENCY_SEC
        fake.on_oauth(
            lambda record: token_response(
                member_id=by_refresh[record.params["refresh_token"]],
                access_token=FRESH_ACCESS,
                refresh_token=FRESH_REFRESH,
            )
        )
        fake.on(
            "user.admin",
            lambda record: True if record.access_token == FRESH_ACCESS else Err("expired_token"),
        )

        async def work(access_token: str) -> bool:
            async with BitrixClient(
                endpoint=CLIENT_ENDPOINT, access_token=access_token,
            ) as client:
                return bool(await client.call("user.admin"))

        with patch_httpx(fake):
            results = await asyncio.gather(
                with_portal_token(first.portal_id, work),
                with_portal_token(second.portal_id, work),
            )

        assert results == [True, True]
        assert fake.oauth_count == 2, "one exchange each - the lock is per row"
    finally:
        await delete_portal(first.member_id)
        await delete_portal(second.member_id)


# --- the expiry the refresh writes (§5.8 step 1) -------------------------------------
#
# `portals.token_expires_at` has exactly two writers - `store_portal_credential()` and
# `_refresh_locked()` here - and step 1 refreshes proactively whenever the stored value
# is within 60 s of now, with `rate_limit=False`. So a written expiry that is already in
# the past is not a cosmetic wrong number: it makes every following phase start with an
# OAuth exchange at our single `client_id`, which is exactly the "renewal on a schedule"
# §4.1 rule 3 says gets the whole application blocked - for every tenant, not this one.
# Both tests below drive the value the OAuth server sent through the real write path.


async def refresh_and_call(portal: SeededPortal) -> bool:
    """One `with_portal_token` visit whose stored token is refused, forcing a refresh."""

    async def work(access_token: str) -> bool:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return bool(await client.call("user.admin"))

    return await with_portal_token(portal.portal_id, work)


async def test_a_refresh_whose_expires_is_in_the_past_does_not_refresh_on_every_visit(
    portal: SeededPortal,
) -> None:
    """A stale absolute `expires` must be floored, exactly as the install writer floors it.

    `services/portals.py::_expires_at` degrades to `expires_in` when the absolute value
    is not plausibly in the future ("a portal whose clock is wrong ... must not persist
    an expiry in 1970 that makes every worker run refresh first"). The refresh writer
    must reach the same value from the same response, or the two disagree and the one
    without the floor wins on every subsequent visit.

    The second `with_portal_token` below is the whole point: it needs nothing from OAuth,
    and an exchange there is the loop §4.1 rule 3 forbids.
    """
    fake = FakeBitrix()
    stale = token_response(member_id=portal.member_id)
    # An hour in OUR past - a container clock ahead of the auth server, or an on-premise
    # build that sent `expires` as a relative value. Nothing else about it is unusual.
    stale["expires"] = int((dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1)).timestamp())
    fake.on_oauth(stale)
    fake.on(
        "user.admin",
        lambda record: True if record.access_token == FRESH_ACCESS else Err("expired_token"),
    )

    with patch_httpx(fake):
        assert await refresh_and_call(portal) is True
        assert fake.oauth_count == 1, "the stored token was refused, so one exchange is right"

        stored = await read_credential(portal.portal_id, portal.member_id)
        assert stored["access"] == FRESH_ACCESS
        expires_at = stored["token_expires_at"]
        assert isinstance(expires_at, dt.datetime)
        assert expires_at > dt.datetime.now(tz=dt.UTC), (
            "the refresh writer stored an expiry that is already in the past: §5.8 step 1 "
            "will now refresh before every phase"
        )

        # The next visit: the credential is fresh, so it must be used as it stands.
        assert await refresh_and_call(portal) is True

    assert fake.oauth_count == 1, (
        "a second exchange means the stored expiry looked stale to `_expires_soon()` - "
        "the rate-limit-exempt refresh loop of §4.1 rule 3"
    )


async def test_a_refresh_whose_expires_is_out_of_range_still_stores_the_rotated_pair(
    portal: SeededPortal,
) -> None:
    """An unusable `expires` must not cost the portal its refresh chain (§5.8).

    `dt.datetime.fromtimestamp()` raises on an out-of-range epoch. Raising *here* is
    unrecoverable in a way a wrong number is not: `exchange_refresh_token` has already
    succeeded, so the OAuth server has rotated the single-use pair, and an exception
    inside `control_txn` rolls back the write that keeps it - the portal is left holding
    a spent refresh token and dies at `invalid_grant` on the next visit.
    """
    fake = FakeBitrix()
    absurd = token_response(member_id=portal.member_id)
    absurd["expires"] = 99_999_999_999_999  # year 3170843
    fake.on_oauth(absurd)
    fake.on(
        "user.admin",
        lambda record: True if record.access_token == FRESH_ACCESS else Err("expired_token"),
    )

    with patch_httpx(fake):
        assert await refresh_and_call(portal) is True

    stored = await read_credential(portal.portal_id, portal.member_id)
    assert stored["token_version"] == 8, "the exchange happened, so its write must have too"
    assert stored["access"] == FRESH_ACCESS
    assert stored["refresh"] == FRESH_REFRESH, (
        "the rotated refresh token was discarded: the old one is already spent (§5.8)"
    )
    expires_at = stored["token_expires_at"]
    assert isinstance(expires_at, dt.datetime)
    assert expires_at > dt.datetime.now(tz=dt.UTC)
