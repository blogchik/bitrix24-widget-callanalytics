"""The UTM report's pure half: command shapes, the fold, and the bucket ladder (§4.13).

No HTTP and no database here. What this module protects is the set of mistakes that produce
a report which is *plausible and wrong* - the only failure mode of this feature that nobody
downstream can catch:

* **A period filter that is silently ignored.** One flat leg is not safer than §4.12's
  nested group, it is more dangerous: an ignored `>=createdTime` does not widen the union,
  it DELETES the period, and the result is a lifetime report with a one-month date range
  printed above it and every cell internally consistent. `honour_verdict`'s *inconclusive*
  case matters as much as its negative one, because caching an untested verdict pins it on
  the whole portal for an hour.
* **A bucket ladder that loses a record.** Rungs 2 and 3 relabel; they must never drop. The
  `sum(rows) == totals` assertions below are the whole justification for calling this
  "relabelling, not truncation", and without them the distinction is only a comment.
* **A collapsed dimension changing another dimension's numbers.** Lifting `utm_term` out of
  the composite key must leave `utm_source`'s marginal bit-identical. That is a property of
  a projection rather than a hope, and it is asserted rather than assumed.
* **Money arriving as a float.** Every aggregate is a sum of per-record quantised cents, so
  the row strings, the facet strings and the total string must agree digit for digit
  whichever way they are added up. A `Decimal(float)` anywhere makes them disagree in the
  fifteenth digit - the "a row does not add up to its own total" defect §4.12 forbids,
  arriving through a channel nobody would think to look at.
* **A real tag colliding with a reserved bucket.** A portal may genuinely use `other` as a
  `utm_content`, and it must not merge into the residue row.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.bitrix.utm import (
    BUCKET_COLLAPSED,
    BUCKET_NONE,
    BUCKET_OTHER,
    DEAL_ITEM,
    DEAL_LEGACY,
    DIMENSIONS,
    KIND_DEAL,
    KIND_LEAD,
    LEAD_ITEM,
    LEAD_LEGACY,
    EntityDialect,
    _select,
    fields_command,
    honour_probe_commands,
    honour_verdict,
    legacy_of,
    list_page_commands,
    page_key,
    period_filter,
    read_money,
    utm_values,
)
from app.services import utm_stats
from app.services.utm_stats import (
    _Acc,
    _amounts,
    _combinations,
    _facet_wire,
    _facets,
    _fold,
    _remaps,
    _Scan,
)

TASHKENT = ZoneInfo("Asia/Tashkent")
START = "2026-06-01T00:00:00+05:00"
END = "2026-07-01T00:00:00+05:00"


# --- helpers ------------------------------------------------------------------------------


def _with(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Swap the module-level `settings` reference; the model itself is frozen.

    `test_recording.py` already does exactly this for `RECORDING_MODE`. Patching the
    attribute rather than the object is also the honest shape: production changes these by
    restarting with a different environment, never by mutating a live Settings.
    """
    from app.config import settings as real

    monkeypatch.setattr(utm_stats, "settings", real.model_copy(update=overrides))


def lead_row(
    identifier: int,
    *,
    source: str = "google",
    medium: str = "cpc",
    campaign: str = "",
    content: str = "",
    term: str = "",
    semantic: str = "P",
    created: str = "2026-06-10T12:00:00+05:00",
) -> dict[str, Any]:
    return {
        "id": identifier,
        "createdTime": created,
        "stageSemanticId": semantic,
        "assignedById": 101,
        "utmSource": source,
        "utmMedium": medium,
        "utmCampaign": campaign,
        "utmContent": content,
        "utmTerm": term,
    }


def deal_row(
    identifier: int,
    *,
    source: str = "google",
    medium: str = "cpc",
    campaign: str = "",
    content: str = "",
    term: str = "",
    semantic: str = "P",
    created: str = "2026-06-10T12:00:00+05:00",
    amount: str | None = "100.00",
    currency: str = "UZS",
    account: bool = True,
    lead_id: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": identifier,
        "createdTime": created,
        "stageSemanticId": semantic,
        "assignedById": 101,
        "utmSource": source,
        "utmMedium": medium,
        "utmCampaign": campaign,
        "utmContent": content,
        "utmTerm": term,
    }
    if lead_id is not None:
        row["leadId"] = lead_id
    if amount is not None:
        if account:
            row["opportunityAccount"] = amount
            row["accountCurrencyId"] = currency
        row["opportunity"] = amount
        row["currencyId"] = currency
    return row


def scan_of(
    leads: list[dict[str, Any]] | None = None,
    deals: list[dict[str, Any]] | None = None,
) -> _Scan:
    """Fold rows into an accumulator exactly as the live scan does."""
    scan = _Scan()
    seen: dict[str, set[int]] = {KIND_LEAD: set(), KIND_DEAL: set()}
    if leads:
        _fold(leads, dialect=LEAD_ITEM, scan=scan, seen=seen, zone=TASHKENT)
    if deals:
        _fold(deals, dialect=DEAL_ITEM, scan=scan, seen=seen, zone=TASHKENT)
    return scan


def totals_of(scan: _Scan) -> _Acc:
    out = _Acc()
    for acc in scan.raw.values():
        out.merge(acc)
    return out


def summed(rows: dict[tuple[str, ...], _Acc]) -> _Acc:
    out = _Acc()
    for acc in rows.values():
        out.merge(acc)
    return out


def assert_same(left: _Acc, right: _Acc) -> None:
    assert left.leads.wire() == right.leads.wire()
    assert left.deals.wire() == right.deals.wire()
    assert left.amount_account == right.amount_account
    assert left.amount_native == right.amount_native
    assert left.deals_from_lead == right.deals_from_lead


# --- command shapes -------------------------------------------------------------------------


def test_period_filter_is_one_flat_leg_with_no_logic_grouping() -> None:
    """Decision 3 in one line, and the reason the legacy dialects cost nothing extra.

    A `logic` member anywhere here would mean the fallback needs three deduped selections
    again, which is the whole expense §4.12 carries and this page does not.
    """
    for dialect in (LEAD_ITEM, DEAL_ITEM, LEAD_LEGACY, DEAL_LEGACY):
        filter_ = period_filter(dialect, start_iso=START, end_iso=END)
        assert set(filter_) == {f">={dialect.created}", f"<{dialect.created}"}
        assert "logic" not in repr(filter_)
        # Half-open, and both bounds carry an offset: a bare date is read in the PORTAL's
        # zone while the app computes the period in the viewer's.
        assert filter_[f">={dialect.created}"] == START
        assert filter_[f"<{dialect.created}"] == END


def test_lead_selection_never_asks_for_money_or_customer_content() -> None:
    """§6, and the double-counting decision.

    A lead carries `OPPORTUNITY` and it is an estimate a salesperson typed; summing it
    beside deal amounts would count every converted lead twice. It is absent from the
    dialect entirely rather than filtered out later, so there is no way to reintroduce it by
    accident.
    """
    names = _select(LEAD_ITEM)
    assert "opportunity" not in names
    assert "opportunityAccount" not in names
    assert "currencyId" not in names
    assert "leadId" not in names
    for forbidden in ("title", "TITLE", "contactId", "companyId", "comments"):
        assert forbidden not in names
    assert not any(name.startswith("UF_") or name.startswith("ufCrm") for name in names)
    # The five tags, the four core fields, and not one more.
    assert len(names) == 9


def test_deal_selection_names_both_money_pairs_and_the_lead_link() -> None:
    """Both denominations, because the report picks ONE source for the whole report."""
    names = _select(DEAL_ITEM)
    assert {"opportunityAccount", "accountCurrencyId", "opportunity", "currencyId"} <= set(names)
    assert "leadId" in names
    assert len(names) == 14
    assert len(set(names)) == len(names)


def test_entity_type_id_rides_only_on_the_universal_dialects() -> None:
    """`crm.lead.list` names its entity in the METHOD; sending `entityTypeId` would be noise."""
    for dialect in (LEAD_ITEM, DEAL_ITEM):
        (_, _, params), = list_page_commands(
            dialect, filter_=period_filter(dialect, start_iso=START, end_iso=END), starts=[0]
        )
        assert params["entityTypeId"] == dialect.entity_type_id
    for dialect in (LEAD_LEGACY, DEAL_LEGACY):
        (_, _, params), = list_page_commands(
            dialect, filter_=period_filter(dialect, start_iso=START, end_iso=END), starts=[0]
        )
        assert "entityTypeId" not in params


def test_the_operator_pin_lands_in_the_filter_and_never_in_the_select() -> None:
    """§4.7 server-side, on BOTH legs.

    An `own` viewer whose deals were pinned but whose leads were not would get the whole
    company's leads in the denominator of their own conversion rate - a number that is
    wrong in the direction that flatters nobody and explains nothing.
    """
    for dialect in (LEAD_ITEM, DEAL_ITEM):
        (_, _, params), = list_page_commands(
            dialect,
            filter_=period_filter(dialect, start_iso=START, end_iso=END),
            starts=[0],
            assigned_to=[101],
        )
        assert params["filter"][f"@{dialect.assigned_by_id}"] == [101]
        assert dialect.assigned_by_id in params["select"]
        assert not any(name.startswith("@") for name in params["select"])


def test_page_keys_never_collide_between_the_two_entity_streams() -> None:
    """Both selections start at offset 0 in the same batch.

    `BitrixClient.batch` rejects a duplicate key outright rather than overwriting - but only
    after the caller has already built the report wrong in their head.
    """
    keys = [page_key(kind, start) for kind in (KIND_LEAD, KIND_DEAL) for start in (0, 50, 100)]
    assert len(set(keys)) == len(keys)
    assert page_key(KIND_LEAD, 0) != page_key(KIND_DEAL, 0)
    with pytest.raises(ValueError, match="multiple"):
        page_key(KIND_LEAD, 17)
    with pytest.raises(ValueError, match="unknown entity kind"):
        page_key("contact", 0)


def test_fields_command_asks_the_universal_and_the_legacy_map_alike() -> None:
    """The legacy probe is not a formality: it is where `UTM_SOURCE` is actually documented."""
    key, method, params = fields_command(LEAD_ITEM)
    assert method == "crm.item.fields"
    assert params == {"entityTypeId": 1}
    key2, method2, params2 = fields_command(legacy_of(LEAD_ITEM))
    assert method2 == "crm.lead.fields"
    assert params2 == {}
    assert key == key2  # one entity, one slot in the batch, whichever family answered
    assert fields_command(legacy_of(DEAL_ITEM))[1] == "crm.deal.fields"


def test_the_honour_probe_is_one_command_and_never_an_unfiltered_scan() -> None:
    """The regression that took a production portal down, pinned as a shape.

    An UNFILTERED `crm.item.list` makes Bitrix24 count the whole lead table and the whole
    deal table. It is independent of the period, so narrowing to one day does not make it
    cheaper; it repeats on every retry, because a failed report caches nothing; and it spends
    the `crm.item.list` operating budget the deal page shares. The page answered
    `operation_time_limit` for as long as the window took to drain.

    So: one command, and its filter is never empty.
    """
    commands = honour_probe_commands(DEAL_ITEM)
    assert len(commands) == 1
    (_, _, future), = commands
    assert future["filter"] == {">=createdTime": "2999-01-01T00:00:00+00:00"}
    assert future["filter"] != {}
    assert "logic" not in repr(future)
    for dialect in (LEAD_ITEM, DEAL_ITEM, LEAD_LEGACY, DEAL_LEGACY):
        for _, _, params in honour_probe_commands(dialect):
            assert params["filter"], "an unfiltered probe counts the whole table"


def test_honour_verdict_detects_a_dropped_filter_without_a_baseline() -> None:
    """A non-zero answer to "created at or past the year 2999" needs no corroboration.

    No record can be. So `False` is proof on its own, which is why the unfiltered baseline
    could go: it was only ever evidence about whether the verdict may be CACHED, and the
    caller now takes that from the scan it runs anyway.
    """
    assert honour_verdict(future=3) is False
    assert honour_verdict(future=0) is True
    assert honour_verdict(future=None) is None


# --- normalisation -------------------------------------------------------------------------


def test_absent_null_and_whitespace_are_one_bucket() -> None:
    """Three indistinguishable ways to store the same business fact: this record has no tag."""
    absent = utm_values({}, LEAD_ITEM, max_chars=120)
    explicit_null = utm_values({"utmSource": None}, LEAD_ITEM, max_chars=120)
    blank = utm_values({"utmSource": "   "}, LEAD_ITEM, max_chars=120)
    assert absent == explicit_null == blank
    assert all(value == BUCKET_NONE for value in absent)
    assert len(absent) == len(DIMENSIONS)


def test_a_long_tag_is_cut_before_it_becomes_part_of_a_key() -> None:
    """Two values differing only past the cut must MERGE, not render as two identical rows."""
    long_a = "x" * 200 + "a"
    long_b = "x" * 200 + "b"
    key_a = utm_values({"utmSource": long_a}, LEAD_ITEM, max_chars=16)
    key_b = utm_values({"utmSource": long_b}, LEAD_ITEM, max_chars=16)
    assert key_a == key_b
    assert key_a[0] == "x" * 16


def test_case_and_spelling_are_preserved() -> None:
    """v1 states the behaviour on the page rather than guessing which spelling to display."""
    assert utm_values({"utmSource": " Google "}, LEAD_ITEM, max_chars=120)[0] == "Google"
    assert utm_values({"utmSource": "google"}, LEAD_ITEM, max_chars=120)[0] == "google"


def test_money_is_read_in_both_denominations_and_never_as_a_float() -> None:
    money = read_money(
        {
            "opportunityAccount": "1200.5",
            "accountCurrencyId": "UZS",
            "opportunity": 1000.1,
            "currencyId": "USD",
        },
        DEAL_ITEM,
    )
    assert money.account == Decimal("1200.5")
    assert money.account_currency == "UZS"
    assert money.native == Decimal("1000.1")  # via str(), so no binary-float tail
    assert money.native_currency == "USD"
    # A lead dialect names neither field, so a lead can never contribute to a money column.
    assert read_money({"opportunity": "500"}, LEAD_ITEM).native is None


@pytest.mark.parametrize("raw", ["", "   ", None, True, "not-a-number", float("inf")])
def test_an_unusable_amount_is_absent_rather_than_an_exception(raw: Any) -> None:
    """A report must not 500 because one portal has one strange row."""
    assert read_money({"opportunityAccount": raw}, DEAL_ITEM).account is None


# --- the fold ------------------------------------------------------------------------------


def test_a_lead_and_a_deal_sharing_a_tag_land_on_one_row() -> None:
    """The whole point of the unified funnel: one row, both sides of it."""
    scan = scan_of([lead_row(1)], [deal_row(2, lead_id=1)])
    assert len(scan.raw) == 1
    acc = next(iter(scan.raw.values()))
    assert acc.leads.total == 1
    assert acc.deals.total == 1
    assert acc.deals_from_lead == 1


def test_outcomes_always_add_up_to_the_entity_total() -> None:
    """`won + lost + in_progress == total`, on every row and in the grand total."""
    scan = scan_of(
        [lead_row(i, semantic=s) for i, s in enumerate(["S", "F", "P", "", "wat"], start=1)],
        [deal_row(i, semantic=s) for i, s in enumerate(["S", "F", None], start=10)],
    )
    for acc in scan.raw.values():
        for measures in (acc.leads, acc.deals):
            assert measures.won + measures.lost + measures.in_progress == measures.total
    totals = totals_of(scan)
    # Anything that is not S or F is in progress - a value nobody has thought of must never
    # be counted as a won or a lost record.
    assert totals.leads.wire() == {"total": 5, "in_progress": 3, "won": 1, "lost": 1}
    assert totals.deals.wire() == {"total": 3, "in_progress": 1, "won": 1, "lost": 1}


def test_a_repeated_id_is_folded_once() -> None:
    """Speculative page packing can return the same record twice; the count must not."""
    scan = scan_of([lead_row(1), lead_row(1), lead_row(2)])
    assert totals_of(scan).leads.total == 2
    assert scan.folded[KIND_LEAD] == 2


def test_days_are_bucketed_in_the_viewer_zone() -> None:
    """A record created at 02:00 Tashkent on the 11th is NOT the 10th, though UTC says so."""
    scan = scan_of([lead_row(1, created="2026-06-10T21:30:00+00:00")])
    assert list(scan.days) == [dt.date(2026, 6, 11)]
    utc_scan = _Scan()
    _fold(
        [lead_row(1, created="2026-06-10T21:30:00+00:00")],
        dialect=LEAD_ITEM,
        scan=utc_scan,
        seen={KIND_LEAD: set(), KIND_DEAL: set()},
        zone=ZoneInfo("UTC"),
    )
    assert list(utc_scan.days) == [dt.date(2026, 6, 10)]


def test_tagged_rows_counts_records_carrying_any_tag_at_all() -> None:
    """The one failure `crm.item.fields` cannot detect, reported rather than guessed at."""
    scan = scan_of([lead_row(1), lead_row(2, source="", medium="")])
    assert scan.tagged[KIND_LEAD] == 1
    assert scan.folded[KIND_LEAD] == 2


# --- the bucket ladder ------------------------------------------------------------------------


def test_facets_are_exact_marginals_and_sum_to_the_totals() -> None:
    """Computed BEFORE bucketing, which is what keeps the filter option lists honest."""
    scan = scan_of(
        [lead_row(i, source=f"s{i % 3}", medium=f"m{i % 2}") for i in range(1, 13)],
        [deal_row(i, source=f"s{i % 3}", medium=f"m{i % 2}") for i in range(20, 26)],
    )
    facets = _facets(scan)
    totals = totals_of(scan)
    for name in DIMENSIONS:
        assert_same(summed({(v,): acc for v, acc in facets[name].items()}), totals)


def test_the_value_cap_relabels_and_never_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rung 2. `sum(rows) == totals` exactly, which is the whole claim of "not truncation"."""
    _with(monkeypatch, utm_value_cap=2)
    scan = scan_of([lead_row(i, source=f"s{i}") for i in range(1, 8)])
    totals = totals_of(scan)

    facets = _facets(scan)
    remaps = _remaps(facets)
    rows, collapsed = _combinations(scan, dimensions=DIMENSIONS, remaps=remaps)

    assert collapsed == ()
    assert_same(summed(rows), totals)
    sources = {key[0] for key in rows}
    assert BUCKET_OTHER in sources
    assert len(sources) == 3  # two kept plus the residue


def test_a_real_tag_spelled_like_a_bucket_does_not_merge_into_the_residue() -> None:
    """A portal may genuinely use `other` as a tag. The sentinel is why that is survivable."""
    scan = scan_of([lead_row(1, source="other"), lead_row(2, source="google")])
    facets = _facets(scan)
    assert set(facets["utm_source"]) == {"other", "google"}
    assert BUCKET_OTHER not in facets["utm_source"]
    assert BUCKET_OTHER != "other"


def test_collapsing_a_dimension_leaves_every_other_marginal_bit_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rung 3, and the property that makes it safe.

    Lifting `utm_term` out of the composite key cannot change `utm_source`'s numbers - that
    is what a projection IS - so a page that lost its finest control still prints exactly
    the same totals for the ones it kept.
    """
    _with(monkeypatch, utm_combination_cap=3)
    scan = scan_of([lead_row(i, source=f"s{i % 2}", term=f"t{i}") for i in range(1, 12)])
    totals = totals_of(scan)

    facets = _facets(scan)
    before = {value: acc.weight() for value, acc in facets["utm_source"].items()}

    remaps = _remaps(facets)
    rows, collapsed = _combinations(scan, dimensions=DIMENSIONS, remaps=remaps)

    assert "utm_term" in collapsed
    assert all(key[DIMENSIONS.index("utm_term")] == BUCKET_COLLAPSED for key in rows)
    assert_same(summed(rows), totals)

    after: dict[str, int] = {}
    index = DIMENSIONS.index("utm_source")
    for key, acc in rows.items():
        after[key[index]] = after.get(key[index], 0) + acc.weight()
    assert after == before


def test_dimensions_narrow_the_fold_without_losing_a_record() -> None:
    """The `dimensions` parameter is a response-size control, never a cost control."""
    scan = scan_of([lead_row(i, source=f"s{i % 2}", campaign=f"c{i}") for i in range(1, 9)])
    totals = totals_of(scan)
    facets = _facets(scan)
    remaps = _remaps(facets)

    wide, _ = _combinations(scan, dimensions=DIMENSIONS, remaps=remaps)
    narrow, _ = _combinations(scan, dimensions=("utm_source",), remaps=remaps)

    assert len(narrow) == 2
    assert len(wide) > len(narrow)
    assert_same(summed(wide), totals)
    assert_same(summed(narrow), totals)


def test_a_facet_wire_row_includes_the_residue_so_it_still_sums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residue is emitted as a real row rather than omitted.

    `sum(values) == totals` must hold for every dimension independently - the invariant that
    catches an off-by-one in the ladder where nothing else would.
    """
    _with(monkeypatch, utm_value_cap=2)
    scan = scan_of([lead_row(i, source=f"s{i}") for i in range(1, 8)])
    facets = _facets(scan)
    wire = _facet_wire("utm_source", facets["utm_source"], source="account", selected=True, collapsed=False)

    assert wire["distinct_total"] == 7
    assert wire["distinct_kept"] == 2
    assert wire["values"][-1]["value"] == BUCKET_OTHER
    assert sum(row["leads"]["total"] for row in wire["values"]) == 7


# --- money --------------------------------------------------------------------------------


def test_one_account_currency_is_trusted_and_sums_exactly() -> None:
    """Row strings, facet strings and the total string agree digit for digit."""
    scan = scan_of(
        deals=[deal_row(i, source=f"s{i}", amount="10.005", currency="UZS") for i in range(1, 5)]
    )
    source, amounts = _amounts(scan)
    assert source == "account"
    assert amounts["trusted"] is True
    assert amounts["currency"] == "UZS"
    totals = totals_of(scan)
    # 10.005 quantises to 10.01 per RECORD, so four records are exactly 40.04 - and the
    # browser adding the four row strings gets the same number.
    assert totals.amount_account == Decimal("40.04")


def test_more_than_one_account_currency_is_not_trusted() -> None:
    """The account currency is a PORTAL setting; two values mean the field is not what we think.

    The page then hides the money rather than printing a sum across currencies nobody
    converted: a missing column a sentence explains is recoverable, a wrong total a
    supervisor acts on is not.
    """
    scan = scan_of(
        deals=[
            deal_row(1, amount="100", currency="UZS"),
            deal_row(2, amount="100", currency="USD"),
        ]
    )
    _, amounts = _amounts(scan)
    assert amounts["trusted"] is False
    assert amounts["currencies"] == ["USD", "UZS"]


def test_a_build_without_the_account_pair_falls_back_to_the_native_column() -> None:
    """And the fallback is chosen for the WHOLE report, never per row."""
    scan = scan_of(
        deals=[
            deal_row(1, amount="100", currency="UZS", account=False),
            deal_row(2, amount="250", currency="UZS", account=False),
        ]
    )
    source, amounts = _amounts(scan)
    assert source == "native"
    assert amounts["trusted"] is True
    assert totals_of(scan).amount_native == Decimal("350.00")


def test_one_row_missing_the_account_pair_demotes_the_whole_report() -> None:
    """Mixing two denominations inside one column is the failure this prevents."""
    scan = scan_of(
        deals=[
            deal_row(1, amount="100", currency="UZS", account=True),
            deal_row(2, amount="250", currency="UZS", account=False),
        ]
    )
    source, _ = _amounts(scan)
    assert source == "native"


def test_a_deal_with_no_amount_at_all_is_counted_and_reported() -> None:
    """It still belongs to its UTM row; the page just says how many carried no number."""
    scan = scan_of(deals=[deal_row(1, amount=None), deal_row(2, amount="50")])
    assert totals_of(scan).deals.total == 2
    assert scan.rows_without_amount == 1


def test_deals_from_lead_never_exceeds_the_deal_count() -> None:
    """The cheap stand-in for the join §4.13 rejects - and a bounded one."""
    scan = scan_of(deals=[deal_row(1, lead_id=5), deal_row(2), deal_row(3, lead_id=0)])
    totals = totals_of(scan)
    assert totals.deals_from_lead == 1
    assert totals.deals_from_lead <= totals.deals.total


def test_the_legacy_dialect_reads_the_same_report_from_upper_case_rows() -> None:
    """`crm.lead.list` says `STATUS_SEMANTIC_ID` where `crm.deal.list` says `STAGE_*`.

    The one place the two entities genuinely disagree, and the reason the dialects are a
    table of constants rather than one shape with an entity id swapped in.
    """
    row = {
        "ID": "7",
        "DATE_CREATE": "2026-06-10T12:00:00+05:00",
        "STATUS_SEMANTIC_ID": "S",
        "ASSIGNED_BY_ID": "101",
        "UTM_SOURCE": "yandex",
        "UTM_MEDIUM": "cpc",
    }
    scan = _Scan()
    _fold(
        [row],
        dialect=LEAD_LEGACY,
        scan=scan,
        seen={KIND_LEAD: set(), KIND_DEAL: set()},
        zone=TASHKENT,
    )
    ((key, acc),) = scan.raw.items()
    assert key[0] == "yandex"
    assert acc.leads.won == 1


def test_every_dialect_agrees_about_which_tags_it_can_read() -> None:
    """A dialect that named four tags would silently drop the fifth from every key."""
    for dialect in (LEAD_ITEM, DEAL_ITEM, LEAD_LEGACY, DEAL_LEGACY):
        assert isinstance(dialect, EntityDialect)
        assert set(dialect.utm) == set(DIMENSIONS)
        assert len(set(dialect.utm.values())) == len(DIMENSIONS)
