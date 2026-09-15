"""`bitrix/crm_items.py`: the mirror's select allowlist, its commands, and its parsers.

The allowlist tests are the D-3 privacy decision written as assertions: the mirror stores
these fields and no others, so a select that grows by one field has to change this file in
the same commit. The parser tests use the shapes a real portal answered with
(docs/spike-crm-mirror.md, S-A), not the shapes the docs imply.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from app.bitrix import users
from app.bitrix import utm as utm_dialects
from app.bitrix.crm_items import (
    DEAL_ITEM,
    DEAL_LEGACY,
    DIALECTS,
    LEAD_ITEM,
    LEAD_LEGACY,
    ItemRow,
    MirrorDialect,
    _check_minimal,
    get_command,
    high_id_command,
    ids_command,
    keyset_command,
    page_rows,
    parse_item,
    parse_money,
    parse_timestamp,
    range_count_command,
)
from app.bitrix.errors import (
    EntityTypeNotSupported,
    IntranetUserOnly,
    InvalidArgValue,
    NotFound,
    UnknownBitrixError,
    classify,
)
from tests.fixtures.crm import deal_row

UTM_CAMEL = ["utmSource", "utmMedium", "utmCampaign", "utmContent", "utmTerm"]
UTM_UPPER = ["UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]


# --- the D-3 allowlist -----------------------------------------------------------------


def test_the_deal_select_is_exactly_the_stored_fields() -> None:
    assert DEAL_ITEM.select == [
        "id", "stageId", "stageSemanticId", "assignedById", "createdTime", "updatedTime",
        "movedTime", "contactIds", "companyId", "categoryId", "closed", "opportunity",
        "currencyId", "leadId", *UTM_CAMEL,
    ]
    assert "opportunityAccount" not in DEAL_ITEM.select, "S-A.5: never returned, so never asked"


def test_the_lead_select_is_exactly_the_stored_fields() -> None:
    assert LEAD_ITEM.select == [
        "id", "stageId", "stageSemanticId", "assignedById", "createdTime", "updatedTime",
        "movedTime", "contactIds", "companyId", *UTM_CAMEL, "contactId",
    ]


def test_the_legacy_selects_are_the_same_fields_upper_case() -> None:
    assert DEAL_LEGACY.select == [
        "ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "MOVED_TIME", "COMPANY_ID",
        "CATEGORY_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CLOSED", "OPPORTUNITY", "CURRENCY_ID",
        "LEAD_ID", *UTM_UPPER, "CONTACT_ID",
    ]
    assert LEAD_LEGACY.select == [
        "ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "MOVED_TIME", "COMPANY_ID",
        "STATUS_ID", "STATUS_SEMANTIC_ID", *UTM_UPPER, "CONTACT_ID",
    ]


@pytest.mark.parametrize("field", ["title", "TITLE", "*", "UF_CRM_1", "COMMENTS", "fm", "NAME", "phone"])
def test_a_dialect_naming_customer_content_is_refused(field: str) -> None:
    widened = MirrorDialect(
        name="widened",
        entity_type_id=2,
        method=DEAL_ITEM.method,
        universal=True,
        wire={**DEAL_ITEM.wire, "extra": field},
        contact_fallback=None,
        utm=DEAL_ITEM.utm,
    )
    with pytest.raises(ValueError, match="D-3"):
        _check_minimal(widened)


# --- commands --------------------------------------------------------------------------


def test_a_keyset_page_is_ascending_uncounted_and_bounded() -> None:
    key, method, params = keyset_command(DEAL_ITEM, "r0", after_id=100, below_id=2000)
    assert (key, method) == ("r0", "crm.item.list")
    assert params == {
        "entityTypeId": 2,
        "select": DEAL_ITEM.select,
        "order": {"id": "ASC"},
        "filter": {">id": 100, "<id": 2000},
        "start": -1,
    }


def test_the_legacy_keyset_speaks_upper_case_and_takes_no_entity_type() -> None:
    _, method, params = keyset_command(
        DEAL_LEGACY, "r0", after_id=0, updated_since="2026-09-01T00:00:00+00:00", ids_only=True
    )
    assert method == "crm.deal.list"
    assert "entityTypeId" not in params
    assert params["filter"] == {">ID": 0, ">=DATE_MODIFY": "2026-09-01T00:00:00+00:00"}
    assert params["select"] == ["ID"] and params["order"] == {"ID": "ASC"}


def test_an_empty_keyset_range_is_refused() -> None:
    with pytest.raises(ValueError):
        keyset_command(DEAL_ITEM, "r0", after_id=10, below_id=11)


def test_the_high_id_probe_never_counts() -> None:
    _, _, params = high_id_command(LEAD_ITEM, "hi")
    assert params["order"] == {"id": "DESC"} and params["start"] == -1 and params["filter"] == {}


def test_ids_command_bounds() -> None:
    _, _, params = ids_command(DEAL_ITEM, "i0", [5, 3, 5])
    assert params["filter"] == {"@id": [3, 5]}
    with pytest.raises(ValueError):
        ids_command(DEAL_ITEM, "i0", [])
    with pytest.raises(ValueError):
        ids_command(DEAL_ITEM, "i0", range(1, 52))


def test_a_range_count_is_the_only_counted_list() -> None:
    _, _, params = range_count_command(DEAL_ITEM, "c0", lo=10, hi=2010)
    assert params["start"] == 0 and params["filter"] == {">=id": 10, "<id": 2010}
    with pytest.raises(ValueError):
        range_count_command(DEAL_ITEM, "c0", lo=10, hi=10)


def test_get_command() -> None:
    assert get_command(1, "g5", 5) == ("g5", "crm.item.get", {"entityTypeId": 1, "id": 5})


# --- parsing ---------------------------------------------------------------------------


def test_a_deal_parses_from_the_shape_a_real_portal_answered() -> None:
    raw = deal_row(
        41, assignedById="7", closed="Y", opportunity="1500.005", leadId=0, companyId="0",
        contactIds=["12", 12, 0, "x"], categoryId=0, stageSemanticId="S",
    )
    row = parse_item(raw, DEAL_ITEM, utm_max_chars=120)
    assert isinstance(row, ItemRow)
    assert (row.entity_type_id, row.id, row.category_id) == (2, 41, 0)
    assert row.assigned_by_id == 7 and row.closed is True and row.stage_semantic == "S"
    assert row.opportunity == Decimal("1500.01"), "half-up to cents, as /utm rounds"
    assert row.lead_id is None and row.company_id is None, "0 means none, not record 0"
    assert row.contact_ids == (12,)
    assert row.created_time == dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))


def test_a_lead_falls_back_to_its_single_contact() -> None:
    raw = {"id": "9", "stageId": "NEW", "contactId": "33", "createdTime": "2026-09-01T10:00:00+05:00"}
    row = parse_item(raw, LEAD_ITEM, utm_max_chars=120)
    assert isinstance(row, ItemRow)
    assert row.contact_ids == (33,) and row.closed is None and row.opportunity is None
    assert row.category_id is None


def test_a_legacy_deal_parses_upper_case() -> None:
    raw = {
        "ID": "8",
        "CATEGORY_ID": "3",
        "STAGE_ID": "C3:WON",
        "STAGE_SEMANTIC_ID": "S",
        "CLOSED": "Y",
        "OPPORTUNITY": "10",
        "CURRENCY_ID": "USD",
        "CONTACT_ID": "5",
        "DATE_MODIFY": "2026-09-01T10:00:00+03:00",
    }
    row = parse_item(raw, DEAL_LEGACY, utm_max_chars=120)
    assert isinstance(row, ItemRow)
    assert (row.id, row.category_id, row.stage_id, row.closed) == (8, 3, "C3:WON", True)
    assert row.contact_ids == (5,) and row.opportunity == Decimal("10.00")


@pytest.mark.parametrize("raw", [None, "row", {"id": 0}, {"id": "abc"}, {}])
def test_a_row_without_a_usable_id_is_refused(raw: Any) -> None:
    assert isinstance(parse_item(raw, DEAL_ITEM, utm_max_chars=120), str)


def test_a_timestamp_without_an_offset_is_not_guessed() -> None:
    assert parse_timestamp("2026-09-01T10:00:00") is None
    assert parse_timestamp("not a date") is None
    assert parse_timestamp("2026-09-01T10:00:00+03:00") is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10.005", Decimal("10.01")), (15, Decimal("15.00")), ("", None), ("abc", None), (True, None),
     (float("nan"), None), (None, None)],
)
def test_money(raw: Any, expected: Decimal | None) -> None:
    assert parse_money(raw) == expected


def test_utm_is_normalised_exactly_as_the_utm_report_normalises_it() -> None:
    raw = deal_row(1, utmSource="  google  ", utmMedium=None, utmCampaign="x" * 200, utmTerm="\t")
    row = parse_item(raw, DEAL_ITEM, utm_max_chars=120)
    assert isinstance(row, ItemRow)
    assert row.utm == utm_dialects.utm_values(raw, utm_dialects.DEAL_ITEM, max_chars=120)
    assert row.utm[0] == "google" and len(row.utm[2]) == 120 and row.utm[4] == ""


def test_page_shapes() -> None:
    assert page_rows({"items": [{"id": 1}]}, DEAL_ITEM) == [{"id": 1}]
    assert page_rows([{"id": 1}], DEAL_ITEM) is None, "a universal answer must wrap its rows"
    assert page_rows({"items": "x"}, DEAL_ITEM) is None
    assert page_rows([{"ID": 1}], DEAL_LEGACY) == [{"ID": 1}]
    assert page_rows({"0": {"ID": 1}}, DEAL_LEGACY) == [{"ID": 1}]
    assert page_rows("x", DEAL_LEGACY) is None


def test_every_dialect_is_checked_at_import() -> None:
    for dialect in DIALECTS:
        _check_minimal(dialect)


# --- the error codes the mirror branches on ---------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NOT_FOUND", NotFound),
        ("INVALID_ARG_VALUE", InvalidArgValue),
        ("ENTITY_TYPE_NOT_SUPPORTED", EntityTypeNotSupported),
        ("allowed_only_intranet_user", IntranetUserOnly),
    ],
)
def test_the_mirror_error_codes_have_types(code: str, expected: type) -> None:
    assert isinstance(classify(code), expected)
    assert not isinstance(classify(code), UnknownBitrixError)


def test_the_admin_mode_fallback_still_retries_what_it_retried_before() -> None:
    """NOT_FOUND and INVALID_ARG_VALUE used to be UnknownBitrixError, which it retries."""
    assert NotFound in users._ADMIN_MODE_RETRYABLE
    assert InvalidArgValue in users._ADMIN_MODE_RETRYABLE
