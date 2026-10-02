"""The Settings page's deal-period rule, end to end (owner decision 3, 2026-10-02).

What the owner asked for, as behaviour:

1. only an administrator reads or changes which stages also count deals moved into them or
   modified in a period - a grant never makes anybody one;
2. the choice is made from the portal's own stages, and a key the dictionary does not know is
   refused rather than stored as a rule that silently does nothing;
3. every change is one `portal_events` row naming both sets of stages, and saving the same
   rule twice writes none;
4. nothing changes until an administrator saves a rule: a new column defaults to `{}`;
5. with CRM analytics off there is nothing to choose from, and the page is told so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from app.main import create_app
from app.security.session_token import issue_session
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

pytestmark = pytest.mark.asyncio

RULE_PATH: Final[str] = "/api/v1/portal/deal-period-rule"
ADMIN: Final[int] = 7
EMPLOYEE: Final[int] = 9

#: Portal 1's two working funnels as they stood on 2026-10-02, cut down to what matters here.
_STAGES: Final[tuple[tuple[int, str, str, int, str], ...]] = (
    (16, "C16:NEW", "Новый", 10, "P"),
    (16, "C16:UC_0U9IW2", "Заклад", 80, "P"),
    (16, "C16:WON", "Успешний", 90, "S"),
    (18, "C18:NEW", "Новая", 10, "P"),
    (18, "C18:WON", "Успешний", 60, "S"),
)


@pytest_asyncio.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://b24.texnobus.test") as http:
        yield http


async def _cleanup(seeded: SeededPortal) -> None:
    async with tenant_txn(seeded.portal_id) as session:
        for table in TENANT_TABLES:
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                {"pid": seeded.portal_id},
            )
    await delete_portal(seeded.member_id)


async def _seed_dictionary(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for category_id, name, sort in ((16, "Воронка", 100), (18, "База", 200)):
            await session.execute(
                text(
                    "INSERT INTO crm_funnels (portal_id, entity_type_id, category_id, name, sort) "
                    "VALUES (:pid, 2, :cid, :name, :sort)"
                ),
                {"pid": portal_id, "cid": category_id, "name": name, "sort": sort},
            )
        for category_id, status_id, name, sort, semantic in _STAGES:
            await session.execute(
                text(
                    "INSERT INTO crm_stages (portal_id, entity_type_id, category_id, status_id, "
                    "name, sort, semantic) VALUES (:pid, 2, :cid, :sid, :name, :sort, :semantic)"
                ),
                {
                    "pid": portal_id,
                    "cid": category_id,
                    "sid": status_id,
                    "name": name,
                    "sort": sort,
                    "semantic": semantic,
                },
            )


@pytest_asyncio.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal(crm_mode="mirror")
    try:
        await _seed_dictionary(seeded.portal_id)
        yield seeded
    finally:
        await _cleanup(seeded)


@pytest_asyncio.fixture()
async def portal_without_crm(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal(crm_mode="off")
    try:
        yield seeded
    finally:
        await _cleanup(seeded)


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


async def _stored_rule(portal_id: int) -> Any:
    async with control_txn() as session:
        return (
            await session.execute(
                text("SELECT deal_period_rule FROM portals WHERE id = :pid"), {"pid": portal_id}
            )
        ).scalar_one()


async def _events(portal_id: int) -> list[dict[str, Any]]:
    async with control_txn() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT user_id, details FROM portal_events "
                    "WHERE portal_id = :pid AND kind = 'deal_period_rule_set' ORDER BY id"
                ),
                {"pid": portal_id},
            )
        ).mappings()
        return [dict(row) for row in rows]


async def test_a_new_portal_counts_by_creation_alone(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The column defaults to `{}`: no report changes until an administrator saves a rule."""
    assert await _stored_rule(portal.portal_id) == {}
    response = await client.get(RULE_PATH, headers=_headers(portal))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage_keys"] == []
    assert body["available"] is True
    # Every funnel and its stages, in the portal's own order, to choose from.
    assert [funnel["name"] for funnel in body["funnels"]] == ["Воронка", "База"]
    assert [stage["key"] for stage in body["funnels"][0]["stages"]] == [
        "16:C16:NEW",
        "16:C16:UC_0U9IW2",
        "16:C16:WON",
    ]
    assert body["funnels"][0]["stages"][1] == {"key": "16:C16:UC_0U9IW2", "name": "Заклад", "semantic": "P"}


async def test_an_administrator_names_stages_and_the_change_is_audited_once(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    keys = ["16:C16:WON", "16:C16:UC_0U9IW2"]
    response = await client.post(RULE_PATH, json={"stage_keys": keys}, headers=_headers(portal))
    assert response.status_code == 200, response.text
    # Stored and answered in the dictionary's order, whatever order the page sent.
    assert response.json()["stage_keys"] == ["16:C16:UC_0U9IW2", "16:C16:WON"]
    assert await _stored_rule(portal.portal_id) == {"stage_keys": ["16:C16:UC_0U9IW2", "16:C16:WON"]}

    events = await _events(portal.portal_id)
    assert events == [
        {"user_id": ADMIN, "details": {"from": [], "to": ["16:C16:UC_0U9IW2", "16:C16:WON"]}}
    ]

    # A double click is one decision, not two.
    again = await client.post(RULE_PATH, json={"stage_keys": keys}, headers=_headers(portal))
    assert again.status_code == 200
    assert len(await _events(portal.portal_id)) == 1

    # An empty list returns the portal to creation time alone.
    cleared = await client.post(RULE_PATH, json={"stage_keys": []}, headers=_headers(portal))
    assert cleared.status_code == 200
    assert cleared.json()["stage_keys"] == []
    assert (await _events(portal.portal_id))[-1]["details"]["to"] == []


async def test_a_stage_the_dictionary_does_not_know_is_refused_not_stored(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A rule that silently does nothing is one its author would believe works."""
    response = await client.post(
        RULE_PATH, json={"stage_keys": ["16:C16:WON", "16:C16:GONE"]}, headers=_headers(portal)
    )
    assert response.status_code == 400
    assert response.json()["code"] == "deal_period_rule_invalid"
    assert await _stored_rule(portal.portal_id) == {}
    assert await _events(portal.portal_id) == []


@pytest.mark.parametrize(
    "body",
    [
        {"stage_keys": "16:C16:WON"},
        {"stage_keys": [16]},
        {"stage_keys": ["C16:WON"]},
        {"stage_keys": ["16:C16:WON"], "won": True},
        ["16:C16:WON"],
    ],
)
async def test_a_malformed_body_is_a_bad_request(
    client: httpx.AsyncClient, portal: SeededPortal, body: Any
) -> None:
    response = await client.post(RULE_PATH, json=body, headers=_headers(portal))
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


async def test_only_an_administrator_reads_or_writes_the_rule(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The `adm` claim decides, as on the rest of the Settings page."""
    headers = _headers(portal, user_id=EMPLOYEE, is_admin=False)
    read = await client.get(RULE_PATH, headers=headers)
    write = await client.post(RULE_PATH, json={"stage_keys": ["16:C16:WON"]}, headers=headers)
    assert (read.status_code, write.status_code) == (403, 403)
    assert write.json()["code"] == "admin_only"
    assert await _stored_rule(portal.portal_id) == {}


async def test_without_crm_analytics_there_is_nothing_to_choose_from(
    client: httpx.AsyncClient, portal_without_crm: SeededPortal
) -> None:
    read = await client.get(RULE_PATH, headers=_headers(portal_without_crm))
    assert read.status_code == 200
    assert read.json() == {"stage_keys": [], "available": False, "funnels": []}

    write = await client.post(
        RULE_PATH, json={"stage_keys": []}, headers=_headers(portal_without_crm)
    )
    assert write.status_code == 409
    assert write.json()["code"] == "deal_dictionary_unavailable"
