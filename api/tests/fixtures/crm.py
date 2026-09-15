"""An in-memory CRM for the mirror's extraction code, duck-typed as `BitrixClient`.

Same approach as `test_cursor.py::FakeStatistics`: the code under test takes a client, and
giving it a real one plus a mock transport would only move the guesswork into HTTP
encoding. The answers follow what a real portal was measured to do
(docs/spike-crm-mirror.md, S-A): `result.items`, 50 rows, `total`/`next` null under
`start: -1`, `closed` as `Y`/`N`, `contactIds` as a list, `operating` as an accumulator.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.bitrix.client import BatchResult, CommandResult
from app.bitrix.errors import BitrixError, classify

PAGE: int = 50


def deal_row(item_id: int, **overrides: Any) -> dict[str, Any]:
    """A deal as `crm.item.list` spells it, carrying every field the mirror selects."""
    row: dict[str, Any] = {
        "id": item_id,
        "categoryId": 0,
        "stageId": "NEW",
        "stageSemanticId": "P",
        "assignedById": 7,
        "createdTime": "2026-09-01T10:00:00+03:00",
        "updatedTime": "2026-09-02T11:30:00+03:00",
        "movedTime": "2026-09-01T10:00:00+03:00",
        "closed": "N",
        "opportunity": 1500,
        "currencyId": "KZT",
        "leadId": 0,
        "contactIds": [12],
        "companyId": 0,
        "utmSource": "",
        "utmMedium": "",
        "utmCampaign": "",
        "utmContent": "",
        "utmTerm": "",
        # Present on the portal, never selected: proves `select` is applied.
        "title": "Customer content the mirror must not receive",
    }
    row.update(overrides)
    return row


def _id_of(row: Mapping[str, Any]) -> int:
    return int(row.get("id", row.get("ID")))


class CrmStub:
    """`crm.item.list` / `crm.item.get` over an in-memory table, with a full request log."""

    def __init__(self, deal_ids: Iterable[int] = ()) -> None:
        self.items: dict[int, dict[int, dict[str, Any]]] = {2: {}, 1: {}}
        for item_id in deal_ids:
            self.items[2][int(item_id)] = deal_row(int(item_id))
        #: One entry per HTTP request: the commands it carried.
        self.requests: list[list[tuple[str, str, dict[str, Any]]]] = []
        #: (request index, command index) -> error returned in `result_error`.
        self.errors: dict[tuple[int, int], BitrixError] = {}
        #: Answer every list as if its filter were absent (a build ignoring an operator).
        self.ignore_filter = False
        #: (entity type, id) that `crm.item.get` refuses for rights.
        self.forbidden: set[tuple[int, int]] = set()
        #: A command answering this non-list shape instead of its rows.
        self.malformed: set[tuple[int, int]] = set()
        self.operating = 0.0
        self.last_time: dict[str, Any] | None = None
        #: How the portal shifts a datetime filter (S-A.10): `>=updatedTime: v` selects from
        #: `v - filter_shift`, as a portal whose token user sits that far east of the server.
        self.filter_shift = dt.timedelta(0)

    # -- client surface ----------------------------------------------------------------

    async def __aenter__(self) -> CrmStub:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def batch(
        self, commands: Sequence[tuple[str, str, dict[str, Any]]], *, halt: int = 0
    ) -> BatchResult:
        index = len(self.requests)
        recorded = [(key, method, dict(params)) for key, method, params in commands]
        self.requests.append(recorded)
        results: list[CommandResult] = []
        for position, (key, method, params) in enumerate(recorded):
            error = self.errors.get((index, position))
            if error is not None:
                results.append(CommandResult(key=key, result=None, error=error, time=None))
                continue
            self.operating += 0.25
            time = self._time()
            if (index, position) in self.malformed:
                results.append(CommandResult(key=key, result="not a page", error=None, time=time))
                continue
            if method == "crm.item.list":
                rows, total = self._list(params)
                results.append(
                    CommandResult(key=key, result={"items": rows}, error=None, time=time, total=total)
                )
            elif method == "crm.item.get":
                results.append(self._get(key, params, time))
            else:  # pragma: no cover - the extraction code calls nothing else
                results.append(CommandResult(key=key, result=True, error=None, time=time))
        self.last_time = self._time()
        return BatchResult(commands=tuple(results), time=self.last_time)

    # -- behaviour ---------------------------------------------------------------------

    def _time(self) -> dict[str, Any]:
        now = dt.datetime.now(tz=dt.UTC)
        return {
            "duration": 0.25,
            "processing": 0,
            "operating": self.operating,
            "operating_reset_at": int((now + dt.timedelta(minutes=10)).timestamp()),
            "date_start": now.isoformat(),
        }

    def _list(self, params: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
        table = self.items[int(params["entityTypeId"])]
        rows = [table[item_id] for item_id in sorted(table)]
        if not self.ignore_filter:
            rows = [row for row in rows if self._matches(row, params.get("filter") or {})]
        order = params.get("order") or {}
        if str(next(iter(order.values()), "ASC")).upper() == "DESC":
            rows.reverse()
        total = len(rows) if int(params.get("start", 0)) >= 0 else None
        select = params.get("select") or []
        page = [{field: row[field] for field in select if field in row} for row in rows[:PAGE]]
        return page, total

    def _matches(self, row: Mapping[str, Any], filter: Mapping[str, Any]) -> bool:
        item_id = _id_of(row)
        for key, value in filter.items():
            if key == ">id" and not item_id > int(value):
                return False
            if key == "<id" and not item_id < int(value):
                return False
            if key == ">=id" and not item_id >= int(value):
                return False
            if key == "@id" and item_id not in {int(v) for v in value}:
                return False
            if key == ">=updatedTime" and not (
                dt.datetime.fromisoformat(row["updatedTime"])
                >= dt.datetime.fromisoformat(value) - self.filter_shift
            ):
                return False
        return True

    def _get(self, key: str, params: Mapping[str, Any], time: dict[str, Any]) -> CommandResult:
        entity, item_id = int(params["entityTypeId"]), int(params["id"])
        if (entity, item_id) in self.forbidden:
            error = classify("ACCESS_DENIED", description="Access denied", http_status=403)
            return CommandResult(key=key, result=None, error=error, time=None)
        row = self.items[entity].get(item_id)
        if row is None:
            error = classify("NOT_FOUND", description="Элемент не найден")
            return CommandResult(key=key, result=None, error=error, time=None)
        return CommandResult(key=key, result={"item": row}, error=None, time=time)
