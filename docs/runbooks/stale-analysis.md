# Stale Analysis State Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_analysis_state_age_minutes`, model ID, source cycle, and run ID.
- Identify the last accepted source, forcing, Slurm, and QC evidence bundle.

## Commands

```bash
uv run nhms-production validate-e2e --evidence-root artifacts/production-closure --run-id stale-analysis-check
uv run pytest -q tests/test_production_e2e_validation.py
```

## Expected Evidence

- `e2e/stage_manifest.json` records the freshest accepted stage and blockers.
- `ops/monitoring_alerts.json` records stale-state age and threshold.

## Recovery Steps

1. Validate source cycle freshness and best-available lineage.
2. Re-run analysis from the last accepted state snapshot.
3. Block publication if QC, frequency, tile, API, or frontend stages are stale.

## Residual Risks

Replaying from an old state can mask source gaps. Final release remains blocked until the full stage chain is accepted.
