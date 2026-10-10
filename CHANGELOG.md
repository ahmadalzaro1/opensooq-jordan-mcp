# Changelog

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
