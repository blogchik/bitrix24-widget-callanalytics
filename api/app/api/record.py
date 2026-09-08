"""`GET /api/v1/calls/{id}/record?t=<token>` - the only path audio ever takes (§4.6, §9).

This endpoint is the reason §4.6 mints a second kind of token at all. An `<audio src>`
cannot send an `Authorization` header, and a `fetch` + Blob workaround would destroy
Range seeking on a forty-minute recording, so the grant travels in the query string -
and everything about it is shaped by that fact: five minutes, one portal, one viewer,
one call id, and `?t=` is the *whole* authorisation. There is no session here, no
cookie, and no header to fall back on.

What the endpoint does, in order:

1. verify `t` (signature, `typ`, expiry) - `verify_play_token` refuses a session token
   presented in its place, which is the separation §4.6's two codecs exist to keep;
2. re-apply the **portal-status check** (§4.6 revocation: an uninstalled portal stops
   answering now, not when the grant expires);
3. re-apply `scope_filter` through `services/calls_repo` - the same predicate the mint
   applied, rebuilt from the grant's own `acc` / `sub`, so a token minted while the
   viewer had access is still checked against the row it names;
4. honour `settings.recording_mode`.

`RECORDING_MODE` is a three-value decision of which only two values may exist in code
(§9 step 5):

* **`off`** - the v1 default until the spike is answered. 409 with a machine code; the
  SPA renders "open the call in Bitrix24" and never points an `<audio>` at us.
* **`proxy`** - stream from the stored URL with httpx: `Range` forwarded up,
  `Content-Range` / `Accept-Ranges` / `Content-Type` / `Content-Length` forwarded down,
  **nothing written to disk** (a container that spools a 40-minute WAV to /tmp is a
  disclosure surface and a disk-space incident). For a non-admin viewer the fetch uses
  **the viewer's own token**, so Bitrix24's separate "listen to recordings" permission is
  enforced by Bitrix24 rather than bypassed by our portal token (§4.7, §9 step 3).
* **`redirect`** - forbidden. §9 step 5: it is permitted only if the spike proves the URL
  carries no credential, and the research note says the family to expect is
  `download.json?auth=<access token>`. Handing that to a browser would hand every viewer
  the portal's access token. The guard is at import time, below, because a mode that
  leaks a credential must stop the container, not the request.

The stored `call_record_url` is never in a response body, never in a log line and never
in a header (§3 decision 21); it is read into a local, used to open one upstream
request, and dropped.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from app.bitrix.errors import BitrixError
from app.bitrix.oauth import CredentialUnavailable, MemberIdMismatch, with_portal_token
from app.config import settings
from app.db.models import Call, Portal
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger
from app.security.crypto import DecryptionError
from app.security.principal import Principal, PrincipalErrorRoute
from app.security.session_token import PlayClaims, TokenError, verify_play_token
from app.services.calls_repo import base_select

__all__ = ["forget_viewer_tokens", "remember_viewer_token", "router", "viewer_token"]

router = APIRouter(route_class=PrincipalErrorRoute)

_log = get_logger(__name__)

#: The two modes that may exist. `redirect` is rejected at import time (§9 step 5).
_MODE_OFF: Final[str] = "off"
_MODE_PROXY: Final[str] = "proxy"
_SUPPORTED_MODES: Final[frozenset[str]] = frozenset({_MODE_OFF, _MODE_PROXY})

if settings.recording_mode not in _SUPPORTED_MODES:
    # `config.py` types the field as a Literal, so this is normally unreachable - it is
    # the message that would otherwise be missing. §9 step 5 forbids `redirect` outright
    # until the spike proves `CALL_RECORD_URL` carries no credential; a signed URL of the
    # `download.json?auth=<token>` family handed to the browser is the portal's access
    # token handed to every viewer.
    raise RuntimeError(
        f"RECORDING_MODE={settings.recording_mode!r} is not supported. "
        "Only 'off' and 'proxy' may run: 'redirect' stays forbidden until the §9 spike "
        "proves the recording URL carries no credential (docs/spike-recording-playback.md)."
    )

#: §4.7's `all`, i.e. an administrator. Only they are streamed with the portal token
#: (§11 assumption 4: a portal administrator always has full telephony access).
_ACCESS_ALL: Final[str] = "all"

#: §3 `portals_status_chk`: the only status that may answer a request.
_ACTIVE: Final[str] = "active"

#: Upstream statuses that mean "this cached link is no longer good" (§5.7 rule 3): the
#: recording was deleted, the URL expired, or this viewer may not listen to it. All three
#: are answered with one machine code that makes the SPA call the refresh endpoint.
_STALE_UPSTREAM: Final[frozenset[int]] = frozenset({401, 403, 404, 410})

#: Response headers forwarded verbatim. Nothing else crosses: an upstream `Set-Cookie`
#: or `Location` on our own origin is not something a media proxy should relay.
_PASSTHROUGH_HEADERS: Final[tuple[str, ...]] = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
)

#: Request headers forwarded up. `Range` and `If-Range` are what seeking needs; a
#: browser's `Accept-Encoding`/cookies are deliberately not passed on.
_FORWARDED_REQUEST_HEADERS: Final[tuple[str, ...]] = ("range", "if-range")

#: Streaming timeouts. The read timeout is per chunk, not per response, so a long file is
#: fine while a stalled upstream is not. Well under §5.1's 120 s ceiling for REST calls.
#:
#: 60 s was measured to be far too patient. On the first real portal the provider
#: (sipuni.com, reached through `CALL_RECORD_URL`) answers a small byte range in 0.2 s but
#: returns headers and then NOTHING for a whole-file or open-ended request. Every such
#: playback held a worker for a full minute and then raised, so the browser waited a minute
#: to learn nothing. A stalled upstream is not a slow upstream: if the first byte has not
#: arrived in 15 s it is not coming, and the SPA is better served by a prompt machine code
#: it can turn into "open it in Bitrix24".
_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=15.0, read=15.0, write=15.0, pool=15.0)

#: 64 KiB chunks: large enough that the event loop is not woken per packet, small enough
#: that a dozen concurrent listeners cost kilobytes rather than the whole file (§9 step 3
#: measures container memory while streaming, and this is the number it measures).
_CHUNK_BYTES: Final[int] = 64 * 1024

#: Bitrix24-style credential parameter appended to the stored URL. §5.5 strips `auth`
#: (and friends) before storage precisely so that the credential is re-attached here,
#: per request, per viewer - never persisted next to the row.
_AUTH_PARAM: Final[str] = "auth"


# --- the viewer-token hand-off -------------------------------------------------------
#
# §9 step 3 requires a non-admin's recording to be fetched with that viewer's OWN token,
# and §4.6 forbids putting such a token in a URL. `POST /calls/{id}/play-url` therefore
# proves the token under the live session and leaves it here, keyed by exactly the triple
# the playback grant names, for exactly as long as the grant lives.
#
# WHY in memory: the alternative is persisting a live user credential, which would need
# encryption, a retention rule and a purge path for a value that is worthless in five
# minutes. It never reaches disk, never reaches a log, and dies with the process - and
# the cost of losing it is one 409 that the SPA answers by re-minting the grant.
#
# WHY process-local is acceptable: v1 runs exactly one api container (§10), the same
# assumption `bitrix/oauth.py` documents for its rate limiter. A second replica would
# scatter these entries and produce spurious `viewer_token_required` answers - correct
# but noisy - so scaling out means moving this into a shared store first.

_ViewerKey = tuple[int, int, int]
_viewer_tokens: dict[_ViewerKey, tuple[str, float]] = {}

#: Bounds the map if a portal mints many grants in one five-minute window.
_MAX_VIEWER_TOKENS: Final[int] = 2048


def _sweep_viewer_tokens(now: float) -> None:
    for key in [key for key, (_, expires) in _viewer_tokens.items() if expires <= now]:
        del _viewer_tokens[key]


def remember_viewer_token(*, pid: int, sub: int, cid: int, token: str, ttl_seconds: int) -> None:
    """Hold one proven viewer token for the life of one playback grant (§9 step 3)."""
    now = time.monotonic()
    _sweep_viewer_tokens(now)
    if len(_viewer_tokens) >= _MAX_VIEWER_TOKENS:
        # Drop the entry closest to expiry rather than refusing the newest: the oldest
        # grant is the one whose listener has most likely finished.
        oldest = min(_viewer_tokens, key=lambda key: _viewer_tokens[key][1])
        del _viewer_tokens[oldest]
    _viewer_tokens[(int(pid), int(sub), int(cid))] = (token, now + float(ttl_seconds))


def viewer_token(*, pid: int, sub: int, cid: int) -> str | None:
    """The held token, or None once it has expired or was never posted.

    Read, not consumed: an `<audio>` element issues several Range requests against the
    same URL, and popping the token on the first one would break seeking - which is the
    behaviour the whole signed-URL design exists to preserve.
    """
    now = time.monotonic()
    entry = _viewer_tokens.get((int(pid), int(sub), int(cid)))
    if entry is None:
        return None
    token, expires = entry
    if expires <= now:
        del _viewer_tokens[(int(pid), int(sub), int(cid))]
        return None
    return token


def forget_viewer_tokens() -> None:
    """Clear the held tokens. For tests and for an operator-driven reset only."""
    _viewer_tokens.clear()


# --- helpers -------------------------------------------------------------------------


def _error(code: str, status: int) -> JSONResponse:
    """One machine code, no message (§8: the SPA translates)."""
    return JSONResponse({"code": code}, status_code=status)


def _principal_of(claims: PlayClaims, portal: Portal) -> Principal:
    """Rebuild just enough principal to re-apply §4.7's scope predicate.

    `scope_filter` reads three fields - `portal_id`, `user_id`, `access` - and this is
    the honest way to hand it those without inventing a second scope implementation for
    the one endpoint that has no session. The claims are signed by us and were minted
    only after `play-url` matched the row under a live session, so `acc` here is the same
    level that authorised the mint, at most five minutes old.

    The display fields are placeholders on purpose: nothing on this path renders text.
    """
    return Principal(
        portal_id=portal.id,
        member_id=portal.member_id,
        user_id=claims.sub,
        is_admin=claims.acc == _ACCESS_ALL,
        access=claims.acc,
        timezone=portal.timezone,
        lang=portal.lang or "",
        placement="",
        entity=None,
        issued_at=0,
    )


def _same_host(url: str, *candidates: str | None) -> bool:
    """True when `url`'s host is one of the portal's own hosts.

    The gate on attaching a credential. `call_record_url` arrives inside a Bitrix24 REST
    response, and a response field is still portal-controlled data (§4.1): appending the
    portal's access token to a URL that points somewhere else would mail the credential
    to whoever that somewhere is. A foreign host is still fetched - the recording may
    legitimately live on a CDN - but it is fetched anonymously.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for candidate in candidates:
        if not candidate:
            continue
        try:
            # A bare hostname (portals.domain, possibly with a port) has no scheme, so it
            # is parsed with one bolted on; urlsplit would otherwise read it as a path.
            parsed = urlsplit(candidate if "//" in candidate else f"//{candidate}")
        except ValueError:
            continue
        if (parsed.hostname or "").lower() == host:
            return True
    return False


def _with_auth(url: str, token: str) -> str:
    """Re-attach the credential §5.5 stripped before storage.

    The parser removes `auth`/`token`/`sig` from `CALL_RECORD_URL` so the row never holds
    a credential (§3 decision 21). To fetch the file we put ours back, for this one
    request, in this one string, which is never logged and never returned.
    """
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != _AUTH_PARAM]
    query.append((_AUTH_PARAM, token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def _access_token_for(claims: PlayClaims, portal: Portal) -> str | None:
    """The token this stream is fetched with, or None when there is none to use.

    An administrator (`acc='all'`) is streamed with the **portal** token: it is already
    admin-proven, and §11 assumption 4 says an administrator has full telephony access
    anyway, so nothing is bypassed. `with_portal_token` is used rather than a raw decrypt
    so a token that is about to expire is refreshed once under the §5.8 single-flight
    lock instead of producing a mysterious 401 from the file host.

    Anyone else is streamed with **their own** token (§4.7, §9): "Call Recording: Listen"
    is a separate Bitrix24 permission from "Call statistics - view", REST exposes
    neither, and the only honest enforcement is to let Bitrix24 answer the request as the
    person who made it. No token held -> the caller answers `viewer_token_required` and
    the SPA re-mints the grant with a fresh `BX24.getAuth()` token.
    """
    if claims.acc != _ACCESS_ALL:
        return viewer_token(pid=claims.pid, sub=claims.sub, cid=claims.cid)

    async def _use(token: str) -> str:
        # `with_portal_token` exists to run REST work; here the "work" is simply having a
        # valid token in hand, because the stream outlives this call and cannot be
        # retried inside it. Nothing in the body raises `ExpiredToken`, so the wrapper's
        # single retry never fires - the proactive refresh of step 1 is what we want.
        return token

    return await with_portal_token(portal.id, _use)


async def _stream(
    request: Request, url: str, *, portal_id: int, call_id: int
) -> StreamingResponse | JSONResponse:
    """Open the upstream request and hand the body straight through (§9 step 3).

    Nothing is buffered and nothing is written to disk: the generator forwards raw chunks
    and closes both the response and the client when the browser goes away (an aborted
    seek is the normal case, not an error).

    `Accept-Encoding: identity` is sent on purpose. The body is forwarded raw so that the
    upstream `Content-Length` we pass on stays true; audio is already compressed, so
    there is nothing to gain from a content coding and everything to lose from a length
    that describes the compressed bytes of a body we hand over decoded.
    """
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
    }
    headers["Accept-Encoding"] = "identity"

    client = httpx.AsyncClient(
        timeout=_TIMEOUT,
        # Recordings are commonly served from a storage host behind a redirect. Following
        # it is safe here because the credential rides in the query string of the FIRST
        # url only and httpx does not copy a query across a redirect; no Authorization
        # header is ever set on this client.
        follow_redirects=True,
        max_redirects=5,
    )
    upstream: httpx.Response | None = None
    try:
        upstream = await client.send(
            client.build_request("GET", url, headers=headers), stream=True
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        # The exception class only: `str(exc)` from httpx embeds the request URL, which
        # at this point carries the access token (§6).
        _log.warning(
            "record: upstream transport failure",
            extra={"portal_id": portal_id, "call_id": call_id, "error": type(exc).__name__},
        )
        return _error("record_unavailable", 502)

    status = upstream.status_code
    if status in _STALE_UPSTREAM:
        await upstream.aclose()
        await client.aclose()
        _log.info(
            "record: upstream refused the cached link",
            extra={"portal_id": portal_id, "call_id": call_id, "status": status},
        )
        # §5.7 rule 3: this is the code that makes the SPA call the refresh endpoint.
        return _error("record_stale", 409)
    if status not in (200, 206):
        await upstream.aclose()
        await client.aclose()
        _log.warning(
            "record: unexpected upstream status",
            extra={"portal_id": portal_id, "call_id": call_id, "status": status},
        )
        return _error("record_unavailable", 502)

    async def body() -> AsyncIterator[bytes]:
        try:
            # `aiter_bytes`, not `aiter_raw`: raw would hand back whatever content coding
            # the upstream chose, and the `Content-Length` forwarded below describes the
            # decoded body. We asked for `identity`, so in practice the two are the same
            # bytes - but if an upstream ignores that, this is the pair that stays honest.
            async for chunk in upstream.aiter_bytes(_CHUNK_BYTES):
                yield chunk
        except httpx.HTTPError as exc:
            # The response has already begun, so the status is spent and there is no way
            # left to tell the client anything but "the body stopped". Swallowing it keeps
            # the failure out of the 500 handler, which would otherwise log a full
            # traceback per stalled playback and report an application fault for what is
            # an upstream one. The class name alone: httpx puts the request URL in the
            # message, and that URL is a live listen-link (§9 results).
            _log.warning(
                "record: upstream stopped mid-stream",
                extra={
                    "portal_id": portal_id,
                    "call_id": call_id,
                    "error": type(exc).__name__,
                },
            )
        finally:
            # Both closes can themselves raise on a connection that already timed out,
            # and an exception here escapes the generator into Starlette's task group -
            # which is how a caught ReadTimeout still surfaced as a 500 with an exception
            # group in the log. Closing is best-effort by definition: the request is over
            # either way, and there is nobody left to tell.
            for closer in (upstream.aclose, client.aclose):
                try:
                    await closer()
                except Exception:  # noqa: BLE001 - teardown must not fail the response
                    pass

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _PASSTHROUGH_HEADERS
    }
    encoding = upstream.headers.get("content-encoding", "").strip().lower()
    if encoding and encoding != "identity":
        # The length header counts the encoded bytes; the body above is decoded. Dropping
        # it lets the response be chunked rather than truncated at the wrong byte.
        headers.pop("content-length", None)

    return StreamingResponse(body(), status_code=status, headers=headers)


# --- GET /calls/{id}/record ----------------------------------------------------------


@router.get("/calls/{call_id}/record")
async def record(call_id: int, request: Request) -> Response:
    """Serve one recording under a `?t=` grant, or say why not (§4.6, §4.7, §9).

    No `get_principal` here, and that is the design rather than an omission: an `<audio>`
    element sends no `Authorization` header, so the grant in the query string is the only
    credential this request can carry. Everything the session would have provided is
    re-derived from it and re-checked - the portal row (status, endpoint), the row itself
    through `calls_repo` with `scope_filter` re-applied, and the recording mode.

    The path's `call_id` must equal the grant's `cid`. A grant is minted for one row; a
    caller who moves it to another path is asking for a row it was not issued for.
    """
    token = request.query_params.get("t", "")
    try:
        claims = verify_play_token(token)
    except TokenError:
        # Expired grant and forged grant look identical from outside (§4.6). The SPA
        # answers both the same way: mint a new one.
        _log.info("record: playback grant rejected")
        return _error("invalid_grant", 401)

    if claims.cid != call_id:
        _log.warning("record: playback grant used for another call", extra={"call_id": call_id})
        return _error("invalid_grant", 401)

    async with control_txn() as session:
        portal = (
            await session.execute(select(Portal).where(Portal.id == claims.pid))
        ).scalar_one_or_none()
    if portal is None or portal.status != _ACTIVE:
        # §4.6 revocation, re-applied: an uninstalled portal stops serving audio the
        # moment it uninstalls, not when the five minutes run out.
        return _error("portal_inactive", 401)

    principal = _principal_of(claims, portal)
    # `scope_filter` raises `PrincipalError('no_stats_permission', 403)` for a denied
    # level; `PrincipalErrorRoute` renders it. A grant can only carry a level that was
    # live at mint time, so this is the backstop, not the common path.
    statement = (
        base_select(principal)
        .where(Call.id == call_id)
        .with_only_columns(Call.call_record_url, Call.has_record)
    )
    async with tenant_txn(portal.id) as session:
        row = (await session.execute(statement)).first()

    if row is None:
        return _error("call_not_found", 404)

    if settings.recording_mode == _MODE_OFF:
        # §9: until the spike decides, the table shows a "has recording" icon and a
        # `BX24.openPath` link to the call in Bitrix24. This code is what the SPA renders
        # as "open the call in Bitrix24".
        return _error("recording_disabled", 409)

    url = (row.call_record_url or "").strip()
    if not url:
        # `has_record` is true for a row with only `RECORD_FILE_ID` (§3): the recording
        # exists in Bitrix24 but there is no URL to stream, and `disk.file.get` needs a
        # scope this app does not request (§9 option 3). The SPA falls back to the link.
        return _error("record_missing", 404)

    try:
        access_token = await _access_token_for(claims, portal)
    except (CredentialUnavailable, DecryptionError, MemberIdMismatch, BitrixError) as exc:
        _log.warning(
            "record: no usable token for playback",
            extra={"portal_id": portal.id, "call_id": call_id, "error": type(exc).__name__},
        )
        return _error("record_unavailable", 502)

    if access_token is None:
        if claims.acc != _ACCESS_ALL:
            # The five-minute hand-off expired or this process never saw it: the SPA
            # re-mints the grant with a fresh `BX24.getAuth()` token (§9 step 3).
            return _error("viewer_token_required", 409)
        return _error("record_unavailable", 502)

    # The credential is attached ONLY for the portal's own hosts (§4.1: a URL that came
    # back inside a REST response is still portal-controlled data).
    upstream_url = (
        _with_auth(url, access_token)
        if _same_host(url, portal.client_endpoint, portal.domain)
        else url
    )
    return await _stream(request, upstream_url, portal_id=portal.id, call_id=call_id)
