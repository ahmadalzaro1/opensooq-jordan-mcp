#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27.0,<1"]
# ///
"""Alert on new affordable listings and further price drops on known listings.

Run with uv run watch.py --query 'ايفون 15 برو' --max-price 400 --city عمان.
Exit 2: alerts; 0: unchanged; 1: failure. No scheduler is installed by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

import opensooq_client as oc
from listing_quality import filter_quality
from local_state import atomic_json, exclusive_lock
from market import resolve_city

HERE = Path(__file__).resolve().parent


async def find_hits(args):
    raw = await oc.search_multi_page(
        args.query_ar or args.query,
        pages=args.pages,
        city_id=resolve_city(args.city),
        sort=oc.SORT_CODES["relevant"],
    )
    matches, _ = filter_quality(
        oc.relevant_listings(args.query_ar or args.query, raw),
        condition=args.condition,
        storage_gb=args.storage_gb,
        include_risky=args.include_risky,
    )
    # Threshold watches retain genuinely cheap matches rather than applying a median cut.
    matches = [listing for listing in matches if listing["currency"] == oc.CURRENCY]
    return sorted(
        [
            listing
            for listing in matches
            if 0 < listing["price"] <= args.max_price
            and (not args.verified_only or listing["seller"]["is_verified"])
            and (not args.shops_only or listing["seller"]["is_shop"])
        ],
        key=lambda listing: listing["price"],
    )


def select_alerts(hits, state):
    """Track the last notified price, so unchanged hits never alert twice."""
    prices = dict(state.get("prices", {}))
    legacy = set(state.get("legacy_seen", []))
    alerts = []
    seen = set()
    for hit in hits:
        if hit["id"] in seen:
            continue
        seen.add(hit["id"])
        ident, price = hit["id"], hit["price"]
        previous = prices.get(ident)
        if previous is None and ident not in legacy:
            alerts.append({**hit, "alert_type": "new_match", "previous_price": None})
            prices[ident] = price
        elif previous is not None and price < previous:
            alerts.append({**hit, "alert_type": "price_drop", "previous_price": previous})
            prices[ident] = price
        elif previous is None:
            prices[ident] = price  # establish a baseline when migrating old seen-ID state
    return alerts, {
        "version": 2,
        "country": oc.COUNTRY,
        "prices": prices,
        "legacy_seen": sorted(legacy - prices.keys()),
    }


def load_state(path):
    if not path.exists():
        return {"version": 2, "country": oc.COUNTRY, "prices": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        if any(not isinstance(ident, str) or not ident.isdigit() for ident in value):
            raise ValueError("Invalid legacy watcher IDs")
        return {"version": 2, "country": oc.COUNTRY, "prices": {}, "legacy_seen": value}
    if (
        not isinstance(value, dict)
        or value.get("version") != 2
        or value.get("country") != oc.COUNTRY
    ):
        raise ValueError(
            "Unsupported watcher state or different country; use a separate --state file"
        )
    prices = value.get("prices")
    if not isinstance(prices, dict) or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
        for v in prices.values()
    ):
        raise ValueError("Invalid watcher prices; state has been preserved")
    if any(not isinstance(k, str) or not k.isdigit() for k in prices):
        raise ValueError("Invalid watcher listing IDs")
    legacy = value.get("legacy_seen", [])
    if not isinstance(legacy, list) or any(
        not isinstance(k, str) or not k.isdigit() for k in legacy
    ):
        raise ValueError("Invalid legacy watcher state")
    return value


def save_state(path, state):
    atomic_json(path, state)


def _format_hit(hit):
    previous = hit.get("previous_price")
    prefix = f"DROP {previous:.3f} → " if previous is not None else "NEW "
    return f"{prefix}{hit['price']:.3f} {hit['currency']} | {hit['title']} | {hit['url']}"


async def post_webhook(url, query, hits, kind="discord"):
    header = f"OpenSooq {oc.COUNTRY}: '{query}'\n"
    limit = 1900 if kind == "discord" else 3500
    messages = []
    current = header[:300]
    for hit in hits:
        line = _format_hit(hit)[: limit - 350] + "\n"
        if len(current) + len(line) > limit:
            messages.append(current)
            current = header[:300]
        current += line
    if current:
        messages.append(current)
    async with httpx.AsyncClient(timeout=10) as client:
        for text in messages:
            payload = {"content" if kind == "discord" else "text": text}
            response = await client.post(url, json=payload)
            response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(
        description=f"Watch OpenSooq {oc.COUNTRY} prices ({oc.CURRENCY})."
    )
    parser.add_argument("--query", required=True)
    parser.add_argument("--query-ar")
    parser.add_argument("--max-price", type=float, required=True)
    parser.add_argument("--city")
    parser.add_argument("--condition", choices=["all", "new", "used"], default="all")
    parser.add_argument("--storage-gb", type=int)
    parser.add_argument("--include-risky", action="store_true")
    parser.add_argument("--pages", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--verified-only", action="store_true")
    parser.add_argument("--shops-only", action="store_true")
    parser.add_argument("--state")
    parser.add_argument("--log")
    parser.add_argument("--webhook")
    parser.add_argument("--webhook-kind", choices=["discord", "slack"], default="discord")
    args = parser.parse_args()
    if args.storage_gb is not None and not 1 <= args.storage_gb <= 8192:
        parser.error("storage-gb must be between 1 and 8192")
    if not args.query.strip() or not math.isfinite(args.max_price) or args.max_price <= 0:
        parser.error("query must be nonempty and max-price must be a finite positive number")
    identity = [
        oc.COUNTRY,
        args.query_ar or args.query,
        args.city,
        args.max_price,
        args.verified_only,
        args.shops_only,
        args.condition,
        args.storage_gb,
        args.include_risky,
    ]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:16]
    state_path = Path(args.state) if args.state else HERE / f"watch-{oc.COUNTRY}-{key}.json"
    log_path = Path(args.log) if args.log else HERE / f"watch-{oc.COUNTRY}-{key}.log"
    try:
        with exclusive_lock(state_path.with_name(state_path.name + ".lock")):
            state = load_state(state_path)
            alerts, updated = select_alerts(asyncio.run(find_hits(args)), state)
            if alerts:
                if args.webhook:
                    asyncio.run(post_webhook(args.webhook, args.query, alerts, args.webhook_kind))
                now = datetime.now(timezone.utc).isoformat()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    for hit in alerts:
                        line = f"{now} | {_format_hit(hit)}"
                        print(line)
                        handle.write(line + "\n")
            else:
                print("No new matches or further price drops.")
            save_state(state_path, updated)
            return 2 if alerts else 0
    except Exception as error:
        # Avoid exposing webhook URLs containing secrets in error messages.
        print(
            f"Watcher failed: {type(error).__name__}; state preserved for retry.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
