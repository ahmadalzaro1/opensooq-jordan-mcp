#!/usr/bin/env python3
"""
Extensive diagnostic harness for the OpenSooq MCP.

The `mcp` SDK requires Python 3.10+, but the tool *logic* and the OpenSooq client
work on 3.9. This script loads `server.py` via an AST shim that strips only the
`mcp` import / `@mcp.tool` decorators (keeping every function and Pydantic model),
then exercises the tools against the live API with many detailed Arabic + English
product names across categories.

Run:  PYTHONIOENCODING=utf-8 python troubleshoot.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import time
import types
import traceback
from typing import Any, Dict, List


def load_server() -> types.ModuleType:
    src = open("server.py", encoding="utf-8").read()
    tree = ast.parse(src)
    keep = []
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("mcp"):
            continue
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "mcp" for t in n.targets):
            continue
        if isinstance(n, ast.If):
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.decorator_list = [
                d for d in n.decorator_list
                if not (isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool")
            ]
        keep.append(n)
    mod = types.ModuleType("srv")
    mod.__dict__["__name__"] = "srv"
    sys.modules["srv"] = mod
    exec(compile(ast.Module(body=keep, type_ignores=[]), "srv", "exec"), mod.__dict__)
    return mod


S = load_server()

# (label, english, arabic, category-or-None) across many categories
PRODUCTS = [
    ("iPhone 15 Pro Max 256GB", "iphone 15 pro max 256", "ايفون 15 برو ماكس 256", None),
    ("Samsung Galaxy S24 Ultra", "samsung galaxy s24 ultra", "سامسونج جالاكسي اس 24 الترا", None),
    ("PlayStation 5 Slim", "playstation 5 slim", "بلايستيشن 5 سليم", "Gaming & Toys"),
    ("Toyota Land Cruiser 2022", "toyota land cruiser 2022", "تويوتا لاند كروزر 2022", "Autos"),
    ("MacBook Pro M3 14 inch", "macbook pro m3 14", "ماك بوك برو ام3", "Computers & Laptops"),
    ("Apartment for rent Salmiya", "apartment for rent salmiya", "شقة للايجار السالمية", "Property For Rent"),
    ("Dyson V15 vacuum", "dyson v15", "دايسون في 15", "Electronics & Appliances"),
    ("Rolex Submariner watch", "rolex submariner", "ساعة روليكس سابمارينر", None),
    ("Nintendo Switch OLED", "nintendo switch oled", "نينتندو سويتش او ليد", "Gaming & Toys"),
    ("PRODUCT-THAT-DOES-NOT-EXIST-xyzqwerty", "zzxqwerty nonexistent product 9999", None, None),
]


def jload(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return {"_raw_text": s[:200]}


async def run() -> None:
    issues: List[str] = []
    timings: List[tuple] = []

    async def call(name: str, coro):
        t0 = time.time()
        try:
            res = await coro
            dt = time.time() - t0
            timings.append((name, dt))
            return res, dt
        except Exception as e:
            dt = time.time() - t0
            issues.append(f"[EXCEPTION] {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return None, dt

    print("=" * 78)
    print("PART 1 — SEARCH across detailed product names (EN vs AR)")
    print("=" * 78)
    for label, en, ar, cat in PRODUCTS:
        # English
        res, dt = await call(
            f"search EN '{label}'",
            S.opensooq_search_listings(S.SearchInput(query=en, limit=5, response_format="json")),
        )
        d = jload(res) if res else {}
        en_count = d.get("count", 0)
        en_total = d.get("total")
        # Arabic (if provided)
        ar_count = ar_total = None
        if ar:
            res2, _ = await call(
                f"search AR '{label}'",
                S.opensooq_search_listings(S.SearchInput(query=en, query_ar=ar, limit=5, response_format="json")),
            )
            d2 = jload(res2) if res2 else {}
            ar_count = d2.get("count", 0)
            ar_total = d2.get("total")
        flag = ""
        if ar and (ar_total or 0) > (en_total or 0) * 1.3:
            flag = "  <- AR notably richer"
        if isinstance(res, str) and res.startswith("Error"):
            flag = "  <- ERROR"
        print(f"{label:34} EN total={str(en_total):>6} | AR total={str(ar_total):>6}{flag}")
        if (en_total or 0) == 0 and (ar_total or 0) == 0 and 'nonexistent' not in en.lower() and 'xyz' not in en.lower():
            issues.append(f"[ZERO RESULTS] '{label}' returned nothing in EN or AR")

    print()
    print("=" * 78)
    print("PART 2 — SORT correctness (cheapest ascending, priciest descending, newest)")
    print("=" * 78)
    for sort in ("cheapest", "priciest", "newest"):
        res, _ = await call(
            f"sort {sort}",
            S.opensooq_search_listings(S.SearchInput(query="iphone 15 pro", query_ar="ايفون 15 برو", sort=sort, limit=8, response_format="json")),
        )
        d = jload(res) if res else {}
        ls = d.get("listings", [])
        if sort in ("cheapest", "priciest"):
            ps = [l["price"] for l in ls if l["price"] > 0]
            ok = ps == sorted(ps) if sort == "cheapest" else ps == sorted(ps, reverse=True)
            print(f"{sort:9} prices={ps[:6]} monotonic={ok}")
            if not ok and len(ps) > 2:
                issues.append(f"[SORT] {sort} not monotonic: {ps[:8]}")
        else:
            dates = [l["postedDate"] for l in ls]
            ok = dates == sorted(dates, reverse=True)
            print(f"{sort:9} first dates={[x[:16] for x in dates[:4]]} desc={ok}")

    print()
    print("=" * 78)
    print("PART 3 — Category + city scoping")
    print("=" * 78)
    for cat, city in [("Mobile - Tablet", "Hawally"), ("Autos", "Al Ahmadi"), ("Gaming & Toys", None)]:
        res, _ = await call(
            f"scope {cat}/{city}",
            S.opensooq_search_listings(S.SearchInput(query="iphone" if "Mobile" in cat else ("toyota" if cat == "Autos" else "ps5"), category=cat, city=city, limit=6, response_format="json")),
        )
        d = jload(res) if res else {}
        ls = d.get("listings", [])
        cities = {l["location"]["city"] for l in ls}
        cats = {l["category"] for l in ls}
        ok_city = (city is None) or (cities <= {city})
        print(f"{cat:18}/{str(city):12} total={d.get('total')} cities={cities} city_ok={ok_city}")
        if city and not ok_city:
            issues.append(f"[CITY FILTER] {cat}/{city} leaked cities {cities}")

    print()
    print("=" * 78)
    print("PART 4 — Pricing tools (summary, distribution, deals, compare)")
    print("=" * 78)
    res, _ = await call("market_summary", S.opensooq_market_summary(S.MarketSummaryInput(query="iphone 15 pro", query_ar="ايفون 15 برو", pages=2)))
    print("SUMMARY:", {k: v for k, v in jload(res).items() if k in ("priced_listings", "min", "max", "median", "p25", "p75")})
    res, _ = await call("price_distribution", S.opensooq_price_distribution(S.PriceDistributionInput(query="iphone 15 pro", pages=2, buckets=5)))
    dd = jload(res)
    print("DISTRIBUTION buckets:", [(b["range_low"], b["range_high"], b["count"]) for b in dd.get("buckets", [])])
    res, _ = await call("find_deals", S.opensooq_find_deals(S.FindDealsInput(query="iphone 15 pro", query_ar="ايفون 15 برو", pages=3, discount_pct=30, response_format="json")))
    fd = jload(res)
    print(f"DEALS median={fd.get('median_price')} excluded={fd.get('outliers_excluded')} count={fd.get('deal_count')} sample={[(x['price'], x['discount_pct']) for x in fd.get('deals', [])[:5]]}")
    # Sanity: any deal cheaper than 10% of median is suspicious (likely junk that survived trimming)
    susp = [x for x in fd.get("deals", []) if x["price"] < fd.get("median_price", 0) * 0.1]
    if susp:
        issues.append(f"[DEALS] {len(susp)} deals < 10% of median survived trimming: {[x['price'] for x in susp]}")
    res, _ = await call("compare_prices", S.opensooq_compare_prices(S.ComparePricesInput(queries=["iphone 13", "iphone 14", "iphone 15"], pages=2)))
    print("COMPARE:", [(c["query"], c.get("median")) for c in jload(res).get("comparisons", [])])

    print()
    print("=" * 78)
    print("PART 5 — Seller tools (top, profile, trusted)")
    print("=" * 78)
    res, _ = await call("top_sellers", S.opensooq_top_sellers(S.TopSellersInput(query="iphone 15", query_ar="ايفون 15", pages=2, limit=5)))
    ts = jload(res)
    print(f"TOP: seen={ts.get('listings_seen')} unique={ts.get('unique_sellers')} returned={ts.get('returned')}")
    for s in ts.get("sellers", [])[:4]:
        print(f"   {str(s['display_name'])[:22]:22} n={s['listing_count']} shop={s['is_shop']} verified={s['is_verified']} rating={s['rating_avg']} price={s['min_price']}-{s['max_price']}")
    if ts.get("sellers"):
        mid = ts["sellers"][0]["member_id"]
        res, _ = await call("seller_profile", S.opensooq_seller_profile(S.SellerProfileInput(member_id=mid, max_pages=2)))
        sp = jload(res)
        inv = sp.get("inventory", {})
        print(f"PROFILE {mid}: found={sp.get('found')} total={inv.get('total')} price={inv.get('min_price')}-{inv.get('max_price')}")
        if not sp.get("found"):
            issues.append(f"[PROFILE] member {mid} from top_sellers not found by seller_profile")
    res, _ = await call("trusted", S.opensooq_find_trusted_sellers(S.TrustedSellersInput(query="iphone 15", pages=2, min_rating=4.0)))
    tr = jload(res)
    print(f"TRUSTED: count={tr.get('seller_count')} top={[(str(s['display_name'])[:18], s['rating_avg'], s['is_verified']) for s in tr.get('sellers', [])[:3]]}")
    # consistency: trusted sellers must all be verified + meet rating
    bad = [s for s in tr.get("sellers", []) if not s["is_verified"] or s["rating_avg"] < 4.0]
    if bad:
        issues.append(f"[TRUSTED] {len(bad)} sellers violate criteria (verified+rating>=4)")

    print()
    print("=" * 78)
    print("PART 6 — Edge cases & reference tools")
    print("=" * 78)
    res, _ = await call("breakdown", S.opensooq_category_breakdown(S.CategoryBreakdownInput(query="iphone")))
    cb = jload(res)
    print("BREAKDOWN iphone:", [(c["category"], c["count"]) for c in cb.get("categories", [])[:4]])
    res, _ = await call("latest", S.opensooq_latest_listings(S.LatestInput(query="iphone 15", limit=3, response_format="json")))
    print("LATEST count:", jload(res).get("count"))
    res, _ = await call("browse", S.opensooq_browse_category(S.BrowseCategoryInput(category="Autos", sort="priciest", limit=3, response_format="json")))
    br = jload(res)
    print("BROWSE Autos priciest:", [l["price"] for l in br.get("listings", [])])
    # markdown rendering smoke test (Arabic content must not crash)
    res, _ = await call("markdown render", S.opensooq_search_listings(S.SearchInput(query="iphone 15 pro", query_ar="ايفون 15 برو", limit=2)))
    print("MARKDOWN ok, length:", len(res) if isinstance(res, str) else "n/a")
    # bad category / city error handling
    print("BAD CAT:", (await S.opensooq_search_listings(S.SearchInput(query="x", category="Spaceships")))[:55])
    print("BAD CITY:", (await S.opensooq_search_listings(S.SearchInput(query="x", city="Atlantis")))[:55])

    print()
    print("=" * 78)
    print("PART 7 — Listing detail & per-seller endpoints")
    print("=" * 78)
    # seed a real listing + member from search (AR query for variety)
    import opensooq_client as _oc
    seed = _oc.extract_items(await _oc.raw_search("ايفون 15 برو"))[0]
    lid, mid = str(seed["id"]), str(seed["member_id"])
    print(f"seed listing={lid} member={mid}")
    res, _ = await call("get_listing", S.opensooq_get_listing(S.GetListingInput(listing_id=lid, response_format="json")))
    gl = jload(res)
    base_desc = len(gl.get("listing", {}).get("description") or "")
    full_desc = len((gl.get("details", {}).get("full_description") or ""))
    sp = gl.get("details", {}).get("seller_profile") or {}
    print(f"GET_LISTING: title={gl.get('listing',{}).get('title','')[:30]!r} base_desc={base_desc} full_desc={full_desc} attrs={len(gl.get('details',{}).get('attributes',[]))} seller_total={sp.get('total_listings')} since={sp.get('member_since')}")
    if gl.get("listing", {}).get("id") != lid:
        issues.append(f"[GET_LISTING] returned wrong id for {lid}")
    if full_desc and base_desc and full_desc < base_desc:
        issues.append("[GET_LISTING] full_description shorter than masked search description")
    # include_details=False should skip the scrape
    res, _ = await call("get_listing no-detail", S.opensooq_get_listing(S.GetListingInput(listing_id=lid, include_details=False, response_format="json")))
    nd = jload(res)
    if nd.get("details"):
        issues.append("[GET_LISTING] include_details=False still returned details")
    # bad id
    bad = await S.opensooq_get_listing(S.GetListingInput(listing_id="999999999"))
    print("GET_LISTING bad id ->", bad[:48])
    if not bad.startswith("Error"):
        issues.append("[GET_LISTING] bad id did not error")
    # seller_listings: only that member, pagination meta
    res, _ = await call("seller_listings", S.opensooq_seller_listings(S.SellerListingsInput(member_id=mid, sort="cheapest", response_format="json")))
    sll = jload(res)
    same = all(l["seller"]["member_id"] == mid for l in sll.get("listings", []))
    prices = [l["price"] for l in sll.get("listings", []) if l["price"] > 0]
    asc = prices == sorted(prices)
    print(f"SELLER_LISTINGS: count={sll.get('count')} total={sll.get('total')} pages={sll.get('total_pages')} all_same={same} cheapest_sorted={asc}")
    if not same:
        issues.append("[SELLER_LISTINGS] returned listings from other members")
    if prices and not asc:
        issues.append(f"[SELLER_LISTINGS] cheapest sort not ascending: {prices[:6]}")
    # seller_profile by member_id (no query)
    res, _ = await call("seller_profile", S.opensooq_seller_profile(S.SellerProfileInput(member_id=mid, max_pages=2)))
    spr = jload(res)
    print(f"SELLER_PROFILE: found={spr.get('found')} total={spr.get('inventory',{}).get('total')} cats={list(spr.get('inventory',{}).get('categories',{}))[:4]} since={spr.get('profile',{}).get('member_since')}")
    if not spr.get("found"):
        issues.append(f"[SELLER_PROFILE] member {mid} not found via member_ids")

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    avg = sum(t for _, t in timings) / len(timings) if timings else 0
    slow = sorted(timings, key=lambda x: -x[1])[:3]
    print(f"Calls: {len(timings)} | avg {avg:.2f}s | slowest: {[(n, round(t,2)) for n,t in slow]}")
    if issues:
        print(f"\n*** {len(issues)} ISSUE(S) FOUND ***")
        for i in issues:
            print(" -", i)
    else:
        print("\nNo issues found. All tools behaved correctly.")


if __name__ == "__main__":
    asyncio.run(run())
