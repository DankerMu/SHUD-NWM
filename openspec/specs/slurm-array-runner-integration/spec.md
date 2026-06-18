# slurm-array-runner-integration Specification

## Purpose
TBD - created by archiving change m20-production-multibasin-continuous-automation. Update Purpose after archive.
## Requirements
### Requirement: Slurm-first heavy execution

The production scheduler SHALL submit heavy download/canonical/forcing/SHUD/parse/publish work through the Slurm gateway by default when Slurm execution is enabled.

#### Scenario: compute-node database preflight

WHEN Slurm execution is enabled and `DATABASE_URL` points to localhost or is missing
THEN the scheduler rejects submission before creating Slurm jobs
AND records a preflight blocker explaining the required compute-node reachable database endpoint.

#### Scenario: project-local runtime roots

WHEN Slurm jobs are submitted
THEN workspace, object-store, logs, ecCodes/runtime dependencies, and model artifacts resolve under configured project or production storage roots
AND jobs do not write large artifacts to the system disk by default.

### Requirement: Array-capable model stages

The scheduler SHALL support array-capable model stages for forcing, forecast, parse, and frequency computation across multiple registered models in a source/cycle. Display/tile publication SHALL remain cycle-level unless a separate per-model publish contract is introduced.

#### Scenario: partial array failure

WHEN one model task fails in a multi-model array but other model tasks succeed
THEN task-level status is preserved
AND downstream stages receive reduced manifests containing only eligible successful model tasks
AND the source/cycle aggregate status uses existing `_partial` semantics such as `forcing_ready_partial` or `parsed_partial` rather than failed or succeeded globally.

#### Scenario: Slurm accounting evidence

WHEN a Slurm job or array completes
THEN job id, array task ids, state, exit code, elapsed time, MaxRSS when available, and log URI are recorded in `ops.pipeline_job` fields where available and in `ops.pipeline_event.details` or scheduler evidence artifacts for metrics without dedicated columns.

#### Scenario: unsafe storage roots

WHEN Slurm execution is enabled and workspace, object-store, runtime dependency, or log roots are missing, outside configured production/project roots, or not visible to compute nodes
THEN the scheduler rejects submission before creating Slurm jobs
AND records a storage preflight blocker.

#### Scenario: safe template and environment export

WHEN the scheduler submits through the real or mock Slurm gateway
THEN the submitted job uses only an allowlisted sbatch template for the requested stage
AND exported environment/config values are shell-safe, bounded, and redacted from evidence when sensitive
AND secret leakage, shell metacharacter injection, and unrecognized template names are rejected before submission.

