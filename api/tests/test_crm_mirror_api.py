"""`GET /api/v1/deals` and `GET /api/v1/utm`, the CRM mirror's routes, end to end (§4.14).

What the routes decide, as behaviour:

1. a portal not promoted to `mirror` answers 409 `crm_mirror_unavailable`, the code a page
   that loaded before a mode change reads `/me` again on;
2. only an administrator is served: an `own` viewer's funnels are not stored yet, so they
   keep the live POST;
3. a promoted portal answers a year without a single Bitrix24 request, and says how much of
   the history it holds;
4. an administrator's opt-out wins over everything;
5. `/me.crm.read` tells each viewer which path their pages take.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from app.main import create_app
from app.security.session_token import issue_session
from app.services import crm_repo
from app.sync import crm_lanes
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

pytestmark = pytest.mark.asyncio

ME_PATH: Final[str] = "/api/v1/me"
ADMIN: Final[int] = 7
EMPLOYEE: Final[int] = 9
_YEAR: Final[dict[str, str]] = {"period": "custom", "from": "2025-07-01", "to": "2026-06-30"}
_TOO_LONG: Final[dict[str, str]] = {"period": "custom", "from": "2025-06-29", "to": "2026-06-30"}

Make = Callable[[str], Awaitable[SeededPortal]]


@pytest_asyncio.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://b24.texnobus.test") as http:
        yield http


@pytest_asyncio.fixture()
async def make_portal(app_engine: AsyncEngine) -> AsyncIterator[Make]:
    crm_repo.reset_report_gate()
    created: list[SeededPortal] = []

    async def make(mode: str) -> SeededPortal:
        seeded = await seed_portal(crm_mode=mode)
        created.append(seeded)
        return seeded

    try:
        yield make
    finally:
        for seeded in created:
            async with tenant_txn(seeded.portal_id) as session:
                for table in TENANT_TABLES:
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                        {"pid": seeded.portal_id},
                    )
            await delete_portal(seeded.member_id)


def _headers(portal: SeededPortal, *, admin: bool = True) -> dict[str, str]:
    token = issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=ADMIN if admin else EMPLOYEE,
        adm=admin,
        acc="all" if admin else "own",
        tz="Asia/Tashkent",
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )
    return {"Authorization": f"Bearer {token}"}


async def _history_loaded(portal_id: int) -> None:
    async with control_txn() as session:
        for lane, status in (
            (crm_lanes.DEAL_BACKFILL, crm_lanes.DONE),
            (crm_lanes.LEAD_BACKFILL, crm_lanes.DONE),
            (crm_lanes.DEAL_SWEEP, crm_lanes.ACTIVE),
            (crm_lanes.LEAD_SWEEP, crm_lanes.ACTIVE),
        ):
            await session.execute(
                text(
                    "INSERT INTO crm_lanes (portal_id, lane, status, last_clean_at) "
                    "VALUES (:pid, :lane, :status, now())"
                ),
                {"pid": portal_id, "lane": lane, "status": status},
            )


@pytest.mark.parametrize("path", ["/api/v1/deals", "/api/v1/utm"])
async def test_a_portal_not_promoted_to_the_mirror_answers_409(
    client: httpx.AsyncClient, make_portal: Make, path: str
) -> None:
    portal = await make_portal("sync")

    response = await client.get(path, params=_YEAR, headers=_headers(portal))

    assert response.status_code == 409
    assert response.json() == {"code": "crm_mirror_unavailable"}


@pytest.mark.parametrize("path", ["/api/v1/deals", "/api/v1/utm"])
async def test_an_administrator_reads_a_year_from_the_mirror_without_bitrix24(
    client: httpx.AsyncClient, make_portal: Make, path: str
) -> None:
    portal = await make_portal("mirror")
    await _history_loaded(portal.portal_id)

    # No viewer token and no fake Bitrix24: a request to the portal would fail the report.
    response = await client.get(path, params=_YEAR, headers=_headers(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["range"]["days"] == 365
    assert body["scan"]["rest_requests"] == 0
    assert body["coverage"] == {"window_from": None, "history_complete": True, "progress_pct": 100}
    assert body["stale"] is None


@pytest.mark.parametrize("path", ["/api/v1/deals", "/api/v1/utm"])
async def test_a_period_past_a_year_is_refused_even_from_the_mirror(
    client: httpx.AsyncClient, make_portal: Make, path: str
) -> None:
    portal = await make_portal("mirror")
    await _history_loaded(portal.portal_id)

    response = await client.get(path, params=_TOO_LONG, headers=_headers(portal))

    assert response.status_code == 400
    assert response.json()["code"] == "period_too_long"


@pytest.mark.parametrize("path", ["/api/v1/deals", "/api/v1/utm"])
async def test_a_viewer_who_is_not_an_administrator_keeps_the_live_path(
    client: httpx.AsyncClient, make_portal: Make, path: str
) -> None:
    portal = await make_portal("mirror")
    await _history_loaded(portal.portal_id)

    response = await client.get(path, params=_YEAR, headers=_headers(portal, admin=False))

    assert response.status_code == 409
    assert response.json() == {"code": "crm_mirror_unavailable"}


async def test_an_opt_out_closes_the_mirror_route_too(
    client: httpx.AsyncClient, make_portal: Make
) -> None:
    portal = await make_portal("mirror")
    await _history_loaded(portal.portal_id)
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portals SET crm_mode = 'off', crm_opt_out_at = now(), crm_opt_out_by = :admin "
                "WHERE id = :pid"
            ),
            {"pid": portal.portal_id, "admin": ADMIN},
        )

    response = await client.get("/api/v1/deals", params=_YEAR, headers=_headers(portal))

    assert response.status_code == 409
    assert response.json() == {"code": "crm_analytics_off"}


async def test_me_names_the_path_each_viewer_takes(client: httpx.AsyncClient, make_portal: Make) -> None:
    promoted = await make_portal("mirror")
    syncing = await make_portal("sync")

    admin = (await client.get(ME_PATH, headers=_headers(promoted))).json()["crm"]
    employee = (await client.get(ME_PATH, headers=_headers(promoted, admin=False))).json()["crm"]
    not_yet = (await client.get(ME_PATH, headers=_headers(syncing))).json()["crm"]

    assert admin["read"] == "mirror"
    assert employee["read"] == "live"
    assert not_yet["read"] == "live"
