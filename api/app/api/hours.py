"""`GET /api/v1/hours` - talk time per employee, per local day, per hour.

The page this answers asks one question the dashboard cannot: *when* is a given person on
the phone, and for how long. The dashboard's heatmap is the whole portal and counts calls;
the employee chart is per person and has no clock. This is the cell where the two meet.

Three things about it are decisions rather than mechanics:

* **It is its own endpoint, not a fourth grouping set on `/dashboard`.** That query runs on
  every open of the left-menu page, and a `(portal_user_id, hour)` set would make it
  aggregate up to `_EMPLOYEE_CAP` x 24 rows that nothing on the dashboard draws.
* **A row is one (employee, local day)**, not one employee. The period is read across days
  precisely so the days can be compared - "she is busy at ten on Mondays" is not a fact the
  same numbers summed over a month can state.
* **The two numbers in a cell describe different sets of calls.** Talk time is
  `call_duration` over answered calls, the count is every call in that hour, and the gap
  between them is the point: forty-three attempts and twenty minutes of conversation is a
  sentence about an hour that one number cannot say.

No SQL lives here (§4.7: every read of `calls` goes through `calls_repo`) and no filter
parsing either - `services/stats.py` owns both, so this page filters the same rows by the
same rules as the table and the charts.
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
from app.services.stats import FilterError, load_hours, parse_filters

__all__ = ["router"]

_log = get_logger(__name__)

#: Required on every `/api/v1` router: `include_router` re-registers routes with the
#: sub-router's own class, so a `PrincipalError` would otherwise not render as
#: `{"code": …}` (§4.7).
router = APIRouter(route_class=PrincipalErrorRoute)


@router.get("/hours", dependencies=[Depends(require_data_access)])
async def hours(request: Request, principal: Principal = Depends(get_principal)) -> JSONResponse:
    """One grid: the selected employees, a row per day, twenty-four cells per row.

    The employee filter is the same repeatable `employee` parameter every other read
    takes, so "several people at once" needed nothing here - `parse_filters` has always
    answered `?employee=1&employee=2` with an `IN` list, and only the control was missing.

    A malformed filter is answered as a 400 machine code, exactly as `/dashboard` and
    `/calls` answer it, and never as an authentication failure: `services/stats.py` owns
    that distinction.
    """
    try:
        filters = parse_filters(request.query_params, principal)
    except FilterError as exc:
        _log.info(
            "hours: filter rejected",
            extra={"portal_id": principal.portal_id, "code": exc.code},
        )
        return exc.as_response()

    return JSONResponse(await load_hours(principal, filters))
