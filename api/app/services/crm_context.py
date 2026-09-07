"""Resolve a CRM tab's matching set, and cache it honestly (§4.4 steps 4-7, §4.8, §3).

A `CRM_*_DETAIL_TAB` open has to answer "which cached call rows belong to THIS card?".
For LEAD / CONTACT / COMPANY that is the entity itself, because `voximplant.statistic.get`
can name those in `CRM_ENTITY_TYPE`. For a DEAL it cannot - `CRM_ENTITY_TYPE` is documented
as CONTACT, COMPANY or LEAD only (docs/bitrix24-api-research.md, correction 1) - so a deal
is matched through its contacts, its company and the ids of its call activities. Building
those command triples is `bitrix/crm.py`'s job; this module turns their results into a row
and decides when that row may be read back.

Three rules, and all three exist for the same reason - **we never model Bitrix24's CRM
permissions, we borrow the answer**:

1. **Resolution uses the opener's own token.** The batch was issued with `AUTH_ID` (§4.4
   step 4), so Bitrix24 evaluated the read permission on that deal, not us.
2. **All or nothing.** `resolve_crm_context` returns `None` if any required command
   errored or is missing, and §4.4 step 5 then renders `crm_no_access` and mints **no**
   entity JWT. A half-resolved context would be a cache row that quietly under- or
   over-reports for everyone who comes after.
3. **A cached row is not a permission.** `load_crm_context` serves a row only when the
   caller resolved it themselves or it was resolved after their session began (§4.8); see
   that function's docstring for why the alternative is a replay of someone else's rights.

Writes go through `tenant_txn` (§1 decision 8): `crm_contexts` carries FORCED row-level
security bound to the transaction-local `app.portal_id`, and RLS fails **silently** closed
- an INSERT from a control transaction is rejected by WITH CHECK, a SELECT returns nothing.
`tests/test_registry_lint.py` names this module as the only writer of the table.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from app.bitrix.client import BatchResult
from app.bitrix.crm import (
    DEAL_CONTACTS_KEY,
    DEAL_KEY,
    ENTITY_TYPES,
    context_command_keys,
    parse_activity_ids,
    parse_deal_entity_keys,
    present_activity_keys,
)
from app.db.models import CrmContext as CrmContextRow
from app.db.session import tenant_txn
from app.logging import get_logger

__all__ = [
    "CrmContext",
    "load_crm_context",
    "resolve_crm_context",
    "store_crm_context",
]

log = get_logger(__name__)


@dataclass(frozen=True)
class CrmContext:
    """What a CRM tab must match, as §3's COMMENT ON describes the row.

    `entity_keys` is a list of `[type, id]` pairs - `[["CONTACT", 12], ["COMPANY", 3]]` -
    stored as jsonb and read back by §4.8's first matching clause. Lists rather than tuples
    because that is what a jsonb round trip produces, and a dataclass whose field changes
    shape between "just resolved" and "just loaded" would be a trap.

    Frozen: this object travels from the batch parser into a cache write and, on the read
    path, into a SQL filter. Neither should be able to edit it in passing.
    """

    entity_type: str
    entity_id: int
    entity_keys: list[list[Any]]
    activity_ids: list[int]


def _check_entity_type(entity_type: str) -> str:
    """`crm_contexts_type_chk` (§3), enforced before the statement runs.

    The type is ours - it comes from the `PLACEMENT` allowlist of §4.2 - so an unknown one
    is a bug in our routing, not bad input from a portal. Failing here turns it into a
    clean error instead of a constraint violation that aborts the caller's transaction.
    """
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown CRM entity type: {entity_type!r}")
    return entity_type


def _check_entity_id(entity_id: int) -> int:
    """A CRM id is a positive integer; it reaches us from an attacker-chosen POST (§4.2)."""
    number = int(entity_id)
    if number <= 0:
        raise ValueError(f"CRM entity id must be positive, got {entity_id!r}")
    return number


async def resolve_crm_context(
    batch: BatchResult, *, entity_type: str, entity_id: int
) -> CrmContext | None:
    """Read the open-time batch into a `CrmContext`, or `None` if it may not be trusted.

    `None` is the whole §4.4 step 5 rule: "if any CRM command returned an error, render
    `/state/crm_no_access` and mint **no** entity JWT". The commands ran with the opener's
    `AUTH_ID`, so an `ACCESS_DENIED` here means Bitrix24 says this user may not read this
    card - and a cached context resolved by someone who could must never be served to them.
    A *missing* command counts the same way: an absent `acts` key would otherwise resolve
    to an empty activity list and cache it as if the deal genuinely had no calls.

    Which keys are required comes from `crm.context_command_keys` (deal: `deal`,
    `contacts`, `acts`; the other three: `acts`), plus every follow-up activity page the
    caller chose to pack into the batch - a page that errored is a silently truncated set,
    which is the same kind of quiet wrongness the all-or-nothing rule exists to prevent.

    Not a coroutine by necessity - nothing here does I/O - but by contract: it sits between
    two `await`s in the handler and reads as one step of the flow.
    """
    entity_type = _check_entity_type(entity_type)
    entity_id = _check_entity_id(entity_id)

    # `dict.fromkeys` keeps the order and drops the one overlap: `acts` is both a
    # required command and the first activity page.
    required = tuple(
        dict.fromkeys((*context_command_keys(entity_type), *present_activity_keys(batch)))
    )
    sent = {command.key for command in batch.commands}
    for key in required:
        if key not in sent:
            log.info(
                "crm context unresolved: command missing",
                extra={"entity_type": entity_type, "entity_id": entity_id, "command": key},
            )
            return None
        error = batch.error(key)
        if error is not None:
            # `error.__class__.__name__`, never the code string: `bitrix/errors.py` owns
            # every comparison against a Bitrix24 error spelling (registry lint).
            log.info(
                "crm context unresolved: command failed",
                extra={
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "command": key,
                    "error": type(error).__name__,
                },
            )
            return None

    if entity_type == "DEAL":
        entity_keys = parse_deal_entity_keys(batch.get(DEAL_KEY), batch.get(DEAL_CONTACTS_KEY))
    else:
        # LEAD / CONTACT / COMPANY are values `CRM_ENTITY_TYPE` itself can carry, so the
        # entity is its own key (§4.8's first matching clause).
        entity_keys = [[entity_type, entity_id]]

    return CrmContext(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_keys=entity_keys,
        activity_ids=parse_activity_ids(batch),
    )


async def store_crm_context(portal_id: int, ctx: CrmContext, *, user_id: int) -> None:
    """Cache one resolution, stamped with WHO resolved it and WHEN (§4.4 step 7, §3).

    Only ever called with a `CrmContext` that `resolve_crm_context` returned, i.e. a fully
    successful resolution by the user who is opening the tab right now - §3: "Written ONLY
    from a fully successful resolution by the current opener." `resolved_by_user_id` and
    `resolved_at` are not bookkeeping; they are the two values §4.8's read rule is built
    on, and a row without them honest would be a row that can be replayed for anyone.

    `resolved_at` is the database's `now()` (transaction start), not a Python clock: the
    freshness comparison in `load_crm_context` is against timestamps from this same source,
    and mixing an application clock into that ordering would make the rule drift with NTP.

    Inside `tenant_txn` because `crm_contexts` is RLS-bound and fails silently closed - an
    INSERT from a control transaction is rejected by WITH CHECK and nothing is stored.
    """
    entity_type = _check_entity_type(ctx.entity_type)
    entity_id = _check_entity_id(ctx.entity_id)
    values: dict[str, Any] = {
        "portal_id": int(portal_id),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_keys": [list(pair) for pair in ctx.entity_keys],
        "activity_ids": [int(identifier) for identifier in ctx.activity_ids],
        "resolved_by_user_id": int(user_id),
        "resolved_at": func.now(),
    }
    statement = (
        pg_insert(CrmContextRow)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["portal_id", "entity_type", "entity_id"],
            set_={
                "entity_keys": values["entity_keys"],
                "activity_ids": values["activity_ids"],
                "resolved_by_user_id": values["resolved_by_user_id"],
                "resolved_at": func.now(),
            },
        )
    )
    async with tenant_txn(int(portal_id)) as session:
        await session.execute(statement)


def _as_entity_keys(raw: Any) -> list[list[Any]]:
    """jsonb -> `[["CONTACT", 12], ...]`, dropping anything that is not a `[type, id]` pair.

    Defensive because the column is jsonb: a row written by an older build, or repaired by
    hand in support, must not be able to put a malformed key into a SQL `IN` list.
    """
    keys: list[list[Any]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return keys
    for pair in raw:
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            continue
        entity_type, entity_id = pair[0], pair[1]
        if isinstance(entity_type, str) and isinstance(entity_id, int) and not isinstance(
            entity_id, bool
        ):
            keys.append([entity_type, entity_id])
    return keys


async def load_crm_context(
    portal_id: int,
    *,
    entity_type: str,
    entity_id: int,
    not_older_than: int,
    user_id: int,
) -> CrmContext | None:
    """The cached context, but only when it belongs to THIS session (§4.8).

    A row is served on exactly two conditions, and `None` otherwise:

    * `resolved_by_user_id == user_id` - the caller resolved it themselves, on this or an
      earlier open; or
    * `resolved_at >= not_older_than` (the JWT's `iat`) - it was resolved after this
      session's token was minted.

    WHY the rule is shaped like that, and what it stops: `crm_contexts` is a *shared*,
    per-portal cache. Without this test, an administrator opening a deal tab would leave
    behind a row listing the deal's contacts, its company and every call activity on it -
    and the next request from a salesperson who cannot read that deal would be answered
    from it. The privileged user's resolution would have become the unprivileged user's
    permission. Freshness is the proxy for provenance here: the only resolutions a caller
    may be served are their own, and ones that happened inside the lifetime of a token
    whose issue was itself gated on `resolve_crm_context` succeeding with the caller's own
    Bitrix24 token (§4.4 step 5 mints no entity JWT when a CRM command errors).

    When this returns `None` the caller answers **409 `context_missing`** - never 403 and
    never an empty result set. That code is a instruction to the SPA, not an error: it runs
    the `/session/exchange` of §4.6, which re-runs the open-time batch with the current
    user's fresh token, re-resolves the context and mints a new JWT. A user with the rights
    gets their tab a round trip later; a user without them gets `crm_no_access` from the
    exchange, decided by Bitrix24 rather than by this cache.

    `not_older_than` is a UNIX timestamp (the JWT claim's own units); it is compared as an
    aware UTC datetime against `resolved_at`, which `store_crm_context` writes from the
    database clock.
    """
    entity_type = _check_entity_type(entity_type)
    entity_id = _check_entity_id(entity_id)
    minted_at = dt.datetime.fromtimestamp(int(not_older_than), dt.UTC)

    statement = select(
        CrmContextRow.entity_keys,
        CrmContextRow.activity_ids,
        CrmContextRow.resolved_by_user_id,
        CrmContextRow.resolved_at,
    ).where(
        CrmContextRow.portal_id == int(portal_id),
        CrmContextRow.entity_type == entity_type,
        CrmContextRow.entity_id == entity_id,
    )
    async with tenant_txn(int(portal_id)) as session:
        row = (await session.execute(statement)).first()

    if row is None:
        return None

    resolved_at: dt.datetime = row.resolved_at
    if resolved_at.tzinfo is None:  # defensive: the column is timestamptz
        resolved_at = resolved_at.replace(tzinfo=dt.UTC)

    mine = row.resolved_by_user_id is not None and int(row.resolved_by_user_id) == int(user_id)
    if not mine and resolved_at < minted_at:
        # Stale for this caller, not "absent": logged so a support ticket about a tab that
        # keeps re-exchanging has something to read.
        log.info(
            "crm context too old for this session",
            extra={
                "portal_id": int(portal_id),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "user_id": int(user_id),
            },
        )
        return None

    return CrmContext(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_keys=_as_entity_keys(row.entity_keys),
        activity_ids=[int(identifier) for identifier in (row.activity_ids or [])],
    )
