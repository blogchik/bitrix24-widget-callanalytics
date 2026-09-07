"""The OAuth refresh exchange and the single-flight portal-token wrapper.

This module owns the **trust root** of the whole system (§4.1). Three rules explain
almost every line below:

1. **`member_id` proves nothing on its own; it is proven only by a refresh exchange.**
   A Bitrix24 POST can claim any `member_id` - the value is public (§11 assumption 6).
   What cannot be forged is a refresh token that our `client_id`/`client_secret`
   exchange at an allowlisted OAuth host, whose response then *tells us* which portal
   the credential belongs to. `expected_member_id` mismatch therefore raises rather than
   warns: everything downstream (which tenant's rows, which encryption AAD, which RLS
   context) hangs off that one comparison.
2. **The refresh URL is never built from `DOMAIN`.** It comes from the portal-sent
   `SERVER_ENDPOINT` host *only if* that host is in `OAUTH_HOST_ALLOWLIST`, otherwise
   from the hardcoded default. A portal that could name the OAuth host freely could
   collect our `client_secret` on the first exchange.
3. **Refreshing is rare and reactive.** The documentation is explicit ("Do not renew the
   token before every REST API request, and do not schedule the renewal for once an hour
   or once a day"); excessive refreshes can get the application blocked. So: refresh on
   `expired_token`, or within 60 s of a known expiry, and never on a timer. Refresh
   tokens also rotate and are single-use - a lost response burns the chain - which is
   why the write is serialized by `SELECT ... FOR UPDATE` on the `portals` row and why a
   second process that observes a moved `token_version` uses the stored token instead of
   starting a second exchange.

The chain dies after 180 days of not being refreshed; §4.4 step 6 re-seeds from an admin
open after `TOKEN_RESEED_AFTER_DAYS` (120) so that never happens silently.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.errors import (
    BitrixError,
    ExpiredToken,
    InsufficientScope,
    InvalidGrant,
    NoAuthFound,
    PaymentRequired,
    TransportError,
    classify,
)
from app.config import settings
from app.db.models import Portal
from app.db.session import control_txn
from app.logging import get_logger, get_request_id
from app.security.crypto import decrypt, encrypt
from app.security.redact import REDACTED
from app.services.rest_log import write_rest_log

__all__ = [
    "DEFAULT_OAUTH_TOKEN_URL",
    "CredentialUnavailable",
    "MemberIdMismatch",
    "OAuthRateLimited",
    "TokenResponse",
    "exchange_refresh_token",
    "reset_exchange_rate_limit",
    "with_portal_token",
]

logger = get_logger(__name__)

# S105 below: these are URLs whose PATH happens to contain the word "token"; the linter's
# hardcoded-password heuristic has no way to tell the difference.
#: §4.1: used whenever `SERVER_ENDPOINT` is absent or names a host outside the allowlist.
DEFAULT_OAUTH_TOKEN_URL: Final[str] = "https://oauth.bitrix.info/oauth/token/"  # noqa: S105
_TOKEN_PATH: Final[str] = "/oauth/token/"  # noqa: S105

#: §5.8 step 4 holds the `portals` row lock across this call, so it must be short.
_EXCHANGE_TIMEOUT_S: Final[float] = 15.0

#: §4.1: `OAUTH_EXCHANGE_LIMIT` exchanges per 10 minutes.
_RATE_WINDOW_S: Final[float] = 600.0

#: Beyond this many tracked keys the window map is swept; a flood of distinct source IPs
#: must not turn the limiter itself into an unbounded allocation.
_MAX_TRACKED_KEYS: Final[int] = 4096

#: §5.8 step 1: refresh proactively when the stored token is this close to expiry.
_PROACTIVE_MARGIN_S: Final[int] = 60

#: The documented access-token lifetime is one hour; the bound keeps a hostile or broken
#: `expires_in` from parking `token_expires_at` in the year 3000 (the same 1..86400 range
#: `bitrix/forms.py` enforces on `AUTH_EXPIRES`).
_MIN_EXPIRES_IN: Final[int] = 1
_MAX_EXPIRES_IN: Final[int] = 86_400
_DEFAULT_EXPIRES_IN: Final[int] = 3_600

#: What the `rest_log` row records as the request (§6). The real parameters are never
#: built into this dict: `client_secret` and `refresh_token` must not exist in a row that
#: retention keeps for a week, and recording the *shape* is all the moderation trail
#: needs. `redact()` in the log writer is the second layer, not the first.
_LOGGED_REQUEST: Final[dict[str, str]] = {
    "grant_type": "refresh_token",
    "client_id": REDACTED,
    "client_secret": REDACTED,
    "refresh_token": REDACTED,
}

#: §5.8 terminal states reachable from a refresh. `portals.token_status` is CHECK-
#: constrained to this vocabulary, so nothing else may be written here.
_TERMINAL_TOKEN_STATUS: Final[dict[type[BitrixError], str]] = {
    InvalidGrant: "reauth_required",
    NoAuthFound: "reauth_required",
    PaymentRequired: "reauth_required",
    # "fail loudly, never retried" (§5.8): a missing scope is a deploy-time bug and the
    # settings page must show it instead of the worker retrying it forever.
    InsufficientScope: "reauth_required",
}


@dataclass(frozen=True)
class TokenResponse:
    """One successful `grant_type=refresh_token` response.

    `client_endpoint` is the ONLY sanctioned source of a REST base URL (§4.1 endpoint
    invariant) and `member_id` is the proven tenant identity.

    `domain` is kept for completeness but is **the authorization server**
    (`oauth.bitrix.info`), not the portal - the docs say so explicitly and
    docs/bitrix24-api-research.md records it as a correction to the brief. Writing it
    into `portals.domain` would break the CSP `frame-ancestors` header of §4.10 and the
    display name on every state page, so nothing may copy it there.

    `refresh_token` is a NEW value: the old one is spent. Whoever receives a
    `TokenResponse` must persist both tokens or the chain is lost.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    expires: int | None
    client_endpoint: str
    server_endpoint: str | None
    member_id: str
    user_id: int | None
    status: str | None
    scope: str
    domain: str | None

    @property
    def expires_at(self) -> dt.datetime:
        """Absolute expiry, preferring the server's own epoch over our clock.

        `expires` is the authoritative value (it is the auth server's own view); the
        `expires_in` fallback exists because on-premise builds have been seen omitting it.
        """
        if self.expires is not None:
            return dt.datetime.fromtimestamp(self.expires, tz=dt.UTC)
        return dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self.expires_in)


class OAuthRateLimited(Exception):
    """Too many exchanges for one `member_id` or one source IP (§4.1).

    The handler renders the translated "please try again" state and logs
    `portal_events(refresh_failed, reason=rate_limited)`. `scope_key` names which bucket
    tripped so that log line is actionable; it holds a `member_id` (public, §11) or a
    source IP - never a credential.
    """

    scope_key: str

    def __init__(self, scope_key: str) -> None:
        self.scope_key = scope_key
        super().__init__(f"oauth exchange rate limit reached for {scope_key}")


class MemberIdMismatch(Exception):
    """The exchange proved a DIFFERENT portal than the caller expected (§4.1).

    This is the failure of the comparison that the entire trust model rests on: the
    caller was told "I am portal X", the OAuth server answered "this credential belongs
    to portal Y". §4.3 step 2 renders bad request (HTTP 400), §4.4 step 2 refuses to
    self-heal, §4.9 rule 6 creates nothing. It is deliberately NOT a `BitrixError`: no
    Bitrix24 error code produced it, and no retry or back-off policy applies to it.
    """

    expected: str
    received: str

    def __init__(self, *, expected: str, received: str) -> None:
        self.expected = expected
        self.received = received
        super().__init__("refresh response member_id does not match the expected portal")


class CredentialUnavailable(Exception):
    """There is no stored credential to work with.

    `reason` is one of `portal_not_found`, `no_refresh_token`, `no_access_token`.
    `no_refresh_token` is the §4.1 unsupported-portal case (a fully isolated on-premise
    box with a custom auth provider posts an empty `REFRESH_ID`); the others mean the row
    was cleared by an uninstall (§4.9 rule 3) while a job still held its id.
    """

    reason: str

    def __init__(self, reason: str, *, portal_id: int | None = None) -> None:
        self.reason = reason
        self.portal_id = portal_id
        super().__init__(f"{reason} (portal_id={portal_id})")


# --- rate limiting ------------------------------------------------------------------
#
# An in-process sliding window is correct for v1 because the compose file runs exactly
# ONE api container (§10) and the worker bypasses the limiter entirely. A second api
# replica would give each replica its own window and multiply the effective limit, so
# scaling out requires moving these deques into a shared store (Redis / a Postgres table
# keyed by scope) before adding the replica - not after.

_exchange_window: dict[str, deque[float]] = {}


def _sweep(now: float) -> None:
    """Drop windows that have fully aged out; bounds the limiter's memory."""
    for key in [k for k, w in _exchange_window.items() if not w or now - w[-1] >= _RATE_WINDOW_S]:
        del _exchange_window[key]


def _scope_keys(member_id: str | None, source_ip: str | None) -> list[str]:
    """§4.1 limits per `member_id` AND per source IP; either one alone is bypassable."""
    keys: list[str] = []
    if member_id:
        keys.append(f"member:{member_id.strip().lower()}")
    if source_ip:
        keys.append(f"ip:{source_ip.strip()}")
    return keys


def _check_rate_limit(keys: Iterable[str]) -> None:
    """Raise `OAuthRateLimited` when any bucket is full, else charge them all.

    Checking every bucket before charging any keeps a tripped `member_id` from also
    consuming the IP budget of an unrelated portal behind the same NAT. A rejected call
    allocates nothing: buckets are created only when an exchange is actually charged, so
    a flood of distinct source IPs cannot grow the map through refusals alone.
    """
    limit = settings.oauth_exchange_limit
    now = time.monotonic()
    if len(_exchange_window) > _MAX_TRACKED_KEYS:
        _sweep(now)

    scopes = list(keys)
    for key in scopes:
        window = _exchange_window.get(key)
        if window is None:
            continue
        while window and now - window[0] >= _RATE_WINDOW_S:
            window.popleft()
        if not window:
            del _exchange_window[key]
            continue
        if len(window) >= limit:
            raise OAuthRateLimited(key)

    for key in scopes:
        _exchange_window.setdefault(key, deque()).append(now)


def reset_exchange_rate_limit() -> None:
    """Clear the sliding windows. For tests and for an operator-driven unblock only."""
    _exchange_window.clear()


# --- the exchange -------------------------------------------------------------------


def _token_url(server_endpoint: str | None) -> str:
    """§4.1: the allowlisted OAuth host, or the hardcoded default. Never `DOMAIN`.

    Only the *host* of `SERVER_ENDPOINT` is taken, and only after an allowlist hit; the
    scheme is forced to https and the path is ours. A portal-controlled scheme, port or
    path is how a `client_secret` walks out of the process.
    """
    if not server_endpoint:
        return DEFAULT_OAUTH_TOKEN_URL
    try:
        host = (urlsplit(server_endpoint).hostname or "").lower()
    except ValueError:  # malformed URL: not a reason to trust it
        return DEFAULT_OAUTH_TOKEN_URL
    if host and host in settings.oauth_host_allowlist:
        return f"https://{host}{_TOKEN_PATH}"
    return DEFAULT_OAUTH_TOKEN_URL


def _correlation(explicit: uuid.UUID | None) -> uuid.UUID | None:
    """Prefer the caller's id, else the request/run id bound by the logging contextvar."""
    if explicit is not None:
        return explicit
    raw = get_request_id()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


def _str_field(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _int_field(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _valid_endpoint(value: str | None) -> str | None:
    """Shape check for `client_endpoint` / `server_endpoint` before they are stored.

    Not an allowlist - `client_endpoint` is authoritative by definition (§4.1) and an
    on-premise portal is any hostname at all - but a value that is not an absolute http(s)
    URL cannot become a REST base and must be rejected at the boundary rather than
    concatenated into a request later.
    """
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value


def _parse_token_response(body: dict[str, Any], *, http_status: int | None) -> TokenResponse:
    """Build the dataclass, or raise a typed error describing what was unusable."""
    access_token = _str_field(body, "access_token")
    refresh_token = _str_field(body, "refresh_token")
    client_endpoint = _valid_endpoint(_str_field(body, "client_endpoint"))
    member_id = _str_field(body, "member_id")

    missing = [
        name
        for name, value in (
            ("access_token", access_token),
            ("refresh_token", refresh_token),
            ("client_endpoint", client_endpoint),
            ("member_id", member_id),
        )
        if not value
    ]
    if missing or access_token is None or refresh_token is None:
        raise classify(
            "ERROR_UNEXPECTED_ANSWER",
            http_status=http_status,
            description=f"refresh response is missing or malformed: {','.join(missing)}",
        )
    assert client_endpoint is not None and member_id is not None  # narrowed by `missing`

    expires_in = _int_field(body, "expires_in") or _DEFAULT_EXPIRES_IN
    expires_in = max(_MIN_EXPIRES_IN, min(_MAX_EXPIRES_IN, expires_in))

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        expires=_int_field(body, "expires"),
        client_endpoint=client_endpoint,
        server_endpoint=_valid_endpoint(_str_field(body, "server_endpoint")),
        member_id=member_id.lower(),
        user_id=_int_field(body, "user_id"),
        status=_str_field(body, "status"),
        scope=_str_field(body, "scope") or "",
        # Kept, never stored as the portal domain - see the class docstring.
        domain=_str_field(body, "domain"),
    )


async def exchange_refresh_token(
    refresh_token: str,
    *,
    server_endpoint: str | None = None,
    expected_member_id: str | None = None,
    portal_id: int | None = None,
    source_ip: str | None = None,
    rate_limit: bool = True,
    correlation_id: uuid.UUID | None = None,
) -> TokenResponse:
    """`GET <oauth host>/oauth/token/?grant_type=refresh_token&...` - one exchange.

    Writes exactly one `rest_log` row (`kind="oauth"`, §6) on every path that actually
    reached the network, including the exception path, with the secret never present in
    the logged request. A rate-limited call writes none: no exchange happened, and §4.1
    records that case as `portal_events(refresh_failed, reason=rate_limited)` instead.

    `rate_limit=True` is the default because most callers are handlers acting on
    unauthenticated Bitrix24 input (§4.3 step 2, §4.4 step 2, §4.9 rule 6). The worker's
    `expired_token` path passes `rate_limit=False`: it is driven by our own stored,
    admin-proven credential and by a real 401, and throttling it would strand a portal.

    Raises: `OAuthRateLimited`, `MemberIdMismatch`, `InvalidGrant` (dead chain - the
    refresh token was reused or 180 days passed), `TransportError`, or whatever
    `classify` makes of the OAuth server's `error` field.
    """
    token = (refresh_token or "").strip()
    if not token:
        # §4.1 endpoint invariant: an isolated box posts an empty REFRESH_ID and the
        # handler renders /state/unsupported_portal. Never call the OAuth host with "".
        raise CredentialUnavailable("no_refresh_token", portal_id=portal_id)

    if rate_limit:
        _check_rate_limit(_scope_keys(expected_member_id, source_ip))

    url = _token_url(server_endpoint)
    params = {
        "grant_type": "refresh_token",
        "client_id": settings.b24_client_id,
        "client_secret": settings.b24_client_secret,
        "refresh_token": token,
    }

    started = time.perf_counter()
    http_status: int | None = None
    error_code: str | None = None
    body: dict[str, Any] | None = None
    logged_member_id: str | None = expected_member_id
    logged_user_id: int | None = None

    try:
        try:
            async with httpx.AsyncClient(
                timeout=_EXCHANGE_TIMEOUT_S,
                # A redirect from the OAuth host would forward `client_secret` to
                # whatever host it names. There is no legitimate redirect here.
                follow_redirects=False,
            ) as http:
                response = await http.get(url, params=params, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            # `str(exc)` from httpx frequently embeds the full request URL, which carries
            # `client_secret` and the refresh token. Only the exception CLASS is recorded.
            error_code = TransportError.default_code
            raise TransportError(description=type(exc).__name__) from exc

        http_status = response.status_code
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        body = parsed if isinstance(parsed, dict) else None

        if body is None:
            error_code = "ERROR_UNEXPECTED_ANSWER"
            raise classify(
                error_code,
                http_status=http_status,
                description="refresh response was not a JSON object",
            )

        error_code = _str_field(body, "error")
        if error_code or http_status != 200:
            raise classify(
                error_code,
                http_status=http_status,
                description=_str_field(body, "error_description") or "",
                payload=body,
            )

        tokens = _parse_token_response(body, http_status=http_status)
        logged_member_id = tokens.member_id
        logged_user_id = tokens.user_id

        if expected_member_id is not None:
            expected = expected_member_id.strip().lower()
            if tokens.member_id != expected:
                # The single comparison the whole trust model rests on (§4.1 decision 2).
                error_code = "member_id_mismatch"
                logger.warning(
                    "refresh response member_id mismatch",
                    extra={"portal_id": portal_id, "expected_member_id": expected},
                )
                raise MemberIdMismatch(expected=expected, received=tokens.member_id)

        return tokens
    finally:
        # §6: one row per exchange, in its own transaction, even when we are unwinding.
        # `write_rest_log` swallows its own failures - logging must never fail the work.
        await write_rest_log(
            direction="out",
            kind="oauth",
            method="GET",
            url=url,  # already query-less; the parameters live in `request` redacted
            portal_id=portal_id,
            member_id=logged_member_id,
            correlation_id=_correlation(correlation_id),
            token_user_id=logged_user_id,
            request=_LOGGED_REQUEST,
            http_status=http_status,
            error_code=error_code,
            response=body,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


# --- single-flight portal token (§5.8) ----------------------------------------------


@dataclass(frozen=True)
class _StoredCredential:
    """What the unlocked read of §5.8 step 1 learns."""

    token_version: int
    access_token: str | None
    token_expires_at: dt.datetime | None


@dataclass(frozen=True)
class _Credential:
    """A usable access token and the `token_version` it was stored under."""

    token_version: int
    access_token: str


def _expires_soon(expires_at: dt.datetime | None) -> bool:
    """§5.8 step 1. A NULL expiry does NOT trigger a refresh.

    Unknown expiry means "try it first": the docs warn that refreshing more often than
    necessary can get the application blocked, and a genuinely dead token comes back as
    `expired_token`, which step 2 already handles.
    """
    if expires_at is None:
        return False
    return expires_at < dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=_PROACTIVE_MARGIN_S)


async def _load_credential(portal_id: int) -> _StoredCredential:
    """§5.8 step 1: read the credential WITHOUT a lock and remember `token_version`.

    Unlocked on purpose - the common case is a valid token and taking a row lock for
    every REST call would serialize a portal's whole sync behind its credential row.
    """
    async with control_txn() as session:
        row = (
            await session.execute(
                select(
                    Portal.member_id,
                    Portal.access_token_enc,
                    Portal.token_version,
                    Portal.token_expires_at,
                ).where(Portal.id == portal_id)
            )
        ).one_or_none()

    if row is None:
        raise CredentialUnavailable("portal_not_found", portal_id=portal_id)

    access_token: str | None = None
    if row.access_token_enc is not None:
        access_token = decrypt(
            row.access_token_enc, member_id=row.member_id, column="access_token"
        )
    return _StoredCredential(
        token_version=row.token_version,
        access_token=access_token,
        token_expires_at=row.token_expires_at,
    )


async def _refresh_locked(portal_id: int, *, seen_version: int) -> _Credential:
    """§5.8 steps 3-4: the single-flight refresh, under `SELECT ... FOR UPDATE`.

    **This is the narrow refresh-only credential updater** referred to in §4.1. It is the
    one place other than `services.portals.store_portal_credential` that writes
    `access_token_enc` / `refresh_token_enc` / `client_endpoint`, and it exists because
    `store_portal_credential` demands an admin `Identity` as proof - which this path does
    not have and must not manufacture. Re-proving `user.admin` here would mean an extra
    REST call inside a held row lock on every token expiry, and there is nothing new to
    prove: the credential was already admin-proven when it was stored, refreshing only
    extends the SAME chain for the SAME owner.

    Consequently this updater deliberately does **not** touch `token_user_id`,
    `token_admin_verified_at`, `scope`, `domain` or `app_status`. Ownership and admin
    standing are re-established only by a path that ran `verify_admin_token`
    (§4.3 step 3, §4.4 step 6, §4.9 rule 4) plus the worker's daily re-verification
    (§5.8). `domain` in particular is never written from a refresh response at all - the
    response's `domain` is the authorization server.
    """
    failure: BaseException | None = None
    refreshed: _Credential | None = None

    async with control_txn() as session:
        row = (
            await session.execute(
                select(
                    Portal.member_id,
                    Portal.access_token_enc,
                    Portal.refresh_token_enc,
                    Portal.server_endpoint,
                    Portal.token_version,
                )
                .where(Portal.id == portal_id)
                # FOR NO KEY UPDATE, not FOR UPDATE. Both serialize refreshers against
                # each other (they conflict with themselves), which is all §5.8 step 3
                # needs - we only ever change non-key columns. The difference matters:
                # FOR UPDATE also conflicts with the FOR KEY SHARE lock that Postgres
                # takes on this row for every `rest_log` INSERT that references it, and
                # §6 writes that row from a SEPARATE transaction while the exchange (and
                # therefore this lock) is still in flight. With FOR UPDATE the coroutine
                # deadlocks against itself - two live sessions, no cycle for Postgres's
                # detector to break, so the visit hangs until the lease expires.
                .with_for_update(key_share=True)
            )
        ).one_or_none()

        if row is None:
            raise CredentialUnavailable("portal_not_found", portal_id=portal_id)

        if row.token_version != seen_version and row.access_token_enc is not None:
            # §5.8 step 3: another process refreshed while we waited for the lock. Using
            # its token is not an optimisation - starting a second exchange would spend a
            # refresh token that has already been rotated and kill the chain.
            logger.info(
                "reusing token refreshed by another process",
                extra={"portal_id": portal_id, "token_version": row.token_version},
            )
            return _Credential(
                token_version=row.token_version,
                access_token=decrypt(
                    row.access_token_enc, member_id=row.member_id, column="access_token"
                ),
            )

        if row.refresh_token_enc is None:
            raise CredentialUnavailable("no_refresh_token", portal_id=portal_id)
        refresh_token = decrypt(
            row.refresh_token_enc, member_id=row.member_id, column="refresh_token"
        )

        try:
            tokens = await exchange_refresh_token(
                refresh_token,
                server_endpoint=row.server_endpoint,
                expected_member_id=row.member_id,
                portal_id=portal_id,
                # §4.1: the limiter guards unauthenticated triggers. This one is our own
                # stored credential reacting to a real 401.
                rate_limit=False,
            )
        except (BitrixError, MemberIdMismatch) as exc:
            terminal = _terminal_status(exc)
            if terminal is not None:
                # §5.8: terminal states set `token_status` and are RAISED, never
                # swallowed. `tick()` only dispatches portals with `token_status='ok'`,
                # so this single column both stops the loop and drives the settings-page
                # banner; the sync runner owns `portal_sync.next_run_at`.
                await session.execute(
                    update(Portal).where(Portal.id == portal_id).values(token_status=terminal)
                )
                await _record_sync_blocked(session, portal_id, exc, terminal)
            failure = exc
        else:
            new_version = row.token_version + 1
            now = dt.datetime.now(tz=dt.UTC)
            await session.execute(
                update(Portal)
                .where(Portal.id == portal_id)
                .values(
                    access_token_enc=encrypt(
                        tokens.access_token, member_id=row.member_id, column="access_token"
                    ),
                    # Single-use and rotated: losing this write kills the chain (§5.8).
                    refresh_token_enc=encrypt(
                        tokens.refresh_token, member_id=row.member_id, column="refresh_token"
                    ),
                    token_expires_at=tokens.expires_at,
                    token_refreshed_at=now,
                    # §4.4 step 4: re-learning this is how a renamed portal or a newly
                    # connected custom domain is cured.
                    client_endpoint=tokens.client_endpoint,
                    token_version=new_version,
                )
            )
            refreshed = _Credential(token_version=new_version, access_token=tokens.access_token)

    # Outside the context so the terminal-state UPDATE above is committed before the
    # exception unwinds; raising inside `control_txn` would roll it back.
    if failure is not None:
        raise failure
    assert refreshed is not None  # exactly one of the two branches ran
    return refreshed


def _terminal_status(exc: BaseException) -> str | None:
    """§5.8 terminal mapping for a refresh failure, by exception TYPE (never a string).

    `MemberIdMismatch` is included and fails closed: the stored refresh token exchanged
    to a different portal, so nothing about this credential can be trusted until an
    administrator re-authorizes. `PortalDeleted` is deliberately absent - it demands
    `status='uninstalled'` + `purge_pending`, which is `services/portals.py`'s
    transition (§4.9 rule 3), not a `token_status` value.
    """
    if isinstance(exc, MemberIdMismatch):
        return "reauth_required"
    if isinstance(exc, BitrixError):
        return _TERMINAL_TOKEN_STATUS.get(type(exc))
    return None


async def _record_sync_blocked(
    session: AsyncSession, portal_id: int, exc: BaseException, token_status: str
) -> None:
    """`portal_events(sync_blocked)` in the same transaction as the status change (§5.8).

    The import is deferred because `services/portals.py` imports `TokenResponse` and
    `Identity` from this package; a module-level import would be a cycle. Keeping the one
    `portal_events` writer in `services/portals.py` is worth the deferred import.
    """
    from app.services.portals import record_event

    code = exc.code if isinstance(exc, BitrixError) else type(exc).__name__
    await record_event(
        session,
        portal_id,
        "sync_blocked",
        details={"reason": token_status, "error_code": code, "source": "oauth_refresh"},
    )


async def with_portal_token[T](portal_id: int, fn: Callable[[str], Awaitable[T]]) -> T:
    """Run `fn(access_token)` with the portal credential, refreshing at most once (§5.8).

    The five steps of §5.8, in order:

    1. Read the credential unlocked and remember `token_version`; if the stored token
       expires within 60 s (or there is none), go straight to the refresh.
    2. Call `fn`. Anything other than `expired_token` propagates untouched - `fn` owns
       its own errors and this wrapper must not turn a throttle or a permission answer
       into a refresh.
    3. On `expired_token`, take `SELECT ... FOR UPDATE` on the `portals` row. If
       `token_version` moved, another process already refreshed and we use ITS token.
    4. Otherwise refresh once and store the new pair.
    5. Retry `fn` **exactly once**. A second `expired_token` propagates.

    There is no loop anywhere in this function, by construction: a refresh loop against
    Bitrix24 is how an application gets blocked, and a token that is still rejected after
    a successful refresh means something the caller must see, not something to retry.
    """
    stored = await _load_credential(portal_id)
    token = stored.access_token
    version = stored.token_version

    if token is None or _expires_soon(stored.token_expires_at):
        credential = await _refresh_locked(portal_id, seen_version=version)
        token, version = credential.access_token, credential.token_version

    try:
        return await fn(token)
    except ExpiredToken:
        logger.info(
            "portal token expired; single-flight refresh",
            extra={"portal_id": portal_id, "token_version": version},
        )

    credential = await _refresh_locked(portal_id, seen_version=version)
    # Exactly one retry: a second ExpiredToken is raised to the caller (§5.8 step 5).
    return await fn(credential.access_token)
