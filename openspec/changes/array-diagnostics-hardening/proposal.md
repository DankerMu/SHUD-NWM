## Why

Slurm array stdout/stderr currently uses the first task's `run_id` as the directory for every member, which has already caused operators to attribute another basin's failure to the cohort leader (#1742). A separate array-accounting normalizer raises a bare `IndexError` for empty or missing state instead of producing a classified failed task (#1539); both defects make the array diagnostic plane untrustworthy.

## What Changes

- Store all four production array templates' scheduler logs in a cohort-neutral, submission-specific directory that is bound to the immutable manifest index and exists before `sbatch` runs.
- Return each discovered array log with its exact `task_id`, `model_id`, and `run_id`; retain bounded, fail-safe lookup for legacy leader-run paths and for gateway restart.
- Treat empty or whitespace-only orchestrator array task state as `failed`, preserving the existing `succeeded|cancelled|failed` return domain and preventing bare `IndexError` on sacct and gateway-payload paths.
- Add contract, regression, and node-22 live evidence for both issues. No public endpoint is removed.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `real-slurm-gateway-contract`: array logs use neutral storage, expose proven member identity, and remain readable across old/new layouts and restart.
- `job-array-orchestration`: absent array task state converges to the existing failed accounting status without an unclassified exception.

## Impact

Affected surfaces are the four `infra/sbatch/*_array.sbatch` templates, `services/slurm_gateway/real_backend.py` and its log response, `services/orchestrator/chain_array_accounting.py`, owning tests, and the two OpenSpec contracts. New log placement changes only diagnostics; task manifests, worker execution, and artifact ownership remain unchanged. The fixture is `expanded` with `high` repair intensity because it changes file paths, an API payload, restart discovery, and a shared accounting boundary.
