"""§4.8 — the calls table: paging, filters, ordering, the CRM match, and one absence.

Three different kinds of failure live in this endpoint and only one of them is loud.

*Paging and ordering* fail loudly: a moderator sees a duplicate or a hole. They are still
worth pinning, because "50 a page, newest first" is the contract the table is written
against, and the two ways OFFSET paging goes wrong both keep the page lengths right - an
off-by-one repeats one row per boundary and hides another, and an unstable sort reshuffles
equal rows between two requests so one row lands on two pages and another on none. Only
comparing the union against the seeded set catches either.

*The CRM match* fails quietly. §4.8 is a three-way OR and each clause exists for a
different real portal: the resolved entity keys (a deal's contacts and companies, because
telephony rows are documented to carry CONTACT/COMPANY/LEAD and never the deal), the
activity ids from the deal's timeline, and the raw `DEAL` clause for the portals that *do*
emit `DEAL` in their statistics rows (assumption 18). Drop any one and the tab shows an
empty state on a card that plainly has calls - which reads as "the app is broken" rather
than as "one OR branch is missing". The stale-context rule beside it is a permission
boundary wearing a caching costume: `crm_contexts` is a *shared* per-portal cache, so
serving a row an administrator resolved to a salesperson who cannot open that deal would
turn the administrator's rights into theirs. The answer is 409 `context_missing` - never
403, never an empty list - because the SPA reacts to that code by re-resolving the entity
with the caller's own Bitrix24 token.

*The recording URL* fails silently and permanently. Decision 19 and §9 make
`call_record_url` server-side data: it can be a `download.json?auth=<portal access token>`
URL, so returning it to the browser hands every viewer the portal's admin credential for
an hour, and nothing in the response body announces that it happened. The check here is
therefore not "the item model omits the field" but a recursive walk of every response this
file produces - error bodies included - looking for the key at any depth and for the
value's own sentinel substring.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.db.session import tenant_txn
from app.main import create_app
from app.security.session_token import issue_session
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

CALLS_PATH: Final[str] = "/api/v1/calls"

#: §2: "GET /calls (50/page)". Fixed server-side, never a client parameter - a page size
#: the caller chooses is a way to ask for the whole table in one request.
PAGE_SIZE: Final[int] = 50
PAGES_OF_HISTORY: Final[int] = 120

TASHKENT: Final[str] = "Asia/Tashkent"
USER_A: Final[int] = 101
USER_B: Final[int] = 102

#: The deal whose tab is under test, and the keys a real open-time resolution produces.
DEAL_ID: Final[int] = 77
DEAL_CONTACT: Final[int] = 12
DEAL_COMPANY: Final[int] = 3
DEAL_ACTIVITY: Final[int] = 9001

#: A recording URL of the shape §9 step 1 warns about: the credential IS the query string.
#: The sentinel is long and unique so the recursive scan cannot match it by accident.
RECORD_URL: Final[str] = (
    "https://portal.bitrix24.test/rest/download.json?FILE_ID=42&auth=SENTINELRECORDINGURL0011"
)
RECORD_SENTINEL: Final[str] = "SENTINELRECORDINGURL0011"
#: `call_record_url`, `recordUrl`, `record_url`, `urlRecord` - any spelling of the field.
_RECORD_KEY: Final[re.Pattern[str]] = re.compile(r"record.{0,3}url|url.{0,3}record", re.IGNORECASE)

BASE_DAY: Final[date] = date(2026, 4, 6)  # a Monday


# --- reading a response ----------------------------------------------------------------


def _pick(payload: Any, *names: str) -> Any:
    assert isinstance(payload, Mapping), f"expected an object, got {type(payload).__name__}"
    for name in names:
        if name in payload:
            return payload[name]
    raise AssertionError(f"none of {list(names)} in the response; it has {sorted(payload)}")


def rows_of(body: Any) -> list[Mapping[str, Any]]:
    rows = _pick(body, "rows", "items", "calls")
    assert isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
    for row in rows:
        assert isinstance(row, Mapping), f"a call entry is not an object: {row!r}"
        assert "id" in row, (
            "every row needs the opaque `calls.id` (§3): it is what `/calls/{id}/play-url` "
            f"and `/calls/{{id}}/refresh` are addressed by. Got keys {sorted(row)}."
        )
    return list(rows)


def ids_of(body: Any) -> list[int]:
    return [int(row["id"]) for row in rows_of(body)]


# --- the absence that must hold on every single response --------------------------------


def assert_no_record_url(response: httpx.Response) -> None:
    """Walk the WHOLE body for `call_record_url`, by key at any depth and by value.

    Not "the item model omits the field": a nested `raw` block, a serialiser that dumps the
    ORM row into an error payload, or a debug field added later would all bypass a schema
    check. §9 makes this absolute - the URL can carry the portal's access token, so one
    leak is an hour-long credential handout to every viewer, and nothing in the response
    would say so.
    """
    assert RECORD_SENTINEL not in response.text, (
        "the recording URL's own query string appears in the response body. §9 step 1: a "
        "`download.json?auth=<token>` URL IS the portal access token, so this hands every "
        "viewer admin-level CRM read for the life of that token (decision 19)."
    )
    assert "download.json" not in response.text, (
        "an upstream recording URL reached the browser; playback goes through the signed "
        "`/calls/{id}/record?t=` grant, never through the raw URL (§4.6, §9)."
    )

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                assert not _RECORD_KEY.search(str(key)), (
                    f"{path}.{key} exposes the recording URL field; `calls.py` projects "
                    "named columns precisely so this cannot happen by accident."
                )
                walk(value, f"{path}.{key}")
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    if response.headers.get("content-type", "").startswith("application/json"):
        # A body that does not parse is not skipped silently - the substring scan above
        # already covered it, and that is the check that matters for a leaked URL.
        with suppress(json.JSONDecodeError):
            walk(response.json(), "$")


# --- seeding ---------------------------------------------------------------------------


async def seed_calls(portal_id: int, rows: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """Insert many `calls` in ONE tenant transaction; return `{bx_id: calls.id}`.

    The mapping is what every assertion below identifies rows by: the API answers with the
    opaque surrogate id (§3 - `bx_id` is the sync cursor and is deliberately not exposed),
    so matching on it keeps the tests independent of which display fields the row carries.
    """
    identifiers: dict[int, int] = {}
    async with tenant_txn(portal_id) as session:
        for row in rows:
            bx_id = int(row["bx_id"])
            identifiers[bx_id] = int(
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO calls (portal_id, bx_id, call_id, call_type,
                                               call_start_date, call_duration, call_failed_code,
                                               portal_user_id, phone_number, portal_number,
                                               crm_entity_type, crm_entity_id, crm_activity_id,
                                               rest_app_id, call_record_url)
                            VALUES (:pid, :bx_id, :call_id, :call_type,
                                    :started, :duration, :code,
                                    :user_id, :phone, 'line-1',
                                    :crm_type, :crm_id, :activity_id,
                                    :rest_app_id, :record_url)
                            RETURNING id
                            """
                        ),
                        {
                            "pid": portal_id,
                            "bx_id": bx_id,
                            "call_id": f"api-{bx_id}",
                            "call_type": int(row.get("call_type", 1)),
                            "started": row["started"],
                            "duration": int(row.get("duration", 60)),
                            "code": row.get("code", "200"),
                            "user_id": row.get("user_id", USER_A),
                            "phone": row.get("phone", f"+99890000{bx_id:04d}"),
                            "crm_type": row.get("crm_type"),
                            "crm_id": row.get("crm_id"),
                            "activity_id": row.get("activity_id"),
                            "rest_app_id": row.get("rest_app_id"),
                            "record_url": row.get("record_url"),
                        },
                    )
                ).scalar_one()
            )
    return identifiers


async def seed_history(portal_id: int, count: int = PAGES_OF_HISTORY) -> dict[int, int]:
    """`count` calls one minute apart, so "newest first" is a strict ordering on `bx_id`."""
    start = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 8, 0, tzinfo=UTC)
    return await seed_calls(
        portal_id,
        [
            {
                "bx_id": index,
                "started": start + timedelta(minutes=index),
                "code": "200" if index % 4 else "304",
                "user_id": USER_A if index % 3 else USER_B,
            }
            for index in range(1, count + 1)
        ],
    )


async def store_context(
    portal_id: int,
    *,
    resolved_by: int,
    age: timedelta = timedelta(0),
    entity_keys: Sequence[Sequence[Any]] = (("CONTACT", DEAL_CONTACT), ("COMPANY", DEAL_COMPANY)),
    activity_ids: Sequence[int] = (DEAL_ACTIVITY,),
) -> None:
    """The row §4.4 step 7 writes at open time, aged on demand for the staleness test."""
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO crm_contexts (portal_id, entity_type, entity_id, entity_keys,
                                          activity_ids, resolved_by_user_id, resolved_at)
                VALUES (:pid, 'DEAL', :eid, CAST(:keys AS jsonb), CAST(:acts AS bigint[]),
                        :uid, now() - make_interval(secs => :age))
                ON CONFLICT (portal_id, entity_type, entity_id) DO UPDATE
                SET entity_keys = EXCLUDED.entity_keys,
                    activity_ids = EXCLUDED.activity_ids,
                    resolved_by_user_id = EXCLUDED.resolved_by_user_id,
                    resolved_at = EXCLUDED.resolved_at
                """
            ),
            {
                "pid": portal_id,
                "eid": DEAL_ID,
                "keys": json.dumps([list(pair) for pair in entity_keys]),
                "acts": [int(value) for value in activity_ids],
                "uid": resolved_by,
                "age": age.total_seconds(),
            },
        )


async def seed_employee(portal_id: int, user_id: int, name: str) -> None:
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (portal_id, bx_user_id, name, last_name, active, found,
                                       fetched_at)
                VALUES (:pid, :uid, :name, 'Operator', true, true, now())
                ON CONFLICT (portal_id, bx_user_id) DO NOTHING
                """
            ),
            {"pid": portal_id, "uid": user_id, "name": name},
        )


async def wipe(portal_id: int) -> None:
    async with tenant_txn(portal_id) as session:
        for table in ("calls", "employees", "crm_contexts"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE portal_id = :pid"),  # noqa: S608 - fixed names
                {"pid": portal_id},
            )


# --- fixtures --------------------------------------------------------------------------


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
    user_id: int = USER_A,
    access: str = "all",
    is_admin: bool = True,
    entity: dict[str, Any] | None = None,
    placement: str = "DEFAULT",
) -> str:
    return issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=user_id,
        adm=is_admin,
        acc=access,
        tz=TASHKENT,
        lang="ru",
        plc=placement,
        ent=entity,
        ttl_seconds=3600,
    )


def period(start: date, end: date) -> dict[str, str]:
    """The inclusive, viewer-local range `services/stats.py::parse_filters` reads."""
    return {"from": start.isoformat(), "to": end.isoformat()}


#: Every request below carries an explicit period. Not decoration: the endpoint defaults to
#: the last seven local days (that is what the SPA opens on) and the seeded history sits on
#: a fixed date, so a test that relied on the default would return zero rows on any run day
#: more than a week from `BASE_DAY`.
AROUND_BASE_DAY: Final[dict[str, str]] = period(
    BASE_DAY - timedelta(days=1), BASE_DAY + timedelta(days=1)
)


async def get_calls(client: httpx.AsyncClient, token: str, **filters: Any) -> httpx.Response:
    """`GET /calls`, scanning every response for the recording URL before returning it.

    Putting the scan in the one place every request goes through is deliberate: §9's rule
    covers *every* response, including the 409s and 403s below, and a per-test assertion
    would only cover the ones somebody remembered to check.
    """
    params: dict[str, Any] = {}
    for key, value in filters.items():
        if value is None:
            continue
        params[key] = "true" if value is True else ("false" if value is False else value)
    response = await client.get(
        CALLS_PATH, params=params, headers={"Authorization": f"Bearer {token}"}
    )
    assert_no_record_url(response)
    return response


# --- paging and ordering ---------------------------------------------------------------


async def test_paging_walks_the_whole_history_once_with_no_gap_and_no_repeat(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§2: 50 a page. The property that matters is that the pages partition the set."""
    identifiers = await seed_history(portal.portal_id)
    token = session_for(portal)

    seen: list[int] = []
    for page in (1, 2, 3):
        response = await get_calls(client, token, page=page, **AROUND_BASE_DAY)
        assert response.status_code == 200, response.text
        body = response.json()
        rows = ids_of(body)
        expected = PAGE_SIZE if page < 3 else PAGES_OF_HISTORY - 2 * PAGE_SIZE
        assert len(rows) == expected, (
            f"page {page} returned {len(rows)} rows, expected {expected} (§2 fixes 50/page)"
        )
        assert body["has_more"] is (page < 3), (
            f"page {page} reports has_more={body['has_more']}; the table pages on this flag "
            "and a wrong one either hides rows or offers an empty page."
        )
        seen.extend(rows)

    assert len(seen) == len(set(seen)), (
        "a row was returned on two different pages: the ordering is not total, so OFFSET "
        "sees a different sequence on each request (§4.8 orders by start date THEN bx_id)."
    )
    assert set(seen) == set(identifiers.values()), (
        "the three pages do not add up to the seeded history: rows were skipped at a page "
        "boundary."
    )


async def test_calls_are_returned_newest_first(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Newest first is what the CRM tab and the dashboard table both assume.

    Checked through the seeded ordering (`bx_id` grows with `call_start_date`) rather than
    by parsing the timestamp field, so the assertion is about the order rather than about
    a format.
    """
    identifiers = await seed_history(portal.portal_id)
    by_row_id = {row_id: bx_id for bx_id, row_id in identifiers.items()}
    token = session_for(portal)

    response = await get_calls(client, token, page=1, **AROUND_BASE_DAY)
    assert response.status_code == 200, response.text
    order = [by_row_id[row_id] for row_id in ids_of(response.json())]

    assert order == sorted(order, reverse=True), (
        f"the first page is not in descending start order: {order[:8]}..."
    )
    assert order[0] == PAGES_OF_HISTORY, "the newest call must be the first row of page 1"


async def test_a_page_past_the_end_is_empty_rather_than_an_error(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """An empty page is a legitimate answer; a malformed one is not.

    `page=0` and `page=abc` are refused with a machine code rather than clamped, for the
    same reason the period cap is (§10 step 5): a silently corrected request answers a
    question the caller did not ask.
    """
    await seed_history(portal.portal_id, count=3)
    token = session_for(portal)

    beyond = await get_calls(client, token, page=4, **AROUND_BASE_DAY)
    assert beyond.status_code == 200 and ids_of(beyond.json()) == []
    assert beyond.json()["has_more"] is False

    for bad in ("0", "-1", "abc"):
        refused = await get_calls(client, token, page=bad, **AROUND_BASE_DAY)
        assert refused.status_code == 400, f"page={bad!r} was accepted"
        assert refused.json()["code"] == "bad_page"


# --- the filter set --------------------------------------------------------------------


async def test_each_filter_narrows_the_result_to_exactly_the_rows_it_names(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The facets §3's indexes and comments name, each proven by the rows it returns.

    One test rather than six because they share a hand-built set of nine calls whose every
    attribute is deliberate; asserting the returned id sets against that set is what makes
    a facet that is silently ignored - a query parameter the endpoint never declared - fail
    here instead of passing as "well, it returned some calls".

    The combination at the end matters on its own: facets must AND together. Two filters
    that OR would *widen* the result, and a viewer narrowing by employee and by result
    would be shown more rows than either filter alone.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 9, 0, tzinfo=UTC)
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {"bx_id": 1, "started": day, "user_id": USER_A, "code": "200", "call_type": 1},
            {"bx_id": 2, "started": day + timedelta(minutes=1), "user_id": USER_A, "code": "304"},
            {"bx_id": 3, "started": day + timedelta(minutes=2), "user_id": USER_B, "code": "200"},
            {"bx_id": 4, "started": day + timedelta(minutes=3), "user_id": USER_B, "code": "603"},
            {"bx_id": 5, "started": day + timedelta(minutes=4), "call_type": 2, "code": "200"},
            {"bx_id": 6, "started": day + timedelta(minutes=5), "rest_app_id": 7, "code": "200"},
            {"bx_id": 7, "started": day + timedelta(minutes=6), "code": "200"},
            {"bx_id": 8, "started": day + timedelta(minutes=7), "code": "200"},
            # Outside the period every request below asks for.
            {"bx_id": 9, "started": day - timedelta(days=30), "user_id": USER_A, "code": "200"},
        ],
    )
    token = session_for(portal)
    days = period(BASE_DAY, BASE_DAY)

    async def returned(**filters: Any) -> set[int]:
        response = await get_calls(client, token, **days, **filters)
        assert response.status_code == 200, response.text
        return set(ids_of(response.json()))

    def expect(*bx_ids: int) -> set[int]:
        return {identifiers[bx_id] for bx_id in bx_ids}

    assert await returned() == expect(1, 2, 3, 4, 5, 6, 7, 8), (
        "the period filter is not applied: the call thirty days earlier is in the result "
        "(or the eight inside the day are not)."
    )
    assert await returned(employee=USER_B) == expect(3, 4), (
        "the employee facet did not narrow to USER_B's two calls"
    )
    assert await returned(result="answered") == expect(1, 3, 5, 6, 7, 8), (
        "the result facet must use the generated `result_group` (§3), the single "
        "server-side mapping shared by the cards, the filter and the recheck job."
    )
    assert await returned(result="missed") == expect(2)
    assert await returned(result="not_connected") == expect(4), (
        "603 is neither answered nor missed; it is the third group"
    )
    assert await returned(result=["missed", "not_connected"]) == expect(2, 4), (
        "a repeated facet is an OR within itself - two result groups, both shown"
    )
    assert await returned(direction=2) == expect(5), (
        "the direction facet must select on the raw `call_type` Bitrix24 delivered (§3 "
        "keeps unknown codes rather than constraining them)"
    )
    assert await returned(line=7) == expect(6), (
        "the line / source facet selects on `rest_app_id` (§3)"
    )
    assert await returned(line="builtin") == expect(1, 2, 3, 4, 5, 7, 8), (
        "`rest_app_id IS NULL` means built-in telephony (§3), which cannot be expressed as "
        "an id - so 'builtin' has to be its own value rather than a missing filter."
    )
    assert await returned(employee=USER_B, result="answered") == expect(3), (
        "two facets must AND. If they OR, every additional filter WIDENS the result, and a "
        "viewer narrowing a table would see more rows than before."
    )


async def test_an_own_principal_never_sees_a_colleagues_call(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: `scope_filter` is applied in exactly one place and therefore to this list too.

    The employee facet is hidden in the UI for an `own` viewer (open question 15), so the
    only thing standing between them and the portal's whole history is the predicate - and
    a hand-crafted request is not obliged to honour a hidden control.
    """
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {"bx_id": 1, "started": datetime(2026, 4, 6, 9, 0, tzinfo=UTC), "user_id": USER_A},
            {"bx_id": 2, "started": datetime(2026, 4, 6, 9, 1, tzinfo=UTC), "user_id": USER_B},
            {"bx_id": 3, "started": datetime(2026, 4, 6, 9, 2, tzinfo=UTC), "user_id": USER_B},
        ],
    )
    token = session_for(portal, user_id=USER_B, access="own", is_admin=False)

    response = await get_calls(client, token, **period(BASE_DAY, BASE_DAY))
    assert response.status_code == 200, response.text
    assert set(ids_of(response.json())) == {identifiers[2], identifiers[3]}, (
        "an 'own' principal was served rows belonging to another employee"
    )

    # And the facet cannot be talked out of it by naming somebody else.
    other = await get_calls(
        client, token, employee=USER_A, employee_id=USER_A, **period(BASE_DAY, BASE_DAY)
    )
    assert other.status_code in (200, 403)
    if other.status_code == 200:
        assert ids_of(other.json()) == [], (
            "asking for a colleague's user id returned their calls: a facet must narrow "
            "the scope, never widen it (§4.7)."
        )


async def test_a_denied_principal_is_refused_the_table_outright(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: every data endpoint answers 403 `no_stats_permission` for a denied viewer."""
    await seed_history(portal.portal_id, count=3)
    token = session_for(portal, user_id=USER_B, access="denied", is_admin=False)

    response = await get_calls(client, token, **AROUND_BASE_DAY)

    assert response.status_code == 403
    assert response.json() == {"code": "no_stats_permission"}


# --- the number search -----------------------------------------------------------------


#: Five counterparties, each written the way some portal really writes one. The point of
#: the set is that two of them are the SAME number under different typography and two of
#: them are not numbers at all - which is the whole problem the facet has to solve.
SEARCH_ROWS: Final[tuple[dict[str, Any], ...]] = (
    {"bx_id": 1, "phone": "+998901234567"},
    {"bx_id": 2, "phone": "998 90 123 45 67"},
    {"bx_id": 3, "phone": "+7 (495) 765-43-21"},
    {"bx_id": 4, "phone": "sip:reception@office.local"},
    {"bx_id": 5, "phone": None},
)


async def seed_search_rows(portal_id: int) -> dict[int, int]:
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 9, 0, tzinfo=UTC)
    return await seed_calls(
        portal_id,
        [
            {**row, "started": day + timedelta(minutes=index), "code": "200"}
            for index, row in enumerate(SEARCH_ROWS)
        ],
    )


async def test_the_number_search_matches_on_digits_however_the_number_was_typed(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The reason the facet exists: typography must not decide whether a row matches.

    §3 stores `PHONE_NUMBER` exactly as Bitrix24 delivered it and normalises nothing, so a
    portal can hold `+998901234567` and `998 90 123 45 67` for the same person. The table
    then renders both regrouped with spaces (`format.ts::formatPhone`), which makes the
    single most likely search anybody performs a number copied out of the row in front of
    them - and under a raw substring comparison that search matches nothing at all.

    Both directions are asserted separately on purpose. Normalising only the needle passes
    the first assertion and fails the second; normalising only the column passes the second
    and fails the first. Only doing both passes both.
    """
    identifiers = await seed_search_rows(portal.portal_id)
    token = session_for(portal)
    days = period(BASE_DAY, BASE_DAY)

    async def returned(**filters: Any) -> set[int]:
        response = await get_calls(client, token, **days, **filters)
        assert response.status_code == 200, response.text
        return set(ids_of(response.json()))

    def expect(*bx_ids: int) -> set[int]:
        return {identifiers[bx_id] for bx_id in bx_ids}

    assert await returned() == expect(1, 2, 3, 4, 5), (
        "without a search the facet must be absent entirely, not applied as an empty one"
    )
    assert await returned(search="9012345") == expect(1, 2), (
        "separators in the COLUMN must not hide a row: these two rows are the same number, "
        "one of them written with spaces."
    )
    assert await returned(search="+998 90 123 45 67") == expect(1, 2), (
        "separators in the NEEDLE must not hide a row either - this is the number exactly "
        "as the table renders it, which is what a reader copies and pastes back in."
    )
    assert await returned(search="4957654321") == expect(3), (
        "a different number must not be dragged in by the normalisation"
    )
    assert await returned(search="765") == expect(3), (
        "a few digits from the middle must match: the whole number is never required."
    )
    assert await returned(search="55555") == set(), (
        "a number nobody was called on returns nothing, not everything"
    )
    for needle in ("9012345", "765", "sip"):
        assert identifiers[5] not in await returned(search=needle), (
            "a call with no counterparty number cannot match a number. NULL must drop out "
            "of the predicate rather than be coalesced into an empty string, which would "
            "match every substring search ever made."
        )


async def test_a_non_numeric_search_finds_a_sip_address_and_never_widens_the_result(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The fallback branch, and the two ways this facet can silently stop being a filter.

    A needle with no digits cannot be digit-normalised - the normalisation yields the empty
    string, and matching on it would return every row that has a number while dropping
    every row that has none. That is neither "no filter" nor "no rows" but a third answer
    that looks plausible in a table and is wrong in both directions. So a non-numeric
    needle keeps the escaped ILIKE, which also keeps SIP addresses and aliases findable:
    §3 stores them raw and `formatPhone` shows them verbatim, so they are rows a reader
    can see on screen and will therefore search for.

    The wildcard assertions are the other half. `%` and `_` are LIKE syntax, and a portal
    where typing `%` returned the entire table would have a search box that is a way to
    remove the filter it appears to be applying.
    """
    identifiers = await seed_search_rows(portal.portal_id)
    token = session_for(portal)
    days = period(BASE_DAY, BASE_DAY)

    async def returned(**filters: Any) -> set[int]:
        response = await get_calls(client, token, **days, **filters)
        assert response.status_code == 200, response.text
        return set(ids_of(response.json()))

    assert await returned(search="reception") == {identifiers[4]}, (
        "a SIP address is a counterparty the table renders, so it must be searchable"
    )
    assert await returned(search="RECEPTION") == {identifiers[4]}, (
        "the non-numeric branch stays case-insensitive"
    )
    assert await returned(search="%") == set(), (
        "a bare LIKE wildcard must be escaped into a literal. Unescaped it matches every "
        "row that has a number and silently drops every row that has none."
    )
    assert await returned(search="_") == set(), (
        "the single-character wildcard has to be escaped for the same reason"
    )
    assert await returned(search="   ") == set(identifiers.values()), (
        "whitespace is trimmed away by `_first`, so it is no filter at all - not an empty "
        "needle that matches everything carrying a number."
    )


async def test_the_search_ands_with_the_other_facets_and_pages_its_own_result(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Two properties the facet shares with every other one, and one it does not.

    Shared: it must AND with the rest (a search combined with an employee cannot return
    more rows than the search alone), and `total` must describe the *filtered* set - a
    table that pages a search while counting the period would promise the reader eight
    hundred results and then run out after two.

    Not shared: this is the only facet the charts never see, which is also why `total` has
    never been exercised against a facet anywhere else in this file.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 9, 0, tzinfo=UTC)
    matching = PAGE_SIZE + 10
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {
                "bx_id": index,
                "started": day + timedelta(minutes=index),
                "code": "200",
                "user_id": USER_A if index % 2 else USER_B,
                # Every matching row shares the run 5551; none of the others can contain it.
                "phone": f"+99855510{index:04d}",
            }
            for index in range(1, matching + 1)
        ]
        + [
            {
                "bx_id": 900 + index,
                "started": day + timedelta(minutes=index),
                "code": "200",
                "phone": f"+99877720{index:04d}",
            }
            for index in range(1, 6)
        ],
    )
    token = session_for(portal)
    days = period(BASE_DAY, BASE_DAY)
    wanted = {identifiers[index] for index in range(1, matching + 1)}

    first = await get_calls(client, token, **days, search="5551")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == matching, (
        "`total` must count the searched set. Counting the unfiltered period would make "
        "the pager describe a different table than the one on the screen."
    )
    assert body["has_more"] is True
    assert len(ids_of(body)) == PAGE_SIZE

    second = await get_calls(client, token, **days, search="5551", page=2)
    assert second.status_code == 200, second.text
    assert second.json()["has_more"] is False

    walked = ids_of(body) + ids_of(second.json())
    assert len(walked) == len(set(walked)) == matching, (
        "the pages of a searched result must partition it: no row twice, none missing"
    )
    assert set(walked) == wanted, (
        "a row that does not carry the digits leaked into one of the pages"
    )

    narrowed = await get_calls(client, token, **days, search="5551", employee=USER_B)
    assert narrowed.status_code == 200, narrowed.text
    assert set(ids_of(narrowed.json())) <= wanted, (
        "the search must AND with the employee facet. If they OR, every added filter "
        "WIDENS the result - the bug `test_each_filter_narrows_...` pins for the others."
    )
    assert narrowed.json()["total"] < matching


async def test_the_search_is_echoed_back_raw_and_an_over_long_one_is_a_machine_code(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """What the SPA gets back, and the one way this facet is allowed to fail.

    The echo carries what was *typed*, not the digits it was reduced to: the input is still
    on the screen above the table, and a server answering `9012345` to a search for
    `+998 90 123 45 67` would be describing a query the reader never made.

    The length cap is answered as `bad_search` with the limit attached (§8: machine codes,
    never sentences) so the SPA can state the limit instead of discovering it. The client
    caps its field at the same number, which is what keeps this path unreachable in
    practice - but an unreachable path that answers with a 500 is still a moderation
    finding, and this endpoint replaces the whole table with an error card on a failed
    first page.
    """
    await seed_search_rows(portal.portal_id)
    token = session_for(portal)
    days = period(BASE_DAY, BASE_DAY)

    typed = "+998 90 123 45 67"
    response = await get_calls(client, token, **days, search=typed)
    assert response.status_code == 200, response.text
    assert response.json()["table_filters"]["search"] == typed, (
        "the echo must be the raw needle, so the SPA can re-render what was typed"
    )

    unfiltered = await get_calls(client, token, **days)
    assert unfiltered.json()["table_filters"]["search"] is None

    at_limit = await get_calls(client, token, **days, search="9" * 64)
    assert at_limit.status_code == 200, "64 characters is the limit, not one past it"

    too_long = await get_calls(client, token, **days, search="9" * 65)
    assert too_long.status_code == 400, too_long.text
    assert too_long.json() == {"code": "bad_search", "max_length": 64}, (
        "§8: a machine code plus the limit, never a translated sentence"
    )


async def test_an_own_viewer_cannot_use_the_search_to_reach_a_colleagues_call(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The search is a facet, not a way around `scope_filter` (§4.7).

    `_filtered` layers the table facets onto `calls_repo.base_select`, so the scope is
    already on the statement before the search narrows it. It is written down because the
    one refactor that would break it - building the search into a statement of its own -
    would still return perfectly plausible rows, and the rows would be somebody else's.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 9, 0, tzinfo=UTC)
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {"bx_id": 1, "started": day, "user_id": USER_A, "phone": "+998901110000"},
            {
                "bx_id": 2,
                "started": day + timedelta(minutes=1),
                "user_id": USER_B,
                "phone": "+998902220000",
            },
        ],
    )
    days = period(BASE_DAY, BASE_DAY)
    own = session_for(portal, user_id=USER_A, access="own", is_admin=False)

    response = await get_calls(client, own, **days, search="2220000")
    assert response.status_code == 200, response.text
    assert ids_of(response.json()) == [], (
        "an `own` viewer searched for a number only a colleague's call carries and was "
        "shown it. The scope predicate must be applied before the facet, not beside it."
    )

    mine = await get_calls(client, own, **days, search="1110000")
    assert ids_of(mine.json()) == [identifiers[1]], (
        "the same viewer must still find their own call by the same means"
    )


# --- the CRM tab -----------------------------------------------------------------------


async def test_the_deal_tab_matches_all_three_ways_including_a_raw_deal_row(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.8's three-way OR, one seeded call per clause plus one that must not match.

    Each clause has a portal behind it. Clause 1 is the documented case - telephony rows
    carry CONTACT/COMPANY/LEAD, so a deal is matched through the entities resolved onto it.
    Clause 2 catches the call whose `crm_entity_*` points at the contact while the link to
    the deal exists only as a timeline activity. Clause 3 is assumption 18: some portals
    emit `CRM_ENTITY_TYPE='DEAL'` outright, and §3 stores the value raw precisely so those
    rows survive - without this clause they sit in `calls` while the tab says the deal has
    no calls, which is the kind of bug that gets reported as "your app does not work".

    The negative row is what stops this passing on an implementation that ignores the
    context entirely and returns the portal's whole history on a CRM tab.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 10, 0, tzinfo=UTC)
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {"bx_id": 1, "started": day, "crm_type": "CONTACT", "crm_id": DEAL_CONTACT},
            {
                "bx_id": 2,
                "started": day + timedelta(minutes=1),
                "crm_type": "CONTACT",
                "crm_id": 999,
                "activity_id": DEAL_ACTIVITY,
            },
            {
                "bx_id": 3,
                "started": day + timedelta(minutes=2),
                "crm_type": "DEAL",
                "crm_id": DEAL_ID,
            },
            {
                "bx_id": 4,
                "started": day + timedelta(minutes=3),
                "crm_type": "COMPANY",
                "crm_id": DEAL_COMPANY,
            },
            {
                "bx_id": 5,
                "started": day + timedelta(minutes=4),
                "crm_type": "CONTACT",
                "crm_id": 999,
                "activity_id": 9999,
            },
        ],
    )
    await store_context(portal.portal_id, resolved_by=USER_A)
    token = session_for(
        portal,
        user_id=USER_A,
        entity={"t": "DEAL", "id": DEAL_ID},
        placement="CRM_DEAL_DETAIL_TAB",
    )

    response = await get_calls(client, token, **AROUND_BASE_DAY)
    assert response.status_code == 200, response.text
    got = set(ids_of(response.json()))

    assert identifiers[1] in got, "clause 1 (resolved entity keys: the deal's contact) missed"
    assert identifiers[2] in got, "clause 2 (crm_activity_id from the deal's timeline) missed"
    assert identifiers[3] in got, (
        "clause 3 missed a row the portal emitted with CRM_ENTITY_TYPE='DEAL'. Assumption "
        "18 and the §3 column comment exist for exactly these portals; without the clause "
        "their deal tab is permanently empty while the rows sit in `calls`."
    )
    assert identifiers[4] in got, "clause 1 must cover the company key as well as the contact"
    assert identifiers[5] not in got, (
        "an unrelated call appeared on the deal tab: the context is not being applied at all"
    )


async def test_a_crm_tab_still_applies_the_permission_scope(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.8 ends with "Then `scope_filter`, then period/paging" - in that order, not instead.

    A CRM tab is the easy place to lose the scope, because the context already looks like a
    filter. It is not one: it says which calls belong to the card, never who may see them.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 10, 0, tzinfo=UTC)
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {
                "bx_id": 1,
                "started": day,
                "crm_type": "CONTACT",
                "crm_id": DEAL_CONTACT,
                "user_id": USER_A,
            },
            {
                "bx_id": 2,
                "started": day + timedelta(minutes=1),
                "crm_type": "CONTACT",
                "crm_id": DEAL_CONTACT,
                "user_id": USER_B,
            },
        ],
    )
    await store_context(portal.portal_id, resolved_by=USER_B)
    token = session_for(
        portal,
        user_id=USER_B,
        access="own",
        is_admin=False,
        entity={"t": "DEAL", "id": DEAL_ID},
        placement="CRM_DEAL_DETAIL_TAB",
    )

    response = await get_calls(client, token, **AROUND_BASE_DAY)
    assert response.status_code == 200, response.text
    assert set(ids_of(response.json())) == {identifiers[2]}, (
        "the deal tab served a colleague's call to an 'own' viewer: the CRM match replaced "
        "the scope predicate instead of being combined with it (§4.8)."
    )


async def test_a_stale_context_answers_409_context_missing_rather_than_serving_it(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.8: the row must be `resolved_at >= JWT.iat` **or** resolved by this same user.

    `crm_contexts` is a shared, per-portal cache. Without this rule an administrator who
    opened a deal yesterday leaves behind a row listing its contacts, its company and every
    call activity on it, and the next request from a salesperson who cannot read that deal
    is answered from it - the administrator's rights, silently inherited.

    409 rather than 403 or an empty list is the contract: it is an instruction to the SPA
    to run the §4.6 session exchange, which re-resolves the entity with the caller's own
    Bitrix24 token. A user who does have the rights gets their tab one round trip later; a
    user who does not gets `crm_no_access` decided by Bitrix24 rather than by our cache. An
    empty list would tell them "this deal has no calls", which is a lie, and a 403 would
    strand a legitimate user for as long as the cache lives.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 10, 0, tzinfo=UTC)
    await seed_calls(
        portal.portal_id,
        [{"bx_id": 1, "started": day, "crm_type": "CONTACT", "crm_id": DEAL_CONTACT}],
    )
    # Resolved an hour ago, by somebody else: both halves of the rule fail.
    await store_context(portal.portal_id, resolved_by=USER_A, age=timedelta(hours=1))
    token = session_for(
        portal,
        user_id=USER_B,
        entity={"t": "DEAL", "id": DEAL_ID},
        placement="CRM_DEAL_DETAIL_TAB",
    )

    response = await get_calls(client, token, **AROUND_BASE_DAY)

    assert response.status_code == 409, (
        f"expected 409 context_missing for a context this session may not reuse, got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json() == {"code": "context_missing"}


async def test_a_missing_context_is_409_and_not_the_whole_portal(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The same code when there is no row at all - the fail-closed half of §4.8.

    The dangerous alternative is not an error: it is reading "no context" as "no
    restriction" and answering with the portal's entire call history inside a deal card.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 10, 0, tzinfo=UTC)
    await seed_calls(portal.portal_id, [{"bx_id": 1, "started": day, "user_id": USER_A}])
    token = session_for(
        portal,
        user_id=USER_A,
        entity={"t": "DEAL", "id": DEAL_ID},
        placement="CRM_DEAL_DETAIL_TAB",
    )

    response = await get_calls(client, token, **AROUND_BASE_DAY)

    assert response.status_code == 409
    assert response.json() == {"code": "context_missing"}


# --- the recording URL, once more, deliberately ----------------------------------------


async def test_no_response_carries_the_recording_url_at_any_depth(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§9 / decision 19: `call_record_url` never leaves the server, in any shape.

    Every request in this file already runs through `assert_no_record_url`; this test makes
    the guarantee explicit and exercises the shapes most likely to carry the URL by
    accident - a row that *has* a recording, an employee joined onto it, and the `play-url`
    response whose whole job is to talk about that recording without disclosing it.

    The row must still SAY there is a recording: §9 keeps the "has recording" icon and the
    `BX24.openPath` link while `RECORDING_MODE` is `off`, so a projection that dropped
    `has_record` along with the URL would remove the affordance too.
    """
    day = datetime(BASE_DAY.year, BASE_DAY.month, BASE_DAY.day, 10, 0, tzinfo=UTC)
    identifiers = await seed_calls(
        portal.portal_id,
        [
            {"bx_id": 1, "started": day, "record_url": RECORD_URL, "duration": 120},
            {"bx_id": 2, "started": day + timedelta(minutes=1), "record_url": RECORD_URL},
            {"bx_id": 3, "started": day + timedelta(minutes=2)},
        ],
    )
    await seed_employee(portal.portal_id, USER_A, "Aziza")
    token = session_for(portal)

    listing = await get_calls(client, token, **period(BASE_DAY, BASE_DAY))
    assert listing.status_code == 200, listing.text
    rows = {int(row["id"]): row for row in rows_of(listing.json())}
    assert set(rows) == set(identifiers.values())
    assert rows[identifiers[1]]["has_record"] is True, (
        "the table still has to say a recording exists (§9: until the spike is answered the "
        "row shows an icon and a link) - it just must not say where it is."
    )
    assert rows[identifiers[3]]["has_record"] is False

    minted = await client.post(
        f"{CALLS_PATH}/{identifiers[1]}/play-url",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert_no_record_url(minted)
    if settings.recording_mode == "off":
        assert minted.status_code == 409 and minted.json()["code"] == "recording_disabled", (
            "with playback off the SPA must be able to tell 'we cannot play this here' from "
            "'there is nothing to play' (§9); a 404 would hide a recording that exists."
        )
