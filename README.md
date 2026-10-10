# OpenSooq Jordan MCP

An unofficial, read-only MCP server for searching Jordan's OpenSooq marketplace,
comparing asking prices, inspecting listings, and filtering sellers by verification
and review evidence. Forked from [isaamthalhath07/opensooq-mcp](https://github.com/isaamthalhath07/opensooq-mcp).
Original MIT attribution is preserved in `LICENSE`.

Jordan is the default: `jo.opensooq.com`, JOD prices, 17 top-level categories and
14 cities. The bundled Jordan taxonomy was obtained from a live search response
on **10 October 2026**. Names and IDs can change; the tools identify their catalog
as a dated snapshot. Kuwait remains available with `OPENSOOQ_COUNTRY=kw`, using
the smaller inherited catalog.

## Run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone this fork,
and launch:

```sh
git clone --branch jordan-support https://github.com/ahmadalzaro1/opensooq-jordan-mcp.git
cd opensooq-jordan-mcp
uv run server.py
```

`server.py` runs over stdio and waits for an MCP client. It does not open a web
server. Python 3.12 is pinned in `.python-version`; supported Python versions
start at 3.10. The MCP SDK is constrained to v1 because this implementation uses
FastMCP. The inline dependency metadata and `requirements.txt` agree.

Configure a stdio MCP client (Claude Desktop, Cursor, Windsurf, or another host):

```json
{
  "mcpServers": {
    "opensooq-jordan": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/opensooq-jordan-mcp", "server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "OPENSOOQ_COUNTRY": "jo",
        "OPENSOOQ_LANGUAGE": "en"
      }
    }
  }
}
```

`OPENSOOQ_LANGUAGE` accepts `en` or `ar` and selects upstream locale. Queries,
city names and category names accept either language independently. Environment
configuration is read at process startup; restart the server after changing it.
Unsupported country or locale values fail explicitly.

`python install.py --print-only` prints configuration without provisioning Python,
installing dependencies or editing client files (uv must already be available).
`python install.py` provisions dependencies and adds `opensooq-jordan` to each
supported client it detects, with unique timestamped backups. It does not configure
Codex automatically. Manual configuration lets you target a single client.

## Tools

| Area | Tools |
|---|---|
| Discovery | `opensooq_search_listings`, `opensooq_latest_listings`, `opensooq_category_breakdown`, `opensooq_browse_category` |
| Details | `opensooq_get_listing` |
| Reference | `opensooq_list_categories`, `opensooq_list_cities` |
| Prices | `opensooq_market_summary`, `opensooq_price_distribution`, `opensooq_find_deals`, `opensooq_compare_prices` |
| Sellers | `opensooq_top_sellers`, `opensooq_seller_listings`, `opensooq_seller_profile`, `opensooq_find_trusted_sellers` |

Examples:

- “Find iPhone 15 Pro listings in Amman below 400 JOD.”
- “ابحث عن ايفون 15 برو في عمان.”
- “Compare asking prices for Toyota Prius and Hyundai Ioniq in Jordan.”
- “Show verified sellers with a rating of at least 4 and at least 10 reviews.”

Most discovery tools support Markdown or JSON. `opensooq_search_listings` accepts
`query`, optional `query_ar` (which overrides the query), `city`, `category`, `sort`,
`page`, `limit`, `min_price`, `max_price`, `strict_match`, and seller/media filters.
`min_seller_reviews` defaults to 0 for general search; trusted-seller discovery
uses `min_reviews=5` by default. Ambiguous city/category names produce an error
instead of silently selecting the first partial match. Numeric category IDs can
also address subcategories; city IDs must exist in the selected market catalog.

## Improvements over upstream

- Market configuration replaces hardcoded Kuwait routing, cookies, currency and locale.
- Arabic city/category names, diacritics, letter variants and Arabic/Persian digits work.
- Arabic decimal/thousands separators and JOD fractional prices are parsed.
- Paginated samples deduplicate listings and stop at the upstream last page.
- Locally sorted search samples honor `page` and `limit`; empty search results in
  JSON mode retain a structured response. The upstream total is identified as
  the fuzzy count before local filtering.
- Price summaries identify their sample and use interpolated quartiles. Other
  currencies are excluded rather than mixed into the selected market's prices.
- Seller filters can require a review count; ratings and verification are explicitly
  described as signals rather than guarantees.
- Requests are spaced at least 300 ms per process. HTTP 429/502/503/504 responses
  get at most two retries, honoring numeric `Retry-After` up to 10 seconds.
  Longer delays return the error for the caller to handle. Authentication gets
  one fresh-registration retry; schema drift produces a clear error.
- Page enrichment only fetches HTTPS URLs on the selected market's host and
  follows at most three redirects after validating each target host. Targeted listing/seller lookups validate returned IDs.
- The watcher tracks previously notified prices and alerts on further drops.
  Webhook failures retain state for retry; malformed state is preserved and fails.
- Offline regression tests and a Python 3.10/3.12/3.13 GitHub Actions matrix.

## Price watcher

```sh
uv run watch.py --query 'ايفون 15 برو' --city عمان --max-price 400
```

Alerts identify `NEW` matches or `DROP` events. Prices display three decimal
places. An unchanged price or a price rebound above the last alerted low does
not repeat an alert. This is a bounded search sample, so disappearance from the
sample does not mean the listing was sold. State only tracks qualifying listings,
not the full market's historical prices. Old seen-ID state files migrate by
establishing a price baseline without repeating old notifications.

Optional delivery:

```sh
uv run watch.py --query 'ايفون 15 برو' --max-price 400 \
  --webhook 'YOUR_WEBHOOK_URL' --webhook-kind discord
```

Discord and Slack payloads are supported explicitly (`--webhook-kind slack`).
No scheduler is installed automatically. Avoid overlapping runs sharing one
state file. Default state filenames include the country and a hash of the query,
city, target price and seller filters. Exit codes: **2** alerts, **0** unchanged,
**1** operational failure. A webhook failure prevents state advancement; delivery
is at least once, so a later failure after delivery can produce a duplicate retry.
State/log files can include listing links; keep them local.

## Tests and opt-in live checks

```sh
uv run --with pytest --with 'mcp>=1.2.0,<2' --with httpx --with pydantic python -m pytest -q
uv run --with 'mcp>=1.2.0,<2' --with httpx --with pydantic python troubleshoot.py --live
```

Unit tests use fixtures and mocked HTTP, without contacting OpenSooq. Live
checks are explicit and small: a Jordan search scoped to Amman/mobile phones,
one listing lookup and a one-page price summary. See `VERIFICATION.md` for the
checks performed on this fork.

## Limits

This is a community project, with no claimed OpenSooq endorsement. It accesses
private website endpoints using the website's anonymous-device handshake.
Those endpoints, signing rules, catalog IDs and HTML can change at any time.
No logged-in seller-phone reveal, posting, messaging, editing or purchasing tools
are provided. Seller phone numbers remain masked.

Price/deal estimates describe **asking prices in a filtered sample**, not completed
sales, valuations or the entire inventory. Cheapest/priciest search scans up to
five relevance pages and sorts that sample locally; a result is not guaranteed to
be the cheapest on OpenSooq. Keyword matching and broad price-band heuristics
can include unrelated products or omit genuine bargains. Product condition,
capacity, trim, location and payment terms must be checked before comparing.
Rich details are scraped best-effort and can be absent.

## Layout

- `market.py`, `catalogs.json`: validated environment configuration and bilingual taxonomy.
- `opensooq_client.py`: anonymous handshake, bounded requests, parsing and pagination.
- `server.py`: 15 FastMCP tools and input validation.
- `watch.py`: threshold and price-drop alerts with local state.
- `install.py`: optional client configuration helper.
- `tests/`: offline regression tests.
- `troubleshoot.py`: explicit live smoke check.
