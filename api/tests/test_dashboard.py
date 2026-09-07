"""§4.7 / §10 step 5 — the dashboard numbers, and the timezone they are counted in.

A dashboard is the one part of this app whose bugs are *plausible*. A missing row in a
table is noticed; a summary card that is 11 % low, or a "Monday" column that is really
"Sunday from 19:00 UTC onwards", looks exactly like a quiet week. So this file seeds a
call set whose every figure is known by construction and asserts the arithmetic outright:
totals, the answered rate, the missed count, talk time, the per-day series, the
hour x weekday matrix and the per-employee list.

The centre of the file is the timezone proof. Decision 9 and assumption 16 put the
aggregation in the **viewer's** `TIME_ZONE`, which means the day and hour a call lands in
are a property of who is looking, not of the server. One call at 20:30 UTC belongs to
Tuesday for a viewer in UTC and to Wednesday 01:30 for a viewer in Tashkent, so
`from=2026-03-11&to=2026-03-11` must *include* it for one and *exclude* it for the other.
A server-timezone bug here is wrong by one day at the edges and right everywhere else:
invisible in review, invisible in a demo, and it surfaces months later as a support
ticket about "yesterday's calls".

`services/stats.py` computes all four axes in a single `GROUPING SETS` pass, which is
cheap and has one characteristic failure: the roll-up row and the detail rows come back
in the same result set, told apart only by `GROUPING()` bits, so a mis-read bit adds the
summary to the per-day series or files an employee as a weekday. Every test below
therefore checks that the axes *agree* - each breakdown must re-add to the same total -
rather than checking any one of them alone.

The three permission shapes are asserted on the same data, because they are the same
query with one predicate different (§4.7): `all` sees the portal, `own` sees one
employee's rows and nothing else, `denied` gets 403 and no numbers at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
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

DASHBOARD: Final[str] = "/api/v1/dashboard"

#: UTC+5, no DST - so "the viewer's day" is a pure offset question and a failure cannot be
#: blamed on a transition.
TASHKENT: Final[str] = "Asia/Tashkent"
UTC_TZ: Final[str] = "UTC"

#: 2026-03-10 is a Tuesday (isodow 2); 2026-03-11 a Wednesday (isodow 3). `stats.py`
#: buckets the matrix with `EXTRACT(isodow …)`, so both numbers are asserted below.
DAY: Final[date] = date(2026, 3, 10)
DAY_ISODOW: Final[int] = 2
NEXT_DAY: Final[date] = date(2026, 3, 11)
NEXT_DAY_ISODOW: Final[int] = 3

USER_A: Final[int] = 101
USER_B: Final[int] = 102

#: (bx_id, UTC time of day, failure code, user, duration seconds)
#:
#: Chosen so every figure is exact and none of them coincide: 8 calls, 4 answered
#: (600 s of talk time between them), 2 missed, 2 not connected; 5 belong to USER_A and 3
#: to USER_B; in Tashkent they fall in hours 11..17 with hour 11 carrying two, which is
#: what makes the heatmap assertion more than "something is non-zero".
CALLS: Final[tuple[tuple[int, tuple[int, int], str, int, int], ...]] = (
    (1, (6, 10), "200", USER_A, 60),
    (2, (6, 40), "200", USER_A, 120),
    (3, (7, 15), "200", USER_B, 180),
    (4, (8, 20), "200", USER_B, 240),
    (5, (9, 5), "304", USER_A, 0),
    (6, (10, 30), "304", USER_A, 0),
    (7, (11, 45), "603", USER_A, 0),
    (8, (12, 50), "603", USER_B, 0),
)

TOTAL: Final[int] = 8
ANSWERED: Final[int] = 4
MISSED: Final[int] = 2
NOT_CONNECTED: Final[int] = 2
#: §10 step 5 / `stats._talk`: talk time is `call_duration` over ANSWERED calls only.
TALK_TOTAL: Final[int] = 600
TALK_AVERAGE: Final[int] = 150
#: The local hour of each seeded call, as a multiset, in each of the two viewer zones.
TASHKENT_HOURS: Final[tuple[int, ...]] = (11, 11, 12, 13, 14, 15, 16, 17)
UTC_HOURS: Final[tuple[int, ...]] = (6, 6, 7, 8, 9, 10, 11, 12)


# --- reading the payload ---------------------------------------------------------------
#
# The accessors below accept the handful of synonyms `services/stats.py` deliberately
# emits (`range`/`period`, `talk_time_total`/`talk_seconds`, `count`/`total` on a matrix
# cell) - the module ships several spellings of the same number precisely because a chart
# component, a table component and this file all read it. Nothing here is tolerant about a
# *value*: every number below is compared exactly.


def _pick(payload: Any, *names: str) -> Any:
    assert isinstance(payload, Mapping), f"expected an object, got {type(payload).__name__}"
    for name in names:
        if name in payload:
            return payload[name]
    raise AssertionError(f"none of {list(names)} in {sorted(payload)}")


def range_of(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """The echo of the period actually aggregated - what the page labels its axis with."""
    return _pick(body, "range", "period")


def summary_of(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return body["summary"]


def talk_total(block: Mapping[str, Any]) -> int:
    if "talk_time" in block:
        return int(block["talk_time"]["total_seconds"])
    return int(_pick(block, "talk_time_total", "talk_seconds"))


def talk_average(block: Mapping[str, Any]) -> float | None:
    if "talk_time" in block:
        return block["talk_time"]["average_seconds"]
    return _pick(block, "talk_time_avg", "average_seconds")


def talk_basis(block: Mapping[str, Any]) -> str:
    if "talk_time" in block:
        return str(block["talk_time"]["basis"])
    return str(_pick(block, "talk_basis"))


def day_series(body: Mapping[str, Any]) -> dict[str, int]:
    """`{"2026-03-10": 8}`. `stats.py` zero-fills the axis, so absent days are 0, not gone."""
    return {row["date"]: int(row["total"]) for row in body["per_day"]}


def _matrix(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = _pick(body, "hour_weekday", "heatmap")
    cells = raw["cells"] if isinstance(raw, Mapping) else raw
    assert len(cells) == 7 * 24, (
        f"the matrix has {len(cells)} cells; it must be dense (7 x 24). A heatmap with "
        "holes has to be read twice, and the SPA would have to do date arithmetic in a "
        "timezone it does not own."
    )
    return list(cells)


def heatmap_cells(body: Mapping[str, Any]) -> dict[tuple[int, int], int]:
    """The non-empty `(isodow, hour)` cells of the 7 x 24 matrix."""
    cells = _matrix(body)
    assert {int(cell["weekday"]) for cell in cells} == {1, 2, 3, 4, 5, 6, 7}, (
        "ISO weekdays, Monday = 1 (that is what `EXTRACT(isodow …)` produces)"
    )
    assert {int(cell["hour"]) for cell in cells} == set(range(24))
    return {
        (int(cell["weekday"]), int(cell["hour"])): int(_pick(cell, "total", "count"))
        for cell in cells
        if int(_pick(cell, "total", "count"))
    }


def heatmap_max(body: Mapping[str, Any]) -> int:
    raw = _pick(body, "hour_weekday", "heatmap")
    if isinstance(raw, Mapping) and "max" in raw:
        return int(raw["max"])
    return int(body["hour_weekday_max"])


def all_employees(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every employee bucket - not the colour-capped series the chart draws."""
    rows = body.get("per_employee_all")
    if rows is None:
        block = body["per_employee"]
        rows = block["all"] if isinstance(block, Mapping) else block
    return list(rows)


def employee_totals(body: Mapping[str, Any]) -> dict[int | None, int]:
    return {
        _pick(row, "employee_id", "bx_user_id"): int(row["total"]) for row in all_employees(body)
    }


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
                VALUES (:pid, :bx_id, :call_id, :call_type, :started,
                        :duration, :code, :user_id, :phone, 'line-1')
                """
            ),
            {
                "pid": portal_id,
                "bx_id": bx_id,
                "call_id": f"dash-{bx_id}",
                "call_type": 1 + (bx_id % 2),
                "started": started,
                "duration": duration,
                "code": code,
                "user_id": user_id,
                "phone": f"+99890000{bx_id:04d}",
            },
        )


async def seed_the_known_set(portal_id: int) -> None:
    for bx_id, (hour, minute), code, user_id, duration in CALLS:
        await seed_call(
            portal_id,
            bx_id,
            started=datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=UTC),
            code=code,
            user_id=user_id,
            duration=duration,
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
    """A verified session, minted directly (§4.6).

    Going through `POST /app/` would drag the whole open flow - and a Bitrix24 fake - into
    a test about SQL aggregation. `issue_session` is the same function the handler calls,
    so the claims under test (`pid`, `sub`, `acc`, `tz`) are produced exactly as in
    production; §4.1's rule that these reach the database only from a signed token is what
    makes this both possible and safe.
    """
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


async def get_dashboard(
    client: httpx.AsyncClient, token: str, *, start: date, end: date
) -> httpx.Response:
    """`GET /dashboard` over an inclusive, viewer-local custom range (`stats.parse_filters`)."""
    return await client.get(
        DASHBOARD,
        params={"period": "custom", "from": start.isoformat(), "to": end.isoformat()},
        headers={"Authorization": f"Bearer {token}"},
    )


# --- the numbers -----------------------------------------------------------------------


async def test_the_summary_counts_the_seeded_calls_exactly(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Totals, the answered rate and talk time, to the unit.

    Every figure comes from `CALLS` above rather than from a second query, so this fails
    if the aggregation drops a `result_group`, sums the roll-up row into the detail rows
    (the classic `GROUPING SETS` mistake) or counts the duration of calls that never
    connected.

    Talk time is asserted together with its declared `basis`. §10 step 5 defines it over
    *answered* calls and has the response say so, because "average call 38 s" over a set
    that is two thirds missed calls is a number that means nothing and looks like it means
    something.
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal)

    response = await get_dashboard(client, token, start=DAY, end=DAY)
    assert response.status_code == 200, response.text
    body = response.json()
    summary = summary_of(body)

    assert summary["total"] == TOTAL
    assert summary["answered"] == ANSWERED
    assert summary["missed"] == MISSED
    assert summary["not_connected"] == NOT_CONNECTED
    assert summary["with_recording"] == 0
    assert summary["answered_rate"] == 0.5, (
        f"answered_rate is {summary['answered_rate']!r}; four answered out of eight is 0.5"
    )

    assert talk_basis(summary) == "answered", (
        "the response must declare which calls the average is over (§10 step 5), or the "
        "SPA cannot label the tile honestly."
    )
    assert talk_total(summary) == TALK_TOTAL, (
        "total talk time must sum `call_duration` over the answered calls; the missed and "
        "not-connected rows carry 0 s, so any other figure means rows were double counted."
    )
    assert talk_average(summary) == TALK_AVERAGE, "600 s over 4 answered calls is 150 s"

    echo = range_of(body)
    assert echo["from"] == DAY.isoformat() and echo["to"] == DAY.isoformat()
    assert echo["days"] == 1
    assert echo["timezone"] == TASHKENT, (
        "the page labels its own axis from this echo; it must name the zone the numbers "
        "were actually bucketed in."
    )
    assert body["access"] == "all"


async def test_an_empty_period_answers_zeroes_and_a_null_rate(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.11: "explicit 'No calls in this period' when empty" - which needs a real answer.

    Two distinct failures are ruled out here. A `GROUPING SETS` query returns its `()` row
    even over zero input rows, so an empty portal must still get a summary rather than a
    missing key or a 500. And the answered rate must be `null`, not `0.0`: "no calls yet"
    and "none of the calls were answered" are different facts, and a 0 % tile over an
    empty week is exactly the kind of number a moderator screens an app for.
    """
    token = session_for(portal)

    response = await get_dashboard(client, token, start=DAY, end=DAY)
    assert response.status_code == 200, response.text
    body = response.json()
    summary = summary_of(body)

    assert summary["total"] == 0 and summary["answered"] == 0
    assert summary["answered_rate"] is None, (
        "0.0 would render as '0 % answered' on a period with nothing in it"
    )
    assert talk_average(summary) is None
    assert day_series(body) == {DAY.isoformat(): 0}, "the axis is still drawn, zero-filled"
    assert all_employees(body) == []
    assert heatmap_cells(body) == {}, "and the matrix is still 7 x 24, all zeroes"


async def test_the_per_day_series_and_per_employee_list_split_the_same_total(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Every breakdown must re-add to the summary; a chart that does not is a lie told twice.

    The per-employee list is also the one place `employees` and `calls` meet (§7): the
    fixture seeds no `employees` rows at all, and the calls must still be attributed - the
    UI renders "User #id" for an unresolved id. A breakdown built as an inner join against
    the employee cache silently loses those calls, and the loss is invisible because the
    remaining bars still look like a plausible team.
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal)

    response = await get_dashboard(client, token, start=DAY, end=DAY)
    assert response.status_code == 200, response.text
    body = response.json()

    series = day_series(body)
    assert series == {DAY.isoformat(): TOTAL}, (
        f"the per-day series is {series!r}; all eight calls are inside this one "
        "viewer-local day and the period covers exactly that day."
    )

    per_employee = employee_totals(body)
    assert per_employee == {USER_A: 5, USER_B: 3}, (
        "the per-employee split is wrong or incomplete. Note that no `employees` row was "
        "seeded: §7 requires unresolved ids to still be reported (rendered as 'User #id'), "
        "so an inner join against the employee cache fails here."
    )
    assert sum(per_employee.values()) == TOTAL
    assert {row["name"] for row in all_employees(body)} == {None}, (
        "an uncached employee has no name; the server must send null and let the SPA "
        "render 'User #id' rather than inventing an untranslated placeholder (§8)."
    )
    assert sum(heatmap_cells(body).values()) == TOTAL


async def test_the_heatmap_places_every_call_in_its_viewer_local_hour_and_weekday(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The hour x weekday matrix, counted in the viewer's timezone (assumption 16).

    The seeded calls run 06:10 to 12:50 UTC, which is 11:10 to 17:50 in Tashkent. A matrix
    built with `date_trunc` on the stored `timestamptz` would put them in hours 6..12 and
    still look entirely reasonable - a team that starts at six in the morning - which is
    why the hours are asserted as an exact multiset rather than as "not empty".
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal, timezone=TASHKENT)

    response = await get_dashboard(client, token, start=DAY, end=DAY)
    assert response.status_code == 200, response.text
    body = response.json()
    cells = heatmap_cells(body)

    assert sum(cells.values()) == TOTAL, f"the heatmap holds {sum(cells.values())} of {TOTAL} calls"
    assert {weekday for weekday, _ in cells} == {DAY_ISODOW}, (
        f"every call is on one local weekday ({DAY} is a Tuesday, isodow {DAY_ISODOW}); "
        f"the matrix put them on {sorted({w for w, _ in cells})}"
    )
    hours = sorted(hour for (_, hour), count in cells.items() for _ in range(count))
    assert tuple(hours) == TASHKENT_HOURS, (
        f"the heatmap hours are {hours}; expected {list(TASHKENT_HOURS)}. "
        f"{list(UTC_HOURS)} would mean the aggregation ran in UTC, not in the viewer's "
        "timezone (§4.7)."
    )
    assert heatmap_max(body) == 2, "hour 11 carries two calls and is the peak cell"


# --- the timezone proof ----------------------------------------------------------------


async def test_one_call_lands_on_a_different_day_for_each_viewers_timezone(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Decision 9: "today" belongs to the viewer, not to the server.

    A single call at 2026-03-10 20:30 UTC. For a viewer in UTC that is Tuesday the 10th at
    20:00; for a viewer in Tashkent (+5) it is Wednesday the 11th at 01:30. Both viewers
    look at the same portal, the same row, over the same requested range - so any
    difference in the answer can only come from the timezone the SQL grouped in.

    The second half is the one that actually bites in production: the *period bounds* must
    move too. `from=2026-03-11&to=2026-03-11` is one call for the Tashkent viewer and zero
    for the UTC viewer. An implementation that buckets in the viewer's timezone but
    resolves the period with the server's offset passes the first half and gets the edge
    of every "today" and "7 days" preset wrong - by exactly one evening's calls, every day.
    """
    await seed_call(
        portal.portal_id,
        21,
        started=datetime(2026, 3, 10, 20, 30, tzinfo=UTC),
        code="200",
        duration=90,
    )
    tashkent = session_for(portal, timezone=TASHKENT)
    utc = session_for(portal, timezone=UTC_TZ)

    wide_start, wide_end = date(2026, 3, 9), date(2026, 3, 12)
    east = await get_dashboard(client, tashkent, start=wide_start, end=wide_end)
    west = await get_dashboard(client, utc, start=wide_start, end=wide_end)
    assert east.status_code == 200 and west.status_code == 200

    east_series, west_series = day_series(east.json()), day_series(west.json())
    assert east_series[NEXT_DAY.isoformat()] == 1, (
        f"a viewer in {TASHKENT} must see this call on {NEXT_DAY}; the series says {east_series!r}"
    )
    assert east_series[DAY.isoformat()] == 0, "and must NOT also see it on the UTC day"
    assert west_series[DAY.isoformat()] == 1, (
        f"a viewer in UTC must see the same call on {DAY}; the series says {west_series!r}"
    )
    assert west_series[NEXT_DAY.isoformat()] == 0

    assert heatmap_cells(east.json()) == {(NEXT_DAY_ISODOW, 1): 1}, (
        "in Tashkent the call is Wednesday 01:xx"
    )
    assert heatmap_cells(west.json()) == {(DAY_ISODOW, 20): 1}, "in UTC it is Tuesday 20:xx"

    # And the period bounds themselves, resolved in the viewer's timezone.
    east_narrow = await get_dashboard(client, tashkent, start=NEXT_DAY, end=NEXT_DAY)
    west_narrow = await get_dashboard(client, utc, start=NEXT_DAY, end=NEXT_DAY)
    assert summary_of(east_narrow.json())["total"] == 1, (
        "the requested day is a LOCAL day: 2026-03-11 in Tashkent begins at 2026-03-10 "
        "19:00 UTC, and this call is inside it."
    )
    assert summary_of(west_narrow.json())["total"] == 0, (
        "the same requested day in UTC begins five hours later and excludes the call; a "
        "shared server-side boundary would answer 1 here."
    )
    assert range_of(east_narrow.json())["timezone"] == TASHKENT
    assert range_of(west_narrow.json())["timezone"] == UTC_TZ


# --- permission shapes -----------------------------------------------------------------


async def test_an_own_principal_is_counted_over_its_own_calls_only(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: `acc='own'` narrows every read to `portal_user_id = sub`, summaries included.

    The scope predicate lives in `calls_repo.scope_filter` precisely so no endpoint can
    forget it, and a summary is the endpoint most likely to: the table below the chart
    would be correctly filtered while the cards above it report the whole portal, and both
    would look right on their own. The per-employee list must collapse to one row for a
    stronger reason - a non-admin who can see colleagues' names and call volumes has been
    shown data Bitrix24 withheld from them.
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal, user_id=USER_B, access="own", is_admin=False)

    response = await get_dashboard(client, token, start=DAY, end=DAY)
    assert response.status_code == 200, response.text
    body = response.json()
    summary = summary_of(body)

    assert summary["total"] == 3, (
        "USER_B owns three of the eight seeded calls; anything else means `scope_filter` "
        "was not applied to the aggregation (§4.7)."
    )
    assert summary["answered"] == 2 and summary["not_connected"] == 1
    assert talk_total(summary) == 420, "USER_B's two answered calls are 180 + 240"
    assert employee_totals(body) == {USER_B: 3}, (
        "the per-employee breakdown leaked colleagues to an 'own' viewer"
    )
    assert day_series(body) == {DAY.isoformat(): 3}
    assert sum(heatmap_cells(body).values()) == 3
    assert body["access"] == "own", "the SPA shows the 'you see your own calls only' banner"


async def test_a_denied_principal_gets_a_machine_code_and_no_numbers(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """§4.7: "`acc='denied'`: only `GET /me` answers; every data endpoint returns 403".

    A denied user is one Bitrix24 itself refused `voximplant.statistic.get` to, so serving
    them our cache would republish exactly what the portal withheld - the moderation issue
    this whole permission path exists to avoid. The answer is a machine code, not a
    sentence: §8 has the SPA translate, and the mandated "ask your administrator" copy
    lives in the shared message catalogue.
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal, user_id=USER_B, access="denied", is_admin=False)

    response = await get_dashboard(client, token, start=DAY, end=DAY)

    assert response.status_code == 403, response.text
    assert response.json() == {"code": "no_stats_permission"}, (
        f"expected the documented machine code and nothing else, got {response.json()!r}"
    )


# --- the period cap --------------------------------------------------------------------


async def test_a_range_longer_than_max_period_days_is_refused_not_silently_truncated(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """Assumption 16 / open question 10: custom periods are capped at `MAX_PERIOD_DAYS`.

    The cap exists because the aggregation runs live over a covering index, and an
    unbounded range on a 500k-row tenant is a slow query the moderator experiences as a
    hang. What matters here is that going over it *fails*: an implementation that quietly
    clamps to the last 366 days answers 200 with numbers that do not describe the period
    the user asked for, and nobody ever finds out.

    Both ends are asserted one request apart, so the refusal cannot be a general failure
    of long ranges or of that particular start date; and `max_days` must travel with the
    code so the SPA can state the limit instead of hard-coding a second copy of it (§8).
    """
    await seed_the_known_set(portal.portal_id)
    token = session_for(portal)
    start = date(2025, 1, 1)

    inside = await get_dashboard(
        client, token, start=start, end=start + timedelta(days=settings.max_period_days - 1)
    )
    assert inside.status_code == 200, (
        f"a range of exactly MAX_PERIOD_DAYS ({settings.max_period_days}) must be accepted; "
        f"got {inside.status_code}: {inside.text}"
    )
    assert range_of(inside.json())["days"] == settings.max_period_days

    over = await get_dashboard(
        client, token, start=start, end=start + timedelta(days=settings.max_period_days + 5)
    )
    assert over.status_code == 400, (
        "a range past MAX_PERIOD_DAYS was not refused. Whether it was truncated or "
        "actually executed the caller cannot tell - and truncation is the dangerous one, "
        f"because the answer looks right. Got {over.status_code}: {over.text}"
    )
    assert over.json() == {"code": "period_too_long", "max_days": settings.max_period_days}


async def test_a_backwards_or_unparsable_period_is_a_machine_code_too(
    client: httpx.AsyncClient, portal: SeededPortal
) -> None:
    """The rest of the filter contract: a bad query string never becomes a default period.

    A silently corrected filter is the same failure as a silently truncated one - the page
    draws an answer to a question nobody asked. `bad_result` is included because a typo in
    a facet would otherwise match no rows and read as "this team made no calls".
    """
    token = session_for(portal)
    headers = {"Authorization": f"Bearer {token}"}

    backwards = await client.get(
        DASHBOARD,
        params={"period": "custom", "from": "2026-03-12", "to": "2026-03-10"},
        headers=headers,
    )
    assert backwards.status_code == 400 and backwards.json()["code"] == "bad_period"

    unparsable = await client.get(
        DASHBOARD, params={"period": "custom", "from": "yesterday", "to": "today"}, headers=headers
    )
    assert unparsable.status_code == 400 and unparsable.json()["code"] == "bad_period"

    unknown_preset = await client.get(DASHBOARD, params={"period": "all_time"}, headers=headers)
    assert unknown_preset.status_code == 400 and unknown_preset.json()["code"] == "bad_period", (
        "an unknown preset must not fall back to the default: the user asked for all time "
        "and would be shown a week."
    )

    bad_facet = await client.get(
        DASHBOARD,
        params={"period": "custom", "from": "2026-03-10", "to": "2026-03-10", "result": "answerd"},
        headers=headers,
    )
    assert bad_facet.status_code == 400 and bad_facet.json()["code"] == "bad_result", (
        "a misspelt result facet must be refused, not applied - applied, it matches nothing "
        "and the page reads as an empty period."
    )
