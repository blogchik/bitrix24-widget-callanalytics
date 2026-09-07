"""One batch of `voximplant.statistic.get` pages, and the two rules that make a cursor
safe to move afterwards (§5.2, §5.4).

Every fetch in this system is a `batch` of up to `batch_pages` commands issued at fixed
offsets. Two failure modes of that shape are silent, permanent data loss, so they are
decided here once instead of in `head_fetch`, `incremental`, `backfill`, `rescan` and
the recheck budget separately:

**1. The contiguous-prefix rule (§5.2).** With `halt=0` a batch answers HTTP 200 and
reports per-command failures in `result_error`. Command 3 of 20 can fail while 4..19
succeed. Advancing the cursor to the end of the batch would then skip page 3 forever -
the forward cursor only ever looks *above* `high_id` and the backward cursor only
*below* `low_id`, so nothing revisits a hole between them. The cursor may therefore
advance only across the longest error-free PREFIX; the rows returned by later commands
are still real rows and worth upserting (`extra_rows`), they simply must not move
anything. `prefix_len` is what the caller uses to decide how far it may go.

**2. The filter-honoured guard (§5.4).** The whole design rests on `FILTER[>ID]` /
`FILTER[<ID]` being honoured. A build that ignores the operator - lagging on-premise
versions do exist (§11 assumption 1) - answers HTTP 200 with the *same* first page every
time, and the naive loop asks for it again, forever, against an API shared by every
tenant. So every returned row is asserted against the filter it was requested with. A
violation is reported in `filter_violation` and the outcome is neutered: no rows in the
cursor-advancing set at all, so no caller can move a cursor even by mistake. §5.4 then
parks the portal with `token_status='filter_unsupported'`, `next_run_at='infinity'` and
a `portal_events(sync_blocked)` - loudly and once, never a retry.

A per-command error is a VALUE here, never an exception: the prefix rule needs to see
the whole ordered list. A failure of the batch request itself (transport, non-2xx,
top-level `error`) still raises out of `bitrix/client.py` - in that case nothing about
the batch succeeded and there is no prefix to reason about.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.bitrix.client import MAX_BATCH_COMMANDS, BatchResult, BitrixClient
from app.bitrix.errors import BitrixError, classify
from app.bitrix.statistic import (
    FILTER_OPERATORS,
    PAGE_SIZE,
    STATISTIC_METHOD,
    parse_rows,
    statistic_params,
)
from app.logging import get_logger
from app.sync.throttle import merge_time_blocks

__all__ = ["FetchOutcome", "fetch_pages"]

_log = get_logger(__name__)

#: The cursor column of `calls` and the only field the filter guard asserts on: it is the
#: one that can turn an ignored operator into an infinite loop (§5.2).
_ID_FIELD: Final[str] = "ID"
_ID_COLUMN: Final[str] = "bx_id"


def _key(index: int) -> str:
    """The batch key of the command at `index`.

    Must match `bitrix/client.py::_CMD_KEY_RE`, and carries the index rather than the
    offset so a `rest_log` row lines up with the ordered `commands` list the prefix rule
    is defined on.
    """
    return f"p{index}"


@dataclass(frozen=True)
class FetchOutcome:
    """Everything one batch of pages tells the caller, with the prefix rule applied.

    `rows` and `extra_rows` are split rather than flagged because the distinction is the
    whole point: `rows` may advance a cursor, `extra_rows` may not, and a caller that
    concatenates them has re-introduced the 50-row hole this module exists to prevent.
    """

    #: Parsed rows from the error-free PREFIX only - the rows a cursor may be moved over.
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: Parsed rows from commands at or after the first failure. Safe to upsert (they are
    #: real rows, and the upsert is idempotent), but they must NOT move a cursor.
    extra_rows: list[dict[str, Any]] = field(default_factory=list)
    #: `(bx_id or None, reason)` for every row the parser refused, prefix or not (§5.5).
    rejected: list[tuple[int | None, str]] = field(default_factory=list)
    #: Number of leading commands with no error and a well-formed result.
    prefix_len: int = 0
    #: Number of commands actually issued (== len(starts)).
    command_count: int = 0
    #: `result_total` of the FIRST command - the size of the whole selection (§5.2, §5.4).
    total: int | None = None
    #: True when the last command of the prefix still reported `result_next`, i.e. the
    #: selection continues past this batch.
    has_next: bool = False
    #: Every per-command error, in the order requested. Empty on a fully clean batch.
    errors: list[BitrixError] = field(default_factory=list)
    #: The batch's `time{}` folded with the per-command `result_time` blocks, ready for
    #: `throttle.observe_time_block` (§5.6: operating is the max over the sub-commands).
    time_block: dict[str, Any] | None = None
    #: Set when a returned row breaks the filter it was requested with. The caller must
    #: treat it as terminal (§5.4); `rows` is empty whenever it is set.
    filter_violation: str | None = None

    @property
    def clean(self) -> bool:
        """True when every command succeeded and every row honoured the filter."""
        return not self.errors and self.filter_violation is None


# ------------------------------------------------------------------- the filter guard


def _as_int(value: Any) -> int | None:
    """An id from `7`, `"7"` or `7.0`; anything else cannot be asserted on."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("-"):
            text = text[1:]
            return -int(text) if text.isascii() and text.isdigit() else None
        return int(text) if text.isascii() and text.isdigit() else None
    return None


def _split_operator(key: str) -> tuple[str, str]:
    """`">=ID"` -> `(">=", "ID")`. Bitrix24 puts the operator in front of the field.

    Split with `statistic.FILTER_OPERATORS` (longest first) rather than a local character
    set: that tuple is what `statistic_params` validates the filter against, and a guard
    that segmented `">=ID"` differently from the builder would assert against a clause
    that was never sent.
    """
    text = str(key).strip()
    for operator in FILTER_OPERATORS:
        if text.startswith(operator):
            return operator, text[len(operator) :].strip().upper()
    return "", text.upper()


def _scalar_check(operator: str, bound: int) -> Callable[[int], bool] | None:
    """The predicate for `>ID`, `<ID`, `>=ID`, `<=ID`, `ID` or `!ID` against one bound.

    Built by a factory rather than a closure inside the loop so the bound is captured by
    value - a late-binding closure would assert every row against the LAST clause of the
    filter, which is a guard that silently checks the wrong thing.
    """
    if operator == ">":
        return lambda value: value > bound
    if operator == ">=":
        return lambda value: value >= bound
    if operator == "<":
        return lambda value: value < bound
    if operator == "<=":
        return lambda value: value <= bound
    if operator in ("", "="):
        return lambda value: value == bound
    if operator in ("!", "!="):
        return lambda value: value != bound
    return None


def _list_check(operator: str, ids: frozenset[int]) -> Callable[[int], bool] | None:
    """The predicate for an `ID` clause whose value is a list of ids (§5.7's recheck).

    Membership OR the closed range is accepted on purpose: the method reference shows
    `{'ID': [1, 7]}` in a way that reads as a range, while the recheck budget uses the
    same shape as an IN-list. Accepting either reading still catches the only case that
    matters - a build that ignored the clause and answered with unrelated ids - without
    parking a portal (`next_run_at='infinity'`, admin-only recovery) over an ambiguity in
    someone else's documentation.
    """
    low, high = min(ids), max(ids)
    if operator in ("", "=", "@"):
        return lambda value: value in ids or low <= value <= high
    if operator in ("!", "!=", "!@"):
        return lambda value: value not in ids
    return None


def _id_checks(filter: dict[str, Any]) -> list[tuple[str, Callable[[int], bool]]]:
    """Build one predicate per `ID` clause of the filter it was requested with (§5.4).

    Only `ID` is asserted: it is the cursor field, and an ignored operator on it is the
    difference between "one page re-read" and "an infinite loop against a shared API".
    An unknown operator produces no check rather than a false violation - a wrong park is
    as bad as a missed one, because `next_run_at='infinity'` needs an admin to clear.
    """
    checks: list[tuple[str, Callable[[int], bool]]] = []
    for raw_key, raw_value in (filter or {}).items():
        operator, field_name = _split_operator(raw_key)
        if field_name != _ID_FIELD:
            continue
        label = f"FILTER[{raw_key}]={raw_value!r}"
        check: Callable[[int], bool] | None

        if isinstance(raw_value, (list, tuple, set, frozenset)):
            items = list(raw_value)
            ids = [value for value in (_as_int(item) for item in items) if value is not None]
            # A value we cannot read is a value we must not assert on.
            check = _list_check(operator, frozenset(ids)) if ids and len(ids) == len(items) else None
        else:
            bound = _as_int(raw_value)
            check = None if bound is None else _scalar_check(operator, bound)

        if check is not None:
            checks.append((label, check))
    return checks


def _violation(
    row: Mapping[str, Any],
    checks: Sequence[tuple[str, Callable[[int], bool]]],
    guard: Callable[[dict[str, Any]], bool] | None,
) -> str | None:
    """The first filter clause this row breaks, or None. Never raises.

    A guard that raises would turn "the portal ignores an operator" into a 500 in the
    worker, and the whole point of §5.4 is that this condition is reported, not crashed.
    """
    bx_id = _as_int(row.get(_ID_COLUMN))
    if bx_id is not None:
        for label, check in checks:
            try:
                honoured = check(bx_id)
            except Exception:  # pragma: no cover - a predicate above cannot raise
                honoured = True
            if not honoured:
                return f"{label} returned bx_id={bx_id}"
    if guard is not None:
        try:
            honoured = guard(dict(row))
        except Exception as exc:  # a caller-supplied predicate is not trusted to be total
            return f"guard raised {type(exc).__name__} for bx_id={bx_id}"
        if not honoured:
            return f"guard rejected bx_id={bx_id}"
    return None


# ------------------------------------------------------------------------- the fetch


def _validate_starts(starts: Sequence[int]) -> list[int]:
    """Offsets must be distinct, non-negative multiples of the fixed 50-row page.

    Failing here costs no request against the shared 2 req/s bucket, and each of these
    mistakes is a data-loss bug rather than a runtime condition: a duplicate offset
    double-counts `backfill_done`, and an offset off the page grid silently skips rows
    between two pages.
    """
    values = [int(start) for start in starts]
    if not values:
        raise ValueError("fetch_pages requires at least one start offset")
    if len(values) > MAX_BATCH_COMMANDS:
        raise ValueError(f"fetch_pages accepts at most {MAX_BATCH_COMMANDS} pages per batch")
    if len(set(values)) != len(values):
        raise ValueError(f"fetch_pages received duplicate start offsets: {values!r}")
    for value in values:
        if value < 0 or value % PAGE_SIZE:
            raise ValueError(f"start offset must be a non-negative multiple of {PAGE_SIZE}: {value}")
    return values


def _result_rows(result: Any) -> tuple[Sequence[Any] | None, str | None]:
    """The row list of one command, or `(None, reason)` when the shape is unusable.

    An unusable shape truncates the prefix exactly like an error does. Reading it as
    "zero rows" would be worse than a failure: zero rows with no `result_next` is
    indistinguishable from "the selection ended", which is how a cursor jumps a page.
    """
    if isinstance(result, (list, tuple)):
        return result, None
    if isinstance(result, Mapping):
        # Some builds wrap a single page as `{"0": {...}, "1": {...}}` (PHP renders a
        # non-sequential array as an object); values in insertion order are the rows.
        return list(result.values()), None
    return None, f"unexpected result shape {type(result).__name__}"


async def fetch_pages(
    client: BitrixClient,
    *,
    filter: dict[str, Any],
    sort: str,
    order: str,
    starts: Sequence[int],
    guard: Callable[[dict[str, Any]], bool] | None = None,
) -> FetchOutcome:
    """Issue ONE batch of `statistic.get` pages and apply §5.2 and §5.4 to the answer.

    Always a `batch`, even for a single page: the envelope keeps `result_next` and
    `result_total` OUTSIDE `result`, and `BitrixClient.call` returns only `result`. §5.4
    sizes the following batch from exactly those two numbers, so the probe command has to
    travel the batch path too - and a batch of one costs the same single request against
    the intensity bucket as a plain call.

    The caller owns pacing and the operating-time guard (`sync/throttle.py`); this
    function performs no retry and no back-off of its own, so that one request here is
    one request against the bucket every other tenant shares.

    `guard` is applied IN ADDITION to the checks derived from `filter`, never instead of
    them: the derived assertion is the safety property and must not be switchable off.
    """
    offsets = _validate_starts(starts)
    commands = [
        (
            _key(index),
            STATISTIC_METHOD,
            statistic_params(filter=filter, sort=sort, order=order, start=start),
        )
        for index, start in enumerate(offsets)
    ]

    # A whole-batch failure (transport, non-2xx, top-level error) raises out of here by
    # design: nothing about the batch succeeded, so there is no prefix to reason about.
    batch: BatchResult = await client.batch(commands, halt=0)

    checks = _id_checks(filter)
    rows: list[dict[str, Any]] = []
    extra_rows: list[dict[str, Any]] = []
    rejected: list[tuple[int | None, str]] = []
    errors: list[BitrixError] = []
    violation: str | None = None

    # The prefix ends at the first command that either reported an error or answered with
    # a shape we cannot read; both mean "we did not see this page".
    prefix_len = len(batch.commands)
    parsed_by_index: list[list[dict[str, Any]]] = []

    for index, command in enumerate(batch.commands):
        if command.error is not None:
            errors.append(command.error)
            prefix_len = min(prefix_len, index)
            parsed_by_index.append([])
            continue
        page, reason = _result_rows(command.result)
        if page is None:
            error = classify(
                None,
                description=f"statistic.get page at start={offsets[index]}: {reason}",
            )
            errors.append(error)
            prefix_len = min(prefix_len, index)
            parsed_by_index.append([])
            continue
        outcome = parse_rows(page)
        rejected.extend(outcome.rejected)
        parsed_by_index.append(list(outcome.rows))

    for index, parsed in enumerate(parsed_by_index):
        for row in parsed:
            if violation is None:
                violation = _violation(row, checks, guard)
            if index < prefix_len:
                rows.append(row)
            else:
                extra_rows.append(row)

    first = batch.commands[0] if batch.commands else None
    total = first.total if first is not None and first.error is None else None
    last_prefix = batch.commands[prefix_len - 1] if prefix_len > 0 else None
    has_next = last_prefix is not None and last_prefix.next is not None

    time_block = merge_time_blocks(
        [batch.time, *(command.time for command in batch.commands)]
    )

    if violation is not None:
        # Neutered on purpose (§5.4): with the operator ignored, NOTHING in this answer
        # may move a cursor, and a caller that forgot to check `filter_violation` must
        # still be unable to advance. The rows themselves stay available to upsert.
        _log.error(
            "sync: statistic.get ignored its filter",
            extra={"filter_violation": violation, "sort": sort, "order": order},
        )
        extra_rows = rows + extra_rows
        rows = []
        prefix_len = 0
        has_next = False
    elif errors:
        _log.warning(
            "sync: batch page failed, cursor stops at the error-free prefix",
            extra={
                "prefix_len": prefix_len,
                "command_count": len(batch.commands),
                "error_code": errors[0].code,
            },
        )

    return FetchOutcome(
        rows=rows,
        extra_rows=extra_rows,
        rejected=rejected,
        prefix_len=prefix_len,
        command_count=len(batch.commands),
        total=total,
        has_next=has_next,
        errors=errors,
        time_block=time_block,
        filter_violation=violation,
    )
