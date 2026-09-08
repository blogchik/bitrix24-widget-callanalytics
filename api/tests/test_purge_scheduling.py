"""§5.9 purge *scheduling*: who the tick picks, and what a reinstall does to a purge.

`test_purge.py` proves the deletes themselves are honest (RLS fails silently closed, so
"the DELETE succeeded" is not evidence). These tests cover the two seams around that
job, both of which survived the 336-test baseline because nothing exercised `tick()`'s
purge branch or a purge running against a tenant that is live again:

1. **The one purge slot must not be spent on a portal the job will only skip.** §5.9 (b)
   dispatches `SELECT id FROM portals WHERE purge_pending LIMIT 1` and `sync/purge.py`
   paces a failed purge for `INCOMPLETE_RETRY_SECONDS`. If the selection cannot see that
   pacing, the lowest-id parked portal is re-picked every 15 s and no other uninstalled
   tenant is ever purged - their `calls`/`employees`/`crm_contexts` stay on disk (brief
   rule 7) and, because `lease.py` refuses to lease a `purge_pending` portal, a tenant
   that reinstalled behind the parked one never syncs and shows an empty dashboard.
2. **A reinstall during cleanup must converge.** §4.4 step 3 blesses `purge_pending` on
   an ACTIVE portal, and the /app/ open handler writes `employees` through
   `upsert_viewer` under tenant context with no purge check. If that row makes the
   purge's final count non-zero and the purge calls it `purge_incomplete`, the portal
   keeps `purge_pending`, is never leased, never syncs - and every open during the next
   attempt re-arms the same hour. Nothing errors; the dashboard is simply empty forever.

Everything runs against the compose Postgres as the NOBYPASSRLS `ca_app` role, because
that is the only place these interactions are real.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from app.bitrix.identity import Identity
from app.db.session import control_txn, tenant_txn
from app.jobs import definitions
from app.services.employees import upsert_viewer
from app.sync import purge as purge_module
from app.sync.lease import Fence
from app.sync.purge import purge_portal_data
from tests.conftest import TENANT_TABLES, PortalFixture, TwoPortals

pytestmark = pytest.mark.asyncio


async def _no_leases(limit: int, owner: str) -> list[Fence]:
    """Stub for `acquire_leases`: these tests are about the tick's purge branch only.

    Left unstubbed, the sync half of `tick()` would lease and dispatch whatever else the
    database happens to hold and make real HTTP calls.
    """
    return []


async def _arm_uninstalled(portal_id: int, *, purge_bodies: bool = False) -> None:
    """The column effects of §4.9 rule 3 that matter here."""
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portals SET status = 'uninstalled', uninstalled_at = now(), "
                "purge_pending = true, purge_bodies = :bodies WHERE id = :pid"
            ),
            {"pid": portal_id, "bodies": purge_bodies},
        )


async def _reinstall(portal_id: int) -> None:
    """§4.4 step 3 / §4.3 step 4: the tenant is back, `purge_pending` deliberately stays.

    `store_portal_credential` writes `status='active'` and `uninstalled_at=NULL` and does
    NOT clear `purge_pending` - the purge owns that flag (§5.9), because rule 7 still
    requires the pre-uninstall rows to go. This helper writes exactly that much.
    """
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portals SET status = 'active', uninstalled_at = NULL "
                "WHERE id = :pid"
            ),
            {"pid": portal_id},
        )


async def _arm_cooldown(portal_id: int, *, age_seconds: int = 0) -> None:
    """What `_finish` leaves behind after a `purge_incomplete` (`purge_pending` kept)."""
    async with control_txn() as session:
        await session.execute(
            text(
                "UPDATE portal_sync SET last_error_code = 'purge_incomplete', "
                "last_error_text = 'seeded by the test', "
                "last_error_at = now() - make_interval(secs => :age) WHERE portal_id = :pid"
            ),
            {"pid": portal_id, "age": age_seconds},
        )


async def _rows(portal_id: int) -> int:
    """Total customer rows visible under THIS tenant's context - the only honest count."""
    total = 0
    async with tenant_txn(portal_id) as session:
        for table in TENANT_TABLES:
            total += int(
                (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                        {"pid": portal_id},
                    )
                ).scalar_one()
            )
    return total


async def _state(portal_id: int) -> dict[str, Any]:
    async with control_txn() as session:
        row = (
            await session.execute(
                text(
                    "SELECT p.status, p.purge_pending, s.last_error_code "
                    "FROM portals p JOIN portal_sync s ON s.portal_id = p.id WHERE p.id = :pid"
                ),
                {"pid": portal_id},
            )
        ).one()
    return {
        "status": row.status,
        "purge_pending": row.purge_pending,
        "last_error_code": row.last_error_code,
    }


async def _tick_and_wait() -> int | None:
    """One `tick()`, then wait for the purge it dispatched. Returns the portal it picked.

    `tick()` never awaits its work (§5.9 dispatch rule), so a test that only calls it
    would assert against a purge that has not run yet.
    """
    await definitions.tick()
    picked = next(iter(definitions._purging), None)
    tasks = list(definitions._purging.values())
    if tasks:
        await asyncio.gather(*tasks)
    return picked


def _total(portal: PortalFixture) -> int:
    return portal.calls + portal.employees + portal.crm_contexts


@pytest.fixture(autouse=True)
def _no_purge_in_flight() -> Any:
    """`_purging` is process-global; a leaked entry would silently disable the next tick."""
    definitions._purging.clear()
    yield
    definitions._purging.clear()


async def test_tick_skips_a_cooled_down_portal_and_purges_the_next_tenant(
    two_portals: TwoPortals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One tenant parked in the `purge_incomplete` cooldown must not starve the others.

    Before the fix the selection was `WHERE purge_pending ORDER BY id LIMIT 1`: the tick
    picked the lowest id, `purge_portal_data` skipped it (cooldown), the single purge slot
    was spent, and portal B's rows survived every tick for the whole hour - or forever,
    if B's cooldown keeps being renewed.
    """
    monkeypatch.setattr(definitions, "acquire_leases", _no_leases)
    parked, waiting = two_portals.a, two_portals.b
    assert parked.portal_id < waiting.portal_id, (
        "the fixture must seed the parked portal first, or 'lowest id wins' is untested"
    )
    await _arm_uninstalled(parked.portal_id)
    await _arm_uninstalled(waiting.portal_id)
    await _arm_cooldown(parked.portal_id)

    picked = await _tick_and_wait()

    assert picked == waiting.portal_id, (
        "the tick spent its one purge slot on the portal it was only going to skip"
    )
    assert await _rows(waiting.portal_id) == 0, (
        "a tenant behind a parked portal is never purged - brief rule 7 stops working "
        "fleet-wide because of one bad tenant"
    )
    assert (await _state(waiting.portal_id))["purge_pending"] is False
    assert await _rows(parked.portal_id) == _total(parked), (
        "the cooldown must still hold: skipping it in the selection is pacing, not force"
    )
    assert (await _state(parked.portal_id))["purge_pending"] is True


async def test_tick_returns_to_the_parked_portal_once_the_cooldown_expires(
    two_portals: TwoPortals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cooldown paces the retry; it must never drop a portal out of the queue."""
    monkeypatch.setattr(definitions, "acquire_leases", _no_leases)
    parked = two_portals.a
    await _arm_uninstalled(parked.portal_id)
    await _arm_cooldown(parked.portal_id, age_seconds=purge_module.INCOMPLETE_RETRY_SECONDS + 60)

    picked = await _tick_and_wait()

    assert picked == parked.portal_id
    assert await _rows(parked.portal_id) == 0
    state = await _state(parked.portal_id)
    assert state["purge_pending"] is False
    assert state["last_error_code"] is None


def _open_the_app_during(
    monkeypatch: pytest.MonkeyPatch, portal: PortalFixture, *, at_count: int
) -> None:
    """Commit a real `/app/` viewer write inside the purge, just before a final count.

    `handlers/open.py` calls `upsert_viewer` on every open, in its own `tenant_txn`,
    before any purge check - so on a portal that reinstalled during cleanup this insert
    can land after a table's last DELETE and before `purge_portal_data`'s final count.
    Counting `_count` calls is how the window is hit deterministically: three pre-counts
    (calls, employees, crm_contexts), then the three final counts.
    """
    original = purge_module._count
    seen = {"n": 0}

    async def counting(table: Any, portal_id: int) -> int:
        seen["n"] += 1
        if portal_id == portal.portal_id and seen["n"] == at_count:
            await upsert_viewer(
                portal.portal_id,
                Identity(
                    user_id=portal.user_ids[0],
                    is_admin=True,
                    timezone="UTC",
                    name="Viewer",
                    last_name="MidPurge",
                    second_name=None,
                    work_position="Operator",
                    photo_url=None,
                ),
            )
        return await original(table, portal_id)

    monkeypatch.setattr(purge_module, "_count", counting)


async def test_reinstall_during_cleanup_converges_despite_a_live_open(
    two_portals: TwoPortals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reinstalled tenant's own open must not park it for an hour with no sync.

    The state is the one §4.4 step 3 blesses: `purge_pending=true` on an ACTIVE portal.
    Before the fix the viewer row written by the open made `final_counts['employees']`
    non-zero, the purge reported `purge_incomplete`, `purge_pending` stayed set - and
    `acquire_leases` (lease.py: `Portal.purge_pending.is_(False)`) then refuses the portal,
    so the tenant never syncs and its dashboard stays empty with nothing in the logs but
    an admin-only `last_error_code`.
    """
    portal = two_portals.a
    await _arm_uninstalled(portal.portal_id)
    await _reinstall(portal.portal_id)
    # The 4th `_count` call is the first FINAL count: every delete loop has finished, so
    # the row this open writes is the new install's data, not a purge that failed.
    _open_the_app_during(monkeypatch, portal, at_count=4)

    outcome = await purge_portal_data(portal.portal_id)

    assert outcome.incomplete is False, (
        "a live reinstalled tenant's own write was counted as a failed purge"
    )
    assert outcome.live_rows_after_reinstall is True, (
        "the audit must still record that the final count was not zero (§5.9 rule 3)"
    )
    state = await _state(portal.portal_id)
    assert state["purge_pending"] is False, (
        "purge_pending kept => lease.py never leases this tenant => it never syncs"
    )
    assert state["last_error_code"] is None
    async with tenant_txn(portal.portal_id) as session:
        remaining_calls = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM calls WHERE portal_id = :pid"),
                    {"pid": portal.portal_id},
                )
            ).scalar_one()
        )
        viewer_rows = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM employees WHERE portal_id = :pid"),
                    {"pid": portal.portal_id},
                )
            ).scalar_one()
        )
    assert remaining_calls == 0, "the pre-uninstall rows must still be gone (brief rule 7)"
    assert viewer_rows == 1, "the reinstalled tenant's own viewer cache row survives"


async def test_a_still_uninstalled_portal_keeps_the_strict_emptiness_proof(
    two_portals: TwoPortals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relaxation above is scoped to a portal that is provably back.

    For an uninstalled tenant a row that re-appears after the deletes has no legitimate
    author, so §5.9 rule 3 stands: `purge_incomplete`, `purge_pending` kept, retried.
    """
    portal = two_portals.b
    await _arm_uninstalled(portal.portal_id)
    _open_the_app_during(monkeypatch, portal, at_count=4)

    outcome = await purge_portal_data(portal.portal_id)

    assert outcome.incomplete is True
    assert outcome.verified_empty is False
    state = await _state(portal.portal_id)
    assert state["purge_pending"] is True
    assert state["last_error_code"] == "purge_incomplete"
