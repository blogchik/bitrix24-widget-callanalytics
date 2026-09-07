"""`GET /api/v1/filters` - what the filter bar may offer this viewer (§4.7, §10 step 5).

Two lists, both scoped exactly like the data they filter:

* **employees**, from the `employees` cache of §7 - including dismissed users, which the
  SPA greys by `active` rather than hides, because §3 keeps them for the calls that
  remain and a comparison chart that quietly drops a dismissed employee's calls is
  wrong twice over;
* **lines / sources**, the distinct `rest_app_id` of §3, with an explicit built-in
  telephony entry for the NULL bucket.

An `own` principal (§4.7) gets a single-entry employee list - themselves - and
`employee_filter_enabled: false`, which is open question 15's default ("hidden with a
banner") expressed as data rather than as a second copy of the permission rule in the
frontend. The collapse is derived inside `services/stats.py` from
`calls_repo.scope_filter`, so there is still exactly one place that knows what `own`
means.

No SQL lives here (§4.7: every read of `calls` goes through `calls_repo`), and no filter
parsing either: this endpoint answers what the *options* are, while `/dashboard` and
`/calls` answer with a period applied. The counts are therefore over the portal's whole
scoped history, which is what makes the list stable while a user changes periods - a
facet that disappears because today was quiet is a filter bar that fights its user.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.security.principal import (
    Principal,
    PrincipalErrorRoute,
    get_principal,
    require_data_access,
)
from app.services.stats import load_filter_facets

__all__ = ["router"]

#: Required on every `/api/v1` router: `include_router` re-registers routes with the
#: sub-router's own class, so a `PrincipalError` would otherwise not render as
#: `{"code": …}` (§4.7).
router = APIRouter(route_class=PrincipalErrorRoute)


@router.get("/filters", dependencies=[Depends(require_data_access)])
async def filters(principal: Principal = Depends(get_principal)) -> JSONResponse:
    """The employee and line/source facets for this portal and this viewer (§10 step 5).

    Cheap by construction rather than by caching: the employee list is a primary-key
    range of `employees`, and the line list is a grouped walk of this portal's own range
    of `calls_portal_rest_app_idx` (§3) - never an unscoped `SELECT DISTINCT` over
    `calls`. Every API response is `no-store` (§4.10), so "cache-friendly" here means the
    query is cheap enough to repeat, not that anything downstream may keep it.
    """
    return JSONResponse(await load_filter_facets(principal))
