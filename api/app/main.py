"""FastAPI application factory.

WHY the middleware is raw ASGI rather than `BaseHTTPMiddleware`: the request id is a
contextvar (`app.logging.set_request_id`) and must be visible to every log record the
endpoint emits; `BaseHTTPMiddleware` runs the downstream app in a task whose context is
copied at a point that has burned several people, while a plain ASGI callable stays in
the caller's context and cannot lose it.

WHY the access log carries the path but neither the query string nor any header
value (§4.10): the JWT itself only ever rides in a URL fragment, which never reaches
the server — but query strings and `Location` headers are exactly what leaks tokens
into logs, which is also why Caddy is configured to delete both. This process must
not re-create in stdout what the edge was configured to strip.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.engine import dispose_engine
from app.db.session import control_txn
from app.logging import get_logger, set_request_id, setup_logging

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_access_log = get_logger("app.access")
_log = get_logger("app")

# §4.10: every response, not just the API ones — the handoff and state pages are
# rendered per user and per portal and must never sit in a shared cache.
_NO_STORE = (b"cache-control", b"no-store")


class RequestContextMiddleware:
    """Request id, `Cache-Control: no-store`, and the one access-log line per request."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Generated here, never read from an inbound header: an id supplied by a
        # caller is untrusted input that would end up in every log line of the request.
        rid = uuid.uuid4().hex
        set_request_id(rid)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers: list[tuple[bytes, bytes]] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                headers.append(_NO_STORE)
                headers.append((b"x-request-id", rid.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Logged without the body and without headers (§6): an exception on
            # /session/exchange or /portal/reauthorize would otherwise capture tokens.
            _access_log.exception(
                "request failed",
                extra={
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            raise
        else:
            _access_log.info(
                "request",
                extra={
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),  # never scope["query_string"]
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        finally:
            # Cleared rather than left behind: on a keep-alive connection the next
            # request shares this task's context and must not inherit a stale id.
            set_request_id(None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Logging up before anything can log; the connection pool closed on SIGTERM."""
    setup_logging()
    _log.info("api started")
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Build the ASGI app. Called by uvicorn (`app.main:create_app`, factory mode)."""
    app = FastAPI(
        title="Call Analytics",
        version="1.0.0",
        lifespan=lifespan,
        # No interactive docs in a Marketplace app: the surface is Bitrix24-facing
        # form POSTs and a bearer API, and an open schema endpoint is free recon.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness plus a real round trip to Postgres.

        `control_txn` on purpose: a health check must not need a tenant, and the
        control plane is what the worker tick and every handler depend on. The
        design is silent on the failure shape, so this returns the smallest thing
        that a load balancer reads correctly: 503 with no detail.
        """
        try:
            async with control_txn() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            _log.exception("healthz: database unreachable")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # Mount order is fixed by §2 and §4: the three Bitrix24-facing form handlers, then
    # the bearer API. The paths do not overlap, so the order is documentation rather
    # than routing — but it is the order §4 reads in, and it stays that way.
    from app.api.router import router as api_router
    from app.handlers.events import router as events_router
    from app.handlers.install import router as install_router
    from app.handlers.open import router as open_router

    app.include_router(install_router)  # POST /install/
    app.include_router(open_router)  # POST /app/ and POST /settings/
    app.include_router(events_router)  # POST /events/ (§4.9)
    app.include_router(api_router)  # /api/v1/*, all behind get_principal

    _add_handler_get_pages(app)

    return app


#: The four Bitrix24-facing handler URLs (§4.3, §4.4, §4.5, §4.9) — exactly the paths
#: registered in the vendor cabinet, trailing slash included.
_HANDLER_PATHS: Final[tuple[str, ...]] = ("/install/", "/app/", "/settings/", "/events/")

#: The one sentence that tells a person what to do here. It lives in the shared
#: catalogue (§8) like every other string; `has_message` guards the lookup so a stripped
#: bundle renders the state page without a dotted key on it.
_OPEN_FROM_BITRIX24: Final[str] = "common.openFromBitrix24"


def _add_handler_get_pages(app: FastAPI) -> None:
    """`GET` on a handler URL renders a state page instead of Starlette's 405 (§4.11).

    The four handler paths are POST-only: Bitrix24 form-posts the iframe there. But they
    are also ordinary URLs that a human opens in a browser — a moderator walking
    `docs/moderation-checklist.md` step 8, an administrator who pasted the cabinet URL —
    and without a GET route the answer is Starlette's default `{"detail":"Method Not
    Allowed"}`: raw, untranslated JSON rendered inside the Bitrix24 iframe, which §4.11
    counts as a moderation rejection just as a blank frame does.

    So the page every other unusable request gets is rendered here, framed and
    translated from the `DOMAIN` / `PROTOCOL` / `LANG` that Bitrix24 repeats on the
    handler URL (§4.4 step 8) — `render_state` re-validates all three. `bad_request` is
    the §4.11 kind for "opened with parameters we cannot use", which is what a GET is.

    This is a safety net and not a route the product uses. In particular it is NOT how
    the admin Settings page is served: `/settings` (no trailing slash) is the SPA page
    on `web:3000` and only `/settings/` belongs to this app — see the routing comment in
    `docker/Caddyfile.snippet`. If a browser ever reaches this page for a Settings open,
    the ingress has regressed; the user then sees a page rather than raw JSON, and
    `api/tests/test_routing.py` is what stops that regression from shipping.
    """
    # Imported here, next to their only use, in the same spirit as the routers above:
    # `_Hints` is the install handler's reader for the display-only URL parameters and
    # must have exactly one definition (see the note on the imports in handlers/open.py).
    from app.handlers.install import _Hints
    from app.handlers.render import render_state
    from app.i18n import has_message, resolve_locale, t

    async def handler_get(request: Request) -> Response:
        hints = _Hints.from_request(request)
        locale = resolve_locale(hints.lang)
        extra = (
            {"hint": t(locale, _OPEN_FROM_BITRIX24)}
            if has_message(_OPEN_FROM_BITRIX24)
            else None
        )
        response = render_state(
            request,
            "bad_request",
            lang=hints.lang,
            domain=hints.domain,
            protocol_https=hints.protocol_https,
            # A rendered page under an honest status: the endpoint really does accept
            # only POST, and RFC 9110 requires `Allow` on a 405. Browsers render the
            # body of a 405 exactly as they render a 200, so the frame shows the page.
            status_code=405,
            extra=extra,
        )
        response.headers["Allow"] = "POST"
        return response

    for path in _HANDLER_PATHS:
        app.add_api_route(path, handler_get, methods=["GET"], include_in_schema=False)


# Module-level instance so the container can run the conventional
# `uvicorn app.main:app`. Building it opens no sockets and no database connection —
# the engine is lazy (db/engine.py) and the lifespan has not run yet.
app = create_app()
