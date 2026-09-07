"""The per-request principal: who is asking, for which portal, at which access level (§4.7).

Every JSON endpoint under `/api/v1` sits behind `get_principal`. It is the only place
where a request turns from bytes into an identity, and it is deliberately the *whole*
authorisation story for the API surface:

* the bearer JWT of §4.6 carries `pid`, `mid`, `sub`, `adm`, `acc`, `tz`, `lang`, `plc`
  and `ent` - so `portal_id`, `user_id`, the access level and the timezone reach the
  database only from a signed token. §4.1: "No API endpoint accepts `member_id`,
  `portal_id` or `user_id` from the client", and this module is why that is true - it
  never reads any of them from the query string, the body or a header.
* the token is taken from `Authorization: Bearer` and from nowhere else (§4.6). Not a
  cookie: there is no cookie in this design at all, so no cross-site request can ride an
  ambient credential into an endpoint that answers with another tenant's numbers. Not a
  query parameter either: a query string is exactly what proxies, browsers and access
  logs persist, and §4.10 has Caddy stripping them for that reason.
* the `portals` row is loaded on **every** request. That is not a convenience lookup:
  §4.6 names it the revocation path ("`get_principal` loads the `portals` row on every
  request; `status != 'active'` -> 401 `portal_inactive`"). An hour-long JWT is otherwise
  unrevokable, and a portal that uninstalled mid-session must stop answering *now*, not
  when its token happens to expire. The read is also load-bearing for §3: `tenant_txn`
  needs a portal id that exists, and the `member_id` re-check catches a token minted
  against a row that has since been replaced (a restored database, a re-created tenant)
  before that id is used as the RLS key of somebody else's data.

The three failure codes are machine codes, never sentences (§8: the SPA translates):
`invalid_session` (401), `portal_inactive` (401), `no_stats_permission` (403).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.db.models import Portal
from app.db.session import control_txn
from app.logging import get_logger
from app.security.session_token import SessionClaims, TokenError, verify_session

__all__ = [
    "Principal",
    "PrincipalError",
    "PrincipalErrorRoute",
    "get_principal",
    "require_data_access",
]

_log = get_logger(__name__)

#: The scheme of §4.6. Compared case-insensitively because RFC 7235 says the scheme
#: token is case-insensitive and some HTTP clients title-case it.
_BEARER: Final[str] = "bearer"

#: §3 `portals_status_chk`: the only status that may answer an API request.
_ACTIVE: Final[str] = "active"

#: §4.7 collapses Bitrix24's four statistics levels to three; `denied` is the one that
#: may reach `GET /me` and nothing else.
_DENIED: Final[str] = "denied"


@dataclass(frozen=True)
class Principal:
    """One verified caller, for the lifetime of one request.

    Frozen because it is the input to `calls_repo.scope_filter` (§4.7): a mutable
    `access` or `user_id` would turn "the single place the scope is applied" into "the
    single place the scope is applied, unless something re-assigned it first".

    `entity` is the JWT's `ent` claim - `{"t": "DEAL", "id": 123}` for a CRM tab, None
    for the dashboard. `issued_at` is the JWT `iat`, which §4.8 compares against
    `crm_contexts.resolved_at`: a cached context older than this token was resolved by
    somebody else's rights and must not be served.
    """

    portal_id: int
    member_id: str
    user_id: int
    is_admin: bool
    access: str
    timezone: str
    lang: str
    placement: str
    entity: dict[str, Any] | None
    issued_at: int


class PrincipalError(Exception):
    """A request that cannot be served as anyone.

    Carries the machine `code` the SPA switches on and the `http_status` to answer with.
    `api/router.py` turns it into `{"code": ...}` for every route mounted under
    `/api/v1`, so an endpoint never has to remember to catch it.

    The three codes are distinguishable on purpose - they mean different things to the
    SPA, not to an attacker: `invalid_session` -> run the §4.6 exchange (the tab may
    simply have been open for an hour), `portal_inactive` -> the app is gone from this
    portal, stop retrying, `no_stats_permission` -> render the mandated no-access copy.
    Nothing here reveals whether a *portal* exists: a token that does not verify and a
    token for a purged portal both end as 401 with no detail beyond the code.
    """

    def __init__(self, code: str, http_status: int) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)

    def as_response(self) -> JSONResponse:
        """The wire form: a machine code, no message, no detail (§8)."""
        return JSONResponse({"code": self.code}, status_code=self.http_status)


class PrincipalErrorRoute(APIRoute):
    """The route class every `/api/v1` router is built with.

    A `PrincipalError` is raised from a dependency, so no endpoint can catch it and no
    endpoint should have to. Starlette resolves exception handlers on the app, which
    would put the API's error contract in `main.py` - one more place to forget. A route
    class keeps it next to the exception it renders and travels with the router.

    NOTE for the routers added later: `APIRouter.include_router` re-registers a
    sub-router's routes with `route_class_override=type(route)`, i.e. the SUB-router's
    class, not the parent's. Every router mounted under `/api/v1` must therefore be
    constructed as `APIRouter(route_class=PrincipalErrorRoute)` itself.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except PrincipalError as exc:
                return exc.as_response()

        return wrapped


def _bearer_token(request: Request) -> str:
    """Read `Authorization: Bearer <jwt>`; anything else is not a session (§4.6)."""
    header = request.headers.get("authorization")
    if not header:
        raise PrincipalError("invalid_session", 401)
    scheme, _, value = header.partition(" ")
    token = value.strip()
    if scheme.strip().lower() != _BEARER or not token:
        raise PrincipalError("invalid_session", 401)
    return token


def _entity_of(claims: SessionClaims) -> dict[str, Any] | None:
    """Normalise the `ent` claim, or refuse the session.

    `ent` is our own signed data, so a malformed one is a bug on our side rather than an
    attack - but it decides which CRM entity's calls are shown, so "shape it or refuse
    it" is the only safe reading. The entity *type* is checked where it is used
    (`crm_contexts.entity_type` is CHECK-constrained, §3); here only the shape is.
    """
    ent = claims.ent
    if ent is None:
        return None
    entity_type = ent.get("t")
    entity_id = ent.get("id")
    if not isinstance(entity_type, str) or not entity_type:
        raise PrincipalError("invalid_session", 401)
    # bool is an int subclass; `{"id": true}` is malformed, not entity 1.
    if isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id <= 0:
        raise PrincipalError("invalid_session", 401)
    return {"t": entity_type, "id": entity_id}


async def get_principal(request: Request) -> Principal:
    """FastAPI dependency: verified JWT + live `portals` row -> `Principal` (§4.6, §4.7).

    Order matters. The signature is checked first (a forged token never reaches the
    database), then the row is loaded by the token's `pid`, then `member_id` and
    `status` are asserted against it. A row that is missing, renamed to another tenant
    or `uninstalled` is one answer - `portal_inactive`, 401 - because all three mean the
    same thing to the SPA: this session cannot be continued at this portal.

    The `portals` read happens on every request by design (§4.6). FastAPI caches a
    dependency within one request, so an endpoint that also depends on
    `require_data_access` still loads the row exactly once - per request, which is the
    granularity §4.6 specifies, not per dependency.

    `control_txn` is correct here and `tenant_txn` is not: `portals` is a control-plane
    table with no RLS (§3), and the tenant context cannot be set before the portal id is
    known to be real. Endpoints open their own `tenant_txn(principal.portal_id)` for the
    customer data afterwards - `SET LOCAL` dies with its transaction, so this one cannot
    and must not carry it.
    """
    token = _bearer_token(request)
    try:
        claims = verify_session(token)
    except TokenError:
        # Logged without the token and without the reason: an expired tab and a forged
        # signature must look identical from the outside (§4.6), and the token itself is
        # never allowed into a log line.
        _log.info("api: session token rejected")
        raise PrincipalError("invalid_session", 401) from None

    async with control_txn() as session:
        portal = (
            await session.execute(select(Portal).where(Portal.id == claims.pid))
        ).scalar_one_or_none()

    if portal is None or portal.member_id != claims.mid or portal.status != _ACTIVE:
        _log.info(
            "api: session refused, portal not active",
            extra={"portal_id": claims.pid, "user_id": claims.sub},
        )
        raise PrincipalError("portal_inactive", 401)

    return Principal(
        portal_id=portal.id,
        member_id=portal.member_id,
        user_id=claims.sub,
        is_admin=claims.adm,
        access=claims.acc,
        timezone=claims.tz,
        lang=claims.lang,
        placement=claims.plc,
        entity=_entity_of(claims),
        issued_at=claims.iat,
    )


async def require_data_access(
    principal: Principal = Depends(get_principal),
) -> None:
    """Gate every endpoint that reads calls; `GET /me` is the one that omits it (§4.7).

    §4.7: "`acc='denied'`: only `GET /me` answers; every data endpoint returns 403
    `{"code":"no_stats_permission"}`". A denied user is a user Bitrix24 itself refused
    `voximplant.statistic.get` to, so serving them cached rows would be re-publishing
    data the portal already decided they may not see - the moderation issue this whole
    permission path exists to avoid.

    Usable both ways: `Depends(require_data_access)` as a route dependency, or awaited
    directly with a principal already in hand. `calls_repo.scope_filter` refuses a denied
    principal a second time, so forgetting this dependency cannot leak rows - it can only
    produce a 403 from one layer deeper.
    """
    if principal.access == _DENIED:
        raise PrincipalError("no_stats_permission", 403)
