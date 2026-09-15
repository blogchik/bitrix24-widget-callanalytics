"""The CRM mirror's three reads, each exactly one `batch` (§5.6 pacing belongs to the caller).

* `fetch_ranges` - one keyset page per open id range (backfill, patrol).
* `fetch_by_ids` - the stored select for known ids (the dirty refresh). An id missing from
  a CLEAN answer is only a candidate: a list omits a deleted record and an unreadable one
  alike, so nothing here tombstones.
* `confirm_absent` - `crm.item.get` per candidate, sorted into not-found (proof of a
  deletion, S-A.7), access-denied (the token narrowed) and unresolved.

No retries and no back-off: one call here is one request against the bucket every tenant
shares, and the lane that called decides what an error means for its schedule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.bitrix.client import MAX_BATCH_COMMANDS, BitrixClient
from app.bitrix.crm_items import (
    MAX_IDS_PER_COMMAND,
    ItemRow,
    MirrorDialect,
    get_command,
    ids_command,
    page_rows,
    parse_item,
    row_id,
)
from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    InvalidCredentials,
    NotFound,
    UserAccessError,
    classify,
)
from app.sync.crm_ranges import (
    RangeBatchOutcome,
    RangeStream,
    apply_range_batch,
    range_commands,
)
from app.sync.throttle import merge_time_blocks

__all__ = [
    "ConfirmOutcome",
    "IdsOutcome",
    "confirm_absent",
    "fetch_by_ids",
    "fetch_ranges",
]


async def fetch_ranges(
    client: BitrixClient,
    dialect: MirrorDialect,
    streams: Sequence[RangeStream],
    *,
    max_commands: int,
    utm_max_chars: int,
    ids_only: bool = False,
) -> RangeBatchOutcome | None:
    """One batch over the open ranges; `None` when every range is already done."""
    sent = range_commands(dialect, streams, max_commands=max_commands, ids_only=ids_only)
    if not sent:
        return None
    batch = await client.batch([command for _, command in sent], halt=0)
    return apply_range_batch(dialect, streams, sent, batch, utm_max_chars=utm_max_chars)


@dataclass(frozen=True)
class IdsOutcome:
    rows: list[ItemRow] = field(default_factory=list)
    rejected: list[tuple[int | None, str]] = field(default_factory=list)
    #: Asked for in a clean command and not returned: candidates for `confirm_absent`.
    missing: set[int] = field(default_factory=set)
    #: Asked for in a command that errored: nothing is known about them.
    unresolved: set[int] = field(default_factory=set)
    errors: list[BitrixError] = field(default_factory=list)
    time_block: dict[str, Any] | None = None


def _chunks(values: Sequence[int], size: int) -> list[list[int]]:
    return [list(values[start : start + size]) for start in range(0, len(values), size)]


async def fetch_by_ids(
    client: BitrixClient,
    dialect: MirrorDialect,
    ids: Sequence[int],
    *,
    utm_max_chars: int,
) -> IdsOutcome:
    """The stored select for up to 2 500 ids (50 commands of 50)."""
    wanted = sorted({int(value) for value in ids if int(value) > 0})
    if not wanted:
        return IdsOutcome()
    chunks = _chunks(wanted, MAX_IDS_PER_COMMAND)
    if len(chunks) > MAX_BATCH_COMMANDS:
        raise ValueError(f"fetch_by_ids accepts at most {MAX_BATCH_COMMANDS * MAX_IDS_PER_COMMAND} ids")
    commands = [ids_command(dialect, f"i{index}", chunk) for index, chunk in enumerate(chunks)]
    batch = await client.batch(commands, halt=0)
    by_key = {command.key: command for command in batch.commands}

    outcome = IdsOutcome(time_block=merge_time_blocks([batch.time, *(c.time for c in batch.commands)]))
    for (key, _method, _params), chunk in zip(commands, chunks, strict=True):
        command = by_key.get(key)
        error: BitrixError | None = None
        page = None
        if command is None:
            error = classify(None, description=f"batch answered no {key}")
        elif command.error is not None:
            error = command.error
        else:
            page = page_rows(command.result, dialect)
            if page is None:
                error = classify(None, description=f"{key}: unexpected result shape")
        if error is not None or page is None:
            outcome.errors.append(error or classify(None, description=key))
            outcome.unresolved.update(chunk)
            continue

        asked = set(chunk)
        returned: list[ItemRow] = []
        stray: int | None = None
        for raw in page:
            parsed = parse_item(raw, dialect, utm_max_chars=utm_max_chars)
            if isinstance(parsed, str):
                outcome.rejected.append((row_id(raw, dialect), parsed))
                continue
            if parsed.id not in asked:
                stray = parsed.id
                break
            returned.append(parsed)
        if stray is not None:
            # An id nobody asked for means `@id` was not honoured. The whole command is
            # unreadable: none of its rows is kept, and none of its ids is called missing.
            outcome.errors.append(
                classify(None, description=f"{key}: returned id {stray} it was not asked for")
            )
            outcome.unresolved.update(asked)
            continue
        outcome.rows.extend(returned)
        outcome.missing.update(asked - {row.id for row in returned})
    return outcome


@dataclass(frozen=True)
class ConfirmOutcome:
    #: `crm.item.get` answered NOT_FOUND: the record is gone.
    not_found: set[int] = field(default_factory=set)
    #: Refused for rights: the record may exist and the token can no longer read it.
    access_denied: set[int] = field(default_factory=set)
    #: The record came back after all.
    present: set[int] = field(default_factory=set)
    #: Anything else, including a missing command: nothing is known.
    unresolved: dict[int, BitrixError] = field(default_factory=dict)
    time_block: dict[str, Any] | None = None


_RIGHTS_ERRORS = (AccessDenied, UserAccessError, InvalidCredentials)


async def confirm_absent(client: BitrixClient, entity_type_id: int, ids: Sequence[int]) -> ConfirmOutcome:
    """`crm.item.get` for up to 50 candidates, sorted by what the answer proves."""
    wanted = sorted({int(value) for value in ids if int(value) > 0})
    if not wanted:
        return ConfirmOutcome()
    if len(wanted) > MAX_BATCH_COMMANDS:
        raise ValueError(f"confirm_absent accepts at most {MAX_BATCH_COMMANDS} ids")
    commands = [get_command(entity_type_id, f"g{item_id}", item_id) for item_id in wanted]
    batch = await client.batch(commands, halt=0)
    by_key = {command.key: command for command in batch.commands}

    outcome = ConfirmOutcome(time_block=merge_time_blocks([batch.time, *(c.time for c in batch.commands)]))
    for item_id in wanted:
        command = by_key.get(f"g{item_id}")
        if command is None:
            outcome.unresolved[item_id] = classify(None, description=f"batch answered no g{item_id}")
        elif isinstance(command.error, NotFound):
            outcome.not_found.add(item_id)
        elif isinstance(command.error, _RIGHTS_ERRORS):
            outcome.access_denied.add(item_id)
        elif command.error is not None:
            outcome.unresolved[item_id] = command.error
        else:
            outcome.present.add(item_id)
    return outcome
