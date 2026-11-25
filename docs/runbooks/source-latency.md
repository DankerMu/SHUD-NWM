# Source Latency Alert Runbook

## Preconditions

- `nhms-monitor` links this runbook from two alerts, both carrying `alert`, `severity` and `runbook`:
  `scheduler_evidence_missing` (`observed` = the evidence status, no `threshold`) and
  `scheduler_stale` (`observed` = age of the latest evidence in minutes, with `threshold`).
- Neither names a source or a cycle: read the latest scheduler evidence to see which source the scheduler is waiting on.

## Recovery Steps

1. Check upstream availability and retry window for the delayed source.
2. If the cycle remains unavailable, select the documented best-available fallback.
3. Record the fallback lineage before allowing downstream forcing or publication.

## Residual Risks

Fallback data may reduce forecast freshness.
