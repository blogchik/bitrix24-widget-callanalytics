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

The Caddy assertions read *both* ingress configurations under `docker/`, because the
one that is deployed is the one that has to be right:

* `Caddyfile.tunnel` is what actually runs on the shared host — inside our compose
  project, plain HTTP on the compose network, with Cloudflare Tunnel in front of it
  terminating TLS. It also carries a few properties that only make sense behind a
  tunnel (see the tunnel section below); each one was a confirmed deployment blocker.
* `Caddyfile.snippet` is the reference block for a dedicated host where Caddy itself
  owns :80/:443 and gets a certificate.

The routing half must hold identically in both: how TLS is obtained has nothing to do
with which upstream `/settings` belongs to, and a guard that only covered the file
nobody deploys would be theatre.

Neither file lives inside the api image (the Dockerfile copies `api/` and `web/`'s i18n
bundle only), so every assertion skips when its file is not reachable, naming the file
it wanted. To run them inside the container, mount the directory where
`API_ROOT.parent` looks for them::

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

_DOCKER_DIR: Final[Path] = API_ROOT.parent / "docker"

#: Every ingress configuration the repo ships, all of which must route alike.
CADDYFILES: Final[tuple[str, ...]] = ("Caddyfile.snippet", "Caddyfile.tunnel")

#: The one that runs in production on the shared host, behind Cloudflare Tunnel.
TUNNEL_CADDYFILE: Final[str] = "Caddyfile.tunnel"


@pytest.fixture()
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The real app over ASGI. No database: none of these paths touches one."""
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


# --- 1. the ingress ------------------------------------------------------------------


def _caddyfile_text(name: str) -> str:
    """The text of `docker/<name>`, or a skip that names the file that is missing."""
    path = _DOCKER_DIR / name
    if not path.is_file():
        pytest.skip(f"docker/{name} is not in this image ({path}); see the module docstring")
    return path.read_text(encoding="utf-8")


def _api_matcher_tokens(name: str) -> tuple[str, ...]:
    """The tokens of the `@api path …` line of `docker/<name>`."""
    for line in _caddyfile_text(name).splitlines():
        stripped = line.strip()
        if stripped.startswith("@api ") and " path " in f" {stripped} ":
            return tuple(stripped.split()[2:])
    pytest.fail(f"no `@api path …` matcher found in docker/{name}")


def _upstream(path: str, name: str) -> str:
    """Which service Caddy hands `path` to, per that file's `@api` matcher.

    Caddy's `path` matcher is an exact, case-insensitive comparison unless the token
    carries a `*` wildcard — `fnmatchcase` on the lowered strings has the same effect
    for tokens as simple as ours (no `?`, no character class).
    """
    lowered = path.lower()
    matched = any(
        fnmatchcase(lowered, token.lower()) for token in _api_matcher_tokens(name)
    )
    return "api" if matched else "web"


@pytest.mark.parametrize("caddyfile", CADDYFILES)
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
def test_caddy_routes_each_path_to_the_service_that_serves_it(
    path: str, expected: str, caddyfile: str
) -> None:
    assert _upstream(path, caddyfile) == expected, (
        f"{path} must be served by {expected} in docker/{caddyfile}"
    )


@pytest.mark.parametrize("caddyfile", CADDYFILES)
def test_caddy_config_documents_the_trailing_slash(caddyfile: str) -> None:
    """The next reader must be told why `/settings/*` may not lose its slash.

    Both files carry the same matcher, so both need the same warning next to it: a
    reader who "tidies" the token in whichever file they happen to open reintroduces
    the blocker.
    """
    assert re.search(r"trailing slash", _caddyfile_text(caddyfile), re.IGNORECASE), (
        f"the routing comment in docker/{caddyfile} must explain that the trailing "
        "slash separates the Bitrix24 POST handler from the SPA page"
    )


@pytest.mark.parametrize("caddyfile", CADDYFILES)
def test_caddy_config_strips_x_frame_options(caddyfile: str) -> None:
    """§4.10: the app renders inside a Bitrix24 iframe on a per-portal origin.

    `X-Frame-Options` has no origin granularity, so any value an upstream (or, behind
    the tunnel, a Cloudflare setting) sets would blank the frame for every portal.
    Framing is controlled exclusively by the per-request `Content-Security-Policy:
    frame-ancestors` built from the validated DOMAIN, which is why the ingress deletes
    the header outright.
    """
    assert re.search(r"^\s*-X-Frame-Options\s*$", _caddyfile_text(caddyfile), re.MULTILINE), (
        f"docker/{caddyfile} must strip X-Frame-Options; the app is framed by Bitrix24"
    )


@pytest.mark.parametrize("caddyfile", CADDYFILES)
def test_caddy_access_log_keeps_credentials_off_disk(caddyfile: str) -> None:
    """§4.10: neither the `Location` header nor the query string may be persisted.

    The handoff response's `Location` carries the session JWT in its fragment and
    Bitrix24's POST-back query string carries `AUTH_ID`/`APP_SID`. The log encoder must
    delete the one and rewrite the other away in every ingress we ship — an access log
    on a shared host is exactly where a credential goes unnoticed.
    """
    text = _caddyfile_text(caddyfile)

    assert re.search(r"^\s*resp_headers>Location\s+delete\s*$", text, re.MULTILINE), (
        f"docker/{caddyfile} must delete the Location response header from the log; "
        "it carries the session JWT at handoff time"
    )
    uri_filter = re.search(r"^\s*request>uri\s+regexp\s+(.+)$", text, re.MULTILINE)
    assert uri_filter is not None, (
        f"docker/{caddyfile} must rewrite the logged request URI to drop the query string"
    )
    assert "?" in uri_filter.group(1), (
        f"the `request>uri regexp` filter in docker/{caddyfile} must strip everything "
        f"from the first `?` on; found `{uri_filter.group(0).strip()}`"
    )


# --- 1b. the tunnel ingress, the one that actually runs ------------------------------


def _site_addresses(name: str) -> tuple[str, ...]:
    """The address tokens of the first top-level site block of `docker/<name>`.

    Site addresses sit at column 0 and open a brace; anything indented is a directive
    inside a block, and a bare `{` is the global options block.
    """
    for raw in _caddyfile_text(name).splitlines():
        if raw != raw.lstrip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.endswith("{"):
            tokens = tuple(stripped[:-1].split())
            if tokens:
                return tokens
    pytest.fail(f"no site block found in docker/{name}")


def test_tunnel_caddyfile_listens_on_plain_http_with_no_hostname() -> None:
    """`:80` and no hostname, so automatic HTTPS cannot engage at all.

    Cloudflare terminates TLS at the edge and cloudflared reaches us over our own
    compose network as `http://caddy:80`; we publish no ports on this shared host.
    A site address carrying `b24.texnobus.uz` would switch Caddy's automatic HTTPS on
    and give two failure modes at once: an ACME order for a name whose A record is
    Cloudflare's, with no way for the HTTP-01 challenge to reach us (a renewal loop
    that retries forever), and the automatic :80 -> :443 redirect, which the tunnel
    would follow straight back into the same block. An address with no host name is
    the documented way to keep automatic HTTPS off; nothing else in the file needs to.
    """
    addresses = _site_addresses(TUNNEL_CADDYFILE)

    assert addresses == (":80",), (
        f"docker/{TUNNEL_CADDYFILE} must have the single site address `:80` (no "
        f"hostname, no scheme), else automatic HTTPS engages; found "
        f"`{' '.join(addresses)}`"
    )
    # And no certificate directive anywhere either: behind the tunnel there is nothing
    # for Caddy to serve TLS to, and `tls internal` would only add a cert cloudflared
    # is not configured to trust.
    text = _caddyfile_text(TUNNEL_CADDYFILE)
    assert not re.search(r"^\s*tls\b", text, re.MULTILINE), (
        f"docker/{TUNNEL_CADDYFILE} must not carry a `tls` directive; Cloudflare "
        "terminates TLS and this site speaks plain HTTP on the compose network"
    )


def test_tunnel_caddyfile_does_not_overwrite_the_forwarded_scheme() -> None:
    """cloudflared already sends `X-Forwarded-Proto: https`; we must not clobber it.

    Our Caddy is reached over plain HTTP, so `{scheme}` evaluates to `http` here. A
    `header_up X-Forwarded-Proto {scheme}` line therefore tells the app the request
    arrived insecure, and the app answers the Bitrix24 iframe with a redirect to
    `http://b24.texnobus.uz` — mixed content inside an HTTPS frame, which the portal
    blocks and §4.11 counts as a rejection. Omitting the line passes Cloudflare's own
    value through untouched. (The dedicated-host snippet keeps the line: there Caddy
    itself terminates TLS, so `{scheme}` really is the client's scheme.)
    """
    text = _caddyfile_text(TUNNEL_CADDYFILE)

    for match in re.finditer(
        r"^\s*header_up\s+X-Forwarded-Proto\b(?P<value>.*)$", text, re.MULTILINE | re.IGNORECASE
    ):
        value = match.group("value").split("#", 1)[0].strip().strip('"').lower()
        assert "{" not in value and value != "http", (
            f"docker/{TUNNEL_CADDYFILE} must not set X-Forwarded-Proto to a placeholder "
            f"or to `http` (found `{match.group(0).strip()}`): behind the tunnel the "
            "scheme this Caddy sees is http, and the app would downgrade the iframe"
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
