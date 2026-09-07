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
from typing import Any

from fastapi import FastAPI
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

    # TODO(milestone 5/6): mount the remaining routers. Uncomment as each lands — the
    # import paths and mount order are fixed by §2 and §4, so nothing here is a guess.
    from app.api.router import router as api_router
    from app.handlers.install import router as install_router
    from app.handlers.open import router as open_router

    # from app.handlers.events import router as events_router
    #
    app.include_router(install_router)  # POST /install/
    app.include_router(open_router)  # POST /app/ and POST /settings/
    # app.include_router(events_router)    # POST /events/
    app.include_router(api_router)  # /api/v1/*, all behind get_principal

    return app


# Module-level instance so the container can run the conventional
# `uvicorn app.main:app`. Building it opens no sockets and no database connection —
# the engine is lazy (db/engine.py) and the lifespan has not run yet.
app = create_app()
