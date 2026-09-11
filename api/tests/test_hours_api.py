"""`GET /api/v1/hours` — the grid of talk time by employee, by local day, by hour.

This endpoint is one `GROUP BY` and a Python assembly step, and each half fails in its own
way.

*The SQL half* fails in the timezone. A day bucket and an hour bucket are both derived
from `call_start_date AT TIME ZONE :tz`, and the two are not independent: a call at 23:30
in Tashkent is 18:30 UTC on the same date, while one at 01:30 Tashkent is 20:30 UTC on the
*previous* one. An implementation that converts the hour but bounds the day in UTC gets
every late-evening and early-morning call wrong, and gets it wrong quietly - the numbers
are all still plausible, they are just filed one row up or down. So the same seeded set is
read twice, in two zones, and the whole (day, hour) placement is compared.

*The Python half* fails in the shape. The grid is dense - twenty-four cells per row
whether or not a call happened in each - and the two numbers in a cell describe different
sets of calls on purpose: talk time is answered calls only, the count is every call. That
asymmetry is the entire point of the page (forty-three attempts, twenty minutes of
conversation), and it is exactly the kind of thing a later reader "fixes" into consistency.
It is asserted directly.

Two properties are not about this endpoint at all and are here because every read of
`calls` must have them: an `own` viewer reaches only their own rows, and one portal never
sees another's. `scope_filter` is applied by `base_select` before this module's predicates,
and the test that proves it is the one that would still pass if somebody built the grid
from a `select(Call)` of its own - with somebody else's rows in it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.session import tenant_txn
from app.main import create_app
from app.security.session_token import issue_session
from tests.fixtures.bitrix import SeededPortal, delete_portal, seed_portal

HOURS_PATH: Final[str] = "/api/v1/hours"

TASHKENT: Final[str] = "Asia/Tashkent"  # UTC+5, no DST
UTC_ZONE: Final[str] = "UTC"

DAY: Final[date] = date(2026, 3, 10)
NEXT_DAY: Final[date] = date(2026, 3, 11)

USER_A: Final[int] = 101
USER_B: Final[int] = 102


# --- seeding ---------------------------------------------------------------------------


async def seed_call(
    portal_id: int,
    bx_id: int,
    *,
    started: datetime,
    code: str = "200",
    user_id: int | None = USER_A,
    duration: int = 60,
) -> None:
    """One `calls` row, under tenant context (§3: RLS makes a control-plane insert a no-op)."""
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO calls (portal_id, bx_id, call_id, call_type, call_start_date,
                                   call_duration, call_failed_code, portal_user_id,
                                   phone_number, portal_number)
                VALUES (:pid, :bx_id, :call_id, 1, :started,
                        :duration, :code, :user_id, :phone, 'line-1')
                """
            ),
            {
                "pid": portal_id,
                "bx_id": bx_id,
                "call_id": f"hours-{bx_id}",
                "started": started,
                "duration": duration,
                "code": code,
                "user_id": user_id,
                "phone": f"+99890000{bx_id:04d}",
            },
        )


async def seed_employee(
    portal_id: int, user_id: int, name: str, phone_inner: str | None = None
) -> None:
    async with tenant_txn(portal_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO employees (portal_id, bx_user_id, name, last_name, phone_inner,
                                       active, found, fetched_at)
                VALUES (:pid, :uid, :name, 'Operator', :ext, true, true, now())
                ON CONFLICT (portal_id, bx_user_id) DO NOTHING
                """
            ),
            {"pid": portal_id, "uid": user_id, "name": name, "ext": phone_inner},
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
    timezone: str = TASHKENT,
    is_admin: bool = True,
) -> str:
    return issue_session(
        pid=portal.portal_id,
        mid=portal.member_id,
        sub=user_id,
        adm=is_admin,
        acc=access,
        tz=timezone,
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )


async def get_hours(
    client: httpx.AsyncClient,
    token: str,
    *,
    start: date = DAY,
    end: date = NEXT_DAY,
    **extra: Any,
) -> httpx.Response:
    params: dict[str, Any] = {
        "period": "custom",
        "from": start.isoformat(),
        "to": end.isoformat(),
        **{key: value for key, value in extra.items() if value is not None},
    }
    return await client.get(
        HOURS_PATH, params=params, headers={"Authorization": f"Bearer {token}"}
    )


def cell(row: Mapping[str, Any], hour: int) -> tuple[int, int]:
    """One cell as `(talk_seconds, calls)`, with the shape contract asserted once."""
    hours = row["hours"]
    assert isinstance(hours, Sequence) and len(hours) == 24, (
        "every row carries all twenty-four hours, zero-filled. A sparse row would make the "
        f"grid's columns mean different hours on different rows. Got {len(hours)}."
    )
    pair = hours[hour]
    assert isinstance(pair, Sequence) and len(pair) == 2
    return int(pair[0]), int(pair[1])


def keyed(body: Mapping[str, Any]) -> dict[tuple[Any, str], Mapping[str, Any]]:
    """Rows by `(employee_id, date)`, which is what a row IS."""
    rows = body["rows"]
    assert isinstance(rows, list)
    out: dict[tuple[Any, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (row["employee_id"], row["date"])
        assert key not in out, f"the same (employee, day) came back twice: {key}"
        out[key] = row
    return out


# --- the buckets -----------------------------------------------------------------------


async def test_a_call_lands_in_the_viewers_own_day_and_hour(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The same three calls, read in two zones, land in different cells - correctly.

    `21:30 UTC` is `02:30` the NEXT day in Tashkent, and that is the assertion that matters:
    the day and the hour are derived from one conversion, so an implementation that
    converts the clock but bounds the date in UTC puts this call on the wrong row while
    every number on it still looks reasonable.
    """
    await seed_employee(portal.portal_id, USER_A, "Ada")
    # 06:10 UTC -> 11:10 Tashkent, same day.
    await seed_call(portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 10, tzinfo=UTC))
    # 21:30 UTC -> 02:30 Tashkent, the NEXT day.
    await seed_call(portal.portal_id, 2, started=datetime(2026, 3, 10, 21, 30, tzinfo=UTC))

    tashkent = await get_hours(client, session_for(portal, timezone=TASHKENT))
    assert tashkent.status_code == 200, tashkent.text
    rows = keyed(tashkent.json())
    assert set(rows) == {(USER_A, "2026-03-10"), (USER_A, "2026-03-11")}, (
        "the late call belongs to the viewer's next day, on its own row"
    )
    assert cell(rows[(USER_A, "2026-03-10")], 11) == (60, 1)
    assert cell(rows[(USER_A, "2026-03-11")], 2) == (60, 1)

    utc = await get_hours(client, session_for(portal, timezone=UTC_ZONE))
    assert utc.status_code == 200, utc.text
    utc_rows = keyed(utc.json())
    assert set(utc_rows) == {(USER_A, "2026-03-10")}, (
        "in UTC both calls are the same day; the day bucket must follow the zone, not the "
        "server's calendar"
    )
    assert cell(utc_rows[(USER_A, "2026-03-10")], 6) == (60, 1)
    assert cell(utc_rows[(USER_A, "2026-03-10")], 21) == (60, 1)


async def test_talk_time_counts_answered_calls_and_the_count_counts_all_of_them(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The asymmetry the page exists for, pinned so nobody tidies it away.

    Four calls in one hour, one of them answered. The cell reads "one minute, four calls" -
    four attempts and sixty seconds of conversation to show for them. Making the two
    numbers describe the same set of calls would look like a consistency fix and would
    delete the comparison this grid is read for.

    The unanswered rows carry a non-zero `call_duration` on purpose: §3 stores whatever
    Bitrix24 sent, and a sum that forgot its FILTER would quietly pass a test where the
    unanswered calls all had duration zero.
    """
    await seed_employee(portal.portal_id, USER_A, "Ada")
    at = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)  # 11:00 Tashkent
    await seed_call(portal.portal_id, 1, started=at, code="200", duration=60)
    await seed_call(portal.portal_id, 2, started=at, code="304", duration=45)
    await seed_call(portal.portal_id, 3, started=at, code="603", duration=30)
    await seed_call(portal.portal_id, 4, started=at, code="486", duration=15)

    response = await get_hours(client, session_for(portal))
    assert response.status_code == 200, response.text
    row = keyed(response.json())[(USER_A, "2026-03-10")]

    assert cell(row, 11) == (60, 4), (
        "talk time is `call_duration` over ANSWERED calls only; the count is every call in "
        "the hour. A sum without its FILTER reads (150, 4) here."
    )
    assert row["talk_seconds"] == 60 and row["calls"] == 4, (
        "the row totals are the row's own cells, added up"
    )
    assert all(cell(row, hour) == (0, 0) for hour in range(24) if hour != 11), (
        "every other hour of the row is a real zero, not a missing key"
    )


async def test_each_employee_and_day_is_its_own_row_and_empty_days_are_absent(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """A row is one (employee, day) — and only where there were calls.

    The alternative, a row per selected employee per day of the period, fills a week-long
    view with rows that are twenty-four zeroes wide. The reader scrolls past them to find
    the two that say something.
    """
    await seed_employee(portal.portal_id, USER_A, "Ada")
    await seed_employee(portal.portal_id, USER_B, "Ben")
    await seed_call(
        portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), user_id=USER_A
    )
    await seed_call(
        portal.portal_id, 2, started=datetime(2026, 3, 11, 6, 0, tzinfo=UTC), user_id=USER_A
    )
    await seed_call(
        portal.portal_id, 3, started=datetime(2026, 3, 11, 7, 0, tzinfo=UTC), user_id=USER_B
    )

    response = await get_hours(client, session_for(portal))
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(keyed(body)) == {
        (USER_A, "2026-03-10"),
        (USER_A, "2026-03-11"),
        (USER_B, "2026-03-11"),
    }, "USER_B has no row for the 10th, because USER_B made no calls on the 10th"

    dates = [row["date"] for row in body["rows"]]
    assert dates == sorted(dates, reverse=True), (
        "newest day first: the question is nearly always about this week"
    )
    assert body["total_rows"] == 3 and body["truncated"] is False


async def test_an_employees_name_and_extension_travel_with_the_row(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§7: the grid names people, and it names them the way the filter above it does."""
    await seed_employee(portal.portal_id, USER_A, "Ada", phone_inner="101")
    await seed_call(portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC))

    response = await get_hours(client, session_for(portal))
    row = keyed(response.json())[(USER_A, "2026-03-10")]
    assert row["name"] == "Ada Operator"
    assert row["phone_inner"] == "101"
    assert row["unassigned"] is False


async def test_a_call_with_no_employee_gets_a_row_instead_of_disappearing(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§3 allows a NULL `portal_user_id`, and this page must not pretend otherwise.

    Such a call is nobody's, so it cannot be attributed - but filtering it out would make
    this page disagree with every other count in the app about how many calls the portal
    made, and disagree silently. It gets a row flagged `unassigned`, exactly as the
    dashboard's employee breakdown keeps its own bucket for the same rows.
    """
    await seed_call(
        portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), user_id=None
    )

    response = await get_hours(client, session_for(portal))
    assert response.status_code == 200, response.text
    rows = keyed(response.json())
    assert set(rows) == {(None, "2026-03-10")}
    row = rows[(None, "2026-03-10")]
    assert row["unassigned"] is True and row["name"] is None, (
        "the SPA names this bucket itself (§8); the server must not invent a sentence"
    )


# --- the filters -----------------------------------------------------------------------


async def test_several_employees_can_be_selected_at_once(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The multi-select needed nothing on this side, and this is what proves it.

    `employee` has always been a repeatable parameter (`parse_filters`), so two ids are an
    `IN` list. Only the control was missing.
    """
    for index, user_id in enumerate((USER_A, USER_B, 103), start=1):
        await seed_employee(portal.portal_id, user_id, f"User{user_id}")
        await seed_call(
            portal.portal_id,
            index,
            started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC),
            user_id=user_id,
        )

    everyone = await get_hours(client, session_for(portal))
    assert {key[0] for key in keyed(everyone.json())} == {USER_A, USER_B, 103}, (
        "no employee filter means every employee, not none"
    )

    two = await get_hours(client, session_for(portal), employee=[USER_A, 103])
    assert two.status_code == 200, two.text
    assert {key[0] for key in keyed(two.json())} == {USER_A, 103}


async def test_an_own_viewer_reaches_only_their_own_rows(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: the scope is on the statement before this module narrows anything.

    Written down because the refactor that breaks it — building the grid from a
    `select(Call)` of its own instead of `calls_repo.base_select` — returns a perfectly
    plausible grid, and the rows in it are somebody else's working day.
    """
    await seed_employee(portal.portal_id, USER_A, "Ada")
    await seed_employee(portal.portal_id, USER_B, "Ben")
    await seed_call(
        portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), user_id=USER_A
    )
    await seed_call(
        portal.portal_id, 2, started=datetime(2026, 3, 10, 7, 0, tzinfo=UTC), user_id=USER_B
    )

    own = session_for(portal, user_id=USER_A, access="own", is_admin=False)
    response = await get_hours(client, own)
    assert response.status_code == 200, response.text
    assert {key[0] for key in keyed(response.json())} == {USER_A}, (
        "an `own` viewer was shown a colleague's hours"
    )

    asked = await get_hours(client, own, employee=USER_B)
    assert {key[0] for key in keyed(asked.json())} == set(), (
        "and cannot reach them by naming the colleague in the filter either"
    )


async def test_a_denied_viewer_is_refused_the_grid_outright(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: `denied` is Bitrix24's own answer about this user, and we do not overrule it."""
    denied = session_for(portal, access="denied", is_admin=False)
    response = await get_hours(client, denied)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "no_stats_permission"


async def test_one_portal_never_sees_anothers_grid(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """`calls` carries FORCED RLS (§3); this asserts the predicate as well as the policy."""
    other = await seed_portal()
    try:
        await seed_call(
            portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), user_id=USER_A
        )
        await seed_call(
            other.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), user_id=USER_B
        )

        mine = await get_hours(client, session_for(portal))
        assert {key[0] for key in keyed(mine.json())} == {USER_A}

        theirs = await get_hours(client, session_for(other))
        assert {key[0] for key in keyed(theirs.json())} == {USER_B}
    finally:
        await wipe(other.portal_id)
        await delete_portal(other.member_id)


async def test_a_malformed_period_is_a_machine_code_and_not_a_500(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§8: this page answers a bad filter exactly as `/dashboard` and `/calls` do."""
    token = session_for(portal)

    half = await client.get(
        HOURS_PATH,
        params={"period": "custom", "from": DAY.isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert half.status_code == 400, half.text
    assert half.json()["code"] == "bad_period"

    too_long = await get_hours(client, token, start=date(2020, 1, 1), end=date(2026, 1, 1))
    assert too_long.status_code == 400, too_long.text
    assert too_long.json() == {"code": "period_too_long", "max_days": 366}


async def test_the_row_cap_truncates_and_says_that_it_did(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """CONTRIBUTING: a bounded answer has to admit that it is bounded.

    The cap is on the ANSWER, so the response still reports how many rows there really
    were. Without `total_rows` the grid reads as "this is all of it", and a manager
    comparing a team over a month would be reading a silently clipped table.

    The cap is exercised through the API rather than by calling the service directly,
    because the field the SPA reads is the one that has to be right.
    """
    from app.services.stats import _HOUR_ROW_CAP

    rows_wanted = _HOUR_ROW_CAP + 3
    async with tenant_txn(portal.portal_id) as session:
        # One call per employee, all in the same hour of the same day: the cheapest way to
        # make more (employee, day) pairs than the cap allows.
        await session.execute(
            text(
                """
                INSERT INTO calls (portal_id, bx_id, call_id, call_type, call_start_date,
                                   call_duration, call_failed_code, portal_user_id,
                                   phone_number, portal_number)
                SELECT :pid, g, 'cap-' || g, 1, :started, 60, '200', g,
                       '+998900000000', 'line-1'
                FROM generate_series(1, :rows) AS g
                """
            ),
            {
                "pid": portal.portal_id,
                "started": datetime(2026, 3, 10, 6, 0, tzinfo=UTC),
                "rows": rows_wanted,
            },
        )

    response = await get_hours(client, session_for(portal))
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["rows"]) == _HOUR_ROW_CAP
    assert body["total_rows"] == rows_wanted
    assert body["truncated"] is True
    assert body["row_cap"] == _HOUR_ROW_CAP


async def test_the_colour_scale_is_normalised_over_the_answer_the_client_receives(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """`max_cell_seconds` is the ramp's top, and it is computed server-side.

    A client that re-derived it would be normalising over whatever it was sent, which after
    truncation is a different table than the one the number describes. Server-side, the two
    always agree.
    """
    await seed_employee(portal.portal_id, USER_A, "Ada")
    await seed_call(
        portal.portal_id, 1, started=datetime(2026, 3, 10, 6, 0, tzinfo=UTC), duration=60
    )
    await seed_call(
        portal.portal_id, 2, started=datetime(2026, 3, 10, 7, 0, tzinfo=UTC), duration=900
    )
    # Unanswered, and longer than both: it must not raise the ramp's top, because it is not
    # talk time and no cell will ever be painted with it.
    await seed_call(
        portal.portal_id,
        3,
        started=datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
        code="304",
        duration=5000,
    )

    body = (await get_hours(client, session_for(portal))).json()
    assert body["max_cell_seconds"] == 900, (
        "the ramp is normalised over cell TALK time, which the unanswered call is not part of"
    )
