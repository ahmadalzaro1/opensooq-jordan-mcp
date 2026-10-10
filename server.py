#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.2.0,<2",
#   "httpx>=0.27.0,<1",
#   "pydantic>=2.0.0,<3",
# ]
# ///
"""
Comprehensive MCP server for OpenSooq (Jordan classifieds).

Wraps OpenSooq's private `serp/search/v2` API — the same handshake the web
client uses (anonymous device registration + derived-key HS256 JWT signing) —
behind a broad, read-only tool set covering:

  * Search & discovery   : full-text search, sorting, newest listings, autocomplete-style category facets
  * Categories & cities  : taxonomy listing + category/city-scoped browsing
  * Pricing & deals      : market summaries, price distributions, below-market deal finder, query comparisons
  * Sellers & reputation : seller leaderboards, single-seller profiles, trusted-shop discovery

All network/parsing logic lives in `opensooq_client.py` (no MCP deps, unit-testable).
This module is the MCP tool layer. Everything is read-only — nothing is created,
edited, or deleted, and the masked seller phone number is never unmasked.

Run locally (stdio):   python server.py
"""

from __future__ import annotations

import json
import re
import statistics
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mcp.server.fastmcp import FastMCP

import opensooq_client as oc
from market import resolve_city, resolve_category, PROFILE, COUNTRY, CURRENCY, LANGUAGE

mcp = FastMCP("opensooq_jordan_mcp")


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class SortOrder(str, Enum):
    """Friendly sort names mapped to OpenSooq sort codes."""
    RELEVANT = "relevant"
    NEWEST = "newest"
    CHEAPEST = "cheapest"
    PRICIEST = "priciest"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sort_code(order: Optional[SortOrder]) -> Optional[str]:
    return oc.SORT_CODES[order.value] if order else None


_resolve_category = resolve_category
_resolve_city = resolve_city

def _apply_post_filters(
    listings: List[Dict[str, Any]],
    *,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    has_video: bool = False,
    has_images: bool = False,
    shops_only: bool = False,
    verified_only: bool = False,
    min_seller_rating: Optional[float] = None,
    min_seller_reviews: int = 0,
) -> List[Dict[str, Any]]:
    """Apply client-side refinements OpenSooq does not honor server-side."""
    out = []
    for l in listings:
        price = l["price"]
        if min_price is not None and (price <= 0 or price < min_price):
            continue
        if max_price is not None and (price <= 0 or price > max_price):
            continue
        if has_video and not l["media"]["hasVideo"]:
            continue
        if has_images and l["media"]["imageCount"] <= 0:
            continue
        s = l["seller"]
        if shops_only and not s["is_shop"]:
            continue
        if verified_only and not s["is_verified"]:
            continue
        if min_seller_rating is not None and (s["rating_avg"] or 0) < min_seller_rating:
            continue
        if s["rating_count"] < min_seller_reviews:
            continue
        out.append(l)
    return out


# Relevance + price-band filtering live in the shared client (reused by watch.py).
_query_tokens = oc.query_tokens
_relevant_listings = oc.relevant_listings
_comparable_listings = oc.comparable_listings


def _percentile(values: List[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _sample_metadata(listings: List[Dict[str, Any]], pages: int) -> Dict[str, Any]:
    return {"country": COUNTRY, "pages_requested": pages, "scanned_listings": len(listings),
            "sample_note": "Sample of asking prices, not completed sales or the entire market. Condition and specifications may differ."}


def _price_stats(prices: List[float]) -> Dict[str, float]:
    """Compute summary statistics for a list of positive prices."""
    s = sorted(prices)
    n = len(s)
    return {
        "count": n,
        "min": s[0],
        "max": s[-1],
        "average": round(statistics.fmean(s), 3),
        "median": round(statistics.median(s), 3),
        "p25": round(_percentile(s, 0.25), 3),
        "p75": round(_percentile(s, 0.75), 3),
    }


def _handle_error(e: Exception) -> str:
    if isinstance(e, oc.OpenSooqError):
        return f"Error: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return ("Error: OpenSooq rejected the signed JWT (401) even after "
                    "re-registration. The signing scheme may have changed.")
        if code == 403:
            return "Error: OpenSooq blocked the request (403) — device may be rate-limited or geo-fenced."
        if code == 429:
            return "Error: Rate limit exceeded (429). Wait before retrying."
        return f"Error: OpenSooq API returned status {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to OpenSooq timed out. Try again."
    if isinstance(e, (httpx.ConnectError, httpx.RequestError)):
        return f"Error: Could not reach OpenSooq ({type(e).__name__})."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: Unexpected {type(e).__name__}: {e}"


def _effective_query(query: str, query_ar: Optional[str]) -> str:
    return query_ar or query


def _seller_line(s: Dict[str, Any]) -> str:
    name = s["display_name"] or s["username"] or "Unknown seller"
    bits = [name]
    if s["is_shop"]:
        bits.append("🏪 shop")
    if s["is_verified"]:
        bits.append("✓ verified")
    if s["rating_count"]:
        bits.append(f"★ {s['rating_avg']:.2f} ({s['rating_count']})")
    return " · ".join(bits)


def _format_listings_md(title: str, listings: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> str:
    lines = [f"# {title}", ""]
    priced = [l["price"] for l in listings if l["price"] > 0]
    header = f"Showing **{len(listings)}** listings"
    if meta and meta.get("count") is not None:
        header += f" of **{meta['count']}** total"
    if priced:
        header += f" — price range {min(priced):,.3f}–{max(priced):,.3f} {listings[0]['currency']}"
    lines.append(header + ".")
    lines.append("")
    for l in listings:
        price = f"{l['price']:,.3f} {l['currency']}" if l["price"] > 0 else "Price on request"
        loc = ", ".join(p for p in (l["location"]["neighborhood"], l["location"]["city"]) if p)
        lines.append(f"## {l['title']}")
        lines.append(f"- **Price**: {price}")
        if l["category"]:
            lines.append(f"- **Category**: {l['category']}")
        if loc:
            lines.append(f"- **Location**: {loc}")
        lines.append(f"- **Seller**: {_seller_line(l['seller'])}")
        media_bits = [f"{l['media']['imageCount']} photo(s)"]
        if l["media"]["hasVideo"]:
            media_bits.append("video")
        if l["media"]["has360"]:
            media_bits.append("360°")
        lines.append(f"- **Media**: {', '.join(media_bits)}")
        if l["tags"]:
            lines.append(f"- **Tags**: {', '.join(l['tags'])}")
        lines.append(f"- **Posted**: {l['postedDate']}")
        lines.append(f"- **Link**: {l['url']}")
        if l["description"]:
            d = l["description"].strip().replace("\n", " ")
            lines.append(f"- **Description**: {d[:200] + '…' if len(d) > 200 else d}")
        lines.append("")
    return "\n".join(lines)


def _format_listing_detail_md(item: Dict[str, Any], details: Optional[Dict[str, Any]]) -> str:
    price = f"{item['price']:,.3f} {item['currency']}" if item["price"] > 0 else "Price on request"
    loc = ", ".join(p for p in (item["location"]["neighborhood"], item["location"]["city"]) if p)
    lines = [f"# {item['title']}", "", f"- **Price**: {price}", f"- **Listing ID**: {item['id']}"]
    if item["category"]:
        lines.append(f"- **Category**: {item['category']}")
    if loc:
        lines.append(f"- **Location**: {loc}")
    lines.append(f"- **Status**: {item['status']} ({'active' if item['isActive'] else 'inactive'})")
    lines.append(f"- **Posted**: {item['postedDate']}")
    media = item["media"]
    mbits = [f"{media['imageCount']} photo(s)"] + (["video"] if media["hasVideo"] else []) + (["360°"] if media["has360"] else [])
    lines.append(f"- **Media**: {', '.join(mbits)}")
    lines.append(f"- **Link**: {item['url']}")

    sp = (details or {}).get("seller_profile") or {}
    s = item["seller"]
    lines += ["", "## Seller"]
    lines.append(f"- **Name**: {sp.get('full_name') or s['display_name'] or s['username'] or 'Unknown'}")
    badges = []
    if sp.get("is_shop") or s["is_shop"]:
        badges.append("🏪 shop")
    if s["is_verified"] or sp.get("verification_level"):
        badges.append("✓ verified")
    if sp.get("authorised_seller"):
        badges.append("authorised")
    if badges:
        lines.append(f"- **Badges**: {', '.join(badges)}")
    rating = sp.get("rating_avg") or s["rating_avg"]
    rcount = sp.get("rating_count") or s["rating_count"]
    if rcount:
        lines.append(f"- **Rating**: ★ {rating:.2f} ({rcount} ratings)")
    if sp.get("total_listings") is not None:
        lines.append(f"- **Total listings**: {sp['total_listings']}")
    if sp.get("member_since"):
        lines.append(f"- **Member since**: {sp['member_since']}")
    if sp.get("response_time"):
        lines.append(f"- **Response time**: {sp['response_time']}")
    if s["has_phone"] and s["phone_masked"]:
        lines.append(f"- **Phone (masked)**: {s['phone_masked']}")
    if sp.get("member_link"):
        lines.append(f"- **Seller page**: {sp['member_link']}")

    attrs = (details or {}).get("attributes") or []
    if attrs:
        lines += ["", "## Attributes"]
        for a in attrs:
            lines.append(f"- **{a['label']}**: {a['value']}")

    desc = (details or {}).get("full_description") or item.get("description")
    if desc:
        lines += ["", "## Description", desc.strip()]
    return "\n".join(lines)


def _render(payload: Dict[str, Any], fmt: ResponseFormat, md_title: str,
            listings_key: str = "listings", meta: Optional[Dict[str, Any]] = None,
            note: Optional[str] = None) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    md = _format_listings_md(md_title, payload[listings_key], meta)
    return f"> ⚠️ {note}\n\n{md}" if note else md


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class _QueryBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    query: str = Field(..., description="English search term (e.g. 'iphone 15 pro').", min_length=1, max_length=200)
    query_ar: Optional[str] = Field(default=None, description="Optional Arabic term; overrides `query` for the API call (better recall).", max_length=200)

    @field_validator("query")
    @classmethod
    def _v(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query cannot be empty")
        return v.strip()


class SearchInput(_QueryBase):
    sort: SortOrder = Field(default=SortOrder.RELEVANT, description="Sort order: relevant, newest, cheapest, priciest.")
    category: Optional[str] = Field(default=None, description="Category name or id to scope to (see opensooq_list_categories).", max_length=60)
    city: Optional[str] = Field(default=None, description="City name or id to scope to (e.g. 'Amman', 59).", max_length=40)
    page: int = Field(default=1, description="1-indexed result page.", ge=1, le=50)
    limit: Optional[int] = Field(default=None, description="Cap listings returned from the page (1-59).", ge=1, le=50)
    min_price: Optional[float] = Field(default=None, description="Client-side: drop listings priced below this.", ge=0)
    max_price: Optional[float] = Field(default=None, description="Client-side: drop listings priced above this.", ge=0)
    has_video: bool = Field(default=False, description="Client-side: keep only listings with a video.")
    has_images: bool = Field(default=False, description="Client-side: keep only listings that have images.")
    shops_only: bool = Field(default=False, description="Keep only professional 'shop' sellers.")
    verified_only: bool = Field(default=False, description="Keep only verified sellers.")
    min_seller_rating: Optional[float] = Field(default=None, description="Drop sellers rated below this (0-5).", ge=0, le=5)
    min_seller_reviews: int = Field(default=0, ge=0, le=100000, description="Minimum number of seller reviews.")
    strict_match: bool = Field(default=False, description="Drop listings whose title/description don't contain a query token (suppresses OpenSooq's loosely-related fallback results).")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price must not exceed max_price")
        return self


class LatestInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    city: Optional[str] = Field(default=None, description="Optional city name/id to scope to.", max_length=40)
    limit: int = Field(default=15, description="Number of newest listings to return (1-59).", ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")


class BrowseCategoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    category: str = Field(..., description="Category name or id to browse (see opensooq_list_categories).", min_length=1, max_length=60)
    query: Optional[str] = Field(default=None, description="Optional keyword to narrow within the category.", max_length=200)
    city: Optional[str] = Field(default=None, description="Optional city name/id.", max_length=40)
    sort: SortOrder = Field(default=SortOrder.RELEVANT, description="Sort order.")
    page: int = Field(default=1, description="1-indexed page.", ge=1, le=50)
    limit: Optional[int] = Field(default=None, description="Cap listings returned (1-59).", ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")


class CategoryBreakdownInput(_QueryBase):
    pass


class MarketSummaryInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    pages: int = Field(default=2, description="Result pages to scan for a larger sample (1-5).", ge=1, le=5)


class PriceDistributionInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    pages: int = Field(default=3, description="Result pages to scan (1-5).", ge=1, le=5)
    buckets: int = Field(default=6, description="Number of histogram buckets (2-12).", ge=2, le=12)


class FindDealsInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    pages: int = Field(default=3, description="Result pages to scan for the price baseline (1-5).", ge=1, le=5)
    discount_pct: float = Field(default=30.0, description="Flag listings at least this %% below the median price (5-90).", ge=5, le=90)
    verified_only: bool = Field(default=False, description="Only surface deals from verified sellers.")
    limit: int = Field(default=15, description="Max deals to return (1-59).", ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")


class ComparePricesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    queries: List[str] = Field(..., description="2-5 search terms to compare price-wise.", min_length=2, max_length=5)
    pages: int = Field(default=2, description="Pages to scan per query (1-5).", ge=1, le=5)


class TopSellersInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    pages: int = Field(default=2, description="Result pages to scan (1-5).", ge=1, le=5)
    limit: int = Field(default=15, description="Max sellers to return (1-59).", ge=1, le=50)


class _IdModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")


class GetListingInput(_IdModel):
    listing_id: str = Field(..., description="The listing/post id (digits) — e.g. from a search result's `id`.", min_length=1, max_length=30)
    include_details: bool = Field(default=True, description="Also scrape the listing page for the full untruncated description, attribute rows, and the rich seller profile (memberSince, responseTime, total listings). Slightly slower.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("listing_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("listing_id must be the numeric post id (e.g. '281910976').")
        return v


class SellerListingsInput(_IdModel):
    member_id: str = Field(..., description="The seller's member_id (from a listing's seller object or opensooq_top_sellers).", min_length=1, max_length=30)
    sort: SortOrder = Field(default=SortOrder.RELEVANT, description="Sort order: relevant, newest, cheapest, priciest.")
    page: int = Field(default=1, description="1-indexed page of the seller's inventory.", ge=1, le=20)
    limit: Optional[int] = Field(default=None, description="Cap listings returned from the page (1-30).", ge=1, le=30)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'.")

    @field_validator("member_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("member_id must be numeric (e.g. '42899294').")
        return v


class SellerProfileInput(_IdModel):
    member_id: str = Field(..., description="The seller's member_id (from a listing's seller object or opensooq_top_sellers).", min_length=1, max_length=30)
    max_pages: int = Field(default=3, description="Pages of the seller's inventory to scan for stats (1-5).", ge=1, le=5)
    include_sample: bool = Field(default=True, description="Include a few sample listings in the response.")

    @field_validator("member_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("member_id must be numeric (e.g. '42899294').")
        return v


class TrustedSellersInput(_QueryBase):
    category: Optional[str] = Field(default=None, description="Optional category name/id to scope to.", max_length=60)
    pages: int = Field(default=3, description="Result pages to scan (1-5).", ge=1, le=5)
    min_rating: float = Field(default=4.0, description="Minimum average seller rating (0-5).", ge=0, le=5)
    min_reviews: int = Field(default=5, ge=0, le=100000, description="Minimum seller review count; ratings do not guarantee trustworthiness.")
    require_shop: bool = Field(default=False, description="Require the 'shop' badge in addition to verification.")


# ---------------------------------------------------------------------------
# Seller aggregation helper (shared by several tools)
# ---------------------------------------------------------------------------

def _aggregate_sellers(listings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sellers: Dict[str, Dict[str, Any]] = {}
    for l in listings:
        s = l["seller"]
        mid = s["member_id"]
        if not mid:
            continue
        e = sellers.get(mid)
        if e is None:
            e = {
                "member_id": mid,
                "display_name": s["display_name"],
                "username": s["username"],
                "is_shop": s["is_shop"],
                "is_verified": s["is_verified"],
                "rating_avg": s["rating_avg"],
                "rating_count": s["rating_count"],
                "listing_count": 0,
                "currency": l["currency"],
                "sample_listing_url": l["url"],
                "_prices": [],
            }
            sellers[mid] = e
        e["listing_count"] += 1
        if l["price"] > 0:
            e["_prices"].append(l["price"])
    ranked = sorted(sellers.values(), key=lambda e: (e["listing_count"], e["rating_avg"]), reverse=True)
    for e in ranked:
        prices = e.pop("_prices")
        e["min_price"] = min(prices) if prices else 0
        e["max_price"] = max(prices) if prices else 0
    return ranked


_ANN = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


# Pages to scan when a search must be relevance-gated (price sorts / strict_match).
_RELEVANCE_SCAN_PAGES = 5


def _sort_listings(listings: List[Dict[str, Any]], order: SortOrder) -> List[Dict[str, Any]]:
    """Order listings client-side. Unpriced items sink to the bottom for price sorts."""
    if order == SortOrder.CHEAPEST:
        return sorted(listings, key=lambda l: l["price"] if l["price"] > 0 else float("inf"))
    if order == SortOrder.PRICIEST:
        return sorted(listings, key=lambda l: l["price"], reverse=True)
    if order == SortOrder.NEWEST:
        return sorted(listings, key=lambda l: l["postedDate"], reverse=True)
    return listings  # RELEVANT: preserve OpenSooq's relevance order


async def _relevance_gated_fetch(
    eff_query: str, cat_id: Optional[int], city_id: Optional[int],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch several pages by RELEVANCE and return (deduped listings, page-1 meta).

    Used when the requested view (cheapest/priciest, or strict_match) cannot trust
    OpenSooq's server-side price ordering — that ranks the whole loose fallback set,
    burying the real product under unrelated cheap junk. Relevance ordering instead
    surfaces genuine matches, which we then filter and sort ourselves.
    """
    raw: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    seen: set = set()
    async with httpx.AsyncClient() as client:
        for page in range(1, _RELEVANCE_SCAN_PAGES + 1):
            items, m = await oc.search_page(
                eff_query, page=page, cat_id=cat_id, city_id=city_id,
                sort=oc.SORT_CODES["relevant"], client=client,
            )
            if page == 1:
                meta = m
            if not items:
                break
            for l in items:
                if l["id"] not in seen:
                    seen.add(l["id"])
                    raw.append(l)
    return raw, meta


# ===========================================================================
# SEARCH & DISCOVERY
# ===========================================================================

@mcp.tool(name="opensooq_search_listings", annotations={"title": "Search OpenSooq Listings", **_ANN})
async def opensooq_search_listings(params: SearchInput) -> str:
    """Search live OpenSooq (Jordan) classified listings with sorting and filters.

    The primary discovery tool. Supports server-side sorting (relevant/newest/
    cheapest/priciest), category and city scoping, plus client-side refinements
    (price range, media presence, seller quality). Prefer `query_ar` for recall.

    Args:
        params (SearchInput): query, query_ar, sort, category, city, page, limit,
            min_price, max_price, has_video, has_images, shops_only, verified_only,
            min_seller_rating, response_format.

    Returns:
        str: JSON `{query, sort, page, count, total, listings:[...]}` or a markdown
        digest. Each listing carries id, title, description, price, currency,
        dates, category/categoryPath, location{city,neighborhood}, status,
        media{imageUrl,gallery,imageCount,hasVideo,has360}, tags, and a seller
        object (member_id, display_name, badges, is_shop, rating_avg, rating_count,
        is_verified, masked phone). On failure, a string starting "Error:".

    Examples:
        - "Cheapest iPhone 15 Pro in Amman from verified shops" ->
          query="iphone 15 pro", sort="cheapest", city="Amman", shops_only=True, verified_only=True
        - "PS5 listings 80-120 JOD" -> query="ps5", min_price=80, max_price=120

    Note: OpenSooq's server-side price sort ranks its whole loose result set, so
    sorting a specific product by 'cheapest'/'priciest' would otherwise surface
    unrelated 1-JOD junk while the real product sits pages deep. For those sorts
    (and whenever strict_match=true) this tool instead scans several pages by
    relevance, keeps genuine matches, drops decoy/outlier prices, and sorts
    client-side. The ranking covers only the scanned sample, not all listings.
    """
    try:
        cat_id = _resolve_category(params.category)
        city_id = _resolve_city(params.city)
        eff_query = _effective_query(params.query, params.query_ar)
        # Price sorts and strict_match can't trust OpenSooq's server-side ordering
        # of its fuzzy result set — fetch by relevance across pages and sort here.
        price_sort = params.sort in (SortOrder.CHEAPEST, SortOrder.PRICIEST)
        gated = price_sort or params.strict_match

        if gated:
            raw, meta = await _relevance_gated_fetch(eff_query, cat_id, city_id)
            fetched = len(raw)
            relevant = _relevant_listings(eff_query, raw)
            listings = relevant
            if price_sort:
                # Drop decoy teaser prices (e.g. 9.99 installment hooks) and junk
                # outliers so the price ranking reflects the real product.
                comparable, _, _ = _comparable_listings(listings)
                if comparable:
                    listings = comparable
        else:
            listings, meta = await oc.search_page(
                eff_query, page=params.page, cat_id=cat_id, city_id=city_id,
                sort=_sort_code(params.sort),
            )
            fetched = len(listings)
            relevant = _relevant_listings(eff_query, listings)

        listings = _apply_post_filters(
            listings, min_price=params.min_price, max_price=params.max_price,
            has_video=params.has_video, has_images=params.has_images,
            shops_only=params.shops_only, verified_only=params.verified_only,
            min_seller_rating=params.min_seller_rating, min_seller_reviews=params.min_seller_reviews,
        )
        if gated:
            listings = _sort_listings(listings, params.sort)
        matching_count = len(listings)
        if gated:
            page_size = params.limit or 30
            offset = (params.page - 1) * page_size
            listings = listings[offset:offset + page_size]
        elif params.limit is not None:
            listings = listings[:params.limit]
        payload: Dict[str, Any] = {
            "query": eff_query,
            "sort": params.sort.value,
            "page": params.page,
            "count": len(listings),
            "total": meta.get("count"),
            "listings": listings,
        }
        if gated:
            payload["matched"] = len(relevant)
            payload["scanned"] = fetched
            payload["sample_count"] = matching_count
            payload["sample_note"] = "Sorted within up to five scanned relevance pages; not a marketplace-wide price ranking."
            payload["total_note"] = "total is the upstream fuzzy result count before local filters"
        # Warn when even a relevance scan found almost no genuine matches.
        if fetched and len(relevant) / fetched < 0.2:
            payload["relevance_warning"] = (
                f"Only {len(relevant)}/{fetched} scanned listings match '{eff_query}'. "
                f"OpenSooq likely has little/no real inventory for it (loosely-related "
                f"results were dropped)."
            )
        return _render(payload, params.response_format,
                       f"OpenSooq: '{params.query}' ({params.sort.value})", meta=meta,
                       note=" ".join(filter(None, [payload.get("sample_note"), payload.get("relevance_warning")])))
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_latest_listings", annotations={"title": "Newest OpenSooq Listings", **_ANN})
async def opensooq_latest_listings(params: LatestInput) -> str:
    """Get the most recently posted OpenSooq listings for a product.

    Convenience wrapper over search sorted by recency — for "what just got
    listed?" / monitoring use cases.

    Args:
        params (LatestInput): query, query_ar, category, city, limit, response_format.

    Returns:
        str: JSON `{query, count, listings:[...]}` (newest first) or markdown.
        On failure, a string starting "Error:".

    Examples:
        - "Newest iPhone 15 listings" -> query="iphone 15"
        - "Latest cars posted in Jordan City" -> query="", category="Autos", city="Jordan City"  # note: query required, use a term
    """
    try:
        listings, _ = await oc.search_page(
            _effective_query(params.query, params.query_ar), page=1,
            cat_id=_resolve_category(params.category), city_id=_resolve_city(params.city),
            sort=oc.SORT_CODES["newest"],
        )
        listings = listings[: params.limit]
        if not listings:
            return f"No recent OpenSooq listings for '{_effective_query(params.query, params.query_ar)}'."
        payload = {"query": _effective_query(params.query, params.query_ar), "count": len(listings), "listings": listings}
        return _render(payload, params.response_format, f"Newest OpenSooq listings: '{params.query}'")
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_category_breakdown", annotations={"title": "OpenSooq Category Breakdown", **_ANN})
async def opensooq_category_breakdown(params: CategoryBreakdownInput) -> str:
    """Show which categories a search term appears in, with listing counts.

    Uses OpenSooq's facet data to reveal how a query distributes across the
    catalog — e.g. "iphone" -> mostly Mobile (715) but also Gaming (10),
    Electronics (9). Useful before scoping a search to one category.

    Args:
        params (CategoryBreakdownInput): query, query_ar.

    Returns:
        str: JSON `{query, total, categories:[{category, cat1_id, count, url}]}`.
        On failure, a string starting "Error:".

    Examples:
        - "Which categories have 'playstation' listings?" -> query="playstation"
    """
    try:
        data = await oc.raw_search(_effective_query(params.query, params.query_ar))
        facets = oc.extract_category_facets(data)
        return json.dumps({
            "query": _effective_query(params.query, params.query_ar),
            "total": oc.extract_total(data),
            "categories": facets,
        }, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# LISTING DETAIL
# ===========================================================================

@mcp.tool(name="opensooq_get_listing", annotations={"title": "Get OpenSooq Listing Detail", **_ANN})
async def opensooq_get_listing(params: GetListingInput) -> str:
    """Fetch one listing by id, with full description, attributes, and seller profile.

    Retrieves the listing via OpenSooq's `post_ids` lookup (clean API), then —
    when `include_details` is set — scrapes the listing page for the extras the
    search payload omits: the **full untruncated description** (often containing
    specs like storage/battery%), **attribute rows** (location, sub-category,
    post date), and a **rich seller profile** (member-since date, response time,
    total listing count, profile picture, seller page link). The page scrape is
    best-effort: if it fails, the API-level listing is still returned.

    Args:
        params (GetListingInput): listing_id (numeric), include_details (default
            True), response_format ('markdown' | 'json').

    Returns:
        str: In JSON mode `{listing:{...full listing schema...},
        details:{full_description, attributes:[{type,label,value}],
        seller_profile:{full_name, rating_avg, rating_count, total_listings,
        member_since, response_time, is_shop, authorised_seller,
        verification_level, profile_picture, member_link}}}`. In markdown mode a
        formatted listing card. Returns "Error: listing not found ..." if the id
        is unknown/expired, or a string starting "Error:" on failure.

    Examples:
        - "Show full details for OpenSooq listing 281910976" -> listing_id="281910976"
        - "Get listing 281910976 as JSON without scraping" -> listing_id="281910976", include_details=False
    """
    try:
        item = await oc.fetch_listing_by_id(params.listing_id)
        if not item:
            return (f"Error: listing {params.listing_id} not found (it may be expired, "
                    f"removed, or the id is wrong).")
        details: Dict[str, Any] = {}
        if params.include_details:
            details = await oc.fetch_listing_page_details(item["url"])
        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"listing": item, "details": details}, indent=2, ensure_ascii=False)
        return _format_listing_detail_md(item, details)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# CATEGORIES & CITIES (reference)
# ===========================================================================

@mcp.tool(name="opensooq_list_categories", annotations={"title": "List OpenSooq Categories", **_ANN})
async def opensooq_list_categories() -> str:
    """List OpenSooq's top-level categories (id, English/Arabic name, url slug).

    Reference tool — use the returned id or name with `category` parameters on
    other tools (search, browse, deals, etc.).

    Returns:
        str: JSON `{count, categories:[{id, name_en, name_ar, url_name}]}`.
    """
    cats = [{"id": cid, "name_en": m["en"], "name_ar": m["ar"], "url_name": m["url"]}
            for cid, m in oc.CATEGORIES.items()]
    return json.dumps({"country": COUNTRY, "catalog_source": "bundled snapshot", "verified_at": PROFILE["verified_at"], "count": len(cats), "categories": cats}, indent=2, ensure_ascii=False)


@mcp.tool(name="opensooq_list_cities", annotations={"title": "List OpenSooq Cities", **_ANN})
async def opensooq_list_cities() -> str:
    """List the Jordan cities/governorates usable as `city` filters.

    Returns:
        str: JSON `{count, cities:[{id, name}]}`.
    """
    cities = [{"id": cid, "name": m["en"], "name_ar": m["ar"], "url_name": m["url"]} for cid, m in oc.CITY_DETAILS.items()]
    return json.dumps({"country": COUNTRY, "catalog_source": "bundled snapshot", "verified_at": PROFILE["verified_at"], "count": len(cities), "cities": cities}, indent=2, ensure_ascii=False)


@mcp.tool(name="opensooq_browse_category", annotations={"title": "Browse OpenSooq Category", **_ANN})
async def opensooq_browse_category(params: BrowseCategoryInput) -> str:
    """Browse listings within a specific category, optionally narrowed by keyword/city.

    For category-first exploration ("show me what's in Gaming & Toys") rather
    than keyword-first search. Supports the same sorting as search.

    Args:
        params (BrowseCategoryInput): category (name/id, required), query, city,
            sort, page, limit, response_format.

    Returns:
        str: JSON `{category, query, page, count, total, listings:[...]}` or markdown.
        On failure, a string starting "Error:".

    Examples:
        - "Browse the cheapest gaming listings" -> category="Gaming & Toys", sort="cheapest"
        - "Laptops in Amman" -> category="Computers & Laptops", city="Amman"
    """
    try:
        cat_id = _resolve_category(params.category)
        listings, meta = await oc.search_page(
            params.query or "", page=params.page, cat_id=cat_id,
            city_id=_resolve_city(params.city), sort=_sort_code(params.sort),
        )
        if params.limit is not None:
            listings = listings[: params.limit]
        cat_name = oc.CATEGORIES.get(cat_id, {}).get("en", str(cat_id))
        if not listings:
            return f"No listings found in category '{cat_name}'."
        payload = {
            "category": cat_name, "query": params.query, "page": params.page,
            "count": len(listings), "total": meta.get("count"), "listings": listings,
        }
        return _render(payload, params.response_format, f"OpenSooq category: {cat_name}", meta=meta)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# PRICING & DEALS
# ===========================================================================

@mcp.tool(name="opensooq_market_summary", annotations={"title": "OpenSooq Price Summary", **_ANN})
async def opensooq_market_summary(params: MarketSummaryInput) -> str:
    """Compute price statistics for a product across multiple pages of listings.

    Scans several result pages and derives min/max/average/median/quartiles —
    for "what's the going rate?" questions. Unpriced ("on request") listings are
    excluded from stats but counted separately.

    Args:
        params (MarketSummaryInput): query, query_ar, category, pages (1-5).

    Returns:
        str: JSON `{query, currency, total_listings, relevant_listings,
        priced_listings, outliers_excluded, min, max, average, median, p25, p75,
        cheapest_url, priciest_url}` or a note if no priced listings. Stats are
        computed on listings that match the query and fall within a sane price
        band (accessories/troll listings excluded). On failure, "Error:".

    Examples:
        - "Average price of a used PS5 on OpenSooq?" -> query="ps5", query_ar="بلايستيشن 5"
    """
    try:
        eff = _effective_query(params.query, params.query_ar)
        listings = await oc.search_multi_page(
            eff, pages=params.pages, cat_id=_resolve_category(params.category),
        )
        if not listings:
            return f"No OpenSooq listings found for '{eff}'."
        relevant = _relevant_listings(eff, listings)
        comparable, _, excluded = _comparable_listings(relevant)
        priced = sorted(comparable, key=lambda l: l["price"])
        if not priced:
            return json.dumps({
                "query": eff, "total_listings": len(listings),
                "relevant_listings": len(relevant), "priced_listings": 0,
                "note": "No comparable priced listings (all 'on request' or filtered as outliers).",
            }, indent=2, ensure_ascii=False)
        stats = _price_stats([l["price"] for l in priced])
        return json.dumps({
            "query": eff,
            "currency": priced[0]["currency"],
            **_sample_metadata(listings, params.pages),
            "total_listings": len(listings),
            "relevant_listings": len(relevant),
            "priced_listings": stats["count"],
            "outliers_excluded": excluded,
            "min": stats["min"], "max": stats["max"],
            "average": stats["average"], "median": stats["median"],
            "p25": stats["p25"], "p75": stats["p75"],
            "cheapest_url": priced[0]["url"], "priciest_url": priced[-1]["url"],
        }, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_price_distribution", annotations={"title": "OpenSooq Price Distribution", **_ANN})
async def opensooq_price_distribution(params: PriceDistributionInput) -> str:
    """Build a price histogram for a product to reveal how listings are distributed.

    Buckets priced listings into evenly sized price ranges — useful for spotting
    clusters, outliers, and the bulk of the market.

    Args:
        params (PriceDistributionInput): query, query_ar, category, pages, buckets (2-12).

    Returns:
        str: JSON `{query, currency, priced_listings, min, max, buckets:[{range_low,
        range_high, count}]}`. On failure, a string starting "Error:".

    Examples:
        - "Price distribution of iPhone 13 on OpenSooq with 8 buckets" -> query="iphone 13", buckets=8
    """
    try:
        eff = _effective_query(params.query, params.query_ar)
        listings = await oc.search_multi_page(
            eff, pages=params.pages, cat_id=_resolve_category(params.category),
        )
        comparable, _, excluded = _comparable_listings(_relevant_listings(eff, listings))
        prices = sorted(l["price"] for l in comparable)
        if not prices:
            return f"No comparable priced OpenSooq listings for '{eff}'."
        lo, hi = prices[0], prices[-1]
        bucket_count = 1 if hi == lo else params.buckets
        width = (hi - lo) / bucket_count
        buckets = []
        for i in range(bucket_count):
            b_lo = lo + i * width
            b_hi = hi if i == bucket_count - 1 else lo + (i + 1) * width
            cnt = sum(1 for p in prices if (b_lo <= p <= b_hi if i == bucket_count - 1 else b_lo <= p < b_hi))
            buckets.append({"range_low": round(b_lo, 3), "range_high": round(b_hi, 3), "count": cnt})
        return json.dumps({
            "query": eff,
            "currency": comparable[0]["currency"],
            **_sample_metadata(listings, params.pages),
            "priced_listings": len(prices), "outliers_excluded": excluded,
            "min": lo, "max": hi, "buckets": buckets,
        }, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_find_deals", annotations={"title": "Find OpenSooq Deals", **_ANN})
async def opensooq_find_deals(params: FindDealsInput) -> str:
    """Find heavily discounted listings priced well below the market median.

    Scans several pages, computes the median price, then surfaces listings at
    least `discount_pct`%% below it — ranked by discount. Great for bargain
    hunting. Optionally restrict to verified sellers to avoid scams.

    Args:
        params (FindDealsInput): query, query_ar, category, pages, discount_pct
            (default 30), verified_only, limit, response_format.

    Returns:
        str: JSON `{query, currency, median_price, threshold_price, deal_count,
        deals:[{...listing, discount_pct}]}` or markdown. On failure, "Error:".

    Examples:
        - "Find iPhone 15 Pro deals 40%+ below median from verified sellers" ->
          query="iphone 15 pro", discount_pct=40, verified_only=True
    """
    try:
        eff = _effective_query(params.query, params.query_ar)
        listings = await oc.search_multi_page(
            eff, pages=params.pages, cat_id=_resolve_category(params.category),
        )
        # Keep only query-relevant listings within a sane price band of the median,
        # so accessories/troll prices can't masquerade as "99% off" deals.
        comparable, median, removed = _comparable_listings(_relevant_listings(eff, listings))
        if not comparable:
            return f"No comparable priced OpenSooq listings for '{eff}'."
        threshold = median * (1 - params.discount_pct / 100.0)
        deals = [l for l in comparable if l["price"] <= threshold]
        if params.verified_only:
            deals = [l for l in deals if l["seller"]["is_verified"]]
        for l in deals:
            l["discount_pct"] = round((1 - l["price"] / median) * 100, 1)
        deals.sort(key=lambda l: l["discount_pct"], reverse=True)
        deals = deals[: params.limit]
        currency = comparable[0]["currency"]
        if not deals:
            return (f"No listings at least {params.discount_pct:.0f}% below the median "
                    f"({median:,.3f} {currency}) for '{eff}'.")
        payload = {
            "query": eff,
            "currency": currency,
            **_sample_metadata(listings, params.pages),
            "median_price": round(median, 3),
            "threshold_price": round(threshold, 3),
            "outliers_excluded": removed,
            "deal_count": len(deals), "deals": deals,
        }
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(payload, indent=2, ensure_ascii=False)
        title = f"Deals for '{params.query}' (≥{params.discount_pct:.0f}% below median {median:,.3f} {currency})"
        return _format_listings_md(title, deals) + "\n\n" + _sample_metadata(listings, params.pages)["sample_note"]
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_compare_prices", annotations={"title": "Compare OpenSooq Prices", **_ANN})
async def opensooq_compare_prices(params: ComparePricesInput) -> str:
    """Compare median/average prices across several products or variants side by side.

    Runs a price summary for each query and returns them together — for
    "is the 256GB or 512GB cheaper?" / cross-model comparisons.

    Args:
        params (ComparePricesInput): queries (2-5 terms), pages (1-5).

    Returns:
        str: JSON `{comparisons:[{query, currency, priced_listings, min, max,
        average, median}]}` sorted by median ascending. On failure, "Error:".

    Examples:
        - Compare ["iphone 14", "iphone 15", "iphone 16"]
    """
    try:
        comparisons = []
        for q in params.queries:
            listings = await oc.search_multi_page(q, pages=params.pages)
            comparable, _, _ = _comparable_listings(_relevant_listings(q, listings))
            if not comparable:
                comparisons.append({"query": q, "priced_listings": 0, "note": "no comparable priced listings"})
                continue
            stats = _price_stats([l["price"] for l in comparable])
            comparisons.append({
                "query": q,
                "currency": comparable[0]["currency"],
                **_sample_metadata(listings, params.pages),
                "priced_listings": stats["count"], "min": stats["min"], "max": stats["max"],
                "average": stats["average"], "median": stats["median"],
            })
        comparisons.sort(key=lambda c: c.get("median", float("inf")))
        return json.dumps({"comparisons": comparisons}, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# SELLERS & REPUTATION
# ===========================================================================

@mcp.tool(name="opensooq_top_sellers", annotations={"title": "Top OpenSooq Sellers", **_ANN})
async def opensooq_top_sellers(params: TopSellersInput) -> str:
    """Rank the sellers behind a product's listings by volume and reputation.

    Scans pages and groups listings by seller (member_id), ranked by listing
    count then rating — answers "who are the main dealers, and which are
    reputable?".

    Args:
        params (TopSellersInput): query, query_ar, category, pages, limit.

    Returns:
        str: JSON `{query, listings_seen, unique_sellers, sellers:[{member_id,
        display_name, username, is_shop, is_verified, rating_avg, rating_count,
        listing_count, min_price, max_price, currency, sample_listing_url}]}`.
        On failure, a string starting "Error:".

    Examples:
        - "Biggest iPhone 15 dealers on OpenSooq" -> query="iphone 15", pages=3
    """
    try:
        eff = _effective_query(params.query, params.query_ar)
        listings = await oc.search_multi_page(
            eff, pages=params.pages, cat_id=_resolve_category(params.category),
        )
        if not listings:
            return f"No OpenSooq listings found for '{eff}'."
        relevant = _relevant_listings(eff, listings)
        all_sellers = _aggregate_sellers(relevant)
        return json.dumps({
            "query": eff,
            "listings_seen": len(relevant), "unique_sellers": len(all_sellers),
            "returned": min(len(all_sellers), params.limit),
            "sellers": all_sellers[: params.limit],
        }, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_seller_listings", annotations={"title": "Get OpenSooq Seller's Listings", **_ANN})
async def opensooq_seller_listings(params: SellerListingsInput) -> str:
    """List a specific seller's entire inventory, by member_id, with pagination.

    Uses OpenSooq's `member_ids` lookup (clean API) to return only that seller's
    listings — their full catalog, not just items matching some query. Supports
    sorting and paging through the seller's total inventory. Discover member_ids
    via opensooq_top_sellers, a listing's seller object, or opensooq_get_listing.

    Args:
        params (SellerListingsInput): member_id (numeric), sort, page, limit,
            response_format.

    Returns:
        str: JSON `{member_id, page, count, total, total_pages, price_range,
        listings:[...full listing schema...]}` or a markdown digest. `total` is
        the seller's authoritative listing count. On failure, "Error:".

    Examples:
        - "Show everything seller 42899294 is selling, cheapest first" ->
          member_id="42899294", sort="cheapest"
        - "Page 2 of seller 42899294's inventory" -> member_id="42899294", page=2
    """
    try:
        listings, meta = await oc.fetch_member_listings(
            params.member_id, page=params.page, sort=_sort_code(params.sort),
        )
        if not listings:
            return f"No listings found for seller {params.member_id} (page {params.page})."
        if params.limit is not None:
            listings = listings[: params.limit]
        payload = {
            "member_id": params.member_id,
            "page": params.page,
            "count": len(listings),
            "total": meta.get("count"),
            "total_pages": meta.get("pages"),
            "price_range": {"min": meta.get("minimum_price"), "max": meta.get("maximum_price")},
            "listings": listings,
        }
        seller_name = listings[0]["seller"]["display_name"] or params.member_id
        return _render(payload, params.response_format,
                       f"Listings by {seller_name} (member {params.member_id})", meta=meta)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_seller_profile", annotations={"title": "OpenSooq Seller Profile", **_ANN})
async def opensooq_seller_profile(params: SellerProfileInput) -> str:
    """Build a full reputation + inventory profile for a seller by member_id.

    Fetches the seller's actual inventory via the `member_ids` lookup (authoritative
    total from pagination meta), aggregates pricing and the categories they sell
    in, and enriches with the rich seller profile scraped from one of their
    listing pages (member-since date, response time, profile picture, seller page
    link). No search query needed. Discover member_ids via opensooq_top_sellers.

    Args:
        params (SellerProfileInput): member_id (numeric), max_pages (inventory
            pages to scan for stats, 1-5), include_sample.

    Returns:
        str: JSON `{member_id, found, profile:{display_name, is_shop, is_verified,
        verification_level, rating_avg, rating_count, total_listings, member_since,
        response_time, authorised_seller, profile_picture, seller_page}, inventory:
        {scanned, total, currency, min_price, max_price, categories:{name:count}},
        sample_listings:[...]}`. `found` is false for an unknown/empty member.
        On failure, a string starting "Error:".

    Examples:
        - "Profile OpenSooq seller 42899294" -> member_id="42899294"
        - "Is seller 42899294 reputable and what do they sell?" -> member_id="42899294", max_pages=5
    """
    try:
        listings, meta = await oc.fetch_all_member_listings(params.member_id, max_pages=params.max_pages)
        if not listings:
            return json.dumps({
                "member_id": params.member_id, "found": False,
                "note": "No listings found for this member_id (it may be wrong, or the seller has no active listings).",
            }, indent=2, ensure_ascii=False)
        s = listings[0]["seller"]
        prices = [l["price"] for l in listings if l["price"] > 0]
        categories: Dict[str, int] = {}
        for l in listings:
            cat = l["category"] or "Uncategorized"
            categories[cat] = categories.get(cat, 0) + 1
        # Enrich with the rich seller profile from one listing page (best-effort).
        ssr = await oc.fetch_listing_page_details(listings[0]["url"])
        sp = ssr.get("seller_profile") or {}
        profile = {
            "display_name": sp.get("full_name") or s["display_name"],
            "username": s["username"],
            "is_shop": sp.get("is_shop") if sp.get("is_shop") is not None else s["is_shop"],
            "is_verified": s["is_verified"] or bool(sp.get("verification_level")),
            "verification_level": sp.get("verification_level", s["verification_level"]),
            "rating_avg": sp.get("rating_avg") or s["rating_avg"],
            "rating_count": sp.get("rating_count") or s["rating_count"],
            "total_listings": sp.get("total_listings") or meta.get("count"),
            "member_since": sp.get("member_since"),
            "response_time": sp.get("response_time"),
            "authorised_seller": sp.get("authorised_seller"),
            "profile_picture": sp.get("profile_picture"),
            "seller_page": sp.get("member_link"),
            "badges": s["badges"],
        }
        result = {
            "member_id": params.member_id, "found": True,
            "profile": profile,
            "inventory": {
                "scanned": len(listings),
                "total": meta.get("count"),
                "currency": listings[0]["currency"],
                "min_price": min(prices) if prices else 0,
                "max_price": max(prices) if prices else 0,
                "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
            },
        }
        if params.include_sample:
            result["sample_listings"] = listings[:5]
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(name="opensooq_find_trusted_sellers", annotations={"title": "Find Trusted OpenSooq Sellers", **_ANN})
async def opensooq_find_trusted_sellers(params: TrustedSellersInput) -> str:
    """Surface verified / highly-rated sellers for a product (reduce scam risk).

    Scans pages, keeps sellers that are verified (and optionally carry the 'shop'
    badge) with an average rating at or above `min_rating`, ranked by rating then
    volume.

    Args:
        params (TrustedSellersInput): query, query_ar, category, pages, min_rating
            (default 4.0), require_shop.

    Returns:
        str: JSON `{query, criteria, seller_count, sellers:[{member_id,
        display_name, is_shop, is_verified, rating_avg, rating_count,
        listing_count, min_price, max_price, sample_listing_url}]}`.
        On failure, a string starting "Error:".

    Examples:
        - "Trusted iPhone shops rated 4.5+" -> query="iphone", min_rating=4.5, require_shop=True
    """
    try:
        eff = _effective_query(params.query, params.query_ar)
        listings = await oc.search_multi_page(
            eff, pages=params.pages, cat_id=_resolve_category(params.category),
        )
        if not listings:
            return f"No OpenSooq listings found for '{eff}'."
        sellers = _aggregate_sellers(_relevant_listings(eff, listings))
        trusted = [
            s for s in sellers
            if s["is_verified"]
            and s["rating_avg"] >= params.min_rating
            and s["rating_count"] >= params.min_reviews
            and (s["is_shop"] if params.require_shop else True)
        ]
        trusted.sort(key=lambda s: (s["rating_avg"], s["listing_count"]), reverse=True)
        return json.dumps({
            "query": _effective_query(params.query, params.query_ar),
            "criteria": {"min_rating": params.min_rating, "min_reviews": params.min_reviews, "verified": True, "require_shop": params.require_shop},
            "seller_count": len(trusted), "sellers": trusted,
            "note": "Verification and ratings are signals, not a guarantee of seller trustworthiness.",
        }, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


if __name__ == "__main__":
    mcp.run()
