#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["httpx>=0.27.0"]
# ///
"""
OpenSooq price watcher — alert when a listing drops below a target price.

A tiny standalone script (no MCP/Pydantic needed — just `httpx`) meant to be run
on a schedule (cron / Windows Task Scheduler). On each run it:

  1. Searches OpenSooq for your product (relevance-gated, so "s26 ultra" won't
     match a Galaxy A52s — it reuses the same matcher as the MCP server).
  2. Keeps genuine matches priced at or below your threshold.
  3. Remembers what it has already seen (a small JSON state file), so it only
     alerts on **new** listings — no repeat spam.
  4. Prints each new hit, appends it to an alerts log, and (optionally) POSTs it
     to a webhook (Discord / Slack / Telegram / your own endpoint).

Examples
--------
  # cheapest real Galaxy S26 Ultra under 320 KWD
  python watch.py --query "samsung galaxy s26 ultra" \
                  --query-ar "سامسونج جالاكسي اس 26 الترا" --max-price 320

  # iPhone 15 Pro under 170 KWD, verified sellers only, ping a Discord webhook
  python watch.py --query "iphone 15 pro" --query-ar "ايفون 15 برو" \
                  --max-price 170 --verified-only \
                  --webhook https://discord.com/api/webhooks/XXX/YYY

State and logs default to files named after the query, in this folder.
Exit code 2 means "new hits found" (handy for scripting); 0 means none.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

import opensooq_client as oc

HERE = Path(__file__).resolve().parent


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "watch"


def _city_id(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    name = name.strip()
    if name.isdigit():
        return int(name)
    for cid, cname in oc.CITIES.items():
        if name.lower() in cname.lower():
            return cid
    raise SystemExit(f"Unknown city '{name}'. Valid: {', '.join(oc.CITIES.values())}")


async def find_hits(args: argparse.Namespace) -> List[Dict[str, Any]]:
    eff_query = args.query_ar or args.query
    # Fetch by RELEVANCE (not cheapest): a server-side cheapest sort only returns
    # OpenSooq's globally-cheapest junk/decoys and never reaches the real product.
    raw = await oc.search_multi_page(
        eff_query, pages=args.pages, city_id=_city_id(args.city),
        sort=oc.SORT_CODES["relevant"],
    )
    # Same "is this really the product?" filter the MCP server uses, then drop
    # decoy/teaser/junk prices (e.g. 9.99 KWD installment hooks) via the median band.
    matches = oc.relevant_listings(eff_query, raw)
    matches, _, _ = oc.comparable_listings(matches)
    hits = []
    seen_ids = set()
    for l in matches:
        if l["id"] in seen_ids:
            continue
        seen_ids.add(l["id"])
        if l["price"] <= 0 or l["price"] > args.max_price:
            continue
        if args.verified_only and not l["seller"]["is_verified"]:
            continue
        if args.shops_only and not l["seller"]["is_shop"]:
            continue
        hits.append(l)
    hits.sort(key=lambda l: l["price"])
    return hits


def _format_hit(l: Dict[str, Any]) -> str:
    s = l["seller"]
    loc = ", ".join(p for p in (l["location"]["neighborhood"], l["location"]["city"]) if p)
    seller = s["display_name"] or s["username"] or "?"
    badge = " 🏪" if s["is_shop"] else (" ✓" if s["is_verified"] else "")
    rating = f" ★{s['rating_avg']:.1f}({s['rating_count']})" if s["rating_count"] else ""
    return (f"{l['price']:.0f} {l['currency']} | {l['title'][:55]} | {loc} | "
            f"{seller}{badge}{rating} | {l['url']}")


async def post_webhook(url: str, query: str, hits: List[Dict[str, Any]]) -> None:
    lines = [f"🔔 OpenSooq: {len(hits)} new '{query}' listing(s) under target:"]
    lines += [f"• {_format_hit(h)}" for h in hits]
    text = "\n".join(lines)
    # Works with Discord ({"content"}) and Slack/Telegram-style ({"text"}) webhooks.
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"content": text, "text": text})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] webhook failed: {type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="Alert on OpenSooq listings below a target price.")
    p.add_argument("--query", required=True, help="English search term, e.g. 'iphone 15 pro'.")
    p.add_argument("--query-ar", default=None, help="Arabic term (better recall).")
    p.add_argument("--max-price", required=True, type=float, help="Alert when price <= this (KWD).")
    p.add_argument("--city", default=None, help="Optional city name/id to scope to.")
    p.add_argument("--verified-only", action="store_true", help="Only verified sellers.")
    p.add_argument("--shops-only", action="store_true", help="Only professional 'shop' sellers.")
    p.add_argument("--pages", type=int, default=5, help="Result pages to scan (default 5).")
    p.add_argument("--state", default=None, help="Seen-IDs JSON file (default: watch-<query>.json).")
    p.add_argument("--log", default=None, help="Alerts log file (default: watch-<query>.log).")
    p.add_argument("--webhook", default=None, help="Optional webhook URL (Discord/Slack/Telegram).")
    args = p.parse_args()

    base = _slug(args.query)
    state_path = Path(args.state) if args.state else HERE / f"watch-{base}.json"
    log_path = Path(args.log) if args.log else HERE / f"watch-{base}.log"

    seen = set()
    if state_path.exists():
        try:
            seen = set(json.loads(state_path.read_text(encoding="utf-8")))
        except Exception:
            seen = set()

    hits = asyncio.run(find_hits(args))
    new_hits = [h for h in hits if h["id"] not in seen]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    if new_hits:
        print(f"[{now}] {len(new_hits)} NEW '{args.query}' listing(s) <= {args.max_price:.0f} KWD:")
        with log_path.open("a", encoding="utf-8") as logf:
            for h in new_hits:
                line = _format_hit(h)
                print("  " + line)
                logf.write(f"{now} | {line}\n")
        if args.webhook:
            asyncio.run(post_webhook(args.webhook, args.query, new_hits))
    else:
        print(f"[{now}] no new '{args.query}' listings <= {args.max_price:.0f} KWD "
              f"({len(hits)} match the price, all already seen).")

    # Persist the full set of currently-matching ids so we don't re-alert.
    seen.update(h["id"] for h in hits)
    state_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    return 2 if new_hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
