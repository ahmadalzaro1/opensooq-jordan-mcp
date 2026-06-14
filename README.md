# opensooq-mcp

A comprehensive, **read-only MCP server** that gives any LLM agent live access to
**OpenSooq** — the largest classifieds marketplace in Kuwait (and the wider Gulf/
Levant). It turns natural-language questions like *"what's the going rate for a
used iPhone 15 Pro in Hawally, and who are the trusted sellers?"* into real
marketplace data: listings, prices, deals, categories, and seller reputation.

OpenSooq publishes **no official API**, so this server reverse-engineers and
replicates the web client's private `serp/search/v2` API — including
its anonymous device-registration + JWT-signing handshake — and exposes **15
well-documented tools** through the [Model Context Protocol](https://modelcontextprotocol.io).

Drop the folder anywhere and
run it with [`uv`](https://docs.astral.sh/uv/) (no Python install, no venv, no
config); see [Quick start](#quick-start).

---

## Table of contents

- [Features at a glance](#features-at-a-glance)
- [Quick start](#quick-start)
- [Integrating with an MCP client](#integrating-with-an-mcp-client)
  - [Claude Desktop](#claude-desktop)
  - [Claude Code (CLI)](#claude-code-cli)
  - [Cursor](#cursor)
  - [Google Antigravity](#google-antigravity)
  - [Windsurf / other MCP clients](#windsurf--other-mcp-clients)
  - [Programmatic use (no LLM)](#programmatic-use-no-llm)
- [See it in action](#see-it-in-action)
- [How the MCP works](#how-the-mcp-works)
- [Tool reference](#tool-reference)
- [Use cases & applications](#use-cases--applications)
- [Price-drop watcher (cron-ready)](#price-drop-watcher-cron-ready)
- [Data-quality handling](#data-quality-handling)
- [Limitations & responsible use](#limitations--responsible-use)
- [Development & testing](#development--testing)
- [Troubleshooting](#troubleshooting)

---

## Features at a glance

| Area | What you get |
|------|--------------|
| **Search** | Full-text search with server-side sorting (relevant / newest / cheapest / priciest), category + city scoping, and client-side refinements (price range, media, seller quality). Bilingual — pass an Arabic term for much better recall. |
| **Listing detail** | Any listing by id, enriched with the full untruncated description, attribute rows, and a rich seller profile. |
| **Categories & cities** | The full category taxonomy and city list, plus category-first browsing and per-query category breakdowns. |
| **Pricing & deals** | Market price summaries (min/max/avg/median/quartiles), price histograms, a below-market **deal finder**, and side-by-side price comparison across variants. |
| **Sellers & reputation** | Seller leaderboards, a seller's full inventory, a complete reputation profile, and a **trusted-seller** finder to reduce scam risk. |
| **Robustness** | Outputs are defended against OpenSooq's noisy data (accessories, troll prices, loosely-related fallback results). |

Every tool is **read-only** (it never posts, edits, or deletes), returns either
human-readable **markdown** or structured **JSON**, and has rich docstrings the
agent uses to pick the right tool automatically.

---

## See it in action

The whole point of the MCP is turning a vague, training-data answer into **live
marketplace facts**. Same three questions, asked of the model *without* the MCP
vs. *with* it (real OpenSooq Kuwait data, captured 2026-06-13):

### "Cheapest Samsung Galaxy S26 Ultra?"

| Without the MCP | With the MCP |
|---|---|
| *"I can't see live listings. A new flagship Ultra in the Gulf is *maybe* 350–450 KWD — but I can't confirm any actual price, seller, or that it's even listed."* | **310 KWD (256 GB)** — real listings: *Almohands Shop* 🏪 ★3.65 in Kuwait City ([282305058](https://kw.opensooq.com/search/282305058)) and a sealed one in Farwaniya ([281723370](https://kw.opensooq.com/search/281723370)). It also **caught two traps** a naive price-sort puts first: a "200 KWD 1TB" that's actually a *social-media-account* listing in disguise, and a "280 KWD" that's an iPhone offered *in exchange* — and warned that only 20/150 scanned were genuine S26 Ultras. |

### "Going rate for a used iPhone 15 Pro?"

| Without the MCP | With the MCP |
|---|---|
| *"Roughly $600–800 used → very loosely ~190–250 KWD. A guess — not Kuwait-specific, not current, no idea of the spread."* | From **55 real priced listings**: median **200 KWD**, average 196.6, range 100–275, **p25–p75 = 155–230 KWD**. So "fair" is ~200, and under ~155 is a genuine deal (or a red flag). |

### "Which sellers are trustworthy for an iPhone 15 Pro?"

| Without the MCP | With the MCP |
|---|---|
| *"Unanswerable — I don't know individual sellers. Generic advice only: prefer verified sellers, meet in public…"* | Named **verified** sellers ≥★4: **Hamza** ✓ ★5.0 at 210 KWD ([282253000](https://kw.opensooq.com/search/282253000)), **LaRushka** ✓ ★5.0 at 250 KWD ([281670200](https://kw.opensooq.com/search/281670200)) — with member ids, ratings, and links. |

The difference in one line: **stale, generic, global guesses → live, specific,
Kuwait-current listings, prices, sellers, and scam-filtering.**

---

## Quick start

The **only requirement is [`uv`](https://docs.astral.sh/uv/)** — one tool that
handles everything else. You do **not** install Python or make a virtualenv; `uv`
fetches the pinned Python (3.12, from the bundled `.python-version`) and the deps
(`mcp`, `httpx`, `pydantic`) automatically. Works the same on Windows, macOS, Linux.

**1. Install `uv`** (once per machine):

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your terminal so `uv` is on `PATH`.

**2. Run the installer** — from this folder:

```bash
python install.py
```

That one command does everything: it provisions Python + deps, verifies the
server loads, then **auto-writes the correct config into every MCP client it
finds** on your machine (Claude Desktop, Cursor, Antigravity, Windsurf) — backing
up each file first. **Fully restart your client**, then ask *"search OpenSooq for
iphone 15 pro"*. Done.

- `python install.py --print-only` — just print the config, change nothing.
- Moved the folder, or merged it elsewhere? Re-run `python install.py` and it
  rewrites every client config with the new path. (It's how you stay portable.)

> Prefer to wire it up by hand, or want the per-client file locations? See
> [Integrating with an MCP client](#integrating-with-an-mcp-client). No `uv`?
> See [Plan B](#plan-b--without-uv).

---

## Integrating with an MCP client

`python install.py` (above) does this for you. This section is for doing it by
hand, or just to see where each client keeps its config.

All MCP clients use the same idea: they spawn the server over stdio. With `uv` the
block is **identical on every OS and client** — you only change the path to the
`opensooq-mcp` folder:

```json
{
  "mcpServers": {
    "opensooq": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/ABSOLUTE/PATH/TO/opensooq-mcp",
        "server.py"
      ],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

That single block is all you need below. `uv` reads the deps + pinned Python (3.12,
from the bundled `.python-version`) and provisions them automatically — no
`pip install`, no venv, no version conflicts, no GitHub download for a newer Python.

> **If `uv` isn't found by your client:** GUI apps (e.g. Claude Desktop) sometimes
> don't inherit your terminal's `PATH`. Put the **absolute path to `uv`** in
> `command` (`which uv` / `where.exe uv`). The installer already does this.

<details><summary>Legacy: pointing at a Python interpreter directly (Plan B)</summary>

If you used [Plan B](#plan-b--without-uv) (your own venv), set `"command"` to that
venv's python and `"args"` to the absolute path of `server.py`:

```json
{
  "mcpServers": {
    "opensooq": {
      "command": "/ABSOLUTE/PATH/TO/opensooq-mcp/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/opensooq-mcp/server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

On Windows use `.venv\Scripts\python.exe`; if you didn't make a venv, use `"command": "python"` (macOS/Linux) or
`"command": "py"` (Windows) — just make sure that interpreter has the deps.

</details>

### Claude Desktop

Edit `claude_desktop_config.json`:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Paste the config block above (merge into any existing `mcpServers`), save, and
**fully restart** Claude Desktop. You'll see a 🔌/tools icon; ask *"search
OpenSooq for iphone 15 pro"* to confirm it's live.

### Claude Code (CLI)

The fastest path is the CLI helper — it writes the config for you:

```bash
claude mcp add opensooq -e PYTHONIOENCODING=utf-8 -- uv run --directory /abs/path/opensooq-mcp server.py
```

Then `claude mcp list` to verify, and `/mcp` inside a session to inspect the
tools. (You can also commit a project-scoped `.mcp.json` with the same
`mcpServers` block so teammates get it automatically.)

### Cursor

Create **`.cursor/mcp.json`** in your project (or `~/.cursor/mcp.json` for a
global server) with the config block above. Then open **Cursor Settings → MCP**,
confirm `opensooq` is listed and toggled on (green). In Composer/Agent, the model
can now call the tools; you may be asked to approve tool use the first time.

### Google Antigravity

Antigravity (Gemini) reads MCP servers from **`~/.gemini/config/mcp_config.json`**
(on Windows: `C:\Users\<you>\.gemini\config\mcp_config.json`).

1. Create/open that file and paste the reusable `mcpServers` block from above
   (same `command` / `args` / `env` — Antigravity uses the standard schema), or
   just run `python install.py`, which writes this file for you.
2. Save and **fully restart Antigravity**. Ask *"find the cheapest verified-seller
   iPhone 15 on OpenSooq"* to test.

> Earlier builds hit `dns error: No such host is known` → `calling "initialize":
> EOF` here, because `uv` tried to download a newer Python (3.14) from GitHub. The
> bundled **`.python-version`** (3.12) now prevents that — `uv` reuses the cached
> interpreter and starts immediately.

### Windsurf / other MCP clients

- **Windsurf:** `~/.codeium/windsurf/mcp_config.json`, same `mcpServers` block.
- **Any stdio MCP client** (Zed, Cline, custom hosts, the MCP Inspector): they
  all accept a `command` + `args` (+ optional `env`). Use the block above.
- **Debug with the MCP Inspector:**
  `npx @modelcontextprotocol/inspector uv run --directory /abs/path/opensooq-mcp server.py`
  — a browser UI to list/call the tools directly, great for verifying the server in isolation.

### Programmatic use (no LLM)

You don't need an LLM to use the data layer. `opensooq_client.py` has **no MCP/
Pydantic dependency** and can be imported anywhere `httpx` is installed:

```python
import asyncio, opensooq_client as oc

async def main():
    # one page of normalized listings
    listings, meta = await oc.search_page("iphone 15 pro", sort=oc.SORT_CODES["cheapest"])
    print(meta["count"], "total;", listings[0]["price"], listings[0]["currency"])

    # a specific seller's whole inventory
    items, m = await oc.fetch_member_listings("42899294", page=1)

    # one listing by id
    one = await oc.fetch_listing_by_id("281910976")

asyncio.run(main())
```

This is how you'd embed OpenSooq data into a backend, a cron job, or another app
without going through the MCP layer.

### Plan B — without `uv`

Prefer to manage Python yourself? You need **Python 3.10+**, then:

```bash
cd opensooq-mcp
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt     # Windows: .venv\Scripts\python
```

Then point your client at that venv's interpreter (the *Legacy* config block in
[Integrating](#integrating-with-an-mcp-client)). `requirements.txt`:

```
mcp>=1.2.0
httpx>=0.27.0
pydantic>=2.0.0
```

Note: only the MCP server (`server.py`) needs Python 3.10+. The standalone
[`watch.py`](watch.py) and [`opensooq_client.py`](opensooq_client.py) need only
`httpx` and run on Python ≥3.9.

---

## How the MCP works

### Architecture

```
┌─────────────────────┐   stdio (MCP)   ┌──────────────────────────────────────┐
│  MCP client          │ ◄────────────► │  server.py  (FastMCP tool layer)      │
│  Claude / Cursor /   │   tools/call    │  • 15 @mcp.tool functions             │
│  Antigravity / …     │                 │  • Pydantic input models (validation) │
└─────────────────────┘                 │  • markdown / JSON formatting         │
                                         │  • relevance + price-band defenses    │
                                         └───────────────┬──────────────────────┘
                                                         │ imports
                                         ┌───────────────▼──────────────────────┐
                                         │  opensooq_client.py  (pure client)    │
                                         │  • device registration + JWT signing  │
                                         │  • raw_search / post_ids / member_ids │
                                         │  • HTML/RSC scraping for extras        │
                                         │  • parsing → stable listing schema     │
                                         └───────────────┬──────────────────────┘
                                                         │ HTTPS
                                         ┌───────────────▼──────────────────────┐
                                         │  kw.opensooq.com  (private web API)   │
                                         └──────────────────────────────────────┘
```

The split is deliberate: **`server.py`** is the MCP/agent surface (tools,
validation, formatting, data-quality logic), while **`opensooq_client.py`** is a
dependency-light API client that's independently testable and reusable.

### The OpenSooq handshake (the hard part)

OpenSooq's search API rejects unauthenticated requests, so the client replicates
exactly what the browser does:

1. **Register an anonymous device** — `POST /api/register-device` returns a secret
   triple `(deviceUUID, t, k)`. This is cached and reused for ~4 minutes.
2. **Mint a short-lived HS256 JWT** — the signing key is *not* `k` directly; the
   browser derives it with a **two-stage HMAC-SHA256 chain**
   (`HMAC(k, "desktop..rnd.t")` → base64url → that becomes the HMAC key over the
   token body). `_generate_jwt()` reproduces this precisely.
3. **Send the exact cookie + header set** the SERP endpoint expects (Kuwait
   locale, KWD currency, `desktop` source, `x-tracking-uuid`, release version…).
4. **POST the query**, then **normalize** the noisy raw payload into one stable
   listing schema (see below). If a request 401s (stale registration), the client
   **re-registers once and retries** automatically.

### What one normalized listing looks like

```jsonc
{
  "id": "281910976",
  "title": "Apple iPhone 15 Pro Max 256 GB in Hawally",
  "description": "…",                 // masked/preview from search
  "price": 210.0, "currency": "KWD",  // 0 = "price on request"
  "postedDate": "2026-05-31T…Z", "insertedDate": "2026-05-31", "expiresAt": "30-06-2026",
  "category": "Mobiles", "categoryPath": ["Mobile - Tablet", "Mobiles"],
  "location": { "city": "Hawally", "neighborhood": "Hawally" },
  "status": "posted", "isActive": true,
  "media": { "imageUrl": "…", "gallery": ["…"], "imageCount": 8, "hasVideo": false, "has360": false },
  "tags": ["Delivery by seller"],
  "seller": {
    "member_id": "42899294", "display_name": "…", "username": "…",
    "badges": ["shop"], "is_shop": true, "rating_avg": 3.2, "rating_count": 274,
    "is_verified": false, "verification_level": 0,
    "phone_masked": "555471XX", "has_phone": true
  },
  "url": "https://kw.opensooq.com/…"
}
```

### Endpoints discovered (beyond plain search)

Several capabilities aren't in the obvious API and were recovered by inspecting
the site's server-rendered payloads:

- **Per-seller listings** → the search body accepts a **top-level `member_ids`**
  array (not inside `filters`). Returns a seller's whole inventory, paginated,
  with their authoritative total count. (Found by reading the seller shop page's
  embedded `__NEXT_DATA__`, which itself issues a `member_ids`-filtered search.)
- **Single listing by id** → a **top-level `post_ids`** array returns exactly that
  listing. (`rel_type` must *not* be sent alongside these — it triggers a 500.)
- **Rich detail** (full description, attribute rows, member-since / response-time
  / total-listings / profile picture) → scraped **best-effort** from the listing
  page's App-Router RSC stream (`self.__next_f` chunks), because the JSON
  `/api/listing/{id}` endpoint requires a login (401). If the page format changes,
  these extras degrade gracefully to the API-level data.

### Verified API capabilities & limits

| Capability | Status |
|------------|--------|
| Full-text search, pagination, total counts | ✅ server-side |
| Sort: relevant / newest / cheapest / priciest | ✅ server-side (`sort_code`) |
| Category filter (`cat_ids`) + city filter (`city_ids`) | ✅ server-side |
| Per-query category facets | ✅ server-side |
| Per-seller listings (`member_ids`) + single listing (`post_ids`) | ✅ server-side, top-level body fields |
| Price-range / media filters | ⚙️ applied **client-side** (not honored in the search body) |
| Full description / attributes / rich seller profile | ⚙️ scraped **best-effort** from the listing page |
| "No exact match" signal | ❌ none — OpenSooq returns loosely-related fallbacks; handled client-side (`relevance_warning` / `strict_match`) |
| Phone unmasking | ⛔ requires a logged-in account (anonymous token → 401); intentionally not built — see [Limitations](#limitations--responsible-use) |

---

## Tool reference

All 15 tools are read-only. `query_ar` (Arabic) is optional but **strongly
recommended** — the Kuwait catalog is predominantly Arabic, so Arabic terms
return far more results (e.g. Rolex: 36 → 1358). Most tools take
`response_format: "markdown" | "json"`.

### Search & discovery

| Tool | Key parameters | Returns |
|------|----------------|---------|
| **`opensooq_search_listings`** | `query`, `query_ar`, `sort` (relevant/newest/cheapest/priciest), `category`, `city`, `page`, `limit`, `min_price`, `max_price`, `has_video`, `has_images`, `shops_only`, `verified_only`, `min_seller_rating`, `strict_match`, `response_format` | A page of listings (+ `total`, + a `relevance_warning` if the page looks like a fallback). |
| **`opensooq_get_listing`** | `listing_id`, `include_details` (default true), `response_format` | One listing + (best-effort) full description, attributes, and rich seller profile. |
| **`opensooq_latest_listings`** | `query`, `query_ar`, `category`, `city`, `limit` | Newest-first listings — "what just got posted". |
| **`opensooq_category_breakdown`** | `query`, `query_ar` | Which categories a term appears in, with counts. |

### Categories & cities

| Tool | Key parameters | Returns |
|------|----------------|---------|
| **`opensooq_list_categories`** | *(none)* | The top-level category taxonomy (id, EN/AR name, url slug). |
| **`opensooq_list_cities`** | *(none)* | The Kuwait cities/governorates usable as `city`. |
| **`opensooq_browse_category`** | `category` (required), `query`, `city`, `sort`, `page`, `limit` | Category-first browsing, optionally narrowed. |

### Pricing & deals

| Tool | Key parameters | Returns |
|------|----------------|---------|
| **`opensooq_market_summary`** | `query`, `query_ar`, `category`, `pages` (1-5) | min/max/avg/median/p25/p75 + cheapest & priciest links. |
| **`opensooq_price_distribution`** | `query`, `query_ar`, `category`, `pages`, `buckets` (2-12) | A price histogram. |
| **`opensooq_find_deals`** | `query`, `query_ar`, `category`, `pages`, `discount_pct` (default 30), `verified_only`, `limit` | Listings ≥N% below the median (outlier-trimmed). |
| **`opensooq_compare_prices`** | `queries` (2-5 terms), `pages` | Side-by-side median/avg per term. |

### Sellers & reputation

| Tool | Key parameters | Returns |
|------|----------------|---------|
| **`opensooq_top_sellers`** | `query`, `query_ar`, `category`, `pages`, `limit` | Sellers ranked by listing volume + rating. |
| **`opensooq_seller_listings`** | `member_id` (required), `sort`, `page` (1-20), `limit` | A seller's full inventory, paginated, with their total count. |
| **`opensooq_seller_profile`** | `member_id` (required), `max_pages` (1-5), `include_sample` | Reputation + inventory stats + category breakdown + scraped profile. No query needed. |
| **`opensooq_find_trusted_sellers`** | `query`, `query_ar`, `category`, `pages`, `min_rating` (default 4.0), `require_shop` | Verified / highly-rated sellers, to reduce scam risk. |

---

## Use cases & applications

### Ask it directly (in any connected client)

- *"What's the going rate for a used PlayStation 5 on OpenSooq?"* → `market_summary`
- *"Find iPhone 15 Pro deals at least 40% below market from verified sellers."* → `find_deals` (verified-only)
- *"Show me the cheapest verified-shop iPhone 15 listings in Hawally."* → `search_listings` (sort=cheapest, shops_only, verified_only, city)
- *"Is 128 or 256 GB cheaper right now — iPhone 14 vs 15 vs 16?"* → `compare_prices`
- *"Who are the biggest Toyota Land Cruiser dealers, and which are reputable?"* → `top_sellers` + `find_trusted_sellers`
- *"Pull everything seller 42899294 is selling, newest first."* → `seller_listings`
- *"Give me the full details and seller history for listing 281910976."* → `get_listing` + `seller_profile`

### Apps you can build on top

**For buyers**
- **Deal-alert agent** — schedule `find_deals` (verified-only) over a watchlist and notify on a hit. (Pairs well with a scheduled/cron agent.)
- **"Fair price" assistant** — paste any listing; it runs `market_summary` + `price_distribution` and says whether it's over/under market and which percentile it sits in.
- **Negotiation helper** — `compare_prices` across variants + the cheapest verified comps, to back up a lowball offer with evidence.

**For sellers / dealers**
- **Pricing copilot** — before listing, `market_summary` + `top_sellers` show the going rate and what competitors charge, suggesting an optimal price.
- **Competitor monitor** — track a rival's `seller_listings` / `seller_profile` over time to watch their inventory and pricing moves.

**For trust & safety**
- **Scam-risk scorer** — combine `find_trusted_sellers`, the relevance signal, and "too-good-to-be-true" deal detection to flag likely scams before a buyer makes contact.

**For resale / arbitrage**
- **Cross-platform arbitrage scanner** — compare OpenSooq P2P prices against retail or other marketplaces to surface buy-low/sell-high gaps.
- **Depreciation dashboard** — periodically snapshot `price_distribution` / `market_summary` for popular models to chart how prices move over time.

---

## Price-drop watcher (cron-ready)

The simplest "useful app" ships in this folder: [`watch.py`](watch.py) — a tiny
script that **alerts you when a listing drops below a target price**. It reuses
the exact same relevance + price-band filtering as the MCP server (so it won't
ping you about a Galaxy A52s or a 10 KWD installment-teaser), and it remembers
what it has already seen so you only hear about **new** matches.

It needs **only `httpx`** (no MCP/Pydantic), so it runs on any Python ≥3.9 — or
zero-setup via `uv run --directory /abs/path/opensooq-mcp watch.py …`.

### Try it once

```bash
# real Galaxy S26 Ultra under 320 KWD
python watch.py --query "samsung galaxy s26 ultra" \
                --query-ar "سامسونج جالاكسي اس 26 الترا" --max-price 320

# iPhone 15 Pro under 150 KWD, verified sellers only, ping a Discord/Slack/Telegram webhook
python watch.py --query "iphone 15 pro" --query-ar "ايفون 15 برو" \
                --max-price 150 --verified-only \
                --webhook "https://discord.com/api/webhooks/XXX/YYY"
```

Sample output (first run):

```
[2026-06-13 10:25Z] 3 NEW 'samsung galaxy s26 ultra' listing(s) <= 320 KWD:
  310 KWD | Samsung Galaxy S26 Ultra 256 GB in Kuwait City | Sharq, Kuwait City | Almohands. Shop 🏪 ★3.6(325) | https://kw.opensooq.com/search/282305058
  310 KWD | SAMSUNG S26 ULTRA 256 gb | West Abdullah Al-Mubarak, Farwaniya | khaled ★3.7(3) | https://kw.opensooq.com/search/281723370
  320 KWD | Samsung Galaxy S26 Ultra 512 GB in Hawally | Maidan Hawally, Hawally | Ahmed ★3.7(3) | https://kw.opensooq.com/search/282363402
```

Each run prints new hits, appends them to `watch-<query>.log`, optionally POSTs
to your webhook, and saves seen ids to `watch-<query>.json`. Flags: `--city`,
`--shops-only`, `--verified-only`, `--pages`, `--state`, `--log`, `--webhook`.

### Schedule it

**Linux/macOS (cron)** — check every 15 minutes:

```cron
*/15 * * * * cd /abs/path/opensooq-mcp && /abs/path/.venv/bin/python watch.py \
  --query "iphone 15 pro" --query-ar "ايفون 15 برو" --max-price 150 \
  --webhook "https://discord.com/api/webhooks/XXX/YYY" >> watch.cron.log 2>&1
```

Or with `uv` (no venv needed): replace the python call with
`uv run --directory /abs/path/opensooq-mcp watch.py …`.

**Windows (Task Scheduler)** — register a task that runs every 15 minutes:

```powershell
$py   = "C:\path\opensooq-mcp\.venv\Scripts\python.exe"
$args = 'watch.py --query "iphone 15 pro" --query-ar "ايفون 15 برو" --max-price 150'
$act  = New-ScheduledTaskAction -Execute $py -Argument $args -WorkingDirectory "C:\path\opensooq-mcp"
$trg  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
          -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "OpenSooq iPhone15Pro watch" -Action $act -Trigger $trg
```

Set `PYTHONIOENCODING=utf-8` in the environment (cron line / task) so Arabic
titles print cleanly. The webhook payload works as-is with Discord (`content`)
and Slack/Telegram-style (`text`) incoming webhooks.

---

## Data-quality handling

OpenSooq search results are noisy: accessories priced at ~1 KWD, troll listings
at "100 billion", and **loosely-related fallback results** for weak queries
(OpenSooq returns *something* rather than "no match"). The pricing, deals, and
seller tools defend against this with:

- **Query-relevance filtering** — drop listings whose title/description don't
  contain a query token. Search surfaces a `relevance_warning` when a page looks
  like a fallback, and `strict_match: true` removes that noise entirely.
- **A median price-band** — when computing medians, distributions, deals, and
  seller stats, ignore listings below 20% or above 5× the median, so a 1 KWD
  phone case can't masquerade as a "99% off" iPhone.

These invariants are asserted by [`troubleshoot.py`](troubleshoot.py),
which exercises every tool across many detailed Arabic + English product names.

---

## Limitations & responsible use

- **Read-only, single-market.** The server only reads public Kuwait (`kw`)
  classifieds. It never posts, edits, or deletes, and currency is KWD.
- **Unofficial API.** OpenSooq can change its endpoints or signing scheme at any
  time; the scraped "rich detail" extras are best-effort and degrade gracefully.
  Be a good citizen: keep request volume modest (the tools already cap pages 1-5)
  and don't hammer the API.
- **Seller phone numbers stay masked.** Each listing exposes a masked phone
  (`555471XX`) and a reveal key. Unmasking requires a **logged-in OpenSooq
  account** (the anonymous token gets a 401), so it is **intentionally not
  implemented**. Revealing a single number to contact a seller mirrors the
  site's "show phone" button and is legitimate; **bulk-harvesting phone numbers
  is out of scope and not supported.**

---

## Development & testing

Layout:

- [`install.py`](install.py) — the one-command installer: finds `uv`, provisions
  + verifies, and writes the config into every detected MCP client.
- [`.python-version`](.python-version) — pins Python `3.12` so `uv` never tries to
  download a newer one (the cause of the GitHub/DNS startup failures).
- [`opensooq_client.py`](opensooq_client.py) — the OpenSooq API
  client (handshake, search, `post_ids`/`member_ids` lookups, scraping, parsing).
  **No MCP/Pydantic deps** → unit-testable on any Python with `httpx`.
- [`server.py`](server.py) — the MCP tool layer (FastMCP + Pydantic
  input models + formatting + data-quality logic).
- [`watch.py`](watch.py) — the standalone, cron-ready price-drop watcher
  ([see above](#price-drop-watcher-cron-ready)); reuses the client's relevance +
  price-band filters, needs only `httpx`.
- [`troubleshoot.py`](troubleshoot.py) — a **live** diagnostic
  harness. It loads the tools via an AST shim (so it runs on Python 3.9 *without*
  the `mcp` SDK) and exercises all 15 across detailed Arabic + English products,
  asserting sort/filter/relevance/deal invariants.

```bash
# with uv (run from this folder; .python-version pins 3.12):
PYTHONIOENCODING=utf-8 uv run --with httpx --with pydantic troubleshoot.py   # ~40 live calls, pass/fail summary

# quick syntax check:
uv run --with httpx --with pydantic python -m py_compile server.py opensooq_client.py watch.py
```

(Or, with a Plan-B venv, swap `uv run …` for `.venv/bin/python`.)

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Failed to download …python-build-standalone…` / `dns error: No such host is known` / `calling "initialize": EOF` | `uv` tried to fetch a newer Python from GitHub and the network/DNS is blocked. The bundled **`.python-version`** (3.12) prevents this — make sure your `--directory` points at this folder (which contains it), or re-run `python install.py`. |
| Client log: `uv: command not found` | GUI clients may not inherit your shell `PATH`. Use the **absolute path to `uv`** in `command` (`which uv` / `where.exe uv`) — `install.py` does this automatically. |
| `could not attach to MCP server` / server disconnected | A wrong `--directory` (it must point at the folder that contains `server.py` **and** `.python-version`), or the download error above. Re-run `python install.py` and check the client's MCP log. |
| First request hangs a few seconds | Normal — `uv` is provisioning Python + deps that one time. Instant afterward. |
| Client shows the server but no tools | Wrong `--directory` path (must point at the folder containing `server.py`). |
| Garbled/`?` Arabic text | Set `"env": { "PYTHONIOENCODING": "utf-8" }` in the MCP config. |
| Empty results for a real product | OpenSooq may have returned fallbacks — add `query_ar` (Arabic) and/or set `strict_match: true`. |
| `Error: … 401` | A stale device registration; the client auto-retries once. If it persists, OpenSooq may have changed the signing scheme. |
| `Error: … 429` | Rate-limited. Slow down / reduce `pages`. |
| Verify the server in isolation | `npx @modelcontextprotocol/inspector uv run --directory /abs/path/opensooq-mcp server.py` |
