# OpenSooq Jordan MCP

An unofficial, read-only MCP server for Jordan's OpenSooq marketplace. Search
listings, inspect seller information, compare sampled asking prices, and watch
qualifying listings for further price reductions.

Forked from [Isaam Thalhath's opensooq-mcp](https://github.com/isaamthalhath07/opensooq-mcp).
The original MIT license and attribution are preserved. This fork defaults to
`jo.opensooq.com` and JOD. Its 17 top-level categories and 14 city filters were
verified against a live Jordan response on **10 October 2026**. Kuwait remains
selectable with `OPENSOOQ_COUNTRY=kw`; that inherited catalog has fewer categories.

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run:

```sh
git clone --branch jordan-support https://github.com/ahmadalzaro1/opensooq-jordan-mcp.git
cd opensooq-jordan-mcp
uv run --locked server.py
```

The server speaks **stdio MCP** and waits for a client; it does not open a web
server. Python 3.12 is selected by `.python-version`. Python 3.10+ is supported.
The MCP SDK is constrained to v1 for FastMCP compatibility. `server.py.lock`,
`watch.py.lock` and `uv.lock` make script and development dependencies reproducible.

A stdio client's configuration:

```json
{
  "mcpServers": {
    "opensooq-jordan": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "--locked", "--directory", "/ABSOLUTE/PATH/TO/opensooq-jordan-mcp", "server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "OPENSOOQ_COUNTRY": "jo",
        "OPENSOOQ_LANGUAGE": "en"
      }
    }
  }
}
```

Country accepts `jo` or `kw`; locale accepts `en` or `ar`. Configuration is read at
startup. Queries and city/category names accept Arabic or English independently
of locale. Arabic diacritics, common letter variants and Arabic/Persian digits are
normalized. The alias dictionary covers common products; it does not translate
arbitrary English queries into Arabic. `query_ar` overrides `query` when supplied.

## Tools

| Area | Tools |
|---|---|
| Discovery | `opensooq_search_listings`, `opensooq_latest_listings`, `opensooq_category_breakdown`, `opensooq_browse_category` |
| Details | `opensooq_get_listing` |
| Reference | `opensooq_list_categories`, `opensooq_list_cities` |
| Prices | `opensooq_market_summary`, `opensooq_price_distribution`, `opensooq_find_deals`, `opensooq_compare_prices` |
| Sellers | `opensooq_top_sellers`, `opensooq_seller_listings`, `opensooq_seller_profile`, `opensooq_find_trusted_sellers` |

Examples:

- “Find iPhone 15 Pro listings in Amman, used, 256 GB, below 450 JOD.”
- “ابحث عن تويوتا بريوس في عمان.”
- “Show verified sellers with a rating of at least 4 and at least 10 reviews.”
- “Compare iPhone 14 and iPhone 15 asking prices in Amman.”

Most listing tools accept `response_format: "json" | "markdown"`; analytics and
reference tools return JSON. Empty results remain structured, with empty arrays
or null statistics. Expired/unknown listing IDs return `found=false`. Network,
authentication, invalid-input and schema errors are reported as **MCP tool errors**
rather than successful text responses that happen to contain an error message.
Comparison tools retain per-query failures so a successful query is not lost.

## Product matching and offer quality

The matcher checks every significant query term, with bilingual aliases for common
brands and products. Recognized product families and explicit phone generations
must appear in the title; another device mentioned in a seller's description is
not enough. Explicit phone generations use exact tiers, so “iPhone 15 Pro” does
not also match “iPhone 15 Pro Max”. This intentionally favors precision over recall.

Normalized listings include `quality`:

- `condition`: `new`, `used` or `unknown`; “like new” is used.
- `storage_gb`: one identifiable capacity; missing or conflicting capacities stay null.
- `variant`, `model_identity`, and `identity_hint`.
- `risk_flags`: accessories, payment offers, damaged items, wanted/exchange ads,
  multiple capacities or multiple phone generations when detected.

`opensooq_search_listings` and price tools support `condition` and `storage_gb`.
Unknown specifications are excluded when a specific filter is selected. Risky
advertisements are excluded by default; use `include_risky=true` to inspect them
or deliberately search for accessories. General latest/category browsing can show
flagged offers, which remain labeled in the returned listing.

The rules are explainable heuristics. They can reject a legitimate listing or miss
a misleading one. Titles, descriptions and available API attributes are evidence,
not a guarantee of an item's condition. RAM versus storage and less familiar
product spellings can remain ambiguous. Check the listing before deciding.

## Price analysis

Price tools support city, category, condition, capacity, and a bounded page budget
(1–5). Samples deduplicate IDs and stop at the last or empty page. JOD fractional
prices and Arabic separators are parsed; another currency is excluded rather
than mixed into calculations. Unknown dates stay null rather than appearing new.

Summaries report aggregate statistics **and separate cohorts** by recognized
model, tier, capacity and condition. Recognized Prius/Ioniq vehicle years are also
separated. Quartiles use linear interpolation. Broad outlier filtering is applied
within cohorts, rather than excluding new-device prices against a used-device
median. Aggregated medians can still mix specifications: inspect the cohorts.

Deal candidates are compared with their **own cohort median**. Each candidate
includes its baseline size and median. The default `min_sample=5` requires five
priced listings in that cohort. Cohorts containing unknown specifications do not
produce deals; the tool returns their unusable baseline explicitly. No result is
presented as a guaranteed bargain, sale price or formal valuation.

Cheapest/priciest search scans up to five relevance pages and sorts locally. Its
pagination operates on that sample. `total` is the upstream fuzzy count **before
local filtering**, while `sample_count` is the filtered sample size. Searching
cannot guarantee the cheapest listing across the entire marketplace. Price-ranked
search and threshold watches do not discard a match simply because it is cheap;
offer flags and identity checks are used instead of a global median cutoff.

## Watch prices

```sh
uv run --locked watch.py --query 'ايفون 15 برو' --city عمان \
  --condition used --storage-gb 256 --max-price 400
```

The watcher reports new qualifying listings and further drops below the last
notified price. Unchanged prices and rebounds do not repeat notifications. It
uses the same identity/offer filters as search, and checks the selected currency.
An exclusive file lock prevents overlapping processes from modifying the same
state. JSON writes are atomic, malformed state is preserved, and old seen-ID
files migrate by establishing a baseline without repeating old alerts.

State filenames include the market and a hash of query/filter settings. Only
qualifying listings are tracked, not complete marketplace price history. A listing
missing from the next sample has not necessarily been sold or removed.

Optional delivery:

```sh
uv run --locked watch.py --query 'ايفون 15 برو' --max-price 400 \
  --webhook 'YOUR_WEBHOOK_URL' --webhook-kind discord
```

Discord and Slack have explicit payload formats (`--webhook-kind slack`). Messages
are split to fit payload limits. No real webhook was contacted in verification;
HTTP acceptance and failure paths are covered with mocks. A rejected webhook
prevents state advancement. Delivery is at least once: a later failure after
partial delivery can result in duplicate retries. Webhook credentials are not
printed in errors. Exit codes: **2** alerts, **0** unchanged, **1** failure.
No scheduler is installed automatically.

## Optional installer

`python install.py --print-only` prints configuration without provisioning or
editing client files; uv must already be available.

`python install.py --client Cursor` provisions dependencies, verifies imports,
and configures only that detected client. The default selects all detected clients
among Claude Desktop, Cursor, Antigravity and Windsurf. Config updates use a lock,
atomic replacement, and unique backups. Invalid JSON/config shapes are preserved
and skipped. Codex is not configured automatically.

## Verify and develop

```sh
uv sync --locked --group dev
uv run --locked --group dev pytest -q
uv run --locked --group dev ruff check .
uv run --locked --group dev ruff format --check .

# Explicit live tests through an actual MCP stdio client:
uv run --locked python troubleshoot.py --live
uv run --locked python troubleshoot.py --live --all
```

The full live check exercises all 15 tools, cars/property, seller lookup, empty
results and invalid inputs. It makes a modest sequence of real read-only requests.
Offline tests do not contact OpenSooq. CI checks Python 3.10/3.12/3.13 on Linux,
plus Python 3.12 on macOS and Windows. See `VERIFICATION.md` for actual results.

## Maintenance and limits

This is a community integration with no claimed OpenSooq endorsement. It uses
private website endpoints and an anonymous-device signing handshake. APIs, signing,
HTML and catalog IDs can change. The catalog is a dated bundled snapshot, not an
automatically refreshed source. HTTP calls are spaced by at least 300 ms per
process, retry transient network/server failures at most twice, and honor bounded
numeric or HTTP-date `Retry-After`. Access blocks are surfaced, not bypassed.

Rich details are parsed from structured Next.js JSON and decoded Flight rows,
including nested JSON strings. Key order and nested seller fields are supported.
Each detail-page redirect must remain HTTPS on the selected market host. A page
with a different listing ID is rejected. Enrichment failure leaves the API listing
available. Full details can still be unavailable after a website change.

No seller contact, posting, editing, purchasing or logged-in phone-reveal tools
are provided. Phone reveal keys are never included in normalized output. Seller
verification/review filters provide signals, not a guarantee of trustworthiness.

## Files

- `market.py`, `catalogs.json`: market configuration and bilingual reference data.
- `listing_quality.py`: deterministic matching, offer flags and comparison cohorts.
- `opensooq_client.py`: anonymous access, bounded HTTP, parsing and pagination.
- `page_parser.py`: structured page enrichment.
- `server.py`: validated inputs and 15 read-only MCP tools.
- `watch.py`, `local_state.py`: alerts, atomic state and process locking.
- `install.py`: optional client configuration.
- `troubleshoot.py`, `tests/`: explicit live checks and offline regressions.
