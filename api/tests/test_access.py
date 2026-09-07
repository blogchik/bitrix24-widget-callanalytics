"""§4.7 — the whole permission model of this app, decided in one pure function.

Bitrix24 has four statistics levels (own / department / any / none) and no REST method
that reports which one you hold. §4.7 collapses them to three by *observation*: an
administrator always has everything, and everyone else is asked to read one row of
`voximplant.statistic.get` filtered to themselves. What comes back — a result, or one
of five different errors — is the entire input to the decision, which is why
`decide_access` takes facts rather than a client and can be driven exhaustively here.

Getting a cell of this matrix wrong is not a cosmetic bug:

* `AccessDenied`/`InvalidCredentials` → `own` would show one employee another's calls;
* `InsufficientScope` → `denied` would tell a customer their *people* lack rights when
  in fact *the app* was installed without `telephony`, sending them to the wrong
  support queue (§4.11 wants the "reinstall and confirm permissions" state);
* `QueryLimitExceeded`/`OperationTimeLimit` → `denied` would bake a transient
  rate-limit into an hour-long JWT, so a portal that hiccuped once locks its users out
  until the token expires. Those two are the reason `AccessDecision` carries a *state*
  instead of a level: there is no honest level to record, so the app must not proceed.

The second half of the module proves the decision is actually enforced at the edge:
`acc='denied'` answers `GET /me` and nothing else, and `scope_filter` — the single
place §4.7 allows a scoping predicate to exist — narrows an `own` read and leaves an
`all` read alone.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    InsufficientScope,
    InvalidCredentials,
    OperationTimeLimit,
    QueryLimitExceeded,
)
from app.db.models import Call
from app.db.session import tenant_txn
from app.main import create_app
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
    require_data_access,
)
from app.security.session_token import issue_session
from app.services.access import decide_access
from tests.conftest import TwoPortals
from tests.fixtures.bitrix import delete_portal, seed_portal

# --- 1. the decision matrix (§4.4 step 5 / §4.7) -------------------------------------

#: `(label, is_admin, probe_error, probe_ran, statistic_get_available, level, state)`.
#: `level` is None where the outcome is a state page: the app does not proceed, so no
#: access level is recorded and asserting one would pin down an arbitrary choice.
_MATRIX: list[tuple[str, bool, BitrixError | None, bool, bool, str | None, str | None]] = [
    (
        "an administrator is 'all' and the probe is never even run",
        True, None, False, True, "all", None,
    ),
    (
        "a non-admin whose probe answered sees their own calls",
        False, None, True, True, "own", None,
    ),
    (
        "ACCESS_DENIED means no statistics permission at all",
        False, AccessDenied("ACCESS_DENIED", http_status=403), True, True, "denied", None,
    ),
    (
        "INVALID_CREDENTIALS is the other spelling of the same refusal",
        False,
        InvalidCredentials("INVALID_CREDENTIALS", http_status=403),
        True, True, "denied", None,
    ),
    (
        "insufficient_scope is the APP's problem, not the user's",
        False,
        InsufficientScope("insufficient_scope", http_status=403),
        True, True, None, "scope",
    ),
    (
        "QUERY_LIMIT_EXCEEDED is transient and must not be frozen into a JWT",
        False,
        QueryLimitExceeded("QUERY_LIMIT_EXCEEDED", http_status=503),
        True, True, None, "retry",
    ),
    (
        "OPERATION_TIME_LIMIT is transient too",
        False,
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        True, True, None, "retry",
    ),
    (
        "a portal without voximplant.statistic.get has nothing to show anyone",
        False, None, False, False, None, "method_missing",
    ),
    (
        "...not even an administrator",
        True, None, False, False, None, "method_missing",
    ),
]


@pytest.mark.parametrize(
    ("label", "is_admin", "probe_error", "probe_ran", "statistic_get", "level", "state"),
    _MATRIX,
    ids=[row[0] for row in _MATRIX],
)
def test_decide_access_matrix(
    label: str,
    is_admin: bool,
    probe_error: BitrixError | None,
    probe_ran: bool,
    statistic_get: bool,
    level: str | None,
    state: str | None,
) -> None:
    """Every branch of §4.4 step 5, as a table."""
    decision = decide_access(
        is_admin=is_admin,
        probe_error=probe_error,
        probe_ran=probe_ran,
        statistic_get_available=statistic_get,
    )
    assert decision.state == state, label
    if level is not None:
        assert decision.level == level, label
    assert decision.level in {"all", "own", "denied"}, (
        "AccessDecision.level is what lands in the JWT `acc` claim, which "
        "session_token.issue_session refuses unless it is all|own|denied."
    )


def test_a_state_decision_never_silently_continues() -> None:
    """The two rate-limit errors and the missing method must STOP the open.

    A decision that returned `('own', None)` for `QUERY_LIMIT_EXCEEDED` would look
    correct in every test that only inspects the level, and would silently downgrade
    an administrator's dashboard the first time a portal was busy.
    """
    for error in (
        QueryLimitExceeded("QUERY_LIMIT_EXCEEDED", http_status=503),
        OperationTimeLimit("OPERATION_TIME_LIMIT", http_status=429),
        InsufficientScope("insufficient_scope", http_status=403),
    ):
        decision = decide_access(
            is_admin=False, probe_error=error, probe_ran=True, statistic_get_available=True
        )
        assert decision.state is not None, f"{error.code} must render a state page (§4.4 step 5)"


def test_an_administrator_is_never_probed() -> None:
    """§4.4 step 4: the probe is "skipped when the user is an administrator".

    Not an optimisation for its own sake: `voximplant.statistic.get` spends the
    portal's shared operating-time budget (§5.6) on every single open, and the review
    called out the per-open admin probe as waste.
    """
    decision = decide_access(
        is_admin=True, probe_error=None, probe_ran=False, statistic_get_available=True
    )
    assert (decision.level, decision.state) == ("all", None)


# --- 2. enforcement at the HTTP edge (§4.7, §4.6) -----------------------------------


def build_probe_app() -> FastAPI:
    """The real app plus one endpoint that is nothing but the two dependencies.

    The data endpoints of §2 (`/dashboard`, `/calls`, `/filters`) land in milestone 5;
    the *rule* they must all obey exists now, and it is exactly "resolve the principal,
    then `require_data_access`". Mounting that pair on the real app - with the real
    middleware and the real `/api/v1` error contract - tests the rule without pretending
    to test a view that has not been written.

    The probe router is built with `PrincipalErrorRoute`, exactly as `api/router.py`
    requires of every router under this prefix (FastAPI re-registers an included
    router's routes with the SUB-router's route class, so inheriting it is not an
    option). A probe wired any other way would be testing a route the app does not have.
    """
    app = create_app()
    probe = APIRouter(prefix="/api/v1", route_class=PrincipalErrorRoute)

    @probe.get("/_probe/data")
    async def _probe_data(principal: Principal = Depends(get_principal)) -> JSONResponse:
        await require_data_access(principal)
        return JSONResponse({"acc": principal.access, "sub": principal.user_id})

    app.include_router(probe)
    return app


@pytest.fixture()
async def api_client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_probe_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def issue_for(member_id: str, portal_id: int, *, acc: str, sub: int = 42) -> str:
    """A live session token for a seeded portal, as `/app/` would have minted it."""
    return issue_session(
        pid=portal_id,
        mid=member_id,
        sub=sub,
        adm=acc == "all",
        acc=acc,
        tz="Asia/Tashkent",
        lang="en",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )


async def get_json(
    client: httpx.AsyncClient, path: str, token: str
) -> tuple[int, dict[str, Any] | None]:
    """One authenticated GET, turning an unhandled `PrincipalError` into a verdict.

    §4.7 specifies the *response* ("403 `{"code":"no_stats_permission"}`"), so a
    `PrincipalError` escaping the app is a missing exception handler, not a test
    failure to be reported as a crash three frames deep.
    """
    try:
        response = await client.get(path, headers=bearer(token))
    except PrincipalError as exc:  # pragma: no cover - only on a wiring bug
        pytest.fail(
            f"PrincipalError({exc.code!r}) escaped the app on {path}. §4.7 requires it to "
            f"render as HTTP {exc.http_status} with a JSON `code`; register an exception "
            "handler for it in create_app()."
        )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def test_a_denied_principal_is_refused_by_a_data_endpoint_but_can_still_ask_who_it_is(
    api_client: httpx.AsyncClient,
) -> None:
    """§4.7: "`acc='denied'`: only `GET /me` answers; every data endpoint returns 403
    `{"code":"no_stats_permission"}`".

    `/me` has to answer, and this is not a nicety: the SPA needs the locale, the
    timezone and the access level to render the mandated "ask your administrator" text
    at all (§4.11). If `/me` 403'd too, the user would get a blank frame instead of the
    sentence the Marketplace review requires.
    """
    seeded = await seed_portal(status="active")
    try:
        token = await issue_for(seeded.member_id, seeded.portal_id, acc="denied")

        status, body = await get_json(api_client, "/api/v1/_probe/data", token)
        assert status == 403, f"a denied principal must not reach data; got {status}"
        assert body is not None and body.get("code") == "no_stats_permission", (
            "§4.7 pins this error code - the SPA switches on it to render the mandated "
            f"no-access text; got {body!r}"
        )

        status, body = await get_json(api_client, "/api/v1/me", token)
        assert status == 200, (
            "GET /me is the one endpoint a denied principal must reach (§4.7); "
            f"got {status}. Is session.router mounted on the /api/v1 router?"
        )
    finally:
        await delete_portal(seeded.member_id)


async def test_an_allowed_principal_reaches_the_same_data_endpoint(
    api_client: httpx.AsyncClient,
) -> None:
    """The control for the test above: otherwise a blanket 403 would pass it."""
    seeded = await seed_portal(status="active")
    try:
        for acc in ("all", "own"):
            token = await issue_for(seeded.member_id, seeded.portal_id, acc=acc)
            status, body = await get_json(api_client, "/api/v1/_probe/data", token)
            assert status == 200, f"acc={acc!r} must reach data endpoints; got {status}"
            assert body is not None and body["acc"] == acc
    finally:
        await delete_portal(seeded.member_id)


async def test_a_request_without_a_session_is_not_a_principal(
    api_client: httpx.AsyncClient,
) -> None:
    """§4.6: no cookie, no ambient identity - a request with no bearer is anonymous,
    and `portal_id`/`user_id` reach the database only from a verified JWT (§4.1)."""
    response = await api_client.get("/api/v1/_probe/data")
    assert response.status_code == 401
    response = await api_client.get("/api/v1/_probe/data", headers=bearer("not.a.jwt"))
    assert response.status_code == 401


async def test_a_session_for_an_inactive_portal_is_dead(api_client: httpx.AsyncClient) -> None:
    """§4.6 revocation: "`get_principal` loads the `portals` row on every request;
    `status != 'active'` → 401 `portal_inactive`."

    This is the whole revocation story - there is no token blacklist. An uninstall must
    therefore take effect on the very next request of a tab that is already open.
    """
    seeded = await seed_portal(status="uninstalled")
    try:
        token = await issue_for(seeded.member_id, seeded.portal_id, acc="all")
        status, body = await get_json(api_client, "/api/v1/_probe/data", token)
        assert status == 401
        assert body is not None and body.get("code") == "portal_inactive"
    finally:
        await delete_portal(seeded.member_id)


# --- 3. scope_filter: the single place a scoping predicate may exist (§4.7) ----------


def scope_filter() -> Any:
    """`services.calls_repo.scope_filter`, or a skip that says who must write it.

    `calls_repo` is the milestone-5 read layer; §4.7 nevertheless names it as the ONE
    place `portal_user_id = principal.user_id` may be appended, so the rule is tested
    here with the rest of the access model rather than with the views.
    """
    module = pytest.importorskip(
        "app.services.calls_repo",
        reason="§4.7 scope_filter lands with the read layer (calls_repo, milestone 5)",
    )
    fn = getattr(module, "scope_filter", None)
    if fn is None:
        pytest.skip("app.services.calls_repo has no scope_filter() yet (§4.7)")
    return fn


def principal_for(portal_id: int, member_id: str, *, acc: str, user_id: int) -> Principal:
    return Principal(
        portal_id=portal_id,
        member_id=member_id,
        user_id=user_id,
        is_admin=acc == "all",
        access=acc,
        timezone="Asia/Tashkent",
        lang="en",
        placement="DEFAULT",
        entity=None,
        issued_at=int(time.time()),
    )


async def count_calls(portal_id: int, clause: Any) -> int:
    """Count `calls` under tenant context with the scoping predicate applied.

    Behavioural rather than textual: a predicate that compiles to the right SQL but is
    attached to the wrong column, or is silently dropped by the query builder, still
    passes a string comparison and still leaks another employee's calls.
    """
    statement = select(func.count()).select_from(Call).where(Call.portal_id == portal_id)
    if clause is not None:
        statement = statement.where(clause)
    async with tenant_txn(portal_id) as session:
        return int((await session.execute(statement)).scalar_one())


async def test_own_narrows_a_read_to_the_viewer_and_all_does_not(two_portals: TwoPortals) -> None:
    """§4.7: `own` appends `portal_user_id = principal.user_id`; `all` appends nothing.

    The seeded portal has four calls split evenly between two employees, so a filter
    that is present but wrong (`!=`, the other user, a no-op `true()`) produces a
    different count than a filter that is right - which a "does the SQL mention the
    column" assertion cannot tell apart.
    """
    build = scope_filter()
    portal = two_portals.a
    viewer, colleague = portal.user_ids

    own = principal_for(portal.portal_id, portal.member_id, acc="own", user_id=viewer)
    everything = principal_for(portal.portal_id, portal.member_id, acc="all", user_id=viewer)

    assert await count_calls(portal.portal_id, None) == portal.calls, "fixture sanity"
    assert await count_calls(portal.portal_id, build(own)) == 2, (
        "an `own` viewer must see exactly their own two calls"
    )
    assert await count_calls(portal.portal_id, build(everything)) == portal.calls, (
        "`all` must add no predicate at all (§4.7)"
    )

    colleagues = principal_for(portal.portal_id, portal.member_id, acc="own", user_id=colleague)
    assert await count_calls(portal.portal_id, build(colleagues)) == 2
    # And the two `own` scopes must not overlap - together they are the whole portal.
    assert (
        await count_calls(portal.portal_id, build(own))
        + await count_calls(portal.portal_id, build(colleagues))
        == portal.calls
    )


async def test_the_own_predicate_names_portal_user_id_and_the_all_predicate_does_not(
    two_portals: TwoPortals,
) -> None:
    """The compiled SQL, as a readable second opinion on the counts above."""
    build = scope_filter()
    portal = two_portals.a
    viewer = portal.user_ids[0]

    own = build(principal_for(portal.portal_id, portal.member_id, acc="own", user_id=viewer))
    assert own is not None, "an `own` principal must carry a predicate (§4.7)"
    rendered = str(own.compile(compile_kwargs={"literal_binds": True}))
    assert "portal_user_id" in rendered
    assert str(viewer) in rendered

    everything = build(
        principal_for(portal.portal_id, portal.member_id, acc="all", user_id=viewer)
    )
    if everything is not None:
        assert "portal_user_id" not in str(
            everything.compile(compile_kwargs={"literal_binds": True})
        ), "`all` must not be scoped to the viewer"
