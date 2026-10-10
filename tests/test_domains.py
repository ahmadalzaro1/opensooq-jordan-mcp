import asyncio
import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pydantic import ValidationError

import diagnose
import export
import opensooq_client as oc
import server as s
import watch
from listing_quality import cohort_key, filter_quality, match_listing


def listing(domain="vehicle", ident="1", title=None, **fields):
    if domain == "vehicle":
        defaults = {
            "car_make": "Toyota",
            "car_model": "Prius",
            "car_trim": "Limited",
            "Car_Year": "2020",
            "Kilometers_Cars": "100000 - 109999",
            "Fuel": "Hybrid",
            "Transmission": "Automatic",
            "Cars_body_types": "Sedan",
            "ConditionUsed": "Used",
            "Engine Size": "1800 cc",
        }
        category, title = "Cars For Sale", title or "Toyota Prius 2020"
    else:
        defaults = {
            "Rooms": "3",
            "Bathrooms": "2",
            "Furnished_RealEstate": "Furnished",
            "RentalPeriod": "Month",
            "Floors": "0",
        }
        category, title = "Apartments for Rent", title or "شقة للايجار"
    defaults.update(fields)
    return oc.normalize_item(
        {
            "id": ident,
            "title": title,
            "cat2_label": category,
            "cat1_label": "Property For Rent" if domain == "property" else "Autos",
            "price_amount": 300,
            "price_currency_iso": "JOD",
            "city_label": "Amman",
            "nhood_label": "Abdoun",
            "highlightsObject": {
                k: [{"value": v, "label": str(v)}] for k, v in defaults.items() if v is not None
            },
            "cps": ["Area: ١٨٠ m²"] if domain == "property" else [],
        }
    )


def test_nested_api_attributes_preserve_keys_evidence_and_ranges():
    item = listing()
    spec = item["specifications"]
    assert spec["domain"] == "vehicle"
    assert spec["fields"]["year"] == 2020
    assert spec["fields"]["mileage_km"] == {"min": 100000, "max": 109999, "exact": False}
    assert spec["fields"]["fuel"] == "hybrid"
    assert spec["evidence"]["year"][0]["source"] == "highlightsObject"
    assert any(row["label"] == "Car_Year" for row in item["attributes"])
    assert "unknown" not in cohort_key(item)


def test_property_rental_units_zero_floor_and_source():
    spec = listing("property")["specifications"]
    fields = spec["fields"]
    assert fields["bedrooms"] == 3 and fields["bathrooms"] == 2
    assert fields["floor"] == 0 and fields["area_m2"] == 180
    assert fields["rental_period"] == "monthly"
    assert fields["transaction"] == "rent"
    assert spec["evidence"]["area_m2"][0]["source"] == "cps"


@pytest.mark.parametrize("period", ["Day", "Month", "Year", None])
def test_rent_periods_never_share_a_cohort(period):
    key = cohort_key(listing("property", RentalPeriod=period))
    assert key != cohort_key(listing("property", RentalPeriod="Week"))
    if period is None:
        assert (
            "unknown_rental_period"
            in listing("property", RentalPeriod=None)["specifications"]["warnings"]
        )
        assert "unknown" in key


@pytest.mark.parametrize(
    "value,expected",
    [
        ("٥٠٬٠٠٠ - ٥٩٬٩٩٩", {"min": 50000, "max": 59999, "exact": False}),
        ("50000 miles", {"min": 80467.2, "max": 80467.2, "exact": True}),
        ("100 - 50", None),
        ("unknown", None),
        ("-1", None),
    ],
)
def test_mileage_ranges_units_and_invalid_values(value, expected):
    assert listing(Kilometers_Cars=value)["specifications"]["fields"]["mileage_km"] == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"Car_Year": None},
        {"Fuel": None},
        {"Transmission": None},
        {"Kilometers_Cars": "49000 - 51000"},
    ],
)
def test_incomplete_vehicle_specs_abstain(changes):
    assert "unknown" in cohort_key(listing(**changes))


def test_structured_year_and_title_conflict_are_unknown_and_flagged():
    item = listing(title="Toyota Prius 2012", Car_Year="2010")
    spec = item["specifications"]
    assert spec["fields"]["year"] is None
    assert "year_title_mismatch" in spec["conflicts"]
    assert spec["evidence"]["year"][0]["value"] == "2010"
    assert filter_quality([item])[0] == []
    assert not match_listing("toyota prius 2012", item)


def test_structured_model_handles_title_typo_without_using_description():
    assert match_listing("toyota prius 2020", listing(title="ريوس 2020"))


def test_furnishing_conflict_is_visible_and_unknown():
    spec = listing(
        "property", title="شقة شبه مفروشة للايجار", Furnished_RealEstate="Not furnished"
    )["specifications"]
    assert spec["fields"]["furnished"] is None
    assert "furnished_title_mismatch" in spec["conflicts"]


def test_bare_surface_does_not_fabricate_area_units():
    item = oc.normalize_item(
        {
            "id": 1,
            "cat2_label": "Apartments for Rent",
            "highlightsObject": {"Surface": [{"value": 180}]},
        }
    )
    assert item["specifications"]["fields"]["area_m2"] is None


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"year": 2020}, True),
        ({"year": 2019}, False),
        ({"max_mileage_km": 109999}, True),
        ({"max_mileage_km": 105000}, False),
        ({"bedrooms": 3}, False),
        ({"neighborhood": "ABDOUN"}, True),
        ({"rental_period": "monthly"}, False),
    ],
)
def test_domain_filters_require_known_matching_fields(params, expected):
    kept, reasons = s._filter_domains([listing()], s.SearchInput(query="prius", **params))
    assert bool(kept) is expected
    assert bool(reasons) is not expected


def test_invalid_domain_filters_are_rejected():
    for params in [
        {"min_area_m2": 200, "max_area_m2": 100},
        {"max_mileage_km": float("inf")},
        {"bedrooms": -1},
        {"rental_period": "sometimes"},
        {"query_ar": "   "},
    ]:
        with pytest.raises(ValidationError):
            s.SearchInput(query="prius", **params)


def test_bilingual_budget_overlap_and_independent_pagination():
    calls = []

    async def search(query, page=1, **kwargs):
        calls.append((query, page))
        ids = ["1"] if page == 1 else ["2"] if query == "english" else ["3"]
        return [listing(ident=i) for i in ids], {"count": 100, "pages": 2}

    with patch.object(oc, "search_page", search):
        items, meta = asyncio.run(oc.search_bilingual(["english", "arabic"], budget=4))
    assert calls == [("english", 1), ("arabic", 1), ("english", 2), ("arabic", 2)]
    assert [item["id"] for item in items] == ["1", "2", "3"]
    assert items[0]["source_queries"] == ["english", "arabic"]
    assert meta["count"] is None and meta["page_requests"] == 4
    assert not meta["partial"]


def test_bilingual_budget_is_shared_not_per_language():
    mock = AsyncMock(
        side_effect=lambda *a, **k: ([listing(ident=str(mock.await_count))], {"pages": 50})
    )
    with patch.object(oc, "search_page", mock):
        _, meta = asyncio.run(oc.search_bilingual(["en", "ar"], budget=5))
    assert mock.await_count == 5 and meta["budget_exhausted"]


def test_bilingual_failure_keeps_success_and_marks_partial():
    async def search(query, **kwargs):
        if query == "ar":
            raise oc.OpenSooqError("sensitive details must not leak")
        return [listing()], {"count": 1, "pages": 1}

    with patch.object(oc, "search_page", search):
        items, meta = asyncio.run(oc.search_bilingual(["en", "ar"], budget=2))
    assert len(items) == 1 and meta["partial"]
    assert "sensitive" not in json.dumps(meta)
    with patch.object(oc, "search_page", AsyncMock(side_effect=oc.OpenSooqError("failure"))):
        with pytest.raises(oc.OpenSooqError):
            asyncio.run(oc.search_bilingual(["en", "ar"], budget=2))


def test_bilingual_search_uses_both_queries_and_filtered_sample():
    items = [listing(), listing(ident="2", Car_Year="2019", title="Toyota Prius 2019")]
    meta = {"queries": ["toyota prius", "تويوتا بريوس"], "count": None, "partial": False}
    with patch.object(oc, "search_bilingual", AsyncMock(return_value=(items, meta))) as mock:
        payload = json.loads(
            asyncio.run(
                s.opensooq_search_listings(
                    s.SearchInput(
                        query="toyota prius",
                        query_ar="تويوتا بريوس",
                        year=2020,
                        response_format="json",
                    )
                )
            )
        )
    assert mock.call_args.args[0] == meta["queries"]
    assert payload["count"] == 1 and payload["total"] is None
    assert payload["quality_rejections"]["year_mismatch_or_unknown"] == 1


def test_detail_enrichment_merges_attributes_and_recomputes_quality():
    original = oc.normalize_item({"id": 1, "title": "iPhone 15 Pro", "member_id": 7})
    details = {
        "full_description": "Used 256GB",
        "attributes": [{"label": "Condition", "value": "Used"}],
        "seller_profile": None,
    }
    with (
        patch.object(oc, "fetch_listing_by_id", AsyncMock(return_value=original)),
        patch.object(oc, "fetch_listing_page_details", AsyncMock(return_value=details)),
    ):
        result = json.loads(
            asyncio.run(
                s.opensooq_get_listing(s.GetListingInput(listing_id="1", response_format="json"))
            )
        )
    assert result["listing"]["quality"]["storage_gb"] == 256
    assert result["listing"]["quality"]["condition"] == "used"
    assert result["listing"]["attributes"][0]["source"] == "detail_page"
    assert original["quality"]["condition"] == "unknown"


def test_rental_summary_separates_cadence_and_abstains_on_unknown():
    items = []
    for period, prices in [("Day", [10] * 5), ("Month", [300] * 5), (None, [20] * 5)]:
        for price in prices:
            x = listing("property", ident=str(len(items)), RentalPeriod=period)
            x["price"] = price
            items.append(x)
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        summary = json.loads(
            asyncio.run(
                s.opensooq_market_summary(
                    s.MarketSummaryInput(query="شقة للايجار", group_by="rental_period")
                )
            )
        )
        deals = json.loads(
            asyncio.run(
                s.opensooq_find_deals(s.FindDealsInput(query="شقة للايجار", response_format="json"))
            )
        )
    assert {g["value"] for g in summary["groups"]} == {"daily", "monthly", "unknown"}
    assert len(summary["cohorts"]) == 3 and deals["deal_count"] == 0
    assert any(not row["baseline_usable"] for row in deals["baselines"])


def test_partial_price_sample_does_not_generate_deal():
    items = [listing(ident=str(i)) for i in range(5)]
    items[0]["price"] = 100
    with patch.object(oc, "search_bilingual", AsyncMock(return_value=(items, {"partial": True}))):
        payload = json.loads(
            asyncio.run(
                s.opensooq_find_deals(
                    s.FindDealsInput(
                        query="toyota prius", query_ar="تويوتا بريوس", response_format="json"
                    )
                )
            )
        )
    assert payload["deal_count"] == 0 and not payload["baselines"][0]["baseline_usable"]


def test_partial_summary_and_groups_do_not_claim_usable_baselines():
    items = [listing(ident=str(i)) for i in range(5)]
    with patch.object(oc, "search_bilingual", AsyncMock(return_value=(items, {"partial": True}))):
        result = json.loads(
            asyncio.run(
                s.opensooq_market_summary(
                    s.MarketSummaryInput(
                        query="toyota prius", query_ar="تويوتا بريوس", group_by="year"
                    )
                )
            )
        )
    assert not result["cohorts"][0]["baseline_usable"]
    assert not result["groups"][0]["cohorts"][0]["baseline_usable"]
    assert "partial_bilingual_sample" in result["warnings"]


def test_rental_contract_period_is_not_assumed_price_billing_period():
    item = listing("property", RentalPeriod="Year")
    assert item["specifications"]["fields"]["rental_period"] == "yearly"
    assert item["specifications"]["fields"]["price_period"] is None
    assert "unconfirmed_rent_price_period" in item["specifications"]["warnings"]
    assert "price_period=unknown" in cohort_key(item)
    explicit = listing("property", **{"rent billing period": "Month"})
    assert explicit["specifications"]["fields"]["price_period"] == "monthly"


def test_contradictory_structured_attribute_values_remain_unknown():
    item = oc.normalize_item(
        {
            "id": 1,
            "title": "Toyota Prius",
            "cat2_label": "Cars For Sale",
            "highlightsObject": {"Car_Year": [{"value": "2010"}, {"value": "2012"}]},
        }
    )
    assert item["specifications"]["fields"]["year"] is None
    assert item["specifications"]["conflicts"] == ["year"]
    assert len(item["specifications"]["evidence"]["year"]) == 2


def test_csv_export_refuses_overwrite_before_network(tmp_path):
    target = tmp_path / "report.csv"
    target.write_text("preserve")
    with (
        patch("sys.argv", ["export.py", "--query", "prius", "--output", str(target)]),
        patch.object(export, "collect", AsyncMock()) as collect,
    ):
        with pytest.raises(SystemExit) as error:
            export.main()
    assert error.value.code == 2 and target.read_text() == "preserve"
    collect.assert_not_awaited()


def test_watcher_abstains_on_partial_poll():
    args = SimpleNamespace(query="prius", query_ar="بريوس", pages=2, city=None)
    with patch.object(
        oc, "search_bilingual", AsyncMock(return_value=([listing()], {"partial": True}))
    ):
        with pytest.raises(oc.OpenSooqError):
            asyncio.run(watch.find_hits(args))


def test_local_diagnostics_do_not_request_network_or_expose_credentials():
    with patch.object(oc, "raw_search", AsyncMock()) as mock:
        result = asyncio.run(diagnose.diagnose())
    mock.assert_not_awaited()
    assert result["status"] == "not_probed"
    assert set(result["requests"]) == {
        "requests",
        "errors",
        "last_status",
        "last_latency_ms",
        "last_error_type",
    }


def test_diagnostics_reports_block_without_error_body():
    response = httpx.Response(403, request=httpx.Request("POST", "https://example.com"))
    with patch.object(
        oc,
        "raw_search",
        AsyncMock(
            side_effect=httpx.HTTPStatusError("secret", request=response.request, response=response)
        ),
    ):
        result = asyncio.run(diagnose.diagnose(True))
    assert result["status"] == "blocked" and "secret" not in json.dumps(result)


def test_nonretryable_transport_error_is_counted_without_secret_url():
    client = SimpleNamespace(request=AsyncMock(side_effect=httpx.ReadError("sensitive URL")))
    with (
        patch.dict(
            oc._telemetry, {"requests": 0, "errors": 0, "last_status": 200, "last_error_type": None}
        ),
        patch.object(oc, "_last_request_at", 0),
    ):
        with pytest.raises(httpx.ReadError):
            asyncio.run(oc._request(client, "GET", "https://example.com"))
        status = oc.request_status()
        assert status["requests"] == 1 and status["errors"] == 1
        assert status["last_status"] is None and status["last_error_type"] == "ReadError"
        assert "sensitive" not in json.dumps(status)
        client.request.assert_awaited_once()


@pytest.mark.parametrize("text", ["=HYPERLINK(1)", "+SUM(1)", "-2+3", "@SUM(1)", " \t=1", "\n+1"])
def test_csv_neutralizes_formulas(text):
    assert export.csv_cell(text).startswith("'")


def test_csv_round_trip_arabic_quotes_units_and_provenance():
    item = listing("property", title='شقة، "مفروشة"\nللإيجار')
    payload = {"listings": [item], "search": {"queries": ["شقة"]}}
    rows = list(csv.DictReader(io.StringIO(export.csv_text(payload, "2026-10-10T10:00:00Z"))))
    assert rows[0]["title"] == item["title"]
    assert rows[0]["currency"] == "JOD" and rows[0]["observed_at"].endswith("Z")
    assert json.loads(rows[0]["specifications"])["fields"]["area_m2"] == 180
    assert json.loads(rows[0]["source_queries"]) == ["شقة"]
