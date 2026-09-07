"""`GET /me` and `POST /session/exchange` - the session's own two endpoints (§4.6, §4.7).

`GET /me` is the SPA's first call and the only endpoint a `denied` user may reach
(§4.7). It answers with the principal's own view of itself plus the portal's sync
summary, so the dashboard can render the "importing history" banner and the
`token_status` warning without a second round trip.

`POST /session/exchange` is what keeps §4.6's promise that "access is re-evaluated at
least hourly". A tab left open past the JWT's hour gets a 401, calls
`BX24.refreshAuth()` / `getAuth()` and posts the fresh access token together with the
dead JWT. The dead JWT is verified **ignoring expiry** for one purpose only - to learn
which portal, placement and CRM entity this tab belongs to - and then nothing about it
is trusted: the fresh token is re-proven at the stored `client_endpoint`, it must belong
to the same `sub`, the access level is re-decided from a live probe, and a CRM tab's
context is re-resolved with the *current* user's rights before any cached matching keys
are reused (§4.8).

Two things this module is careful about, both from §4.6's "bodies of `/session/exchange`
and `/portal/reauthorize` are excluded from all exception logging":

* the body is parsed by hand rather than through a Pydantic model. FastAPI's
  `RequestValidationError` response embeds the offending `input` value, which for this
  endpoint is a live Bitrix24 access token - a validation slip would echo it straight
  back out and into whatever records the response;
* every log line here carries ids and error *types*, never a field of the body and never
  the minted JWT (§4.6: the token is never logged, never a header, never a query
  parameter - the response body is the only place it appears).

Everything answered here is a machine code; the SPA translates (§8).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.bitrix.crm import deal_context_commands, entity_activity_commands
from app.bitrix.errors import (
    BitrixError,
    ExpiredToken,
    InsufficientScope,
    InvalidCredentials,
    NoAuthFound,
    OperationTimeLimit,
    QueryLimitExceeded,
    TransportError,
    UserAccessError,
)
from app.bitrix.identity import resolve_identity_with
from app.db.models import Portal, PortalSync
from app.db.session import control_txn
from app.i18n import resolve_locale
from app.logging import get_logger, get_request_id
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
)
from app.security.session_token import TokenError, issue_session, verify_session
from app.services.access import decide_access
from app.services.crm_context import resolve_crm_context, store_crm_context

__all__ = ["router"]

router = APIRouter(route_class=PrincipalErrorRoute)

_log = get_logger(__name__)

#: §4.6: `exp = iat + min(AUTH_EXPIRES, 3600)`. The exchange body carries no
#: `AUTH_EXPIRES` (BX24.getAuth() hands the SPA a token, not the iframe POST), so the
#: cap is the whole rule here - and it is the cap that makes the re-evaluation hourly.
_SESSION_TTL_SECONDS: Final[int] = 3600

#: A generous ceiling for `{access_token, jwt}`; §4.2 caps Bitrix24-facing bodies at
#: 64 KB and this one is two opaque strings. Read before parsing so a large body is
#: refused without being decoded.
_MAX_BODY_BYTES: Final[int] = 8 * 1024

#: §4.2's allowlist shape for `AUTH_ID` / `access_token`, applied by length and alphabet
#: only - the value is proven by Bitrix24, not by us.
_TOKEN_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512

#: JWTs are three base64url segments; 8 KB of them is already absurd.
_JWT_MAX: Final[int] = 4096

#: The per-user probe of §4.4 step 4. `start:0` and `ORDER:DESC` keep it to one page of
#: the newest rows: the answer we want is the error code, not the data.
_STATISTIC_METHOD: Final[str] = "voximplant.statistic.get"
_PROBE: Final[str] = "probe"

#: `backfill_status` values that mean "history is still coming" (§5.2), i.e. the states
#: in which the dashboard shows the import banner instead of "no calls in this period".
_IMPORTING: Final[frozenset[str]] = frozenset({"pending", "head", "running"})

#: §4.7's mandated no-access text, as the message key the SPA already renders on
#: `/state/denied` (§8: one message source, the server never sends the sentence).
_DENIED_COPY_KEY: Final[str] = "state.denied.body"
_DENIED_CODE: Final[str] = "no_stats_permission"

#: The §4.7 level that may not read call data, and the §3 `portals.status` that may.
_DENIED_LEVEL: Final[str] = "denied"
_ACTIVE: Final[str] = "active"

#: How a §4.4 step-5 state kind is answered over JSON. The kinds are the SPA's own
#: `/state/[kind]` pages, so the code doubles as the page to render. `retry` is the only
#: one that is worth trying again, hence the only 5xx and the only `Retry-After`.
_STATE_STATUS: Final[dict[str, int]] = {
    "retry": 503,
    "scope": 403,
    "method_missing": 403,
    "crm_no_access": 403,
    "denied": 403,
}
_RETRY_AFTER_SECONDS: Final[int] = 10


def _correlation_id() -> uuid.UUID:
    """Reuse the request id as `rest_log.correlation_id` (§6), as the handlers do."""
    raw = get_request_id()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return uuid.uuid4()


def _isoformat(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _error(code: str, status: int, **extra: Any) -> JSONResponse:
    """One machine code, optionally with fields the SPA needs to render (§8)."""
    body: dict[str, Any] = {"code": code, **extra}
    headers = {"Retry-After": str(_RETRY_AFTER_SECONDS)} if status == 503 else None
    return JSONResponse(body, status_code=status, headers=headers)


def _state_error(state: str) -> JSONResponse:
    """Answer a §4.4 step-5 state decision as JSON."""
    if state == _DENIED_LEVEL:
        return _error(_DENIED_CODE, 403, copy_key=_DENIED_COPY_KEY)
    return _error(state, _STATE_STATUS.get(state, 403))


# --- GET /me -------------------------------------------------------------------------


@router.get("/me")
async def me(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """The principal's own view of this session (§4.7).

    Deliberately NOT behind `require_data_access`: §4.7 says "`acc='denied'`: only
    `GET /me` answers". A denied user still needs a working endpoint - it is what tells
    the SPA to render the mandated "ask your administrator" text instead of an empty
    dashboard - and this endpoint touches no call data, so there is nothing to withhold.

    The sync summary comes from the control plane (`portals` + `portal_sync`, no RLS,
    §3) and carries no customer data: how far the backfill got, whether it is still
    running, and `token_status` so the SPA can show the banner that explains a stalled
    import (§5.8). `last_error_code` is admin-only: it is support vocabulary
    (`QUERY_LIMIT_EXCEEDED`, …) that a regular employee cannot act on.
    """
    async with control_txn() as session:
        row = (
            await session.execute(
                select(Portal, PortalSync)
                .outerjoin(PortalSync, PortalSync.portal_id == Portal.id)
                .where(Portal.id == principal.portal_id)
            )
        ).first()

    if row is None:
        # The portal disappeared between `get_principal` and this query: same answer as
        # any other revoked session (§4.6).
        raise PrincipalError("portal_inactive", 401)

    portal, sync = row
    body: dict[str, Any] = {
        "user_id": principal.user_id,
        "is_admin": principal.is_admin,
        "access": principal.access,
        "locale": resolve_locale(principal.lang),
        "timezone": principal.timezone,
        "placement": principal.placement,
        "entity": principal.entity,
        "issued_at": principal.issued_at,
        "no_access": (
            {"code": _DENIED_CODE, "copy_key": _DENIED_COPY_KEY}
            if principal.access == _DENIED_LEVEL
            else None
        ),
        "sync": {
            "token_status": portal.token_status,
            "backfill_status": sync.backfill_status if sync else "pending",
            "backfill_done": sync.backfill_done if sync else 0,
            "backfill_total": sync.backfill_total if sync else None,
            # The §4.11 banner condition: history is still being imported, so an empty
            # or short period is "not there yet", not "no calls".
            "importing": (sync.backfill_status if sync else "pending") in _IMPORTING,
            "last_incremental_at": _isoformat(sync.last_incremental_at if sync else None),
        },
    }
    if principal.is_admin:
        body["sync"]["last_error_code"] = sync.last_error_code if sync else None
    return JSONResponse(body)


# --- POST /session/exchange ----------------------------------------------------------


def _read_token(payload: dict[str, Any], field: str, maximum: int) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum:
        return None
    return value


async def _parse_body(request: Request) -> tuple[str, str] | None:
    """`{access_token, jwt}` or None - and never the value in an error (§4.6)."""
    raw = await request.body()
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    access_token = _read_token(payload, "access_token", _TOKEN_MAX)
    old_jwt = _read_token(payload, "jwt", _JWT_MAX)
    if access_token is None or old_jwt is None:
        return None
    if len(access_token) < _TOKEN_MIN or not set(access_token) <= _TOKEN_CHARS:
        return None
    return access_token, old_jwt


def _crm_commands(entity_type: str, entity_id: int) -> list[tuple[str, str, dict[str, Any]]]:
    """The CRM half of the open-time batch (§4.4 step 4).

    A deal needs its own related entities (contacts, companies) because telephony rows
    are documented to carry CONTACT / COMPANY / LEAD and not the deal; every other
    entity type only needs its call activities.
    """
    if entity_type == "DEAL":
        return list(deal_context_commands(entity_id))
    return list(entity_activity_commands(entity_type, entity_id))


@router.post("/session/exchange")
async def exchange(request: Request) -> JSONResponse:
    """Re-prove a session against Bitrix24 and mint a new JWT (§4.6).

    No `get_principal` here on purpose: the whole point is that the presented JWT has
    expired, so the dependency would reject it. The old token is still verified - by
    signature, `typ` and claim shape, with expiry ignored - because it is the only thing
    telling us which portal (`pid`), placement (`plc`) and CRM entity (`ent`) this tab
    belongs to. Everything that could be *used* is re-derived from Bitrix24's answer to
    the fresh access token: identity, admin flag, access level, CRM context.

    Order of proof, and why:

    1. old JWT verified ignoring expiry -> `pid`, `mid`, `sub`, `plc`, `ent`;
    2. `portals` row loaded and re-checked (`member_id`, `status='active'`) - the same
       revocation gate `get_principal` applies, because this path mints a new hour;
    3. one batch at the **stored** `client_endpoint` with the fresh token: `user.current`
       + `user.admin` (+ the statistics probe, + the CRM commands). Never at `DOMAIN`
       (§4.1), and never with a refreshed *portal* token - a user's token is only ever
       presented as the user;
    4. the resolved user must be the same `sub`. A different user with a valid token is
       not an error to smooth over: it means this JWT and this access token belong to
       two different people, and the only safe answer is "no session";
    5. access re-decided by `services.access.decide_access`, CRM context re-resolved
       with this user's own rights (§4.8), then and only then a new JWT.

    A transport failure is answered `retry` rather than cured: §4.4's refresh-and-retry
    for a renamed portal needs the `REFRESH_ID` from an iframe POST, which this body
    does not carry (and must not).
    """
    parsed = await _parse_body(request)
    if parsed is None:
        return _error("bad_request", 400)
    access_token, old_jwt = parsed

    try:
        claims = verify_session(old_jwt, ignore_exp=True)
    except TokenError:
        _log.info("exchange: presented session token rejected")
        return _error("invalid_session", 401)

    async with control_txn() as session:
        portal = (
            await session.execute(select(Portal).where(Portal.id == claims.pid))
        ).scalar_one_or_none()

    if portal is None or portal.member_id != claims.mid or portal.status != _ACTIVE:
        _log.info("exchange: portal not active", extra={"portal_id": claims.pid})
        return _error("portal_inactive", 401)

    entity = claims.ent if isinstance(claims.ent, dict) else None
    crm_entity: tuple[str, int] | None = None
    crm_commands: list[tuple[str, str, dict[str, Any]]] = []
    if entity is not None:
        entity_type = entity.get("t")
        entity_id = entity.get("id")
        if (
            not isinstance(entity_type, str)
            or not entity_type
            # bool is an int subclass; `{"id": true}` is malformed, not entity 1.
            or isinstance(entity_id, bool)
            or not isinstance(entity_id, int)
            or entity_id <= 0
        ):
            # Our own claim, malformed: refuse rather than silently downgrade a CRM tab
            # to a portal-wide session.
            _log.warning("exchange: malformed ent claim", extra={"portal_id": claims.pid})
            return _error("invalid_session", 401)
        try:
            crm_commands = _crm_commands(entity_type, entity_id)
        except ValueError:
            # `crm.py` refuses an entity type outside the §3 CHECK / §4.2 placement
            # allowlist. Only reachable with a hand-built claim set: fail closed.
            _log.warning("exchange: unusable ent claim", extra={"portal_id": claims.pid})
            return _error("invalid_session", 401)
        crm_entity = (entity_type, entity_id)

    statistic_get_available = portal.capabilities.get("statistic_get") is not False
    commands: list[tuple[str, str, dict[str, Any]]] = []
    if statistic_get_available:
        # Unlike §4.4 step 4, the probe is NOT skipped for administrators here. There the
        # opener's admin flag is unknown until the batch answers; here the only admin
        # flag we have before the call is the expired token's `adm` claim - exactly the
        # thing this endpoint exists to re-check. One extra command once an hour per open
        # tab is a cheaper way to be right than trusting a stale claim.
        commands.append(
            (
                _PROBE,
                _STATISTIC_METHOD,
                {
                    # The filter uses the claimed `sub`; step 4 below refuses the whole
                    # exchange unless the token really belongs to that user, so the probe
                    # can never describe somebody else's rights.
                    "FILTER": {"PORTAL_USER_ID": claims.sub},
                    "SORT": "ID",
                    "ORDER": "DESC",
                    "start": 0,
                },
            )
        )
    commands.extend(crm_commands)

    try:
        identity, batch = await resolve_identity_with(
            endpoint=portal.client_endpoint,
            access_token=access_token,
            extra_commands=commands,
            portal_id=portal.id,
            member_id=portal.member_id,
            correlation_id=_correlation_id(),
        )
    except (ExpiredToken, NoAuthFound, InvalidCredentials, UserAccessError):
        # §4.4 step 4: the user's token is not valid at this portal. We never refresh a
        # user's token - the SPA reopens the app and Bitrix24 issues a new one.
        _log.info("exchange: user token refused", extra={"portal_id": portal.id})
        return _error("reopen_required", 401)
    except InsufficientScope:
        return _error("scope", 403)
    except (QueryLimitExceeded, OperationTimeLimit, TransportError):
        return _error("retry", 503)
    except BitrixError as exc:
        _log.warning(
            "exchange: batch failed",
            extra={"portal_id": portal.id, "error": type(exc).__name__},
        )
        return _error("retry", 503)

    if identity.user_id != claims.sub:
        _log.warning(
            "exchange: token belongs to another user",
            extra={"portal_id": portal.id, "user_id": claims.sub},
        )
        return _error("invalid_session", 401)

    decision = decide_access(
        is_admin=identity.is_admin,
        probe_error=batch.error(_PROBE),
        probe_ran=statistic_get_available,
        statistic_get_available=statistic_get_available,
    )
    if decision.state is not None:
        _log.info(
            "exchange: access state",
            extra={
                "portal_id": portal.id,
                "user_id": identity.user_id,
                "state": decision.state,
            },
        )
        return _state_error(decision.state)
    if decision.level == _DENIED_LEVEL:
        # §4.4 mints no JWT for a denied user; the SPA renders the mandated text from the
        # copy key, exactly as the server-rendered `/state/denied` page does.
        return _error(_DENIED_CODE, 403, copy_key=_DENIED_COPY_KEY)

    if crm_entity is not None:
        # §4.4 step 5: any CRM command in error means this user cannot see the entity, so
        # no entity session is minted - a cached context must never be served to someone
        # who lacks the rights that produced it.
        if any(batch.error(key) is not None for key, _, _ in crm_commands):
            return _error("crm_no_access", 403)
        context = await resolve_crm_context(
            batch, entity_type=crm_entity[0], entity_id=crm_entity[1]
        )
        if context is None:
            return _error("crm_no_access", 403)
        # Re-cached under THIS user's id and timestamp, which is what makes the §4.8
        # freshness check (`resolved_at >= JWT.iat`) pass for the token minted below.
        await store_crm_context(portal.id, context, user_id=identity.user_id)

    timezone = identity.timezone or claims.tz
    token = issue_session(
        pid=portal.id,
        mid=portal.member_id,
        sub=identity.user_id,
        adm=identity.is_admin,
        acc=decision.level,
        tz=timezone,
        lang=claims.lang,
        plc=claims.plc,
        ent=entity,
        ttl_seconds=_SESSION_TTL_SECONDS,
    )
    _log.info(
        "exchange: session renewed",
        extra={
            "portal_id": portal.id,
            "user_id": identity.user_id,
            "access": decision.level,
        },
    )
    # The JWT rides in the body and nowhere else: not a header, not a query parameter,
    # not a log line (§4.6). The response is `no-store` (middleware, §4.10).
    return JSONResponse(
        {
            "jwt": token,
            "access": decision.level,
            "is_admin": identity.is_admin,
            "timezone": timezone,
            "locale": resolve_locale(claims.lang),
            "placement": claims.plc,
            "entity": entity,
            "expires_in": _SESSION_TTL_SECONDS,
        }
    )
