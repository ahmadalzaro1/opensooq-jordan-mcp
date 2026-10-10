import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

import install
import opensooq_client as oc
import server as s
import watch
from listing_quality import filter_quality, match_listing, quality
from local_state import atomic_json, exclusive_lock
from page_parser import extract_details


def item(title, price=300, ident="1", description="", **extra):
    return oc.normalize_item(
        {
            "id": ident,
            "title": title,
            "price_amount": price,
            "masked_description": description,
            "member_id": 7,
            "member_rating_count": "20",
            "member_rating_avg": "4.5",
            "verification_level_i": "1",
            **extra,
        }
    )


@pytest.mark.parametrize(
    "query,title,expected",
    [
        ("iphone 15 pro", "Samsung 15 Pro", False),
        ("iphone 15 pro", "ايفون 15 برو 256 جيجا", True),
        ("apple iphone 15 pro", "آيفون ١٥ برو", True),
        ("iphone 15 pro", "iPhone 15 Pro Max", False),
        ("iphone 15", "iPhone 15 Pro", False),
        ("iphone 15 pro max", "iPhone 15 Pro", False),
        ("iphone 15 pro 256GB", "ايفون 15 برو 256", True),
        ("iphone 15 pro 1TB", "ايفون 15 برو 1024 جيجا", True),
        ("samsung galaxy s24 ultra", "سامسونج اس 24 الترا", True),
        ("samsung galaxy s24 ultra", "Samsung Galaxy A24 Ultra", False),
        ("samsung galaxy s24 ultra", "Samsung Galaxy S23 Ultra", False),
        ("toyota prius 2020", "تويوتا بريوس 2020", True),
        ("toyota prius 2020", "Toyota Corolla 2020", False),
        ("professional camera", "camera", False),
        ("ps5", "بلايستيشن 5", True),
        ("iphone 15 used", "iPhone 15 like new", True),
        ("iphone 15 new", "iPhone 15 used like new", False),
    ],
)
def test_product_identity(query, title, expected):
    assert match_listing(query, item(title)) is expected


@pytest.mark.parametrize(
    "title,flag",
    [
        ("iPhone 15 Pro case", "accessory"),
        ("كفر ايفون 15 برو", "accessory"),
        ("iPhone 15 Pro installments 20 JOD", "payment_offer"),
        ("ايفون 15 برو دفعة اولى 50", "payment_offer"),
        ("ايفون 15 برو للبدل", "wanted_or_exchange"),
        ("iPhone 15 Pro broken", "damaged"),
        ("iPhone 15 Pro 128/256", "multiple_capacities"),
    ],
)
def test_risky_offers(title, flag):
    listing = item(title)
    assert flag in listing["quality"]["risk_flags"]
    assert filter_quality([listing])[0] == []
    assert filter_quality([listing], include_risky=True)[0] == [listing]


@pytest.mark.parametrize(
    "title,condition,storage",
    [
        ("ايفون 15 برو 256 مستعمل بحالة الجديد", "used", 256),
        ("iPhone 15 Pro 512GB new sealed", "new", 512),
        ("iPhone 15 Pro battery 85%", "unknown", None),
        ("ايفون 15 برو 128/256 جديد", "new", None),
        ("iPhone 15 Pro 1TB", "unknown", 1024),
    ],
)
def test_quality_extraction(title, condition, storage):
    result = quality(item(title))
    assert result["condition"] == condition
    assert result["storage_gb"] == storage


def test_condition_storage_filters_exclude_unknowns():
    items = [
        item("iPhone 15 Pro 256GB used"),
        item("iPhone 15 Pro"),
        item("iPhone 15 Pro 512GB new"),
    ]
    kept, reasons = filter_quality(items, condition="used", storage_gb=256)
    assert len(kept) == 1 and reasons["condition_mismatch"] == 2


def test_new_and_used_are_not_compared_as_deals():
    # Aggregate median would incorrectly flag every used phone as a deal against new phones.
    items = [item("iPhone 15 Pro 256GB used", 100, str(i)) for i in range(5)]
    items += [item("iPhone 15 Pro 256GB new", 500, str(i)) for i in range(5, 10)]
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        result = json.loads(
            asyncio.run(
                s.opensooq_find_deals(
                    s.FindDealsInput(query="iphone 15 pro", response_format="json")
                )
            )
        )
    assert result["deal_count"] == 0 and len(result["baselines"]) == 2
    assert all(row["baseline_usable"] for row in result["baselines"])


def test_small_cohort_does_not_produce_deal():
    items = [item("iPhone 15 Pro 256GB used", 100), item("iPhone 15 Pro 256GB used", 500, "2")]
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        result = json.loads(
            asyncio.run(
                s.opensooq_find_deals(
                    s.FindDealsInput(query="iphone 15 pro", response_format="json")
                )
            )
        )
    assert result["deal_count"] == 0 and result["baselines"][0]["baseline_usable"] is False


def test_same_cohort_deal_has_evidence():
    items = [
        item("iPhone 15 Pro 256GB used", price, str(i))
        for i, price in enumerate([100, 200, 200, 200, 200])
    ]
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        result = json.loads(
            asyncio.run(
                s.opensooq_find_deals(
                    s.FindDealsInput(query="iphone 15 pro", response_format="json")
                )
            )
        )
    assert result["deal_count"] == 1
    assert result["deals"][0]["median_price"] == 200 and result["deals"][0]["baseline_count"] == 5


def test_structured_page_parser_nested_owner_and_full_description():
    description = 'A full description with braces {inside} and "quotes". ' * 30
    obj = {
        "id": 123,
        "description": {"content": description},
        "ownerData": {
            "numberOfRatings": 11,
            "memberSince": "2020-01-01",
            "id": 7,
            "fullName": "Test seller",
            "ratingAvg": 4.2,
            "phoneDetails": {"secret": "excluded"},
            "memberLink": "/shop/test",
        },
        "attributes": [{"value": "Used", "key_type": "Condition", "label": "Condition"}],
    }
    # The listing is encoded as JSON inside a JSON string, split across Flight chunks.
    stream = "a:" + json.dumps({"payload": json.dumps(obj)}) + "\n"
    split = len(stream) // 2
    html = "".join(
        "<script>self.__next_f.push([1," + json.dumps(part) + "])</script>"
        for part in [stream[:split], stream[split:]]
    )
    result = extract_details(html, oc.BASE_URL, listing_id="123")
    assert result["full_description"] == description
    assert result["seller_profile"]["member_id"] == "7"
    assert result["attributes"][0]["value"] == "Used"
    assert "secret" not in json.dumps(result)


def test_next_data_fallback():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"listing": {"id": 1, "description": {"content": "Full text"}}})
        + "</script>"
    )
    assert extract_details(html, oc.BASE_URL)["full_description"] == "Full text"


def test_dates_unknown_iso_and_arabic():
    assert oc.parse_posted_date("not a date") is None
    assert oc.parse_posted_date(None) is None
    assert oc.parse_posted_date("2026-10-10T08:00:00Z").hour == 8
    assert oc.parse_posted_date("٥ دقائق") is not None
    assert item("iPhone 15")["postedDate"] is None


def test_noisy_types_do_not_crash_or_fabricate_verification():
    listing = item(
        "iPhone 15",
        member_rating_avg="invalid",
        member_rating_count="invalid",
        verification_level_i="0",
        is_active="false",
        has_video="0",
    )
    assert not listing["seller"]["is_verified"] and listing["seller"]["rating_avg"] == 0
    assert listing["isActive"] is False and listing["media"]["hasVideo"] is False
    assert oc.extract_items({"listings": {"items": [None, {}, {"id": "1"}]}}) == [{"id": "1"}]


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (s.SearchInput, {"query": "iphone", "max_price": float("inf")}),
        (s.FindDealsInput, {"query": "iphone", "discount_pct": float("nan")}),
        (s.ComparePricesInput, {"queries": ["iphone", " "]}),
        (s.ComparePricesInput, {"queries": ["iphone", "iphone"]}),
        (s.GetListingInput, {"listing_id": "0"}),
        (s.SearchInput, {"query": "iphone", "condition": "maybe"}),
    ],
)
def test_invalid_inputs_fail(model, kwargs):
    with pytest.raises(ValueError):
        model(**kwargs)


@pytest.mark.parametrize(
    "function,params,key",
    [
        (
            s.opensooq_latest_listings,
            s.LatestInput(query="iphone", response_format="json"),
            "listings",
        ),
        (s.opensooq_market_summary, s.MarketSummaryInput(query="iphone"), "priced_listings"),
        (s.opensooq_price_distribution, s.PriceDistributionInput(query="iphone"), "buckets"),
        (s.opensooq_find_deals, s.FindDealsInput(query="iphone", response_format="json"), "deals"),
        (s.opensooq_top_sellers, s.TopSellersInput(query="iphone"), "sellers"),
        (s.opensooq_find_trusted_sellers, s.TrustedSellersInput(query="iphone"), "sellers"),
        (
            s.opensooq_seller_listings,
            s.SellerListingsInput(member_id="7", response_format="json"),
            "listings",
        ),
    ],
)
def test_empty_tool_results_stay_json(function, params, key):
    with (
        patch.object(oc, "search_page", AsyncMock(return_value=([], {}))),
        patch.object(oc, "search_multi_page", AsyncMock(return_value=[])),
        patch.object(oc, "fetch_member_listings", AsyncMock(return_value=([], {}))),
    ):
        result = json.loads(asyncio.run(function(params)))
    assert result[key] in ([], 0)


def test_network_error_is_mcp_tool_error():
    with patch.object(oc, "search_page", AsyncMock(side_effect=httpx.ConnectError("secret-url"))):
        with pytest.raises(ToolError, match="could not be reached"):
            asyncio.run(s.opensooq_search_listings(s.SearchInput(query="iphone")))


def test_registration_schema_error_is_actionable():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"bad": True}))
        ) as client:
            with pytest.raises(oc.OpenSooqError, match="registration"):
                await oc._register_device(client)

    asyncio.run(run())


def test_config_shape_failure_preserves_original(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1,2]")
    assert "skipped" in install.merge_into(path, "test", {})
    assert path.read_text() == "[1,2]"


def test_atomic_write_and_overlap_lock(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"prices": {"1": 300}})
    assert json.loads(path.read_text())["prices"]["1"] == 300
    with exclusive_lock(tmp_path / "state.lock"):
        with pytest.raises(RuntimeError):
            with exclusive_lock(tmp_path / "state.lock"):
                pass
    assert not list(tmp_path.glob("*.tmp"))


def test_installer_provisions_dependencies_explicitly():
    with patch.object(install, "run") as run:
        run.return_value.returncode = 0
        assert install.ensure_runtime("/bin/uv")
    assert "--with-requirements" in run.call_args_list[-1].args[0]


def test_webhook_chunks_fit_discord_limit():
    response = httpx.Response(200, request=httpx.Request("POST", "https://example.com"))
    client = AsyncMock()
    client.post.return_value = response
    hits = [item("iPhone 15 " + "x" * 300, ident=str(i)) for i in range(20)]
    with patch.object(watch.httpx, "AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value = client
        asyncio.run(watch.post_webhook("https://example.com", "iphone", hits))
    assert client.post.await_count > 1
    assert all(len(call.kwargs["json"]["content"]) <= 2000 for call in client.post.call_args_list)


def test_title_condition_overrides_generic_dealer_description():
    listing = item("iPhone 15 Pro new", description="We sell new and used phones")
    assert listing["quality"]["condition"] == "new"


def test_different_vehicle_years_have_different_cohorts():
    from listing_quality import cohort_key

    assert cohort_key(item("Toyota Prius 2020")) != cohort_key(item("Toyota Prius 2022"))


def test_multi_model_offer_is_flagged():
    assert "multiple_models" in item("iPhone 15 / 16 Pro")["quality"]["risk_flags"]


def test_summary_does_not_trim_new_prices_against_used_cohort():
    items = [item("iPhone 15 Pro 256GB used", 100, str(i)) for i in range(10)]
    items.append(item("iPhone 15 Pro 256GB new", 700, "20"))
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        result = json.loads(
            asyncio.run(s.opensooq_market_summary(s.MarketSummaryInput(query="iphone 15 pro")))
        )
    assert result["priced_listings"] == 11 and len(result["cohorts"]) == 2


def test_unknown_specifications_do_not_produce_deals():
    items = [
        item("iPhone 15 Pro", price, str(i)) for i, price in enumerate([100, 200, 200, 200, 200])
    ]
    with patch.object(oc, "search_multi_page", AsyncMock(return_value=items)):
        result = json.loads(
            asyncio.run(
                s.opensooq_find_deals(
                    s.FindDealsInput(query="iphone 15 pro", response_format="json")
                )
            )
        )
    assert result["deal_count"] == 0 and result["baselines"][0]["baseline_usable"] is False


def test_page_parser_rejects_another_listing():
    html = (
        '<script type="application/json">'
        + json.dumps({"id": 456, "description": {"content": "Other listing"}})
        + "</script>"
    )
    assert (
        extract_details(html, oc.BASE_URL, listing_id="123")["enrichment_status"]
        == "listing_mismatch"
    )


def test_watcher_repeat_run_does_not_repeat_alerts(tmp_path, capsys):
    state = tmp_path / "watch.json"
    log = tmp_path / "alerts.log"
    args = [
        "watch.py",
        "--query",
        "iphone 15 pro",
        "--max-price",
        "400",
        "--state",
        str(state),
        "--log",
        str(log),
    ]
    with (
        patch("sys.argv", args),
        patch.object(watch, "find_hits", AsyncMock(return_value=[item("iPhone 15 Pro", 300)])),
    ):
        assert watch.main() == 2
        assert watch.main() == 0
    with (
        patch("sys.argv", args),
        patch.object(watch, "find_hits", AsyncMock(return_value=[item("iPhone 15 Pro", 280)])),
    ):
        assert watch.main() == 2
    assert len(log.read_text().splitlines()) == 2
    assert "DROP" in capsys.readouterr().out


def test_watcher_delivery_failure_preserves_state(tmp_path):
    state = tmp_path / "watch.json"
    watch.save_state(state, {"version": 2, "country": "jo", "prices": {"1": 300}})
    original = state.read_text()
    args = [
        "watch.py",
        "--query",
        "iphone",
        "--max-price",
        "400",
        "--state",
        str(state),
        "--webhook",
        "https://example.com",
    ]
    with (
        patch("sys.argv", args),
        patch.object(watch, "find_hits", AsyncMock(return_value=[item("iPhone 15", 280)])),
        patch.object(watch, "post_webhook", AsyncMock(side_effect=RuntimeError("rejected"))),
    ):
        assert watch.main() == 1
    assert state.read_text() == original


def test_other_model_mentioned_in_description_does_not_match():
    assert not match_listing(
        "iphone 15 pro", item("Samsung 15 Pro", description="Comparable to iphone 15 pro")
    )
    assert not match_listing(
        "iphone 15 pro", item("iPhone 14 Pro", description="Also available: iPhone 15 Pro")
    )
