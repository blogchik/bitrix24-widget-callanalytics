"""§5.8 - "refresh once, retry once, then fail". Proven by counting HTTP requests.

The rule is a *budget*, not a behaviour: Bitrix24's documentation warns that excessive
refresh exchanges get an application blocked (research note (b)), and a retry loop on
`expired_token` is the classic way to produce them. So every assertion here is on the
exact number of round trips the fake transport saw, not on "did it eventually work".

Four cases, all from §5.8 step 2 and step 5:

* a single `call()` that returns `expired_token`     -> 1 OAuth + 2 REST, success
* a second `expired_token` after the refresh          -> 1 OAuth + 2 REST, raises
* `expired_token` inside a batch `result_error`       -> the same budget (step 2 says
  the check must look inside `result_error`, because a halt=0 batch reports auth
  failures per command at HTTP 200 - research note (e))
* any non-auth error                                  -> 0 OAuth, error to the caller

The refresh itself lives in `oauth.with_portal_token` (it needs the row lock and the
`token_version` of §5.8 step 3), so these tests drive that entry point with a `fn` that
builds a `BitrixClient` - which is exactly how the worker and the handlers use it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.errors import AccessDenied, BitrixError, ExpiredToken, QueryLimitExceeded
from app.bitrix.oauth import with_portal_token
from tests.fixtures.bitrix import (
    CLIENT_ENDPOINT,
    FRESH_ACCESS,
    SEED_ACCESS,
    Err,
    FakeBitrix,
    SeededPortal,
    delete_portal,
    patch_httpx,
    seed_portal,
    token_response,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    """One active portal holding `SEED_ACCESS`, whose refresh answers with `FRESH_ACCESS`."""
    seeded = await seed_portal()
    try:
        yield seeded
    finally:
        await delete_portal(seeded.member_id)


def fake_for(seeded: SeededPortal) -> FakeBitrix:
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=seeded.member_id))
    return fake


def surface_expired(result: BatchResult) -> BatchResult:
    """Re-raise an `expired_token` that a halt=0 batch reported per command.

    WHY this helper exists: the milestone-2 contract has `batch()` returning a
    `BatchResult` with per-command errors, while §5.8 step 2 requires the refresh
    decision to look inside `result_error`. A client that raises `ExpiredToken` for an
    auth failure anywhere in the batch satisfies the design directly; one that returns
    it needs the caller to surface it. This accepts both readings and still holds the
    implementation to the same request budget - the thing that actually matters.
    """
    for command in result.commands:
        if isinstance(command.error, ExpiredToken):
            raise command.error
    return result


# --- single call --------------------------------------------------------------------


async def test_expired_token_triggers_exactly_one_refresh_and_one_retry(
    portal: SeededPortal,
) -> None:
    """§5.8 steps 2-5: the first call fails, ONE exchange happens, ONE retry succeeds."""
    fake = fake_for(portal)
    # The last scripted entry repeats, so this is "fail once, then always succeed".
    fake.on("user.admin", Err("expired_token", "The access token provided has expired"), True)
    seen: list[str | None] = []

    async def work(access_token: str) -> Any:
        seen.append(access_token)
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
            member_id=portal.member_id,
        ) as client:
            return await client.call("user.admin")

    with patch_httpx(fake):
        result = await with_portal_token(portal.portal_id, work)

    assert result is True
    assert fake.oauth_count == 1, "exactly one refresh exchange (§5.8 step 4)"
    assert fake.rest_count == 2, "the original call plus exactly one retry (§5.8 step 5)"
    # The retry must use the NEW token; retrying with the same one is a wasted exchange.
    assert seen == [SEED_ACCESS, FRESH_ACCESS]
    assert fake.calls_to("user.admin")[1].access_token == FRESH_ACCESS


async def test_a_second_expired_token_propagates_instead_of_looping(
    portal: SeededPortal,
) -> None:
    """§5.8 step 5: "A second expired_token raises... No loop." The budget is the test."""
    fake = fake_for(portal)
    fake.on("user.admin", Err("expired_token"))  # every attempt fails the same way

    async def work(access_token: str) -> Any:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
            member_id=portal.member_id,
        ) as client:
            return await client.call("user.admin")

    with patch_httpx(fake), pytest.raises(ExpiredToken):
        await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 1, "the failed retry must NOT trigger a second exchange"
    assert fake.rest_count == 2, "one original attempt and one retry, and then stop"


async def test_a_non_auth_error_is_returned_without_any_refresh(
    portal: SeededPortal,
) -> None:
    """§5.8 step 2: "Anything other than expired_token ... is returned." An
    ACCESS_DENIED means the installer lost the Call-statistics right, and burning a
    refresh on it would neither fix it nor be free."""
    fake = fake_for(portal)
    fake.on("voximplant.statistic.get", Err("ACCESS_DENIED", "Access denied"))

    async def work(access_token: str) -> Any:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
            member_id=portal.member_id,
        ) as client:
            return await client.call("voximplant.statistic.get", {"start": 0})

    with patch_httpx(fake), pytest.raises(AccessDenied):
        await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 0, "a rights error must never consume a refresh"
    assert fake.rest_count == 1, "and must not be retried either"


async def test_a_throttle_error_is_returned_without_any_refresh(
    portal: SeededPortal,
) -> None:
    """§5.6: 503 QUERY_LIMIT_EXCEEDED is a throttle hit, not an auth problem. Refreshing
    on it would add a request to a bucket that is already overflowing."""
    fake = fake_for(portal)
    fake.on("voximplant.statistic.get", Err("QUERY_LIMIT_EXCEEDED", "Too many requests"))

    async def work(access_token: str) -> Any:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return await client.call("voximplant.statistic.get", {"start": 0})

    with patch_httpx(fake), pytest.raises(QueryLimitExceeded):
        await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 0
    assert fake.rest_count == 1


# --- batch (halt=0) -----------------------------------------------------------------


async def test_expired_token_inside_a_batch_result_error_refreshes_once(
    portal: SeededPortal,
) -> None:
    """§5.8 step 2 explicitly names `result_error`.

    A halt=0 batch returns HTTP 200 with per-command errors, so an implementation that
    only inspects the envelope's `error` and the HTTP status sees a *successful* batch
    whose commands all failed - the portal then never refreshes and sync dies silently
    until an admin re-authorizes.
    """
    fake = fake_for(portal)
    fake.on("user.current", Err("expired_token"), {"ID": "42", "TIME_ZONE": "Asia/Tashkent"})
    fake.on("user.admin", Err("expired_token"), True)

    async def work(access_token: str) -> BatchResult:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
            member_id=portal.member_id,
        ) as client:
            return surface_expired(
                await client.batch(
                    [("me", "user.current", {}), ("admin", "user.admin", {})], halt=0
                )
            )

    with patch_httpx(fake):
        result = await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 1, "one exchange for an auth failure reported per command"
    assert fake.rest_count == 2, "one batch, one retried batch - a batch is ONE request"
    assert result.ok("me") and result.ok("admin")
    assert result.get("admin") is True


async def test_a_second_batch_expired_token_propagates(portal: SeededPortal) -> None:
    fake = fake_for(portal)
    fake.on("user.admin", Err("expired_token"))

    async def work(access_token: str) -> BatchResult:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return surface_expired(
                await client.batch([("me", "user.current", {}), ("admin", "user.admin", {})])
            )

    with patch_httpx(fake), pytest.raises(ExpiredToken):
        await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 1
    assert fake.rest_count == 2


async def test_a_batch_command_failing_for_a_non_auth_reason_never_refreshes(
    portal: SeededPortal,
) -> None:
    """The contiguous-prefix rule of decision 10 needs a batch whose commands can fail
    individually WITHOUT the whole visit being treated as an auth problem."""
    fake = fake_for(portal)
    fake.on("voximplant.statistic.get", Err("INTERNAL_SERVER_ERROR", "Internal error"))

    async def work(access_token: str) -> BatchResult:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return await client.batch(
                [
                    ("p0", "voximplant.statistic.get", {"start": 0}),
                    ("p1", "voximplant.statistic.get", {"start": 50}),
                ]
            )

    with patch_httpx(fake):
        result = await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 0
    assert fake.rest_count == 1
    assert result.first_error_index == 0
    assert isinstance(result.error("p0"), BitrixError)
    assert result.get("p0") is None


async def test_batch_preserves_the_requested_order(portal: SeededPortal) -> None:
    """Decision 10: the cursor may only advance across the longest error-free PREFIX,
    which is meaningless unless `BatchResult.commands` is in the order we asked for."""
    fake = fake_for(portal)
    fake.on("voximplant.statistic.get", [], [], Err("OPERATION_TIME_LIMIT"), [], [])
    keys = [f"p{index}" for index in range(5)]

    async def work(access_token: str) -> BatchResult:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return await client.batch(
                [(key, "voximplant.statistic.get", {"start": index * 50})
                 for index, key in enumerate(keys)]
            )

    with patch_httpx(fake):
        result = await with_portal_token(portal.portal_id, work)

    assert [command.key for command in result.commands] == keys
    assert result.first_error_index == 2, "the hole is at p2; p0..p1 is the safe prefix"


# --- the whole batch failing at the HTTP level --------------------------------------


async def test_a_401_on_the_batch_envelope_also_refreshes_exactly_once(
    portal: SeededPortal,
) -> None:
    """§5.8 step 2 checks the JSON `error` first and the HTTP status second, so an
    envelope-level 401 must reach the same single-refresh path as a per-command one."""
    fake = fake_for(portal)
    fake.on("batch", Err("expired_token", status=401), None)

    async def work(access_token: str) -> BatchResult:
        async with BitrixClient(
            endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=portal.portal_id,
        ) as client:
            return surface_expired(await client.batch([("me", "user.current", {})]))

    with patch_httpx(fake):
        result = await with_portal_token(portal.portal_id, work)

    assert fake.oauth_count == 1
    assert fake.rest_count == 2
    assert result.ok("me")
