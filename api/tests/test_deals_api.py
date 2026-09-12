"""`POST /api/v1/deals` end to end, against a scripted Bitrix24 (§4.12).

This endpoint is unlike every other read in the app: it holds no rows of its own, it spends
the portal's REST budget on every open, and it runs on a credential the CALLER posted. So
three properties are asserted here that no other module needs, and each of them is a way the
feature could be wrong while looking right:

* **The round-trip count.** Every case asserts `fake.rest_count`, because the budget the
  owner accepted is measured in HTTP requests and nothing else in the response reveals it.
  A warm report is ONE request; a cold one is three. A refactor that quietly re-reads the
  dictionary on every render would pass every other assertion in this file.
* **Whose token was used.** The viewer posts theirs, and `with_portal_token` must never
  appear on this path - an installer credential would answer a question the viewer was not
  entitled to ask, which is exactly the replay §4.8 refuses for CRM contexts.
* **Refusal rather than truncation.** A selection past the cap is a 400 carrying the count,
  the limit and the period length. A partial report would carry a partial Итого row that a
  supervisor might act on, with nothing on screen saying so.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import tenant_txn
from app.main import create_app
from app.security.session_token import issue_session
from tests.fixtures.bitrix import (
    USER_AUTH,
    Err,
    FakeBitrix,
    Page,
    SeededPortal,
    delete_portal,
    patch_httpx,
    seed_portal,
)

DEALS_PATH: Final[str] = "/api/v1/deals"
TASHKENT: Final[str] = "Asia/Tashkent"

VIEWER: Final[int] = 101
OTHER: Final[int] = 202

FROM: Final[dt.date] = dt.date(2026, 6, 1)
TO: Final[dt.date] = dt.date(2026, 6, 30)

DEFAULT_FUNNEL: Final[int] = 0
GROW: Final[int] = 7


# --- scripted Bitrix24 payloads ----------------------------------------------------------


def me(user_id: int = VIEWER) -> dict[str, Any]:
    return {"ID": str(user_id), "NAME": "Абдурозик"}


def item_fields() -> dict[str, Any]:
    """`crm.item.fields`: the four names the union filter depends on must exist."""
    return {
        "fields": {
            "id": {"type": "integer"},
            "categoryId": {"type": "integer"},
            "stageId": {"type": "crm_status"},
            "assignedById": {"type": "user"},
            "stageSemanticId": {"type": "string"},
            "createdTime": {"type": "datetime"},
            "updatedTime": {"type": "datetime"},
            "movedTime": {"type": "datetime"},
            "closed": {"type": "boolean"},
        }
    }


def categories() -> dict[str, Any]:
    return {
        "categories": [
            {"id": GROW, "name": "Grow Dermozil", "sort": 100, "entityTypeId": 2, "isDefault": "N"},
            {"id": DEFAULT_FUNNEL, "name": "Общая", "sort": 300, "entityTypeId": 2, "isDefault": "Y"},
        ]
    }


def stages(prefix: str) -> list[dict[str, Any]]:
    """One funnel's directory: three working stages, one won, two lost."""
    return [
        {"STATUS_ID": f"{prefix}NEW", "NAME": "Новая", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": f"{prefix}CALL", "NAME": "обзвон", "SORT": "20", "SEMANTICS": None},
        {"STATUS_ID": f"{prefix}THINK", "NAME": "Думает", "SORT": "30", "SEMANTICS": ""},
        {"STATUS_ID": f"{prefix}WON", "NAME": "одобренные", "SORT": "40", "SEMANTICS": "S"},
        {"STATUS_ID": f"{prefix}LOSE", "NAME": "отказы", "SORT": "50", "SEMANTICS": "F"},
        {"STATUS_ID": f"{prefix}DUP", "NAME": "дубль", "SORT": "60", "SEMANTICS": "F"},
    ]


def deal(identifier: int, category: int, stage: str, user: int | None, semantic: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "categoryId": category,
        "stageId": stage,
        "assignedById": user,
        "stageSemanticId": semantic,
    }


def sample_deals() -> list[dict[str, Any]]:
    """Two funnels, two operators and one unassigned deal."""
    return [
        deal(1, GROW, "C7:NEW", VIEWER, "P"),
        deal(2, GROW, "C7:CALL", VIEWER, "P"),
        deal(3, GROW, "C7:WON", VIEWER, "S"),
        deal(4, GROW, "C7:LOSE", OTHER, "F"),
        deal(5, GROW, "C7:DUP", OTHER, "F"),
        deal(6, DEFAULT_FUNNEL, "NEW", VIEWER, "P"),
        deal(7, DEFAULT_FUNNEL, "WON", None, "S"),
    ]


def cold_fake(rows: list[dict[str, Any]] | None = None, *, total: int | None = None) -> FakeBitrix:
    """A portal that answers the whole cold path: identity, dictionary, probe and one page."""
    items = sample_deals() if rows is None else rows
    return (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields())
        .on("crm.category.list", categories())
        # One queue per method, consumed in request order: the batch asks for funnel 7 then
        # funnel 0, in the order `crm.category.list` returned them.
        .on("crm.status.list", stages("C7:"), stages(""))
        .on(
            "crm.item.list",
            # The three honour-probe answers, then the page. `hp0` must be non-empty or the
            # verdict is inconclusive and the dialect is never cached.
            Page(items=[deal(99, GROW, "C7:NEW", VIEWER, "P")], total=1),
            Page(items=[], total=0),
            Page(items=[], total=0),
            Page(items=items, total=total if total is not None else len(items)),
        )
    )


# --- fixtures --------------------------------------------------------------------------


async def wipe(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for table in ("calls", "employees", "crm_contexts"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


@pytest.fixture()
async def client(app_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="https://b24.texnobus.test"
    ) as http_client:
        yield http_client


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    seeded = await seed_portal()
    try:
        yield seeded
    finally:
        await wipe(seeded.portal_id)
        await delete_portal(seeded.member_id)


def session_for(
    portal: SeededPortal,
    *,
    user_id: int = VIEWER,
    access: str = "all",
    is_admin: bool = True,
) -> str:
    return issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=user_id,
        adm=is_admin,
        acc=access,
        tz=TASHKENT,
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )


async def post_deals(
    client: httpx.AsyncClient,
    token: str,
    *,
    viewer_token: str | None = USER_AUTH,
    start: dt.date = FROM,
    end: dt.date = TO,
    **extra: Any,
) -> httpx.Response:
    params: dict[str, Any] = {
        "period": "custom",
        "from": start.isoformat(),
        "to": end.isoformat(),
        **{key: value for key, value in extra.items() if value is not None},
    }
    body: dict[str, Any] = {"access_token": viewer_token} if viewer_token else {}
    return await client.post(
        DEALS_PATH, params=params, json=body, headers={"Authorization": f"Bearer {token}"}
    )


def group_of(body: Mapping[str, Any], category_id: int) -> Mapping[str, Any]:
    for group in body["groups"]:
        if group["category_id"] == category_id:
            return group
    raise AssertionError(f"funnel {category_id} is not in the report: {body['groups']}")


# --- the happy path ------------------------------------------------------------------------


async def test_cold_report_costs_three_round_trips_and_groups_by_funnel(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Identity + dictionary, stages + probe, then the deals. Three, and not one more."""
    fake = cold_fake()
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert fake.rest_count == 3

    # Funnel order follows the portal's own `sort`, not the ids.
    assert [group["category_id"] for group in body["groups"]] == [GROW, DEFAULT_FUNNEL]
    assert group_of(body, GROW)["name"] == "Grow Dermozil"

    grow = group_of(body, GROW)
    assert grow["subtotal"]["total"] == 5
    assert grow["subtotal"]["won"] == 1
    # BOTH failure stages, not just the one called "отказы".
    assert grow["subtotal"]["lost"] == 2
    assert grow["subtotal"]["in_progress"] == 2

    # Columns arrive in the portal's kanban order.
    assert [key.split(":", 1)[1] for key in grow["stage_keys"]] == [
        "C7:NEW",
        "C7:CALL",
        "C7:THINK",
        "C7:WON",
        "C7:LOSE",
        "C7:DUP",
    ]

    assert body["totals"]["total"] == 7
    # The cross-funnel strip carries no per-stage cells on purpose.
    assert "cells" not in body["totals"]
    assert body["scan"]["list_dialect"] == "item"
    assert body["scan"]["deals"] == 7


async def test_the_second_report_is_warm_and_costs_one_round_trip(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The dictionary is cached per viewer; only the deals are re-read."""
    fake = cold_fake()
    token = session_for(portal)
    with patch_httpx(fake):
        first = await post_deals(client, token)
        assert first.status_code == 200, first.text
        before = fake.rest_count

        fake.on("crm.item.list", Page(items=sample_deals(), total=7))
        second = await post_deals(client, token)

    assert second.status_code == 200, second.text
    assert fake.rest_count - before == 1
    assert second.json()["scan"]["from_cache"] is True


async def test_every_row_adds_up_to_its_own_total(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The one discrepancy a reader can see and cannot explain."""
    with patch_httpx(cold_fake()):
        response = await post_deals(client, session_for(portal))
    body = response.json()
    for group in body["groups"]:
        for row in [*group["rows"], group["subtotal"]]:
            assert sum(row["cells"].values()) + row["unknown_stage"] == row["total"]
            assert row["won"] + row["lost"] + row["in_progress"] == row["total"]


async def test_the_unassigned_row_survives(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    with patch_httpx(cold_fake()):
        response = await post_deals(client, session_for(portal))
    rows = group_of(response.json(), DEFAULT_FUNNEL)["rows"]
    assert [row["user_id"] for row in rows][-1] is None


# --- the viewer's own token ------------------------------------------------------------------


async def test_a_missing_body_asks_for_the_viewer_token(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """409, the code the SPA already answers with `BX24.getAuth()` — never 401."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal), viewer_token=None)
    assert response.status_code == 409
    assert response.json()["code"] == "viewer_token_required"
    # Refused before a single REST request was spent.
    assert fake.rest_count == 0


async def test_a_token_belonging_to_somebody_else_is_refused(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """`user.current` must name the viewer whose JWT this is."""
    fake = cold_fake()
    fake.on("user.current", me(OTHER))
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal, user_id=VIEWER))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"


async def test_an_expired_viewer_token_asks_for_a_new_one_rather_than_401(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A 401 would make the SPA re-mint OUR JWT and re-post the same stale Bitrix token."""
    fake = FakeBitrix().on("batch", Err("expired_token", status=401))
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 409
    assert response.json()["code"] == "viewer_token_required"


async def test_a_denied_viewer_gets_no_page_at_all(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal, access="denied", is_admin=False))
    assert response.status_code == 403
    assert response.json()["code"] == "no_stats_permission"
    assert fake.rest_count == 0


async def test_an_own_viewer_is_pinned_to_their_own_deals(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7 on a CRM read: the filter is set server-side, not trusted to the control.

    It also keeps other people's NAMES off the page: the `employees` cache is filled by the
    worker under the installer's credential with `ADMIN_MODE: true`.
    """
    fake = cold_fake()
    with patch_httpx(fake):
        response = await post_deals(
            client,
            session_for(portal, user_id=VIEWER, access="own", is_admin=False),
            employee=str(OTHER),
        )
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["employees"] == [VIEWER]

    # And the wire says so. A batch carries its sub-commands as encoded strings, so the
    # assertion is on those rather than on the batch's own form fields.
    listed = [
        command
        for record in fake.requests
        if record.kind == "batch"
        for command in record.commands.values()
        if command.startswith("crm.item.list")
    ]
    assert listed, "the report must have asked Bitrix24 for deals"
    # `%40assignedById` is the FILTER key; plain `assignedById` also appears in `select`,
    # which every command carries and which proves nothing about scope.
    assigned = [command for command in listed if "%40assignedById" in command]
    assert assigned, "the report must have pinned @assignedById server-side"
    # Exactly one id, and it is the viewer's. A second entry would mean the query-string
    # employee had been honoured alongside it; `202` cannot be matched as a bare substring
    # because every command also carries the year 2026.
    assert all(f"%40assignedById%5D%5B0%5D={VIEWER}" in command for command in assigned)
    assert not any("%40assignedById%5D%5B1%5D" in command for command in assigned)


async def test_no_funnels_reads_as_no_crm_access(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """An empty list is indistinguishable from "you may see none" — never a blank table."""
    fake = cold_fake()
    fake.on("crm.category.list", {"categories": []})
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 403
    assert response.json()["code"] == "crm_no_access"


# --- refusals ---------------------------------------------------------------------------------


async def test_a_selection_past_the_cap_is_refused_with_its_numbers(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Refused before a single page is fetched, and carrying what the user needs to act."""
    fake = cold_fake(total=999_999)
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "deal_scan_too_large"
    assert body["deals"] == 999_999
    assert body["max_deals"] > 0
    assert body["days"] == 30


async def test_a_period_longer_than_the_page_allows_is_refused(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """92 days here, against the 366 the pages that read Postgres offer."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_deals(
            client, session_for(portal), start=dt.date(2025, 1, 1), end=dt.date(2025, 12, 31)
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "period_too_long"
    assert body["max_days"] == 92
    assert fake.rest_count == 0


@pytest.mark.parametrize("facet", ["direction", "result", "line"])
async def test_a_calls_facet_is_refused_rather_than_ignored(
    client: httpx.AsyncClient, portal: SeededPortal, facet: str
) -> None:
    """Silently dropping it would answer a different question and say so nowhere."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal), **{facet: "incoming"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "unsupported_filter"
    assert body["filter"] == facet
    assert fake.rest_count == 0


async def test_an_errored_page_refuses_rather_than_under_counting(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A dropped page under-counts one column and reads to the user as data."""
    fake = cold_fake(total=120)
    # The preflight answers; the follow-up pages fail.
    fake.on("crm.item.list", Err("QUERY_LIMIT_EXCEEDED", status=503))
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code in (502, 503)
    assert response.json()["code"] != "deal_report_ok"


# --- the dialect fallback -------------------------------------------------------------------


async def test_a_build_without_the_universal_method_falls_back(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """An old on-premise portal answers `ERROR_METHOD_NOT_FOUND` and must still work."""
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", Err("ERROR_METHOD_NOT_FOUND", status=400))
        .on("crm.category.list", Err("ERROR_METHOD_NOT_FOUND", status=400))
        .on("crm.dealcategory.list", [{"ID": "0", "NAME": "Общая", "SORT": "10"}])
        .on("crm.dealcategory.stage.list", stages(""))
        .on("crm.deal.list", Page(items=[], total=0))
    )
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 200, response.text
    scan = response.json()["scan"]
    assert scan["list_dialect"] == "deal"
    assert scan["dictionary_dialect"] == "dealcategory"


async def test_a_portal_that_ignores_the_or_filter_is_demoted(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The one failure that would otherwise produce a plausible, wrong report."""
    fake = cold_fake()
    fake.on(
        "crm.item.list",
        # hp0 non-empty, and BOTH future-dated probes come back non-empty: the filter was
        # dropped, so every date bound in the union would have been dropped too.
        Page(items=[deal(99, GROW, "C7:NEW", VIEWER, "P")], total=1),
        Page(items=[deal(98, GROW, "C7:NEW", VIEWER, "P")], total=1),
        Page(items=[deal(97, GROW, "C7:NEW", VIEWER, "P")], total=1),
    )
    fake.on("crm.deal.list", Page(items=[], total=0))
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 200, response.text
    assert response.json()["scan"]["list_dialect"] == "deal"


# --- degraded dictionaries ----------------------------------------------------------------------


async def test_a_funnel_whose_stages_failed_still_reports_its_deals(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Only the column HEADERS are missing; the arithmetic stays correct and says so."""
    fake = cold_fake()
    fake.on("crm.status.list", Err("ACCESS_DENIED", status=403), stages(""))
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    assert response.status_code == 200, response.text
    body = response.json()
    assert GROW in body["scan"]["stage_dictionary_failed"]
    grow = group_of(body, GROW)
    assert grow["subtotal"]["total"] == 5
    assert sum(grow["subtotal"]["cells"].values()) == 5
    # Every column it drew was synthesised from the deals themselves.
    synthesised = {stage["key"]: stage for stage in body["stages"] if not stage["known"]}
    assert set(grow["stage_keys"]) <= set(synthesised)


async def test_a_funnel_with_no_deals_is_dropped_and_counted(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """An all-dash block demands an explanation; an absent one does not."""
    fake = cold_fake(rows=[deal(1, GROW, "C7:NEW", VIEWER, "P")])
    with patch_httpx(fake):
        response = await post_deals(client, session_for(portal))
    body = response.json()
    assert [group["category_id"] for group in body["groups"]] == [GROW]
    assert body["scan"]["funnels_hidden"] == 1
