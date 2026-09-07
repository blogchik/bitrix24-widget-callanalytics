"""`POST /install/` - the Marketplace installation handler (§4.3).

The whole endpoint is one long proof that the POST is what it claims to be, in the
order §4.1 fixes, because NOTHING in a Bitrix24 form body proves anything on its own:

1. the body is parsed by the allowlist (`bitrix/forms.py`) and logged;
2. `member_id` is proven by an OAuth refresh exchange, whose response - not the POST -
   is the authority for `client_endpoint`, `user_id`, `scope` and `status`;
3. the installer is proven to be an administrator with that token, at that endpoint;
4. only then may a row be written, and only through `services/portals.py`.

Every early exit renders a translated page (§4.11): a blank frame, an HTTP 500 or a raw
Bitrix24 error string in the iframe is a moderation rejection, so the outer handler
catches everything and falls back to `error.html` with the request id.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from sqlalchemy import update

from app.bitrix.client import BitrixClient
from app.bitrix.errors import (
    BitrixError,
    InsufficientScope,
    MethodNotFound,
    OperationTimeLimit,
    PaymentRequired,
    PortalDeleted,
    QueryLimitExceeded,
    TransportError,
)
from app.bitrix.forms import (
    MAX_BODY_BYTES,
    FormValidationError,
    IframePost,
    is_event_body,
    parse_iframe_post,
)
from app.bitrix.identity import NotAnAdministrator, verify_admin_token_with
from app.bitrix.oauth import OAuthRateLimited, TokenResponse, exchange_refresh_token
from app.bitrix.placements import bind_all
from app.config import settings
from app.db.models import Portal
from app.db.session import control_txn
from app.handlers.render import render_error, render_install, render_state
from app.logging import get_logger, get_request_id
from app.security.redact import REDACTED
from app.services.portals import record_event, store_portal_credential
from app.services.rest_log import write_rest_log

router = APIRouter()
_log = get_logger(__name__)

#: The method the whole product depends on; §4.3 step 3 records its presence as a
#: capability instead of failing the install, so the admin sees an explicit state.
_STATISTIC_METHOD: Final[str] = "voximplant.statistic.get"

#: `app.info` keys worth keeping in `portals.capabilities` (research (f) app.info).
#: An allowlist, not the whole payload: the column is support-visible JSONB.
_APP_INFO_KEYS: Final[tuple[str, ...]] = (
    "ID",
    "CODE",
    "VERSION",
    "STATUS",
    "INSTALLED",
    "PAYMENT_EXPIRED",
    "DAYS",
    "LANGUAGE_ID",
    "LICENSE",
    "LICENSE_TYPE",
    "LICENSE_FAMILY",
)

#: Credential-bearing form fields whose NAME does not match the §6 key regex. `AUTH_ID`
#: and `APPLICATION_TOKEN` do match it ("auth", "token"); `REFRESH_ID` does not.
_SECRET_FIELDS: Final[frozenset[str]] = frozenset({"refresh_id"})


# --- small helpers -------------------------------------------------------------------


@dataclass
class _Hints:
    """Display-only fields kept for the page we may have to render at any moment.

    §4.10 needs a `DOMAIN` for the `frame-ancestors` header even on the paths where the
    body was REJECTED - without one the CSP is `'none'` and Bitrix24 shows a blank
    frame, which §4.11 counts as a rejection. So the raw values are captured as soon as
    they are readable and refined once validation succeeds. They are never trusted for
    anything else: `render.py` re-validates the hostname before it reaches a header.
    """

    lang: str | None = None
    domain: str | None = None
    protocol_https: bool = True

    @classmethod
    def from_request(cls, request: Request) -> _Hints:
        """Bitrix24 repeats DOMAIN/PROTOCOL/LANG on the handler URL (§4.4 step 8)."""
        hints = cls()
        hints.absorb(request.query_params)
        return hints

    def absorb(self, source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            lowered = key.lower()
            if lowered == "domain" and value.strip():
                self.domain = value.strip()
            elif lowered == "lang" and value.strip():
                self.lang = value.strip()
            elif lowered == "protocol" and value.strip() in {"0", "1"}:
                self.protocol_https = value.strip() == "1"

    def absorb_post(self, post: IframePost) -> None:
        self.domain = post.domain
        self.lang = post.lang
        self.protocol_https = post.protocol_https


def _correlation_id() -> uuid.UUID:
    """Reuse the request id as `rest_log.correlation_id` (§6).

    The middleware generates it as `uuid4().hex`, so one id ties the stdout lines, the
    inbound log row, every outbound exchange and the id printed on the page together.
    """
    raw = get_request_id()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            _log.debug("install: request id is not a uuid, correlating with a fresh one")
    return uuid.uuid4()


def _client_ip(request: Request) -> str | None:
    """The peer address for the §4.1 per-IP exchange limiter.

    uvicorn runs with `--proxy-headers`, so behind Caddy this is the real client.
    """
    return request.client.host if request.client else None


def _request_url(request: Request) -> str:
    """The endpoint URL without its query string (§6: query strings never reach a log)."""
    return str(request.url.replace(query=""))


def _reason(exc: FormValidationError) -> str:
    """A stable `field:reason` slug for the log - never the offending value (§4.2)."""
    return f"{exc.field}:{exc.reason}"


def _raw_member_id(form: Mapping[str, Any]) -> str | None:
    """`member_id` for the log row of a body that FAILED validation (§6).

    Attribution matters for rejected requests, but the column is `varchar(32)`, so an
    unvalidated value can only be kept when it already has the documented shape. Both
    spellings are accepted: an iframe POST carries `member_id`, an event body of §4.9
    carries it inside `auth[member_id]`.
    """
    for key, value in form.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.lower() not in {"member_id", "auth[member_id]"}:
            continue
        candidate = value.strip().lower()
        if len(candidate) == 32 and all(c in "0123456789abcdef" for c in candidate):
            return candidate
    return None


def _loggable(form: Mapping[str, Any]) -> dict[str, Any]:
    """The POST body as it may be written to `rest_log` (§6).

    `security/redact.py` redacts by key NAME (`token|secret|auth|password|client_id`) and
    Bitrix24 calls the refresh token `REFRESH_ID` - a credential whose key matches none
    of those words, and the most valuable one in the body: it is a 180-day chain that
    mints portal access tokens. It is substituted here, before the body ever reaches the
    log writer, which then applies the generic recursive redaction on top.
    """
    return {
        key: (REDACTED if isinstance(key, str) and key.lower() in _SECRET_FIELDS else value)
        for key, value in form.items()
    }


def _jsonable(value: Any) -> Any:
    """Coerce a helper's return value into something JSONB can hold.

    Defensive because the value is written into `portals.capabilities` /
    `portals.placements`, and a non-serialisable object there would turn a best-effort
    bind result into a failed install.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _method_available(result: Any) -> bool:
    """Read `method.get {name: ...}` (§4.3 step 3).

    The response shape is not pinned by the docs we verified, so every documented and
    plausible shape is accepted and anything unrecognised counts as "not available":
    the consequence is an explicit `method_missing` state, never a silent half-install.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        existing = result.get("isExisting", result.get("IS_EXISTING"))
        available = result.get("isAvailable", result.get("IS_AVAILABLE"))
        if existing is None and available is None:
            return bool(result)
        return bool(existing if existing is not None else True) and bool(
            available if available is not None else True
        )
    if isinstance(result, (list, tuple)):
        return _STATISTIC_METHOD in {str(item).lower() for item in result}
    if isinstance(result, str):
        return result.strip().lower() in {"y", "true", "1", _STATISTIC_METHOD}
    return False


def _app_info(result: Any) -> dict[str, Any]:
    """The allowlisted subset of `app.info`, keys upper-cased for stable lookups."""
    if not isinstance(result, Mapping):
        return {}
    upper = {str(k).upper(): v for k, v in result.items()}
    return {key: _jsonable(upper[key]) for key in _APP_INFO_KEYS if key in upper}


def _state_for_error(exc: BaseException) -> str:
    """Map a failure of the admin proof / capability batch onto a §4.11 state.

    Deliberately conservative: everything that can plausibly be transient renders
    "retry" (close and reopen), because at this point the payload has already proven
    itself and telling the admin their request was malformed would be a lie.
    """
    if isinstance(exc, InsufficientScope):
        return "scope"
    if isinstance(exc, MethodNotFound):
        return "method_missing"
    if isinstance(exc, (PortalDeleted, PaymentRequired)):
        return "error"
    if isinstance(exc, (TransportError, QueryLimitExceeded, OperationTimeLimit, BitrixError)):
        return "retry"
    return "error"


async def _read_form(request: Request) -> dict[str, str]:
    """The POST body as a flat string mapping, or a `FormValidationError`.

    Content-Length is checked before parsing so an oversized body is refused without
    being materialised; `bitrix/forms.py` re-checks the real size afterwards (§4.2).
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise FormValidationError("body", "body_too_large")
    try:
        raw = await request.form()
    except Exception as exc:  # malformed urlencoded/multipart body
        raise FormValidationError("body", "unparsable") from exc
    # Non-string values are file parts; §4.2 knows no file field, and dropping them
    # here keeps every downstream signature a plain str->str mapping.
    return {key: value for key, value in raw.multi_items() if isinstance(value, str)}


async def _log_inbound(
    request: Request,
    form: Mapping[str, str],
    *,
    member_id: str | None,
    correlation_id: uuid.UUID,
    kind: str = "install",
    error_code: str | None = None,
    http_status: int | None = None,
) -> None:
    """One `rest_log` row per inbound Bitrix24 POST (§6), redacted by the writer.

    `portal_id` is NULL: at this point in the flow nothing has proven which portal (or
    whether the portal exists), and §6 says the rejected/unknown case keeps `member_id`
    as the only attribution.
    """
    await write_rest_log(
        direction="in",
        kind=kind,
        method="POST",
        url=_request_url(request),
        portal_id=None,
        member_id=member_id,
        correlation_id=correlation_id,
        token_user_id=None,
        request=_loggable(form),
        http_status=http_status,
        error_code=error_code,
    )


# --- placement / event binding (§4.3 step 5) -----------------------------------------


def _placement_codes(raw: Any) -> frozenset[str]:
    """The placements already bound, from the `placement.get` of the step-3 batch.

    `bind_all` skips what is already there, which is what makes a re-run of the
    install URL idempotent: a second `placement.bind` for the same tab adds a duplicate
    rather than replacing the first. The response is untrusted (§4.1), so anything that
    is not an object with a string `placement` is ignored rather than trusted.
    """
    if not isinstance(raw, list):
        return frozenset()
    codes: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        code = item.get("placement") or item.get("PLACEMENT")
        if isinstance(code, str) and code:
            codes.add(code.upper())
    return frozenset(codes)


# --- the endpoint --------------------------------------------------------------------


@router.post("/install/", include_in_schema=False)
async def install(request: Request) -> Response:
    """§4.3 with an unconditional safety net (§4.11).

    ANY unexpected exception becomes `error.html` carrying the request id: the frame
    must never show a FastAPI traceback, and a bare 500 in the installation slider is
    indistinguishable, to a moderator, from a broken app.
    """
    hints = _Hints.from_request(request)
    try:
        return await _install(request, hints)
    except Exception:
        # No body, no headers in the log line (§6): this path can see tokens.
        _log.exception("install: unhandled failure")
        return render_error(
            request,
            lang=hints.lang,
            domain=hints.domain,
            protocol_https=hints.protocol_https,
        )


async def _install(request: Request, hints: _Hints) -> Response:
    correlation_id = _correlation_id()

    # ---- step 1: parse, validate, log ------------------------------------------------
    try:
        form = await _read_form(request)
    except FormValidationError as exc:
        await _log_inbound(
            request, {}, member_id=None, correlation_id=correlation_id,
            error_code=_reason(exc), http_status=400,
        )
        _log.warning("install: unreadable body", extra={"reason": exc.reason})
        return render_state(
            request, "bad_request", lang=hints.lang, domain=hints.domain,
            protocol_https=hints.protocol_https, status_code=400,
        )

    hints.absorb(form)

    # ---- step 2: an `event=` body is an event, whatever URL it arrived on (§4.2) -----
    if is_event_body(form):
        return await _dispatch_event(request, form, correlation_id=correlation_id)

    try:
        post = parse_iframe_post(form, request.query_params, request.url.query)
    except FormValidationError as exc:
        await _log_inbound(
            request, form, member_id=_raw_member_id(form), correlation_id=correlation_id,
            error_code=_reason(exc), http_status=400,
        )
        _log.warning("install: rejected body", extra={"field": exc.field, "reason": exc.reason})
        return render_state(
            request, "bad_request", lang=hints.lang, domain=hints.domain,
            protocol_https=hints.protocol_https, status_code=400,
        )

    hints.absorb_post(post)
    await _log_inbound(request, form, member_id=post.member_id, correlation_id=correlation_id)

    def state(kind: str, *, status_code: int = 200) -> Response:
        return render_state(
            request, kind, lang=post.lang, domain=post.domain,
            protocol_https=post.protocol_https, status_code=status_code,
        )

    # ---- step 3: prove `member_id` by an OAuth exchange (§4.3 step 2) ----------------
    if post.refresh_id is None:
        # §4.1 endpoint invariant: `client_endpoint` may ONLY come from an OAuth
        # response, so a portal that cannot refresh is out of scope in v1 - and no row
        # is created or touched, not even a status flag.
        _log.info("install: portal without a refresh token", extra={"member_id": post.member_id})
        return state("unsupported_portal")

    try:
        tokens = await exchange_refresh_token(
            post.refresh_id,
            server_endpoint=post.server_endpoint,
            expected_member_id=post.member_id,
            source_ip=_client_ip(request),
            correlation_id=correlation_id,
        )
    except OAuthRateLimited:
        # §4.1: the exchange is triggered by unauthenticated input, so it is capped per
        # member_id and per source IP; the honest answer is "try again in a minute".
        _log.warning("install: exchange rate limited", extra={"member_id": post.member_id})
        return state("retry")
    except Exception:
        # Includes the member_id mismatch of §4.3 step 2: the POST claimed a portal the
        # refresh token does not belong to. Never log the exchange payload itself.
        _log.warning(
            "install: refresh exchange failed", extra={"member_id": post.member_id}, exc_info=True
        )
        return state("bad_request", status_code=400)

    # ---- step 4: admin proof AND capabilities in ONE batch (§4.3 step 3) ------------
    # The design specifies a single batch carrying user.current, user.admin, app.info,
    # method.get and placement.get, and budgets the whole install at three round trips
    # (§4.3 step 6). Splitting the proof from the probe would spend a fourth and burn
    # shared operating time (§5.6) for nothing.
    try:
        admin, batch = await verify_admin_token_with(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            extra_commands=[
                ("app", "app.info", {}),
                ("method", "method.get", {"name": _STATISTIC_METHOD}),
                ("placement", "placement.get", {}),
            ],
            member_id=post.member_id,
            correlation_id=correlation_id,
        )
    except NotAnAdministrator:
        # Writes NOTHING: the credential invariant of §4.1 admits only admin-proven
        # tokens, and a half-written row here would be a non-admin credential.
        _log.info("install: installer is not an administrator", extra={"member_id": post.member_id})
        return state("admin_only")
    except BitrixError as exc:
        _log.warning(
            "install: admin proof failed",
            extra={"member_id": post.member_id, "error_code": exc.code},
        )
        return state(_state_for_error(exc))

    app_info = _app_info(batch.get("app"))
    method_error = batch.error("method")
    statistic_get = method_error is None and _method_available(batch.get("method"))
    placement_error = batch.error("placement")

    capabilities: dict[str, Any] = {
        # §4.3 step 3: a missing method does NOT abort the install - the portal is
        # recorded with the flag and the admin gets the explicit `method_missing` state.
        "statistic_get": statistic_get,
        "method_get_error": method_error.code if method_error else None,
        "placement_get_error": placement_error.code if placement_error else None,
        "app_info": app_info,
        "scope": tokens.scope,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    token_status = "ok" if statistic_get else "method_missing"

    # ---- step 6: the only write of a credential in this file (§4.1) -----------------
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
            app_status=str(app_info.get("STATUS")) if app_info.get("STATUS") else tokens.status,
            capabilities=capabilities,
            token_status=token_status,
        )
        kind = "install" if outcome.created else ("reinstall" if outcome.reinstalled else "app_update")
        await record_event(
            session,
            outcome.portal_id,
            kind,
            user_id=admin.user_id,
            details={
                "statistic_get": statistic_get,
                "token_status": outcome.token_status,
                "app_version": app_info.get("VERSION"),
                "app_status": app_info.get("STATUS"),
            },
        )

    _log.info(
        "install: portal stored",
        extra={
            "portal_id": outcome.portal_id,
            "member_id": outcome.member_id,
            "event": kind,
            "token_status": outcome.token_status,
        },
    )

    # ---- step 7: placements and lifecycle events, best effort (§4.3 step 5) ---------
    await _bind_widgets(
        tokens=tokens,
        admin_user_id=admin.user_id,
        post=post,
        outcome_portal_id=outcome.portal_id,
        existing=batch.get("placement"),
        capabilities=capabilities,
        correlation_id=correlation_id,
    )

    # ---- step 8: hand over to the browser; installFinish is the last step -----------
    return render_install(
        request, lang=post.lang, domain=post.domain, protocol_https=post.protocol_https
    )


async def _dispatch_event(
    request: Request, form: Mapping[str, str], *, correlation_id: uuid.UUID
) -> Response:
    """Hand an `event=` body to the events handler (§4.2, §4.9).

    Some cabinets deliver lifecycle events to the installation URL, so the check runs
    before the placement allowlist - an `ONAPPUNINSTALL` posted here must not be
    answered with "bad request".

    The import is function-local because `handlers/events.py` imports this module's
    helpers (one definition of the inbound-log shape, the correlation id and the
    `member_id`-of-a-rejected-body rule): at module scope the two would be a cycle.
    `handlers/events.py` owns its own `rest_log` row, so nothing is logged twice, and it
    catches its own failures - a server-to-server endpoint must never be answered with
    the HTML error page of §4.11.
    """
    from app.handlers.events import handle_event_form

    return await handle_event_form(request, form, correlation_id=correlation_id)


async def _bind_widgets(
    *,
    tokens: TokenResponse,
    admin_user_id: int,
    post: IframePost,
    outcome_portal_id: int,
    existing: Any,
    capabilities: dict[str, Any],
    correlation_id: uuid.UUID,
) -> None:
    """`placement.bind` x4 + `event.bind`, then record the results (§4.3 step 5).

    Never fatal: the app is usable from the left-menu item even with no CRM tab, and
    the settings page exposes a "Re-bind" button that runs exactly this code again.
    Results land in `portals.placements` / `capabilities.event_bind` so support can see
    what a portal actually has bound.
    """
    # Two different handler URLs on purpose: the widgets open the app (§4.4), the
    # lifecycle events must reach the events endpoint (§4.9).
    handler_url = f"{settings.app_base_url}/app/"
    events_url = f"{settings.app_base_url}/events/"
    placements: dict[str, Any] = {"ok": False, "reason": "not_attempted"}
    event_bind: dict[str, Any] = {"ok": False, "reason": "not_attempted"}

    try:
        async with BitrixClient(
            endpoint=tokens.client_endpoint,
            access_token=tokens.access_token,
            portal_id=outcome_portal_id,
            member_id=post.member_id,
            token_user_id=tokens.user_id if tokens.user_id is not None else admin_user_id,
            correlation_id=correlation_id,
        ) as client:
            placements, event_bind = await bind_all(
                client,
                handler_url=handler_url,
                events_url=events_url,
                existing=_placement_codes(existing),
            )
    except Exception:
        # Both binders promise not to raise; this is the belt for the braces, because
        # §4.3 step 5 is explicit that a bind failure never stops an install.
        _log.warning("install: binding step failed", exc_info=True)

    merged = {**capabilities, "event_bind": event_bind}
    try:
        # The one `portals` write outside services/portals.py: it touches neither a
        # token column nor `client_endpoint` (§4.1's credential and endpoint
        # invariants), only the two best-effort result columns of §4.3 step 5.
        async with control_txn() as session:
            await session.execute(
                update(Portal)
                .where(Portal.id == outcome_portal_id)
                .values(placements=_jsonable(placements), capabilities=_jsonable(merged))
            )
    except Exception:
        _log.warning(
            "install: could not record bind results", extra={"portal_id": outcome_portal_id},
            exc_info=True,
        )
