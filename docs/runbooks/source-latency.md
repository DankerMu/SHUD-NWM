# Source Latency Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_source_cycle_latency_minutes`, source ID, cycle time, and run ID.
- Verify the current ops evidence bundle is under `artifacts/production-closure/<run_id>/ops/`.

## Commands

```bash
uv run nhms-production validate-met --evidence-root artifacts/production-closure --run-id source-latency-check
uv run nhms-production validate-e2e --evidence-root artifacts/production-closure --run-id source-latency-e2e
```

## Expected Evidence

- `met/summary.json` records source discovery status, cycle freshness, and best-available lineage.
- `ops/monitoring_alerts.json` links this runbook and records the breached threshold.

## Recovery Steps

1. Check upstream availability and retry window for the delayed source.
2. If the cycle remains unavailable, select the documented best-available fallback.
3. Record the fallback lineage before allowing downstream forcing or publication.

## Residual Risks

Fallback data may reduce forecast freshness. Final readiness remains blocked until live source freshness and alert delivery receipts are reviewed.
