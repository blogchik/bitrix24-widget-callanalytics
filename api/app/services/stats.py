"""Dashboard aggregation, in the viewer's timezone, as one `GROUPING SETS` query (§4.7, §10 step 5).

§10 step 5 fixes the shape of this module before it fixes anything else: "`/dashboard` as
one GROUPING SETS query". The left-menu page shows four things - a summary strip, calls
per day, an hour x weekday matrix and a per-employee comparison - and all four are the
same rows counted along different axes. Asking Postgres four times would read a 500k-row
portal four times; asking the browser would ship those rows into a Bitrix24 slider. One
statement with four grouping sets reads the range once and hands back roughly
`1 + days + 168 + employees` rows.

**Timezone.** §4.7 and §11 assumption 16: aggregation happens in the *viewer's*
`TIME_ZONE`, which arrives in the JWT (`tz`) and nowhere else. The conversion is done in
SQL (`call_start_date AT TIME ZONE :tz`, spelled `timezone(:tz, ...)`) so the day buckets,
the "today" preset and the hour x weekday matrix all agree with the clock the user reads
in Bitrix24 - and so a portal at UTC+5 does not find its evening calls filed under the
next day because the server runs in UTC. The period bounds are converted in Python from
the *same* zone name, because a request for "2026-03-11" means a local day: in Tashkent it
starts at 2026-03-10 19:00 UTC, and an implementation that groups in the viewer's zone but
bounds the period in the server's gets every "today" wrong by a few hours at the edges.

**Where the scope lives.** Every statement starts at `calls_repo.base_select`, which
carries the tenant predicate and `scope_filter` (§4.7) - so an `own` principal aggregates
only their own rows, and the employee facet in `/filters` collapses to that one person by
the same rule rather than by a second, hand-written copy of it. This module never writes a
`select(Call)` of its own; §4.7's "single place" only survives if the aggregation obeys it
too.

**Why the query has the shape it has.** The grouping expressions are plain columns of an
inner subquery rather than the `timezone(...)` / `extract(...)` expressions themselves.
Postgres matches a target-list expression against a grouping element structurally, and a
bound parameter compares equal only to *itself*: `timezone($1, ...)` in the select list
and `timezone($5, ...)` in `GROUP BY` are two different trees, and the statement would be
rejected with "column must appear in the GROUP BY clause". Projecting each bucket once and
grouping by the projection removes the question. The employee display name is joined onto
the aggregate (a handful of rows, by primary key) rather than into it, so the scan itself
stays on the covering `calls_portal_start_idx` of §3.

**What the columns mean** (§3, §5): `result_group` and `has_record` are GENERATED columns
- the single server-side definition of the call outcome and of "has a recording". This
module never re-derives either from `call_failed_code` or `record_file_id`; a second
definition is how a filter and a summary card start disagreeing. What it does do, once,
is COLLAPSE the outcome: §3 generates three values and the app speaks two (`answered` /
`no_answer`), because "missed" and "not connected" are one answer to the question a call
list is read to answer. `_ANSWERED_STORED` is the only place the stored vocabulary is
named, so the filter and the aggregation collapse it identically.
`rest_app_id` NULL means built-in telephony (§3), which is why the line facet carries an
explicit `builtin` entry instead of dropping NULLs.

`parse_filters` lives here, not in the route module, because `/calls` (§4.8) filters the
same rows with the same query string and must not grow a second parser: one place decides
what a period means, what the `MAX_PERIOD_DAYS` cap answers, and which predicates come out
the other end. It accepts the singular and plural spellings of every facet for the same
reason - the frontend and the two API modules are written by different agents against the
same §3 column list, and a naming disagreement should cost a code review, not a blank
chart on a customer's portal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.responses import JSONResponse
from sqlalchemy import Date, Integer, and_, bindparam, extract, func, or_, select, tuple_
from sqlalchemy.sql.elements import BindParameter, ColumnElement
from starlette.datastructures import QueryParams

from app.config import settings
from app.db.models import Call, Employee
from app.db.session import tenant_txn
from app.logging import get_logger
from app.security.principal import Principal
from app.services.calls_repo import base_select, scope_filter

__all__ = [
    "CallFilters",
    "FilterError",
    "load_dashboard",
    "load_filter_facets",
    "load_hours",
    "parse_filters",
    "resolve_timezone",
]

_log = get_logger(__name__)

#: The two outcomes the whole app speaks: the call was answered, or it was not.
#:
#: §3's generated `result_group` still stores three values and stays the raw storage - it
#: is derived from `call_failed_code` by the database and nothing here re-derives it. What
#: collapses is the VOCABULARY: `missed` (304) and `not_connected` (everything else) are
#: one answer to the only question a sales manager asks of a call list, and splitting them
#: put a third series on every chart that nobody read. The fine detail is not lost - the
#: raw `call_failed_code` is still on every row and the table shows it as the cell's title.
#:
#: This is deliberately the single place the collapse happens: `facet_predicates` filters
#: through it and `load_dashboard` counts through it, so the filter and the chart cannot
#: disagree about what "no answer" means.
_ANSWERED: Final[str] = "answered"
_NO_ANSWER: Final[str] = "no_answer"
_RESULT_GROUPS: Final[tuple[str, ...]] = (_ANSWERED, _NO_ANSWER)

#: The raw value §3 generates for an answered call (`CALL_FAILED_CODE = '200'`). Every
#: other generated value means "no answer", which is why the predicate below is `!=` and
#: not a second `IN` list that a new §3 value could silently fall out of.
_ANSWERED_STORED: Final[str] = "answered"

#: `CALL_TYPE` (research note (a)): 1 outgoing, 2 incoming, 3 incoming with redirection,
#: 4 callback, 5 informational. The portal cares about two of those: a redirected call is
#: still a call that came in, and a callback is the system dialling out. 5 belongs to
#: neither and is deliberately not forced into one - it matches no direction filter and
#: the table renders it with a dash, which is the honest answer for "this is not a
#: conversation with a customer".
_INCOMING: Final[str] = "incoming"
_OUTGOING: Final[str] = "outgoing"
_DIRECTIONS: Final[dict[str, tuple[int, ...]]] = {_INCOMING: (2, 3), _OUTGOING: (1, 4)}

#: Presets and the number of local days each covers, counting today (§10 step 5:
#: "today / 7 / 30 days / custom range"). The SPA resolves its own presets into `from`/`to`
#: in the viewer's zone, so these exist for callers that would rather not do date maths.
_PRESETS: Final[dict[str, int]] = {"today": 1, "7d": 7, "d7": 7, "30d": 30, "d30": 30}
_CUSTOM: Final[str] = "custom"
_DEFAULT_PRESET: Final[str] = "7d"

#: The chart may carry at most eight entity colours; a ninth employee folds into "Other"
#: rather than being given a generated hue (chart specification: categorical hues are
#: assigned in a fixed order and never cycled).
_SERIES_CAP: Final[int] = 8

#: ISO weekday numbering, as `EXTRACT(isodow …)` produces it: Monday = 1 … Sunday = 7.
_WEEKDAYS: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6, 7)
_HOURS: Final[tuple[int, ...]] = tuple(range(24))

#: Facet bounds. Neither is a business rule; both stop one pathological portal (a hundred
#: REST telephony apps, ten thousand cached users) from turning a filter list into a
#: megabyte of JSON inside a slider.
_LINE_CAP: Final[int] = 25
_EMPLOYEE_CAP: Final[int] = 2000

#: How the line facet spells "built-in telephony", i.e. `rest_app_id IS NULL` (§3). `0` is
#: accepted as the same thing because a numeric filter list cannot carry a NULL and no
#: Bitrix24 REST application has id 0.
_BUILTIN: Final[str] = "builtin"

#: Accepted spellings per facet, in the order they are read. One parser, several vocabularies.
_EMPLOYEE_KEYS: Final[tuple[str, ...]] = ("employee", "employee_id", "employee_ids")
_DIRECTION_KEYS: Final[tuple[str, ...]] = ("direction", "directions", "call_type")
_RESULT_KEYS: Final[tuple[str, ...]] = ("result", "results", "result_group")
_LINE_KEYS: Final[tuple[str, ...]] = ("line", "line_id", "line_ids", "rest_app_id")
_FROM_KEYS: Final[tuple[str, ...]] = ("from", "date_from", "start")
_TO_KEYS: Final[tuple[str, ...]] = ("to", "date_to", "end")


class FilterError(Exception):
    """A query string that cannot be honoured, as a machine code (§8).

    Answered rather than corrected: §10 step 5 requires that a custom range longer than
    `MAX_PERIOD_DAYS` is "a 400 with a machine code, not a silent truncation". A truncated
    period would draw a chart that looks like an answer to the question the user asked and
    is not one.

    Shaped like `PrincipalError` so a route can hand it straight to the client, but
    deliberately a different type: `PrincipalErrorRoute` renders authentication failures,
    and a malformed filter is not one.
    """

    def __init__(self, code: str, http_status: int = 400, **extra: Any) -> None:
        self.code = code
        self.http_status = http_status
        self.extra = extra
        super().__init__(code)

    def as_response(self) -> JSONResponse:
        """The wire form: a machine code plus whatever the SPA needs to explain it (§8)."""
        return JSONResponse({"code": self.code, **self.extra}, status_code=self.http_status)


def resolve_timezone(name: str | None) -> tuple[ZoneInfo, str]:
    """The viewer's zone, or UTC - never the server's local zone (§4.7, §11 assumption 16).

    The name reaches us from `user.current`'s `TIME_ZONE` through the JWT, so it is our own
    signed data and still not trusted to be a zone Postgres knows: an unknown name would
    make `timezone(:tz, …)` raise mid-query and turn a dashboard into a 500. Resolving it
    against Python's tz database first means the same string is either good for both
    engines (both use IANA names) or replaced by UTC in both - Python computes the period
    bounds, Postgres computes the buckets, and the two must not disagree about where a day
    starts.
    """
    candidate = (name or "").strip()
    if candidate:
        try:
            return ZoneInfo(candidate), candidate
        except (ZoneInfoNotFoundError, ValueError):
            _log.warning("stats: unknown viewer timezone, falling back to UTC")
    return ZoneInfo("UTC"), "UTC"


def _midnight_utc(day: dt.date, zone: ZoneInfo) -> dt.datetime:
    """Local midnight of `day`, as a UTC instant.

    On a day whose local midnight does not exist (a zone that shifts at 00:00) Python
    resolves the wall clock with the pre-transition offset rather than raising, which moves
    the boundary by that offset and never drops or duplicates a day.
    """
    return dt.datetime.combine(day, dt.time.min, tzinfo=zone).astimezone(dt.UTC)


@dataclass(frozen=True)
class CallFilters:
    """One parsed, validated query string (§10 step 5), ready to become SQL.

    Frozen for the reason `Principal` is: `predicates()` is the only description of what
    the user asked for, and a mutable one is one rewrite away from the response echo and
    the SQL disagreeing about which period was drawn.

    The dates are *local* to `tz_name` and inclusive at both ends, because that is how a
    person reads a date picker; `start_utc` / `end_utc` are the half-open UTC instants they
    correspond to, which is what a `timestamptz` index range wants.
    """

    preset: str
    date_from: dt.date
    date_to: dt.date
    start_utc: dt.datetime
    end_utc: dt.datetime
    previous_start_utc: dt.datetime
    tz_name: str
    employees: tuple[int, ...]
    directions: tuple[str, ...]
    results: tuple[str, ...]
    lines: tuple[int, ...]
    builtin_line: bool

    @property
    def days(self) -> int:
        """Inclusive length of the local period, in days."""
        return (self.date_to - self.date_from).days + 1

    def facet_predicates(self) -> list[ColumnElement[bool]]:
        """Everything except the period - employee, direction, result, line.

        Separated from the period because the dashboard scans one window further back than
        it draws (the previous-period comparison on the summary tiles) while every facet
        applies to both windows identically.

        The line filter is the one that needs care: `rest_app_id IS NULL` means built-in
        telephony (§3), so "built-in" cannot be expressed as an id and is OR-ed in
        separately.
        """
        terms: list[ColumnElement[bool]] = []
        if self.employees:
            terms.append(Call.portal_user_id.in_(self.employees))
        if self.directions:
            # Each name is a set of raw `CALL_TYPE` codes (§3 stores the code, never a
            # word), so two selected directions OR into one IN list rather than two terms.
            codes = sorted({code for name in self.directions for code in _DIRECTIONS[name]})
            terms.append(Call.call_type.in_(codes))
        if self.results and set(self.results) != set(_RESULT_GROUPS):
            # Both groups selected is no predicate at all, and saying so here keeps the
            # SQL free of a tautology. `no_answer` is the complement of `answered`, not a
            # list: a §3 value nobody has thought of yet is an outcome that was not an
            # answer, and must fall on that side rather than out of the result entirely.
            if _ANSWERED in self.results:
                terms.append(Call.result_group == _ANSWERED_STORED)
            else:
                terms.append(Call.result_group != _ANSWERED_STORED)
        if self.lines or self.builtin_line:
            line_terms: list[ColumnElement[bool]] = []
            if self.lines:
                line_terms.append(Call.rest_app_id.in_(self.lines))
            if self.builtin_line:
                line_terms.append(Call.rest_app_id.is_(None))
            terms.append(or_(*line_terms))
        return terms

    def predicates(self) -> list[ColumnElement[bool]]:
        """The `WHERE` terms to add to a `calls_repo` statement - and nothing else.

        The period is a half-open range on `call_start_date` (a `timestamptz`), not a
        comparison against a converted local timestamp: the §3 index
        `calls_portal_start_idx` leads with `(portal_id, call_start_date DESC)`, and a
        predicate wrapped in `AT TIME ZONE` could not use it. The conversion belongs in the
        bucket expressions, applied to the rows that survived the range.
        """
        return [
            Call.call_start_date >= self.start_utc,
            Call.call_start_date < self.end_utc,
            *self.facet_predicates(),
        ]

    def describe(self) -> dict[str, Any]:
        """The echo the SPA renders above the charts, so the page states its own scope.

        `range` is what the server actually aggregated - after preset resolution and after
        the cap - which is the only period the numbers describe.
        """
        return {
            "range": {
                "from": self.date_from.isoformat(),
                "to": self.date_to.isoformat(),
                "days": self.days,
                "timezone": self.tz_name,
                "preset": self.preset,
            },
            "filters": {
                "employees": list(self.employees),
                "directions": list(self.directions),
                "results": list(self.results),
                "lines": list(self.lines),
                "builtin_line": self.builtin_line,
            },
        }


def _values(params: QueryParams, names: tuple[str, ...]) -> list[str]:
    """Every non-empty occurrence of a facet, under any of its accepted spellings."""
    out: list[str] = []
    for name in names:
        out.extend(value.strip() for value in params.getlist(name) if value and value.strip())
    return out


def _first(params: QueryParams, names: tuple[str, ...]) -> str:
    for name in names:
        value = (params.get(name) or "").strip()
        if value:
            return value
    return ""


def _ints(params: QueryParams, names: tuple[str, ...], code: str) -> tuple[int, ...]:
    """A repeatable integer facet; one bad value refuses the request (§8 machine code).

    De-duplicated and ordered so the same question always produces the same SQL and the
    same response echo, whatever order the SPA appended the parameters in.
    """
    out: list[int] = []
    for raw in _values(params, names):
        try:
            out.append(int(raw))
        except ValueError:
            raise FilterError(code) from None
    return tuple(sorted(set(out)))


def _date(raw: str) -> dt.date:
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise FilterError("bad_period") from None


def parse_filters(params: QueryParams, principal: Principal) -> CallFilters:
    """Query string -> `CallFilters`, in the viewer's timezone (§10 step 5).

    Accepted parameters, all optional:

    * `from` / `to` (also `date_from` / `date_to`) - inclusive **local** dates,
      `YYYY-MM-DD`. Given either one, both are required and the period is that range;
    * `period` - `today` | `7d` | `30d` | `custom` when no dates are sent (default `7d`).
      The SPA resolves its own presets into dates in the viewer's zone, so this is the
      convenience path, not the main one;
    * `employee` / `employee_id` - `portal_user_id`, repeatable;
    * `direction` - `incoming` | `outgoing`, repeatable. Names rather than raw
      `CALL_TYPE` codes: which code counts as incoming is a reading of Bitrix24's
      semantics and belongs here, beside the research note, not in the query string;
    * `result` - `answered` | `no_answer`, repeatable;
    * `line` / `line_id` - `rest_app_id`, or `builtin` (or `0`) for built-in telephony.

    "Today" is the *viewer's* today, taken from `datetime.now(zone)`: a request at 23:30 in
    Tashkent must not be filed under the server's yesterday.

    The `MAX_PERIOD_DAYS` cap (366, §11 assumption 16) is answered as `period_too_long`
    with the limit attached, so the SPA can state the limit rather than guess it. `result`
    values are checked against §3's generated vocabulary because a typo would otherwise
    match nothing and read as "no calls".
    """
    zone, tz_name = resolve_timezone(principal.timezone)
    today = dt.datetime.now(zone).date()

    raw_from = _first(params, _FROM_KEYS)
    raw_to = _first(params, _TO_KEYS)
    preset = (params.get("period") or "").strip().lower()

    if raw_from or raw_to:
        # An explicit range wins over any preset: it is what the user picked in the date
        # controls, and half a range is a bug on the caller's side, not a default.
        if not raw_from or not raw_to:
            raise FilterError("bad_period")
        date_from = _date(raw_from)
        date_to = _date(raw_to)
        preset = preset or _CUSTOM
    else:
        preset = preset or _DEFAULT_PRESET
        if preset not in _PRESETS:
            # `period=custom` without dates included: there is nothing to aggregate over.
            raise FilterError("bad_period")
        date_to = today
        date_from = today - dt.timedelta(days=_PRESETS[preset] - 1)

    if date_from > date_to:
        raise FilterError("bad_period")
    span = (date_to - date_from).days + 1
    if span > settings.max_period_days:
        raise FilterError("period_too_long", max_days=settings.max_period_days)

    results = tuple(sorted(set(_values(params, _RESULT_KEYS))))
    for value in results:
        if value not in _RESULT_GROUPS:
            raise FilterError("bad_result")

    # Directions are names now, not raw `CALL_TYPE` codes. The codes stay in `_DIRECTIONS`
    # and never cross the wire: which code counts as incoming is a decision about Bitrix24
    # semantics, and it belongs on this side with the research note that supports it.
    directions = tuple(sorted(set(_values(params, _DIRECTION_KEYS))))
    for value in directions:
        if value not in _DIRECTIONS:
            raise FilterError("bad_direction")

    lines: list[int] = []
    builtin_line = False
    for raw in _values(params, _LINE_KEYS):
        if raw.lower() == _BUILTIN or raw == "0":
            builtin_line = True
            continue
        try:
            lines.append(int(raw))
        except ValueError:
            raise FilterError("bad_line") from None

    try:
        start_utc = _midnight_utc(date_from, zone)
        end_utc = _midnight_utc(date_to + dt.timedelta(days=1), zone)
        # The equally long window immediately before this one, for the summary tiles'
        # "vs previous period" line. Computed from local dates, so a period that contains a
        # DST change is still compared against the same number of *days*, not of hours.
        previous_start_utc = _midnight_utc(date_from - dt.timedelta(days=span), zone)
    except (OverflowError, ValueError):
        # The three bounds are DERIVED, and a well-formed date does not guarantee they
        # exist: `_date` accepts the whole proleptic calendar, so `9999-12-31` has no next
        # midnight, `0001-01-01` has no previous period, and local midnight of `0001-01-01`
        # in a UTC+n zone falls in year 0. The arithmetic raised `OverflowError` straight
        # out of this function and the routes catch only `FilterError` (dashboard.py,
        # calls.py), so a typed year in the date picker became a bare 500 - the raw error
        # §4.11's moderator table rejects an app for. `bad_period` is the code §8 and
        # `FilterError` already reserve for "a period this server cannot honour"; refusing
        # is the honest answer, because a range at the bottom of the calendar has no
        # previous period to compare against and the tiles would be about nothing.
        raise FilterError("bad_period") from None

    return CallFilters(
        preset=preset,
        date_from=date_from,
        date_to=date_to,
        start_utc=start_utc,
        end_utc=end_utc,
        previous_start_utc=previous_start_utc,
        tz_name=tz_name,
        employees=_ints(params, _EMPLOYEE_KEYS, "bad_employee"),
        directions=directions,
        results=results,
        lines=tuple(sorted(set(lines))),
        builtin_line=builtin_line,
    )


def _rate(part: int, whole: int) -> float | None:
    """A share of a total, or None when there is no total to divide by (§10 step 5).

    None, not 0.0: "no calls yet" and "none of the calls were answered" are different
    facts, and a 0 % tile over an empty period is the kind of number a moderator screens an
    app for.
    """
    if whole <= 0:
        return None
    return round(part / whole, 4)


def _totals(row: dict[str, Any] | None) -> dict[str, Any]:
    """The numbers every bucket answers with, in one shape for every axis.

    Talk time carries its own definition. §10 step 5: "Talk time means `call_duration` over
    **answered** calls; say so in the response so the SPA cannot mislabel it." `talk_basis`
    is that sentence in machine form and `talk_basis_calls` is the divisor, so an average
    over zero answered calls is `null` rather than a division by zero or a misleading 0 s.
    """
    total = int(row["total"]) if row else 0
    answered = int(row[_ANSWERED]) if row else 0
    talk_seconds = int(row["talk_seconds"]) if row else 0
    return {
        "total": total,
        "answered": answered,
        "no_answer": int(row[_NO_ANSWER]) if row else 0,
        "with_recording": int(row["with_record"]) if row else 0,
        "answered_rate": _rate(answered, total),
        # Three spellings of one number: `talk_seconds` is what the summary tiles read,
        # `talk_time_total` what the API contract calls it. They are the same sum.
        "talk_seconds": talk_seconds,
        "talk_time_total": talk_seconds,
        "talk_time_avg": round(talk_seconds / answered, 1) if answered else None,
        "talk_basis": _ANSWERED,
        "talk_basis_calls": answered,
    }


def _display_name(name: str | None, last_name: str | None) -> str | None:
    """"Name Last name" from the `employees` cache, or None.

    None is meaningful: §3 keeps `found=false` rows for users `user.get` no longer returns,
    and §7 has the UI render "User #id" for them. Inventing a placeholder string here would
    put an untranslated sentence into a translated page (§8).
    """
    parts = [part.strip() for part in (name, last_name) if part and part.strip()]
    return " ".join(parts) or None


def _sum(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row[key]) for row in rows)


async def load_dashboard(principal: Principal, filters: CallFilters) -> dict[str, Any]:
    """The whole left-menu page, in one round trip and one aggregate (§10 step 5).

    Four grouping sets over the same scoped rows, each carrying the `current` flag so the
    summary tiles can also show the equally long window before this one:

    * `(current)` - the summary strip, this period and the previous one;
    * `(current, day)` - calls per day, zero-filled here so the time series has a
      continuous axis and the SPA never has to do date arithmetic in a timezone it does not
      own;
    * `(current, weekday, hour)` - the 7 x 24 matrix, also zero-filled: a heatmap with
      holes is a heatmap that has to be read twice;
    * `(current, portal_user_id)` - the employee comparison.

    `GROUPING()` tells the sets apart on the way back: a bit is 1 when the column was *not*
    part of that set, so `g_day = 0` marks a day row, `g_hour = 0` a matrix cell,
    `g_user = 0` an employee, and all-ones the summary. That last one is why an empty
    portal still answers: the summary set produces its row whenever the window has any row
    at all, and when it has none the caller still gets a zeroed strip and the page renders
    "no calls in this period" (§4.11) instead of an error.

    The previous window doubles the scanned range, which is the price of the "vs previous
    period" line on the tiles; it is still one index range and one statement. Its per-day,
    per-cell and per-employee rows are discarded - only its totals are used.

    Employee names are LEFT-joined onto the aggregate by primary key, never into the scan.
    A LEFT join, because §7 requires a `portal_user_id` with no cached employee row to
    still appear (the UI renders "User #id"): an inner join would silently drop those calls
    out of a breakdown that has to re-add to the summary. `active` travels with the name
    because §3 keeps dismissed users and §7 greys rather than hides them.

    Series capping happens here, not in the browser: the chart specification allows eight
    entity colours and the ninth employee folds into an "other" bucket whose numbers are the
    real remainder. `per_employee_all` still carries everyone, because the call table
    doubles as the accessible view of the same data.
    """
    tz_param: BindParameter[str] = bindparam("viewer_tz", filters.tz_name)
    local_ts = func.timezone(tz_param, Call.call_start_date)

    # One projection of the buckets; everything below groups by these columns rather than by
    # the expressions, so no bound parameter has to compare equal to another one (see the
    # module docstring).
    scoped = (
        base_select(principal)
        .with_only_columns(
            (Call.call_start_date >= filters.start_utc).label("current"),
            local_ts.cast(Date).label("day"),
            extract("isodow", local_ts).cast(Integer).label("weekday"),
            extract("hour", local_ts).cast(Integer).label("hour"),
            Call.portal_user_id.label("bx_user_id"),
            Call.result_group.label("result_group"),
            Call.call_duration.label("call_duration"),
            Call.has_record.label("has_record"),
        )
        .where(
            Call.call_start_date >= filters.previous_start_utc,
            Call.call_start_date < filters.end_utc,
            *filters.facet_predicates(),
        )
        .subquery("scoped")
    )

    answered = scoped.c.result_group == _ANSWERED_STORED
    aggregate = (
        select(
            func.grouping(scoped.c.day).label("g_day"),
            func.grouping(scoped.c.hour).label("g_hour"),
            func.grouping(scoped.c.bx_user_id).label("g_user"),
            scoped.c.current,
            scoped.c.day,
            scoped.c.weekday,
            scoped.c.hour,
            scoped.c.bx_user_id,
            func.count().label("total"),
            func.count().filter(answered).label(_ANSWERED),
            # The complement, counted rather than summed from two labels: a §3 value this
            # code has never seen still lands here instead of vanishing from both series
            # and leaving a stack that does not add up to `total`.
            func.count().filter(~answered).label(_NO_ANSWER),
            func.count().filter(scoped.c.has_record).label("with_record"),
            # §10 step 5: talk time is `call_duration` over ANSWERED calls only. The FILTER
            # is that definition; `_totals()` carries it into the response.
            func.coalesce(func.sum(scoped.c.call_duration).filter(answered), 0).label(
                "talk_seconds"
            ),
        )
        .group_by(
            func.grouping_sets(
                tuple_(scoped.c.current),
                tuple_(scoped.c.current, scoped.c.day),
                tuple_(scoped.c.current, scoped.c.weekday, scoped.c.hour),
                tuple_(scoped.c.current, scoped.c.bx_user_id),
            )
        )
        .cte("agg")
    )

    stmt = select(
        aggregate,
        Employee.name.label("emp_name"),
        Employee.last_name.label("emp_last_name"),
        Employee.phone_inner.label("emp_phone_inner"),
        Employee.active.label("emp_active"),
    ).select_from(
        aggregate.outerjoin(
            Employee,
            and_(
                Employee.portal_id == principal.portal_id,
                Employee.bx_user_id == aggregate.c.bx_user_id,
            ),
        )
    )

    async with tenant_txn(principal.portal_id) as session:
        rows = [dict(row) for row in (await session.execute(stmt)).mappings().all()]

    summary_row: dict[str, Any] | None = None
    previous_row: dict[str, Any] | None = None
    by_day: dict[dt.date, dict[str, Any]] = {}
    by_cell: dict[tuple[int, int], dict[str, Any]] = {}
    employee_rows: list[dict[str, Any]] = []

    for row in rows:
        current = bool(row["current"])
        if not int(row["g_day"]):
            if current:
                by_day[row["day"]] = row
        elif not int(row["g_hour"]):
            if current:
                by_cell[(int(row["weekday"]), int(row["hour"]))] = row
        elif not int(row["g_user"]):
            if current:
                employee_rows.append(row)
        elif current:
            summary_row = row
        else:
            previous_row = row

    per_day: list[dict[str, Any]] = []
    cursor = filters.date_from
    while cursor <= filters.date_to:
        per_day.append({"date": cursor.isoformat(), **_totals(by_day.get(cursor))})
        cursor += dt.timedelta(days=1)

    cells: list[dict[str, Any]] = []
    matrix_max = 0
    for weekday in _WEEKDAYS:
        for hour in _HOURS:
            cell = by_cell.get((weekday, hour))
            totals = _totals(cell)
            matrix_max = max(matrix_max, int(totals["total"]))
            # `count` is what the heatmap component reads; `total` keeps every bucket in
            # this response readable by the same accessor.
            cells.append({"weekday": weekday, "hour": hour, "count": totals["total"], **totals})

    employees: list[dict[str, Any]] = [
        {
            # `employee_id` is the SPA's name for it, `bx_user_id` the §3 column's. Both,
            # because this row is read by a chart, a table and a test.
            "employee_id": row["bx_user_id"],
            "bx_user_id": row["bx_user_id"],
            "name": _display_name(row["emp_name"], row["emp_last_name"]),
            # §7: shown beside the name on the comparison chart, exactly as in the filter.
            "phone_inner": row["emp_phone_inner"],
            "active": True if row["emp_active"] is None else bool(row["emp_active"]),
            "other": False,
            # A statistic row with no `PORTAL_USER_ID` (§3 allows NULL) is nobody's call.
            # It is neither an employee nor silently dropped: the axes have to add up.
            "unassigned": row["bx_user_id"] is None,
            **_totals(row),
        }
        for row in employee_rows
    ]
    # Ranked by volume, then by a stable key: a redraw must not reshuffle equal rows. The
    # colour rule says a hue follows the entity, so the SPA keys colours off `employee_id`
    # and never off this order.
    employees.sort(
        key=lambda item: (
            -int(item["total"]),
            item["bx_user_id"] is None,
            item["name"] or "",
            int(item["bx_user_id"] or 0),
        )
    )

    series: list[dict[str, Any]] = employees[:_SERIES_CAP]
    remainder = employees[_SERIES_CAP:]
    if remainder:
        other_total = _sum(remainder, "total")
        other_answered = _sum(remainder, _ANSWERED)
        other_talk = _sum(remainder, "talk_seconds")
        series = [
            *series,
            {
                "employee_id": None,
                "bx_user_id": None,
                "name": None,
                # "Other" is an aggregate of several people; no one extension describes it.
                "phone_inner": None,
                "active": True,
                "other": True,
                "unassigned": False,
                "members": len(remainder),
                "total": other_total,
                "answered": other_answered,
                "no_answer": _sum(remainder, _NO_ANSWER),
                "with_recording": _sum(remainder, "with_recording"),
                "answered_rate": _rate(other_answered, other_total),
                "talk_seconds": other_talk,
                "talk_time_total": other_talk,
                "talk_time_avg": round(other_talk / other_answered, 1) if other_answered else None,
                "talk_basis": _ANSWERED,
                "talk_basis_calls": other_answered,
            },
        ]

    summary = _totals(summary_row)
    # `null`, not a row of zeros, when the window before this one holds nothing at all: the
    # tiles say "no earlier data" instead of reporting a -100 % that never happened.
    summary["previous"] = _totals(previous_row) if previous_row is not None else None

    return {
        **filters.describe(),
        "access": principal.access,
        "summary": summary,
        "per_day": per_day,
        "hour_weekday": cells,
        "hour_weekday_max": matrix_max,
        "per_employee": series,
        "per_employee_all": employees,
        "series_cap": _SERIES_CAP,
    }


#: Rows a single `/hours` answer may carry, where a row is one (employee, local day).
#:
#: The grid is 24 cells wide, so this is 12 000 cells - about what a slider can render
#: before scrolling it becomes the slower half of the page. It is a cap on the ANSWER and
#: not on the question: the response says how many rows there really were, and the SPA
#: tells the reader to narrow the period or the selection. A cap that quietly returns the
#: first N reads as "this is all of it", which is the one thing it must not do.
_HOUR_ROW_CAP: Final[int] = 500

#: The hours of a day, as the grid draws them. Materialised rather than derived per row:
#: every row carries all 24 whether or not a call happened in each, because a grid with
#: holes in it is a grid that has to be read twice (the same argument §4.11 makes for
#: zero-filling the per-day series).
_DAY_HOURS: Final[tuple[int, ...]] = tuple(range(24))


async def load_hours(principal: Principal, filters: CallFilters) -> dict[str, Any]:
    """Talk time per employee, per local day, per hour (the "by hour" page).

    One `GROUP BY` over the period, never a fourth grouping set on `/dashboard`: that
    query is issued on every open of the left-menu page, and a `(portal_user_id, hour)`
    set would make it aggregate up to `_EMPLOYEE_CAP` x 24 rows that nothing on the
    dashboard draws.

    **The two numbers in a cell are deliberately about different sets of calls.** Talk
    time is `call_duration` over ANSWERED calls - the same definition the summary tile
    carries, and the only one that means "time spent talking" - while the count is every
    call in that hour. A cell reading "20 (43)" therefore says: forty-three attempts, and
    twenty minutes of conversation to show for them. Narrowing the count to answered calls
    would make the two numbers describe one set and lose exactly the comparison the page
    exists for.

    Hours and days are the **viewer's**, via `timezone(:tz, ...)` (§4.6 `tz`), so a call at
    23:30 in Tashkent is not filed under the server's yesterday at 18:30. The buckets are
    projected in an inner subquery and grouped by the projection, for the reason the module
    docstring gives: a bound parameter compares equal only to itself, so grouping by the
    expression would be rejected.

    A `portal_user_id` of NULL is nobody's call (§3 allows it). It gets its own row, marked
    `unassigned`, rather than being filtered out - dropping it would make the page quietly
    disagree with every other count in the app about how many calls the portal made.
    """
    tz_param: BindParameter[str] = bindparam("viewer_tz", filters.tz_name)
    local_ts = func.timezone(tz_param, Call.call_start_date)

    scoped = (
        base_select(principal)
        .with_only_columns(
            local_ts.cast(Date).label("day"),
            extract("hour", local_ts).cast(Integer).label("hour"),
            Call.portal_user_id.label("bx_user_id"),
            Call.result_group.label("result_group"),
            Call.call_duration.label("call_duration"),
        )
        # `predicates()` and not `facet_predicates()`: this page has no previous window to
        # compare against, so it reads exactly the period it was asked for.
        .where(*filters.predicates())
        .subquery("scoped")
    )

    answered = scoped.c.result_group == _ANSWERED_STORED
    aggregate = (
        select(
            scoped.c.day,
            scoped.c.hour,
            scoped.c.bx_user_id,
            func.count().label("calls"),
            func.coalesce(
                func.sum(scoped.c.call_duration).filter(answered), 0
            ).label("talk_seconds"),
        )
        .group_by(scoped.c.day, scoped.c.hour, scoped.c.bx_user_id)
        .cte("agg")
    )

    stmt = select(
        aggregate,
        Employee.name.label("emp_name"),
        Employee.last_name.label("emp_last_name"),
        Employee.phone_inner.label("emp_phone_inner"),
        Employee.active.label("emp_active"),
    ).select_from(
        # LEFT, exactly as the dashboard breakdown joins it: a `portal_user_id` with no
        # cached employee row must still appear as "User #id" (§7) instead of vanishing.
        aggregate.outerjoin(
            Employee,
            and_(
                Employee.portal_id == principal.portal_id,
                Employee.bx_user_id == aggregate.c.bx_user_id,
            ),
        )
    )

    async with tenant_txn(principal.portal_id) as session:
        cells = [dict(row) for row in (await session.execute(stmt)).mappings().all()]

    # (bx_user_id, day) -> the row being built. Assembled in Python rather than with a
    # second, wider query: the grid is dense by construction and 24 zeroes per row are
    # cheaper to write here than to make Postgres generate.
    rows: dict[tuple[int | None, dt.date], dict[str, Any]] = {}
    for cell in cells:
        key = (cell["bx_user_id"], cell["day"])
        row = rows.get(key)
        if row is None:
            user_id = cell["bx_user_id"]
            row = {
                "employee_id": user_id,
                "bx_user_id": user_id,
                "name": _display_name(cell["emp_name"], cell["emp_last_name"]),
                "phone_inner": cell["emp_phone_inner"],
                "active": True if cell["emp_active"] is None else bool(cell["emp_active"]),
                "unassigned": user_id is None,
                "date": cell["day"].isoformat(),
                # [talk_seconds, calls] per hour, positionally. A list of objects would
                # trip this payload's size for no gain: the index IS the hour.
                "hours": [[0, 0] for _ in _DAY_HOURS],
                "talk_seconds": 0,
                "calls": 0,
            }
            rows[key] = row
        hour = int(cell["hour"])
        talk = int(cell["talk_seconds"])
        calls = int(cell["calls"])
        row["hours"][hour] = [talk, calls]
        row["talk_seconds"] += talk
        row["calls"] += calls

    # Two stable passes rather than one key: a date has no negation, and reversing the
    # whole sort would also reverse the names inside each day.
    #
    # Newest day first - the question is nearly always about this week - and inside a day,
    # alphabetically. The employee chart ranks by volume because it is read as a ranking;
    # a grid is read by looking somebody up, and for that a stable alphabet beats an order
    # that moves every time the numbers do.
    ordered = sorted(
        rows.values(),
        key=lambda item: (
            item["name"] or "",
            item["bx_user_id"] is None,
            int(item["bx_user_id"] or 0),
        ),
    )
    ordered.sort(key=lambda item: str(item["date"]), reverse=True)

    total_rows = len(ordered)
    shown = ordered[:_HOUR_ROW_CAP]

    return {
        "rows": shown,
        # The ramp is normalised over the WHOLE table, so it is computed here and not in
        # the browser: a client that re-derived it from the rows it received would paint a
        # truncated answer on a different scale than the full one.
        "max_cell_seconds": max(
            (int(cell[0]) for row in shown for cell in row["hours"]), default=0
        ),
        "total_rows": total_rows,
        "row_cap": _HOUR_ROW_CAP,
        "truncated": total_rows > _HOUR_ROW_CAP,
        "scope": principal.access,
        **filters.describe(),
    }


async def load_filter_facets(principal: Principal) -> dict[str, Any]:
    """The two lists the filter bar needs: employees and lines / sources (§10 step 5).

    **Employees** come from the `employees` cache, never from `SELECT DISTINCT
    portal_user_id FROM calls` - §3 says why the placeholder rows exist at all: "so the
    refresh job never has to `SELECT DISTINCT` over `calls`". Dismissed users are included
    with `active=false` (§3 keeps them because their calls remain, §7 greys them). The scope
    collapse is derived from `calls_repo.scope_filter` rather than re-implemented: if it
    returns a predicate at all, this principal sees only their own rows, so the facet is
    only themselves and the SPA hides the control (§4.7, open question 15).

    **Lines** are the distinct `rest_app_id` of §3 - the integration that produced the call,
    NULL meaning built-in telephony - counted through `base_select`, so the tenant predicate
    and the scope are on the statement and the count walks this portal's own range of
    `calls_portal_rest_app_idx` rather than the table.

    The name is fetched separately, one bounded `LIMIT 1` per non-NULL id, and never for the
    built-in entry. Aggregating `max(rest_app_name)` in the same pass would look tidier and
    would cost a heap visit for every row in the portal, because the name is not in that
    index; and the built-in bucket has no name to find at all, so a lookup there would scan
    its whole range to prove a NULL. There are single digits of these ids in practice, hence
    `_LINE_CAP`.

    Both statements run in one `tenant_txn` (§3): `employees` carries FORCED RLS exactly as
    `calls` does, and a read outside the tenant context returns zero rows silently.
    """
    scope = scope_filter(principal)  # raises for `denied`, exactly as every other read does

    employees_stmt = select(
        Employee.bx_user_id,
        Employee.name,
        Employee.last_name,
        Employee.work_position,
        Employee.phone_inner,
        Employee.active,
        Employee.found,
    ).where(Employee.portal_id == principal.portal_id)
    if scope is not None:
        employees_stmt = employees_stmt.where(Employee.bx_user_id == principal.user_id)
    employees_stmt = employees_stmt.order_by(
        Employee.active.desc(),
        func.coalesce(Employee.last_name, ""),
        func.coalesce(Employee.name, ""),
        Employee.bx_user_id,
    ).limit(_EMPLOYEE_CAP)

    lines_stmt = (
        base_select(principal)
        .with_only_columns(Call.rest_app_id.label("rest_app_id"), func.count().label("total"))
        .group_by(Call.rest_app_id)
        .order_by(func.count().desc(), Call.rest_app_id)
        .limit(_LINE_CAP)
    )

    lines: list[dict[str, Any]] = []
    async with tenant_txn(principal.portal_id) as session:
        employee_rows = [dict(row) for row in (await session.execute(employees_stmt)).mappings()]
        line_rows = [dict(row) for row in (await session.execute(lines_stmt)).mappings()]

        for row in line_rows:
            rest_app_id = row["rest_app_id"]
            name: str | None = None
            if rest_app_id is not None:
                name = (
                    await session.execute(
                        base_select(principal)
                        .with_only_columns(Call.rest_app_name)
                        .where(
                            Call.rest_app_id == rest_app_id,
                            Call.rest_app_name.is_not(None),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            lines.append(
                {
                    "id": rest_app_id,
                    "rest_app_id": rest_app_id,
                    # §3: NULL is built-in telephony. The SPA translates that label; the
                    # server never sends the sentence (§8).
                    "builtin": rest_app_id is None,
                    "name": name,
                    "calls": int(row["total"]),
                }
            )

    return {
        "access": principal.access,
        # §4.7: an `own` viewer has nobody else to filter by, so the control is hidden
        # rather than shown with a single, pointless option.
        "employee_filter_enabled": scope is None,
        "employees": [
            {
                "id": int(row["bx_user_id"]),
                "bx_user_id": int(row["bx_user_id"]),
                "name": _display_name(row["name"], row["last_name"]),
                "work_position": row["work_position"],
                # §7: the internal extension, which the filter shows in parentheses after
                # the name. NULL for a portal that sets none, and for any row not yet
                # refreshed - both render as no parentheses rather than as empty ones.
                "phone_inner": row["phone_inner"],
                "active": bool(row["active"]),
                # `found=false` means `user.get` no longer returns this id (§3); the SPA
                # renders "User #id" for it, which is why the name may be null.
                "found": bool(row["found"]),
            }
            for row in employee_rows
        ],
        "lines": lines,
        "results": list(_RESULT_GROUPS),
    }
