# Verification — 10 October 2026

Original upstream baseline: `1f87cac1a07b6bda74a98fc2ee6caebe7e0c0bba`.

## Local regression and startup verification

- **91 regression tests passed** on Python 3.12.
- Ruff lint and format checks passed.
- The actual production launch command (`uv run --locked --directory … server.py`)
  completed MCP initialize/list-tools/call-tool and returned 14 Jordan cities.
- The installer provision/import check loaded all 15 tools with explicitly
  installed dependencies. No user client configuration was edited.
- Dependency locks exist for the project, standalone server and watcher.

Tests cover bilingual identity anchors, exact phone tiers and generations,
wrong products mentioned in descriptions, Arabic/Persian digits, capacity units,
condition conflicts, risk flags, separate new/used cohorts, small/unknown cohort
abstention, equal-price histograms, quartiles, pagination, empty JSON responses,
MCP error propagation, authentication/schema failures, redirects, nested Next.js
JSON and full descriptions, seller profile parsing, state locking/atomic writes,
watcher repeat runs and price drops, failed webhook state preservation, payload
chunking, and installer provisioning/config validation.

## Live checks through MCP stdio

The full opt-in harness exercised **all 15 tools** through the actual MCP protocol:

- Both catalog reference tools.
- Search, newest listings, category facets and category browsing.
- Exact listing lookup with full description and nested seller profile.
- Price summary, distribution, deal candidates and query comparisons.
- Seller ranking, inventory, enriched profile and reviewed/verified filtering.

Additional live searches passed for **Toyota Prius** and **apartments for rent**
in Amman. A nonexistent product returned an empty JSON array, and invalid input
returned a protocol-level tool error without contacting sellers.

The final core recheck after stricter title matching/page identity validation passed
at **12:49 Amman time**: 5 requested iPhone 15 Pro search results, 26 comparable
priced listings across 9 cohorts, full description and matching seller profile.
Numbers describe a point-in-time sample, not current prices or full inventory.

## Hosted checks

The GitHub Actions workflow runs locked dependency installs, all regressions, lint
and formatting on:

- Linux: Python 3.10, 3.12 and 3.13.
- macOS: Python 3.12.
- Windows: Python 3.12.

`UV_PYTHON` is set for each matrix job so `.python-version` cannot silently change
the requested test interpreter. Consult PR checks for the current hosted status.

## Limits of verification

Live tools were checked with bounded Jordan samples; every city/category/product
combination was not tested. Strict matching and quality flags are conservative
heuristics with possible false positives/negatives. The API and page layout remain
unofficial dependencies that can change. Unknown comparison specifications abstain
from deal calculation; aggregate summaries still require cohort interpretation.

Real Discord/Slack notifications were not sent. Notification acceptance/failure,
chunking and state preservation were tested with mocked HTTP. No seller was
contacted, no phone was revealed, no listing was changed, and no scheduler or user
MCP configuration was installed. Kuwait configuration remains available; live
Kuwait operation was not verified in this pass.
