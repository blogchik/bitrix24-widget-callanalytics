"""§4.8 — the CRM tab reads, and the rule that keeps a cached context from becoming a leak.

`crm_contexts` is keyed by `(portal_id, entity_type, entity_id)` and **not** by user, so
one row is shared by everyone who opens that deal's tab. That is deliberate — resolving
a deal costs three CRM commands and the tab is opened constantly — but it means the row
is a small pile of another user's CRM rights sitting in the database: the contact and
company ids attached to the deal, and up to `CRM_ACTIVITY_CAP` activity ids.

The design review's [MINOR] finding is what this module guards. A manager with no rights
to deal 5 forges the open; their CRM commands error; if the handler still minted
`ent={DEAL,5}`, `GET /calls` would serve them the row an administrator resolved
yesterday. `scope_filter` still limits the *calls* to their own, so what leaks is the
association "these of my calls belong to that deal's contacts" — precisely what the CRM
permission exists to withhold. §4.8 closes it with a freshness/ownership test on the
row, and the appendix states the fixed rule: rows "are served only when
`resolved_at >= JWT.iat` (or resolved by the same sub)".

The other half of the module is the matching clause. §4.8 matches three ways, and the
third exists only because of a review finding: some portals do emit `DEAL` (and other
undocumented types) directly in the statistics rows, so a deal tab that matched only the
resolved contact keys would show an empty table on exactly those portals.
"""

from __future__ import annotations

import inspect
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.crm import (
    OWNER_TYPE_IDS,
    activity_page_commands,
    deal_context_commands,
    entity_activity_commands,
)
from app.bitrix.errors import AccessDenied
from app.config import settings
from app.db.models import Call
from app.db.session import tenant_txn
from app.services.crm_context import (
    CrmContext,
    load_crm_context,
    resolve_crm_context,
    store_crm_context,
)
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

#: Two different people opening the same deal tab. `USER_A` has CRM rights; `USER_B`
#: is the manager of the review finding.
USER_A: Final[int] = 42
USER_B: Final[int] = 77

DEAL_ID: Final[int] = 5
CONTACT_ID: Final[int] = 12
COMPANY_ID: Final[int] = 3

DEAL_ROW: Final[dict[str, Any]] = {
    "ID": str(DEAL_ID),
    "TITLE": "Renewal",
    "CONTACT_ID": str(CONTACT_ID),
    "COMPANY_ID": str(COMPANY_ID),
}
CONTACT_ITEMS: Final[list[dict[str, str]]] = [{"CONTACT_ID": str(CONTACT_ID)}]


# --- building the batch a real open would have produced -----------------------------


def context_commands(
    entity_type: str, entity_id: int, *, all_pages: bool = False
) -> list[tuple[str, str, dict[str, Any]]]:
    """The CRM half of the §4.4 step 4 batch, built by the code under test.

    Taking the commands from `bitrix/crm.py` rather than hard-coding keys is the point:
    `resolve_crm_context` reads the batch by the SAME keys this module emits, so a
    rename that broke the pair would break this test rather than production.

    `all_pages` packs every follow-up activity page the cap allows, which is the shape
    the handler uses for an entity with more than one page of calls.
    """
    commands: list[tuple[str, str, dict[str, Any]]] = []
    if entity_type == "DEAL":
        commands.extend(deal_context_commands(entity_id))
    if not any(method.strip().lower() == "crm.activity.list" for _, method, _ in commands):
        commands.extend(entity_activity_commands(entity_type, entity_id))
    if all_pages:
        commands.extend(activity_page_commands(entity_type, entity_id))

    seen: set[str] = set()
    unique: list[tuple[str, str, dict[str, Any]]] = []
    for key, method, params in commands:
        if key not in seen:
            seen.add(key)
            unique.append((key, method, params))
    return unique


def batch_for(
    commands: Sequence[tuple[str, str, dict[str, Any]]],
    *,
    activity_pages: Sequence[Sequence[int]] = ((1001, 1002),),
    error_on: str | None = None,
) -> BatchResult:
    """A `BatchResult` answering `commands` the way the fake Bitrix24 would.

    Built directly rather than through the HTTP fake because §4.8 is about what the
    resolver does with the *answers*; the round trip itself is `test_open_flow`'s job.
    """
    pages = deque(activity_pages)
    results: list[CommandResult] = []
    for key, method, _params in commands:
        name = method.strip().lower()
        if error_on is not None and name == error_on.strip().lower():
            results.append(
                CommandResult(
                    key=key,
                    result=None,
                    error=AccessDenied("ACCESS_DENIED", http_status=403),
                    time=None,
                )
            )
            continue
        if name == "crm.activity.list":
            page = pages.popleft() if pages else ()
            value: Any = [{"ID": str(activity_id)} for activity_id in page]
        elif name.endswith("contact.items.get"):
            value = CONTACT_ITEMS
        elif name == "crm.deal.get":
            value = DEAL_ROW
        else:  # a command this test does not model; the resolver must not need it
            value = True
        results.append(CommandResult(key=key, result=value, error=None, time=None))
    return BatchResult(commands=tuple(results), time=None)


async def resolve(
    entity_type: str,
    entity_id: int,
    *,
    activity_pages: Sequence[Sequence[int]] = ((1001, 1002),),
    error_on: str | None = None,
    all_pages: bool = False,
) -> CrmContext | None:
    commands = context_commands(entity_type, entity_id, all_pages=all_pages)
    batch = batch_for(commands, activity_pages=activity_pages, error_on=error_on)
    return await resolve_crm_context(batch, entity_type=entity_type, entity_id=entity_id)


def keys_of(ctx: CrmContext) -> set[tuple[str, int]]:
    """`entity_keys` as comparable tuples; the stored shape is JSON, so ids may be str."""
    return {(str(pair[0]), int(pair[1])) for pair in ctx.entity_keys}


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture()
async def portal(app_engine: AsyncEngine) -> AsyncIterator[SeededPortal]:
    """One tenant, torn down under tenant context.

    Not by leaning on `ON DELETE CASCADE`: whether a cascade is exempt from FORCED RLS
    is a Postgres implementation detail (see conftest), and rows left behind would
    poison the next run silently.
    """
    seeded = await seed_portal(status="active")
    try:
        yield seeded
    finally:
        async with tenant_txn(seeded.portal_id) as session:
            for table in ("calls", "crm_contexts", "employees"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                    {"pid": seeded.portal_id},
                )
        await delete_portal(seeded.member_id)


# --- 1. a context resolved by one user is not served to another ---------------------


async def test_a_context_resolved_by_user_a_is_not_served_to_user_b(
    portal: SeededPortal,
) -> None:
    """§4.8: the row "must have `resolved_at >= JWT.iat` (or `resolved_by_user_id = sub`),
    otherwise 409 `context_missing`".

    User B's session is younger than the cached row, and B did not resolve it, so B gets
    nothing and the SPA runs the §4.6 exchange - which re-resolves the deal with B's own
    token and therefore fails closed on `crm_no_access` if B may not see it. Serving the
    row instead is the [MINOR] finding: B learns which of the deal's contacts they have
    phoned, which is exactly what the CRM permission withholds.
    """
    ctx = await resolve("DEAL", DEAL_ID)
    assert ctx is not None
    resolved_around = int(time.time())
    await store_crm_context(portal.portal_id, ctx, user_id=USER_A)

    # A JWT minted AFTER the row was written: the row is stale for that session.
    younger_session = resolved_around + 300

    served_to_b = await load_crm_context(
        portal.portal_id,
        entity_type="DEAL",
        entity_id=DEAL_ID,
        not_older_than=younger_session,
        user_id=USER_B,
    )
    assert served_to_b is None, (
        "a context another user resolved was served to a session that never proved its "
        "own CRM rights (§4.8); the endpoint must answer 409 context_missing instead."
    )

    served_to_a = await load_crm_context(
        portal.portal_id,
        entity_type="DEAL",
        entity_id=DEAL_ID,
        not_older_than=younger_session,
        user_id=USER_A,
    )
    assert served_to_a is not None, (
        "the resolver's OWN session must still be served: §4.4 writes the row in step 7 "
        "and mints the JWT in step 8, so `resolved_at` is always a moment older than "
        "`iat` and the `resolved_by_user_id = sub` clause is what makes the tab work."
    )
    assert keys_of(served_to_a) == keys_of(ctx)


async def test_a_missing_context_is_simply_absent(portal: SeededPortal) -> None:
    """The 409 of §4.8 also covers "no row at all" - a deal nobody has opened yet, or
    one whose row the 30-day purge removed. Absence must not read as "match nothing"."""
    assert (
        await load_crm_context(
            portal.portal_id,
            entity_type="DEAL",
            entity_id=DEAL_ID + 999,
            not_older_than=int(time.time()) - 3600,
            user_id=USER_A,
        )
        is None
    )


# --- 2. a successful resolution is stored and matched -------------------------------


async def test_a_full_deal_resolution_is_stored_and_read_back(portal: SeededPortal) -> None:
    """§4.4 step 4/7: `crm.deal.get` + `crm.deal.contact.items.get` + `crm.activity.list`
    become the entity keys and activity ids the tab matches on (§4.8)."""
    ctx = await resolve("DEAL", DEAL_ID, activity_pages=((1001, 1002, 1003),))
    assert ctx is not None
    assert (ctx.entity_type, ctx.entity_id) == ("DEAL", DEAL_ID)
    assert ("CONTACT", CONTACT_ID) in keys_of(ctx), "the deal's contacts are the point"
    assert ("COMPANY", COMPANY_ID) in keys_of(ctx), (
        "crm.deal.get is called for COMPANY_ID; calls to the company's number belong to "
        "the deal too"
    )
    assert sorted(ctx.activity_ids) == [1001, 1002, 1003]

    await store_crm_context(portal.portal_id, ctx, user_id=USER_A)
    loaded = await load_crm_context(
        portal.portal_id,
        entity_type="DEAL",
        entity_id=DEAL_ID,
        not_older_than=int(time.time()) - 300,
        user_id=USER_A,
    )
    assert loaded is not None
    assert keys_of(loaded) == keys_of(ctx)
    assert sorted(loaded.activity_ids) == sorted(ctx.activity_ids)


@pytest.mark.parametrize("entity_type", ["LEAD", "CONTACT", "COMPANY"])
async def test_the_simple_tabs_resolve_to_their_own_key_and_their_activities(
    portal: SeededPortal, entity_type: str
) -> None:
    """§4.4 step 4: the non-deal tabs are "direct entity keys + activity ids" - one
    `crm.activity.list` with the matching `OWNER_TYPE_ID` and nothing else."""
    ctx = await resolve(entity_type, DEAL_ID, activity_pages=((2001,),))
    assert ctx is not None
    assert (entity_type, DEAL_ID) in keys_of(ctx)
    assert ctx.activity_ids == [2001]


async def test_an_errored_crm_command_resolves_to_nothing(portal: SeededPortal) -> None:
    """§4.4 step 5: an error in ANY CRM command ends the open at `crm_no_access`.

    The resolver must therefore refuse to produce a half-context: a `CrmContext` with an
    empty `activity_ids` because the call failed would be stored over a good row and
    would silently narrow the tab for everyone.
    """
    assert await resolve("DEAL", DEAL_ID, error_on="crm.activity.list") is None
    assert await resolve("DEAL", DEAL_ID, error_on="crm.deal.get") is None


# --- 3. the three-way match clause (§4.8) -------------------------------------------


def calls_repo_symbol(name: str) -> Any:
    """One symbol from the milestone-5 read layer, or a skip that names the owner."""
    module = pytest.importorskip(
        "app.services.calls_repo",
        reason="§4.8 matching lives in calls_repo, which lands with the read layer",
    )
    fn = getattr(module, name, None)
    if fn is None:
        pytest.skip(f"app.services.calls_repo has no {name}() yet (§4.8)")
    return fn


def match_clause(ctx: CrmContext) -> Any:
    """Call `crm_match_clause` however it declares its parameters.

    §4.8 pins the *predicate*, not the signature, and the module is written by another
    agent in parallel; adapting here is cheaper than a failing import that says nothing
    about the rule under test.
    """
    fn = calls_repo_symbol("crm_match_clause")
    parameters = inspect.signature(fn).parameters
    pool: dict[str, Any] = {
        "ctx": ctx,
        "context": ctx,
        "crm_context": ctx,
        "entity_type": ctx.entity_type,
        "entity_id": ctx.entity_id,
        "entity_keys": ctx.entity_keys,
        "activity_ids": ctx.activity_ids,
    }
    kwargs = {name: pool[name] for name in parameters if name in pool}
    if not kwargs:
        return fn(ctx)
    if {"ctx", "context", "crm_context"} & set(kwargs):
        first = next(name for name in ("ctx", "context", "crm_context") if name in kwargs)
        return fn(**{first: ctx})
    return fn(**kwargs)


async def insert_call(portal_id: int, bx_id: int, **columns: Any) -> None:
    """One `calls` row under tenant context (§3: the table is FORCE ROW LEVEL SECURITY)."""
    values: dict[str, Any] = {
        "portal_id": portal_id,
        "bx_id": bx_id,
        "call_id": f"call-{portal_id}-{bx_id}",
        "call_type": 1,
        "call_start_date": datetime.now(tz=UTC) - timedelta(minutes=bx_id),
        "call_duration": 30,
        "portal_user_id": USER_A,
        "crm_entity_type": None,
        "crm_entity_id": None,
        "crm_activity_id": None,
    }
    values.update(columns)
    names = ", ".join(values)
    binds = ", ".join(f":{name}" for name in values)
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(f"INSERT INTO calls ({names}) VALUES ({binds})"),  # noqa: S608 - fixed names
            values,
        )


async def matched_bx_ids(portal_id: int, ctx: CrmContext) -> set[int]:
    statement = select(Call.bx_id).where(Call.portal_id == portal_id, match_clause(ctx))
    async with tenant_txn(portal_id) as session:
        return {int(value) for value in (await session.execute(statement)).scalars()}


async def test_the_match_clause_finds_all_three_shapes_and_nothing_else(
    portal: SeededPortal,
) -> None:
    """§4.8: "`(crm_entity_type, crm_entity_id) IN entity_keys` **OR**
    `crm_activity_id = ANY(activity_ids)` **OR** `(crm_entity_type = ent.t AND
    crm_entity_id = ent.id)`".

    Each disjunct answers a different way Bitrix24 attributes a call, and dropping any
    one of them empties the tab on some portals rather than all of them - which is why
    this is a test and not a code review comment:

    * **entity key** - the ordinary case: the call is attributed to the deal's contact.
    * **activity id** - a call logged as an activity of the entity, whose statistics row
      carries no CRM entity at all.
    * **raw `DEAL` row** - the review finding: some portals put `DEAL` straight into
      `CRM_ENTITY_TYPE`, an undocumented value the schema deliberately no longer CHECKs.
    """
    ctx = CrmContext(
        entity_type="DEAL",
        entity_id=DEAL_ID,
        entity_keys=[["CONTACT", CONTACT_ID], ["COMPANY", COMPANY_ID]],
        activity_ids=[1001],
    )

    await insert_call(portal.portal_id, 1, crm_entity_type="CONTACT", crm_entity_id=CONTACT_ID)
    await insert_call(portal.portal_id, 2, crm_activity_id=1001)
    await insert_call(portal.portal_id, 3, crm_entity_type="DEAL", crm_entity_id=DEAL_ID)
    # Neither the deal's contacts, nor its activities, nor the deal itself:
    await insert_call(portal.portal_id, 4, crm_entity_type="CONTACT", crm_entity_id=999)
    await insert_call(portal.portal_id, 5, crm_activity_id=9999)
    await insert_call(portal.portal_id, 6, crm_entity_type="DEAL", crm_entity_id=DEAL_ID + 1)
    await insert_call(portal.portal_id, 7)  # a call attributed to nothing at all

    assert await matched_bx_ids(portal.portal_id, ctx) == {1, 2, 3}


async def test_the_match_clause_of_a_simple_tab_does_not_match_other_entities(
    portal: SeededPortal,
) -> None:
    """The same clause for a CONTACT tab: its own key and its activities, nothing else."""
    ctx = CrmContext(
        entity_type="CONTACT",
        entity_id=CONTACT_ID,
        entity_keys=[["CONTACT", CONTACT_ID]],
        activity_ids=[2001],
    )
    await insert_call(portal.portal_id, 11, crm_entity_type="CONTACT", crm_entity_id=CONTACT_ID)
    await insert_call(portal.portal_id, 12, crm_activity_id=2001)
    await insert_call(portal.portal_id, 13, crm_entity_type="COMPANY", crm_entity_id=CONTACT_ID)
    await insert_call(portal.portal_id, 14, crm_entity_type="CONTACT", crm_entity_id=CONTACT_ID + 1)

    assert await matched_bx_ids(portal.portal_id, ctx) == {11, 12}


# --- 4. the activity cap (§4.4 step 4) ----------------------------------------------


def find_value(node: Any, wanted: str) -> Any:
    """Depth-first search for one parameter key, case-insensitively.

    §4.4 step 4 fixes the *filter*, not how `crm.py` nests it (`filter` vs `FILTER`,
    flat vs nested), so the assertion looks for the value rather than for a path.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).strip().lower() == wanted.lower():
                return value
            found = find_value(value, wanted)
            if found is not None:
                return found
    elif isinstance(node, (list, tuple)):
        for item in node:
            found = find_value(item, wanted)
            if found is not None:
                return found
    return None


def test_the_activity_commands_request_at_most_the_cap() -> None:
    """§4.4 step 4: "max 5 pages of 50 = `CRM_ACTIVITY_CAP` 250".

    The review flagged the original inconsistency (a 1000 default against a handler that
    followed 250), so the request side is asserted against the setting rather than
    against a number written twice. Every page also costs operating time on a batch the
    moderator is waiting for (§5.6), which is why the page count is bounded at all.
    """
    for entity_type, owner_type_id in OWNER_TYPE_IDS.items():
        commands = entity_activity_commands(entity_type, DEAL_ID)
        assert commands, f"{entity_type} must fetch its activities"
        for _key, method, params in commands:
            assert method.strip().lower() == "crm.activity.list"
            assert int(find_value(params, "OWNER_TYPE_ID")) == owner_type_id, (
                "each tab must filter on its own OWNER_TYPE_ID (§4.4 step 4); a wrong "
                "one reads another entity type's activities"
            )
            assert int(find_value(params, "OWNER_ID")) == DEAL_ID
        assert len(commands) * 50 <= settings.crm_activity_cap, (
            f"{entity_type} asks for {len(commands)} pages of 50, which is more than "
            f"CRM_ACTIVITY_CAP ({settings.crm_activity_cap})"
        )


def test_the_owner_type_ids_are_the_documented_ones() -> None:
    """LEAD 1, DEAL 2, CONTACT 3, COMPANY 4 (§4.4 step 4). A wrong number here silently
    reads another entity type's activities - a cross-entity leak that looks like an
    empty tab, not like an error."""
    assert OWNER_TYPE_IDS == {"LEAD": 1, "DEAL": 2, "CONTACT": 3, "COMPANY": 4}


async def test_the_resolved_activity_ids_never_exceed_the_cap(portal: SeededPortal) -> None:
    """The cap is honoured on the *answer*, not only on the request.

    `activity_ids` lands in a `bigint[]` column and then in an `= ANY(...)` predicate on
    every CRM tab read; a portal that answers with more rows than were asked for (or a
    page size that changes under us) must not be able to grow that array without bound.
    """
    commands = context_commands("DEAL", DEAL_ID, all_pages=True)
    activity_commands = [c for c in commands if c[1].strip().lower() == "crm.activity.list"]
    assert len(activity_commands) > 1, "the cap is about MANY pages; pack them all"
    # Every page answers with more rows than a page holds - a portal that ignores
    # `start`, or a page size that changed under us.
    oversized = [
        tuple(range(10_000 + page * 100, 10_000 + page * 100 + 60))
        for page in range(len(activity_commands))
    ]

    ctx = await resolve("DEAL", DEAL_ID, activity_pages=oversized, all_pages=True)
    assert ctx is not None
    assert len(ctx.activity_ids) <= settings.crm_activity_cap, (
        f"{len(ctx.activity_ids)} activity ids resolved against a cap of "
        f"{settings.crm_activity_cap}; truncate the answer (§4.4 step 4)."
    )
    assert len(set(ctx.activity_ids)) == len(ctx.activity_ids), "and never a duplicate"

    await store_crm_context(portal.portal_id, ctx, user_id=USER_A)
    loaded = await load_crm_context(
        portal.portal_id,
        entity_type="DEAL",
        entity_id=DEAL_ID,
        not_older_than=int(time.time()) - 300,
        user_id=USER_A,
    )
    assert loaded is not None
    assert len(loaded.activity_ids) == len(ctx.activity_ids)
