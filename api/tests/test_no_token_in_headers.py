"""§4.6 / ADR 0006 — the session JWT leaves this process in exactly ONE place.

That place is the body of `handoff.html`, as a fragment assignment
(`location.replace(target + '#s=' + jwt)`). A URL fragment is never sent to a server,
so it cannot land in Caddy's access log, in an upstream proxy's log, in the browser's
`Referer`, or in `rest_log`. Everything else about the token is a leak:

* **a response header** — `Location:` on a 3xx is the classic one (and the reason §4.4
  step 8 renders a page instead of redirecting), but ANY header value is logged
  somewhere by someone, so this test walks every header of the response rather than
  spot-checking `Location`;
* **a log line** — captured through the production formatter and the production
  `RedactingFilter`, so a leak here is a leak in the deployed process (same technique
  and reasoning as `test_secret_logging`);
* **a `rest_log` row** — support staff read those for seven days (§6).

Two more assertions ride along because they are properties of the same response and
each of them, when broken, breaks the app in a way that looks like something else:

* `Cache-Control: no-store` and **no** `X-Frame-Options` (§4.10). A cached handoff page
  would hand one user's JWT to the next; `X-Frame-Options` has no origin list and would
  blank the Bitrix24 iframe outright.
* **`APP_SID` survives into the handoff target** (§4.4 step 8, and the review finding
  "APP_SID/DOMAIN/PROTOCOL/LANG dropped by the 303 redirect"). Without it `BX24.init`
  never fires, so `fitWindow`, `openPath` and `getAuth` are inert on every placement —
  a silent failure that no server-side test other than this one would catch.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from typing import Final

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.logging import setup_logging
from app.main import create_app
from app.security.session_token import SessionClaims, verify_session
from tests.fixtures.bitrix import (
    FakeBitrix,
    clear_rest_logs,
    delete_portal,
    fetch_rest_logs,
    install_form,
    install_query,
    patch_httpx,
    seed_portal,
)

#: `install_query()` puts this on the handler URL, and `install_form()` in the body —
#: exactly as Bitrix24 does. The handoff target must still carry it (§4.4 step 8).
APP_SID: Final[str] = "b8b3a9e1c7d24f0a"

#: A compact JWS: three base64url segments, the first of which is a JSON header that
#: always starts `{"` and therefore always base64s to `eyJ`. Matched loosely on purpose —
#: this test must find the token even if the template wraps it in quotes, concatenates
#: it, or writes `#s=` with different spacing.
_JWT_RE: Final[re.Pattern[str]] = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)


@pytest.fixture()
def captured_logs() -> Iterator[io.StringIO]:
    """Capture the root logger through the PRODUCTION formatter and filters.

    Not `caplog`: pytest reads `record.getMessage()` before the handler-level filter
    runs, which would both report leaks the deployed process does not have and hide the
    opposite mistake. (Same fixture as `test_secret_logging`, duplicated rather than
    imported so neither module can silently change the other's meaning.)
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
    root.setLevel(logging.DEBUG)  # the chattiest possible run
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The real app over ASGI; an explicit transport keeps it out of `patch_httpx`."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


def extract_jwt(response: httpx.Response) -> str:
    """The minted session token, read out of the page the way the browser would."""
    assert response.status_code == 200, (
        f"POST /app/ answered {response.status_code}, so there is no handoff page to "
        "inspect. Is `open_router` mounted in create_app() (§2), and does the routine "
        "admin open of §4.4 succeed against the fake?"
    )
    assert "location" not in {key.lower() for key in response.headers}, (
        "§4.4 step 8 renders handoff.html; a 3xx would put the target - and with it the "
        "APP_SID the SDK needs - into a header that Caddy is configured to delete."
    )
    match = _JWT_RE.search(response.text)
    assert match is not None, (
        "no JWT in the handoff body: the SPA reads the session token from the URL "
        "fragment this page writes (§4.6), so a page without one is a dead frame."
    )
    return match.group(0)


async def test_the_jwt_appears_in_the_body_and_nowhere_else(
    client: httpx.AsyncClient, captured_logs: io.StringIO
) -> None:
    """One routine open, then a search for the token everywhere it must not be."""
    seeded = await seed_portal(status="active")
    await clear_rest_logs()
    try:
        fake = FakeBitrix()  # admin opener: user.admin defaults to True, no probe (§4.4/4)
        with patch_httpx(fake):
            response = await client.post(
                f"/app/?{install_query()}",
                data=install_form(member_id=seeded.member_id, placement="DEFAULT"),
            )

        token = extract_jwt(response)
        claims: SessionClaims = verify_session(token)
        assert claims.pid == seeded.portal_id, "the page must carry THIS portal's session"
        assert claims.mid == seeded.member_id

        # 1. No header. Every value, including repeated headers - not just Location.
        for name, value in response.headers.multi_items():
            assert token not in value, f"the JWT reached the {name!r} response header (§4.6)"
        # Belt and braces: a header we never thought to name, serialised whole.
        assert token not in json.dumps(dict(response.headers.multi_items()))

        # 2. No log line. Read through the production formatter + redaction filter.
        assert token not in captured_logs.getvalue(), (
            "the JWT reached the log output; §4.6 says it is 'never logged' and stdout "
            "is shipped off the host."
        )

        # 3. No rest_log row - support reads those for REST_LOG_RETENTION_DAYS (§6).
        rows = await fetch_rest_logs()
        assert token not in json.dumps(rows, default=str), "the JWT reached a rest_log row"

        # 4. But it IS in the body, as the fragment assignment the SPA reads once.
        assert "#s=" in response.text, (
            "handoff.html must write the token into the URL fragment (`#s=`): a query "
            "parameter would be logged by every intermediary between here and the tab."
        )
        assert token in response.text
    finally:
        await delete_portal(seeded.member_id)
        await clear_rest_logs()


async def test_the_handoff_page_is_uncacheable_and_never_frame_denied(
    client: httpx.AsyncClient,
) -> None:
    """§4.10: `Cache-Control: no-store` on every handoff/API response, and no
    `X-Frame-Options` anywhere.

    A cached handoff is a token handed to the next user of the same browser or proxy;
    an `X-Frame-Options` header (which has no origin list) blanks the app inside
    Bitrix24 with no error message at all.
    """
    seeded = await seed_portal(status="active")
    try:
        fake = FakeBitrix()
        with patch_httpx(fake):
            response = await client.post(
                f"/app/?{install_query()}",
                data=install_form(member_id=seeded.member_id, placement="DEFAULT"),
            )

        assert response.status_code == 200
        assert "no-store" in response.headers.get("cache-control", "").lower()
        assert "x-frame-options" not in {key.lower() for key in response.headers}
        assert "frame-ancestors" in response.headers.get("content-security-policy", "")
    finally:
        await delete_portal(seeded.member_id)


async def test_app_sid_survives_into_the_handoff_target(client: httpx.AsyncClient) -> None:
    """§4.4 step 8: the target is the SPA path + **Bitrix24's original query verbatim**.

    `APP_SID` is the frame's identifier for the parent window. Drop it and `BX24.init`
    never resolves, so `fitWindow()` never runs (the app renders in a 300 px stub),
    `openPath()` does nothing and `getAuth()` returns nothing - which then breaks the
    §4.6 session exchange an hour later. Every one of those looks like a frontend bug.
    """
    seeded = await seed_portal(status="active")
    try:
        fake = FakeBitrix()
        with patch_httpx(fake):
            response = await client.post(
                f"/app/?{install_query()}",
                data=install_form(member_id=seeded.member_id, placement="DEFAULT"),
            )

        body = response.text
        assert f"APP_SID={APP_SID}" in body, (
            "the handoff target dropped APP_SID; without it the BX24 JS SDK cannot talk "
            "to the parent frame on ANY placement (§4.4 step 8)."
        )
        # The rest of the original query rides along for the same reason: middleware.ts
        # builds the CSP `frame-ancestors` from DOMAIN/PROTOCOL, and LANG picks the
        # locale before any API call has happened.
        for expected in ("DOMAIN=", "PROTOCOL=", "LANG="):
            assert expected in body, f"the handoff target dropped {expected!r} (§4.4 step 8)"
    finally:
        await delete_portal(seeded.member_id)
