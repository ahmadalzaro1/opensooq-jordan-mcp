"""Export the same bounded, filtered MCP search sample as UTF-8 CSV or JSON."""

import argparse
import asyncio
import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from server import SearchInput, opensooq_search_listings


def csv_cell(value):
    """Prevent seller-controlled strings from becoming spreadsheet formulas."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_text(payload, observed_at):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    columns = [
        "id",
        "title",
        "description",
        "price",
        "currency",
        "city",
        "neighborhood",
        "url",
        "observed_at",
        "specifications",
        "risk_flags",
        "source_queries",
        "sample_note",
        "search_partial",
    ]
    writer.writerow(columns)
    for listing in payload["listings"]:
        location = listing.get("location") or {}
        row = {
            **listing,
            "city": location.get("city"),
            "neighborhood": location.get("neighborhood"),
            "observed_at": observed_at,
            "sample_note": payload.get("sample_note"),
            "search_partial": payload.get("search", {}).get("partial", False),
            "specifications": json.dumps(listing.get("specifications"), ensure_ascii=False),
            "risk_flags": json.dumps(
                (listing.get("quality") or {}).get("risk_flags", []), ensure_ascii=False
            ),
            "source_queries": json.dumps(
                listing.get("source_queries", payload.get("search", {}).get("queries", [])),
                ensure_ascii=False,
            ),
        }
        writer.writerow([csv_cell(row.get(name)) for name in columns])
    return output.getvalue()


async def collect(params):
    payload = json.loads(await opensooq_search_listings(params))
    payload["observed_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--query-ar")
    parser.add_argument("--city")
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--year", type=int)
    parser.add_argument("--max-mileage-km", type=float)
    parser.add_argument("--bedrooms", type=int)
    parser.add_argument("--rental-period", choices=["daily", "weekly", "monthly", "yearly"])
    parser.add_argument("--neighborhood")
    parser.add_argument("--min-area-m2", type=float)
    parser.add_argument("--max-area-m2", type=float)
    parser.add_argument("--condition", choices=["all", "new", "used"], default="all")
    parser.add_argument("--max-price", type=float)
    parser.add_argument("--output", required=True)
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    args = parser.parse_args()
    if Path(args.output).exists():
        parser.error("Output already exists; choose a new path")
    payload = asyncio.run(
        collect(
            SearchInput(
                query=args.query,
                query_ar=args.query_ar,
                city=args.city,
                category=args.category,
                limit=args.limit,
                year=args.year,
                max_mileage_km=args.max_mileage_km,
                bedrooms=args.bedrooms,
                rental_period=args.rental_period,
                neighborhood=args.neighborhood,
                min_area_m2=args.min_area_m2,
                max_area_m2=args.max_area_m2,
                condition=args.condition,
                max_price=args.max_price,
                strict_match=True,
                response_format="json",
            )
        )
    )
    # Exclusive create avoids silently replacing an existing report.
    with Path(args.output).open(
        "x", encoding="utf-8-sig" if args.format == "csv" else "utf-8", newline=""
    ) as handle:
        handle.write(
            csv_text(payload, payload["observed_at"])
            if args.format == "csv"
            else json.dumps(payload, ensure_ascii=False, indent=2)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
