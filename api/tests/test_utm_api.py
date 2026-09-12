"""`POST /api/v1/utm` end to end, against a scripted Bitrix24 (§4.13).

This endpoint holds no rows of its own, spends the portal's REST budget on every open, and
runs on a credential the CALLER posted - so it inherits every property `test_deals_api.py`
asserts, plus three that are its own because it reads TWO entities:

* **The round-trip count.** A warm report is ONE request; a cold one is TWO (the deal page's
  cold path is three, because it also fetches a stage dictionary this page does not have).
  A refactor that re-probed the capability on every render would pass every other assertion
  in this file.
* **Both legs are pinned.** An `own` viewer whose deals were scoped but whose leads were not
  would see the whole company's leads in the denominator of their own conversion rate.
* **A missing lead module DEGRADES, and is distinguishable from an empty one.** "This portal
  has no leads" and "nobody created a lead last month" are different business facts, and a
  marketer would act on them differently. Both are 200s; only one has `available: false`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
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

UTM_PATH: Final[str] = "/api/v1/utm"
TASHKENT: Final[str] = "Asia/Tashkent"

VIEWER: Final[int] = 101
OTHER: Final[int] = 202

FROM: Final[dt.date] = dt.date(2026, 6, 1)
TO: Final[dt.date] = dt.date(2026, 6, 30)

CREATED: Final[str] = "2026-06-10T12:00:00+05:00"


# --- scripted Bitrix24 payloads ----------------------------------------------------------


def me(user_id: int = VIEWER) -> dict[str, Any]:
    return {"ID": str(user_id), "NAME": "Абдурозик"}


def item_fields(*, utm: bool = True, money: bool = True) -> dict[str, Any]:
    """`crm.item.fields`: the names the report infers must be declared before it believes them."""
    fields: dict[str, Any] = {
        "id": {"type": "integer"},
        "createdTime": {"type": "datetime"},
        "stageSemanticId": {"type": "string"},
        "assignedById": {"type": "user"},
        "leadId": {"type": "crm_lead"},
    }
    if utm:
        for name in ("utmSource", "utmMedium", "utmCampaign", "utmContent", "utmTerm"):
            fields[name] = {"type": "string"}
    if money:
        fields["opportunityAccount"] = {"type": "double", "isReadOnly": True}
        fields["accountCurrencyId"] = {"type": "crm_currency", "isReadOnly": True}
    return {"fields": fields}


def legacy_fields(*, utm: bool = True) -> dict[str, Any]:
    """`crm.lead.fields` / `crm.deal.fields`: the UPPER_CASE map, where `UTM_*` is documented."""
    fields: dict[str, Any] = {
        "ID": {"type": "integer"},
        "DATE_CREATE": {"type": "datetime"},
        "STATUS_SEMANTIC_ID": {"type": "string"},
        "STAGE_SEMANTIC_ID": {"type": "string"},
        "ASSIGNED_BY_ID": {"type": "user"},
        "LEAD_ID": {"type": "crm_lead"},
        "OPPORTUNITY": {"type": "double"},
        "CURRENCY_ID": {"type": "crm_currency"},
    }
    if utm:
        for name in ("UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"):
            fields[name] = {"type": "string"}
    return {"fields": fields}


def lead(identifier: int, *, source: str, medium: str = "cpc", semantic: str = "P") -> dict[str, Any]:
    return {
        "id": identifier,
        "createdTime": CREATED,
        "stageSemanticId": semantic,
        "assignedById": VIEWER,
        "utmSource": source,
        "utmMedium": medium,
    }


def deal(
    identifier: int,
    *,
    source: str,
    medium: str = "cpc",
    semantic: str = "P",
    amount: str = "100.00",
    lead_id: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": identifier,
        "createdTime": CREATED,
        "stageSemanticId": semantic,
        "assignedById": VIEWER,
        "utmSource": source,
        "utmMedium": medium,
        "opportunityAccount": amount,
        "accountCurrencyId": "UZS",
        "opportunity": amount,
        "currencyId": "UZS",
    }
    if lead_id is not None:
        row["leadId"] = lead_id
    return row


def sample_leads() -> list[dict[str, Any]]:
    return [
        lead(1, source="google"),
        lead(2, source="google", semantic="S"),
        lead(3, source="yandex", semantic="F"),
        lead(4, source=""),
    ]


def sample_deals() -> list[dict[str, Any]]:
    return [
        deal(10, source="google", semantic="S", amount="1000.00", lead_id=2),
        deal(11, source="yandex", semantic="F", amount="500.00"),
    ]


#: What a well-behaved portal answers the honour probe: nothing is dated the year 2999.
#: One page, not two - there is no unfiltered baseline any more (§4.13).
def honour_ok() -> Page:
    return Page(items=[], total=0)


#: What a portal that IGNORES `>=createdTime` answers: the filter was dropped, so the
#: selection is the whole table and the total is non-zero.
def honour_ignored() -> Page:
    return Page(items=[lead(98, source="probe")], total=7)


def cold_fake(
    *,
    leads: list[dict[str, Any]] | None = None,
    deals: list[dict[str, Any]] | None = None,
    lead_total: int | None = None,
    deal_total: int | None = None,
) -> FakeBitrix:
    """A portal that answers the whole cold path: identity, both field maps, both probes, both pages.

    The `crm.item.list` queue is consumed in batch order: the lead probe then the deal probe
    in the cold batch, then `l0` and `d0` in the scan.
    """
    lead_rows = sample_leads() if leads is None else leads
    deal_rows = sample_deals() if deals is None else deals
    return (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields(), item_fields())
        .on(
            "crm.item.list",
            honour_ok(),
            honour_ok(),
            Page(items=lead_rows, total=lead_total if lead_total is not None else len(lead_rows)),
            Page(items=deal_rows, total=deal_total if deal_total is not None else len(deal_rows)),
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


async def post_utm(
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
        UTM_PATH, params=params, json=body, headers={"Authorization": f"Bearer {token}"}
    )


def row_for(body: dict[str, Any], source: str) -> dict[str, Any]:
    index = body["dimensions"].index("utm_source")
    for row in body["combinations"]:
        if row["k"][index] == source:
            return row
    raise AssertionError(f"{source!r} is not in the report: {body['combinations']}")


def facet_for(body: dict[str, Any], dimension: str) -> dict[str, Any]:
    for facet in body["facets"]:
        if facet["dimension"] == dimension:
            return facet
    raise AssertionError(f"{dimension} is not in the facets: {body['facets']}")


# --- the happy path ------------------------------------------------------------------------


async def test_a_cold_report_costs_two_round_trips_and_unifies_the_funnel(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The capability probe, then both entities' pages. Two, and not one more.

    Three would mean a dictionary phase crept in - the single biggest saving §4.13 makes
    over §4.12, and one nothing else in the response would reveal.
    """
    fake = cold_fake()
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    assert fake.rest_count == 2
    body = response.json()

    assert body["range"]["from"] == FROM.isoformat()
    assert body["scan"]["leads"]["available"] is True
    assert body["scan"]["deals"]["available"] is True
    assert body["scan"]["leads"]["dialect"] == "item-lead"
    assert body["totals"]["leads"]["total"] == 4
    assert body["totals"]["deals"]["total"] == 2

    google = row_for(body, "google")
    assert google["leads"]["total"] == 2
    assert google["leads"]["won"] == 1
    assert google["deals"]["total"] == 1
    assert google["deals"]["won"] == 1
    assert google["deals_from_lead"] == 1
    assert google["amount"] == "1000.00"

    # An untagged lead is a row, never a dropped record: the `none` bucket is declared so
    # the page can label it rather than render an empty cell.
    assert body["buckets"]["none"] == ""
    assert row_for(body, body["buckets"]["none"])["leads"]["total"] == 1


async def test_the_wire_carries_counts_and_decimal_strings_only(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Conversion, average deal and every share are the browser's to compute (§4.13).

    Sending them would put a ratio on the wire that no reader could verify against the
    counts beside it, and would make the page's toggles cost a live CRM scan.
    """
    with patch_httpx(cold_fake()):
        body = (await post_utm(client, session_for(portal))).json()

    for row in [*body["combinations"], body["totals"]]:
        assert set(row) <= {"k", "leads", "deals", "amount", "deals_from_lead"}
        assert isinstance(row["amount"], str)
        for measures in (row["leads"], row["deals"]):
            assert set(measures) == {"total", "in_progress", "won", "lost"}
            assert all(isinstance(value, int) for value in measures.values())
    assert "conversion" not in str(body)


async def test_every_projection_sums_to_the_same_totals(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The invariant the bucket ladder exists to preserve, asserted on the wire itself."""
    with patch_httpx(cold_fake()):
        body = (await post_utm(client, session_for(portal))).json()

    totals = body["totals"]
    for key in ("leads", "deals"):
        assert sum(row[key]["total"] for row in body["combinations"]) == totals[key]["total"]
        for facet in body["facets"]:
            assert sum(v[key]["total"] for v in facet["values"]) == totals[key]["total"], (
                f"{facet['dimension']} does not sum to the total"
            )
    # And each entity's own outcomes add up, everywhere.
    for row in [*body["combinations"], totals]:
        for key in ("leads", "deals"):
            m = row[key]
            assert m["won"] + m["lost"] + m["in_progress"] == m["total"]


async def test_the_day_series_is_dense_across_the_whole_period(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A series that silently drops empty days lies about the shape of a month."""
    with patch_httpx(cold_fake()):
        body = (await post_utm(client, session_for(portal))).json()

    days = body["days"]
    assert len(days) == (TO - FROM).days + 1
    assert days[0]["date"] == FROM.isoformat()
    assert days[-1]["date"] == TO.isoformat()
    assert sum(day["leads"] for day in days) == body["totals"]["leads"]["total"]


async def test_a_warm_report_costs_one_round_trip(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The capability is a property of the BUILD, so the second open skips the probe."""
    with patch_httpx(cold_fake()):
        first = await post_utm(client, session_for(portal))
    assert first.status_code == 200

    warm = (
        FakeBitrix()
        .on("user.current", me())
        .on(
            "crm.item.list",
            Page(items=sample_leads(), total=4),
            Page(items=sample_deals(), total=2),
        )
    )
    with patch_httpx(warm):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    assert warm.rest_count == 1
    body = response.json()
    assert body["scan"]["from_cache"] is True
    assert body["totals"]["leads"]["total"] == 4


# --- the credential ------------------------------------------------------------------------


async def test_a_missing_viewer_token_costs_no_rest_at_all(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """409, never 401: a 401 makes `apiFetch` re-mint OUR jwt and re-post the same stale token."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal), viewer_token=None)
    assert response.status_code == 409
    assert response.json()["code"] == "viewer_token_required"
    assert fake.rest_count == 0


async def test_a_token_naming_somebody_else_is_refused(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The posted token is attacker-supplied; `user.current` must name the jwt's own subject."""
    fake = cold_fake()
    fake.on("user.current", me(OTHER))
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal, user_id=VIEWER))
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_session"


async def test_a_denied_viewer_gets_no_page_at_all(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: `acc='denied'` is a telephony verdict, and it gates every data endpoint."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal, access="denied", is_admin=False))
    assert response.status_code == 403
    assert response.json()["code"] == "no_stats_permission"
    assert fake.rest_count == 0


async def test_an_own_viewer_is_pinned_on_both_entity_legs(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7 on a CRM read, and the half that is easy to forget.

    Pinning the deals but not the leads would put the whole company's leads in the
    denominator of this viewer's own conversion rate - a number that is wrong in a direction
    nothing on screen would explain.
    """
    fake = cold_fake()
    with patch_httpx(fake):
        response = await post_utm(
            client,
            session_for(portal, user_id=VIEWER, access="own", is_admin=False),
            employee=str(OTHER),
        )
    assert response.status_code == 200, response.text
    assert response.json()["filters"]["employees"] == [VIEWER]

    # A batch carries its sub-commands as encoded strings, so the assertion is on those.
    # `%40assignedById` is the FILTER key; the probe commands carry no pin at all, and the
    # plain `assignedById` in every `select` proves nothing about scope.
    pinned = [
        command
        for record in fake.requests
        if record.kind == "batch"
        for command in record.commands.values()
        if "%40assignedById" in command
    ]
    assert len(pinned) == 2, "both the lead page and the deal page must be pinned"
    assert {"entityTypeId=1" in command for command in pinned} == {True, False}
    assert all(f"%40assignedById%5D%5B0%5D={VIEWER}" in command for command in pinned)
    assert not any("%40assignedById%5D%5B1%5D" in command for command in pinned)


async def test_one_portal_never_sees_another(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Two tenants, two scripted portals, and a capability verdict that must not leak.

    The capability cache is keyed by portal for a reason, and a bug that keyed it globally
    would make the second portal skip the probe the first one answered - which is how one
    tenant's build verdict ends up deciding another tenant's report.
    """
    other = await seed_portal()
    try:
        with patch_httpx(cold_fake()):
            first = await post_utm(client, session_for(portal))
        assert first.status_code == 200
        assert first.json()["totals"]["leads"]["total"] == 4

        second_fake = cold_fake(leads=[lead(77, source="direct")], deals=[])
        with patch_httpx(second_fake):
            second = await post_utm(client, session_for(other))

        assert second.status_code == 200, second.text
        # The second portal paid for its own probe: the verdict did not leak across tenants.
        assert second_fake.rest_count == 2
        body = second.json()
        assert body["totals"]["leads"]["total"] == 1
        assert row_for(body, "direct")["leads"]["total"] == 1
    finally:
        await wipe(other.portal_id)
        await delete_portal(other.member_id)


# --- degradation, and the difference between "none" and "off" -------------------------------


async def test_a_portal_without_leads_gets_a_deals_only_report(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Leads can be turned off. That degrades the report; it never refuses it."""
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", Err("ERROR_METHOD_NOT_FOUND"), item_fields())
        .on("crm.lead.fields", Err("ERROR_METHOD_NOT_FOUND"))
        .on(
            "crm.item.list",
            # The lead probe errors too; the deal probe answers normally, then the deal page.
            Err("ERROR_METHOD_NOT_FOUND"),
            honour_ok(),
            Page(items=sample_deals(), total=2),
        )
        .on("crm.lead.list", Err("ERROR_METHOD_NOT_FOUND"))
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan"]["leads"]["available"] is False
    assert body["scan"]["leads"]["reason"] == "method_missing"
    assert body["totals"]["leads"]["total"] == 0
    assert body["totals"]["deals"]["total"] == 2


async def test_a_portal_with_no_leads_this_month_is_not_the_same_thing(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Zero renders as zero with every column present.

    This assertion exists because the two states look identical on a table and mean opposite
    things to the person reading it.
    """
    fake = cold_fake(leads=[], lead_total=0)
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan"]["leads"]["available"] is True
    assert body["scan"]["leads"]["reason"] == ""
    assert body["totals"]["leads"]["total"] == 0


async def test_a_viewer_refused_both_entities_is_refused_the_page(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """An empty table is indistinguishable from "you may see none", and §4.11 forbids that."""
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", Err("ACCESS_DENIED"), Err("ACCESS_DENIED"))
        .on("crm.item.list", Err("ACCESS_DENIED"))
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))
    assert response.status_code == 403
    assert response.json()["code"] == "crm_no_access"


async def test_a_build_with_no_utm_fields_anywhere_is_refused_with_mandated_copy(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Not a 200 with an empty table, and not a 200 with everything in the `none` bucket.

    Both of those are indistinguishable on screen from "nobody used tagged links this
    month", which is a completely different fact that a marketer would act on.
    """
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields(utm=False), item_fields(utm=False))
        .on("crm.lead.fields", legacy_fields(utm=False))
        .on("crm.deal.fields", legacy_fields(utm=False))
        .on("crm.item.list", honour_ok(), honour_ok())
        .on("crm.lead.list", honour_ok())
        .on("crm.deal.list", honour_ok())
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))
    assert response.status_code == 409
    assert response.json()["code"] == "utm_unsupported"


async def test_a_universal_method_without_utm_demotes_to_the_legacy_dialect(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """`UTM_SOURCE` is documented on `crm.lead.fields`; only its camelCase twin is inferred.

    So "the universal method has never heard of these names" is a reason to use the OLD
    method, not a reason to give up on the portal.
    """
    legacy_lead = {
        "ID": "5",
        "DATE_CREATE": CREATED,
        "STATUS_SEMANTIC_ID": "S",
        "ASSIGNED_BY_ID": str(VIEWER),
        "UTM_SOURCE": "yandex",
        "UTM_MEDIUM": "cpc",
    }
    legacy_deal = {
        "ID": "9",
        "DATE_CREATE": CREATED,
        "STAGE_SEMANTIC_ID": "S",
        "ASSIGNED_BY_ID": str(VIEWER),
        "UTM_SOURCE": "yandex",
        "UTM_MEDIUM": "cpc",
        "OPPORTUNITY": "250.00",
        "CURRENCY_ID": "UZS",
    }
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields(utm=False), item_fields(utm=False))
        .on("crm.lead.fields", legacy_fields())
        .on("crm.deal.fields", legacy_fields())
        .on("crm.item.list", honour_ok(), honour_ok())
        .on(
            "crm.lead.list",
            honour_ok(),
            Page(items=[legacy_lead], total=1),
        )
        .on(
            "crm.deal.list",
            honour_ok(),
            Page(items=[legacy_deal], total=1),
        )
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan"]["leads"]["dialect"] == "lead"
    assert body["scan"]["deals"]["dialect"] == "deal"
    assert row_for(body, "yandex")["leads"]["won"] == 1
    # The legacy pair has no `opportunityAccount`, so the report falls back to the native
    # column for the WHOLE report rather than per row.
    assert body["amounts"]["source"] == "native"
    assert body["amounts"]["trusted"] is True


async def test_a_portal_that_ignores_the_period_filter_is_demoted(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The failure that would otherwise be a 200 with a lifetime of records in it.

    A non-empty answer to "created at or past the year 2999" means the key was dropped, and
    on this page a dropped period key does not widen the selection - it deletes it.
    """
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields(), item_fields())
        .on("crm.lead.fields", legacy_fields())
        .on("crm.deal.fields", legacy_fields())
        .on("crm.item.list", honour_ignored(), honour_ignored())
        .on("crm.lead.list", honour_ok(), Page(items=[], total=0))
        .on("crm.deal.list", honour_ok(), Page(items=[], total=0))
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan"]["leads"]["dialect"] == "lead"
    assert body["scan"]["deals"]["dialect"] == "deal"


# --- the refusal ladder ----------------------------------------------------------------------


async def test_a_selection_past_the_cap_is_refused_before_a_single_page(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Refusal, never truncation - and both counts, because the lever differs.

    A portal drowning in leads and one drowning in deals need different advice, so the body
    names each side rather than only their sum.
    """
    fake = cold_fake(lead_total=5000, deal_total=4000)
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "utm_scan_too_large"
    assert body["leads"] == 5000
    assert body["deals"] == 4000
    assert body["total"] == 9000
    assert body["max_total"] == 6000
    assert body["days"] == 30
    # The preflight batch, and nothing after it.
    assert fake.rest_count == 2


async def test_a_period_longer_than_the_page_allows_costs_no_rest(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A live scan of two entities cannot honestly promise a year inside a 30 s timeout."""
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_utm(
            client, session_for(portal), start=dt.date(2026, 1, 1), end=dt.date(2026, 6, 30)
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "period_too_long"
    assert body["max_days"] == 92
    assert fake.rest_count == 0


@pytest.mark.parametrize("facet", ["direction", "result", "line"])
async def test_a_calls_only_facet_is_refused_rather_than_ignored(
    client: httpx.AsyncClient, portal: SeededPortal, facet: str
) -> None:
    """A lead has no direction and no telephony line.

    Serving an answer to a different question is worse than a 400, because nothing on the
    page would say the filter had been dropped.
    """
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal), **{facet: "incoming"})
    assert response.status_code == 400
    assert response.json() == {"code": "unsupported_filter", "filter": facet}
    assert fake.rest_count == 0


async def test_an_unknown_dimension_is_the_callers_bug_and_costs_no_rest(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    fake = FakeBitrix()
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal), dimensions="utm_source,nope")
    assert response.status_code == 400
    assert response.json() == {"code": "bad_dimension", "dimension": "nope"}
    assert fake.rest_count == 0


async def test_dimensions_narrow_the_answer_and_not_the_scan(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The control the page describes as free: same pages fetched, smaller response.

    Asserting the round-trip count is what makes that claim testable rather than a comment.
    """
    with patch_httpx(cold_fake()):
        assert (await post_utm(client, session_for(portal))).status_code == 200

    def warm() -> FakeBitrix:
        return (
            FakeBitrix()
            .on("user.current", me())
            .on(
                "crm.item.list",
                Page(items=sample_leads(), total=4),
                Page(items=sample_deals(), total=2),
            )
        )

    wide_fake, narrow_fake = warm(), warm()
    with patch_httpx(wide_fake):
        wide = (await post_utm(client, session_for(portal))).json()
    with patch_httpx(narrow_fake):
        narrow = (await post_utm(client, session_for(portal), dimensions="utm_source")).json()

    # The same pages, fetched the same number of times, whichever grouping was asked for.
    assert narrow_fake.rest_count == wide_fake.rest_count == 1
    assert narrow["dimensions"] == ["utm_source"]
    assert len(narrow["combinations"][0]["k"]) == 1
    assert len(narrow["combinations"]) <= len(wide["combinations"])
    # Narrowing the fold cannot change a total.
    assert narrow["totals"] == wide["totals"]
    # And the facets stay exact for every dimension the portal stores, selected or not.
    assert {facet["dimension"] for facet in narrow["facets"]} == {
        facet["dimension"] for facet in wide["facets"]
    }
    assert facet_for(narrow, "utm_medium")["selected"] is False


async def test_a_failed_follow_up_page_refuses_rather_than_answering_partially(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A silently dropped page under-counts one UTM row and reads to the user as data."""
    fake = (
        FakeBitrix()
        .on("user.current", me())
        .on("crm.item.fields", item_fields(), item_fields())
        .on(
            "crm.item.list",
            honour_ok(),
            honour_ok(),
            Page(items=sample_leads(), total=120),
            Page(items=sample_deals(), total=2),
            Err("QUERY_LIMIT_EXCEEDED"),
        )
    )
    with patch_httpx(fake):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 503
    assert response.json()["code"] == "query_limit_exceeded"
    assert response.headers.get("Retry-After")


async def test_the_per_viewer_window_refuses_the_thirteenth_report(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Twelve per ten minutes, keyed `(portal, user)`.

    Keyed on the portal alone it would let one person exploring a month lock the page for
    their whole team.
    """
    token = session_for(portal)
    for _ in range(12):
        with patch_httpx(cold_fake()):
            assert (await post_utm(client, token)).status_code == 200

    fake = cold_fake()
    with patch_httpx(fake):
        response = await post_utm(client, token)
    assert response.status_code == 429
    assert response.json()["code"] == "rate_limited"
    assert fake.rest_count == 0


async def test_leads_disappearing_between_two_reports_degrades_rather_than_refusing(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The warm path meets a portal that turned leads off after the verdict was cached.

    Without this the page answers 409 `method_missing` for as long as the capability TTL
    stands - an hour of a refusal where the cold path would have answered a deals-only
    report. The degradation has to be unconditional, and the stale verdict has to be evicted
    so the NEXT report re-probes rather than repeating the discovery.
    """
    with patch_httpx(cold_fake()):
        assert (await post_utm(client, session_for(portal))).status_code == 200

    gone = (
        FakeBitrix()
        .on("user.current", me())
        .on(
            "crm.item.list",
            Err("ERROR_METHOD_NOT_FOUND"),
            Page(items=sample_deals(), total=2),
        )
    )
    with patch_httpx(gone):
        response = await post_utm(client, session_for(portal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scan"]["leads"]["available"] is False
    assert body["scan"]["leads"]["reason"] == "method_missing"
    assert body["totals"]["leads"]["total"] == 0
    assert body["totals"]["deals"]["total"] == 2

    # The stale verdict is gone: the next report pays for a full probe again.
    after = cold_fake()
    with patch_httpx(after):
        assert (await post_utm(client, session_for(portal))).status_code == 200
    assert after.rest_count == 2


async def test_a_period_with_nothing_in_it_does_not_cache_the_verdict(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The trade that paid for deleting the unfiltered baseline, pinned.

    A `>=created: 2999` answering zero only proves the filter held if this viewer can read
    records at all. The scan is where that evidence now comes from, so a selection that
    matched nothing leaves the portal un-cached and the next open cold. That is cheap: the
    cold path is two field maps and two selections that match nothing - where the unfiltered
    baseline it replaced counted the whole lead table and the whole deal table.
    """
    empty = cold_fake(leads=[], deals=[], lead_total=0, deal_total=0)
    with patch_httpx(empty):
        first = await post_utm(client, session_for(portal))
    assert first.status_code == 200, first.text
    assert first.json()["totals"]["leads"]["total"] == 0

    again = cold_fake()
    with patch_httpx(again):
        second = await post_utm(client, session_for(portal))
    assert second.status_code == 200, second.text
    # Cold again: two round trips, and `from_cache` says so.
    assert again.rest_count == 2
    assert second.json()["scan"]["from_cache"] is False

    # And now that the scan has seen rows, the verdict IS remembered.
    warm = (
        FakeBitrix()
        .on("user.current", me())
        .on(
            "crm.item.list",
            Page(items=sample_leads(), total=4),
            Page(items=sample_deals(), total=2),
        )
    )
    with patch_httpx(warm):
        third = await post_utm(client, session_for(portal))
    assert third.status_code == 200, third.text
    assert warm.rest_count == 1
    assert third.json()["scan"]["from_cache"] is True
