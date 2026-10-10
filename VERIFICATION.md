# Verification — 10 October 2026

Upstream baseline: `1f87cac1a07b6bda74a98fc2ee6caebe7e0c0bba`.

## Offline

- Python 3.12, MCP SDK v1, httpx and Pydantic v2.
- 25 regression tests, including a real stdio MCP subprocess handshake,
  listing all 15 tools and calling the Jordan city reference tool.
- Mocked requests exercise country/currency headers, anonymous authentication
  retry, HTTP throttling retry, response schema changes, and restricted redirects.
- Parsing, pagination, sample ranking, interpolated quartiles, equal-price
  histograms, currency isolation, minimum seller reviews and watcher state tested.
- Installer print-only mode tested without invoking runtime provisioning.
- Kuwait configuration import and Hawally lookup passed in a separate process;
  Kuwait live requests were not tested.

## Live Jordan smoke check

`troubleshoot.py --live` passed:

- Jordan anonymous registration and search endpoint returned HTTP 200.
- Search scoped to Amman (`59`) and Mobile - Tablet (`5505`) returned 3 requested
  listings, each in JOD and Amman.
- Single-listing lookup returned the requested ID.
- Listing enrichment followed one same-host redirect and returned detail data.
- One-page price summary returned 23 comparable priced listings in JOD.
- Catalog reference tools returned 14 cities and 17 top-level categories.
- All 15 MCP tools loaded.

These are point-in-time observations. Listings and prices change. The live check
covers a small mobile-phone sample; every category/city combination and seller
lookup was not live-tested. No seller was contacted, no phone was revealed, and
no listing was modified. No MCP client configuration or scheduler was installed.

GitHub Actions runs the offline suite on Python 3.10, 3.12 and 3.13; see the PR
checks for hosted results rather than treating the local 3.12 result as proof of
all three versions.
