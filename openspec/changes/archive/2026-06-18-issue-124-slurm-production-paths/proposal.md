## Why

Issue #124 tracks a production-path split: M3 Forecast uses `infra/sbatch` and real Slurm job type mappings, while Analysis and legacy Forecast still depend on legacy templates or rendered `script` payloads that real Slurm ignores. Hindcast also has its own Slurm submission path and can fall back to metadata-only forcing. These inconsistencies can pass mock tests while failing in a real Slurm deployment.

## What Changes

- Make Forecast M3, Analysis, and Hindcast job type/template/manifest contracts explicit and testable.
- Move Analysis real Slurm execution to the canonical `infra/sbatch` template and `config/job_type_templates.yaml` mapping path, or otherwise make any rendered-script behavior explicit, safe, and covered.
- Ensure real Slurm gateway tests cover submit/status/array/log/cancel and the production template mapping used by Analysis/Hindcast.
- Clarify or retire `workers/sbatch_templates` as legacy/non-production so production code does not silently depend on it.
- Prevent Hindcast metadata-only forcing from entering SHUD runtime and ensure hindcast Slurm manifests carry enough forcing/run context for real execution.

## Capabilities

### New Capabilities

- `slurm-production-orchestration`: Forecast, Analysis, and Hindcast have a consistent production Slurm contract with canonical template mappings, manifest fields, and fake real-gateway regression coverage.

## Impact

- Affects orchestrator stage definitions and Slurm client submission payloads.
- Affects `config/job_type_templates.yaml`, `infra/sbatch/`, and legacy template documentation.
- Affects Hindcast forcing/runtime guards.
- Requires backend worker/orchestrator tests plus real-gateway fake binary tests.
