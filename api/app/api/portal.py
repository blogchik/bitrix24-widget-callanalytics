"""The settings page's two endpoints: what sync is doing, and how to re-seed the token.

Both are administrator-only (§4.5: `/settings/` lands non-admins on the "administrators
only" state, and these are the JSON calls that page makes).

* `GET /portal/sync-status` answers what `/me` deliberately does not. `/me` is every
  user's first call and carries the few sync facts the dashboard banner needs;
  everything here is support vocabulary - cursors, lease state, `capabilities`,
  quarantined rows, the token's owner and age, the last error text - which is useful to
  an administrator and meaningless to a salesperson.
* `POST /portal/reauthorize` is §4.5's "Re-authorize" button. The body is the pair from
  `BX24.getAuth()` and it is proven twice before anything is written: a refresh exchange
  whose response `member_id` must equal the portal's (§4.1: `member_id` proves nothing
  on its own - an OAuth exchange is what proves it), and a `user.admin` proof for the
  resulting token at the endpoint that same response named. Only then does
  `store_portal_credential` run, which is the one function allowed to write those
  columns (§4.1 credential invariant).

Both bodies are excluded from exception logging (§4.6). In practice that means the one
body is parsed by hand rather than through a Pydantic model - FastAPI's
`RequestValidationError` renders the offending `input`, and here that input is a live
Bitrix24 refresh token - and that no log line in this module names a field of it.

Nothing here reads `calls`: this is control-plane data (`portals`, `portal_sync`, §3),
which is why every statement below runs in `control_txn` and none of it needs RLS.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError, InvalidGrant, TransportError
from app.bitrix.identity import NotAnAdministrator, verify_admin_token
from app.bitrix.oauth import (
    CredentialUnavailable,
    MemberIdMismatch,
    OAuthRateLimited,
    exchange_refresh_token,
    with_portal_token,
)
from app.bitrix.placements import bind_all, get_bound_placements
from app.config import settings
from app.db.models import Portal, PortalSync
from app.db.session import control_txn
from app.logging import get_logger, get_request_id
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
)
from app.services.portals import (
    decrypt_portal_token,
    record_event,
    record_placements,
    store_portal_credential,
)

__all__ = ["router"]

router = APIRouter(route_class=PrincipalErrorRoute)

_log = get_logger(__name__)

#: §3 `portals_status_chk`.
_ACTIVE: Final[str] = "active"

#: §3 `portals_token_status_chk`: the healthy credential state, and the one a re-seed
#: restores. A state enum, not a credential - hence the linter exemption.
_TOKEN_STATUS_OK: Final[str] = "ok"  # noqa: S105

#: §5.2: the states in which history is still arriving.
_IMPORTING: Final[frozenset[str]] = frozenset({"pending", "head", "running"})

#: §5.8: the refresh chain dies 180 days after `token_refreshed_at`. An admin open
#: re-seeds automatically from `TOKEN_RESEED_AFTER_DAYS` (120); the settings page shows a
#: "re-authorize soon" banner from 150, which is the number below.
_REAUTH_BANNER_DAYS: Final[int] = 150

#: §4.6: two opaque strings. Read before parsing so a large body is refused undecoded.
_MAX_BODY_BYTES: Final[int] = 8 * 1024

#: §4.2's allowlist shape for `AUTH_ID` / `REFRESH_ID`, by length and alphabet only - the
#: value is proven by Bitrix24, not by us.
_TOKEN_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512

#: §3 `portals.app_status` is 4 characters wide.
_APP_STATUS_MAX: Final[int] = 4

_RETRY_AFTER_SECONDS: Final[int] = 60


def _error(code: str, status: int, **extra: Any) -> JSONResponse:
    """One machine code, optionally with what the SPA needs to render it (§8)."""
    headers = {"Retry-After": str(_RETRY_AFTER_SECONDS)} if status in (429, 503) else None
    return JSONResponse({"code": code, **extra}, status_code=status, headers=headers)


def _isoformat(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _age_days(value: dt.datetime | None) -> int | None:
    if value is None:
        return None
    moment = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(tz=dt.UTC) - moment).days


def _correlation_id() -> uuid.UUID:
    """Reuse the request id as `rest_log.correlation_id` (§6), as the handlers do."""
    raw = get_request_id()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return uuid.uuid4()


def _client_ip(request: Request) -> str | None:
    """The peer address for the §4.1 per-IP exchange limiter.

    uvicorn runs with `--proxy-headers`, so behind Caddy this is the real client.
    """
    return request.client.host if request.client else None


async def _require_admin(principal: Principal) -> None:
    """§4.5: the settings page is administrators only.

    Not `require_data_access`: neither endpoint reads call data, and an administrator is
    never `denied` anyway (§4.4 step 5 maps `admin=true` straight to `all`). The check
    that matters here is the `adm` claim, which was minted from a live `user.admin`
    answer and is re-decided at least hourly by the session exchange (§4.6).
    """
    if not principal.is_admin:
        _log.info(
            "portal: non-administrator asked for a settings endpoint",
            extra={"portal_id": principal.portal_id, "user_id": principal.user_id},
        )
        raise PrincipalError("admin_only", 403)


async def _load(portal_id: int) -> tuple[Portal, PortalSync | None]:
    async with control_txn() as session:
        row = (
            await session.execute(
                select(Portal, PortalSync)
                .outerjoin(PortalSync, PortalSync.portal_id == Portal.id)
                .where(Portal.id == portal_id)
            )
        ).first()
    if row is None:
        # Gone between `get_principal` and this query: the same answer as any other
        # revoked session (§4.6).
        raise PrincipalError("portal_inactive", 401)
    portal, sync = row
    return portal, sync


# --- GET /portal/sync-status ---------------------------------------------------------


@router.get("/portal/sync-status")
async def sync_status(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """Everything the settings page shows beyond `/me` (§4.5, §5).

    Four groups, each answering a question an administrator actually asks when the app
    looks wrong:

    * **backfill** - "is history still coming?" (§5.2's `pending -> head -> running ->
      done|failed`, with the counter the §4.11 banner renders);
    * **token** - "whose token is this and how long will it last?" `token_user_id` is the
      administrator whose credential the worker syncs with (§3), `refreshed_at` starts
      the 180-day chain, and the two flags below turn that into the banner §5.8
      specifies rather than making the page re-derive the arithmetic;
    * **sync** - cursors, cadence, throttle and failure counters, plus
      `quarantined_rows` (§3 `rejected_rows`: "a support signal, never a blocker") and
      the last error. `last_error_text` appears here and nowhere else - §4.5 puts it on
      the settings page, and `/me` deliberately withholds even the code from non-admins;
    * **placements / capabilities** - the §4.3 step 5 bind results that the "Re-bind"
      button acts on, and the probe results (`statistic_get`, `operating_limit_s`) that
      explain a `method_missing` state.

    Read-only, control-plane only, one query.
    """
    await _require_admin(principal)
    portal, sync = await _load(principal.portal_id)

    token_age = _age_days(portal.token_refreshed_at)
    backfill_status = sync.backfill_status if sync else "pending"

    return JSONResponse(
        {
            "portal": {
                # Display data only (§4.1: DOMAIN is never a lookup key or a REST base).
                "domain": portal.domain,
                "status": portal.status,
                "lang": portal.lang,
                "timezone": portal.timezone,
                "app_status": portal.app_status,
                "app_version": portal.app_version,
                "installed_flag": portal.installed_flag,
                "installed_at": _isoformat(portal.installed_at),
                "last_admin_opened_at": _isoformat(portal.last_admin_opened_at),
            },
            "backfill": {
                "status": backfill_status,
                "done": sync.backfill_done if sync else 0,
                "total": sync.backfill_total if sync else None,
                "started_at": _isoformat(sync.backfill_started_at if sync else None),
                "finished_at": _isoformat(sync.backfill_finished_at if sync else None),
                "importing": backfill_status in _IMPORTING,
            },
            "token": {
                "status": portal.token_status,
                # The administrator the worker syncs as (§3): the name to ask when the
                # cache stops updating because that person was dismissed.
                "owner_user_id": portal.token_user_id,
                "refreshed_at": _isoformat(portal.token_refreshed_at),
                "age_days": token_age,
                "expires_at": _isoformat(portal.token_expires_at),
                "admin_verified_at": _isoformat(portal.token_admin_verified_at),
                # §5.8: an admin open re-seeds automatically from 120 days...
                "reseed_due": token_age is not None
                and token_age >= settings.token_reseed_after_days,
                # ...and from 150 the page says so out loud, because a chain that dies at
                # 180 with nobody watching is a silent stop.
                "reauthorize_soon": token_age is not None and token_age >= _REAUTH_BANNER_DAYS,
            },
            "sync": {
                "high_id": sync.high_id if sync else 0,
                "low_id": sync.low_id if sync else None,
                "rescan_from_id": sync.rescan_from_id if sync else None,
                "next_run_at": _isoformat(sync.next_run_at if sync else None),
                "last_incremental_at": _isoformat(sync.last_incremental_at if sync else None),
                "last_rescan_at": _isoformat(sync.last_rescan_at if sync else None),
                "last_recheck_at": _isoformat(sync.last_recheck_at if sync else None),
                "last_employees_at": _isoformat(sync.last_employees_at if sync else None),
                "batch_pages": sync.batch_pages if sync else None,
                "throttle_hits": sync.throttle_hits if sync else 0,
                "consecutive_failures": sync.consecutive_failures if sync else 0,
                # §3: rows the parser quarantined. Never a blocker, always worth seeing.
                "quarantined_rows": sync.rejected_rows if sync else 0,
                "running": bool(sync and sync.run_started_at is not None),
            },
            "last_error": {
                "code": sync.last_error_code if sync else None,
                "text": sync.last_error_text if sync else None,
                "at": _isoformat(sync.last_error_at if sync else None),
            },
            # §4.3 step 5 results, keyed by placement code, for the "Re-bind" button.
            "placements": portal.placements or {},
            "capabilities": portal.capabilities or {},
            "recording_mode": settings.recording_mode,
        }
    )


# --- POST /portal/reauthorize --------------------------------------------------------


def _read_token(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not _TOKEN_MIN <= len(token) <= _TOKEN_MAX or not set(token) <= _TOKEN_CHARS:
        return None
    return token


async def _parse_body(request: Request) -> str | None:
    """The `refresh_token` of the `BX24.getAuth()` pair, or None (§4.5, §4.6).

    Hand-parsed, and no branch of it ever puts a field of the body into an exception, a
    log line or a response: this body carries a live refresh token, which is the one
    credential in this system that is worth stealing for 180 days.

    `access_token` is accepted and validated for shape but not used: the credential we
    are about to store is the one the exchange returns, and §4.1 wants the `user.admin`
    proof run against *that* token at *that* endpoint - proving the old one would prove
    the wrong thing.
    """
    raw = await request.body()
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _read_token(payload, "refresh_token")


@router.post("/portal/reauthorize")
async def reauthorize(
    request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """§4.5's "Re-authorize": re-seed the portal credential from the admin's own pair.

    The ladder, and why each rung is there:

    1. **administrator** (§4.5). The stored token is the whole portal's sync credential;
       a regular employee's token would silently cache a filtered subset of the calls.
    2. **a refresh exchange whose `member_id` matches** (§4.1). `member_id` proves
       nothing on its own, so the posted pair is proven by spending it: the response's
       `member_id`, `client_endpoint`, `scope` and `status` are authoritative, and a
       mismatch means this pair belongs to a different portal - refused, nothing written.
       The exchange is rate-limited per `member_id` and per source IP by
       `bitrix/oauth.py` (§4.1, `OAUTH_EXCHANGE_LIMIT`), which is this endpoint's rate
       limit too: a button that spends refresh tokens must not be free to hold down.
    3. **`user.admin` for the resulting token**, at the `client_endpoint` that same
       response named (§4.1 endpoint invariant - never `DOMAIN`).
    4. **the token must belong to the caller.** The pair comes from this viewer's
       `BX24.getAuth()`, so a pair that resolves to somebody else means the body and the
       session disagree about who is asking; `/session/exchange` refuses the same
       discovery for the same reason.
    5. `store_portal_credential` - the only writer of the credential columns (§4.1), which
       re-checks the admin proof and the tenant key itself before writing.

    On success the portal is unblocked: `token_status='ok'`, `token_version+1` and
    `next_run_at=now()` come from `store_portal_credential`, so the worker picks the
    portal up on the next tick (≤ 15 s) rather than waiting out a parked `infinity`.

    A failure at any rung **leaves the existing credential untouched** (§4.4 step 6) and
    is logged as a decision, never as a payload.
    """
    await _require_admin(principal)

    refresh_token = await _parse_body(request)
    if refresh_token is None:
        # No field name, no value, no length: the body is not describable in an error.
        return _error("bad_request", 400)

    portal, _ = await _load(principal.portal_id)
    if portal.status != _ACTIVE:
        return _error("portal_inactive", 401)
    previous_status = portal.token_status

    try:
        tokens = await exchange_refresh_token(
            refresh_token,
            server_endpoint=portal.server_endpoint,
            expected_member_id=portal.member_id,
            portal_id=portal.id,
            source_ip=_client_ip(request),
            correlation_id=_correlation_id(),
        )
    except OAuthRateLimited:
        _log.warning("portal: reauthorize rate limited", extra={"portal_id": portal.id})
        return _error("rate_limited", 429)
    except MemberIdMismatch:
        # The pair proved a different portal. §4.1 treats this as the failure of the
        # comparison the whole trust model rests on: refuse, write nothing.
        _log.warning("portal: reauthorize member_id mismatch", extra={"portal_id": portal.id})
        return _error("member_id_mismatch", 400)
    except (InvalidGrant, CredentialUnavailable):
        # A spent or dead refresh token: the admin must reopen the app so Bitrix24 hands
        # the iframe a fresh pair.
        _log.info("portal: reauthorize pair rejected", extra={"portal_id": portal.id})
        return _error("reopen_required", 400)
    except TransportError:
        return _error("retry", 503)
    except BitrixError as exc:
        _log.warning(
            "portal: reauthorize exchange failed",
            extra={"portal_id": portal.id, "error": type(exc).__name__},
        )
        return _error("retry", 503)

    try:
        admin = await verify_admin_token(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            portal_id=portal.id,
            member_id=portal.member_id,
            correlation_id=_correlation_id(),
        )
    except NotAnAdministrator:
        _log.info("portal: reauthorize by a non-administrator", extra={"portal_id": portal.id})
        return _error("admin_only", 403)
    except BitrixError as exc:
        _log.warning(
            "portal: reauthorize admin proof failed",
            extra={"portal_id": portal.id, "error": type(exc).__name__},
        )
        return _error("retry", 503)

    if admin.user_id != principal.user_id:
        _log.warning(
            "portal: reauthorize pair belongs to another user",
            extra={"portal_id": portal.id, "user_id": principal.user_id},
        )
        return _error("invalid_session", 401)

    # The install-time probe results are kept (§4.3 step 3 wrote them and nothing here
    # re-ran `method.get`); only the freshness stamp and the provenance are added, so a
    # `statistic_get=false` portal does not silently become "ok" on a re-authorize.
    capabilities: dict[str, Any] = {
        **(portal.capabilities or {}),
        "scope": tokens.scope,
        "checked_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "reauthorized": True,
    }
    statistic_get = capabilities.get("statistic_get") is not False
    token_status = _TOKEN_STATUS_OK if statistic_get else "method_missing"

    async with control_txn() as session:
        outcome = await store_portal_credential(
            session,
            member_id=portal.member_id,
            tokens=tokens,
            admin=admin,
            # Not touched from here: `application_token` changes on a version update and
            # §4.4 step 6 writes it from a verified admin *open*, which carries one.
            application_token=None,
            # Display fields keep their stored values - this request is a JSON call from
            # the SPA and carries no `DOMAIN` / `PROTOCOL` of its own (§4.1: they would
            # be untrusted input anyway).
            domain=portal.domain,
            protocol_https=portal.protocol_https,
            lang=portal.lang,
            app_status=str(portal.app_status)[:_APP_STATUS_MAX] if portal.app_status else tokens.status,
            capabilities=capabilities,
            token_status=token_status,
        )
        await record_event(
            session,
            outcome.portal_id,
            "token_reseeded",
            user_id=admin.user_id,
            details={"source": "reauthorize", "token_status": token_status},
        )
        if previous_status != _TOKEN_STATUS_OK and token_status == _TOKEN_STATUS_OK:
            # §5.8: the counterpart of `sync_blocked`, so the audit trail shows who
            # unblocked a parked portal and when.
            await record_event(
                session,
                outcome.portal_id,
                "sync_unblocked",
                user_id=admin.user_id,
                details={"previous_token_status": previous_status},
            )

    _log.info(
        "portal: credential re-seeded",
        extra={"portal_id": outcome.portal_id, "user_id": admin.user_id},
    )
    return JSONResponse(
        {
            "token_status": token_status,
            "token_user_id": admin.user_id,
            "statistic_get": statistic_get,
            # `next_run_at=now()` is part of the credential write, so the settings page
            # can honestly say the worker will pick this up on its next tick.
            "sync_resumes": True,
        }
    )


@router.post("/portal/rebind-placements")
async def rebind_placements(
    principal: Principal = Depends(get_principal),
) -> JSONResponse:
    """Re-run the §4.3 step 5 binds for a portal whose CRM tabs are missing.

    A bind can fail at install time without failing the install: §4.3 step 5 is explicit
    that reaching `install.html` and running `BX24.installFinish()` matters more than a
    placement, because without `installFinish` the app stays "not installed" and no widget
    appears at all. The cost is that a portal can end up installed with one or more tabs
    unbound, and the only signal is `portals.placements`. This endpoint is the repair the
    settings page offers, so an administrator can fix it without reinstalling.

    Uses the stored portal credential, which `store_portal_credential` guarantees belongs
    to a proven administrator (§4.1) - `placement.bind` requires admin rights, so a
    non-admin token would fail here anyway.
    """
    await _require_admin(principal)
    correlation_id = uuid.UUID(get_request_id() or uuid.uuid4().hex)

    portal, _ = await _load(principal.portal_id)
    access_token = await decrypt_portal_token(portal, "access_token")
    if access_token is None:
        return JSONResponse({"code": "reauthorize_required"}, status_code=409)

    handler_url = f"{settings.app_base_url.rstrip('/')}/app/"
    events_url = f"{settings.app_base_url.rstrip('/')}/events/"

    async def _bind(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        async with BitrixClient(
            endpoint=portal.client_endpoint,
            access_token=token,
            portal_id=portal.id,
            member_id=portal.member_id,
            token_user_id=portal.token_user_id,
            correlation_id=correlation_id,
        ) as client:
            bound = await get_bound_placements(client)
            return await bind_all(
                client, handler_url=handler_url, events_url=events_url, existing=bound
            )

    try:
        placements, event_bind = await with_portal_token(portal.id, _bind)
    except BitrixError as exc:
        _log.warning(
            "rebind: bind batch failed",
            extra={"portal_id": portal.id, "error_code": exc.code},
        )
        return JSONResponse({"code": "rebind_failed", "error": exc.code}, status_code=502)

    async with control_txn() as session:
        await record_placements(
            session,
            portal.id,
            placements=placements,
            capabilities_patch={"event_bind": event_bind},
        )
        await record_event(
            session,
            portal.id,
            "placements_rebound",
            user_id=principal.user_id,
            details={"placements": {k: v.get("ok") for k, v in placements.items()}},
        )

    return JSONResponse({"placements": placements, "event_bind": event_bind})
