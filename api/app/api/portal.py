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
from app.db.models import CrmLane, Employee, Portal, PortalSync
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger, get_request_id
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
)
from app.services import crm_grants
from app.services.portals import (
    decrypt_portal_token,
    dismiss_crm_notice,
    record_event,
    record_placements,
    set_crm_analytics,
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


def crm_block(portal: Portal) -> dict[str, Any]:
    """Where CRM analytics stands for this portal - shared by `sync-status` and the switch."""
    return {
        "analytics_enabled": portal.crm_opt_out_at is None,
        "mode": portal.crm_mode,
        "opted_out_at": _isoformat(portal.crm_opt_out_at),
        "purge_pending": bool(portal.crm_purge_pending),
    }


async def _crm_lanes(portal_id: int) -> list[dict[str, Any]]:
    """The mirror's lanes as the settings page shows them: progress and why one waits."""
    async with control_txn() as session:
        lanes = (
            await session.execute(
                select(CrmLane).where(CrmLane.portal_id == portal_id).order_by(CrmLane.lane)
            )
        ).scalars()
        return [
            {
                "lane": lane.lane,
                "status": lane.status,
                "progress_done": lane.progress_done,
                "progress_total": lane.progress_total,
                "last_clean_at": _isoformat(lane.last_clean_at),
                "paused_until": _isoformat(lane.paused_until),
                "block_reason": lane.block_reason,
                "last_error_code": lane.last_error_code,
            }
            for lane in lanes
        ]


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
            # §4.14: the administrator's switch and the mirror's progress, lane by lane.
            "crm": {**crm_block(portal), "lanes": await _crm_lanes(portal.id)},
        }
    )


# --- POST /portal/crm-analytics, POST /portal/crm-notice/dismiss (D-7) ---------------


async def _read_enabled(request: Request) -> bool | None:
    """`{"enabled": true|false}` and nothing else, or None."""
    raw = await request.body()
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("enabled")
    return value if isinstance(value, bool) else None


@router.post("/portal/crm-analytics")
async def crm_analytics(
    request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """The administrator's CRM analytics switch (D-7, docs/crm-mirror-notice.md sections 2-4).

    Off deletes this portal's CRM mirror (the worker's verified CRM purge, within a tick or two)
    and closes the Deals and Sources reports, live reads included; calls are untouched. On
    starts storage again at once. Administrators only, like everything on the settings page.
    """
    await _require_admin(principal)
    enabled = await _read_enabled(request)
    if enabled is None:
        return _error("bad_request", 400)
    portal, _ = await _load(principal.portal_id)
    if portal.status != _ACTIVE:
        return _error("portal_inactive", 401)

    async with control_txn() as session:
        changed = await set_crm_analytics(
            session, portal.id, enabled=enabled, user_id=principal.user_id
        )
    _log.info(
        "portal: CRM analytics switched",
        # A constant per branch rather than the parsed body value: nothing the caller sent
        # reaches the log, whatever `_read_enabled` is later taught to accept.
        extra={"portal_id": portal.id, "state": "on" if enabled else "off", "changed": changed},
    )
    portal, _ = await _load(principal.portal_id)
    return JSONResponse(crm_block(portal))


@router.post("/portal/crm-notice/dismiss")
async def dismiss_notice(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """"Got it" on the informational CRM notice, for this administrator only."""
    await _require_admin(principal)
    async with control_txn() as session:
        await dismiss_crm_notice(session, principal.portal_id, user_id=principal.user_id)
    return JSONResponse({"notice_visible": False})


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


# --- GET/POST/DELETE /portal/crm-grants (0005) ---------------------------------------


def _read_grant_body(
    payload: Any,
) -> tuple[int, str, list[int], str, bool, bool] | None:
    """`{"user_id", "kind", "department_ids"?, "note"?}` and nothing else, or None.

    Shape only. Whether the combination makes sense is `crm_grants.set_grant`'s decision,
    because that is the single writer and the CHECK constraints behind it are the backstop.
    """
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id")
    kind = payload.get("kind")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    if not isinstance(kind, str):
        return None
    raw_departments = payload.get("department_ids", [])
    if not isinstance(raw_departments, list) or len(raw_departments) > crm_grants.MAX_DEPARTMENTS:
        return None
    departments: list[int] = []
    for value in raw_departments:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        departments.append(value)
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > crm_grants.MAX_NOTE_CHARS * 2:
        return None
    # Absent means the CRM reports and nothing else, which is what a 0005-era body meant.
    covers_crm = payload.get("covers_crm", True)
    covers_calls = payload.get("covers_calls", False)
    if not isinstance(covers_crm, bool) or not isinstance(covers_calls, bool):
        return None
    return user_id, kind, departments, note, covers_crm, covers_calls


async def _department_names(portal_id: int) -> dict[int, str]:
    """`department.get` on the STORED credential, or an empty map.

    Names are a convenience for the person choosing a department, never a permission input:
    the predicate resolves membership from `employees.departments` at query time. So a
    failure here degrades the page to bare ids rather than refusing the page - which is why
    every error is swallowed with its class recorded and nothing else.
    """
    try:
        endpoint = (await _load(portal_id))[0].client_endpoint

        async def call(token: str) -> Any:
            client = BitrixClient(endpoint=endpoint, access_token=token, portal_id=portal_id)
            try:
                return await client.call("department.get", {})
            finally:
                await client.aclose()

        rows = await with_portal_token(portal_id, call)
    except (BitrixError, CredentialUnavailable, ValueError) as exc:
        _log.info(
            "portal: department names unavailable",
            extra={"portal_id": portal_id, "error": type(exc).__name__},
        )
        return {}
    names: dict[int, str] = {}
    if isinstance(rows, list):
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            try:
                key = int(raw.get("ID"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            label = raw.get("NAME")
            names[key] = label[:120] if isinstance(label, str) else ""
    return names


@router.get("/portal/crm-grants")
async def crm_grants_list(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """Who has been granted a CRM scope, plus the people and departments to choose from.

    One payload rather than three endpoints: the page cannot render a picker without the
    employee list, and an administrator opening it always wants all three together.
    """
    await _require_admin(principal)
    portal, _ = await _load(principal.portal_id)
    if portal.status != _ACTIVE:
        return _error("portal_inactive", 401)

    grants = await crm_grants.list_grants(portal.id)
    async with tenant_txn(portal.id) as session:
        employees = (
            (
                await session.execute(
                    select(Employee)
                    .where(Employee.portal_id == portal.id)
                    .order_by(Employee.active.desc(), Employee.last_name, Employee.name)
                )
            )
            .scalars()
            .all()
        )
    department_ids = sorted({dept for row in employees for dept in (row.departments or ())})
    names = await _department_names(portal.id)
    return JSONResponse(
        {
            "grants": [grant.as_json() for grant in grants],
            "employees": [
                {
                    "user_id": row.bx_user_id,
                    "name": " ".join(part for part in (row.name, row.last_name) if part).strip(),
                    "position": row.work_position or "",
                    "active": bool(row.active),
                    "department_ids": list(row.departments or ()),
                }
                for row in employees
            ],
            # Only the departments this portal's people are actually in: a tree of empty
            # departments is a longer list to read and nothing to grant.
            "departments": [
                {"id": dept, "name": names.get(dept, "")} for dept in department_ids
            ],
            # The page says so out loud: this is the one control that can show a person more
            # than Bitrix24 would.
            "widens_beyond_bitrix24": True,
        }
    )


@router.post("/portal/crm-grants")
async def crm_grants_set(
    request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """Grant one employee a CRM scope, or replace the one they have."""
    await _require_admin(principal)
    raw = await request.body()
    if not raw or len(raw) > _MAX_BODY_BYTES:
        return _error("bad_request", 400)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _error("bad_request", 400)
    parsed = _read_grant_body(payload)
    if parsed is None:
        return _error("bad_request", 400)
    user_id, kind, departments, note, covers_crm, covers_calls = parsed

    portal, _ = await _load(principal.portal_id)
    if portal.status != _ACTIVE:
        return _error("portal_inactive", 401)

    try:
        grant = await crm_grants.set_grant(
            portal.id,
            user_id,
            kind=kind,
            department_ids=departments,
            covers_crm=covers_crm,
            covers_calls=covers_calls,
            granted_by=principal.user_id,
            note=note,
        )
    except ValueError:
        # The exception text is NOT logged. `set_grant` builds it from the request body in
        # one branch, and §6's rule is that nothing the caller sent reaches a log line. The
        # code returned below is what support needs; the shape that was refused is in the
        # rest_log entry for the request, already redacted.
        _log.info(
            "portal: grant refused",
            extra={"portal_id": portal.id, "subject": user_id},
        )
        return _error("crm_grant_invalid", 400)
    return JSONResponse(grant.as_json())


@router.delete("/portal/crm-grants/{user_id}")
async def crm_grants_clear(
    user_id: int, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """Remove one employee's grant.

    This does not take their access away so much as return it to whatever Bitrix24 says,
    which today means the live read on their own token.
    """
    await _require_admin(principal)
    if user_id <= 0:
        return _error("bad_request", 400)
    portal, _ = await _load(principal.portal_id)
    if portal.status != _ACTIVE:
        return _error("portal_inactive", 401)
    removed = await crm_grants.clear_grant(
        portal.id, user_id, cleared_by=principal.user_id
    )
    return JSONResponse({"removed": removed})
