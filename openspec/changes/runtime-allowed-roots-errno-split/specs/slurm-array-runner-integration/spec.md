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
THEN on database-backed runtimes, on every scheduler pass that reaches the storage preflight, the storage preflight excludes that root from the effective allowed roots, records a storage preflight blocker, and rejects submission before creating Slurm jobs, with identical storage-preflight behavior across supported CPython versions
AND on database-backed runtimes with runtime roots required, the scheduler runtime-root preflight planes (the lock/evidence arm and the full runtime arm alike) reach the same verdict as the storage preflight for the same configured root — the root is excluded from the effective allowed roots that serve as approved containment bases, a structured scheduler-root blocker in the existing `SCHEDULER_ROOT_*` family records why, and the preflight payload's allowed-roots evidence never lists the excluded root — so the two evidence planes produced by one run never contradict each other and the adjudication never escapes as an unhandled exception on any supported CPython version
AND on database-backed runtimes with runtime roots not required, the not-required preflight payload shares the same adjudication — its allowed-roots evidence never lists the unresolvable root, the adjudication never escapes as an unhandled exception on any supported CPython version, and no runtime-root blocker is emitted (that payload declares no containment adjudication and carries no blocker channel; when Slurm execution is enabled the storage preflight remains the blocker-bearing plane for that configuration)
AND scheduler configuration construction never aborts because an allowed storage root is unresolvable; the configuration layer hands down the canonicalised value without adjudicating and classification is owned by the storage preflight
AND on db-free runtimes the db-free lane keeps the existing lexical-fallback tolerance for such a root without a blocker, and the db-free selector's allowed-roots lane records its existing unresolvable-root rejection for such a root instead of admitting it
AND on a database-backed run that exercises the db-free-lane containment checks (repair authority), the unresolvable root is excluded from that lane's containment bases and a structured blocker in that lane's existing family records why — the lexical tolerance is a property of the db-free lane, not of database-backed runs through it
AND an allowed root that remains merely missing after canonicalization (ENOENT) is never treated as unsafe and never escapes configuration construction or the storage preflight as an unhandled exception on any runtime
AND blocker paths obey the runtime-root preflight's existing evidence-safe masking discipline.

#### Scenario: safe template and environment export

WHEN the scheduler submits through the real or mock Slurm gateway
THEN the submitted job uses only an allowlisted sbatch template for the requested stage
AND exported environment/config values are shell-safe, bounded, and redacted from evidence when sensitive
AND secret leakage, shell metacharacter injection, and unrecognized template names are rejected before submission.
