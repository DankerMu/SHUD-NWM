# Source Latency Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_source_cycle_latency_minutes`, source ID, cycle time, and run ID.

## Recovery Steps

1. Check upstream availability and retry window for the delayed source.
2. If the cycle remains unavailable, select the documented best-available fallback.
3. Record the fallback lineage before allowing downstream forcing or publication.

## Residual Risks

Fallback data may reduce forecast freshness. Final readiness remains blocked until live source freshness and alert delivery receipts are reviewed.
