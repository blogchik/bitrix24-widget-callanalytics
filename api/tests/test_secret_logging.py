"""§6 / decision 20 - the test that keeps redaction honest.

Redaction is the kind of guarantee that is true on the day it is written and quietly
false three commits later: someone adds `logger.info("exchange", extra={"payload": body})`
or widens a `rest_log.request`, and nothing fails. So this module does not test
`redact()` (that is a pure function with its own obvious tests) - it runs the **real**
install and the **real** refresh against mocks and then greps *everything the process
produced* for every sentinel credential the flow touched:

* `settings.b24_client_secret` - ours, and the worst one to leak: it is the same value
  for every tenant, so one log line compromises every portal at once.
* the access token and the refresh token - the portal's sync credential (§3 encrypts
  them at rest precisely because they are worth stealing).
* the `APPLICATION_TOKEN` - the shared secret that authenticates every inbound
  lifecycle event (§4.9 rule 2); with it, a forged `ONAPPUNINSTALL` wipes a customer.

"Everywhere" means: every captured log line, and every column of every `rest_log` row -
as a substring, at any nesting depth inside the JSONB, and inside URL query strings. The
whole row is serialised to text and searched, so a secret hidden in a key we did not
think to check still fails the test.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BitrixClient
from app.bitrix.oauth import with_portal_token
from app.config import settings
from app.logging import setup_logging
from app.main import create_app
from app.security.redact import REDACTED
from tests.fixtures.bitrix import (
    APP_KEY,
    CLIENT_ENDPOINT,
    FRESH_ACCESS,
    FRESH_REFRESH,
    SEED_ACCESS,
    SEED_REFRESH,
    USER_AUTH,
    USER_REFRESH,
    Err,
    FakeBitrix,
    clear_rest_logs,
    delete_portal,
    fetch_rest_logs,
    install_form,
    install_query,
    new_member_id,
    patch_httpx,
    seed_portal,
    token_response,
)

#: Every value that must never be observable. `member_id` is deliberately NOT here: it
#: is public by decision 1 and is what makes a `rest_log` row attributable at all (§3).
#:
#: NOTE on `USER_REFRESH`: the §6 key regex is /(token|secret|auth|password)/i, which
#: catches `AUTH_ID`, `APPLICATION_TOKEN` and every `*_token` field - but NOT the form
#: field literally named `REFRESH_ID`. It is nonetheless a refresh token: §4.3 step 2
#: exchanges it, and a copy sitting in a support-visible `rest_log` row for 7 days is
#: the same liability as any other. Whoever writes the inbound log must redact it
#: explicitly (or `security/redact.py` must widen its key regex to cover `refresh_id`).
SECRETS: tuple[tuple[str, str], ...] = (
    ("B24_CLIENT_SECRET", settings.b24_client_secret),
    ("the stored access token", SEED_ACCESS),
    ("the stored refresh token", SEED_REFRESH),
    ("the refreshed access token", FRESH_ACCESS),
    ("the refreshed refresh token", FRESH_REFRESH),
    ("APPLICATION_TOKEN", APP_KEY),
    ("the POSTed AUTH_ID", USER_AUTH),
    ("the POSTed REFRESH_ID", USER_REFRESH),
)


@pytest.fixture()
def captured_logs() -> Iterator[io.StringIO]:
    """Capture the root logger through the PRODUCTION formatter and filter.

    Not `caplog`: pytest's fixture reads `record.getMessage()` before the handler-level
    `RedactingFilter` has run, which would report leaks the deployed process never has -
    and, worse, could hide the opposite mistake. Everything here goes through the exact
    objects `setup_logging()` installed, so a bug in either is a failure.
    """
    setup_logging()
    root = logging.getLogger()
    installed = root.handlers[0]

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(installed.formatter)
    for log_filter in installed.filters:
        handler.addFilter(log_filter)
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)  # capture the chattiest possible run
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def assert_clean(*, logs: str, rows: list[dict[str, object]], where: str) -> None:
    """Fail with the offending secret named, but never printed, and never re-logged."""
    serialised_rows = json.dumps(rows, default=str)
    for name, secret in SECRETS:
        assert secret, f"{name} is blank, which would make this whole test vacuous"
        assert secret not in logs, f"{name} reached the log output during {where}"
        assert secret not in serialised_rows, f"{name} reached a rest_log row during {where}"


async def run_install(member_id: str) -> FakeBitrix:
    """Drive the real `/install/` handler once, end to end, against the fake."""
    fake = FakeBitrix()
    fake.on_oauth(token_response(member_id=member_id))
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://b24.texnobus.test") as http:
        with patch_httpx(fake):
            await http.post(
                f"/install/?{install_query()}",
                data=install_form(member_id=member_id),
            )
    return fake


# --- install ------------------------------------------------------------------------


async def test_an_install_leaks_nothing_into_logs_or_rest_log(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """§4.3 end to end: the inbound POST carries AUTH_ID/REFRESH_ID/APPLICATION_TOKEN,
    the exchange carries `client_secret`, and the response carries a brand new pair.
    Four §6 log rows are written from that (in: install; out: oauth, rest, rest) - and
    none of them may contain any of it."""
    member_id = new_member_id()
    await clear_rest_logs()
    try:
        await run_install(member_id)
        rows = await fetch_rest_logs()
        assert rows, "§6 requires one rest_log row per exchange; none were written"
        assert_clean(logs=captured_logs.getvalue(), rows=rows, where="install")
    finally:
        await delete_portal(member_id)
        await clear_rest_logs()


async def test_the_inbound_install_post_is_logged_but_redacted(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """§6: "handlers write one row per inbound Bitrix24 POST (`direction=in`)".

    The row must exist - it is the moderation trail - AND its `request` must show the
    redaction placeholder where the tokens were, which is the difference between
    "redacted" and "quietly dropped the whole body".
    """
    member_id = new_member_id()
    await clear_rest_logs()
    try:
        await run_install(member_id)
        rows = await fetch_rest_logs()
        inbound = [row for row in rows if row["direction"] == "in"]
        assert inbound, "the inbound install POST was not logged at all"
        assert {row["kind"] for row in inbound} == {"install"}
        body = json.dumps(inbound[0]["request"], default=str)
        assert REDACTED in body, "the token fields must be present-and-redacted, not absent"
        assert member_id in body, "member_id is public and is what makes the row useful"
    finally:
        await delete_portal(member_id)
        await clear_rest_logs()


async def test_the_oauth_exchange_row_records_the_url_without_its_query_string(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """§6: "method, URL **without query string**".

    The refresh grant is a GET whose query carries `client_secret` and `refresh_token`
    (research note (b)), so a logged full URL is the single most likely way this
    application ever leaks its client secret.
    """
    member_id = new_member_id()
    await clear_rest_logs()
    try:
        await run_install(member_id)
        rows = await fetch_rest_logs()
        oauth_rows = [row for row in rows if row["kind"] == "oauth"]
        assert len(oauth_rows) == 1, "§4.3 step 2 performs exactly one exchange"
        url = str(oauth_rows[0]["url"])
        assert "client_secret" not in url or REDACTED in url
        assert settings.b24_client_secret not in url
        assert "?" not in url or REDACTED in url
        # §3: portal_id is NULL for the install-time exchange (the row does not exist
        # yet) and `member_id` is what keeps the exchange attributable.
        assert oauth_rows[0]["member_id"] == member_id
    finally:
        await delete_portal(member_id)
        await clear_rest_logs()


# --- refresh (§5.8) -----------------------------------------------------------------


async def test_a_worker_refresh_leaks_nothing(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """The other exchange: the worker's `expired_token` refresh, which handles BOTH the
    old pair (read out of the database and decrypted) and the new one."""
    seeded = await seed_portal()
    await clear_rest_logs()
    try:
        fake = FakeBitrix()
        fake.on_oauth(token_response(member_id=seeded.member_id))
        fake.on("user.admin", Err("expired_token"), True)

        async def work(access_token: str) -> object:
            async with BitrixClient(
                endpoint=CLIENT_ENDPOINT, access_token=access_token,
                portal_id=seeded.portal_id, member_id=seeded.member_id,
            ) as client:
                return await client.call("user.admin")

        with patch_httpx(fake):
            await with_portal_token(seeded.portal_id, work)

        rows = await fetch_rest_logs()
        assert rows, "§6 requires a row per REST/OAuth exchange"
        assert_clean(logs=captured_logs.getvalue(), rows=rows, where="refresh")
    finally:
        await delete_portal(seeded.member_id)
        await clear_rest_logs()


async def test_a_failing_exchange_is_still_logged_and_still_redacted(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """§6: the row is "written on the exception path before re-raising".

    The failure path is where redaction is most often forgotten, because the natural
    reflex is to log the whole request "so we can debug it".
    """
    seeded = await seed_portal()
    await clear_rest_logs()
    try:
        fake = FakeBitrix()
        fake.on_oauth(Err("invalid_grant", "The passed refresh token is not valid"))
        fake.on("user.admin", Err("expired_token"))

        async def work(access_token: str) -> object:
            async with BitrixClient(
                endpoint=CLIENT_ENDPOINT, access_token=access_token, portal_id=seeded.portal_id,
            ) as client:
                return await client.call("user.admin")

        with patch_httpx(fake), pytest.raises(Exception):  # noqa: B017
            await with_portal_token(seeded.portal_id, work)

        rows = await fetch_rest_logs()
        assert any(row["kind"] == "oauth" for row in rows), "the failed exchange was not logged"
        assert_clean(logs=captured_logs.getvalue(), rows=rows, where="failed refresh")
    finally:
        await delete_portal(seeded.member_id)
        await clear_rest_logs()


# --- the logging pipeline itself ----------------------------------------------------


def test_httpx_loggers_are_pinned_below_info(captured_logs: io.StringIO) -> None:
    """§6: "`httpx` and `httpcore` log full request URLs at INFO, which for the OAuth
    refresh means `client_secret` and `refresh_token` in plain text."

    The pin is the first layer; the filter below is the second. Both are asserted so a
    regression in either is visible, rather than each silently covering for the other.
    """
    setup_logging()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_a_full_oauth_url_survives_the_filter_only_in_redacted_form(
    captured_logs: io.StringIO,
) -> None:
    """The second layer: if the pin above is ever lifted, the URL must still be scrubbed.

    This is exactly the line `httpx` emits, reproduced verbatim.
    """
    url = (
        "https://oauth.bitrix.info/oauth/token/?grant_type=refresh_token"
        f"&client_id={settings.b24_client_id}"
        f"&client_secret={settings.b24_client_secret}"
        f"&refresh_token={SEED_REFRESH}"
    )
    logging.getLogger("httpx").warning('HTTP Request: GET %s "HTTP/1.1 200 OK"', url)
    output = captured_logs.getvalue()
    assert "oauth/token" in output, "the line itself must still be emitted"
    assert_clean(logs=output, rows=[], where="a raw httpx request line")


def test_a_token_bearing_extra_dict_is_redacted_at_every_depth(
    captured_logs: io.StringIO,
) -> None:
    """§6: "any key matching /(token|secret|auth|password)/i **at any depth**".

    The nesting below is the real shape of an `ONAPPUSERREADY` body (§4.9), which is
    where a long-lived credential most plausibly reaches a log by accident.
    """
    logging.getLogger("app.test").info(
        "event received",
        extra={
            "payload": {
                "event": "ONAPPUSERREADY",
                "auth": {
                    "access_token": FRESH_ACCESS,
                    "refresh_token": FRESH_REFRESH,
                    "application_token": APP_KEY,
                    "nested": [{"deeper": {"access_token": SEED_ACCESS}}],
                },
                "member_id": "a223c6b3710f85df22e9377d6c4f7553",
            }
        },
    )
    output = captured_logs.getvalue()
    assert "ONAPPUSERREADY" in output, "the useful part of the line must survive"
    assert_clean(logs=output, rows=[], where="a nested extra dict")


def test_an_exception_carrying_a_secret_in_its_message_is_redacted(
    captured_logs: io.StringIO,
) -> None:
    """Tracebacks are logged with `exc_info`; §6 runs the formatted exception through
    `redact()` for exactly this case."""
    try:
        raise RuntimeError(f"refresh failed for refresh_token={SEED_REFRESH}")
    except RuntimeError:
        logging.getLogger("app.test").exception("exchange failed")
    assert_clean(logs=captured_logs.getvalue(), rows=[], where="an exception message")


async def test_a_request_whose_query_string_carries_a_secret_is_not_echoed(
    app_engine: AsyncEngine, captured_logs: io.StringIO
) -> None:
    """A scanner (or a mis-built link) puts a token in the URL; the access-log line for
    that request must contain the path and nothing else."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://b24.texnobus.test") as http:
        await http.get(f"/healthz?access_token={FRESH_ACCESS}&t={APP_KEY}")
    assert_clean(logs=captured_logs.getvalue(), rows=[], where="a secret-bearing query string")


def test_the_sentinels_would_actually_be_found_if_they_leaked(
    captured_logs: io.StringIO,
) -> None:
    """Negative control. Every assertion above is of the form "X is not in Y"; without
    this, a broken capture fixture or an empty `SECRETS` tuple would make the entire
    module pass while proving nothing."""
    for name, secret in SECRETS:
        assert len(secret) >= 12, f"{name} is too short to be a reliable sentinel"
    with pytest.raises(AssertionError):
        assert_clean(
            logs=" ".join(secret for _, secret in SECRETS), rows=[], where="the negative control"
        )
