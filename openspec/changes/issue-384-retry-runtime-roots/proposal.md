## Why

Manual retry for shared source-cycle downloads can reconstruct a minimal Slurm
manifest without the production object-store runtime roots. In production this
allowed an IFS retry to write raw bundles under the workspace tree instead of
the configured object-store root, leaving downstream stages unable to consume
the repaired cycle without manual correction.

## What Changes

- Preserve object-store and published-artifact runtime roots when submitting a
  manual retry for `download_source_cycle`.
- Fail closed with an actionable retry error when a shared source-cycle retry
  cannot resolve required runtime roots safely.
- Record redacted runtime-root resolution evidence for retry submission success
  and failure.
- Add production-like regression coverage where `WORKSPACE_ROOT` and
  `OBJECT_STORE_ROOT` differ.
- Document safe operator recovery for legacy retries that already wrote raw
  bundles to the workspace tree.

## Capabilities

### New Capabilities

- `retry-runtime-roots`: Manual retry submissions preserve durable runtime-root
  contracts for shared source-cycle downloads.

### Modified Capabilities

None.

## Impact

- Runtime retry path: `services/orchestrator/retry.py`.
- Slurm manifest and env rendering consumers: `services/slurm_gateway/*`,
  `services/orchestrator/chain.py`, and `infra/sbatch/download_source_cycle.sbatch`.
- Tests: retry service and Slurm route/template contract coverage.
- Docs: node-22/operator retry runbook guidance.
- No frontend, display-readonly, database schema, or stale stage-evidence
  supersession change is intended.
