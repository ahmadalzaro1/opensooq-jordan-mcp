#!/usr/bin/env python3
"""
OpenSooq (Jordan by default) API client.

Pure, dependency-light client for OpenSooq's private `serp/search/v2` endpoint
and related routes. Contains NO MCP/Pydantic imports so it can be unit-tested on
any Python with `httpx` installed, and reused by the MCP server layer.

OpenSooq publishes no official API, so this replicates the browser handshake:

  1. Register an anonymous device (`/api/register-device`) -> (deviceUUID, t, k).
  2. Mint a short-lived HS256 JWT whose signing key is *derived* from `k` via a
     two-stage HMAC-SHA256 chain (the scheme the web bundle uses).
  3. Send the cookie + header set the SERP endpoint expects (configured country/currency/desktop).
  4. POST queries and normalize the raw payload into a stable schema.

Faithful port of `backend-worker/src/services/opensooq.ts`. Read-only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import random
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import httpx

from listing_quality import quality
from listing_quality import relevant_listings as relevant_listings
from market import (
    BASE_URL,
    COUNTRY,
    CURRENCY,
    LANGUAGE,
    normalize_text,
)
from market import CATEGORIES as CATEGORIES
from market import CITIES as CITIES
from market import CITY_DETAILS as CITY_DETAILS
from page_parser import extract_details

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REGISTER_URL = f"{BASE_URL}/api/register-device?abBucket=6"
SEARCH_URL = f"{BASE_URL}/api/serp/search/v2?abBucket=6&Action_screen_name=search"
IMAGE_HOST = "https://opensooq-images.os-cdn.com"
IMAGE_CDN = f"{IMAGE_HOST}/previews/640x480"
IMAGE_SIZE = "640x480"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
RELEASE_VERSION = "9.4.02"
PLATFORM = "desktop"

REQUEST_TIMEOUT_S = 10.0
JWT_TTL_S = 300
_REG_REUSE_S = 240

# Verified sort codes accepted by the SERP body field `sort_code`.
SORT_CODES: Dict[str, str] = {
    "relevant": "default",
    "newest": "recent",
    "cheapest": "price-",  # price ascending
    "priciest": "price+",  # price descending
}


class OpenSooqError(RuntimeError):
    """Raised for non-recoverable OpenSooq API failures."""


# ---------------------------------------------------------------------------
# Device registration + JWT signing
# ---------------------------------------------------------------------------


class _Registration:
    __slots__ = ("device_uuid", "t", "k", "expires_at")

    def __init__(self, device_uuid: str, t: Any, k: str) -> None:
        self.device_uuid = device_uuid
        self.t = t
        self.k = k
        self.expires_at = time.time() + _REG_REUSE_S

    def is_valid(self) -> bool:
        return time.time() < self.expires_at


_cached_reg: Optional[_Registration] = None
_reg_lock = asyncio.Lock()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _json_b64url(obj: Dict[str, Any]) -> str:
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _generate_jwt(reg: _Registration) -> str:
    """Mint the HS256 JWT the SERP endpoint expects (derived-key scheme)."""
    audience_secret = ""
    rnd = "".join(str(random.randint(0, 9)) for _ in range(10))
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "at0": reg.t,
        "aud": PLATFORM,
        "sub": reg.device_uuid,
        "rnd": rnd,
        "exp": now + JWT_TTL_S,
    }
    header_b64 = _json_b64url(header)
    payload_b64 = _json_b64url(payload)
    template = f"{PLATFORM}.{audience_secret}.{rnd}.{reg.t}"
    derived_key = _b64url(_hmac_sha256(reg.k.encode("utf-8"), template))
    token_data = f"{header_b64}.{payload_b64}"
    signature_b64 = _b64url(_hmac_sha256(derived_key.encode("utf-8"), token_data))
    return f"{token_data}.{signature_b64}"


def _build_cookie_header(reg: _Registration, session_id: str) -> str:
    auth_v2 = quote(
        json.dumps(
            {"deviceUUID": reg.device_uuid, "t": reg.t, "k": reg.k},
            separators=(",", ":"),
        )
    )
    session = quote(
        json.dumps(
            {"id": session_id, "startedAt": int(time.time() * 1000)},
            separators=(",", ":"),
        )
    )
    return "; ".join(
        [
            f"auth_v2={auth_v2}",
            f"device_uuid={reg.device_uuid}",
            f"session={session}",
            f"ecountry={COUNTRY}",
            f"default_currency={CURRENCY}",
            f"source={PLATFORM}",
            f"NEXT_LOCALE={LANGUAGE}",
            "userABBucket=6",
        ]
    )


_request_lock = asyncio.Lock()
_last_request_at = 0.0


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Space API calls and retry only throttling/transient server errors, twice."""
    global _last_request_at
    for attempt in range(3):
        async with _request_lock:
            wait = 0.3 - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request_at = time.monotonic()
            try:
                response = await client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 2:
                    raise
                response = None
        if response is None:
            await asyncio.sleep(0.3 * (2**attempt))
            continue
        if response.status_code not in {429, 502, 503, 504} or attempt == 2:
            return response
        retry_after = response.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after is not None else 2**attempt
        except ValueError:
            try:
                when = parsedate_to_datetime(retry_after)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = 2**attempt
        delay = max(0.3, delay)
        if delay > 10 or not math.isfinite(delay):
            return response  # respect long Retry-After without blocking a tool indefinitely
        await asyncio.sleep(delay)
    raise OpenSooqError("Request retry budget exhausted")


async def _register_device(client: httpx.AsyncClient) -> _Registration:
    device_uuid = str(uuid.uuid4())
    resp = await _request(
        client,
        "POST",
        REGISTER_URL,
        headers={
            "content-type": "application/json",
            "origin": BASE_URL,
            "user-agent": USER_AGENT,
        },
        json={"platform": PLATFORM, "deviceUUID": device_uuid},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("deviceUUID"), str)
            or not isinstance(data.get("k"), str)
            or "t" not in data
        ):
            raise ValueError("invalid registration fields")
    except ValueError as error:
        raise OpenSooqError(
            "Unexpected device-registration response; authentication schema may have changed"
        ) from error
    return _Registration(device_uuid=data["deviceUUID"], t=data["t"], k=data["k"])


async def _get_registration(client: httpx.AsyncClient, force: bool = False) -> _Registration:
    global _cached_reg
    async with _reg_lock:
        if not force and _cached_reg is not None and _cached_reg.is_valid():
            return _cached_reg
        _cached_reg = await _register_device(client)
        return _cached_reg


def _search_headers(reg: _Registration, jwt: str, session_id: str) -> Dict[str, str]:
    return {
        "authorization": f"Bearer {jwt}",
        "cookie": _build_cookie_header(reg, session_id),
        "content-type": "application/json",
        "accept": "*/*",
        "accept-language": LANGUAGE,
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/{LANGUAGE}",
        "country": COUNTRY,
        "currency": CURRENCY,
        "x-domain-theme": "opensooq",
        "release-version": RELEASE_VERSION,
        "source": PLATFORM,
        "x-tracking-uuid": reg.device_uuid,
        "user-agent": USER_AGENT,
    }


# ---------------------------------------------------------------------------
# Raw search (generic — accepts arbitrary filters/sort)
# ---------------------------------------------------------------------------


async def raw_search(
    query: str = "",
    *,
    page: int = 1,
    filters: Optional[Dict[str, Any]] = None,
    sort: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """POST a search body and return the raw decoded JSON response.

    `filters` is OpenSooq's filter object (e.g. category ids, price range).
    `sort` is a sort code string (see SORT_CODES). `extra` merges into the body.
    Retries once with a fresh device registration on a 401.
    """
    if not 1 <= page <= 50:
        raise ValueError("page must be between 1 and 50")
    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        body: Dict[str, Any] = {"filters": filters or {}, "query": query, "page": page}
        if sort:
            # Verified body key is `sort_code` (accepts default/recent/price-/price+).
            body["sort_code"] = sort
        if extra:
            body.update(extra)

        for attempt in (0, 1):
            reg = await _get_registration(client, force=(attempt == 1))
            jwt = _generate_jwt(reg)
            session_id = f"{uuid.uuid4().hex[:12]}-{uuid.uuid4().hex[:12]}-{uuid.uuid4()}"
            resp = await _request(
                client,
                "POST",
                SEARCH_URL,
                headers=_search_headers(reg, jwt, session_id),
                json=body,
                timeout=REQUEST_TIMEOUT_S,
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # stale registration; retry once with a fresh device
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as error:
                raise OpenSooqError("OpenSooq returned non-JSON search data") from error
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("listings"), dict)
                or not isinstance(data["listings"].get("items"), list)
            ):
                raise OpenSooqError(
                    "Unexpected search response; OpenSooq API schema may have changed"
                )
            return data
        raise OpenSooqError("Authentication failed after retry (401).")
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Targeted lookups (verified: post_ids / member_ids are top-level body fields)
# ---------------------------------------------------------------------------


async def fetch_listing_by_id(
    listing_id: Any,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch a single listing by id via the serp `post_ids` filter.

    Returns the normalized listing (same schema as search) or None if not found
    / no longer active. Verified: `{"post_ids":[<int>]}` at the body top level
    returns exactly that listing.
    """
    try:
        pid = int(listing_id)
    except (TypeError, ValueError):
        return None
    data = await raw_search("", extra={"post_ids": [pid]}, client=client)
    items = extract_items(data)
    return next((normalize_item(it) for it in items if str(it.get("id")) == str(pid)), None)


async def fetch_member_listings(
    member_id: Any,
    *,
    page: int = 1,
    sort: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch one page of a seller's listings via the serp `member_ids` filter.

    Returns (normalized listings, pagination meta). Verified: `{"member_ids":
    [<int>]}` at the body top level returns only that member's listings, with
    `listings.meta` carrying the seller's total count / page count / price range.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return [], {}
    data = await raw_search("", page=page, sort=sort, extra={"member_ids": [mid]}, client=client)
    return [
        normalize_item(it) for it in extract_items(data) if str(it.get("member_id")) == str(mid)
    ], extract_meta(data)


async def fetch_all_member_listings(
    member_id: Any,
    *,
    max_pages: int = 5,
    sort: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch up to `max_pages` pages of a seller's listings. Returns (items, meta)."""
    out: List[Dict[str, Any]] = []
    if not 1 <= max_pages <= 5:
        raise ValueError("max_pages must be between 1 and 5")
    seen = set()
    first_meta: Dict[str, Any] = {}
    async with httpx.AsyncClient() as client:
        for page in range(1, max_pages + 1):
            items, meta = await fetch_member_listings(
                member_id, page=page, sort=sort, client=client
            )
            if page == 1:
                first_meta = meta
            if not items:
                break
            for item in items:
                if item["id"] not in seen:
                    out.append(item)
                    seen.add(item["id"])
            if meta.get("pages") and page >= meta["pages"]:
                break
    return out, first_meta


# ---------------------------------------------------------------------------
# SSR listing-page enrichment (best-effort; the JSON detail API needs login)
#
# The listing detail page is App-Router RSC: data streams in `self.__next_f`
# chunks. We decode them and harvest the extras the search payload lacks — full
# untruncated description, attribute rows, and the rich seller profile
# (memberSince / responseTime / numberOfListings / profilePicture). Every step
# is defensive: any parse failure yields {}/None so callers degrade gracefully.
# ---------------------------------------------------------------------------


async def fetch_listing_page_details(
    post_url: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    listing_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Scrape the SSR listing page for extras the search payload lacks.

    Returns `{full_description, attributes, seller_profile}` (any of which may be
    None/empty). Never raises — returns {} on any network/parse failure so the
    caller can fall back to the API-level listing data.
    """
    if not post_url:
        return {}
    url = urljoin(BASE_URL, post_url)
    if urlparse(url).scheme != "https" or urlparse(url).netloc != urlparse(BASE_URL).netloc:
        return {}
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    try:
        for hop in range(4):
            resp = await _request(
                client,
                "GET",
                url,
                headers={"user-agent": USER_AGENT, "accept-language": LANGUAGE},
                timeout=REQUEST_TIMEOUT_S,
                follow_redirects=False,
            )
            if resp.status_code not in {301, 302, 303, 307, 308}:
                break
            target = urljoin(url, resp.headers.get("location", ""))
            parsed = urlparse(target)
            if parsed.scheme != "https" or parsed.netloc != urlparse(BASE_URL).netloc:
                return {}
            url = target
        if resp.status_code != 200:
            return {}
        return extract_details(resp.text, BASE_URL, listing_id=listing_id)
    except Exception:
        return {}
    finally:
        if own:
            await client.aclose()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_price(price_amount: Any) -> float:
    """Parse JOD decimals, Arabic digits and thousands separators; reject invalid prices."""
    if price_amount is None:
        return 0.0
    text = normalize_text(str(price_amount)).replace("٬", "").replace("٫", ".")
    match = re.search(r"-?\d[\d,.]*", text)
    if not match:
        return 0.0
    try:
        value = float(match.group(0).replace(",", ""))
        return value if math.isfinite(value) and value >= 0 else 0.0
    except ValueError:
        return 0.0


_REL_UNITS = {
    "hour": ("ساعة", "ساعات"),
    "minute": ("دقيقة", "دقائق", "دقيقه"),
    "day": ("يوم", "أيام", "ايام"),
}


def parse_posted_date(posted_at: Any) -> Optional[datetime]:
    now = datetime.now(timezone.utc)
    if not posted_at:
        return None
    lower = normalize_text(str(posted_at))
    try:
        parsed = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
    except ValueError:
        pass

    def _first_int(s: str) -> Optional[int]:
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else None

    if any(tok in lower for tok in ("hour",) + _REL_UNITS["hour"]):
        n = _first_int(lower)
        return now - timedelta(hours=n) if n else now
    if any(tok in lower for tok in ("minute",) + _REL_UNITS["minute"]):
        n = _first_int(lower)
        return now - timedelta(minutes=n) if n else now
    if any(tok in lower for tok in ("day",) + _REL_UNITS["day"]):
        n = _first_int(lower)
        return now - timedelta(days=n) if n else now
    if "today" in lower or "اليوم" in lower:
        return now
    if "yesterday" in lower or "أمس" in lower or "امس" in lower:
        return now - timedelta(days=1)

    parts = re.split(r"[-/]", lower)
    if len(parts) == 3:
        try:
            day, month, year = (int(p) for p in parts)
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _build_image_url(item: Dict[str, Any]) -> Optional[str]:
    if item.get("image_uri"):
        return f"{IMAGE_CDN}/{item['image_uri']}.webp"
    raw = item.get("image") or item.get("thumb")
    if isinstance(raw, str):
        return raw if raw.startswith("http") else f"{IMAGE_CDN}/{raw}.webp"
    return None


def _resolve_gallery(item: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for path in item.get("images") or []:
        if isinstance(path, str) and path:
            urls.append(
                path
                if path.startswith("http")
                else f"{IMAGE_HOST}{path.replace('{size}', IMAGE_SIZE)}"
            )
    if not urls:
        primary = _build_image_url(item)
        if primary:
            urls.append(primary)
    return urls


def _build_listing_url(item: Dict[str, Any]) -> str:
    rel = item.get("post_url") or item.get("share_deep_link") or ""
    if rel:
        return rel if rel.startswith("http") else f"{BASE_URL}{rel}"
    return f"{BASE_URL}/en/post/{item.get('id')}"


def _number(value: Any, *, integer: bool = False) -> Any:
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return 0
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return 0


def _boolean(value: Any) -> bool:
    return (
        value is True or value == 1 or (isinstance(value, str) and value.lower() in {"true", "1"})
    )


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _attributes(item: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for key in ("highlights", "highlightsObject", "ai_tags"):
        values = item.get(key)
        if isinstance(values, dict):
            values = list(values.values())
        if isinstance(values, list):
            for row in values:
                if isinstance(row, dict):
                    label = row.get("label") or row.get("name") or row.get("key")
                    value = row.get("value")
                    if isinstance(label, str) and isinstance(value, (str, int, float)):
                        rows.append({"label": label, "value": str(value)})
    return rows


def build_seller(item: Dict[str, Any]) -> Dict[str, Any]:
    badges = item.get("member_badges") or []
    badges = [x for x in badges if isinstance(x, str)] if isinstance(badges, list) else []
    verification = _number(
        item.get("verification_level") or item.get("verification_level_i"), integer=True
    )
    return {
        "member_id": str(item.get("member_id")) if item.get("member_id") else None,
        "display_name": item.get("member_display_name"),
        "username": item.get("member_user_name"),
        "avatar_uri": item.get("member_avatar_uri"),
        "badges": badges,
        "is_shop": "shop" in badges,
        "rating_avg": min(5.0, _number(item.get("member_rating_avg"))),
        "rating_count": _number(item.get("member_rating_count"), integer=True),
        "is_verified": bool(verification),
        "verification_level": verification,
        "phone_masked": item.get("phone_number"),
        "has_phone": _boolean(item.get("has_phone")),
    }


def _bottom_tag_labels(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for tag in item.get("bottom_tags") or []:
        if isinstance(tag, dict) and tag.get("label"):
            out.append(tag["label"])
    return out


def normalize_item(item: Dict[str, Any], fallback_title: str = "") -> Dict[str, Any]:
    gallery = _resolve_gallery(item)
    posted = parse_posted_date(item.get("posted_at"))
    result = {
        "id": str(item.get("id") or ""),
        "title": _text(item.get("title")) or fallback_title,
        "description": _text(item.get("masked_description")),
        "price": parse_price(item.get("price_amount")),
        "currency": item.get("price_currency_iso") or CURRENCY,
        "postedDate": posted.isoformat() if posted else None,
        "insertedDate": item.get("inserted_date"),
        "expiresAt": item.get("expired_at"),
        "platform": "OpenSooq",
        "category": _text(item.get("cat2_label")) or _text(item.get("cat1_label")) or "",
        "categoryId": item.get("cat2_code") or item.get("cat1_code"),
        "attributes": _attributes(item),
        "categoryPath": [c for c in (item.get("cat1_label"), item.get("cat2_label")) if c],
        "location": {
            "city": item.get("city_label"),
            "neighborhood": item.get("nhood_label"),
        },
        "status": item.get("listing_status"),
        "isActive": _boolean(item.get("is_active")),
        "media": {
            "imageUrl": gallery[0] if gallery else None,
            "imageCount": _number(item.get("image_count"), integer=True) or len(gallery),
            "gallery": gallery,
            "hasVideo": _boolean(item.get("has_video")),
            "has360": _boolean(item.get("has_360")),
        },
        "tags": _bottom_tag_labels(item),
        "seller": build_seller(item),
        "url": _build_listing_url(item),
    }
    result["quality"] = quality(result)
    return result


def extract_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = (data or {}).get("listings", {}).get("items", []) or []
    return [item for item in values if isinstance(item, dict) and str(item.get("id", "")).isdigit()]


def extract_meta(data: Dict[str, Any]) -> Dict[str, Any]:
    """Pagination metadata from `listings.meta` (count, per_page, pages, current_page)."""
    meta = (data or {}).get("listings", {}).get("meta", {}) or {}
    if not isinstance(meta, dict):
        raise OpenSooqError("Unexpected pagination metadata")
    return {
        **meta,
        **{
            key: _number(meta[key], integer=True)
            for key in ("count", "pages", "per_page", "current_page")
            if key in meta
        },
    }


def extract_total(data: Dict[str, Any]) -> Optional[int]:
    meta = extract_meta(data)
    return meta.get("count") if isinstance(meta.get("count"), int) else None


def extract_category_facets(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-query category breakdown from `facets.items` (label, cat1_id, count)."""
    out: List[Dict[str, Any]] = []
    for f in (data or {}).get("facets", {}).get("items", []) or []:
        if isinstance(f, dict):
            out.append(
                {
                    "category": f.get("label"),
                    "category_ar": f.get("opposite_label"),
                    "cat1_id": f.get("cat1_id"),
                    "count": f.get("count"),
                    "url": f.get("href"),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Relevance filtering (shared by the MCP server and the price watcher)
#
# OpenSooq returns loosely-related fallback listings rather than "no match", so a
# search for "samsung galaxy s26 ultra" also surfaces Galaxy A52s / S24 Ultra
# (shared brand words) and 1-KWD junk. The model number is the real discriminator:
# require every query number as a standalone value ("26" matches "s26" but not
# "256"/"326"; language-neutral, so Arabic titles work), plus any model-tier
# modifier the query names (ultra/pro/max/..., bilingual).
# ---------------------------------------------------------------------------

# Accessories (1-KWD cases) and troll prices ("100 billion") or decoy teasers
# (9.99 installment hooks) wreck price math. The median is robust (junk is a
# minority), so keep listings within a multiplicative band of it.
COMPARABLE_LO_FRAC = 0.2  # < 20% of median  -> almost certainly not the same item
COMPARABLE_HI_FRAC = 5.0  # > 5x  of median  -> bundle / typo / different item


def comparable_listings(
    listings: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], float, int]:
    """Return (priced listings within the median price-band, median, excluded_count).

    Drops accessories / decoys / troll prices so the survivors are plausibly the
    same product. Falls back to all priced listings when the sample is too small
    (<4) to estimate a reliable median.
    """
    priced = [
        listing
        for listing in listings
        if listing["price"] > 0
        and math.isfinite(listing["price"])
        and listing.get("currency", CURRENCY) == CURRENCY
    ]
    if len(priced) < 4:
        med = statistics.median([listing["price"] for listing in priced]) if priced else 0.0
        return priced, med, 0
    med = statistics.median([listing["price"] for listing in priced])
    lo, hi = med * COMPARABLE_LO_FRAC, med * COMPARABLE_HI_FRAC
    kept = [listing for listing in priced if lo <= listing["price"] <= hi]
    if not kept:
        return priced, med, 0
    return kept, statistics.median([listing["price"] for listing in kept]), len(priced) - len(kept)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def _build_filters(
    *,
    cat_id: Optional[int] = None,
    city_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the verified server-side filter object (category/city)."""
    filters: Dict[str, Any] = {}
    if cat_id is not None:
        filters["cat_ids"] = [cat_id]
    if city_id is not None:
        filters["city_ids"] = [city_id]
    return filters


async def search_page(
    query: str,
    *,
    page: int = 1,
    cat_id: Optional[int] = None,
    city_id: Optional[int] = None,
    sort: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Search one page; return (normalized listings, pagination meta)."""
    data = await raw_search(
        query,
        page=page,
        filters=_build_filters(cat_id=cat_id, city_id=city_id),
        sort=sort,
        client=client,
    )
    items = [normalize_item(it, query) for it in extract_items(data)]
    return items, extract_meta(data)


async def search_multi_page(
    query: str,
    *,
    pages: int = 1,
    cat_id: Optional[int] = None,
    city_id: Optional[int] = None,
    sort: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Search across multiple pages, stopping early when a page is empty."""
    if not 1 <= pages <= 5:
        raise ValueError("pages must be between 1 and 5")
    out: List[Dict[str, Any]] = []
    seen = set()
    async with httpx.AsyncClient() as client:
        for page in range(1, pages + 1):
            items, meta = await search_page(
                query, page=page, cat_id=cat_id, city_id=city_id, sort=sort, client=client
            )
            if not items:
                break
            for item in items:
                if item["id"] not in seen:
                    seen.add(item["id"])
                    out.append(item)
            if meta.get("pages") and page >= meta["pages"]:
                break
    return out
