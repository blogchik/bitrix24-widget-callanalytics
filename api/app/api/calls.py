"""The call table and its two row actions (§4.7, §4.8, §4.6, §5.7).

Three endpoints, one read path:

* `GET /calls` - the table. 50 rows a page, newest first, **the same filter set the
  dashboard aggregates over**: the query string is parsed by
  `services/stats.py::parse_filters`, the very function `GET /dashboard` uses, so the
  numbers on the charts and the rows under them can never describe different periods.
  Paging is the one thing this endpoint adds, because the charts have none.
* `POST /calls/{id}/play-url` - mints the narrow 5-minute playback grant of §4.6. An
  `<audio src>` cannot send an `Authorization` header, so the grant travels as `?t=` and
  is bound to one portal, one viewer, one call and five minutes.
* `POST /calls/{id}/refresh` - §5.7 rule 3: the SPA calls it when playback answered
  403/404, and the next sync visit re-reads those rows by id.

Two rules shape every line below.

**Every read of `calls` starts at `services/calls_repo.base_select`** (§4.7). This module
narrows that statement - the CRM clause, the filter predicates, the ordering, the page -
and never writes a `select(Call)` of its own, so `scope_filter` is applied in exactly one
place and an `own` viewer cannot be widened by a filter this file forgot to think about.
The employee names come from a *second*, tiny statement against `employees` (§7 names the
calls table as a legitimate reader of that cache) rather than from a join bolted onto
`base_select`: rebuilding that statement's FROM clause inside a route module is the kind
of surgery that quietly loses a predicate.

**`call_record_url` never leaves the process** (§3, decision 21). The row projection
below is a list of named columns and the URL is not among them; what the browser gets is
`has_record` and, if it asks for it, a signed path to `record.py`.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import deque
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Select, String, func, select, update
from sqlalchemy.sql.elements import ColumnElement
from starlette.datastructures import QueryParams

from app.api.record import remember_viewer_token
from app.bitrix.errors import BitrixError
from app.bitrix.identity import resolve_identity
from app.config import settings
from app.db.models import Call, Employee, Portal
from app.db.session import control_txn, tenant_txn
from app.logging import get_logger, get_request_id
from app.security.principal import (
    Principal,
    PrincipalError,
    PrincipalErrorRoute,
    get_principal,
    require_data_access,
)
from app.security.session_token import issue_play_token
from app.services.calls_repo import base_select, crm_match_clause, scope_filter
from app.services.crm_context import load_crm_context
from app.services.stats import CallFilters, FilterError, parse_filters

__all__ = ["crm_clause_for", "router"]

router = APIRouter(route_class=PrincipalErrorRoute)

_log = get_logger(__name__)

#: §2: "GET /calls (50/page)". Fixed, not a client parameter: a page size the caller
#: chooses is a way to ask for the whole table in one request.
_PAGE_SIZE: Final[int] = 50

#: OFFSET paging is honest up to a point; past it the SPA must narrow the period instead
#: of walking 500k rows. 200 pages = 10,000 rows, well beyond any real table use.
_MAX_PAGE: Final[int] = 200

#: §4.6: the playback grant is five minutes and one call.
_PLAY_TTL_SECONDS: Final[int] = 300

#: §4.7's `all`, i.e. the administrators. Playback uses the portal token only for them.
_ACCESS_ALL: Final[str] = "all"

#: §3 `portals_status_chk`: the only status that may answer a request (§4.6 revocation).
_ACTIVE: Final[str] = "active"

#: §5.7 rule 3: "rate-limited per portal so a broken page cannot hammer the sync". The
#: window is process-local, which is right for the same reason `bitrix/oauth.py` says it
#: is: v1 runs one api container, and this guards a courtesy flag, not a security
#: boundary - the flag itself is idempotent and costs one row update.
_REFRESH_LIMIT: Final[int] = 30
_REFRESH_WINDOW_S: Final[float] = 600.0
_REFRESH_RETRY_AFTER: Final[int] = 60

#: A body that carries a live Bitrix24 access token (§4.6 keeps such bodies out of every
#: log). One opaque string; the length is checked before parsing so an oversized body is
#: refused undecoded.
_MAX_BODY_BYTES: Final[int] = 8 * 1024
_TOKEN_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_TOKEN_MIN: Final[int] = 16
_TOKEN_MAX: Final[int] = 512

#: The two facets that belong to the table and not to the charts, with the spellings the
#: SPA may use. They live here rather than in `services/stats.py` because neither means
#: anything to an aggregate: "has a recording" is a per-row property the player acts on,
#: and a phone search narrows a list rather than a series. Everything the charts and the
#: table do share is parsed once, by `parse_filters` (§4.11).
_RECORD_KEYS: Final[tuple[str, ...]] = ("has_record", "with_record", "record")
_SEARCH_KEYS: Final[tuple[str, ...]] = ("search", "q", "phone")

#: A LIKE pattern the caller controls; bounded so it cannot become a pathological scan.
_SEARCH_MAX: Final[int] = 64

#: `LIKE` wildcards in user input are escaped rather than refused: a `%` inside a phone
#: search is a typo, not a query language, and `_` is a wildcard nobody expects.
_LIKE_ESCAPE: Final[str] = "\\"

#: The character class `_digits` and Postgres must agree on. Two normalisations built from
#: different alphabets is how a needle stops matching the haystack it came from.
_NON_DIGITS: Final[str] = "[^0-9]"


# --- paging, the table facets and the CRM clause -------------------------------------


def _page(params: QueryParams) -> int:
    """`page`, 1-based, capped (§8 machine code, never a silent clamp).

    The only query parameter this endpoint understands that the dashboard does not: the
    charts summarise a period, the table walks it. Everything else about the query string
    is `parse_filters`', so there is one definition of what a filter means.
    """
    raw = (params.get("page") or "").strip()
    if not raw:
        return 1
    try:
        page = int(raw)
    except ValueError:
        raise FilterError("bad_page") from None
    if page < 1 or page > _MAX_PAGE:
        raise FilterError("bad_page", max_page=_MAX_PAGE)
    return page


def _first(params: QueryParams, names: tuple[str, ...]) -> str:
    """The first non-empty value under any accepted spelling of one facet."""
    for name in names:
        value = (params.get(name) or "").strip()
        if value:
            return value
    return ""


def _digits(value: str) -> str:
    """ASCII digits only - exactly the set `regexp_replace(..., '[^0-9]', ...)` keeps.

    `str.isdigit()` alone is also true for Arabic-Indic and superscript digits, which
    Postgres would strip from the column; a needle built from them would therefore match
    nothing while looking like a number to the person who typed it. The `isascii()` guard
    is the same one `bitrix/users.py` applies to an incoming id, for the same reason.
    """
    return "".join(ch for ch in value if ch.isascii() and ch.isdigit())


def _table_facets(params: QueryParams) -> tuple[list[ColumnElement[bool]], dict[str, Any]]:
    """The two table-only predicates, plus an echo of what they were (§8 machine codes).

    `has_record` selects on §3's **generated** `has_record` column - the single
    server-side definition of "has a recording", shared by the table, the player and the
    recheck job (§5.7). Re-deriving it here as `record_file_id IS NOT NULL OR ...` would
    be exactly the second definition the generated column exists to prevent.

    The search is a substring of `phone_number`, matched on **digits alone** - both sides
    normalised, which is the only version of this filter that answers what people actually
    type. §3 stores the number exactly as Bitrix24 delivered it (usually `+998901234567`)
    while the table renders it regrouped with spaces (`format.ts::formatPhone`), so a
    reader copying a number out of the row they are looking at and pasting it back into
    the search would never match it under a raw comparison. Substring rather than prefix
    for the same kind of reason: the digits people remember are the local part.

    Unindexed on purpose - it runs inside a period already narrowed by
    `calls_portal_start_idx`. A generated `phone_digits` column with a `pg_trgm` index was
    weighed and declined: it is a migration, a Postgres extension and a backfill for a
    filter whose range is capped at `MAX_PERIOD_DAYS` anyway. If a large portal ever makes
    this slow, the cheaper lever is to skip the COUNT while a search is active - the SPA
    pages by appending and never reads `total`.
    """
    terms: list[ColumnElement[bool]] = []
    echo: dict[str, Any] = {"has_record": None, "search": None}

    raw_record = _first(params, _RECORD_KEYS).lower()
    if raw_record:
        if raw_record in ("1", "true", "yes"):
            has_record = True
        elif raw_record in ("0", "false", "no"):
            has_record = False
        else:
            raise FilterError("bad_record")
        terms.append(Call.has_record.is_(has_record))
        echo["has_record"] = has_record

    search = _first(params, _SEARCH_KEYS)
    if search:
        if len(search) > _SEARCH_MAX:
            raise FilterError("bad_search", max_length=_SEARCH_MAX)
        digits = _digits(search)
        if digits:
            # `LIKE`, not `ILIKE`: digits have no case, and folding both sides would be
            # work that can never change an answer. No `escape=` either - `digits` is
            # `[0-9]*` by construction, so `%`, `_` and the escape character itself cannot
            # reach the pattern, and writing one would imply a threat removed a line above.
            # `type_` keeps this a `ColumnElement[str]` under mypy strict rather than Any.
            normalised = func.regexp_replace(
                Call.phone_number, _NON_DIGITS, "", "g", type_=String()
            )
            terms.append(normalised.like(f"%{digits}%"))
        else:
            # A SIP address or an alias ("sip:reception@office.local"), which §3 stores raw
            # and `formatPhone` shows verbatim. This branch is not a courtesy: normalising
            # such a needle yields "", and `LIKE '%%'` would match every row that has a
            # number while dropping every row that has none - neither "no filter" nor "no
            # rows", which is the one outcome worse than either.
            pattern = (
                search.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
                .replace("%", _LIKE_ESCAPE + "%")
                .replace("_", _LIKE_ESCAPE + "_")
            )
            terms.append(Call.phone_number.ilike(f"%{pattern}%", escape=_LIKE_ESCAPE))
        # The raw input, not the digits: the SPA re-renders what was typed.
        echo["search"] = search

    return terms, echo


async def crm_clause_for(principal: Principal) -> ColumnElement[bool] | None:
    """§4.8's entity predicate for a CRM tab, or None for the dashboard.

    The JWT's `ent` claim decides which entity; the cached `crm_contexts` row supplies
    what that entity matches - the resolved entity keys, the activity ids, **and** the raw
    `(ent.t, ent.id)` pair, because §4.8 records that some portals do emit `DEAL` in the
    statistics rows even though the documented entity types exclude it. Without that last
    clause such a portal shows an empty deal tab while the rows sit in the table.

    `load_crm_context` returns None when the row is absent or was resolved by somebody
    else before this session began, and §4.8 is explicit about the answer: **409
    `context_missing`, never 403 and never an empty page**. That code is an instruction,
    not an error - the SPA runs the session exchange, which re-resolves the context with
    the current user's own Bitrix24 token. A user who genuinely may not read the card then
    gets `crm_no_access` decided by Bitrix24 rather than by our cache.

    Exported for the dashboard router: a CRM tab that aggregated over a different match
    set than its own table would be a discrepancy nobody could explain.
    """
    entity = principal.entity
    if entity is None:
        return None
    entity_type = str(entity["t"])
    entity_id = int(entity["id"])
    try:
        context = await load_crm_context(
            principal.portal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            not_older_than=principal.issued_at,
            user_id=principal.user_id,
        )
    except ValueError:
        # `crm_context` refuses an entity type outside the §3 CHECK. That claim is ours,
        # so this is a hand-built token rather than a portal that surprised us.
        _log.warning("calls: unusable ent claim", extra={"portal_id": principal.portal_id})
        raise PrincipalError("invalid_session", 401) from None
    if context is None:
        raise PrincipalError("context_missing", 409)
    return crm_match_clause(context, entity_type=entity_type, entity_id=entity_id)


# --- GET /calls ----------------------------------------------------------------------

#: The row projection: what the table renders, and nothing else. Named columns rather
#: than the ORM entity because §3 decision 21 keeps `call_record_url` server-side, and a
#: serialised row object is exactly how it would escape one refactor from now.
_ROW_COLUMNS = (
    Call.id,
    Call.call_start_date,
    Call.portal_user_id,
    Call.call_type,
    Call.phone_number,
    Call.portal_number,
    Call.crm_entity_type,
    Call.crm_entity_id,
    Call.crm_activity_id,
    Call.call_duration,
    Call.result_group,
    Call.call_failed_code,
    Call.rest_app_id,
    Call.rest_app_name,
    Call.has_record,
    Call.record_duration,
)


def _display_name(row: Any) -> str | None:
    """"Last First Second" from the cache, or None so the SPA renders "User #id" (§7)."""
    parts = [row.last_name, row.name, row.second_name]
    joined = " ".join(part.strip() for part in parts if part and part.strip())
    return joined or None


async def _employee_names(
    session: Any, portal_id: int, user_ids: set[int]
) -> dict[int, dict[str, Any]]:
    """The `employees` half of a page (§7: the calls table is a documented reader).

    Fifty ids against a primary-key prefix, inside the same transaction as the page it
    describes. A miss is a real answer, not a gap: `found=false` is §7's "User #id" state
    and `active=false` is its dismissed employee, who keeps their historical calls.
    """
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Employee.bx_user_id,
                Employee.name,
                Employee.last_name,
                Employee.second_name,
                Employee.active,
                Employee.found,
            ).where(Employee.portal_id == portal_id, Employee.bx_user_id.in_(user_ids))
        )
    ).all()
    return {
        int(row.bx_user_id): {
            "id": int(row.bx_user_id),
            "name": _display_name(row),
            "active": bool(row.active),
            "found": bool(row.found),
        }
        for row in rows
    }


def _filtered(
    principal: Principal,
    filters: CallFilters,
    crm: ColumnElement[bool] | None,
    extra: list[ColumnElement[bool]],
) -> Select[Any]:
    """`base_select` -> CRM clause -> shared filters -> table facets, in that order.

    The order is the point: every step only calls `.where()` on what the step before it
    returned, so no step can drop a predicate an earlier one added. §4.8 in particular
    applies the entity clause *before* anything narrows or pages - a tab that paged first
    could otherwise walk out into the portal's other calls.
    """
    statement = base_select(principal)
    if crm is not None:
        statement = statement.where(crm)
    return statement.where(*filters.predicates(), *extra)


def _page_statement(filtered: Select[Any], page: int) -> Select[Any]:
    """The page itself.

    `bx_id DESC` is the tiebreaker after `call_start_date DESC`: two calls that started in
    the same second must not be able to swap places between page 1 and page 2, which is
    how an OFFSET page silently repeats or skips a row.
    """
    return (
        filtered.with_only_columns(*_ROW_COLUMNS)
        .order_by(Call.call_start_date.desc(), Call.bx_id.desc())
        .offset((page - 1) * _PAGE_SIZE)
        # One row past the page: `has_more` without asking a second question.
        .limit(_PAGE_SIZE + 1)
    )


def _count_statement(filtered: Select[Any]) -> Select[Any]:
    """How many rows the filter set matches in total.

    The table needs it: without a total the SPA cannot tell a short last page from the end
    of the data, and cannot render a pager at all. It costs a second pass over the same
    index range the page used - `calls_portal_start_idx` covers this predicate set (§3),
    so it is bounded by the period rather than by history.
    """
    return filtered.with_only_columns(func.count(Call.id))


@router.get("/calls", dependencies=[Depends(require_data_access)])
async def list_calls(
    request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """One page of the table: 50 rows, `call_start_date DESC` (§4.7, §4.8).

    The response carries `period` and `filters` exactly as `GET /dashboard` describes
    them (`CallFilters.describe()`), so the SPA can label the table with the same resolved
    dates and timezone it labels the charts with instead of re-deriving them.

    `total` - and the `pages` it implies - comes from a second pass over the same
    filtered range, because a table that cannot say how many rows it is paging through
    cannot draw a pager; `has_more` stays beside it as the answer the page already knew.
    """
    try:
        filters = parse_filters(request.query_params, principal)
        facets, facet_echo = _table_facets(request.query_params)
        page = _page(request.query_params)
    except FilterError as exc:
        # A malformed filter is not an authentication failure, so it is answered here
        # rather than raised as a `PrincipalError` (`services/stats.py` owns that
        # distinction and `api/dashboard.py` makes the same call).
        _log.info(
            "calls: filter rejected",
            extra={"portal_id": principal.portal_id, "code": exc.code},
        )
        return exc.as_response()

    crm = await crm_clause_for(principal)
    filtered = _filtered(principal, filters, crm, facets)

    async with tenant_txn(principal.portal_id) as session:
        fetched = (await session.execute(_page_statement(filtered, page))).all()
        total = int((await session.execute(_count_statement(filtered))).scalar_one())
        has_more = len(fetched) > _PAGE_SIZE
        page_rows = fetched[:_PAGE_SIZE]
        employees = await _employee_names(
            session,
            principal.portal_id,
            {int(row.portal_user_id) for row in page_rows if row.portal_user_id is not None},
        )

    rows: list[dict[str, Any]] = []
    for row in page_rows:
        user_id = int(row.portal_user_id) if row.portal_user_id is not None else None
        rows.append(
            {
                # `calls.id`, the surrogate key (§3): an opaque row id for `play-url` and
                # `refresh`. `bx_id` is not exposed - it is the sync cursor, not an API.
                "id": int(row.id),
                "call_start_date": row.call_start_date.isoformat(),
                "employee_id": user_id,
                "employee": employees.get(user_id) if user_id is not None else None,
                # Raw as delivered (§3): an unknown CALL_TYPE renders as "Other". It does
                # not disappear and it does not stall anything.
                "call_type": row.call_type,
                "phone_number": row.phone_number,
                "portal_number": row.portal_number,
                # The parts of a `BX24.openPath('/crm/<type>/details/<id>/')` link
                # (§4.10); the SPA builds the path, the server never emits a portal URL.
                "crm": {
                    "type": row.crm_entity_type,
                    "id": row.crm_entity_id,
                    "activity_id": row.crm_activity_id,
                },
                "duration": int(row.call_duration),
                # Generated server-side (§3) so the table, the summary tiles and the
                # recheck job agree on what "missed" means...
                "result_group": row.result_group,
                # ...and the raw code beside it, because 486 and 603 are different answers
                # to a salesperson even though both are "not connected".
                "failed_code": row.call_failed_code,
                "line": {"rest_app_id": row.rest_app_id, "name": row.rest_app_name},
                "has_record": bool(row.has_record),
                "record_duration": row.record_duration,
            }
        )

    return JSONResponse(
        {
            "rows": rows,
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "pages": max(1, -(-total // _PAGE_SIZE)),
            "has_more": has_more,
            **filters.describe(),
            # The two facets the charts do not share, echoed beside the ones they do.
            "table_filters": facet_echo,
            # The SPA hides the player entirely when recordings are off (§9) instead of
            # offering a button that always answers 409.
            "recording_mode": settings.recording_mode,
            "scope": principal.access,
        }
    )


# --- POST /calls/{id}/play-url -------------------------------------------------------


def _correlation_id() -> uuid.UUID:
    """Reuse the request id as `rest_log.correlation_id` (§6), as the handlers do."""
    raw = get_request_id()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    return uuid.uuid4()


async def _viewer_access_token(request: Request) -> str | None:
    """`{"access_token": ...}` from the body, or None - and never the value in an error.

    Parsed by hand for §4.6's reason: FastAPI's `RequestValidationError` renders the
    offending `input`, and for this endpoint that input is a live Bitrix24 access token.
    No log line in this module names it either.
    """
    raw = await request.body()
    if not raw:
        return None
    if len(raw) > _MAX_BODY_BYTES:
        raise PrincipalError("bad_request", 400)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise PrincipalError("bad_request", 400) from None
    if not isinstance(payload, dict):
        raise PrincipalError("bad_request", 400)
    value = payload.get("access_token")
    if value is None:
        return None
    if not isinstance(value, str):
        raise PrincipalError("bad_request", 400)
    token = value.strip()
    if not token:
        return None
    if not _TOKEN_MIN <= len(token) <= _TOKEN_MAX or not set(token) <= _TOKEN_CHARS:
        raise PrincipalError("bad_request", 400)
    return token


async def _call_in_scope(principal: Principal, call_id: int) -> Any | None:
    """One row, re-scoped (§4.7). `None` means "not yours" and "not there" alike."""
    statement = (
        base_select(principal)
        .where(Call.id == call_id)
        .with_only_columns(Call.id, Call.has_record, Call.record_duration)
    )
    async with tenant_txn(principal.portal_id) as session:
        return (await session.execute(statement)).first()


@router.post("/calls/{call_id}/play-url", dependencies=[Depends(require_data_access)])
async def play_url(
    call_id: int, request: Request, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """Mint the `?t=` playback grant for one call (§4.6, §9).

    The grant is minted only after `scope_filter` matched the row under the live session,
    which is what makes `record.py`'s later check a *re*-check rather than the only one:
    the narrow token carries `acc`, `sub` and `cid` so the same predicate can be rebuilt
    without a session, and both ends apply it.

    `RECORDING_MODE=off` (the v1 default until the §9 spike is answered) refuses here as
    well as in `record.py`: the SPA renders "open the call in Bitrix24" and never puts a
    dead URL into an `<audio>` element.

    **The viewer's own token, for non-admins** (§4.7, §9): Bitrix24's "Call Recording:
    Listen" is a separate permission from "Call statistics - view", and REST exposes
    neither. The only honest enforcement is to let Bitrix24 enforce it - by fetching the
    recording *as the viewer*. That token cannot ride on the `<audio src>` (a query
    parameter is precisely what browser history and proxies keep), so the SPA posts it
    here, in a body excluded from every log, where it is proven against `user.current`
    (same `sub`, §9 step 3) and then held in memory for the life of the grant, keyed by
    the same `(pid, sub, cid)` triple the grant itself names. Administrators need none of
    this: the portal token already has full telephony access (§11 assumption 4) and
    re-proving them would cost a REST round trip per play.
    """
    if settings.recording_mode == "off":
        # Not 404: the row exists and may well have a recording. The SPA must be able to
        # tell "we cannot play this here" from "there is nothing to play" (§9).
        raise PrincipalError("recording_disabled", 409)

    row = await _call_in_scope(principal, call_id)
    if row is None:
        raise PrincipalError("call_not_found", 404)
    if not row.has_record:
        raise PrincipalError("record_missing", 404)

    viewer_token = await _viewer_access_token(request)
    if principal.access != _ACCESS_ALL:
        if viewer_token is None:
            # A machine code, not a failure: the SPA answers it by calling
            # `BX24.getAuth()` and posting again (§9 step 3).
            raise PrincipalError("viewer_token_required", 409)
        async with control_txn() as session:
            portal = (
                await session.execute(select(Portal).where(Portal.id == principal.portal_id))
            ).scalar_one_or_none()
        if portal is None or portal.status != _ACTIVE:
            raise PrincipalError("portal_inactive", 401)
        try:
            identity = await resolve_identity(
                # The stored endpoint, never DOMAIN (§4.1).
                endpoint=portal.client_endpoint,
                access_token=viewer_token,
                portal_id=portal.id,
                member_id=portal.member_id,
                correlation_id=_correlation_id(),
            )
        except BitrixError as exc:
            # The class, never the code string - `bitrix/errors.py` owns that mapping.
            _log.info(
                "calls: viewer token refused",
                extra={"portal_id": principal.portal_id, "error": type(exc).__name__},
            )
            raise PrincipalError("viewer_token_required", 409) from None
        if identity.user_id != principal.user_id:
            # The session and the posted token belong to two different people. The same
            # answer `/session/exchange` gives to the same discovery: no session.
            _log.warning(
                "calls: play token request for another user",
                extra={"portal_id": principal.portal_id, "user_id": principal.user_id},
            )
            raise PrincipalError("invalid_session", 401)
        remember_viewer_token(
            pid=principal.portal_id,
            sub=principal.user_id,
            cid=call_id,
            token=viewer_token,
            ttl_seconds=_PLAY_TTL_SECONDS,
        )

    token = issue_play_token(
        pid=principal.portal_id,
        sub=principal.user_id,
        acc=principal.access,
        cid=call_id,
        ttl_seconds=_PLAY_TTL_SECONDS,
    )
    # A relative path: the SPA is framed at our own origin, and an absolute URL would put
    # `APP_BASE_URL` and the grant together into one more string that could be shared.
    return JSONResponse(
        {
            "url": f"/api/v1/calls/{call_id}/record?t={token}",
            "expires_in": _PLAY_TTL_SECONDS,
            "duration": row.record_duration,
        }
    )


# --- POST /calls/{id}/refresh --------------------------------------------------------

#: portal_id -> timestamps of recent refresh requests (§5.7 rule 3).
_refresh_window: dict[int, deque[float]] = {}


def _charge_refresh(portal_id: int) -> bool:
    """Sliding window per portal; False when the bucket is full.

    Per *portal*, not per user: what is being protected is the sync worker's next visit,
    which everyone in the portal shares. §5.7's case is a page stuck in a playback retry
    loop, and that is one user spending the whole portal's budget.
    """
    now = time.monotonic()
    window = _refresh_window.setdefault(portal_id, deque())
    while window and now - window[0] >= _REFRESH_WINDOW_S:
        window.popleft()
    if len(window) >= _REFRESH_LIMIT:
        return False
    window.append(now)
    # Bounded memory: an emptied bucket is dropped rather than kept per portal forever.
    for key in [key for key, aged in _refresh_window.items() if not aged]:
        del _refresh_window[key]
    return True


def reset_refresh_rate_limit() -> None:
    """Clear the windows. For tests and for an operator-driven unblock only."""
    _refresh_window.clear()


@router.post("/calls/{call_id}/refresh", dependencies=[Depends(require_data_access)])
async def request_refresh(
    call_id: int, principal: Principal = Depends(get_principal)
) -> JSONResponse:
    """§5.7 rule 3: flag one row for a targeted re-read on the next sync visit.

    Called by the SPA when playback answered 403/404 - a recording that was deleted, a URL
    that expired, or a row cached before the recording was attached (§5.7 exists because
    `CALL_RECORD_URL` is filled *after* the row first appears). The flag is idempotent,
    costs one indexed update, and is cleared by the upsert that re-reads the row.

    The scope check is the whole authorisation: an `own` viewer can flag only their own
    calls, and a row that is not theirs answers `call_not_found` - the same answer a row
    that does not exist gets, because the difference is not theirs to learn.

    The write is a keyed UPDATE inside the same `tenant_txn` as the read that authorised
    it: `portal_id` + `id` + the scope predicate again, so the statement is correct on its
    own terms and not merely because of what ran before it. It is the only write in this
    module, and §3 names `refresh_requested` as this endpoint's column.
    """
    if not _charge_refresh(principal.portal_id):
        _log.info("calls: refresh rate limited", extra={"portal_id": principal.portal_id})
        return JSONResponse(
            {"code": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(_REFRESH_RETRY_AFTER)},
        )

    read = base_select(principal).where(Call.id == call_id).with_only_columns(Call.id)
    scope = scope_filter(principal)
    statement = update(Call).where(Call.portal_id == principal.portal_id, Call.id == call_id)
    if scope is not None:
        statement = statement.where(scope)
    statement = statement.values(refresh_requested=True)

    async with tenant_txn(principal.portal_id) as session:
        if (await session.execute(read)).first() is None:
            raise PrincipalError("call_not_found", 404)
        await session.execute(statement)

    _log.info(
        "calls: refresh requested",
        extra={"portal_id": principal.portal_id, "user_id": principal.user_id},
    )
    # 202: accepted, not done. The row is re-read on the next visit of the sync worker
    # (§5.9 orders `refresh_requested` rows first), seconds to minutes away.
    return JSONResponse({"status": "queued"}, status_code=202)
