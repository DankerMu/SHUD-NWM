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

## Residual Risks

Fast validation does not replace sustained load testing. Production readiness remains gated until live alert and performance evidence is accepted.
