# slurm-array-runner-integration (delta)

## MODIFIED Requirements

### Requirement: Array-capable model stages

The scheduler SHALL support array-capable model stages for forcing, forecast, and parse across multiple registered models in a source/cycle. Display/tile publication SHALL remain cycle-level unless a separate per-model publish contract is introduced.

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

#### Scenario: unresolvable allowed storage root

WHEN Slurm execution is enabled and a configured allowed storage root cannot be canonically resolved for a reason other than absence (for example a path whose ancestor is a regular file or an untraversable directory), as detected by errno from strict resolution
THEN on database-backed runtimes the storage preflight excludes that root from the effective allowed roots, records a storage preflight blocker, and rejects submission before creating Slurm jobs, with identical storage-preflight behavior across supported CPython versions
AND scheduler configuration construction never aborts because an allowed storage root is unresolvable; the configuration layer passes the value through and classification is owned by the storage preflight
AND on db-free runtimes the root keeps the existing lexical-fallback tolerance without a blocker
AND an allowed root that remains merely missing after canonicalization (ENOENT) is never treated as unsafe and never escapes configuration construction or the storage preflight as an unhandled exception on any runtime.

#### Scenario: safe template and environment export

WHEN the scheduler submits through the real or mock Slurm gateway
THEN the submitted job uses only an allowlisted sbatch template for the requested stage
AND exported environment/config values are shell-safe, bounded, and redacted from evidence when sensitive
AND secret leakage, shell metacharacter injection, and unrecognized template names are rejected before submission.
