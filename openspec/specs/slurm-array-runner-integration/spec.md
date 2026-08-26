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
issue #1627, which adjudicates this surviving-`Path.resolve` sub-family
alongside — and explicitly distinguished from — the ENOENT non-strict fallback
family (#1400 tracked it previously; the PR that closes #1400 leaves this arm
untouched and re-points it here)

#### Scenario: ENOENT and non-loop containment semantics are unchanged

WHEN a configured path merely does not exist yet, or a non-loop path violates
workspace containment
THEN the existing no-existence-validation construction semantics and the
existing "must be under workspace_root" refusal are byte-for-byte unchanged

### Requirement: Scheduler configuration construction SHALL survive an undeterminable home directory on both database arms

Scheduler configuration construction SHALL treat a configured path whose leading `~` component has no determinable home directory as a value to hand down to the storage preflight, not as a construction-time abort, on the database-backed arm exactly as on the database-free arm, because classification of unusable roots belongs to the preflight and a construction-time abort produces no structured blocker at all.

The two arms SHALL produce byte-identical products for the same such input, so that the convergence is pinned by an equality of results rather than merely by the absence of an exception. This applies to the two configuration fields that bypass the deliberate re-raise helper and reach the module's own bare expansions — the allowed storage roots and the log root. Every other root field aborts earlier in the deliberate re-raise, which is an existing design ruling and is out of scope; a third bare expansion in the module is reachable only through the compatibility forwarders and is closed as a ledger item, not because a production field reaches it.

#### Scenario: An allowed storage root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** an allowed storage root configured as `~<unknown user>/roots`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds instead of aborting with an errno-less `RuntimeError`
- **AND** the resulting value is byte-identical to the value the database-free arm produces for the same input

#### Scenario: A log root naming an unknown user constructs successfully on the database-backed arm

- **GIVEN** a log root configured as `~<unknown user>/logs`
- **WHEN** the scheduler configuration is constructed with the database-backed arm selected
- **THEN** construction succeeds and the value is byte-identical to the database-free arm's value

#### Scenario: The compatibility-surface expansion helper stops throwing bare on the same input

- **GIVEN** the module's non-relative preserve-final-component helper, `_config_path_preserve_final_component`, called directly with `~<unknown user>/workspace`
- **WHEN** it expands the value
- **THEN** it returns the same product the database-free arm would produce
- **AND** it does not raise an errno-less `RuntimeError`

#### Scenario: The storage preflight still classifies the unusable root structurally

- **GIVEN** a configuration constructed from such a value
- **WHEN** the storage preflight runs
- **THEN** it returns its structured blocked result rather than raising, the unusable root being admitted by the tolerance arm for a merely missing path and classified through the consequential root blockers

### Requirement: The safe-directory guard SHALL resolve a final-segment symlink identically on every supported interpreter

The safe-directory final-component guard SHALL decide a final-segment symlink by strict real-path resolution and SHALL refuse a resolution loop with the module's structured configuration error on every supported CPython, because non-strict resolution raises an errno-less `RuntimeError` on CPython 3.11 and 3.12 and silently adopts the loop as the field's value on 3.13 and later.

Every strict real-path failure other than a loop SHALL first fall back to non-strict real-path resolution, preserving the established resolution product for a target that does not exist, a target whose parent denies traversal, or a target reached through a regular file. The fallback SHALL NOT itself be treated as proof that the target is absent or a directory: the subsequent final-target classification SHALL distinguish absence, directory, non-directory, and metadata denial without relying on the interpreter-dependent exception swallowing of `Path.exists()` or `Path.is_dir()`. `EACCES` or `EPERM` at that classification boundary SHALL fail closed with the guard's structured `ValueError` safety contract, never leak a raw permission exception and never be accepted as a missing target.

The loop refusal SHALL be recognised by the loop error number obtained from the `errno` module rather than by a hard-coded integer. The guard's separate metadata-lookup failure handler SHALL keep its existing refusal for every error number other than a loop, so that the non-loop geometries already pinned against that handler stay green verbatim.

#### Scenario: A final-segment loop inside the workspace is refused identically on both interpreter arms

- **GIVEN** an evidence directory whose final segment is a symlink loop located inside the workspace root
- **WHEN** the scheduler configuration is constructed on the database-backed arm
- **THEN** it raises the structured configuration error on CPython 3.11 and 3.12 and on 3.13 and later alike
- **AND** the loop is never adopted as the field's value

#### Scenario: The guard reaches the same verdict on both interpreter arms when called directly

- **GIVEN** the same in-workspace final-segment loop
- **WHEN** the safe-directory guard is called directly on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** both interpreters raise the same structured refusal
- **AND** neither interpreter reaches the containment recheck or the directory check with an adopted loop value

#### Scenario: A dangling final-segment symlink pointing inside the workspace is still accepted

- **GIVEN** a final-segment symlink whose target does not exist and lies inside the workspace root
- **WHEN** the guard runs
- **THEN** it returns without raising, exactly as before this change

#### Scenario: A final-segment target denied by an ancestor is refused identically

- **GIVEN** a final-segment symlink whose resolved target lies under a parent that denies traversal
- **WHEN** the guard classifies the resolved target on any supported CPython
- **THEN** it raises the guard's structured `ValueError` safety refusal
- **AND** it neither leaks a raw `PermissionError` nor accepts the target as absent
- **AND** the refusal is not misattributed to a symlink loop

#### Scenario: A target path reached through a regular file keeps its compatibility verdict

- **GIVEN** a final-segment symlink whose target path is reached through a regular file
- **WHEN** the guard runs
- **THEN** it preserves the pre-change non-loop fallback verdict rather than treating the strict-resolution `ENOTDIR` as a symlink loop

#### Scenario: A dangling final-segment symlink pointing outside the workspace is still refused

- **GIVEN** a final-segment symlink whose target does not exist and lies outside the workspace root
- **WHEN** the guard runs
- **THEN** it keeps today's containment refusal naming the workspace root

#### Scenario: The four pre-existing final-segment verdicts are unchanged

- **GIVEN** in turn a healthy directory symlink, a symlink to a file, a symlink escaping the workspace, and an absent final component
- **WHEN** the guard runs on each
- **THEN** the healthy symlink is accepted, the file target is refused as not a directory, the escaping target is refused as not under the workspace root, and the absent component returns without raising

### Requirement: A workspace-root loop refusal SHALL name the operator's own knob and carry the offending path

The configuration refusal raised when the workspace root itself is a symlink loop SHALL carry the offending path as its sibling refusals in the same guard carry their operands and SHALL let an operator reach the workspace-root knob, because the refusal is currently attributed to a derived evidence-directory field the operator never configured and carries neither a path nor an environment variable name.

The refusal SHALL remain a configuration `ValueError`, the message SHALL be identical on CPython 3.11 and 3.12 and on 3.13 and later, and the lock-root containment refusal SHALL keep its current wording verbatim. The path SHALL be carried as the guard's sibling refusals carry their operands, because that guard receives no evidence-redaction flag and cannot reproduce the preflight lane's redaction treatment. The non-loop geometries that reach the same handler — a workspace root that is a regular file, and one whose mode denies traversal — SHALL keep their existing message verbatim, so the enriched wording is reached only by the loop.

#### Scenario: The workspace-root loop refusal is attributable to the workspace root

- **GIVEN** a workspace root configured as a symlink loop and no evidence-root override set
- **WHEN** the scheduler configuration is constructed
- **THEN** the raised configuration error carries the offending path
- **AND** it lets the operator reach the workspace-root knob rather than pointing only at the derived evidence directory

#### Scenario: The refusal text is identical across interpreter arms

- **GIVEN** the same workspace-root loop
- **WHEN** the configuration is constructed on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** the two messages are identical

#### Scenario: The lock-root refusal is untouched

- **GIVEN** a lock root configured as a symlink loop
- **WHEN** the configuration is constructed
- **THEN** the refusal keeps its existing containment wording verbatim

#### Scenario: The non-loop workspace-root refusals are untouched

- **GIVEN** in turn a workspace root that is a regular file and a workspace root whose mode denies traversal
- **WHEN** the configuration is constructed
- **THEN** each keeps its existing refusal message verbatim

### Requirement: The compatibility-surface path helpers SHALL converge on a normalized result instead of diverging by interpreter

The two compatibility-surface configuration path helpers exposed through the runtime-roots forwarders SHALL apply the module's established strict-then-non-strict real-path paradigm, so that a symlink loop yields the same normalized result on CPython 3.11 and 3.12 as on 3.13 and later instead of raising an errno-less `RuntimeError` on the former and silently adopting the loop on the latter.

Their existing contract SHALL be preserved exactly: a `None` or empty value still returns `None` early, a relative value is still joined onto the supplied base, and a path that does not exist still returns a non-strict normalized result rather than raising.

#### Scenario: A loop yields the same normalized result on both interpreter arms

- **GIVEN** a symlink loop passed to either compatibility-surface helper, as an absolute value and as a relative value
- **WHEN** the helper is called on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** both interpreters return the same normalized result
- **AND** neither raises an errno-less `RuntimeError`

#### Scenario: The early-return and relative-join contracts are unchanged

- **GIVEN** a `None` value, an empty value, and a relative value with a base
- **WHEN** the helpers are called
- **THEN** `None` and empty still return `None`, and the relative value is still joined onto the base

#### Scenario: A nonexistent path still returns a normalized result

- **GIVEN** a path that does not exist
- **WHEN** either helper is called
- **THEN** it returns the non-strict normalized result rather than raising

