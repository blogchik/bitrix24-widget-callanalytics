"""The viewer's own Bitrix24 access token, read out of a request body (§4.6, §4.12).

Two endpoints now need it and neither may let it leak, so the parser lives in one place
rather than in whichever module grew it first.

**Why a body and not a query parameter.** The value is a live Bitrix24 access token. A URL
is written to `rest_log`, to any access log in front of the app and to the browser's own
history; a body is excluded from §6's logging by construction. That is also why both
endpoints that take one are `POST` despite being reads.

**Why hand-parsed.** FastAPI's `RequestValidationError` renders the offending `input` back
to the caller - and for these endpoints that input IS the token. A Pydantic model would put
a live credential into a 422 body the first time somebody posted a malformed one. The
length is checked before the JSON is decoded so an oversized body is refused undecoded, and
no log line in this module or its callers ever names the value.
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import Request

from app.security.principal import PrincipalError

__all__ = ["read_viewer_access_token"]

#: A body that carries a live Bitrix24 access token (§4.6 keeps such bodies out of every
#: log). One opaque string; the length is checked before parsing so an oversized body is
#: refused undecoded.
_MAX_BODY_BYTES: Final[int] = 8 * 1024
_TOKEN_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512


async def read_viewer_access_token(request: Request) -> str | None:
    """`{"access_token": ...}` from the body, or None - and never the value in an error.

    `None` means the body was absent or carried no token, which is a state rather than a
    failure: `POST /calls/{id}/play-url` serves an administrator without one, and
    `POST /deals` answers `viewer_token_required` so the SPA can fetch one and come back.
    A body that is present but malformed is a `bad_request`, because a caller that sent
    something unparseable is not the same as a caller that sent nothing.
    """
    raw = await request.body()
    if not raw:
        return None
    if len(raw) > _MAX_BODY_BYTES:
        raise PrincipalError("bad_request", 400)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise PrincipalError("bad_request", 400) from None
    if not isinstance(payload, dict):
        raise PrincipalError("bad_request", 400)
    value = payload.get("access_token")
    if value is None:
        return None
    if not isinstance(value, str):
        raise PrincipalError("bad_request", 400)
    token = value.strip()
    if not token:
        return None
    if not _TOKEN_MIN <= len(token) <= _TOKEN_MAX or not set(token) <= _TOKEN_CHARS:
        raise PrincipalError("bad_request", 400)
    return token
