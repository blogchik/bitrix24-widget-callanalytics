"""Ingress routing and the shape of a bare `GET` on a Bitrix24-facing handler (§2, §4.11).

Two different things are asserted here, because the defect they guard against needed
both halves to be wrong at once:

1. **The ingress may not steal an SPA route.** `docs/architecture.md` §2 routes
   `/app/*`, `/install/*`, `/settings/*`, `/events/*`, `/api/*` and `/healthz` to
   `api:8000` and *everything else* to `web:3000`. `/settings` (no trailing slash) is
   the SPA's admin page — it is the target `render_handoff` writes into
   `location.replace()` (`handlers/open.py` `_SETTINGS_PATH`) and the destination of
   the only in-app link (`web/src/components/SyncBanner.tsx`) — while `/settings/`
   (trailing slash) is the Bitrix24 POST handler. Caddy's `path` matcher is exact, so
   listing the bare token `/settings` next to `/settings/*` diverts the SPA page to
   FastAPI. The trailing slash is the only thing separating the two, hence a test.

2. **No path may end in a raw FastAPI error** (§4.11: every moderator path ends in a
   rendered, translated state; a raw JSON body or a blank frame is a rejection). A
   human who pastes a handler URL — the vendor-cabinet URLs all end in a slash — sends
   a `GET`, and before this test the answer was `{"detail":"Method Not Allowed"}`.

The Caddy assertions read `docker/Caddyfile.snippet`, which lives outside the api image
(the Dockerfile copies `api/` and `web/`'s i18n bundle only), so they skip when the file
is not reachable. To run them inside the container, mount the directory where
`API_ROOT.parent` looks for it::

    docker compose run --rm -T -v "$PWD/docker:/docker:ro" api python -m pytest tests/test_routing.py -q
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final

import httpx
import pytest

from app.i18n import resolve_locale, t
from app.main import create_app
from tests.conftest import API_ROOT

#: The vendor-cabinet handler URLs (§4.3, §4.4, §4.5, §4.9). Every one of them is a
#: `POST` endpoint that a person will sooner or later open in a browser.
HANDLER_PATHS: Final[tuple[str, ...]] = ("/install/", "/app/", "/settings/", "/events/")

#: A realistic Bitrix24 handler query: the parameters are repeated on the URL (§4.4
#: step 8), which is what lets even this page render framed and translated.
QUERY: Final[str] = "DOMAIN=x.bitrix24.kz&PROTOCOL=1&LANG=en&APP_SID=abc"

_CADDYFILE: Final[Path] = API_ROOT.parent / "docker" / "Caddyfile.snippet"


@pytest.fixture()
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The real app over ASGI. No database: none of these paths touches one."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


# --- 1. the ingress ------------------------------------------------------------------


def _api_matcher_tokens() -> tuple[str, ...]:
    """The tokens of the `@api path …` line of the Caddy snippet."""
    if not _CADDYFILE.is_file():
        pytest.skip(f"{_CADDYFILE} is not in this image; see the module docstring")
    for line in _CADDYFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("@api ") and " path " in f" {stripped} ":
            return tuple(stripped.split()[2:])
    pytest.fail("no `@api path …` matcher found in docker/Caddyfile.snippet")


def _upstream(path: str) -> str:
    """Which service Caddy hands `path` to, per the snippet's `@api` matcher.

    Caddy's `path` matcher is an exact, case-insensitive comparison unless the token
    carries a `*` wildcard — `fnmatchcase` on the lowered strings has the same effect
    for tokens as simple as ours (no `?`, no character class).
    """
    lowered = path.lower()
    matched = any(fnmatchcase(lowered, token.lower()) for token in _api_matcher_tokens())
    return "api" if matched else "web"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The collision this test exists for: the SPA page and the POST handler differ
        # by one character and must land on different upstreams (§2).
        ("/settings", "web"),
        ("/settings/", "api"),
        # The other SPA routes, none of which may be diverted either.
        ("/", "web"),
        ("/dashboard", "web"),
        ("/crm", "web"),
        ("/state/denied", "web"),
        # The Bitrix24-facing handlers, the bearer API and the probe.
        ("/install/", "api"),
        ("/app/", "api"),
        ("/events/", "api"),
        ("/api/v1/calls", "api"),
        ("/healthz", "api"),
    ],
)
def test_caddy_routes_each_path_to_the_service_that_serves_it(path: str, expected: str) -> None:
    assert _upstream(path) == expected, f"{path} must be served by {expected}"


def test_caddy_snippet_documents_the_trailing_slash() -> None:
    """The next reader must be told why `/settings/*` may not lose its slash."""
    if not _CADDYFILE.is_file():
        pytest.skip(f"{_CADDYFILE} is not in this image; see the module docstring")
    text = _CADDYFILE.read_text(encoding="utf-8")
    assert re.search(r"trailing slash", text, re.IGNORECASE), (
        "the routing comment must explain that the trailing slash separates the "
        "Bitrix24 POST handler from the SPA page"
    )


# --- 2. a bare GET on a handler path -------------------------------------------------


@pytest.mark.parametrize("path", HANDLER_PATHS)
async def test_get_on_a_handler_path_renders_a_state_page(
    client: httpx.AsyncClient, path: str
) -> None:
    """§4.11: a rendered, translated page — never `{"detail":"Method Not Allowed"}`."""
    response = await client.get(f"{path}?{QUERY}")

    assert response.headers["content-type"].startswith("text/html"), response.text
    assert "detail" not in response.text
    # The state pages are identified by `data-state` in the markup, as everywhere else.
    assert 'data-state="bad_request"' in response.text
    # Translated from the shared catalogue (§8) using the LANG on the URL, not hard-coded.
    assert t(resolve_locale("en"), "state.bad_request.title") in response.text
    assert t(resolve_locale("en"), "common.openFromBitrix24") in response.text
    # §4.10: framed by the portal that sent the parameters, and never cached.
    assert response.headers["content-security-policy"] == (
        "frame-ancestors 'self' https://x.bitrix24.kz"
    )
    assert response.headers["cache-control"] == "no-store"
    # Honest HTTP under the rendered page: the endpoint really does only take POST.
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


@pytest.mark.parametrize("path", HANDLER_PATHS)
async def test_get_on_a_handler_path_is_translated(client: httpx.AsyncClient, path: str) -> None:
    """`LANG=ru` (the primary market) renders Russian copy, not English."""
    response = await client.get(f"{path}?DOMAIN=x.bitrix24.kz&PROTOCOL=1&LANG=ru")

    assert t(resolve_locale("ru"), "state.bad_request.body") in response.text
    assert t(resolve_locale("en"), "state.bad_request.body") not in response.text


@pytest.mark.parametrize("path", ["/install", "/app", "/events"])
async def test_get_without_the_trailing_slash_still_renders(
    client: httpx.AsyncClient, path: str
) -> None:
    """The slash-less spellings redirect onto the handler and end in the same page.

    `/settings` is deliberately absent: it belongs to the SPA, and after the routing
    fix above the ingress never sends it here at all.
    """
    response = await client.get(f"{path}?{QUERY}", follow_redirects=True)

    assert response.headers["content-type"].startswith("text/html"), response.text
    assert 'data-state="bad_request"' in response.text


async def test_the_moderation_checklist_step_8_url_is_never_raw_json(
    client: httpx.AsyncClient,
) -> None:
    """docs/moderation-checklist.md step 8 opens `/settings/` as an administrator.

    Whatever a moderator's browser sends there — including the plain `GET` that follows
    from pasting the URL — the frame shows a page, which is the whole of §4.11.
    """
    response = await client.get(f"/settings/?{QUERY}", follow_redirects=True)

    assert not response.text.lstrip().startswith("{")
    assert "<title>" in response.text
