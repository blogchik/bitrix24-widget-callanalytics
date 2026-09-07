"""The `/api/v1` mount point (§2, §4.7).

One router so `main.py` has exactly one line to include and one prefix to know. The
routers of milestone 5 land here as they are written; their include lines are already
below, commented, because §2 fixes their module names and this file is where the next
agent looks.

Every route mounted under this prefix is built with `PrincipalErrorRoute`, which turns a
`PrincipalError` raised anywhere in a request (almost always from the `get_principal`
dependency) into `{"code": ...}` with the status the error carries. FastAPI's
`include_router` re-registers a sub-router's routes with that sub-router's own route
class, so each sub-router must pass `route_class=PrincipalErrorRoute` itself - the
commented lines below are a reminder of that, and `session.py` is the worked example.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.calls import router as calls_router
from app.api.dashboard import router as dashboard_router
from app.api.filters import router as filters_router
from app.api.portal import router as portal_router
from app.api.record import router as record_router
from app.api.session import router as session_router
from app.security.principal import PrincipalErrorRoute

__all__ = ["router"]

#: §4.6 delivers the bearer token in a URL fragment and §4.10 marks every API response
#: `no-store` (the middleware in `main.py` does that for the whole app), so nothing here
#: is cacheable and nothing here reads a cookie.
router = APIRouter(prefix="/api/v1", route_class=PrincipalErrorRoute)

router.include_router(session_router)  # GET /me, POST /session/exchange
router.include_router(dashboard_router)  # GET /dashboard (one GROUPING SETS query, §10 step 5)
router.include_router(filters_router)  # GET /filters (employees + lines / sources)
router.include_router(calls_router)  # GET /calls, POST /calls/{id}/refresh, /play-url
router.include_router(record_router)  # GET /calls/{id}/record?t=<signed> (§9)
router.include_router(portal_router)  # GET /portal/sync-status, POST /portal/reauthorize
