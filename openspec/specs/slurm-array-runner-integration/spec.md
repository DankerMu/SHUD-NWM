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
AND on database-backed runtimes with runtime roots required, the scheduler runtime-root preflight planes (the lock/evidence arm and the full runtime arm alike) reach the same verdict as the storage preflight for the same configured root — the root is excluded from the effective allowed roots that serve as approved containment bases, a structured scheduler-root blocker in the existing `SCHEDULER_ROOT_*` family records why, and the preflight payload's allowed-roots evidence never lists the excluded root — so for a given filesystem state the two evidence planes produced by one run never contradict each other and the adjudication never escapes as an unhandled exception on any supported CPython version
AND on database-backed runtimes with runtime roots not required, the not-required preflight payload shares the same adjudication — its allowed-roots evidence never lists the unresolvable root, the adjudication never escapes as an unhandled exception on any supported CPython version, and no runtime-root blocker is emitted (that payload declares no containment adjudication and carries no blocker channel; when Slurm execution is enabled the storage preflight remains the blocker-bearing plane for that configuration)
AND scheduler configuration construction never aborts because an allowed storage root is unresolvable; the configuration layer hands down the canonicalised value without adjudicating and classification is owned by the storage preflight
AND on db-free runtimes the db-free lane keeps the existing lexical-fallback tolerance for such a root without a blocker, and the db-free selector's allowed-roots lane records its existing unresolvable-root rejection for such a root instead of admitting it
AND on a database-backed run that exercises the db-free-lane containment checks (repair authority), the unresolvable root is excluded from that lane's containment bases and a structured blocker in that lane's existing family records why — the lexical tolerance is a property of the db-free lane, not of database-backed runs through it
AND an allowed root that remains merely missing after canonicalization (ENOENT) is never treated as unsafe and never escapes configuration construction or the storage preflight as an unhandled exception on any runtime
AND blocker paths obey the runtime-root preflight's existing evidence-safe masking discipline.

#### Scenario: unresolvable general storage root at configuration construction

WHEN a database-backed runtime configures an object-store, published-artifact, log, runtime, or temp root — a root that is not a containment base for other configured paths — whose final path component cannot be canonically resolved for a reason other than absence (for example a symlink loop)
THEN scheduler configuration construction never aborts: the configuration layer hands down the non-strict canonicalised value without adjudicating, producing the same canonical form and the same subsequent verdict across supported CPython versions
AND when Slurm execution is enabled, for such roots consulted by the storage preflight (object-store, log, and runtime roots), the storage preflight records the corresponding unsafe-path storage preflight blocker and rejects submission before creating Slurm jobs
AND a root that is merely missing (ENOENT) keeps the existing construction-time semantics — configuration construction performs no existence validation and the canonicalised value is identical to the previous non-strict resolution product
AND on db-free runtimes the same inputs keep the existing graceful-degradation behavior unchanged.

#### Scenario: safe template and environment export

WHEN the scheduler submits through the real or mock Slurm gateway
THEN the submitted job uses only an allowlisted sbatch template for the requested stage
AND exported environment/config values are shell-safe, bounded, and redacted from evidence when sensitive
AND secret leakage, shell metacharacter injection, and unrecognized template names are rejected before submission.

### Requirement: Configuration construction survives symlink loops in containment bases and parent segments

Scheduler configuration construction SHALL apply the established
strict-realpath-then-non-strict paradigm to the containment-base confinement
helper, to both preserve-final parent-segment helpers, and to the
safe-directory final-component guard's parent-segment resolution, so that a
symlink loop in WORKSPACE_ROOT's or NHMS_SCHEDULER_LOCK_ROOT's final segment,
or in any configured path's parent segment, produces the same exception type
and the same subsequent verdict on CPython 3.11/3.12 as on 3.13+, and never
aborts construction with an errno-less RuntimeError in those geometries. The
safe-directory guard's own final-segment resolve (a loop as the evidence
directory's final segment inside the workspace) remains version-divergent and
is tracked separately as issue #1544 — it is outside this requirement.

#### Scenario: WORKSPACE_ROOT final-segment loop converges to the structured safe-directory refusal

WHEN WORKSPACE_ROOT is a symlink loop's final segment
THEN configuration construction raises the existing structured safe-directory
ValueError identically on CPython 3.11/3.12 and 3.13+ (the current message
attributes the evidence_dir field; that attribution is a known defect tracked
as issue #1545, and this scenario locks the refusal class, not the string)

#### Scenario: NHMS_SCHEDULER_LOCK_ROOT final-segment loop converges to the structured containment refusal

WHEN NHMS_SCHEDULER_LOCK_ROOT is a symlink loop's final segment
THEN configuration construction raises the existing structured containment
ValueError (carrying the field name) identically on CPython 3.11/3.12 and
3.13+

#### Scenario: parent-segment loop no longer aborts construction on 3.11/3.12

WHEN any env-driven root's parent segment contains a symlink loop under the
db-backed configuration arm
THEN configuration construction on CPython 3.11/3.12 produces the same
canonical form and the same subsequent verdict as 3.13+ instead of raising an
errno-less RuntimeError. The db-free preserve-final arm
(`_safe_preserve_final_component`) is outside this requirement: on 3.11/3.12
it still swallows the loop and returns the raw path — that residue belongs to
issue #1627, the family-level ruling on whether an ENOENT non-strict fallback
must be loop-filtered (it was tracked by #1400 until that issue was closed
without touching this arm)

#### Scenario: ENOENT and non-loop containment semantics are unchanged

WHEN a configured path merely does not exist yet, or a non-loop path violates
workspace containment
THEN the existing no-existence-validation construction semantics and the
existing "must be under workspace_root" refusal are byte-for-byte unchanged

