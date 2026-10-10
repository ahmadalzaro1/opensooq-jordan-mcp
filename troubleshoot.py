#!/usr/bin/env python3
"""Opt-in live verification through the actual MCP stdio transport.

Default invocation does not contact OpenSooq. --live checks core paths;
--live --all exercises all 15 tools, extra categories and validation failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from listing_quality import match_listing


async def check(all_tools=False):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("server.py"))],
        env={**os.environ, "OPENSOOQ_COUNTRY": "jo"},
    )
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "country": "jo", "checks": []}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tool_list = await session.list_tools()
            assert len(tool_list.tools) == 15

            async def call(name, arguments=None):
                result = await session.call_tool(
                    name, {"params": arguments} if arguments is not None else {}
                )
                if result.isError:
                    raise AssertionError(f"{name}: " + result.content[0].text)
                data = json.loads(result.content[0].text)
                report["checks"].append({"tool": name, "status": "PASS"})
                return data

            cities = await call("opensooq_list_cities")
            categories = await call("opensooq_list_categories")
            assert cities["country"] == "jo" and cities["count"] == 14
            assert categories["country"] == "jo" and categories["count"] == 17
            query = "ايفون 15 برو"
            search = await call(
                "opensooq_search_listings",
                {
                    "query": query,
                    "city": "عمان",
                    "category": "موبايل - تابلت",
                    "limit": 5,
                    "response_format": "json",
                },
            )
            assert search["count"] > 0, search
            assert all(
                x["currency"] == "JOD"
                and x["location"]["city"] == "Amman"
                and match_listing(query, x)
                for x in search["listings"]
            )
            first = search["listings"][0]
            detail = await call(
                "opensooq_get_listing", {"listing_id": first["id"], "response_format": "json"}
            )
            assert detail["found"] and detail["listing"]["id"] == first["id"]
            assert len(detail["details"].get("full_description") or "") > 0
            seller = detail["details"].get("seller_profile")
            assert seller and seller["member_id"] == first["seller"]["member_id"], detail["details"]
            summary = await call(
                "opensooq_market_summary",
                {"query": query, "category": "موبايل - تابلت", "city": "عمان", "pages": 1},
            )
            assert summary["currency"] == "JOD" and summary["priced_listings"] > 0
            report["core"] = {
                "search_count": search["count"],
                "priced_sample": summary["priced_listings"],
                "cohort_count": len(summary["cohorts"]),
                "description_available": True,
                "seller_profile_available": True,
            }
            if all_tools:
                await call(
                    "opensooq_latest_listings",
                    {"query": query, "city": "عمان", "limit": 3, "response_format": "json"},
                )
                await call("opensooq_category_breakdown", {"query": query})
                await call(
                    "opensooq_browse_category",
                    {
                        "category": "سيارات ومركبات",
                        "city": "عمان",
                        "limit": 3,
                        "response_format": "json",
                    },
                )
                distribution = await call(
                    "opensooq_price_distribution", {"query": query, "city": "عمان", "pages": 1}
                )
                assert (
                    sum(b["count"] for b in distribution["buckets"])
                    == distribution["priced_listings"]
                )
                await call(
                    "opensooq_find_deals",
                    {"query": query, "city": "عمان", "pages": 1, "response_format": "json"},
                )
                comparison = await call(
                    "opensooq_compare_prices",
                    {"queries": [query, "ايفون 14"], "city": "عمان", "pages": 1},
                )
                assert all("error" not in row for row in comparison["comparisons"])
                await call(
                    "opensooq_top_sellers", {"query": query, "city": "عمان", "pages": 1, "limit": 3}
                )
                member = first["seller"]["member_id"]
                inventory = await call(
                    "opensooq_seller_listings",
                    {"member_id": member, "page": 1, "limit": 3, "response_format": "json"},
                )
                assert inventory["count"] > 0 and all(
                    x["seller"]["member_id"] == member for x in inventory["listings"]
                )
                profile = await call(
                    "opensooq_seller_profile", {"member_id": member, "max_pages": 1}
                )
                assert profile["found"] and profile["profile"]["member_id"] == member
                await call(
                    "opensooq_find_trusted_sellers",
                    {"query": query, "city": "عمان", "pages": 1, "min_reviews": 5},
                )
                for term, cat in [
                    ("تويوتا بريوس", "سيارات ومركبات"),
                    ("شقة للايجار", "عقارات للايجار"),
                ]:
                    result = await call(
                        "opensooq_search_listings",
                        {
                            "query": term,
                            "category": cat,
                            "city": "عمان",
                            "limit": 3,
                            "response_format": "json",
                        },
                    )
                    assert result["count"] > 0, (term, result)
                    assert all(match_listing(term, x) for x in result["listings"])
                empty = await call(
                    "opensooq_search_listings",
                    {"query": "zznonexistentproduct9284756", "response_format": "json"},
                )
                assert empty["count"] == 0 and empty["listings"] == []
                invalid = await session.call_tool(
                    "opensooq_search_listings", {"params": {"query": "", "max_price": -1}}
                )
                assert invalid.isError
                report["checks"].append({"check": "invalid_input_is_mcp_error", "status": "PASS"})
                assert len({c.get("tool") for c in report["checks"] if c.get("tool")}) == 15
    report["status"] = "PASS"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--all", action="store_true", help="Exercise all tools plus cars/property and error paths"
    )
    parser.add_argument("--output", type=Path, help="Optional JSON verification report")
    args = parser.parse_args()
    if not args.live:
        parser.print_help()
        return 0
    report = asyncio.run(check(args.all))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
