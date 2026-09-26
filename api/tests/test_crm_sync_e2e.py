"""End to end: `sync_portal` mirrors a portal's CRM without touching its call sync (§5.10).

`test_sync_e2e.py`'s filtering fake, extended with `crm.item.list`, `crm.category.list` and
`crm.status.list` that honour what they are asked. The claims:

1. a fresh portal's deals, leads, funnels and stages all land, and assignees get names;
2. an edit reaches the mirror through the sweep;
3. a CRM refusal or a CRM 429 stays on the CRM lanes - the calls still import and the
   portal's own failure and throttle counters never move;
4. `crm_mode = off` and the fleet kill switch each mean no CRM request at all;
5. an uninstall forgets the lanes, so a reinstall mirrors from nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import control_txn, tenant_txn
from app.services.portals import mark_uninstalled
from app.sync import crm_backfill
from tests.conftest import TENANT_TABLES
from tests.fixtures.bitrix import SeededPortal, delete_portal, patch_httpx, seed_portal
from tests.test_sync_e2e import Answer, FilteringBitrix, _count_calls, _drive, _sync_row

pytestmark = pytest.mark.asyncio

CALLS = 60
DEALS = 260
LEADS = 90

_CATEGORIES = [
    {"id": 0, "name": "Main", "sort": 10, "isDefault": "Y"},
    {"id": 3, "name": "B2B", "sort": 20, "isDefault": "N"},
]
_STATUSES: dict[str, list[dict[str, Any]]] = {
    "DEAL_STAGE": [
        {"STATUS_ID": "NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": "WON", "NAME": "Won", "SORT": "20", "SEMANTICS": "S"},
    ],
    "DEAL_STAGE_3": [{"STATUS_ID": "C3:NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None}],
    "STATUS": [
        {"STATUS_ID": "NEW", "NAME": "New", "SORT": "10", "SEMANTICS": None},
        {"STATUS_ID": "CONVERTED", "NAME": "Converted", "SORT": "20", "SEMANTICS": "S"},
    ],
}


def _deal(item_id: int) -> dict[str, Any]:
    funnel = 3 if item_id % 4 == 0 else 0
    return {
        "id": item_id,
        "categoryId": funnel,
        "stageId": "C3:NEW" if funnel else "NEW",
        "stageSemanticId": "P",
        "assignedById": 100 + item_id % 3,
        "createdTime": "2026-08-01T10:00:00+03:00",
        "updatedTime": "2026-08-02T10:00:00+03:00",
        "movedTime": "2026-08-01T10:00:00+03:00",
        "closed": "N",
        "opportunity": "1000.50",
        "currencyId": "UZS",
        "leadId": 0,
        "contactIds": [item_id],
        "companyId": 0,
        "utmSource": "google" if item_id % 2 else "",
        "utmMedium": "",
        "utmCampaign": "",
        "utmContent": "",
        "utmTerm": "",
        "title": "customer content the mirror must never ask for",
    }


def _lead(item_id: int) -> dict[str, Any]:
    return {
        "id": item_id,
        "stageId": "NEW",
        "stageSemanticId": "P",
        "assignedById": 100 + item_id % 3,
        "createdTime": "2026-08-01T10:00:00+03:00",
        "updatedTime": "2026-08-02T10:00:00+03:00",
        "movedTime": "2026-08-01T10:00:00+03:00",
        "contactIds": [],
        "contactId": item_id,
        "companyId": 0,
        "utmSource": "",
        "utmMedium": "",
        "utmCampaign": "",
        "utmContent": "",
        "utmTerm": "",
        "name": "a person's name",
    }


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


class CrmBitrix(FilteringBitrix):
    """The call statistics of `FilteringBitrix` plus a CRM that filters like a real portal."""

    def __init__(self) -> None:
        super().__init__(total=CALLS)
        self.tables: dict[int, dict[int, dict[str, Any]]] = {
            2: {item_id: _deal(item_id) for item_id in range(1, DEALS + 1)},
            1: {item_id: _lead(item_id) for item_id in range(1, LEADS + 1)},
        }
        self.selected: set[str] = set()
        #: S-A.10: `>=updatedTime: v` selects from `v - filter_shift`, as a portal whose token
        #: user sits that far east of the server does.
        self.filter_shift = timedelta(0)
        #: `crm.settings.mode.get`: 1 Classic, 2 Simple (no leads).
        self.crm_mode = 1

    def _dispatch(self, method: str, params: dict[str, str]) -> Any:
        if method == "crm.item.list":
            return self._items(params)
        if method == "crm.item.get":
            row = self.tables[int(params["entityTypeId"])].get(int(params["id"]))
            return Answer(error="NOT_FOUND") if row is None else Answer(result={"item": row})
        if method == "crm.category.list":
            return {"categories": _CATEGORIES}
        if method == "crm.status.list":
            return _STATUSES.get(params.get("filter[ENTITY_ID]", ""), [])
        if method == "crm.settings.mode.get":
            return self.crm_mode
        return super()._dispatch(method, params)

    def _items(self, params: dict[str, str]) -> Any:
        table = self.tables[int(params["entityTypeId"])]
        ids = sorted(table)
        flat = {
            key[7:-1]: value
            for key, value in params.items()
            if key.startswith("filter[") and key.count("[") == 1
        }
        exact = {int(value) for key, value in params.items() if key.startswith("filter[@id][")}
        if ">id" in flat:
            ids = [i for i in ids if i > int(flat[">id"])]
        if "<id" in flat:
            ids = [i for i in ids if i < int(flat["<id"])]
        if ">=id" in flat:
            ids = [i for i in ids if i >= int(flat[">=id"])]
        if exact:
            ids = [i for i in ids if i in exact]
        if ">=updatedTime" in flat:
            since = _instant(flat[">=updatedTime"]) - self.filter_shift
            ids = [i for i in ids if _instant(table[i]["updatedTime"]) >= since]
        if params.get("order[id]", "ASC").upper() == "DESC":
            ids.reverse()
        select = [value for key, value in params.items() if key.startswith("select[")]
        self.selected.update(select)
        page = {"items": [{name: table[i][name] for name in select if name in table[i]} for i in ids[:50]]}
        # `start: -1` switches the count off; `start: 0` is the reconciliation's count probe.
        if int(params.get("start", "0") or 0) >= 0:
            return Answer(result=page, total=len(ids))
        return page


@pytest_asyncio.fixture()
async def crm_portal(app_engine: AsyncEngine) -> AsyncIterator[tuple[SeededPortal, CrmBitrix]]:
    fake = CrmBitrix()
    seeded = await seed_portal(backfill_status="pending", high_id=0, low_id=None, crm_mode="sync")
    fake.member_id = seeded.member_id
    try:
        with patch_httpx(fake):  # type: ignore[arg-type]
            yield seeded, fake
    finally:
        async with tenant_txn(seeded.portal_id) as session:
            for table in TENANT_TABLES:
                await session.execute(
                    text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                    {"pid": seeded.portal_id},
                )
        await delete_portal(seeded.member_id)


async def _lanes(portal_id: int) -> dict[str, dict[str, Any]]:
    async with control_txn() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM crm_lanes WHERE portal_id = :pid"), {"pid": portal_id}
            )
        ).mappings().all()
    return {row["lane"]: dict(row) for row in rows}


async def _count(portal_id: int, sql: str) -> int:
    async with tenant_txn(portal_id) as session:
        return int((await session.execute(text(sql))).scalar_one())


async def _mirror(portal_id: int, *, max_visits: int = 12) -> dict[str, dict[str, Any]]:
    for _ in range(max_visits):
        await _drive(portal_id, visits=1)
        lanes = await _lanes(portal_id)
        if all(lanes.get(name, {}).get("status") == "done" for name in ("deal.backfill", "lead.backfill")):
            return lanes
    raise AssertionError(f"the CRM backfill did not finish in {max_visits} visits: {await _lanes(portal_id)}")


async def test_a_fresh_portal_mirrors_deals_leads_funnels_and_stages(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, fake = crm_portal
    lanes = await _mirror(seeded.portal_id)
    await _drive(seeded.portal_id, visits=1)  # the employee refresh names the assignees

    pid = seeded.portal_id
    assert await _count(pid, "SELECT count(*) FROM crm_items WHERE entity_type_id = 2") == DEALS
    assert await _count(pid, "SELECT count(*) FROM crm_items WHERE entity_type_id = 1") == LEADS
    assert lanes["deal.backfill"]["progress_done"] == DEALS
    assert await _count(pid, "SELECT count(*) FROM crm_funnels") == 3, "Main, B2B and the lead pipeline"
    assert await _count(pid, "SELECT count(*) FROM crm_stages WHERE entity_type_id = 2") == 3
    assert await _count(pid, "SELECT count(*) FROM crm_items WHERE category_id = 3") == DEALS // 4
    assert await _count(pid, "SELECT count(*) FROM crm_items WHERE utm_source = 'google'") == DEALS // 2
    assert await _count(pid, "SELECT count(*) FROM employees WHERE fetched_at IS NOT NULL") == 3
    assert await _count(pid, "SELECT count(*) FROM calls") == CALLS
    assert "title" not in fake.selected and "name" not in fake.selected, "D-3: never select content"


async def test_an_edit_reaches_the_mirror_through_the_sweep(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, fake = crm_portal
    await _mirror(seeded.portal_id)

    # After the fake's server clock (2026-09-07T10:00Z), which became the sweep's watermark.
    fake.tables[2][7].update(stageId="WON", stageSemanticId="S", updatedTime="2026-09-08T10:00:00+03:00")
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE crm_lanes SET due_at = now() - interval '1 second' "
                "WHERE portal_id = :pid AND lane LIKE '%.sweep'"
            ),
            {"pid": seeded.portal_id},
        )
    await _drive(seeded.portal_id, visits=1)

    async with tenant_txn(seeded.portal_id) as session:
        stage = (
            await session.execute(
                text("SELECT stage_id, stage_semantic FROM crm_items WHERE entity_type_id = 2 AND id = 7")
            )
        ).one()
    assert tuple(stage) == ("WON", "S")


async def _sweep_now(portal_id: int) -> None:
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE crm_lanes SET due_at = now() - interval '1 second' "
                "WHERE portal_id = :pid AND lane LIKE '%.sweep'"
            ),
            {"pid": portal_id},
        )


@pytest.mark.parametrize("shift_hours", [2, -2])
async def test_an_edit_arrives_whichever_way_the_portal_shifts_its_date_filter(
    crm_portal: tuple[SeededPortal, CrmBitrix], shift_hours: int
) -> None:
    """S-A.10. East of the server an unmeasured sweep over-reads and used to park; west of it,
    it under-reads and would skip this edit with nothing to show for it."""
    seeded, fake = crm_portal
    fake.filter_shift = timedelta(hours=shift_hours)
    await _mirror(seeded.portal_id)

    # The watermark is the fake's server clock, 2026-09-07T10:00Z. An edit backdated two hours
    # before it is what an eastward shift returns to an unmeasured bound; an edit thirty minutes
    # after it is what a westward shift hides from one.
    fake.tables[2][8].update(updatedTime="2026-09-07T11:00:00+03:00")
    fake.tables[2][7].update(stageId="WON", stageSemanticId="S", updatedTime="2026-09-07T13:30:00+03:00")
    await _sweep_now(seeded.portal_id)
    await _drive(seeded.portal_id, visits=1)

    lane = (await _lanes(seeded.portal_id))["deal.sweep"]
    assert lane["status"] != "parked", lane
    assert lane["cursor"]["shift_seconds"] == shift_hours * 3600
    async with tenant_txn(seeded.portal_id) as session:
        stage = (
            await session.execute(
                text("SELECT stage_id, stage_semantic FROM crm_items WHERE entity_type_id = 2 AND id = 7")
            )
        ).one()
    assert tuple(stage) == ("WON", "S")


async def test_a_sweep_parked_before_the_filter_clock_existed_resumes(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, _fake = crm_portal
    await _mirror(seeded.portal_id)
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE crm_lanes SET status = 'parked', block_reason = 'filter_unsupported', "
                "last_error_code = 'filter_unsupported', cursor = '{}'::jsonb, due_at = now() "
                "WHERE portal_id = :pid AND lane = 'deal.sweep'"
            ),
            {"pid": seeded.portal_id},
        )

    await _drive(seeded.portal_id, visits=1)

    lane = (await _lanes(seeded.portal_id))["deal.sweep"]
    assert lane["status"] != "parked" and lane["block_reason"] is None
    assert lane["cursor"]["shift_seconds"] == 0, "resumed, then measured on the same visit"


async def test_a_deletion_nobody_signalled_reaches_the_mirror_through_reconciliation(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """No change signal exists yet (M6). A deleted deal is found by the daily reconciliation,
    confirmed by crm.item.get NOT_FOUND, and tombstoned: its values gone, its id kept."""
    seeded, fake = crm_portal
    await _mirror(seeded.portal_id)
    del fake.tables[2][9]
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE crm_lanes SET due_at = now() - interval '1 second' "
                "WHERE portal_id = :pid AND lane LIKE '%.reconcile'"
            ),
            {"pid": seeded.portal_id},
        )

    for _ in range(6):
        await _drive(seeded.portal_id, visits=1)
        deleted = await _count(
            seeded.portal_id,
            "SELECT count(*) FROM crm_items WHERE entity_type_id = 2 AND id = 9 AND deleted_at IS NOT NULL",
        )
        if deleted:
            break

    async with tenant_txn(seeded.portal_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT delete_reason, stage_id, contact_ids, opportunity FROM crm_items "
                    "WHERE entity_type_id = 2 AND id = 9"
                )
            )
        ).one()
    assert tuple(row) == ("not_found", None, None, None), "decision 29: every value NULL, id kept"
    live = "SELECT count(*) FROM crm_items WHERE entity_type_id = 2 AND deleted_at IS NULL"
    assert await _count(seeded.portal_id, live) == DEALS - 1, "exactly one deal, and the right one"
    assert fake.method_calls.get("crm.item.get", 0) >= 1, "absence from a list is never proof"
    assert await _count(seeded.portal_id, "SELECT count(*) FROM crm_dirty") == 0


async def test_a_crm_refusal_stays_on_its_lanes_and_the_calls_still_sync(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, fake = crm_portal
    fake.method_errors["crm.item.list"] = "ACCESS_DENIED"
    await _drive(seeded.portal_id, visits=4)

    assert await _count_calls(seeded.portal_id) == CALLS
    sync = await _sync_row(seeded.portal_id)
    assert int(sync["consecutive_failures"]) == 0 and sync["last_error_code"] is None, (
        "a CRM refusal reached the call sync's failure counter"
    )
    lanes = await _lanes(seeded.portal_id)
    for name in ("deal.backfill", "lead.backfill", "deal.sweep", "lead.sweep"):
        assert lanes[name]["block_reason"] == "ACCESS_DENIED"
        assert lanes[name]["failures"] == 0
        assert lanes[name]["paused_until"] > datetime.now(UTC)
    assert lanes["dict"]["last_clean_at"] is not None, "the dictionary is another method"


async def test_a_429_on_crm_item_list_blocks_only_that_method(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, fake = crm_portal
    fake.method_errors["crm.item.list"] = "OPERATION_TIME_LIMIT"
    await _drive(seeded.portal_id, visits=4)

    assert await _count_calls(seeded.portal_id) == CALLS
    sync = await _sync_row(seeded.portal_id)
    assert int(sync["throttle_hits"]) == 0 and int(sync["consecutive_failures"]) == 0
    async with control_txn() as session:
        blocked = (
            await session.execute(
                text(
                    "SELECT blocked_until FROM sync_method_budgets "
                    "WHERE portal_id = :pid AND method = 'crm.item.list'"
                ),
                {"pid": seeded.portal_id},
            )
        ).scalar_one()
    assert blocked > datetime.now(UTC)
    assert fake.method_calls["crm.item.list"] == 1, "a blocked method is skipped, not retried"


async def test_the_lanes_leave_the_live_reports_their_share_of_crm_item_list(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """The live Deals and Sources reports spend the same crm.item.list accumulator. Past 0.6
    of the limit (sweeps) and 0.5 (backfills) the lanes rest until the baskets reset, instead
    of taking the whole allowance and answering those readers operation_time_limit."""
    seeded, fake = crm_portal
    fake.operating_for["crm.item.list"] = 300.0  # past 0.6 x 480 = 288, short of 0.8 x 480
    await _drive(seeded.portal_id, visits=4)

    assert fake.method_calls["crm.item.list"] == 1, "one request learns the accumulator, then rest"
    lanes = await _lanes(seeded.portal_id)
    for name in ("lead.sweep", "deal.backfill", "lead.backfill"):
        assert lanes[name]["paused_until"] > datetime.now(UTC), name
        assert lanes[name]["failures"] == 0 and lanes[name]["block_reason"] is None, name
    sync = await _sync_row(seeded.portal_id)
    assert int(sync["throttle_hits"]) == 0 and int(sync["consecutive_failures"]) == 0
    assert await _count_calls(seeded.portal_id) == CALLS


async def test_crm_off_and_the_kill_switch_each_mean_no_crm_request(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    seeded, fake = crm_portal
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET crm_mode = 'off' WHERE id = :pid"), {"pid": seeded.portal_id}
        )
    await _drive(seeded.portal_id, visits=2)
    assert not [method for method in fake.method_calls if method.startswith("crm.")]

    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET crm_mode = 'sync' WHERE id = :pid"), {"pid": seeded.portal_id}
        )
        await session.execute(text("UPDATE app_flags SET enabled = false WHERE name = 'crm_mirror'"))
    try:
        await _drive(seeded.portal_id, visits=2)
    finally:
        async with control_txn() as session:
            await session.execute(text("UPDATE app_flags SET enabled = true WHERE name = 'crm_mirror'"))
    assert not [method for method in fake.method_calls if method.startswith("crm.")]
    assert await _count_calls(seeded.portal_id) == CALLS


async def test_a_defect_in_a_crm_lane_never_reaches_the_call_sync(
    crm_portal: tuple[SeededPortal, CrmBitrix], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in CRM code is a lane failure, not a portal failure: the calls keep importing."""
    seeded, _fake = crm_portal

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("a bug in the backfill planner")

    monkeypatch.setattr(crm_backfill, "plan", broken)
    await _drive(seeded.portal_id, visits=4)

    assert await _count_calls(seeded.portal_id) == CALLS
    sync = await _sync_row(seeded.portal_id)
    assert int(sync["consecutive_failures"]) == 0 and sync["last_error_code"] is None
    lanes = await _lanes(seeded.portal_id)
    assert lanes["deal.backfill"]["last_error_code"] == "internal_error"
    assert lanes["deal.backfill"]["failures"] >= 1
    assert lanes["deal.sweep"]["last_clean_at"] is not None, "the other lanes carry on"


async def test_an_uninstall_forgets_the_lanes(crm_portal: tuple[SeededPortal, CrmBitrix]) -> None:
    seeded, _fake = crm_portal
    await _drive(seeded.portal_id, visits=1)
    assert await _lanes(seeded.portal_id)

    async with control_txn() as session:
        assert await mark_uninstalled(session, seeded.portal_id)

    assert await _lanes(seeded.portal_id) == {}


async def _stored_crm_mode(portal_id: int) -> Any:
    async with control_txn() as session:
        return (
            await session.execute(
                text("SELECT capabilities -> 'crm_bitrix_mode' FROM portals WHERE id = :pid"),
                {"pid": portal_id},
            )
        ).scalar_one()


async def test_the_portals_crm_mode_is_stored_and_followed_when_it_switches(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """The dictionary lane reads `crm.settings.mode.get` and the reports read it back.

    Portals switch between Classic and Simple CRM - the first Simple portal did, twice in one
    month - so a value read once at install would label it wrongly for weeks.
    """
    seeded, fake = crm_portal
    fake.crm_mode = 2
    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 2

    fake.crm_mode = 1
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE crm_lanes SET due_at = now() - interval '1 minute' "
                "WHERE portal_id = :pid AND lane = 'dict'"
            ),
            {"pid": seeded.portal_id},
        )
    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 1


async def _make_dict_lane(portal_id: int, *, due_in: timedelta) -> None:
    async with control_txn() as session:
        await session.execute(
            text("UPDATE crm_lanes SET due_at = now() + :due WHERE portal_id = :pid AND lane = 'dict'"),
            {"pid": portal_id, "due": due_in},
        )


async def test_a_failed_mode_read_never_replaces_a_known_mode(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """One flaky `crm.settings.mode.get` must not flip a Simple portal to Classic for an hour."""
    seeded, fake = crm_portal
    fake.crm_mode = 2
    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 2

    fake.crm_mode = Answer(error="ACCESS_DENIED")  # type: ignore[assignment]
    await _make_dict_lane(seeded.portal_id, due_in=timedelta(minutes=-1))
    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 2


async def test_a_missing_mode_is_read_before_the_dictionary_is_due(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """After a deploy, or a credential re-store on an old build, the reports must not wait an
    hour for the next dictionary pass to learn the portal runs Simple CRM."""
    seeded, fake = crm_portal
    fake.crm_mode = 2
    await _drive(seeded.portal_id, visits=1)
    async with control_txn() as session:
        await session.execute(
            text("UPDATE portals SET capabilities = capabilities - 'crm_bitrix_mode' WHERE id = :pid"),
            {"pid": seeded.portal_id},
        )
    await _make_dict_lane(seeded.portal_id, due_in=timedelta(hours=1))

    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 2


async def test_the_capability_key_is_spelled_the_same_where_it_is_kept() -> None:
    from app.services import portals
    from app.sync import crm_dict

    assert portals._CRM_MODE_CAPABILITY == crm_dict.CRM_MODE_CAPABILITY


async def test_a_credential_re_store_keeps_the_portals_crm_mode(
    crm_portal: tuple[SeededPortal, CrmBitrix],
) -> None:
    """Install, update and self-heal rebuild `capabilities` from scratch; the worker-owned
    mode must survive that, or a Simple portal reads as Classic until the next pass."""
    from app.bitrix.identity import Identity
    from app.bitrix.oauth import TokenResponse
    from app.services.portals import store_portal_credential
    from tests.fixtures.bitrix import CLIENT_ENDPOINT, DOMAIN

    seeded, fake = crm_portal
    fake.crm_mode = 2
    await _drive(seeded.portal_id, visits=1)
    assert await _stored_crm_mode(seeded.portal_id) == 2

    async with control_txn() as session:
        await store_portal_credential(
            session,
            member_id=seeded.member_id,
            tokens=TokenResponse(
                access_token="new-access",
                refresh_token="new-refresh",
                expires_in=3600,
                expires=None,
                client_endpoint=CLIENT_ENDPOINT,
                server_endpoint=None,
                member_id=seeded.member_id,
                user_id=1,
                status="L",
                scope="crm,telephony,placement,user_brief",
                domain=DOMAIN,
            ),
            admin=Identity(
                user_id=1, is_admin=True, timezone=None, name=None, last_name=None,
                second_name=None, work_position=None, photo_url=None,
            ),
            application_token=None,
            domain=DOMAIN,
            protocol_https=True,
            lang=None,
            app_status="L",
            capabilities={"statistic_get": True},
        )

    assert await _stored_crm_mode(seeded.portal_id) == 2
    async with control_txn() as session:
        caps = (
            await session.execute(
                text("SELECT capabilities FROM portals WHERE id = :pid"), {"pid": seeded.portal_id}
            )
        ).scalar_one()
    assert caps["statistic_get"] is True, "the caller's own keys are still written"
