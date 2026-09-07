"""`POST /app/` and `POST /settings/` - one handler, routed by `PLACEMENT` (§4.4, §4.5).

This is the endpoint a user's browser hits every time the app is opened inside
Bitrix24, so it is written around three facts:

1. **Nothing in the POST proves anything** (§4.1). `member_id` selects a row, it does
   not authorise; the opener's `AUTH_ID` is proven only by calling the **stored**
   `client_endpoint` with it (§11 assumption 3: a token from portal A is rejected at
   portal B, which is what makes `member_id` spoofing pointless); `DOMAIN` is display
   and CSP data and never a REST base.
2. **Every non-happy path ends in a rendered, translated page** (§4.11). A blank
   frame, a raw Bitrix24 error or an HTTP 500 in the iframe is a moderation rejection,
   so the outer handler catches everything and falls back to `error.html`.
3. **The session token never touches a response header** (§4.6, decision 6). The
   request ends in `handoff.html`, whose inline script does
   `location.replace(path + original query + '#s=<jwt>')`; a 303 with the token in
   `Location` would be written to the reverse proxy's access log on every open of
   every tenant.

The shape of the round trips, from §4.4: **one** `batch` at the stored endpoint with
the opener's token, carrying the identity proof, `app.info` when it is due, and the
CRM tab's reads. The `voximplant.statistic.get` probe is deliberately NOT in it for
administrators (§4.4 step 4, §11 assumptions 4-5: an administrator always has full
telephony access, so spending a command - and shared operating time - to discover
that is waste on the common path). Since `user.admin` is answered by that same batch,
a non-administrator's probe can only be a second request; that is the trade the
design makes, and it is paid by non-administrators only.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.crm import (
    PLACEMENT_ENTITY_TYPES,
    activity_page_commands,
    deal_context_commands,
    entity_activity_commands,
    entity_type_for_placement,
)
from app.bitrix.errors import BitrixError, TransportError
from app.bitrix.forms import (
    FormValidationError,
    IframePost,
    is_event_body,
    parse_iframe_post,
)
from app.bitrix.identity import (
    Identity,
    NotAnAdministrator,
    resolve_identity_with,
    verify_admin_token,
    verify_admin_token_with,
)
from app.bitrix.oauth import OAuthRateLimited, TokenResponse, exchange_refresh_token
from app.bitrix.placements import bind_all
from app.config import settings
from app.db.models import Portal, PortalSync
from app.db.session import control_txn

# §4.4 step 2 says the self-heal runs "§4.3 steps 3-5 inline", so it runs the install
# handler's own helpers rather than a second copy of them: the inbound-log shape (which
# substitutes REFRESH_ID before the generic redaction), the `app.info` allowlist and the
# error -> state mapping must have exactly ONE definition, or the two endpoints would
# drift apart in precisely the places a moderator looks.
from app.handlers.install import (
    _STATISTIC_METHOD,
    _app_info,
    _client_ip,
    _correlation_id,
    _dispatch_event,
    _Hints,
    _log_inbound,
    _method_available,
    _placement_codes,
    _raw_member_id,
    _read_form,
    _reason,
    _state_for_error,
)
from app.handlers.render import render_error, render_handoff, render_install, render_state
from app.i18n import resolve_locale
from app.logging import get_logger
from app.security.crypto import encrypt
from app.security.session_token import issue_session
from app.services.access import LEVEL_DENIED, decide_access
from app.services.crm_context import resolve_crm_context, store_crm_context
from app.services.employees import upsert_viewer
from app.services.portals import (
    get_portal_by_member_id,
    record_event,
    record_placements,
    store_portal_credential,
)

router = APIRouter()
_log = get_logger(__name__)

# --- routing tables (§4.4 step 8) ----------------------------------------------------

#: SPA routes. `DEFAULT` and `LEFT_MENU` both land on the dashboard: the left-menu item
#: comes from the version-card option and arrives as `DEFAULT` (§11 assumption 10), but
#: a portal that binds `LEFT_MENU` explicitly must work too.
_DASHBOARD_PATH: Final[str] = "/dashboard"
_CRM_PATH: Final[str] = "/crm"
_SETTINGS_PATH: Final[str] = "/settings"
#: The two SPA states reached WITHOUT a session token (§4.4 step 8 table).
_DENIED_PATH: Final[str] = "/state/denied"
_CRM_NO_ACCESS_PATH: Final[str] = "/state/crm_no_access"

#: §4.4 step 4: `app.info` runs only when `installed_flag` is not true, or once a day.
_APP_INFO_TTL: Final[timedelta] = timedelta(days=1)

#: §4.4 step 6: "at most one exchange per portal per 10 minutes outside the worker's
#: expired_token path". Process-local by design - it guards a courtesy re-seed, not a
#: security boundary (the per-member_id/IP limiter of §4.1 inside `bitrix/oauth.py` is
#: that), and v1 runs one api container.
_RESEED_COOLDOWN_S: Final[float] = 600.0
_RESEED_GATE_MAX: Final[int] = 4096

#: §4.6: `exp = iat + min(AUTH_EXPIRES, 3600)`. The floor is ours: `AUTH_EXPIRES` is
#: allowed down to 1 second by §4.2, and a token that died before the SPA had loaded
#: would put the tab into an immediate session-exchange loop.
_MAX_SESSION_TTL: Final[int] = 3600
_MIN_SESSION_TTL: Final[int] = 60

#: `portals.app_status` is `varchar(4)` (§3) and its value is Bitrix24's, so it is cut
#: rather than trusted - an over-long value would abort the housekeeping UPDATE.
_APP_STATUS_MAX: Final[int] = 4

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"y", "yes", "true", "1"})
_FALSE_STRINGS: Final[frozenset[str]] = frozenset({"n", "no", "false", "0"})

#: portal_id -> monotonic clock of the last re-seed ATTEMPT (see `_reseed_gate`).
_reseed_attempts: dict[int, float] = {}


# --- small helpers -------------------------------------------------------------------


def _reseed_gate(portal_id: int) -> bool:
    """One opportunistic re-seed attempt per portal per 10 minutes (§4.4 step 6).

    The attempt is marked BEFORE it is made, so a failing exchange is rate-limited
    exactly like a succeeding one - otherwise a portal whose refresh chain is dead
    would fire an exchange on every single open, which is the behaviour Bitrix24
    blocks applications for (§11 assumption 7).
    """
    now = time.monotonic()
    last = _reseed_attempts.get(portal_id)
    if last is not None and now - last < _RESEED_COOLDOWN_S:
        return False
    if len(_reseed_attempts) >= _RESEED_GATE_MAX:
        # Bounded: this dict lives for the life of the process.
        for key in [k for k, seen in _reseed_attempts.items() if now - seen >= _RESEED_COOLDOWN_S]:
            _reseed_attempts.pop(key, None)
    _reseed_attempts[portal_id] = now
    return True


def _flag(value: Any) -> bool | None:
    """Bitrix24's three spellings of a boolean; anything else is "unknown" (None)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None


def _int_or_none(value: Any) -> int | None:
    """An int from whatever Bitrix24 sent, or None. Never raises (§4.1: untrusted)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _AppInfo:
    """What the optional `app.info` command told us (§4.4 step 4 and 6)."""

    ran: bool
    installed: bool | None = None
    version: int | None = None
    status: str | None = None

    @classmethod
    def parse(cls, batch: BatchResult, *, ran: bool) -> _AppInfo:
        """Read the command's result, or record that there is nothing to read.

        An `app.info` error is never fatal - it costs a housekeeping update, not the
        open - and §4.3 step 3's allowlist is reused so both endpoints keep the same
        subset of the payload in `portals.capabilities`.
        """
        if not ran or batch.error("app") is not None:
            return cls(ran=False)
        info = _app_info(batch.get("app"))
        status = info.get("STATUS")
        return cls(
            ran=True,
            installed=_flag(info.get("INSTALLED")),
            version=_int_or_none(info.get("VERSION")),
            status=str(status)[:_APP_STATUS_MAX] if status else None,
        )


@dataclass(frozen=True)
class _CrmTab:
    """The CRM detail tab this open is rendering, if any (§4.4 step 4)."""

    entity_type: str
    entity_id: int
    commands: list[tuple[str, str, dict[str, Any]]]


def _crm_tab(post: IframePost) -> _CrmTab | None:
    """Build the CRM commands for a detail-tab placement (§4.4 step 4).

    `PLACEMENT_OPTIONS["ID"]` is already validated and normalised to a positive int by
    `bitrix/forms.py`, and it is entirely attacker-chosen (anyone can POST this handler
    with any id) - which is why the commands below run with the OPENER's token and why
    a single error among them forbids serving a cached context (§4.4 step 5).
    """
    entity_type = entity_type_for_placement(post.placement)
    if entity_type is None:
        return None
    entity_id = _int_or_none(post.placement_options.get("ID"))
    if entity_id is None or entity_id <= 0:  # pragma: no cover - forms.py guarantees it
        return None
    commands = (
        deal_context_commands(entity_id)
        if entity_type == "DEAL"
        else entity_activity_commands(entity_type, entity_id)
    )
    # §4.4 step 4 budgets the tab at "max 5 pages of 50 = CRM_ACTIVITY_CAP". The
    # follow-up pages are packed into the SAME batch rather than fetched after reading
    # page 0's `result_total`: an entity with more than 50 calls would otherwise cost a
    # second HTTP round trip while the user waits, and a page past the end of the
    # selection comes back as an empty list, not an error.
    commands = [*commands, *activity_page_commands(entity_type, entity_id)]
    return _CrmTab(entity_type=entity_type, entity_id=entity_id, commands=commands)


def _statistic_get_available(portal: Portal) -> bool:
    """`capabilities.statistic_get`, defaulting to available.

    Only an explicit `false` - written by an install or self-heal that actually asked
    `method.get` (§4.3 step 3) - blocks the app. A portal whose capabilities were never
    probed must not be told its build lacks telephony on the strength of a missing key.
    """
    capabilities = portal.capabilities if isinstance(portal.capabilities, dict) else {}
    return capabilities.get("statistic_get") is not False


def _session_ttl(post: IframePost) -> int:
    """§4.6: `exp = iat + min(AUTH_EXPIRES, 3600)`, floored so the tab can load."""
    return max(min(post.auth_expires or _MAX_SESSION_TTL, _MAX_SESSION_TTL), _MIN_SESSION_TTL)


def _target_path(post: IframePost, *, settings_page: bool) -> str:
    """§4.4 step 8 / §4.5: the endpoint and `PLACEMENT` -> the SPA route."""
    if settings_page:
        return _SETTINGS_PATH
    if post.placement in PLACEMENT_ENTITY_TYPES:
        return _CRM_PATH
    return _DASHBOARD_PATH


# --- the endpoints -------------------------------------------------------------------


@router.post("/app/", include_in_schema=False)
async def open_app(request: Request) -> Response:
    """The placement handler for every widget: DEFAULT, LEFT_MENU and the CRM tabs."""
    return await _open(request, settings_page=False)


@router.post("/settings/", include_in_schema=False)
async def open_settings(request: Request) -> Response:
    """§4.5: the same handler, landing on `/settings` for administrators.

    Everyone else gets the translated "administrators only" state: the page shows the
    token owner, the sync state and a Re-authorize button, none of which a regular
    employee may see or act on.
    """
    return await _open(request, settings_page=True)


async def _open(request: Request, *, settings_page: bool) -> Response:
    """§4.4 with the unconditional safety net of §4.11.

    Any unexpected exception becomes `error.html` carrying the request id, which is
    also the `rest_log.correlation_id` of every exchange this request made (§6).
    """
    hints = _Hints.from_request(request)
    try:
        return await _handle(request, hints, settings_page=settings_page)
    except Exception:
        # No body and no headers in the log line (§6): this path can see tokens.
        _log.exception("open: unhandled failure")
        return render_error(
            request, lang=hints.lang, domain=hints.domain, protocol_https=hints.protocol_https
        )


async def _handle(request: Request, hints: _Hints, *, settings_page: bool) -> Response:
    correlation_id = _correlation_id()

    # ---- step 1: parse, validate, log ------------------------------------------------
    try:
        form = await _read_form(request)
    except FormValidationError as exc:
        await _log_inbound(
            request, {}, member_id=None, correlation_id=correlation_id, kind="open",
            error_code=_reason(exc), http_status=400,
        )
        _log.warning("open: unreadable body", extra={"reason": exc.reason})
        return render_state(
            request, "bad_request", lang=hints.lang, domain=hints.domain,
            protocol_https=hints.protocol_https, status_code=400,
        )

    hints.absorb(form)

    # §4.2: a body carrying `event=` is an event whatever URL it arrived on, and the
    # dispatch runs BEFORE the placement allowlist - some cabinets deliver lifecycle
    # events to the app handler, and answering one with "bad request" would lose an
    # uninstall.
    if is_event_body(form):
        return await _dispatch_event(request, form, correlation_id=correlation_id)

    try:
        post = parse_iframe_post(form, request.query_params, request.url.query)
    except FormValidationError as exc:
        await _log_inbound(
            request, form, member_id=_raw_member_id(form), correlation_id=correlation_id,
            kind="open", error_code=_reason(exc), http_status=400,
        )
        _log.warning("open: rejected body", extra={"field": exc.field, "reason": exc.reason})
        return render_state(
            request, "bad_request", lang=hints.lang, domain=hints.domain,
            protocol_https=hints.protocol_https, status_code=400,
        )

    hints.absorb_post(post)
    await _log_inbound(
        request, form, member_id=post.member_id, correlation_id=correlation_id, kind="open"
    )

    def state(kind: str, *, status_code: int = 200) -> Response:
        return render_state(
            request, kind, lang=post.lang, domain=post.domain,
            protocol_https=post.protocol_https, status_code=status_code,
        )

    def handoff(target_path: str, *, token: str = "") -> Response:
        """§4.4 step 8: the SPA path + Bitrix24's ORIGINAL query string + `#s=<jwt>`."""
        return render_handoff(
            request, target_path=target_path, raw_query=post.raw_query, token=token,
            domain=post.domain, protocol_https=post.protocol_https,
        )

    crm = _crm_tab(post)

    # ---- steps 1-2: the tenant row, or the self-heal ---------------------------------
    async with control_txn() as session:
        portal = await get_portal_by_member_id(session, post.member_id)
        appinfo_due = True if portal is None else await _app_info_due(session, portal)

    healed = False
    reseed_tokens: TokenResponse | None = None
    if portal is None or portal.status != "active":
        outcome = await _self_heal(
            request, post, crm=crm, correlation_id=correlation_id, state=state
        )
        if isinstance(outcome, Response):
            return outcome
        portal, identity, batch = outcome
        healed = True
        # The self-heal batch always carries `app.info`, and the credential it wrote is
        # seconds old - so nothing here is due for a re-seed.
        app_info = _AppInfo.parse(batch, ran=True)
        endpoint = portal.client_endpoint
    else:
        # §4.4 step 3: `purge_pending` on an ACTIVE portal is a reinstall during
        # cleanup - the open continues and the dashboard shows the "importing history"
        # banner, because the purge and the fresh sync are both already scheduled.
        if post.auth_id is None:
            # Nothing to prove the opener with; §4.4 step 4's answer to an unusable
            # user token is "close and reopen the app".
            _log.info("open: no AUTH_ID in the body", extra={"portal_id": portal.id})
            return state("retry")
        try:
            identity, batch, reseed_tokens = await _identity_batch(
                post, portal, crm=crm, appinfo=appinfo_due, correlation_id=correlation_id
            )
        except BitrixError as exc:
            # §4.4 step 4: an auth error on the batch means this user's token is not
            # valid at this portal. We NEVER refresh a user's token - the browser holds
            # a live one and reopening the app posts it.
            _log.info(
                "open: identity batch failed",
                extra={"portal_id": portal.id, "error_code": exc.code},
            )
            return state(_state_for_error(exc))
        endpoint = reseed_tokens.client_endpoint if reseed_tokens else portal.client_endpoint
        app_info = _AppInfo.parse(batch, ran=appinfo_due)

    # ---- the viewer is proven; cache them (§7 writer (b), inside tenant_txn) ---------
    await upsert_viewer(portal.id, identity)

    # ---- §4.5: the settings page is administrators only ------------------------------
    if settings_page and not identity.is_admin:
        return state("admin_only")

    # ---- step 5: the access decision (§4.7) -----------------------------------------
    statistic_get = _statistic_get_available(portal)
    probe_ran = False
    probe_error: BitrixError | None = None
    if statistic_get and not identity.is_admin:
        probe_ran = True
        probe_error = await _probe_statistics(
            portal, post, identity, endpoint=endpoint, correlation_id=correlation_id
        )
    decision = decide_access(
        is_admin=identity.is_admin,
        probe_error=probe_error,
        probe_ran=probe_ran,
        statistic_get_available=statistic_get,
    )

    # ---- step 6: housekeeping under the portals row lock -----------------------------
    reinstall_needed = await _housekeeping(portal, post, identity, app_info)
    if not healed:
        await _maybe_reseed(
            portal, post, identity, tokens=reseed_tokens,
            statistic_get=statistic_get, correlation_id=correlation_id,
        )
    if reinstall_needed:
        # §4.4 step 6: `app.info` says the app is not installed and an administrator is
        # looking - re-render the install page so `BX24.installFinish()` runs again.
        _log.info("open: app.info reports not installed", extra={"portal_id": portal.id})
        return render_install(
            request, lang=post.lang, domain=post.domain, protocol_https=post.protocol_https
        )

    if decision.state is not None:
        return state(decision.state)

    # ---- step 7: the CRM tab context (§4.4 steps 5 and 7, §4.8) ----------------------
    entity: dict[str, Any] | None = None
    if crm is not None:
        context = await resolve_crm_context(
            batch, entity_type=crm.entity_type, entity_id=crm.entity_id
        )
        if context is None:
            # `resolve_crm_context` returns None when ANY of the tab's commands errored
            # or is missing (§4.4 step 5) - Bitrix24 says this user may not read this
            # card. No JWT is minted at all, so the SPA cannot even ask for the cached
            # context that someone with rights resolved earlier.
            _log.info(
                "open: CRM read refused",
                extra={
                    "portal_id": portal.id,
                    "user_id": identity.user_id,
                    "entity_type": crm.entity_type,
                },
            )
            return handoff(_CRM_NO_ACCESS_PATH)
        await store_crm_context(portal.id, context, user_id=identity.user_id)
        entity = {"t": crm.entity_type, "id": crm.entity_id}

    if decision.level == LEVEL_DENIED:
        # §4.4 step 8 table: the mandated "ask your administrator" page, inside the SPA
        # shell, with the original query string and NO token.
        return handoff(_DENIED_PATH)

    # ---- step 8: mint the JWT and hand over to the browser ---------------------------
    token = issue_session(
        pid=portal.id,
        mid=portal.member_id,
        sub=identity.user_id,
        adm=identity.is_admin,
        acc=decision.level,
        tz=identity.timezone or portal.timezone or "UTC",
        lang=resolve_locale(post.lang or portal.lang),
        plc=post.placement,
        ent=entity,
        ttl_seconds=_session_ttl(post),
    )
    _log.info(
        "open: session issued",
        # Never the token and never the query string (§4.6, §6).
        extra={
            "portal_id": portal.id,
            "user_id": identity.user_id,
            "placement": post.placement,
            "access": decision.level,
        },
    )
    return handoff(_target_path(post, settings_page=settings_page), token=token)


# --- step 2: self-heal (= §4.3 steps 3-5, inline) -------------------------------------


async def _self_heal(
    request: Request,
    post: IframePost,
    *,
    crm: _CrmTab | None,
    correlation_id: uuid.UUID,
    state: Callable[..., Response],
) -> tuple[Portal, Identity, BatchResult] | Response:
    """Recreate a missing or uninstalled tenant from a proven payload (§4.4 step 2).

    This is the "the database was restored" / "the app was reinstalled without the
    install URL being hit" path, and it is the same proof ladder as `POST /install/`,
    in the same order, for the same reason (§4.1): the POSTed `REFRESH_ID` is exchanged
    at the allowlisted OAuth host, the response - not the POST - is the authority for
    `client_endpoint` and `member_id`, and the opener must then prove `user.admin` with
    that token at that endpoint before a single column is written.

    A non-administrator who trips this path is told to ask an administrator to open the
    app once, and **nothing is written**: a credential proven for a regular employee
    would make the worker cache a filtered slice of the portal's calls forever (§4.1).

    Nothing is ever contacted at `DOMAIN` (§4.1 endpoint invariant), which is why a
    portal that cannot refresh simply looks "not installed" here.
    """
    if post.refresh_id is None:
        _log.info(
            "open: unknown portal without a refresh token", extra={"member_id": post.member_id}
        )
        return state("not_installed")

    try:
        tokens = await exchange_refresh_token(
            post.refresh_id,
            server_endpoint=post.server_endpoint,
            expected_member_id=post.member_id,
            source_ip=_client_ip(request),
            correlation_id=correlation_id,
        )
    except OAuthRateLimited:
        # §4.1: exchanges driven by unauthenticated input are capped per member_id and
        # per source IP; the honest answer is "try again in a minute".
        _log.warning("open: self-heal exchange rate limited", extra={"member_id": post.member_id})
        return state("retry")
    except Exception:
        # Includes the member_id mismatch: the POST claimed a portal this refresh token
        # does not belong to. The exchange payload itself is never logged (§6).
        _log.warning(
            "open: self-heal exchange failed", extra={"member_id": post.member_id}, exc_info=True
        )
        return state("not_installed")

    extra: list[tuple[str, str, dict[str, Any]]] = [
        ("app", "app.info", {}),
        ("method", "method.get", {"name": _STATISTIC_METHOD}),
        ("placement", "placement.get", {}),
    ]
    if crm is not None:
        # The tab's reads ride in the SAME batch: the opener is about to be proven an
        # administrator, and a second HTTP request for them would be waste (§4.3 step 3).
        extra.extend(crm.commands)

    try:
        admin, batch = await verify_admin_token_with(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            extra_commands=extra,
            member_id=post.member_id,
            correlation_id=correlation_id,
        )
    except NotAnAdministrator:
        _log.info("open: self-heal by a non-administrator", extra={"member_id": post.member_id})
        return state("not_installed")
    except BitrixError as exc:
        _log.warning(
            "open: self-heal proof failed",
            extra={"member_id": post.member_id, "error_code": exc.code},
        )
        return state(_state_for_error(exc))

    info = _app_info(batch.get("app"))
    method_error = batch.error("method")
    statistic_get = method_error is None and _method_available(batch.get("method"))
    status = info.get("STATUS")
    capabilities: dict[str, Any] = {
        # §4.3 step 3: a missing method does not abort the write - the portal is
        # recorded with the flag and the admin gets the explicit `method_missing` state.
        "statistic_get": statistic_get,
        "method_get_error": method_error.code if method_error else None,
        "app_info": info,
        "scope": tokens.scope,
        "checked_at": datetime.now(UTC).isoformat(),
        "healed": True,
    }

    async with control_txn() as session:
        outcome = await store_portal_credential(
            session,
            member_id=post.member_id,
            tokens=tokens,
            admin=admin,
            application_token=post.application_token,
            domain=post.domain,
            protocol_https=post.protocol_https,
            lang=post.lang,
            app_status=str(status)[:_APP_STATUS_MAX] if status else tokens.status,
            capabilities=capabilities,
            token_status="ok" if statistic_get else "method_missing",
        )
        await record_event(
            session, outcome.portal_id, "self_heal", user_id=admin.user_id,
            details={
                "statistic_get": statistic_get,
                "created": outcome.created,
                "reinstalled": outcome.reinstalled,
            },
        )

    _log.info(
        "open: portal healed",
        # NOT `created`: `logging.LogRecord` owns that attribute name and passing it in
        # `extra` raises inside the logging call itself (§4.11 - a log line must never
        # be able to turn a working open into an error page).
        extra={
            "portal_id": outcome.portal_id,
            "member_id": outcome.member_id,
            "portal_created": outcome.created,
        },
    )

    await _bind_widgets(
        tokens=tokens, admin=admin, post=post, portal_id=outcome.portal_id,
        existing=batch.get("placement"), correlation_id=correlation_id,
    )

    async with control_txn() as session:
        portal = await get_portal_by_member_id(session, post.member_id)
    if portal is None:  # pragma: no cover - it was written in this very request
        _log.error("open: healed portal disappeared", extra={"member_id": post.member_id})
        return state("retry")
    return portal, admin, batch


async def _bind_widgets(
    *,
    tokens: TokenResponse,
    admin: Identity,
    post: IframePost,
    portal_id: int,
    existing: Any,
    correlation_id: uuid.UUID,
) -> None:
    """§4.3 step 5, run from the self-heal: bind the CRM tabs, subscribe the events.

    Never fatal and never raises: the app is usable from the left-menu item with no CRM
    tab at all, and the settings page has a Re-bind button that runs the same code.
    """
    try:
        async with BitrixClient(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            portal_id=portal_id,
            member_id=post.member_id,
            token_user_id=tokens.user_id if tokens.user_id is not None else admin.user_id,
            correlation_id=correlation_id,
        ) as client:
            placements, event_bind = await bind_all(
                client,
                handler_url=f"{settings.app_base_url}/app/",
                events_url=f"{settings.app_base_url}/events/",
                existing=_placement_codes(existing),
            )
        async with control_txn() as session:
            await record_placements(
                session, portal_id, placements=placements,
                capabilities_patch={"event_bind": event_bind},
            )
    except Exception:
        _log.warning(
            "open: self-heal binding failed", extra={"portal_id": portal_id}, exc_info=True
        )


# --- step 4: the one batch, and the transport-failure cure ----------------------------


async def _app_info_due(session: AsyncSession, portal: Portal) -> bool:
    """§4.4 step 4: `app.info` only when `installed_flag` is not true, or once a day.

    The timestamp lives in `portal_sync.last_appinfo_at` (§3) - the one column that
    means "when did we last ask this portal about itself".
    """
    if portal.installed_flag is not True:
        return True
    last = (
        await session.execute(
            select(PortalSync.last_appinfo_at).where(PortalSync.portal_id == portal.id)
        )
    ).scalar_one_or_none()
    return last is None or last < datetime.now(UTC) - _APP_INFO_TTL


async def _identity_batch(
    post: IframePost,
    portal: Portal,
    *,
    crm: _CrmTab | None,
    appinfo: bool,
    correlation_id: uuid.UUID,
) -> tuple[Identity, BatchResult, TokenResponse | None]:
    """ONE `batch` at the STORED `client_endpoint` with the opener's `AUTH_ID` (§4.4 step 4).

    The endpoint is the stored one and never `DOMAIN` (§4.1): that is what makes a
    forged `member_id` useless - a user token minted by another portal is rejected
    there (§11 assumption 3).

    A **transport-level** failure (DNS/TLS/connect/timeout - never a REST error) is the
    signature of a renamed portal or a newly connected custom domain, and it is the one
    case §4.4 step 4 cures automatically: one refresh exchange re-learns
    `client_endpoint` from the OAuth response, and the batch is retried exactly ONCE.
    The `TokenResponse` travels back to the caller so step 6 can persist the new
    endpoint through `store_portal_credential()` - the only writer §4.1 allows - and
    only when the opener turns out to be an administrator. For a non-administrator the
    new endpoint serves this one request and the next admin open makes it durable.
    """
    extra: list[tuple[str, str, dict[str, Any]]] = []
    if appinfo:
        extra.append(("app", "app.info", {}))
    if crm is not None:
        extra.extend(crm.commands)

    try:
        identity, batch = await resolve_identity_with(
            endpoint=portal.client_endpoint,
            access_token=post.auth_id or "",
            extra_commands=extra,
            portal_id=portal.id,
            member_id=portal.member_id,
            correlation_id=correlation_id,
        )
        return identity, batch, None
    except TransportError:
        _log.warning(
            "open: stored endpoint unreachable, re-learning it", extra={"portal_id": portal.id}
        )
        tokens = await _relearn_endpoint(post, portal, correlation_id=correlation_id)
        if tokens is None:
            raise  # §4.4 step 4: still failing -> /state/retry

    identity, batch = await resolve_identity_with(
        endpoint=tokens.client_endpoint,
        access_token=post.auth_id or "",
        extra_commands=extra,
        portal_id=portal.id,
        member_id=portal.member_id,
        correlation_id=correlation_id,
    )
    return identity, batch, tokens


async def _relearn_endpoint(
    post: IframePost, portal: Portal, *, correlation_id: uuid.UUID
) -> TokenResponse | None:
    """One refresh exchange to re-learn `client_endpoint` (§4.4 step 4).

    Returns None - and the caller renders `/state/retry` - whenever the exchange cannot
    be made or cannot be trusted: no `REFRESH_ID`, the rate limit, a dead chain, or a
    response for a different `member_id` (`expected_member_id` enforces that inside
    `exchange_refresh_token`, which is what stops this path from pointing a tenant at
    an attacker's REST base).
    """
    if post.refresh_id is None:
        return None
    try:
        tokens = await exchange_refresh_token(
            post.refresh_id,
            server_endpoint=post.server_endpoint,
            expected_member_id=portal.member_id,
            portal_id=portal.id,
            correlation_id=correlation_id,
        )
    except Exception:
        _log.warning(
            "open: endpoint re-learn exchange failed",
            extra={"portal_id": portal.id},
            exc_info=True,
        )
        async with control_txn() as session:
            await record_event(
                session, portal.id, "refresh_failed", details={"reason": "endpoint_relearn"}
            )
        return None

    if tokens.client_endpoint and tokens.client_endpoint != portal.client_endpoint:
        # The audit trail for a rename. Neither value is a secret, and support needs to
        # see that the portal moved (§3 `portal_events`).
        async with control_txn() as session:
            await record_event(
                session, portal.id, "domain_changed",
                details={"from": portal.client_endpoint, "to": tokens.client_endpoint},
            )
    return tokens


async def _probe_statistics(
    portal: Portal,
    post: IframePost,
    identity: Identity,
    *,
    endpoint: str,
    correlation_id: uuid.UUID,
) -> BitrixError | None:
    """The per-user statistics probe (§4.4 step 4), for NON-administrators only.

    Its result is not data: it is the only REST way to learn whether this user may read
    call statistics at all (docs/bitrix24-api-research.md). A clean answer means "some
    access" and maps to `own`, never to `all` - a user with the own-calls level gets
    HTTP 200 with a filtered result rather than an error.

    Returns the typed error instead of raising: `services/access.py` owns every mapping
    from it to an access level or a state page.
    """
    params: dict[str, Any] = {
        "FILTER": {"PORTAL_USER_ID": identity.user_id},
        "SORT": "ID",
        "ORDER": "DESC",
        "start": 0,
    }
    try:
        async with BitrixClient(
            endpoint=endpoint,
            access_token=post.auth_id or "",
            portal_id=portal.id,
            member_id=portal.member_id,
            token_user_id=identity.user_id,
            correlation_id=correlation_id,
        ) as client:
            await client.call(_STATISTIC_METHOD, params)
        return None
    except BitrixError as exc:
        _log.info(
            "open: statistics probe refused",
            extra={"portal_id": portal.id, "user_id": identity.user_id, "error_code": exc.code},
        )
        return exc


# --- step 6: housekeeping and the non-routine re-seed ---------------------------------


async def _housekeeping(
    portal: Portal, post: IframePost, identity: Identity, app_info: _AppInfo
) -> bool:
    """§4.4 step 6, under the `portals` row lock. True when the admin needs `install.html`.

    What may be written for ANY opener: `last_opened_at`, `lang`, and `domain` /
    `protocol_https` when they changed (both are display and CSP data - §4.10 builds
    `frame-ancestors` from them, so a stale value would blank the frame after a
    rename). What may be written only for an ADMINISTRATOR: `application_token_enc`
    (Bitrix24 replaces it whenever the application version changes, and it is the
    credential §4.9 verifies EVERY inbound event against - accepting one from a regular
    employee would let them rotate the value that authenticates uninstall events) and
    `last_admin_opened_at`.

    The REST base and the two API tokens are deliberately absent from this function:
    §4.1 gives those columns exactly one writer,
    `services/portals.py::store_portal_credential()`.
    """
    async with control_txn() as session:
        row = (
            await session.execute(select(Portal).where(Portal.id == portal.id).with_for_update())
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover - the row was read moments ago
            return False

        values: dict[str, Any] = {"last_opened_at": func.now()}
        if post.lang and row.lang != post.lang:
            values["lang"] = post.lang
        if row.domain != post.domain:
            values["domain"] = post.domain
        if row.protocol_https != post.protocol_https:
            values["protocol_https"] = post.protocol_https
        if identity.is_admin:
            values["last_admin_opened_at"] = func.now()
            if post.application_token:
                values["application_token_enc"] = encrypt(
                    post.application_token, member_id=row.member_id, column="application_token"
                )
        if app_info.ran:
            if app_info.installed is not None:
                values["installed_flag"] = app_info.installed
            if app_info.version is not None:
                values["app_version"] = app_info.version
            if app_info.status:
                values["app_status"] = app_info.status

        await session.execute(update(Portal).where(Portal.id == row.id).values(**values))
        if app_info.ran:
            await session.execute(
                update(PortalSync)
                .where(PortalSync.portal_id == row.id)
                .values(last_appinfo_at=func.now())
            )

        installed = app_info.installed if app_info.ran else row.installed_flag
        return bool(identity.is_admin and installed is False)


def _reseed_due(portal: Portal) -> bool:
    """§4.4 step 6: "Token re-seed is not routine."

    Only two conditions make it due here (the third, an explicit "Re-authorize" click,
    is `POST /api/v1/portal/reauthorize`, not this handler): the stored credential is
    not healthy, or its refresh token is older than `TOKEN_RESEED_AFTER_DAYS` - 120
    days by default, so the 180-day chain is renewed long before it dies silently.
    """
    if portal.token_status != "ok":  # noqa: S105 - a state enum (§3), not a credential
        return True
    refreshed = portal.token_refreshed_at
    if refreshed is None:
        return True
    return refreshed < datetime.now(UTC) - timedelta(days=settings.token_reseed_after_days)


async def _maybe_reseed(
    portal: Portal,
    post: IframePost,
    identity: Identity,
    *,
    tokens: TokenResponse | None,
    statistic_get: bool,
    correlation_id: uuid.UUID,
) -> None:
    """Re-seed the portal credential from the opener's `REFRESH_ID` (§4.4 step 6).

    Three guards, in order, and all of them matter:

    * **administrator only** - the stored credential is what the worker syncs the whole
      portal with, so a regular employee's token would silently cache a filtered slice
      of the calls (§4.1 credential invariant; `store_portal_credential` refuses it
      anyway, which is the point of passing the proof as an argument);
    * **due, or already exchanged** - a routine open performs no exchange at all (§11
      assumption 7: Bitrix24 blocks applications that refresh excessively). `tokens` is
      non-None only on the transport-failure path of §4.4 step 4, where the exchange
      has already happened and its `client_endpoint` is the very thing worth persisting;
    * **at most one attempt per portal per 10 minutes**, counted whether it succeeds or
      fails.

    A failure leaves the existing credential completely untouched and records
    `portal_events(refresh_failed)`: the app keeps working with whatever it had, which
    is what "opportunistic" has to mean.
    """
    if not identity.is_admin:
        return
    if tokens is None:
        if not _reseed_due(portal) or post.refresh_id is None:
            return
        if not _reseed_gate(portal.id):
            _log.info("open: re-seed suppressed by the cooldown", extra={"portal_id": portal.id})
            return
        try:
            tokens = await exchange_refresh_token(
                post.refresh_id,
                server_endpoint=post.server_endpoint,
                expected_member_id=portal.member_id,
                portal_id=portal.id,
                correlation_id=correlation_id,
            )
        except Exception:
            _log.warning("open: re-seed exchange failed", extra={"portal_id": portal.id})
            async with control_txn() as session:
                await record_event(
                    session, portal.id, "refresh_failed", user_id=identity.user_id,
                    details={"reason": "reseed_exchange"},
                )
            return
        relearned = False
    else:
        relearned = True

    try:
        # §4.4 step 6: the admin proof is for the TOKEN BEING STORED, not for the one
        # the opener presented. They belong to the same person but they are not the
        # same credential, and §4.1 admits only a token that has itself answered
        # `user.admin=true` at an OAuth-derived endpoint.
        admin = await verify_admin_token(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            portal_id=portal.id,
            member_id=portal.member_id,
            correlation_id=correlation_id,
        )
    except Exception:
        _log.warning("open: re-seed admin proof failed", extra={"portal_id": portal.id})
        async with control_txn() as session:
            await record_event(
                session, portal.id, "refresh_failed", user_id=identity.user_id,
                details={"reason": "reseed_admin_proof"},
            )
        return

    capabilities = dict(portal.capabilities) if isinstance(portal.capabilities, dict) else {}
    capabilities["statistic_get"] = statistic_get
    capabilities["checked_at"] = datetime.now(UTC).isoformat()
    async with control_txn() as session:
        outcome = await store_portal_credential(
            session,
            member_id=portal.member_id,
            tokens=tokens,
            admin=admin,
            application_token=post.application_token,
            domain=post.domain,
            protocol_https=post.protocol_https,
            lang=post.lang,
            app_status=portal.app_status,
            capabilities=capabilities,
            token_status="ok" if statistic_get else "method_missing",
        )
        await record_event(
            session, outcome.portal_id, "token_reseeded", user_id=admin.user_id,
            details={"token_status": outcome.token_status, "endpoint_relearned": relearned},
        )
    _log.info("open: portal credential re-seeded", extra={"portal_id": portal.id})
