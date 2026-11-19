# Stale Analysis State Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_analysis_state_age_minutes`, model ID, source cycle, and run ID.
- Identify the last accepted source, forcing, Slurm, and QC evidence bundle.

## Recovery Steps

1. Validate source cycle freshness and best-available lineage.
2. Re-run analysis from the last accepted state snapshot.
3. Block publication if QC, frequency, tile, API, or frontend stages are stale.

## Residual Risks

Replaying from an old state can mask source gaps. Final release remains blocked until the full stage chain is accepted.
