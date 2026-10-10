"""Parse JSON embedded in Next.js pages without depending on JSON key order."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


def decode_flight(html):
    chunks = []
    for match in _FLIGHT.finditer(html):
        try:
            chunks.append(json.loads(match.group(1)))
        except ValueError:
            continue
    return "".join(chunks)


def _walk(value, depth=0):
    if depth > 40:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, depth + 1)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            yield from _walk(json.loads(value), depth + 1)
        except (ValueError, RecursionError):
            pass


def embedded_objects(html):
    parser = _Scripts()
    parser.feed(html)
    decoder = json.JSONDecoder()
    for script in parser.scripts:
        try:
            yield from _walk(json.loads(script))
        except (ValueError, RecursionError):
            pass
    for line in decode_flight(html).splitlines():
        # Flight rows prefix JSON with a hexadecimal row ID and an optional type tag.
        payload = re.sub(r"^[0-9a-f]+:[A-Z]*", "", line)
        if payload.startswith(("{", "[")):
            try:
                value, _ = decoder.raw_decode(payload)
                yield from _walk(value)
            except (ValueError, RecursionError):
                continue


def extract_details(html, base_url, listing_id=None):
    objects = list(embedded_objects(html))
    candidates = [
        o
        for o in objects
        if isinstance(o.get("description"), dict) and "content" in o["description"]
    ]
    if listing_id is not None:
        exact = [
            o
            for o in candidates
            if str(o.get("id") or o.get("postId") or o.get("listingId")) == str(listing_id)
        ]
        if exact:
            candidates = exact
        elif any(o.get("id") or o.get("postId") or o.get("listingId") for o in candidates):
            return {
                "full_description": None,
                "attributes": [],
                "seller_profile": None,
                "enrichment_status": "listing_mismatch",
            }
    # Prefer the listing object with its ownerData over unrelated SEO descriptions.
    obj = next(
        (o for o in candidates if isinstance(o.get("ownerData"), dict)),
        candidates[0] if candidates else {},
    )
    description = obj.get("description", {}).get("content")
    if not description:
        descriptions = [
            o.get("description")
            for o in objects
            if isinstance(o.get("description"), str) and ("@type" in o or "postId" in o)
        ]
        description = max(descriptions, key=len, default=None)
    attrs = []
    seen = set()
    for o in _walk(obj) if obj else objects:
        if "key_type" in o and isinstance(o.get("label"), str) and isinstance(o.get("value"), str):
            key = (o["label"], o["value"])
            if key not in seen:
                seen.add(key)
                attrs.append({"type": o["key_type"], "label": o["label"], "value": o["value"]})
    owner = obj.get("ownerData")
    if not isinstance(owner, dict):
        owner = next(
            (o for o in objects if "memberSince" in o and "numberOfRatings" in o and "id" in o),
            None,
        )
    seller = None
    if owner:
        seller = {
            "member_id": str(owner.get("id")),
            "full_name": owner.get("fullName"),
            "rating_avg": owner.get("ratingAvg"),
            "rating_count": owner.get("numberOfRatings"),
            "total_listings": owner.get("numberOfListings"),
            "member_since": owner.get("memberSince"),
            "response_time": owner.get("responseTime"),
            "is_shop": owner.get("isShop"),
            "authorised_seller": owner.get("authorisedSeller"),
            "verification_level": owner.get("verificationLevel"),
            "profile_picture": owner.get("profilePicture"),
            "member_link": urljoin(base_url, owner["memberLink"])
            if owner.get("memberLink")
            else None,
        }
    return {
        "full_description": description,
        "attributes": attrs,
        "seller_profile": seller,
        "enrichment_status": "available" if description or attrs or seller else "unavailable",
    }
