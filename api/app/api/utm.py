"""`POST /api/v1/utm` - which advertising tags produced leads, deals and money (§4.13).

The page this answers asks the question none of the other three can: not who was on the
phone, and not what happened to the work, but **where the work came from**. A row is one
combination of UTM tags; the columns are leads, deals, outcomes and amount.

It is §4.12's twin, so three things are true here for the reasons stated there, and one is
true for a reason of its own:

* **It is a POST, and it is a read.** The body carries the viewer's own Bitrix24 access
  token, and a live credential must never enter a URL - `rest_log`, any access log in front
  of the app and the browser's history all keep URLs and none of them keeps a body (§6).
* **No SQL and no aggregation live here.** §4.7's rule for `calls` applies to this read as
  well: the route parses, refuses, and delegates. `services/utm_stats.py` owns the scan, the
  budget ladder, the bucket ladder and the shape of the answer.
* **`require_data_access` stays on the route even though `acc` cannot authorise a CRM
  read.** It decides exactly one thing: a user Bitrix24 refused telephony to gets no
  analytics page from this app at all. Which leads and deals they may see is decided by
  Bitrix24, on their own token, inside the service.
* **`dimensions` is parsed here and is NOT a cost control.** It narrows how the answer is
  grouped, never which pages are fetched, so a request that names one dimension costs the
  same REST as one that names five. The page says so in its own copy; the point of parsing
  it at the edge is that an unknown name is the caller's bug and is worth a 400 before a
  single Bitrix24 command is sent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.viewer_token import read_viewer_access_token
from app.db.models import Portal
from app.db.session import control_txn
from app.logging import get_logger, get_request_id
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
    require_data_access,
)
from app.services.stats import FilterError
from app.services.utm_stats import UtmReportError, load_utm_report, parse_utm_filters

__all__ = ["router"]

_log = get_logger(__name__)

#: Required on every `/api/v1` router: `include_router` re-registers routes with the
#: sub-router's own class, so a `PrincipalError` would otherwise not render as
#: `{"code": …}` (§4.7).
router = APIRouter(route_class=PrincipalErrorRoute)


def _correlation_id() -> uuid.UUID:
    """Reuse the request id as `rest_log.correlation_id` (§6), as the handlers do."""
    raw = get_request_id()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return uuid.uuid4()


async def _active_portal(portal_id: int) -> Portal:
    """The tenant row, refused unless it is still installed.

    Read through `control_txn`: `portals` carries no RLS (§3) and this is the one thing the
    endpoint needs before it can talk to Bitrix24 at all - the client endpoint learned from
    the OAuth refresh, never a URL built from a domain.
    """
    async with control_txn() as session:
        portal = (
            await session.execute(select(Portal).where(Portal.id == portal_id))
        ).scalar_one_or_none()
    if portal is None or portal.status != "active":
        raise PrincipalError("portal_inactive", 401)
    return portal


@router.post("/utm", dependencies=[Depends(require_data_access)])
async def utm(request: Request, principal: Principal = Depends(get_principal)) -> JSONResponse:
    """One report: the period's leads and deals, grouped by the tags that brought them in.

    The body is `{"access_token": "<BX24.getAuth().access_token>"}` and nothing else. An
    absent token is answered **409 `viewer_token_required`** - the machine code
    `web/src/lib/calls.ts` already exports and the SPA already knows to answer with
    `BX24.getAuth()`, because `POST /calls/{id}/play-url` has asked for one since §9.

    `require_data_access` on the route decides one thing and one thing only: `acc='denied'`
    is a user Bitrix24 itself refused telephony to, and §4.7 gives them no data endpoint. It
    is NOT a CRM permission - `acc` comes from a `voximplant.statistic.get` probe - and the
    service does not treat it as one.

    Both refusal types are machine codes rather than prose: `FilterError` for a query string
    this endpoint cannot honour (the three `calls` facets, an over-long period, an unknown
    dimension), `UtmReportError` for a selection or a portal that cannot be served.
    """
    try:
        filters, dimensions = parse_utm_filters(request.query_params, principal)
    except FilterError as exc:
        _log.info(
            "utm: filter rejected",
            extra={"portal_id": principal.portal_id, "code": exc.code},
        )
        return exc.as_response()

    # Read before the portal row is loaded: a malformed body is the caller's bug and costs
    # no database round trip to say so.
    viewer_token = await read_viewer_access_token(request)
    if viewer_token is None:
        return JSONResponse({"code": "viewer_token_required"}, status_code=409)

    portal = await _active_portal(principal.portal_id)

    try:
        report = await load_utm_report(
            principal,
            portal,
            filters,
            dimensions,
            viewer_token=viewer_token,
            correlation_id=_correlation_id(),
        )
    except UtmReportError as exc:
        _log.info(
            "utm: report refused",
            extra={
                "portal_id": principal.portal_id,
                "user_id": principal.user_id,
                "code": exc.code,
            },
        )
        return exc.as_response()

    return JSONResponse(report)
