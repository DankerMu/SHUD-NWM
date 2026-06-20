# API Latency Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_api_p95_latency_ms`, route group, sample window, and run ID.
- Identify whether latency comes from database, object-store, tile, or frontend-facing calls.

## Commands

```bash
uv run nhms-production validate-scale --evidence-root artifacts/production-closure --run-id api-latency-check
uv run pytest -q tests/test_api_contract.py tests/test_production_scale_validation.py
```

## Expected Evidence

- `scale/query_latency_evidence.json` records p95 samples, thresholds, and query plan hashes.
- `ops/monitoring_alerts.json` records the breached API p95 threshold.

## Recovery Steps

1. Inspect recent query plans, cache status, and object-store latency.
2. Reduce oversized bbox or long time-range requests if they are driving the breach.
3. Re-run scale validation after remediation.

## water-level dead variant 22s cold path (已移除, 2026-06-20)

Historical context for any p95 latency alerts referencing the canonical
21.8 s baseline on `/api/v1/layers`:

- **Root cause**: a `water-level` MVT variant remained in the backend layer
  catalog and SQL path even though no frontend consumer rendered it. Every
  `/api/v1/layers` cold request fanned out the dead variant's
  `hydro.river_timeseries` SkipScan (92 M rows), contributing the majority
  of the 21.8 s cold tail. A second coupled vector was the frontend
  hardcoding `flood_product_ready=true` on every `/api/v1/runs` request
  (including the default discharge path), turning a non-blocking enrichment
  gate into a blocking 12 s aggregation.
- **Removed**: Epic [#579](https://github.com/DankerMu/SHUD-NWM/issues/579),
  PR 1/7..PR 5/7 (#587, #588, #589, #590, #591) collectively deleted the
  dead variant from the backend catalog + SQL path + frontend bundle, split
  map-bootstrap from enrichment loading, and layer-gated the
  `flood_product_ready` filter.
- **Receipt**: live node-27 measurement at master `122ea95` —
  [`display-bootstrap-decoupling-20260620.md`](receipts/display-bootstrap-decoupling-20260620.md).
  Pre-merge warm probe reproduced 21,430 ms; post-merge cold median 413 ms
  (≥ 51.9× speedup lower bound); warm steady-state 2–3 ms.
- **Residual gap**: cold p95 413 ms still exceeds the spec's 200 ms p95
  cold floor by ~213 ms; tracked as follow-up
  [#593](https://github.com/DankerMu/SHUD-NWM/issues/593) (uvicorn
  `--preload` / sqlalchemy pool pre-init / spec amendment).

If a new `/api/v1/layers` p95 alert fires with a value ≥ 21 s, do NOT
assume the dead variant has reappeared — that variant is gone from the
code path. Inspect for unrelated regressions: dead-row bloat on
`hydro.river_timeseries`, a new heavy aggregation joined into the catalog
response, or sqlalchemy pool exhaustion. The 51.9× speedup is the new
expected baseline; anything ≥ 1 s cold is a regression vs receipt.

## Residual Risks

Fast validation does not replace sustained load testing. Production readiness remains gated until live alert and performance evidence is accepted.
