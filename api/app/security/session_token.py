"""§4.6 session token: a bearer JWT delivered in a URL fragment, never a cookie.

WHY two token kinds in one module: they share the signing secret, so the only
thing standing between a 5-minute playback token and an hour-long session is the
`typ` claim. Keeping both codecs side by side makes that separation auditable in
one screen — a playback token minted for an `<audio src>` (which cannot carry an
`Authorization` header, §4.6) must never be replayable as a session, and a
session token must never be accepted as a signed media grant.

WHY no `jti` / revocation list: §4.6 revokes by state, not by list — `get_principal`
re-reads the `portals` row on every request and 401s on `status != 'active'`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import jwt

from app.config import settings

_ALG: Final[str] = "HS256"

# §4.6: distinct token kinds, distinct `typ`. Never reuse these letters.
_TYP_SESSION: Final[str] = "s"
_TYP_PLAY: Final[str] = "p"

# §4.7 collapses Bitrix24's four statistics levels to these three.
_ACCESS_LEVELS: Final[frozenset[str]] = frozenset({"all", "own", "denied"})

_SESSION_REQUIRED: Final[list[str]] = [
    "typ",
    "pid",
    "mid",
    "sub",
    "adm",
    "acc",
    "tz",
    "lang",
    "plc",
    "iat",
    "exp",
]
_PLAY_REQUIRED: Final[list[str]] = ["typ", "pid", "sub", "acc", "cid", "exp"]


@dataclass(frozen=True)
class SessionClaims:
    """The whole per-request principal, ~300 bytes on the wire (§4.6)."""

    pid: int
    mid: str
    sub: int
    adm: bool
    acc: str
    tz: str
    lang: str
    plc: str
    ent: dict[str, Any] | None
    iat: int
    exp: int


@dataclass(frozen=True)
class PlayClaims:
    """Narrow grant for one recording of one call, minted under a live session (§4.6)."""

    pid: int
    sub: int
    acc: str
    cid: int
    exp: int


class TokenError(Exception):
    """Any reason a token is unusable: bad signature, wrong `typ`, malformed claims.

    One exception type on purpose — the caller answers 401 and must not be able to
    tell an attacker apart from an expired tab by the state it renders.
    """


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.session_secret, algorithm=_ALG)


def _decode(
    token: str, *, typ: str, required: list[str], verify_exp: bool
) -> dict[str, Any]:
    if not isinstance(token, str) or not token:
        raise TokenError("empty token")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.session_secret,
            algorithms=[_ALG],
            options={
                "require": required,
                "verify_exp": verify_exp,
                # PyJWT >= 2.10 enforces RFC 7519's "sub must be a string". Ours is the
                # Bitrix24 user id and §4.6 types it as an integer, so the registered-claim
                # rule is turned off and `_as_int` does the checking instead.
                "verify_sub": False,
            },
        )
    except jwt.PyJWTError as exc:  # signature, expiry, malformed segments, missing claims
        raise TokenError(str(exc)) from exc
    # Checked only after the signature verified, so an unsigned blob cannot steer it.
    if claims.get("typ") != typ:
        raise TokenError("wrong token type")
    return claims


def _as_int(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    # bool is an int subclass; a `true` where an id belongs is malformed, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TokenError(f"claim {name!r} is not an integer")
    return value


def _as_str(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str):
        raise TokenError(f"claim {name!r} is not a string")
    return value


def _as_access(claims: dict[str, Any]) -> str:
    acc = _as_str(claims, "acc")
    if acc not in _ACCESS_LEVELS:
        raise TokenError("claim 'acc' is not an access level")
    return acc


def issue_session(
    *,
    pid: int,
    mid: str,
    sub: int,
    adm: bool,
    acc: str,
    tz: str,
    lang: str,
    plc: str,
    ent: dict[str, Any] | None,
    ttl_seconds: int,
) -> str:
    """Mint the SPA's bearer token; `handoff.html` writes it into the URL fragment.

    `ttl_seconds` is decided by the caller as `min(AUTH_EXPIRES, 3600)` (§4.6), so
    access is re-evaluated against Bitrix24 at least hourly through the exchange
    path. A non-positive TTL would mint a token that is already dead.
    """
    if acc not in _ACCESS_LEVELS:
        raise TokenError("acc must be one of all|own|denied")
    if ttl_seconds <= 0:
        raise TokenError("ttl_seconds must be positive")
    now = int(time.time())
    return _encode(
        {
            "typ": _TYP_SESSION,
            "pid": int(pid),
            "mid": str(mid),
            "sub": int(sub),
            "adm": bool(adm),
            "acc": acc,
            "tz": str(tz),
            "lang": str(lang),
            "plc": str(plc),
            "ent": ent,
            "iat": now,
            "exp": now + int(ttl_seconds),
        }
    )


def verify_session(token: str, *, ignore_exp: bool = False) -> SessionClaims:
    """Verify a session token.

    `ignore_exp=True` exists for exactly one caller: `POST /api/v1/session/exchange`
    (§4.6). A tab left open past the hour presents a dead JWT only so the backend can
    learn `pid` / `plc` / `ent` from it; the exchange then re-proves the user against
    Bitrix24 with a fresh `BX24.getAuth()` token at the stored `client_endpoint`,
    requires the same `sub`, and re-decides access — nothing is trusted on the strength
    of the expired token alone. Never pass it anywhere else: it would turn every leaked
    JWT into a permanent credential.
    """
    claims = _decode(
        token, typ=_TYP_SESSION, required=_SESSION_REQUIRED, verify_exp=not ignore_exp
    )
    ent = claims.get("ent")
    if ent is not None and not isinstance(ent, dict):
        raise TokenError("claim 'ent' is neither null nor an object")
    adm = claims.get("adm")
    if not isinstance(adm, bool):
        raise TokenError("claim 'adm' is not a boolean")
    return SessionClaims(
        pid=_as_int(claims, "pid"),
        mid=_as_str(claims, "mid"),
        sub=_as_int(claims, "sub"),
        adm=adm,
        acc=_as_access(claims),
        tz=_as_str(claims, "tz"),
        lang=_as_str(claims, "lang"),
        plc=_as_str(claims, "plc"),
        ent=ent,
        iat=_as_int(claims, "iat"),
        exp=_as_int(claims, "exp"),
    )


def issue_play_token(
    *, pid: int, sub: int, acc: str, cid: int, ttl_seconds: int = 300
) -> str:
    """Mint the `?t=` grant for `GET /api/v1/calls/{id}/record` (§4.6).

    Bound to one call id because it travels as a query parameter on an `<audio>`
    source and therefore leaks into browser history and any intermediary that logs
    URLs; five minutes and one row is the whole blast radius. `acc` rides along so
    `record.py` can re-apply `scope_filter` without a session.
    """
    if acc not in _ACCESS_LEVELS:
        raise TokenError("acc must be one of all|own|denied")
    if ttl_seconds <= 0:
        raise TokenError("ttl_seconds must be positive")
    return _encode(
        {
            "typ": _TYP_PLAY,
            "pid": int(pid),
            "sub": int(sub),
            "acc": acc,
            "cid": int(cid),
            "exp": int(time.time()) + int(ttl_seconds),
        }
    )


def verify_play_token(token: str) -> PlayClaims:
    """Verify a playback grant. No `ignore_exp` twin: an expired media URL is simply dead."""
    claims = _decode(token, typ=_TYP_PLAY, required=_PLAY_REQUIRED, verify_exp=True)
    return PlayClaims(
        pid=_as_int(claims, "pid"),
        sub=_as_int(claims, "sub"),
        acc=_as_access(claims),
        cid=_as_int(claims, "cid"),
        exp=_as_int(claims, "exp"),
    )
