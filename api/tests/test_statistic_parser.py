"""§5.5 - `bitrix/statistic.py` turns one `voximplant.statistic.get` page into columns.

WHY this file exists and why it is driven by a stored page rather than by dicts built
inline: the parser is the boundary where a foreign system's JSON becomes our schema, and
the research notes (`docs/bitrix24-api-research.md`, note (a)) record that Bitrix24
serialises numbers as strings, uses `''` and `null` interchangeably, and ships values -
`603-S`, `DEAL`, an unknown `CALL_TYPE` - that no CHECK constraint of ours may reject
(decision 11). `tests/fixtures/statistic_page.json` is that page, kept verbatim so a
future contributor can diff it against a real capture.

The property the whole quarantine design rests on is the last test in the group:
**one unparsable row must not cost the other four**. If `parse_rows` ever raises instead
of quarantining, the chunk rolls back, the cursor never advances, and the portal stalls
forever on a single bad row (§5.5, decision 11) - a silent, permanent data loss that no
alarm would catch. So the assertion is not "the poison row is rejected", it is
"the poison row is rejected AND every other row of the same page came through".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from app.bitrix.statistic import PAGE_SIZE, STATISTIC_METHOD, parse_rows, statistic_params

_PAGE_PATH: Final[Path] = Path(__file__).parent / "fixtures" / "statistic_page.json"

#: Every data column of `calls` the parser is allowed to produce (§3). Generated columns
#: (`has_record`, `result_group`) and bookkeeping columns (`last_synced_at`, …) are the
#: upsert's business, never the parser's, so a key outside this set means the parser is
#: inventing a column and the INSERT would fail at runtime with an unrelated-looking error.
CALL_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "bx_id",
        "call_id",
        "external_call_id",
        "call_category",
        "call_type",
        "call_start_date",
        "call_duration",
        "call_failed_code",
        "call_failed_reason",
        "portal_user_id",
        "portal_number",
        "phone_number",
        "crm_entity_type",
        "crm_entity_id",
        "crm_activity_id",
        "cost",
        "cost_currency",
        "call_vote",
        "call_record_url",
        "record_file_id",
        "record_duration",
        "rest_app_id",
        "rest_app_name",
        "transcript_id",
        "transcript_pending",
        "session_id",
        "redial_attempt",
        "comment",
        "call_log",
    }
)

#: The one row of the stored page that cannot be placed on any chart: its
#: CALL_START_DATE is the MySQL zero date, which no ISO-8601 parser accepts.
POISON_BX_ID: Final[int] = 1004


def _raw_page() -> list[dict[str, Any]]:
    return json.loads(_PAGE_PATH.read_text(encoding="utf-8"))


def _by_id(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["bx_id"]): row for row in rows}


def test_the_unparsable_row_is_quarantined_and_the_rest_of_the_page_survives() -> None:
    """The quarantine property: a poison row costs one row, never the page (§5.5)."""
    raw = _raw_page()
    outcome = parse_rows(raw)

    parsed = _by_id(outcome.rows)
    assert sorted(parsed) == [1001, 1002, 1003, 1005], "one bad row must not eat its neighbours"
    assert len(outcome.rows) == len(raw) - 1

    assert [bx_id for bx_id, _ in outcome.rejected] == [POISON_BX_ID]
    reason = outcome.rejected[0][1]
    # The reason is what `portal_events(row_rejected)` shows support; an empty string
    # would make the audit trail useless exactly when someone is reading it.
    assert isinstance(reason, str) and reason.strip()


def test_parsed_rows_only_carry_real_call_columns() -> None:
    """The parser's output is fed straight into the upsert's column list (§5.5)."""
    for row in parse_rows(_raw_page()).rows:
        unknown = set(row) - CALL_COLUMNS
        assert not unknown, f"parser produced columns that are not in `calls`: {sorted(unknown)}"
        # Without these two the row cannot be inserted at all: bx_id is the upsert key
        # and call_start_date is NOT NULL.
        assert row.get("bx_id") is not None
        assert row.get("call_start_date") is not None


def test_string_typed_numbers_become_int_and_decimal() -> None:
    """Bitrix24 serialises numerics as strings (research note (a)); money is never a float."""
    row = _by_id(parse_rows(_raw_page()).rows)[1001]

    assert row["bx_id"] == 1001
    assert isinstance(row["bx_id"], int)
    assert row["call_type"] == 1
    assert row["call_duration"] == 63
    assert row["portal_user_id"] == 42
    assert row["crm_entity_id"] == 275
    assert row["crm_activity_id"] == 8001
    assert row["call_vote"] == 5
    assert row["rest_app_id"] == 12
    assert row["record_duration"] == 60
    assert row["redial_attempt"] == 0
    # SESSION_ID exceeds 2^31: an `int` column would have overflowed, the schema uses bigint.
    assert row["session_id"] == 3841557776

    # RECORD_FILE_ID arrives as a BARE integer in the same page - both shapes must land.
    assert row["record_file_id"] == 9079

    # COST: `numeric(12,4)` in §3 precisely so "0.0000" never becomes a binary float.
    assert row["cost"] == Decimal("0.0000")
    assert type(row["cost"]) is Decimal, "cost must be Decimal - a float loses money at scale"

    assert row["transcript_pending"] is False  # "N"


def test_empty_string_and_null_are_both_null() -> None:
    """`''` and `null` mean the same absent value in this API (§5.5)."""
    row = _by_id(parse_rows(_raw_page()).rows)[1002]

    # Sent as null …
    for column in ("external_call_id", "call_log", "crm_activity_id", "rest_app_id", "record_file_id"):
        assert row.get(column) is None, column
    # … and sent as '' - indistinguishable to the caller, so identical after parsing.
    for column in (
        "call_category",
        "call_record_url",
        "crm_entity_type",
        "crm_entity_id",
        "cost",
        "cost_currency",
        "session_id",
        "record_duration",
        "comment",
        "transcript_pending",
    ):
        assert row.get(column) is None, column

    # "0" is a value, not an absence: a zero-duration missed call is real data.
    assert row["call_duration"] == 0


def test_call_start_date_keeps_its_offset_and_comes_out_utc() -> None:
    """The portal's offset is data, not decoration: it fixes the instant (§3, note (a))."""
    parsed = _by_id(parse_rows(_raw_page()).rows)

    started = parsed[1001]["call_start_date"]
    assert isinstance(started, datetime)
    assert started.tzinfo is not None, "a naive datetime would be read as the SERVER's timezone"
    assert started.utcoffset() == timedelta(0), "stored UTC, per §3"
    assert started == datetime(2025, 8, 6, 11, 8, 40, tzinfo=UTC)  # 14:08:40+03:00

    # A late-evening +03:00 call must not drift onto the next UTC day by accident.
    assert parsed[1003]["call_start_date"] == datetime(2025, 8, 6, 20, 59, 59, tzinfo=UTC)


def test_undocumented_values_are_stored_raw() -> None:
    """Decision 11: no CHECK, no mapping, no rejection - the UI maps unknowns (§3)."""
    parsed = _by_id(parse_rows(_raw_page()).rows)

    # CALL_TYPE 1..5 is documented; 7 is not. It must survive as an integer.
    assert parsed[1003]["call_type"] == 7
    # DEAL is not a documented CRM_ENTITY_TYPE, but portals emit it (§5.5, note (a)).
    assert parsed[1003]["crm_entity_type"] == "DEAL"
    # `603-S` is why call_failed_code is a varchar and not an int.
    assert parsed[1003]["call_failed_code"] == "603-S"
    assert isinstance(parsed[1003]["call_failed_code"], str)

    assert parsed[1005]["transcript_pending"] is True  # "Y"
    assert parsed[1005]["transcript_id"] == 1


def test_record_url_credentials_are_stripped() -> None:
    """Decision 21: the stored URL must never carry a token, not even in our own database."""
    parsed = _by_id(parse_rows(_raw_page()).rows)
    url = parsed[1001]["call_record_url"]

    assert isinstance(url, str) and url
    assert "SEEKRETACCESSTOKEN0011" not in url, "the access token reached the calls table"
    for parameter in ("auth=", "token=", "sig="):
        assert parameter not in url, f"credential parameter {parameter!r} survived the parse"
    # Stripping parameters must not destroy the address itself - the proxy needs it.
    assert url.startswith("https://portal.bitrix24.test/rest/download.json")

    # A URL with no credential parameters is left alone.
    assert parsed[1005]["call_record_url"] == "https://portal.bitrix24.test/download/1005.mp3?fileId=9080"


def test_unusable_entries_are_rejected_without_an_id_and_never_raise() -> None:
    """A page is attacker-adjacent data: anything at all may arrive in it (§5.5).

    `rejected` carries `None` for the id when even the id could not be read - the
    contract's `tuple[int | None, str]`. Nothing here may raise, because a raise inside
    the parser aborts the whole chunk and stalls the cursor.
    """
    outcome = parse_rows([None, "not a row", [], {"CALL_START_DATE": "2025-08-06T14:08:40+03:00"}])

    assert outcome.rows == []
    assert len(outcome.rejected) == 4
    assert all(bx_id is None for bx_id, _ in outcome.rejected)
    assert all(isinstance(reason, str) and reason.strip() for _, reason in outcome.rejected)


def test_an_empty_page_is_not_an_error() -> None:
    """Backfill's termination condition is exactly "a page with no rows" (§5.3)."""
    outcome = parse_rows([])
    assert outcome.rows == []
    assert outcome.rejected == []


def test_statistic_params_carry_the_documented_request_shape() -> None:
    """FILTER / SORT / ORDER / start, page size fixed at 50 (research note (b))."""
    assert STATISTIC_METHOD == "voximplant.statistic.get"
    assert PAGE_SIZE == 50

    params = statistic_params(filter={">ID": 500}, sort="ID", order="DESC", start=100)
    assert params["FILTER"] == {">ID": 500}
    assert params["SORT"] == "ID"
    assert params["ORDER"] == "DESC"
    assert params["start"] == 100

    # The defaults are the incremental walk of §5.4: forward, by ID, from the first page.
    default = statistic_params(filter={">ID": 0})
    assert default["SORT"] == "ID"
    assert default["ORDER"] == "ASC"
    assert default.get("start", 0) == 0
