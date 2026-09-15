"""Funnels and stages, with their names, read with the installer credential (§5.10, G0 Q1).

The Deals report's columns are portal data: which funnels exist, which stages each has, in what
order, and whether a stage means won, lost or in progress. `bitrix/deals.py` already reads all
of that for the live report; this module stores it, so the mirror can answer without a
dictionary request per viewer.

The rule that is new here is about deletion. A funnel or stage missing from one answer is only
marked (`missing_since`); it is removed when a later read, at least a day after the mark, still
does not list it. Nothing is marked unless every command about that entity answered cleanly, so
a truncated or half-failed answer can never erase a column.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.client import MAX_BATCH_COMMANDS, BatchResult, BitrixClient
from app.bitrix.crm_items import ENTITY_DEAL, ENTITY_LEAD, Command
from app.bitrix.deals import (
    CATEGORIES_KEY,
    CRM_STATUS_LIST,
    Funnel,
    Stage,
    category_commands,
    parse_funnels,
    parse_stages,
    status_commands,
    status_key,
)
from app.bitrix.errors import BitrixError, MethodNotFound, classify
from app.db.models import CrmFunnel, CrmStage
from app.db.session import tenant_txn
from app.sync.crm_lanes import Lane, store_lanes
from app.sync.lease import Fence, fenced_update
from app.sync.throttle import merge_time_blocks

__all__ = ["LEAD_STATUSES_KEY", "REMOVE_AFTER", "Dictionary", "read_dictionary", "store_dictionary"]

#: Batch key of the lead pipeline's statuses (`crm.status.list`, `ENTITY_ID = STATUS`).
LEAD_STATUSES_KEY: Final[str] = "lst"

#: A dictionary row still missing this long after it was first missed is removed.
REMOVE_AFTER: Final[dt.timedelta] = dt.timedelta(hours=24)

Pace = Callable[[dict[str, Any] | None], Awaitable[bool]]


@dataclass
class Dictionary:
    """What one dictionary read produced."""

    #: `(entity type id, funnel)`. Leads have one pipeline, stored as category 0.
    funnels: list[tuple[int, Funnel]] = field(default_factory=list)
    stages: list[tuple[int, Stage]] = field(default_factory=list)
    errors: list[BitrixError] = field(default_factory=list)
    #: Entities whose every command answered cleanly: only these may mark rows missing.
    complete_entities: set[int] = field(default_factory=set)
    batches: int = 0
    universal: bool = True
    time_block: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return self.complete_entities >= {ENTITY_DEAL, ENTITY_LEAD}


def _first_commands(*, universal: bool) -> list[Command]:
    return [
        *category_commands(universal=universal),
        (
            LEAD_STATUSES_KEY,
            CRM_STATUS_LIST,
            {"order": {"SORT": "ASC"}, "filter": {"ENTITY_ID": "STATUS"}},
        ),
    ]


def _error_of(batch: BatchResult, key: str) -> BitrixError | None:
    """The command's error, and an error too when the batch did not answer it at all."""
    if batch.ok(key):
        return None
    return batch.error(key) or classify(None, description=f"batch answered no {key}")


def _merged_time(batch: BatchResult) -> dict[str, Any] | None:
    return merge_time_blocks([batch.time, *(command.time for command in batch.commands)])


async def read_dictionary(client: BitrixClient, *, pace: Pace) -> Dictionary:
    """Funnels, their stages and the lead statuses, in as few batches as the portal allows.

    One batch for the funnel list and the lead statuses, then one per 50 funnels for the
    stages. The frozen `crm.dealcategory.*` family is entered only when `crm.category.list`
    answers `ERROR_METHOD_NOT_FOUND`, never by a version number (`bitrix/deals.py`).
    """
    out = Dictionary()
    first = await client.batch(_first_commands(universal=True), halt=0)
    out.batches += 1
    if isinstance(first.error(CATEGORIES_KEY), MethodNotFound):
        if not await pace(_merged_time(first)):
            return out
        first = await client.batch(_first_commands(universal=False), halt=0)
        out.batches += 1
        out.universal = False
    out.time_block = _merged_time(first)

    lead_error = _error_of(first, LEAD_STATUSES_KEY)
    if lead_error is None:
        out.funnels.append((ENTITY_LEAD, Funnel(id=0, name="", sort=0, is_default=True)))
        out.stages.extend(
            (ENTITY_LEAD, stage)
            for stage in parse_stages(first.get(LEAD_STATUSES_KEY), category_id=0)
        )
        out.complete_entities.add(ENTITY_LEAD)
    else:
        out.errors.append(lead_error)

    category_error = _error_of(first, CATEGORIES_KEY)
    if category_error is not None:
        out.errors.append(category_error)
        return out
    funnels = parse_funnels(first.get(CATEGORIES_KEY), universal=out.universal)
    if not any(funnel.id == 0 for funnel in funnels):
        # The frozen `crm.dealcategory.list` does not list the default funnel on every build,
        # and a mirror without funnel 0 would file most deals under no funnel at all.
        funnels.insert(0, Funnel(id=0, name="", sort=0, is_default=True))
    out.funnels.extend((ENTITY_DEAL, funnel) for funnel in funnels)

    deal_clean = True
    ids = [funnel.id for funnel in funnels]
    previous = first
    for start in range(0, len(ids), MAX_BATCH_COMMANDS):
        if not await pace(_merged_time(previous)):
            return out
        chunk = ids[start : start + MAX_BATCH_COMMANDS]
        batch = await client.batch(status_commands(chunk, universal=out.universal), halt=0)
        out.batches += 1
        out.time_block = _merged_time(batch)
        for category_id in chunk:
            error = _error_of(batch, status_key(category_id))
            if error is not None:
                out.errors.append(error)
                deal_clean = False
                continue
            out.stages.extend(
                (ENTITY_DEAL, stage)
                for stage in parse_stages(batch.get(status_key(category_id)), category_id=category_id)
            )
        previous = batch
    if deal_clean:
        out.complete_entities.add(ENTITY_DEAL)
    return out


async def _forget_missing(
    session: AsyncSession,
    model: type[CrmFunnel] | type[CrmStage],
    key_columns: Sequence[str],
    *,
    portal_id: int,
    entity_type_id: int,
    seen: set[tuple[Any, ...]],
    now: dt.datetime,
) -> None:
    """Mark rows the read did not list; remove the ones already missing for a day."""
    columns = [getattr(model, name) for name in key_columns]
    rows = (
        await session.execute(
            select(*columns, model.missing_since).where(
                model.portal_id == portal_id, model.entity_type_id == entity_type_id
            )
        )
    ).all()
    cutoff = now - REMOVE_AFTER
    remove: list[tuple[Any, ...]] = []
    mark: list[tuple[Any, ...]] = []
    for row in rows:
        key = tuple(row[: len(key_columns)])
        missing_since = row[len(key_columns)]
        if key in seen:
            continue
        if missing_since is None:
            mark.append(key)
        elif missing_since <= cutoff:
            remove.append(key)
    scope = (model.portal_id == portal_id, model.entity_type_id == entity_type_id)
    if remove:
        await session.execute(delete(model).where(*scope, tuple_(*columns).in_(remove)))
    if mark:
        await session.execute(
            update(model).where(*scope, tuple_(*columns).in_(mark)).values(missing_since=now)
        )


def _funnel_rows(
    portal_id: int, funnels: Iterable[tuple[int, Funnel]], now: dt.datetime
) -> list[dict[str, Any]]:
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    for entity_type_id, funnel in funnels:
        rows[(entity_type_id, funnel.id)] = {
            "portal_id": portal_id,
            "entity_type_id": entity_type_id,
            "category_id": funnel.id,
            "name": funnel.name,
            "sort": funnel.sort,
            "is_default": funnel.is_default,
            "seen_at": now,
            "missing_since": None,
        }
    return list(rows.values())


def _stage_rows(
    portal_id: int, stages: Iterable[tuple[int, Stage]], now: dt.datetime
) -> list[dict[str, Any]]:
    rows: dict[tuple[int, int, str], dict[str, Any]] = {}
    for entity_type_id, stage in stages:
        if not stage.status_id:
            continue
        rows[(entity_type_id, stage.category_id, stage.status_id)] = {
            "portal_id": portal_id,
            "entity_type_id": entity_type_id,
            "category_id": stage.category_id,
            "status_id": stage.status_id,
            "name": stage.name,
            "sort": stage.sort,
            "semantic": stage.semantic,
            "seen_at": now,
            "missing_since": None,
        }
    return list(rows.values())


async def store_dictionary(
    fence: Fence, dictionary: Dictionary, *, now: dt.datetime, lanes: Sequence[Lane] = ()
) -> None:
    """Upsert what was read, forget what a complete read no longer lists, store the lanes."""
    portal_id = fence.portal_id
    funnel_rows = _funnel_rows(portal_id, dictionary.funnels, now)
    stage_rows = _stage_rows(portal_id, dictionary.stages, now)
    async with tenant_txn(portal_id) as session:
        if funnel_rows:
            statement = pg_insert(CrmFunnel).values(funnel_rows)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["portal_id", "entity_type_id", "category_id"],
                    set_={
                        name: statement.excluded[name]
                        for name in ("name", "sort", "is_default", "seen_at", "missing_since")
                    },
                )
            )
        if stage_rows:
            statement = pg_insert(CrmStage).values(stage_rows)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["portal_id", "entity_type_id", "category_id", "status_id"],
                    set_={
                        name: statement.excluded[name]
                        for name in ("name", "sort", "semantic", "seen_at", "missing_since")
                    },
                )
            )
        for entity_type_id in sorted(dictionary.complete_entities):
            await _forget_missing(
                session,
                CrmFunnel,
                ("category_id",),
                portal_id=portal_id,
                entity_type_id=entity_type_id,
                seen={
                    (row["category_id"],)
                    for row in funnel_rows
                    if row["entity_type_id"] == entity_type_id
                },
                now=now,
            )
            await _forget_missing(
                session,
                CrmStage,
                ("category_id", "status_id"),
                portal_id=portal_id,
                entity_type_id=entity_type_id,
                seen={
                    (row["category_id"], row["status_id"])
                    for row in stage_rows
                    if row["entity_type_id"] == entity_type_id
                },
                now=now,
            )
        if lanes:
            await store_lanes(session, portal_id, lanes)
        await fenced_update(session, fence, {})
