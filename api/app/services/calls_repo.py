"""The single place `calls` is read (§4.7). No route code lives here.

§4.7 makes one sentence structural: "`scope_filter` is the single place; every read
(dashboard, calls, filters, record, refresh) goes through `calls_repo`". Two rules meet
in this module and neither is enforceable if reads are spread out:

* **Tenant isolation** (§3) is RLS-first: `calls` carries FORCED row-level security
  bound to `app.portal_id`, so a query issued outside `tenant_txn(portal_id)` returns
  zero rows *silently*. `base_select` still adds an explicit `portal_id` predicate -
  not because RLS might fail, but because every index in §3 leads with `portal_id`, and
  because a query that says out loud which tenant it is for is reviewable on its own.
* **The permission scope** (§4.7) collapses Bitrix24's four statistics levels to
  `all` / `own` / `denied`. `own` means one extra predicate, `denied` means the query
  must not run at all: a denied user was refused `voximplant.statistic.get` by the
  portal itself, and answering them from our cache would republish exactly what
  Bitrix24 withheld. `scope_filter` therefore *raises* for `denied` rather than
  returning a predicate that happens to match nothing - a "match nothing" filter is one
  refactor away from being dropped as redundant.

Milestone 5 adds the aggregations (`services/stats.py`) and the endpoints; they start
from `base_select` and never build their own `select(Call)`.

One thing this module deliberately does not do: hand out `call_record_url`. §3 stores it
with credentials stripped and §9 keeps it server-side; a projection for the browser is
built by the endpoint, from named columns, never by serialising the ORM row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Call
from app.logging import get_logger
from app.security.principal import Principal, PrincipalError

if TYPE_CHECKING:
    # Type-only: the data layer must not import the service that resolves CRM contexts
    # (§4.4 step 7 writes it, §4.8 reads it), and nothing here needs it at runtime -
    # `crm_match_clause` only reads `entity_keys` and `activity_ids` off the value.
    from app.services.crm_context import CrmContext

__all__ = ["base_select", "crm_match_clause", "scope_filter"]

_log = get_logger(__name__)

#: §4.7's three levels, as they appear in the JWT `acc` claim.
_ALL: Final[str] = "all"
_OWN: Final[str] = "own"
_DENIED: Final[str] = "denied"


def scope_filter(principal: Principal) -> ColumnElement[bool] | None:
    """The per-user row predicate, or None when the whole portal is visible (§4.7).

    * `all` (administrators) -> None: no extra predicate. §11 assumption 4 - a portal
      administrator always has full telephony access - is what makes this safe.
    * `own` -> `portal_user_id = <viewer>`. A non-admin whose probe succeeded may have
      "own", "department" or "any" level at Bitrix24 and REST cannot tell them apart
      (research note: no method exposes telephony roles), so the narrowest of the three
      is the only honest choice. The SPA hides the employee filter and shows the "you
      see your own calls only" banner for the same reason.
    * `denied` -> raises. Never reached in practice (`require_data_access` answers 403
      first), which is the point: this is the backstop for an endpoint that forgets the
      dependency, and it fails the request rather than the check.

    NULL `portal_user_id` rows (a statistic row with no user) are excluded by the `own`
    predicate, which is correct: they are nobody's own calls.
    """
    if principal.access == _ALL:
        return None
    if principal.access == _OWN:
        return Call.portal_user_id == principal.user_id
    if principal.access == _DENIED:
        raise PrincipalError("no_stats_permission", 403)
    # An `acc` outside the three levels cannot come from `issue_session` (it validates),
    # so this is a corrupted or hand-built claim set: fail closed, never open.
    _log.error(
        "calls_repo: unknown access level on a verified principal",
        extra={"portal_id": principal.portal_id, "user_id": principal.user_id},
    )
    raise PrincipalError("no_stats_permission", 403)


def base_select(principal: Principal) -> Select[tuple[Call]]:
    """The statement every read of `calls` starts from (§4.7).

    Carries the tenant predicate and the scope predicate and nothing else - no period,
    no paging, no ordering: those belong to the endpoint that knows what it is asking.
    Callers narrow it with `.where(...)`, `.with_only_columns(...)` or a `GROUPING SETS`
    projection; what they cannot do is start somewhere else and forget one of these two.

    Must be executed inside `tenant_txn(principal.portal_id)`. The explicit `portal_id`
    predicate does not make a control-plane transaction safe - RLS still hides every row
    there - it makes the index choice explicit and the statement self-describing.
    """
    stmt = select(Call).where(Call.portal_id == principal.portal_id)
    scope = scope_filter(principal)
    return stmt if scope is None else stmt.where(scope)


def crm_match_clause(
    ctx: CrmContext,
    entity_type: str | None = None,
    entity_id: int | None = None,
) -> ColumnElement[bool]:
    """§4.8's three-way match for a CRM tab, as one OR-ed predicate.

    `entity_type` / `entity_id` default to the context's own entity and exist so the
    caller can pin the clause to the entity the **JWT** names (`ent`) rather than to
    whatever the cached row says it is about - the two are the same row by construction
    (§4.8 looks the context up by that key), and saying so at the call site is what makes
    a mismatch impossible rather than merely unlikely.

    A call is shown on an entity's tab when any of these holds:

    1. `(crm_entity_type, crm_entity_id)` is one of the resolved `entity_keys` - for a
       deal that is its contacts and companies, because telephony rows are documented to
       carry CONTACT / COMPANY / LEAD, never the deal itself.
    2. `crm_activity_id` is one of the activity ids bound to the entity - the call
       activities on the entity's timeline (capped at `CRM_ACTIVITY_CAP`, §3). This is
       the clause that catches a call linked to the deal through its timeline while its
       `crm_entity_*` points at the contact.
    3. `(crm_entity_type, crm_entity_id)` is the entity itself. §4.8 spells out why this
       exists: some portals *do* emit `DEAL` (and other undocumented types) in the
       statistics rows, and §3 stores `crm_entity_type` raw precisely so those rows
       survive. Without this clause such a portal would show an empty deal tab while the
       rows sit in the table.

    The caller has already decided the row is fresh enough to use (§4.8: `resolved_at >=
    JWT.iat`, else 409 `context_missing`); this function only builds the predicate, and
    combines with `base_select` - so `scope_filter` still applies on a CRM tab.
    """
    own_type = ctx.entity_type if entity_type is None else entity_type
    own_id = ctx.entity_id if entity_id is None else entity_id
    clauses: list[ColumnElement[bool]] = []

    for pair in ctx.entity_keys or []:
        # JSONB round-trips these as lists; a malformed entry is skipped rather than
        # allowed to poison the whole clause - clause 3 still matches the entity itself.
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _log.warning("calls_repo: skipping malformed crm entity key")
            continue
        key_type, key_id = pair
        if not isinstance(key_type, str) or isinstance(key_id, bool):
            _log.warning("calls_repo: skipping malformed crm entity key")
            continue
        try:
            numeric_id = int(key_id)
        except (TypeError, ValueError):
            _log.warning("calls_repo: skipping malformed crm entity key")
            continue
        clauses.append(
            and_(Call.crm_entity_type == key_type, Call.crm_entity_id == numeric_id)
        )

    activity_ids = [int(value) for value in (ctx.activity_ids or [])]
    if activity_ids:
        # `IN` rather than `= ANY(:array)`: identical plan on the
        # `calls_portal_activity_idx` partial index, and it keeps the parameter list
        # readable in `EXPLAIN` output when support debugs an empty tab.
        clauses.append(Call.crm_activity_id.in_(activity_ids))

    clauses.append(
        and_(Call.crm_entity_type == own_type, Call.crm_entity_id == int(own_id))
    )
    return or_(*clauses)
