# Changelog

## 0.3.0 — 10 October 2026

- Fix nested OpenSooq attribute extraction; preserve semantic field keys, raw
  values, display values and source evidence.
- Add conservative car/property specifications, conflict detection, explicit
  mileage/area units and domain-specific comparison cohorts.
- Add local vehicle-year/mileage and property-room/area/rental-period/neighborhood
  filters, plus attribute/location grouped price summaries.
- Search both supplied English/Arabic terms with ID deduplication, independent
  cursors, provenance, a shared maximum-five-page request budget and partial errors.
- Merge detail attributes and recompute specifications/quality after enrichment.
- Abstain from deals on partial samples or unknown rent price billing periods;
  advertised contract term does not establish price cadence.
- Add read-only local/probe diagnostics and safe, capped CSV/JSON export.
- Preserve all 15 existing read-only MCP tools and original MIT attribution.

### Compatibility notes

- `query_ar` now supplements `query`; it no longer replaces it. Fuzzy totals for
  both queries are separate, and the combined total is unknown/null.
- Car/property cohorts become stricter; incomplete specifications may yield fewer
  deal candidates. Conflicting specifications are flagged and excluded by default.
- Bilingual watch defaults hash both query terms; pass `--state` to keep a chosen
  existing state file. Single-query default filenames remain unchanged.

## 0.2.0 — 10 October 2026

- Conservative bilingual identity matching with exact phone generations/tiers.
- Explicit condition/capacity filters and explainable offer-risk flags.
- Model/specification cohorts and evidence-backed deal baselines; abstain for
  insufficient samples and unknown specifications.
- Structured Next.js parsing recovers nested seller profiles and full descriptions;
  page listing identity and redirect destinations are validated.
- Consistent empty JSON results and MCP protocol errors.
- Truthful unknown dates, typed upstream metadata, deduplicated seller inventory.
- Watcher overlap locks, atomic writes, robust state validation and webhook chunking.
- Installer dependency provisioning fix, selected-client mode and config validation.
- Locked dependencies, 91 regression tests, full live stdio verification and a
  Linux/macOS/Windows CI matrix.

### Response behavior changes

- Network/API failures now raise MCP tool errors instead of returning `Error:` text
  as a successful result. Comparison calls keep per-query failures in their JSON.
- Missing listing IDs return `found=false`. Unknown posting dates are null.
- Empty discovery/price results remain structured JSON in JSON mode.
- Deal medians/counts are provided per cohort and per candidate; there is no single
  cross-condition/storage deal baseline.
- Search defaults to excluding flagged offers; use `include_risky=true` when
  deliberately inspecting accessories, payment ads or other flagged listings.
- Explicit phone generation queries match an exact tier; use a broad family query
  when searching several tiers.
