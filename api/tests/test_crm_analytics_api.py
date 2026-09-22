"""The administrator's CRM analytics switch, end to end (D-7, docs/crm-mirror-notice.md).

What the owner approved, as behaviour:

1. only an administrator can switch it, and switching twice is one event;
2. off stamps the opt-out, queues the CRM-only purge, forgets the lanes and fences any visit
   already reading CRM;
3. `/me` tells every viewer whether it is on, and shows the notice to administrators who have
   not dismissed it;
4. while it is off the Deals and Sources reports refuse without a single Bitrix24 request;
5. the purge empties the CRM tables of that portal and nothing else, and the tick runs it;
6. on again restarts storage, but only after the purge has finished.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from app.db.tenancy import CRM_TENANT_TABLES
from app.jobs import definitions
from app.main import create_app
from app.security.session_token import issue_session
from app.sync.purge import purge_crm_data
from tests.conftest import TENANT_TABLES, TwoPortals
from tests.fixtures.bitrix import (
    USER_AUTH,
    FakeBitrix,
    SeededPortal,
    delete_portal,
    patch_httpx,
    seed_portal,
)

pytestmark = pytest.mark.asyncio

SWITCH_PATH: Final[str] = "/api/v1/portal/crm-analytics"
DISMISS_PATH: Final[str] = "/api/v1/portal/crm-notice/dismiss"
ME_PATH: Final[str] = "/api/v1/me"
ADMIN: Final[int] = 7
OTHER_ADMIN: Final[int] = 8
EMPLOYEE: Final[int] = 9
#: Not a second copy of the list: `app/db/tenancy.py` is the only one, for the reason its
#: docstring gives. A CRM table added there extends these assertions by itself, which is what
#: catching a missed purge depends on.
_CRM_TABLES: Final[tuple[str, ...]] = CRM_TENANT_TABLES
_PERIOD: Final[dict[str, str]] = {"period": "custom", "from": "2026-06-01", "to": "2026-06-30"}


@pytest_asyncio.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://b24.texnobus.test") as http:
        yield http


@pytest_asyncio.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal(crm_mode="sync")
    try:
        yield seeded
    finally:
        async with tenant_txn(seeded.portal_id) as session:
            for table in TENANT_TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                    {"pid": seeded.portal_id},
                )
        await delete_portal(seeded.member_id)


def _headers(portal: SeededPortal, *, user_id: int = ADMIN, is_admin: bool = True) -> dict[str, str]:
    token = issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=user_id,
        adm=is_admin,
        acc="all" if is_admin else "own",
        tz="Asia/Tashkent",
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )
    return {"Authorization": f"Bearer {token}"}


async def _row(portal_id: int) -> dict[str, Any]:
    async with control_txn() as session:
        row = (
            await session.execute(
                text(
                    "SELECT p.crm_mode, p.crm_opt_out_at, p.crm_opt_out_by, p.crm_purge_pending, "
                    "p.crm_notice_dismissed_by, s.sync_generation "
                    "FROM portals p JOIN portal_sync s ON s.portal_id = p.id WHERE p.id = :pid"
                ),
                {"pid": portal_id},
            )
        ).mappings().one()
    return dict(row)


async def _events(portal_id: int, kind: str) -> int:
    async with control_txn() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM portal_events WHERE portal_id = :pid AND kind = :kind"),
                    {"pid": portal_id, "kind": kind},
                )
            ).scalar_one()
        )


async def _count(portal_id: int, table: str) -> int:
    async with tenant_txn(portal_id) as session:
        return int(
            (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
        )


async def test_an_administrator_turns_it_off_and_the_mirror_is_queued_for_deletion(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    async with control_txn() as session:
        await session.execute(
            text("INSERT INTO crm_lanes (portal_id, lane) VALUES (:pid, 'deal.backfill')"),
            {"pid": portal.portal_id},
        )
    before = await _row(portal.portal_id)

    response = await client.post(SWITCH_PATH, json={"enabled": False}, headers=_headers(portal))

    assert response.status_code == 200
    assert response.json() == {
        "analytics_enabled": False,
        "mode": "off",
        "opted_out_at": response.json()["opted_out_at"],
        "purge_pending": True,
    }
    after = await _row(portal.portal_id)
    assert after["crm_mode"] == "off" and after["crm_opt_out_at"] is not None
    assert after["crm_opt_out_by"] == ADMIN and after["crm_purge_pending"] is True
    assert after["sync_generation"] == before["sync_generation"] + 1, (
        "a visit already reading CRM must not write a row after the purge"
    )
    async with control_txn() as session:
        lanes = (
            await session.execute(
                text("SELECT count(*) FROM crm_lanes WHERE portal_id = :pid"), {"pid": portal.portal_id}
            )
        ).scalar_one()
    assert lanes == 0

    again = await client.post(SWITCH_PATH, json={"enabled": False}, headers=_headers(portal))
    assert again.status_code == 200
    assert await _events(portal.portal_id, "crm_analytics_off") == 1, "a double click is one event"


async def test_only_an_administrator_may_switch_it_and_only_with_a_boolean(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    refused = await client.post(
        SWITCH_PATH,
        json={"enabled": False},
        headers=_headers(portal, user_id=EMPLOYEE, is_admin=False),
    )
    assert refused.status_code == 403 and refused.json()["code"] == "admin_only"

    malformed = await client.post(SWITCH_PATH, json={"enabled": "no"}, headers=_headers(portal))
    assert malformed.status_code == 400 and malformed.json()["code"] == "bad_request"

    assert (await _row(portal.portal_id))["crm_opt_out_at"] is None


async def test_me_tells_every_viewer_and_only_undismissed_administrators_see_the_notice(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    admin = (await client.get(ME_PATH, headers=_headers(portal))).json()["crm"]
    assert admin["analytics_enabled"] is True and admin["notice_visible"] is True
    assert admin["opted_out_at"] is None and admin["purge_pending"] is False

    employee = (
        await client.get(ME_PATH, headers=_headers(portal, user_id=EMPLOYEE, is_admin=False))
    ).json()["crm"]
    assert employee == {"analytics_enabled": True, "mode": "sync", "read": "live", "notice_visible": False}

    for _ in range(2):
        dismissed = await client.post(DISMISS_PATH, headers=_headers(portal))
        assert dismissed.status_code == 200
    assert (await _row(portal.portal_id))["crm_notice_dismissed_by"] == [ADMIN]
    assert (await client.get(ME_PATH, headers=_headers(portal))).json()["crm"]["notice_visible"] is False
    other = await client.get(ME_PATH, headers=_headers(portal, user_id=OTHER_ADMIN))
    assert other.json()["crm"]["notice_visible"] is True, "the dismissal is per administrator"

    await client.post(SWITCH_PATH, json={"enabled": False}, headers=_headers(portal, user_id=OTHER_ADMIN))
    off = (await client.get(ME_PATH, headers=_headers(portal, user_id=EMPLOYEE, is_admin=False))).json()
    assert off["crm"]["analytics_enabled"] is False
    assert (await client.get(ME_PATH, headers=_headers(portal, user_id=OTHER_ADMIN))).json()["crm"][
        "notice_visible"
    ] is False


async def test_the_reports_refuse_without_asking_bitrix24_while_it_is_off(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    await client.post(SWITCH_PATH, json={"enabled": False}, headers=_headers(portal))

    fake = FakeBitrix()
    with patch_httpx(fake):
        for path in ("/api/v1/deals", "/api/v1/utm"):
            response = await client.post(
                path, params=_PERIOD, json={"access_token": USER_AUTH}, headers=_headers(portal)
            )
            assert response.status_code == 409, path
            assert response.json()["code"] == "crm_analytics_off"

    assert fake.rest_count == 0 and fake.oauth_count == 0


async def test_on_again_restarts_storage_once_the_purge_has_finished(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    await client.post(SWITCH_PATH, json={"enabled": False}, headers=_headers(portal))

    response = await client.post(SWITCH_PATH, json={"enabled": True}, headers=_headers(portal))

    assert response.status_code == 200
    assert response.json()["analytics_enabled"] is True and response.json()["mode"] == "sync"
    row = await _row(portal.portal_id)
    assert row["crm_opt_out_at"] is None and row["crm_mode"] == "sync"
    assert row["crm_purge_pending"] is True, "storage waits for the purge to finish first"
    assert await _events(portal.portal_id, "crm_analytics_on") == 1

    outcome = await purge_crm_data(portal.portal_id)
    assert outcome.verified_empty
    assert (await _row(portal.portal_id))["crm_purge_pending"] is False


async def test_the_crm_purge_empties_only_that_portals_crm_tables(two_portals: TwoPortals) -> None:
    a, b = two_portals.a, two_portals.b
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET crm_purge_pending = true WHERE id = :pid"), {"pid": a.portal_id}
        )

    outcome = await purge_crm_data(a.portal_id)

    assert outcome.verified_empty and outcome.total_deleted == a.crm_rows
    for table in _CRM_TABLES:
        assert await _count(a.portal_id, table) == 0, table
    assert await _count(a.portal_id, "calls") == a.calls, "turning CRM analytics off keeps the calls"
    assert await _count(a.portal_id, "employees") == a.employees
    assert await _count(a.portal_id, "crm_contexts") == a.crm_contexts
    assert sum([await _count(b.portal_id, table) for table in _CRM_TABLES]) == b.crm_rows
    assert (await _row(a.portal_id))["crm_purge_pending"] is False
    assert await _events(a.portal_id, "crm_purge_done") == 1


async def test_the_tick_spends_its_purge_slot_on_a_pending_crm_purge(two_portals: TwoPortals) -> None:
    a = two_portals.a
    async with control_txn() as session:
        # Nothing else due: this tick has one job to find.
        await session.execute(
            text(
                "UPDATE portal_sync SET next_run_at = now() + interval '1 hour' "
                "WHERE portal_id IN (:a, :b)"
            ),
            {"a": a.portal_id, "b": two_portals.b.portal_id},
        )
        await session.execute(
            text("UPDATE portals SET crm_purge_pending = true WHERE id = :pid"), {"pid": a.portal_id}
        )

    await definitions.tick()
    await asyncio.gather(*list(definitions._purging.values()))

    assert sum([await _count(a.portal_id, table) for table in _CRM_TABLES]) == 0
    assert await _count(a.portal_id, "calls") == a.calls
