"""`GET /api/v1/dashboard` - the whole left-menu page in one round trip (§4.7, §10 step 5).

The dashboard draws four things from one set of rows: a summary strip, calls per day, an
hour x weekday matrix and a per-employee comparison. §10 step 5 requires that they arrive
together and are computed together - "one GROUPING SETS query" - so this module is
deliberately thin: it turns the query string into a validated `CallFilters` and hands the
principal to `services/stats.py`. There is no SQL here, because §4.7 puts every read of
`calls` behind `services/calls_repo.py` and a route that builds its own statement is
exactly how a `scope_filter` gets forgotten.

Three things the route itself is responsible for:

* **the gate** - `require_data_access` answers 403 `no_stats_permission` for an `acc`
  of `denied` before any query runs (§4.7). `scope_filter` refuses a second time inside
  the repository, so this dependency is the polite failure, not the only one;
* **the filter contract** - a `FilterError` is answered as `{"code": …}` with 400, never
  as a silently corrected period (§10 step 5: a range over `MAX_PERIOD_DAYS` "is a 400
  with a machine code, not a silent truncation"). `period_too_long` carries `max_days` so
  the SPA can state the limit rather than guess it;
* **nothing else** - the response is `Cache-Control: no-store` from the middleware
  (§4.10) and carries no message text: every string in it is a machine code and the SPA
  translates (§8).

The timezone is never read from the request. It comes from the JWT's `tz` claim through
`Principal` (§4.6), so a client cannot ask for another portal's day boundaries any more
than it can ask for another portal's rows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.logging import get_logger
from app.security.principal import (
    Principal,
    PrincipalErrorRoute,
    get_principal,
    require_data_access,
)
from app.services.stats import FilterError, load_dashboard, parse_filters

__all__ = ["router"]

#: `PrincipalErrorRoute` is not optional: `include_router` re-registers these routes with
#: THIS router's class, so without it a `PrincipalError` raised by `get_principal` would
#: escape as a 500 instead of `{"code": …}` (§4.7).
router = APIRouter(route_class=PrincipalErrorRoute)

_log = get_logger(__name__)


@router.get("/dashboard", dependencies=[Depends(require_data_access)])
async def dashboard(
    request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """Summary, per-day series, hour x weekday matrix and per-employee comparison (§10 step 5).

    Query string (parsed by `services/stats.py`, shared verbatim with `GET /calls` so the
    two views of the same period cannot drift apart): `from`/`to` as inclusive **local**
    dates - which is what the SPA sends, having resolved its own preset in the viewer's
    zone - or `period=today|7d|30d` when the caller would rather not do date maths, plus
    the repeatable `employee`, `direction`, `result` and `line` facets, `line=builtin`
    being the §3 `rest_app_id IS NULL` case, built-in telephony.

    Everything is aggregated in the viewer's timezone, and the response says which zone
    and which resolved dates that was, so the page can label its own axis honestly.
    """
    try:
        filters = parse_filters(request.query_params, principal)
    except FilterError as exc:
        _log.info(
            "dashboard: filter rejected",
            extra={"portal_id": principal.portal_id, "code": exc.code},
        )
        return exc.as_response()

    payload = await load_dashboard(principal, filters)
    return JSONResponse(payload)
