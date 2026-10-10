#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2.0,<2", "httpx>=0.27.0,<1", "pydantic>=2.0.0,<3"]
# ///
"""Read-only OpenSooq MCP tools with conservative matching and sampled analytics."""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from enum import Enum
from functools import wraps
from typing import List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import opensooq_client as oc
from listing_quality import cohort_key, filter_quality
from market import COUNTRY, CURRENCY, PROFILE, normalize_text, resolve_category, resolve_city

mcp = FastMCP("opensooq_jordan_mcp")
_ANN = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class SortOrder(str, Enum):
    RELEVANT = "relevant"
    NEWEST = "newest"
    CHEAPEST = "cheapest"
    PRICIEST = "priciest"


_resolve_category = resolve_category
_resolve_city = resolve_city
_relevant_listings = oc.relevant_listings
_comparable_listings = oc.comparable_listings


class _QueryBase(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid", allow_inf_nan=False
    )

    query: str = Field(
        ...,
        description="Arabic or English search term (e.g. 'iphone 15 pro').",
        min_length=1,
        max_length=200,
    )
    query_ar: Optional[str] = Field(
        default=None,
        description="Optional Arabic term; overrides `query` for the API call (better recall).",
        max_length=200,
    )

    @field_validator("query")
    @classmethod
    def _v(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query cannot be empty")
        return v.strip()


class SearchInput(_QueryBase):
    sort: SortOrder = Field(
        default=SortOrder.RELEVANT, description="Sort order: relevant, newest, cheapest, priciest."
    )
    category: Optional[str] = Field(
        default=None,
        description="Category name or id to scope to (see opensooq_list_categories).",
        max_length=60,
    )
    city: Optional[str] = Field(
        default=None, description="City name or id to scope to (e.g. 'Amman', 59).", max_length=40
    )
    page: int = Field(default=1, description="1-indexed result page.", ge=1, le=50)
    limit: Optional[int] = Field(
        default=None, description="Cap listings returned from the page (1-50).", ge=1, le=50
    )
    min_price: Optional[float] = Field(
        default=None, description="Client-side: drop listings priced below this.", ge=0
    )
    max_price: Optional[float] = Field(
        default=None, description="Client-side: drop listings priced above this.", ge=0
    )
    has_video: bool = Field(
        default=False, description="Client-side: keep only listings with a video."
    )
    has_images: bool = Field(
        default=False, description="Client-side: keep only listings that have images."
    )
    shops_only: bool = Field(default=False, description="Keep only professional 'shop' sellers.")
    verified_only: bool = Field(default=False, description="Keep only verified sellers.")
    min_seller_rating: Optional[float] = Field(
        default=None, description="Drop sellers rated below this (0-5).", ge=0, le=5
    )
    min_seller_reviews: int = Field(
        default=0, ge=0, le=100000, description="Minimum number of seller reviews."
    )
    condition: str = Field(
        default="all",
        pattern="^(all|new|used)$",
        description="Filter explicit new/used condition; unknown condition is excluded when filtering.",
    )
    storage_gb: Optional[int] = Field(
        default=None, ge=1, le=8192, description="Exact known storage capacity in GB."
    )
    include_risky: bool = Field(
        default=False,
        description="Include accessories, damaged items, exchanges and payment offers; inspect quality.risk_flags.",
    )
    strict_match: bool = Field(
        default=False,
        description="Scan up to five relevance pages before applying conservative bilingual product matching.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'."
    )

    @model_validator(mode="after")
    def validate_price_range(self):
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price must not exceed max_price")
        return self


class LatestInput(_QueryBase):
    category: Optional[str] = Field(
        default=None, description="Optional category name/id to scope to.", max_length=60
    )
    city: Optional[str] = Field(
        default=None, description="Optional city name/id to scope to.", max_length=40
    )
    limit: int = Field(
        default=15, description="Number of newest listings to return (1-50).", ge=1, le=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'."
    )


class BrowseCategoryInput(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid", allow_inf_nan=False
    )

    category: str = Field(
        ...,
        description="Category name or id to browse (see opensooq_list_categories).",
        min_length=1,
        max_length=60,
    )
    query: Optional[str] = Field(
        default=None, description="Optional keyword to narrow within the category.", max_length=200
    )
    city: Optional[str] = Field(default=None, description="Optional city name/id.", max_length=40)
    sort: SortOrder = Field(default=SortOrder.RELEVANT, description="Sort order.")
    page: int = Field(default=1, description="1-indexed page.", ge=1, le=50)
    limit: Optional[int] = Field(
        default=None, description="Cap listings returned (1-50).", ge=1, le=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'."
    )


class CategoryBreakdownInput(_QueryBase):
    pass


class _PriceBase(_QueryBase):
    category: Optional[str] = Field(default=None, max_length=60)
    city: Optional[str] = Field(default=None, max_length=40)
    pages: int = Field(default=2, ge=1, le=5)
    condition: str = Field(default="all", pattern="^(all|new|used)$")
    storage_gb: Optional[int] = Field(default=None, ge=1, le=8192)
    include_risky: bool = Field(
        default=False,
        description="Include offers flagged as accessory/payment/damage/multiple variants.",
    )
    min_sample: int = Field(
        default=5,
        ge=3,
        le=100,
        description="Minimum comparable listings per cohort before a deal baseline is used.",
    )


class MarketSummaryInput(_PriceBase):
    pass


class PriceDistributionInput(_PriceBase):
    buckets: int = Field(default=6, ge=2, le=12)


class FindDealsInput(_PriceBase):
    discount_pct: float = Field(default=30, ge=5, le=90)
    verified_only: bool = Field(default=False)
    limit: int = Field(default=15, ge=1, le=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class ComparePricesInput(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid", allow_inf_nan=False
    )

    city: Optional[str] = Field(default=None, max_length=40)
    category: Optional[str] = Field(default=None, max_length=60)
    condition: str = Field(default="all", pattern="^(all|new|used)$")
    storage_gb: Optional[int] = Field(default=None, ge=1, le=8192)
    queries: List[str] = Field(
        ..., description="2-5 search terms to compare price-wise.", min_length=2, max_length=5
    )
    pages: int = Field(default=2, description="Pages to scan per query (1-5).", ge=1, le=5)

    @field_validator("queries")
    @classmethod
    def validate_queries(cls, values):
        if any(not isinstance(q, str) or not q.strip() or len(q) > 200 for q in values):
            raise ValueError("Each comparison query must contain 1-200 characters")
        if len(set(normalize_text(q) for q in values)) != len(values):
            raise ValueError("Comparison queries must be distinct")
        return [q.strip() for q in values]


class TopSellersInput(_QueryBase):
    city: Optional[str] = Field(default=None, max_length=40)
    category: Optional[str] = Field(
        default=None, description="Optional category name/id to scope to.", max_length=60
    )
    pages: int = Field(default=2, description="Result pages to scan (1-5).", ge=1, le=5)
    limit: int = Field(default=15, description="Max sellers to return (1-50).", ge=1, le=50)


class _IdModel(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid", allow_inf_nan=False
    )


class GetListingInput(_IdModel):
    listing_id: str = Field(
        ...,
        description="The listing/post id (digits) — e.g. from a search result's `id`.",
        min_length=1,
        max_length=30,
    )
    include_details: bool = Field(
        default=True,
        description="Also scrape the listing page for the full untruncated description, attribute rows, and the rich seller profile (memberSince, responseTime, total listings). Slightly slower.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'."
    )

    @field_validator("listing_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or int(v) <= 0:
            raise ValueError("listing_id must be the numeric post id (e.g. '281910976').")
        return str(int(v))


class SellerListingsInput(_IdModel):
    member_id: str = Field(
        ...,
        description="The seller's member_id (from a listing's seller object or opensooq_top_sellers).",
        min_length=1,
        max_length=30,
    )
    sort: SortOrder = Field(
        default=SortOrder.RELEVANT, description="Sort order: relevant, newest, cheapest, priciest."
    )
    page: int = Field(
        default=1, description="1-indexed page of the seller's inventory.", ge=1, le=20
    )
    limit: Optional[int] = Field(
        default=None, description="Cap listings returned from the page (1-30).", ge=1, le=30
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'."
    )

    @field_validator("member_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or int(v) <= 0:
            raise ValueError("member_id must be numeric (e.g. '42899294').")
        return str(int(v))


class SellerProfileInput(_IdModel):
    member_id: str = Field(
        ...,
        description="The seller's member_id (from a listing's seller object or opensooq_top_sellers).",
        min_length=1,
        max_length=30,
    )
    max_pages: int = Field(
        default=3,
        description="Pages of the seller's inventory to scan for stats (1-5).",
        ge=1,
        le=5,
    )
    include_sample: bool = Field(
        default=True, description="Include a few sample listings in the response."
    )

    @field_validator("member_id")
    @classmethod
    def _v(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or int(v) <= 0:
            raise ValueError("member_id must be numeric (e.g. '42899294').")
        return str(int(v))


class TrustedSellersInput(_QueryBase):
    city: Optional[str] = Field(default=None, max_length=40)
    category: Optional[str] = Field(
        default=None, description="Optional category name/id to scope to.", max_length=60
    )
    pages: int = Field(default=3, description="Result pages to scan (1-5).", ge=1, le=5)
    min_rating: float = Field(
        default=4.0, description="Minimum average seller rating (0-5).", ge=0, le=5
    )
    min_reviews: int = Field(
        default=5,
        ge=0,
        le=100000,
        description="Minimum seller review count; ratings do not guarantee trustworthiness.",
    )
    require_shop: bool = Field(
        default=False, description="Require the 'shop' badge in addition to verification."
    )


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _safe(function):
    @wraps(function)
    async def call(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except ToolError:
            raise
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            messages = {
                401: "Anonymous authentication was rejected after refresh.",
                403: "OpenSooq blocked access. Retry later; no access bypass is attempted.",
                429: "OpenSooq rate limit reached after bounded retries.",
            }
            raise ToolError(messages.get(status, f"OpenSooq returned HTTP {status}.")) from error
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError) as error:
            raise ToolError("OpenSooq could not be reached after bounded retries.") from error
        except (oc.OpenSooqError, ValueError) as error:
            raise ToolError(str(error)) from error
        except Exception as error:
            raise ToolError(
                f"Unexpected upstream data or processing error ({type(error).__name__})."
            ) from error

    return call


def _effective_query(query, query_ar):
    return query_ar or query


def _sort_code(order):
    return oc.SORT_CODES[order.value]


def _percentile(values, fraction):
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _price_stats(prices):
    values = sorted(p for p in prices if p > 0 and math.isfinite(p))
    if not values:
        return {
            "count": 0,
            **{key: None for key in ("min", "max", "average", "median", "p25", "p75")},
        }
    return {
        "count": len(values),
        "min": values[0],
        "max": values[-1],
        "average": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p25": round(_percentile(values, 0.25), 3),
        "p75": round(_percentile(values, 0.75), 3),
    }


def _sample_metadata(listings, pages):
    return {
        "country": COUNTRY,
        "currency": CURRENCY,
        "pages_requested": pages,
        "scanned_listings": len(listings),
        "sample_note": "Filtered sample of asking prices, not completed sales or the entire market. Unknown specifications remain unknown.",
    }


def _cohorts(listings, min_sample=5):
    grouped = defaultdict(list)
    for listing in listings:
        grouped[cohort_key(listing)].append(listing)
    rows = []
    for key, group in sorted(grouped.items()):
        comparable, _, outliers = _comparable_listings(group)
        stats = _price_stats([listing["price"] for listing in comparable])
        rows.append(
            {
                "cohort": key,
                "listings": len(group),
                "outliers_excluded": outliers,
                **stats,
                "baseline_usable": stats["count"] >= min_sample and "unknown" not in key,
                "specifications_incomplete": "unknown" in key,
            }
        )
    return rows


def _format_listings_md(title, listings, meta=None):
    lines = [f"# {title}", "", f"Showing {len(listings)} listings."]
    if meta and meta.get("count") is not None:
        lines.append(f"Upstream fuzzy result count before local filters: {meta['count']}.")
    for listing in listings:
        price = (
            f"{listing['price']:,.3f} {listing['currency']}"
            if listing["price"] > 0
            else "Price on request"
        )
        seller = listing["seller"]
        lines += [
            "",
            f"## {listing['title']}",
            f"- Price: {price}",
            f"- Location: {listing['location']['city'] or 'Unknown'}",
            f"- Seller: {seller['display_name'] or seller['username'] or 'Unknown'}; rating {seller['rating_avg']:.2f} ({seller['rating_count']} reviews)",
            f"- Link: {listing['url']}",
        ]
        q = listing.get("quality", {})
        lines.append(
            f"- Condition: {q.get('condition', 'unknown')}; storage: {q.get('storage_gb') or 'unknown'} GB"
        )
        if q.get("risk_flags"):
            lines.append("- Offer flags: " + ", ".join(q["risk_flags"]))
        if listing.get("postedDate"):
            lines.append("- Posted: " + listing["postedDate"])
        if listing.get("description"):
            lines.append("- Description: " + listing["description"][:250].replace("\n", " "))
    return "\n".join(lines)


def _render(payload, fmt, title, listings_key="listings", meta=None, note=None):
    if fmt == ResponseFormat.JSON:
        return _json(payload)
    markdown = _format_listings_md(title, payload[listings_key], meta)
    return markdown + "\n\n" + note if note else markdown


def _apply_post_filters(
    listings,
    *,
    min_price=None,
    max_price=None,
    has_video=False,
    has_images=False,
    shops_only=False,
    verified_only=False,
    min_seller_rating=None,
    min_seller_reviews=0,
):
    result = []
    for listing in listings:
        price, seller = listing["price"], listing["seller"]
        if min_price is not None and (price <= 0 or price < min_price):
            continue
        if max_price is not None and (price <= 0 or price > max_price):
            continue
        if has_video and not listing["media"]["hasVideo"]:
            continue
        if has_images and not listing["media"]["imageCount"]:
            continue
        if shops_only and not seller["is_shop"]:
            continue
        if verified_only and not seller["is_verified"]:
            continue
        if min_seller_rating is not None and seller["rating_avg"] < min_seller_rating:
            continue
        if seller["rating_count"] < min_seller_reviews:
            continue
        result.append(listing)
    return result


def _sort_listings(listings, order):
    if order == SortOrder.CHEAPEST:
        return sorted(
            listings, key=lambda listing: listing["price"] if listing["price"] > 0 else float("inf")
        )
    if order == SortOrder.PRICIEST:
        return sorted(listings, key=lambda listing: listing["price"], reverse=True)
    if order == SortOrder.NEWEST:
        return sorted(listings, key=lambda listing: listing["postedDate"] or "", reverse=True)
    return listings


async def _relevance_gated_fetch(query, cat_id, city_id):
    results, seen, meta = [], set(), {}
    async with httpx.AsyncClient() as client:
        for page in range(1, 6):
            items, info = await oc.search_page(
                query,
                page=page,
                cat_id=cat_id,
                city_id=city_id,
                sort=oc.SORT_CODES["relevant"],
                client=client,
            )
            if page == 1:
                meta = info
            if not items:
                break
            added = 0
            for listing in items:
                if listing["id"] not in seen:
                    seen.add(listing["id"])
                    results.append(listing)
                    added += 1
            if not added or (info.get("pages") and page >= info["pages"]):
                break
    return results, meta


async def _price_sample(params):
    query = _effective_query(params.query, params.query_ar)
    raw = await oc.search_multi_page(
        query,
        pages=params.pages,
        cat_id=_resolve_category(params.category),
        city_id=_resolve_city(params.city),
    )
    relevant = _relevant_listings(query, raw)
    selected, reasons = filter_quality(
        relevant,
        condition=params.condition,
        storage_gb=params.storage_gb,
        include_risky=params.include_risky,
    )
    groups = defaultdict(list)
    for listing in selected:
        groups[cohort_key(listing)].append(listing)
    comparable = []
    excluded = 0
    for group in groups.values():
        kept, _, removed = _comparable_listings(group)
        comparable.extend(kept)
        excluded += removed
    median = statistics.median(listing["price"] for listing in comparable) if comparable else 0
    return (
        query,
        raw,
        relevant,
        comparable,
        median,
        {"quality_rejections": reasons, "outliers_excluded": excluded},
    )


@mcp.tool(annotations={"title": "Search OpenSooq Listings", **_ANN})
@_safe
async def opensooq_search_listings(params: SearchInput) -> str:
    """Search Jordan listings with Arabic/English identity matching and explicit offer flags.

    Exact phone generations use exact Pro/Max/Ultra tiers. Unknown specifications
    are excluded when filtering condition or storage. Accessories/payment offers
    are excluded unless include_risky=true. Price sorting covers a bounded sample.
    JSON mode returns an empty listings array for no matches.
    """
    query = _effective_query(params.query, params.query_ar)
    gated = params.strict_match or params.sort in {SortOrder.CHEAPEST, SortOrder.PRICIEST}
    if gated:
        raw, meta = await _relevance_gated_fetch(
            query, _resolve_category(params.category), _resolve_city(params.city)
        )
    else:
        raw, meta = await oc.search_page(
            query,
            page=params.page,
            cat_id=_resolve_category(params.category),
            city_id=_resolve_city(params.city),
            sort=_sort_code(params.sort),
        )
    relevant = _relevant_listings(query, raw)
    listings, reasons = filter_quality(
        relevant,
        condition=params.condition,
        storage_gb=params.storage_gb,
        include_risky=params.include_risky,
    )
    listings = _apply_post_filters(
        listings,
        min_price=params.min_price,
        max_price=params.max_price,
        has_video=params.has_video,
        has_images=params.has_images,
        shops_only=params.shops_only,
        verified_only=params.verified_only,
        min_seller_rating=params.min_seller_rating,
        min_seller_reviews=params.min_seller_reviews,
    )
    if gated:
        listings = _sort_listings(listings, params.sort)
    matching_count = len(listings)
    size = params.limit or 30
    offset = (params.page - 1) * size if gated else 0
    listings = listings[offset : offset + size]
    note = (
        "Sorted within up to five relevance pages, not a marketplace-wide ranking."
        if gated
        else "One upstream page, with local product and offer filtering."
    )
    payload = {
        "query": query,
        "country": COUNTRY,
        "currency": CURRENCY,
        "sort": params.sort.value,
        "page": params.page,
        "page_size": size,
        "count": len(listings),
        "total": meta.get("count"),
        "total_note": "Upstream fuzzy count before local filters.",
        "matched": len(relevant),
        "scanned": len(raw),
        "sample_count": matching_count,
        "sample_note": note,
        "quality_rejections": reasons,
        "listings": listings,
    }
    return _render(payload, params.response_format, f"OpenSooq: {query}", meta=meta, note=note)


@mcp.tool(annotations={"title": "Newest OpenSooq Listings", **_ANN})
@_safe
async def opensooq_latest_listings(params: LatestInput) -> str:
    """Get newest-first product matches from one upstream page; no matches returns an empty array."""
    query = _effective_query(params.query, params.query_ar)
    raw, meta = await oc.search_page(
        query,
        cat_id=_resolve_category(params.category),
        city_id=_resolve_city(params.city),
        sort=oc.SORT_CODES["newest"],
    )
    listings = _relevant_listings(query, raw)[: params.limit]
    return _render(
        {"query": query, "country": COUNTRY, "count": len(listings), "listings": listings},
        params.response_format,
        f"Newest: {query}",
        meta=meta,
    )


@mcp.tool(annotations={"title": "OpenSooq Category Breakdown", **_ANN})
@_safe
async def opensooq_category_breakdown(params: CategoryBreakdownInput) -> str:
    """Return upstream category facets and fuzzy counts for an Arabic/English query."""
    query = _effective_query(params.query, params.query_ar)
    data = await oc.raw_search(query)
    return _json(
        {
            "query": query,
            "country": COUNTRY,
            "total": oc.extract_total(data),
            "categories": oc.extract_category_facets(data),
        }
    )


@mcp.tool(annotations={"title": "Get OpenSooq Listing Detail", **_ANN})
@_safe
async def opensooq_get_listing(params: GetListingInput) -> str:
    """Fetch the exact listing ID and optionally parse structured page description, attributes and seller profile.

    An expired/unknown ID returns found=false; unavailable page details do not
    prevent returning the search API listing. Seller phone stays masked.
    """
    item = await oc.fetch_listing_by_id(params.listing_id)
    if item is None:
        payload = {"found": False, "listing": None, "details": {}, "listing_id": params.listing_id}
        return (
            _json(payload)
            if params.response_format == ResponseFormat.JSON
            else "Listing not found or no longer active."
        )
    details = (
        await oc.fetch_listing_page_details(item["url"], listing_id=item["id"])
        if params.include_details
        else {}
    )
    if (
        details.get("seller_profile")
        and details["seller_profile"].get("member_id") != item["seller"]["member_id"]
    ):
        details = {**details, "seller_profile": None}
    if details.get("full_description"):
        item = {**item, "description": details["full_description"]}
    if params.response_format == ResponseFormat.JSON:
        return _json({"found": True, "listing": item, "details": details})
    markdown = _format_listings_md(f"Listing {item['id']}", [item])
    if details.get("full_description"):
        markdown += "\n\n" + details["full_description"]
    if details.get("attributes"):
        markdown += "\n\n" + "\n".join(
            f"- {a['label']}: {a['value']}" for a in details["attributes"]
        )
    if details.get("seller_profile"):
        seller = details["seller_profile"]
        markdown += f"\n\nSeller since: {seller.get('member_since') or 'unknown'}. Reviews: {seller.get('rating_count') or 0}."
    return markdown


@mcp.tool(annotations={"title": "List OpenSooq Categories", **_ANN})
@_safe
async def opensooq_list_categories() -> str:
    """List the selected market's bundled top-level category IDs, Arabic/English names and snapshot date."""
    rows = [
        {"id": ident, "name_en": m["en"], "name_ar": m["ar"], "url_name": m["url"]}
        for ident, m in oc.CATEGORIES.items()
    ]
    return _json(
        {
            "country": COUNTRY,
            "catalog_source": "bundled snapshot",
            "verified_at": PROFILE["verified_at"],
            "count": len(rows),
            "categories": rows,
        }
    )


@mcp.tool(annotations={"title": "List OpenSooq Cities", **_ANN})
@_safe
async def opensooq_list_cities() -> str:
    """List selected-market city IDs and Arabic/English names; numeric IDs are validated against this catalog."""
    rows = [
        {"id": ident, "name": m["en"], "name_ar": m["ar"], "url_name": m["url"]}
        for ident, m in oc.CITY_DETAILS.items()
    ]
    return _json(
        {
            "country": COUNTRY,
            "catalog_source": "bundled snapshot",
            "verified_at": PROFILE["verified_at"],
            "count": len(rows),
            "cities": rows,
        }
    )


@mcp.tool(annotations={"title": "Browse OpenSooq Category", **_ANN})
@_safe
async def opensooq_browse_category(params: BrowseCategoryInput) -> str:
    """Browse one category page; a supplied product query uses the same matching policy as search."""
    if params.query:
        return await opensooq_search_listings(
            SearchInput(
                query=params.query,
                category=params.category,
                city=params.city,
                sort=params.sort,
                page=params.page,
                limit=params.limit,
                response_format=params.response_format,
            )
        )
    ident = _resolve_category(params.category)
    listings, meta = await oc.search_page(
        "",
        page=params.page,
        cat_id=ident,
        city_id=_resolve_city(params.city),
        sort=_sort_code(params.sort),
    )
    listings = listings[: params.limit] if params.limit else listings
    payload = {
        "category": oc.CATEGORIES.get(ident, {}).get("en", params.category),
        "country": COUNTRY,
        "page": params.page,
        "count": len(listings),
        "total": meta.get("count"),
        "listings": listings,
    }
    return _render(payload, params.response_format, f"Category: {payload['category']}", meta=meta)


@mcp.tool(annotations={"title": "OpenSooq Price Summary", **_ANN})
@_safe
async def opensooq_market_summary(params: MarketSummaryInput) -> str:
    """Summarize a filtered sample of asking prices, with condition/storage/variant cohorts and low-sample warnings.

    Use city, condition and storage_gb to narrow comparisons. Aggregate prices
    can mix cohorts; cohort statistics are more informative. No priced listings
    returns null statistics and priced_listings=0, not a processing failure.
    """
    query, raw, relevant, priced, _, exclusions = await _price_sample(params)
    stats = _price_stats([listing["price"] for listing in priced])
    sorted_prices = sorted(priced, key=lambda listing: listing["price"])
    return _json(
        {
            "query": query,
            **_sample_metadata(raw, params.pages),
            "total_listings": len(raw),
            "relevant_listings": len(relevant),
            "priced_listings": len(priced),
            **exclusions,
            **{k: v for k, v in stats.items() if k != "count"},
            "low_sample": len(priced) < params.min_sample,
            "cohorts": _cohorts(priced, params.min_sample),
            "cheapest_url": sorted_prices[0]["url"] if sorted_prices else None,
            "priciest_url": sorted_prices[-1]["url"] if sorted_prices else None,
        }
    )


@mcp.tool(annotations={"title": "OpenSooq Price Distribution", **_ANN})
@_safe
async def opensooq_price_distribution(params: PriceDistributionInput) -> str:
    """Histogram of filtered asking prices; use condition/storage filters to avoid mixed-specification histograms."""
    query, raw, _, priced, _, exclusions = await _price_sample(params)
    prices = sorted(listing["price"] for listing in priced)
    buckets = []
    if prices:
        lo, hi = prices[0], prices[-1]
        count = 1 if lo == hi else params.buckets
        width = (hi - lo) / count
        for i in range(count):
            low, high = lo + i * width, hi if i == count - 1 else lo + (i + 1) * width
            members = sum(low <= p <= high if i == count - 1 else low <= p < high for p in prices)
            buckets.append(
                {"range_low": round(low, 3), "range_high": round(high, 3), "count": members}
            )
    return _json(
        {
            "query": query,
            **_sample_metadata(raw, params.pages),
            **exclusions,
            "priced_listings": len(prices),
            "min": prices[0] if prices else None,
            "max": prices[-1] if prices else None,
            "buckets": buckets,
            "cohorts": _cohorts(priced, params.min_sample),
        }
    )


@mcp.tool(annotations={"title": "Find OpenSooq Deals", **_ANN})
@_safe
async def opensooq_find_deals(params: FindDealsInput) -> str:
    """Find below-median asking prices within matching variant/storage/condition cohorts.

    Requires min_sample comparable listings per cohort. Accessories, payment
    offers, damaged devices and multi-capacity ads are excluded by default.
    A flagged offer is a candidate to inspect, not a validated bargain.
    """
    query, raw, _, priced, _, exclusions = await _price_sample(params)
    groups = defaultdict(list)
    for listing in priced:
        groups[cohort_key(listing)].append(listing)
    deals = []
    baselines = []
    for key, group in sorted(groups.items()):
        comparable, median, removed = _comparable_listings(group)
        usable = len(comparable) >= params.min_sample and "unknown" not in key
        baselines.append(
            {
                "cohort": key,
                "count": len(comparable),
                "median_price": round(median, 3) if comparable else None,
                "baseline_usable": usable,
                "outliers_excluded": removed,
            }
        )
        if not usable:
            continue
        threshold = median * (1 - params.discount_pct / 100)
        for listing in comparable:
            if listing["price"] <= threshold and (
                not params.verified_only or listing["seller"]["is_verified"]
            ):
                deals.append(
                    {
                        **listing,
                        "discount_pct": round((1 - listing["price"] / median) * 100, 1),
                        "cohort": key,
                        "median_price": round(median, 3),
                        "baseline_count": len(comparable),
                    }
                )
    deals = sorted(deals, key=lambda listing: listing["discount_pct"], reverse=True)[: params.limit]
    note = "Candidates below their cohort median; inspect condition and payment terms. Small cohorts do not produce deals."
    payload = {
        "query": query,
        **_sample_metadata(raw, params.pages),
        **exclusions,
        "baselines": baselines,
        "deal_count": len(deals),
        "deals": deals,
        "note": note,
    }
    return _render(
        payload,
        params.response_format,
        f"Deal candidates: {query}",
        listings_key="deals",
        note=note,
    )


@mcp.tool(annotations={"title": "Compare OpenSooq Prices", **_ANN})
@_safe
async def opensooq_compare_prices(params: ComparePricesInput) -> str:
    """Compare filtered price summaries for 2-5 distinct queries. Failed queries retain an explicit per-query error."""
    comparisons = []
    for query in params.queries:
        try:
            result = await opensooq_market_summary(
                MarketSummaryInput(
                    query=query,
                    pages=params.pages,
                    city=params.city,
                    category=params.category,
                    condition=params.condition,
                    storage_gb=params.storage_gb,
                )
            )
            comparisons.append(json.loads(result))
        except ToolError as error:
            comparisons.append({"query": query, "error": str(error)})
    return _json({"country": COUNTRY, "comparisons": comparisons})


def _aggregate_sellers(listings):
    sellers = {}
    for listing in listings:
        seller = listing["seller"]
        ident = seller["member_id"]
        if not ident:
            continue
        if ident not in sellers:
            sellers[ident] = {
                **{
                    k: seller[k]
                    for k in (
                        "member_id",
                        "display_name",
                        "username",
                        "is_shop",
                        "is_verified",
                        "rating_avg",
                        "rating_count",
                    )
                },
                "listing_count": 0,
                "currency": CURRENCY,
                "sample_listing_url": listing["url"],
                "_prices": [],
            }
        row = sellers[ident]
        row["listing_count"] += 1
        if listing["currency"] == CURRENCY and listing["price"] > 0:
            row["_prices"].append(listing["price"])
    rows = list(sellers.values())
    for row in rows:
        prices = row.pop("_prices")
        row["min_price"] = min(prices) if prices else None
        row["max_price"] = max(prices) if prices else None
    return sorted(rows, key=lambda r: (r["listing_count"], r["rating_avg"]), reverse=True)


@mcp.tool(annotations={"title": "Top OpenSooq Sellers", **_ANN})
@_safe
async def opensooq_top_sellers(params: TopSellersInput) -> str:
    """Rank sellers by matched listing volume in a bounded search sample, then rating. This is not total seller volume."""
    query = _effective_query(params.query, params.query_ar)
    raw = await oc.search_multi_page(
        query,
        pages=params.pages,
        cat_id=_resolve_category(params.category),
        city_id=_resolve_city(params.city),
    )
    relevant = _relevant_listings(query, raw)
    sellers = _aggregate_sellers(relevant)
    return _json(
        {
            "query": query,
            **_sample_metadata(raw, params.pages),
            "listings_seen": len(relevant),
            "unique_sellers": len(sellers),
            "returned": min(len(sellers), params.limit),
            "sellers": sellers[: params.limit],
        }
    )


@mcp.tool(annotations={"title": "Get OpenSooq Seller Listings", **_ANN})
@_safe
async def opensooq_seller_listings(params: SellerListingsInput) -> str:
    """Fetch only the requested seller's listings with server-side sort and pagination. Empty inventory returns an array."""
    listings, meta = await oc.fetch_member_listings(
        params.member_id, page=params.page, sort=_sort_code(params.sort)
    )
    if params.limit:
        listings = listings[: params.limit]
    payload = {
        "member_id": params.member_id,
        "country": COUNTRY,
        "page": params.page,
        "count": len(listings),
        "total": meta.get("count"),
        "total_pages": meta.get("pages"),
        "listings": listings,
    }
    return _render(payload, params.response_format, f"Seller {params.member_id}", meta=meta)


@mcp.tool(annotations={"title": "OpenSooq Seller Profile", **_ANN})
@_safe
async def opensooq_seller_profile(params: SellerProfileInput) -> str:
    """Build seller reputation and sampled inventory statistics; enrich only from a page whose owner ID matches."""
    listings, meta = await oc.fetch_all_member_listings(
        params.member_id, max_pages=params.max_pages
    )
    if not listings:
        return _json(
            {
                "member_id": params.member_id,
                "found": False,
                "profile": None,
                "inventory": {"scanned": 0, "total": meta.get("count")},
            }
        )
    seller = listings[0]["seller"]
    details = await oc.fetch_listing_page_details(listings[0]["url"], listing_id=listings[0]["id"])
    enriched = details.get("seller_profile") or {}
    if str(enriched.get("member_id")) != params.member_id:
        enriched = {}
    mapping = {
        "display_name": "full_name",
        "rating_avg": "rating_avg",
        "rating_count": "rating_count",
        "is_shop": "is_shop",
        "verification_level": "verification_level",
    }
    profile = {**seller}
    profile.pop("phone_masked", None)
    for dest, source in mapping.items():
        if enriched.get(source) is not None:
            profile[dest] = enriched[source]
    profile.update(
        {
            key: enriched.get(key)
            for key in (
                "member_since",
                "response_time",
                "authorised_seller",
                "profile_picture",
                "member_link",
            )
        }
    )
    profile["total_listings"] = (
        enriched.get("total_listings")
        if enriched.get("total_listings") is not None
        else meta.get("count")
    )
    prices = [
        listing["price"]
        for listing in listings
        if listing["price"] > 0 and listing["currency"] == CURRENCY
    ]
    categories = defaultdict(int)
    for listing in listings:
        categories[listing["category"] or "Uncategorized"] += 1
    result = {
        "member_id": params.member_id,
        "found": True,
        "profile": profile,
        "inventory": {
            "scanned": len(listings),
            "total": meta.get("count"),
            "currency": CURRENCY,
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "categories": dict(categories),
            "sample_note": "Inventory statistics cover only scanned pages.",
        },
    }
    if params.include_sample:
        result["sample_listings"] = listings[:5]
    return _json(result)


@mcp.tool(annotations={"title": "Find Rated Verified OpenSooq Sellers", **_ANN})
@_safe
async def opensooq_find_trusted_sellers(params: TrustedSellersInput) -> str:
    """Filter verified sellers by minimum rating/review count and optional shop badge. Signals do not guarantee trustworthiness."""
    query = _effective_query(params.query, params.query_ar)
    raw = await oc.search_multi_page(
        query,
        pages=params.pages,
        cat_id=_resolve_category(params.category),
        city_id=_resolve_city(params.city),
    )
    sellers = _aggregate_sellers(_relevant_listings(query, raw))
    filtered = [
        seller
        for seller in sellers
        if seller["is_verified"]
        and seller["rating_avg"] >= params.min_rating
        and seller["rating_count"] >= params.min_reviews
        and (not params.require_shop or seller["is_shop"])
    ]
    filtered.sort(key=lambda seller: (seller["rating_avg"], seller["rating_count"]), reverse=True)
    return _json(
        {
            "query": query,
            "country": COUNTRY,
            "criteria": {
                "min_rating": params.min_rating,
                "min_reviews": params.min_reviews,
                "verified": True,
                "require_shop": params.require_shop,
            },
            "seller_count": len(filtered),
            "sellers": filtered,
            "note": "Verification and ratings are signals, not a guarantee of seller trustworthiness.",
        }
    )


if __name__ == "__main__":
    mcp.run()
