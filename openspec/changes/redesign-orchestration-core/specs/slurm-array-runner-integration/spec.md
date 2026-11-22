## MODIFIED Requirements

### Requirement: Configuration construction survives symlink loops in containment bases and parent segments

Scheduler configuration construction SHALL apply the established
strict-realpath-then-non-strict paradigm to the containment-base confinement
helper, to both preserve-final parent-segment helpers, and to the
safe-directory final-component guard's parent-segment resolution. The scheduler
SHALL build its configuration only through the DB-free construction (there is no
database-backed configuration arm; `ProductionSchedulerConfig` pins
`scheduler_db_free_required` to `true`, `scheduler_config/config.py`). That
construction SHALL hand a path it cannot confine or canonicalise down to the
scheduler root preflight (`scheduler_config/path_modes.py`
`_confined_path_for_mode`, `_require_under_workspace_for_mode`,
`_require_safe_directory_final_component_for_mode`) instead of raising, so that
a symlink loop in WORKSPACE_ROOT's or NHMS_SCHEDULER_LOCK_ROOT's final segment,
or in any configured path's parent segment, never aborts construction with an
errno-less RuntimeError or a construction-time ValueError, and the root
preflight refuses it before the scheduler lock is acquired with the same
field-named blocker codes on CPython 3.11/3.12 as on 3.13+. The confinement
helper `_confined_path` itself keeps its structured containment refusal as a
helper-level contract. The safe-directory guard's own final-segment resolve (a
loop as the evidence directory's final segment inside the workspace) remains
version-divergent and is tracked separately as issue #1544 — it is outside this
requirement.

#### Scenario: WORKSPACE_ROOT final-segment loop converges to the structured safe-directory refusal

- **WHEN** WORKSPACE_ROOT is a symlink loop's final segment
- **THEN** configuration construction does not raise, identically on CPython
  3.11/3.12 and 3.13+
- **AND** the scheduler root preflight refuses before the lock with the
  field-named blockers `SCHEDULER_ROOT_WORKSPACE_ROOT_SYMLINK` (`workspace_root`),
  `SCHEDULER_ROOT_LOCK_ROOT_UNSAFE_PATH` (`lock_root`) and
  `SCHEDULER_ROOT_EVIDENCE_ROOT_UNSAFE_PATH` (`evidence_root`), and the
  evidence directory the preflight judged is the one derived from the looping
  workspace root (`tests/test_production_scheduler.py`
  `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal`)
- **AND** when the loop is the evidence root's final segment inside the
  workspace, the same construction route refuses it before the lock as
  `SCHEDULER_ROOT_EVIDENCE_ROOT_SYMLINK` (`evidence_root`)
  (`test_final_segment_loop_refuses_on_the_construction_route`).

#### Scenario: NHMS_SCHEDULER_LOCK_ROOT final-segment loop converges to the structured containment refusal

- **WHEN** NHMS_SCHEDULER_LOCK_ROOT is a symlink loop's final segment
- **THEN** configuration construction does not raise, identically on CPython
  3.11/3.12 and 3.13+
- **AND** the scheduler root preflight refuses before the lock with the
  field-named containment blocker `SCHEDULER_ROOT_LOCK_ROOT_OUT_OF_WORKSPACE`
  (`lock_root`), followed by `SCHEDULER_ROOT_EVIDENCE_ROOT_NOT_FOUND`
  (`evidence_root`)
  (`test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal`)
- **AND** the containment-base confinement helper `_confined_path`, called
  directly, still canonicalises a loop in the base's parent chain and keeps a
  final-segment loop verbatim
  (`test_resolve_residue_confined_path_canonicalises_parent_segment_loop`,
  `test_resolve_residue_confined_path_keeps_final_segment_loop_verbatim`).

#### Scenario: parent-segment loop no longer aborts construction on 3.11/3.12

- **WHEN** any env-driven root's parent segment contains a symlink loop and the
  root is canonicalised through the resolve arm (`_resolve_config_path_for_mode`,
  `scheduler_config/path_modes.py`)
- **THEN** configuration construction on CPython 3.11/3.12 produces the same
  canonical form and the same subsequent verdict as 3.13+ instead of raising an
  errno-less RuntimeError, and the root's preflight path carries the same
  canonical form so the storage preflight owns the verdict
  (`test_resolve_residue_config_parent_segment_loop_object_store_root_constructs`)
- **AND** the preserve-final arm (`_safe_preserve_final_component`) is outside
  this requirement: on 3.11/3.12 it still swallows the loop and returns the raw
  path. That sub-family has since been adjudicated by
  `docs/adr/0009-path-canonicalization-dereference-doctrine.md`, which admits
  this arm under clause 1: the products of its call sites reach a kernel
  dereference — the preflight `lstat` — before any verdict derived from them is
  committed. The product does differ between CPython 3.12 and 3.13+ where the
  loop sits behind a symlinked parent, but that difference is neutral at the
  dereference: the condition that makes the two spellings differ is precisely a
  loop surviving in the parent chain, so `lstat` reports `ELOOP` for both. The
  neutrality is backed by `tests/test_preserve_final_component_loop_spelling.py`,
  which builds both spellings and asserts `ELOOP` for each on the pinned
  interpreter; the 3.13 spelling it builds is synthesized from the folding rule
  rather than produced by a 3.13 runtime, so the 3.13 half of this claim rests
  on that construction and is tracked with the rest of the cross-interpreter
  question by issue #2453. The neutrality claim SHALL NOT be widened beyond a
  parent-chain loop: for a `<missing>/../<loop>` input the two spellings do not
  agree at the dereference — the raw spelling faults `ENOENT` and the folded one
  `ELOOP` — and the same test pins that boundary so the wording cannot drift
  outward. A cross-interpreter difference in the refusal's blocker code and in
  the emitted evidence payload does exist on this preflight leg; a
  counterfactual probe attributes it to that leg's own `Path.resolve(strict=False)`
  rather than to this arm, and it is tracked by issue #2453.

#### Scenario: ENOENT and non-loop containment semantics are unchanged

- **WHEN** a configured path merely does not exist yet, or a non-loop path
  violates workspace containment
- **THEN** configuration construction performs no existence validation and the
  canonicalised value is the previous non-strict resolution product
  (`test_resolve_residue_helpers_keep_missing_path_semantics`,
  `test_resolve_residue_config_keeps_missing_lock_root_semantics`)
- **AND** a non-loop lock or evidence path outside the workspace constructs and
  is refused by the root preflight before the lock as
  `SCHEDULER_ROOT_LOCK_ROOT_OUT_OF_WORKSPACE` / `SCHEDULER_ROOT_EVIDENCE_ROOT_OUT_OF_WORKSPACE`
  (`test_explicit_paths_must_stay_under_workspace`)
- **AND** the confinement helper `_confined_path`, called directly, keeps the
  existing "must be under workspace_root" `ValueError` byte-for-byte, whether or
  not the offending parent exists
  (`test_resolve_residue_confined_path_keeps_non_loop_containment_refusal`).

### Requirement: Slurm-first heavy execution

The production scheduler SHALL submit heavy download/canonical/forcing/SHUD/parse/publish work through the Slurm gateway by default when Slurm execution is enabled.

#### Scenario: compute-node database preflight

WHEN Slurm execution is enabled and `DATABASE_URL` is missing, points to localhost, or points to a remote host
THEN the Slurm preflight does not require a database endpoint: it records `checks.database` with `required: false`, `db_free_runtime: true` and `compute_node_reachable: "not_required"`, and emits no `SLURM_PREFLIGHT_DATABASE_URL_*` blocker for any of the three (the scheduler is always DB-free; `ProductionSchedulerConfig` pins `scheduler_db_free_required` to `true`)
AND a configured `DATABASE_URL`, local or remote, is refused by the DB-free runtime preflight as `database_url_forbidden` (`DATABASE_URL`): `run_once()` returns `preflight_blocked` with `execution_boundary` and `lock.reason` both `db_free_runtime_preflight_blocked`, the scheduler lock is not acquired, and no Slurm job is created
AND a missing `DATABASE_URL` is the expected DB-free state and is not refused on that ground (#3311 observed-behaviour rows 1-3, table restated in PR #3325 body).

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

WHEN Slurm execution is enabled and a configured allowed storage root cannot be canonically resolved — its ancestor is a regular file (`ENOTDIR`), an ancestor denies traversal (`EACCES`), it is a symlink loop (`ELOOP`), or it does not exist (`ENOENT`) — as detected by errno from strict resolution
THEN scheduler configuration construction never aborts because of it; the configuration layer hands the value down without adjudicating, admitting it into `allowed_storage_roots` lexically for the three non-`ENOENT` errnos and as the non-strict real path for `ENOENT`
AND the root is admitted as a containment base with no blocker of its own on every plane of the run: the storage preflight emits no `ALLOWED_STORAGE_ROOTS_*` blocker, the scheduler runtime-root preflight emits no `SCHEDULER_ROOT_ALLOWED_ROOTS_*` blocker, and the DB-free lane emits no `db_free_allowed_root_unsafe` blocker; every plane lists the root in its allowed-roots evidence, so for a given filesystem state the evidence planes produced by one run never contradict each other, and the adjudication never escapes as an unhandled exception
AND this admission is adjudicated by `docs/adr/0009-path-canonicalization-dereference-doctrine.md` clause 3: the admitted root serves only as a containment base and carries no assertion that it exists, and a path judged against it is dereferenced before any verdict is committed — a DB-free required path placed under such a root is itself adjudicated by the DB-free runtime preflight's required-path blockers (for example `NHMS_SCHEDULER_JOURNAL_ROOT` under an `ENOTDIR` or `ELOOP` root is refused as `db_free_required_path_unsafe`)
AND the same holds whether or not the pass carries repair authority (`repair_missing_forcing` false and true)
AND an allowed root that remains merely missing after canonicalization (ENOENT) is never treated as unsafe and never escapes configuration construction or the storage preflight as an unhandled exception
AND blocker paths and allowed-roots evidence obey the runtime-root preflight's existing evidence-safe masking discipline (`[local-path]`) (#3311 observed-behaviour rows 4-11, 24-25).

#### Scenario: unresolvable general storage root at configuration construction

WHEN the scheduler configures an object-store, published-artifact, log, runtime, or temp root — a root that is not a containment base for other configured paths — whose final path component cannot be canonically resolved for a reason other than absence (for example a symlink loop)
THEN scheduler configuration construction never aborts: the configuration layer hands down the non-strict canonicalised value without adjudicating, producing the same canonical form and the same subsequent verdict across supported CPython versions
AND when Slurm execution is enabled, the storage preflight records the unsafe-path storage preflight blocker for the roots it consults (`SLURM_PREFLIGHT_OBJECT_STORE_ROOT_UNSAFE_PATH`, `SLURM_PREFLIGHT_LOG_ROOT_UNSAFE_PATH`, `SLURM_PREFLIGHT_RUNTIME_ROOT_UNSAFE_PATH`) and rejects submission before creating Slurm jobs
AND when such a root's final segment is a symlink loop, the scheduler runtime-root preflight refuses the object-store, published-artifact, runtime and temp roots it judges as `SCHEDULER_ROOT_<FIELD>_SYMLINK`; it does not judge the log root
AND a root that is merely missing (ENOENT) keeps the existing construction-time semantics — configuration construction performs no existence validation and the canonicalised value is identical to the previous non-strict resolution product (#3311 observed-behaviour rows 12-16).

#### Scenario: safe template and environment export

WHEN the scheduler submits through the real or mock Slurm gateway
THEN the submitted job uses only an allowlisted sbatch template for the requested stage
AND exported environment/config values are shell-safe, bounded, and redacted from evidence when sensitive
AND secret leakage, shell metacharacter injection, and unrecognized template names are rejected before submission.

### Requirement: The safe-directory guard SHALL resolve a final-segment symlink identically on every supported interpreter

The safe-directory final-component guard SHALL decide a final-segment symlink by strict real-path resolution and SHALL refuse a resolution loop with the module's structured configuration error on every supported CPython, because non-strict resolution raises an errno-less `RuntimeError` on CPython 3.11 and 3.12 and silently adopts the loop as the field's value on 3.13 and later.

Every strict real-path failure other than a loop SHALL first fall back to non-strict real-path resolution, preserving the established resolution product for a target that does not exist, a target whose parent denies traversal, or a target reached through a regular file. The fallback SHALL NOT itself be treated as proof that the target is absent or a directory: the subsequent final-target classification SHALL distinguish absence, directory, non-directory, and metadata denial without relying on the interpreter-dependent exception swallowing of `Path.exists()` or `Path.is_dir()`. `EACCES` or `EPERM` at that classification boundary SHALL fail closed with the guard's structured `ValueError` safety contract, never leak a raw permission exception and never be accepted as a missing target.

The guard is a helper-level contract. Scheduler configuration construction, which is always the DB-free construction, does not let the guard's refusal abort it: it hands the evidence directory down (`scheduler_config/path_modes.py` `_require_safe_directory_final_component_for_mode`) and the scheduler runtime-root preflight refuses the loop before the scheduler lock is acquired.

The loop refusal SHALL be recognised by the loop error number obtained from the `errno` module rather than by a hard-coded integer. The guard's separate metadata-lookup failure handler SHALL keep its existing refusal for every error number other than a loop, so that the non-loop geometries already pinned against that handler stay green verbatim.

#### Scenario: A final-segment loop inside the workspace is refused identically on both interpreter arms

- **GIVEN** an evidence directory whose final segment is a symlink loop located inside the workspace root
- **WHEN** the scheduler configuration is constructed
- **THEN** construction does not raise, and `evidence_dir` keeps the configured spelling of the loop rather than a resolved target
- **AND** the scheduler runtime-root preflight refuses it before the lock as `SCHEDULER_ROOT_EVIDENCE_ROOT_SYMLINK` (`evidence_root`) (`tests/test_production_scheduler.py` `test_final_segment_loop_refuses_on_the_construction_route`; #3311 observed-behaviour row 17)

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

The refusal of a workspace root that is itself a symlink loop SHALL be attributed to the workspace-root knob the operator configured, not only to the evidence directory derived from it. Scheduler configuration construction, which is always the DB-free construction, SHALL NOT raise for this geometry: it hands the workspace root and the lock and evidence roots derived from it down to the scheduler runtime-root preflight, which refuses them before the scheduler lock is acquired with field-named blockers whose first entry names `workspace_root`. The blocker codes SHALL be identical on CPython 3.11 and 3.12 and on 3.13 and later, because the root check decides them from the kernel `lstat` of the configured path (see `runtime-evidence-and-operations` "The scheduler root check SHALL attribute a symlink-loop root identically on every supported interpreter"). Blocker paths obey the preflight's evidence-safe masking discipline. The non-loop geometries — a workspace root that is a regular file, and one whose mode denies traversal — SHALL also construct and SHALL keep a verdict distinct from the loop's.

#### Scenario: The workspace-root loop refusal is attributable to the workspace root

- **GIVEN** a workspace root configured as a symlink loop and no evidence-root override set
- **WHEN** the scheduler configuration is constructed and the scheduler runtime-root preflight runs
- **THEN** construction does not raise
- **AND** the preflight is blocked with `SCHEDULER_ROOT_WORKSPACE_ROOT_SYMLINK` (`workspace_root`), followed by `SCHEDULER_ROOT_LOCK_ROOT_UNSAFE_PATH` (`lock_root`) and `SCHEDULER_ROOT_EVIDENCE_ROOT_UNSAFE_PATH` (`evidence_root`) for the roots derived from it (`tests/test_production_scheduler.py` `test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal`; #3311 observed-behaviour row 18)

#### Scenario: The refusal text is identical across interpreter arms

- **GIVEN** the same workspace-root loop
- **WHEN** the configuration is constructed and the root preflight runs on CPython 3.11 or 3.12 and on 3.13 or later
- **THEN** the two runs carry the same blocker codes and fields

#### Scenario: The lock-root refusal is untouched

- **GIVEN** a lock root configured as a symlink loop
- **WHEN** the configuration is constructed
- **THEN** construction does not raise
- **AND** a loop outside the workspace is refused by the root preflight as `SCHEDULER_ROOT_LOCK_ROOT_OUT_OF_WORKSPACE` (`lock_root`) (`test_resolve_residue_config_final_segment_loop_converges_to_structured_refusal[NHMS_SCHEDULER_LOCK_ROOT-expected_blockers1]`), and a loop inside the workspace as `SCHEDULER_ROOT_LOCK_ROOT_SYMLINK` (`lock_root`); for the latter `run_once()` returns `preflight_blocked` with `execution_boundary` and `lock.reason` both `scheduler_root_preflight_blocked` and the lock is not acquired (#3311 observed-behaviour row 21)
- **AND** the confinement helper `_confined_path`, called directly, keeps its "must be under workspace_root" wording verbatim (`test_resolve_residue_confined_path_keeps_non_loop_containment_refusal`)

#### Scenario: The non-loop workspace-root refusals are untouched

- **GIVEN** in turn a workspace root that is a regular file and a workspace root whose mode denies traversal
- **WHEN** the configuration is constructed and the root preflight runs
- **THEN** construction does not raise
- **AND** the scheduler runtime-root preflight is blocked with `SCHEDULER_ROOT_WORKSPACE_ROOT_OUT_OF_APPROVED_ROOT` (`workspace_root`) for each (#3311 observed-behaviour rows 19-20, table restated in PR #3325 body)

## REMOVED Requirements

### Requirement: Scheduler configuration construction SHALL survive an undeterminable home directory on both database arms

**Reason**: The requirement pinned the database-backed configuration arm to the database-free arm's product. R2b (#3255) removes the database-backed arm: `ProductionSchedulerConfig` is always DB-free, and `scheduler_db_free_required=False` is accepted and ignored. The two-arm equality and the four "database-backed arm" scenario names have no subject left. A MODIFIED delta would have to keep those scenario names, and they would describe an arm that no longer exists.
**Migration**: Restated as the single DB-free construction contract "Scheduler configuration construction SHALL survive an undeterminable home directory" (ADDED below), with the same inputs, the same products and the same preflight verdict.

## ADDED Requirements

### Requirement: Scheduler configuration construction SHALL survive an undeterminable home directory

Scheduler configuration construction, which is always the DB-free construction,
SHALL treat a configured path whose leading `~` component has no determinable
home directory as a value to hand down to the storage preflight, not as a
construction-time abort, because classification of unusable roots belongs to
the preflight and a construction-time abort produces no structured blocker at
all.

This applies to the two configuration fields that bypass the deliberate re-raise
helper and reach the module's own bare expansions — the allowed storage roots
and the log root. Every other root field reaches the construction's mode-aware
expansion (`scheduler_config/path_modes.py` `_expanduser_for_mode`), which also
keeps an unexpandable value instead of raising; the deliberate re-raise that the
database-backed arm applied there was deleted with that arm (#3311), and this
requirement pins only the two fields above. A third bare
expansion in the module is reachable only through the compatibility forwarders
and is closed as a ledger item, not because a production field reaches it. The
construction's product SHALL be pinned by spelled-out values rather than merely
by the absence of an exception, and passing the retired keyword
`scheduler_db_free_required=False` SHALL yield the same DB-free configuration.

#### Scenario: An allowed storage root naming an unknown user constructs successfully

- **GIVEN** an allowed storage root configured as `~<unknown user>/roots`
- **WHEN** the scheduler configuration is constructed, with or without the
  retired keyword `scheduler_db_free_required=False`
- **THEN** construction succeeds instead of aborting with an errno-less
  `RuntimeError`, and the configuration reports `db_free_required` as `true`
- **AND** the value keeps the tilde segment verbatim, anchored at the working
  directory (`tests/test_production_scheduler.py`
  `test_expanduser_residue_config_constructs_on_the_db_free_construction`,
  which replaces the two-arm equality test).

#### Scenario: A log root naming an unknown user constructs successfully

- **GIVEN** a log root configured as `~<unknown user>/logs`
- **WHEN** the scheduler configuration is constructed, with or without the
  retired keyword `scheduler_db_free_required=False`
- **THEN** construction succeeds and the value keeps the tilde segment verbatim,
  anchored at `workspace_root`
  (`test_expanduser_residue_config_constructs_on_the_db_free_construction`).

#### Scenario: The compatibility-surface expansion helper stops throwing bare on the same input

- **GIVEN** the module's non-relative preserve-final-component helper, `_config_path_preserve_final_component`, called directly with `~<unknown user>/workspace`
- **WHEN** it expands the value
- **THEN** it returns the same product as the DB-free construction's
  preserve-final helper (`_config_path_preserve_final_component_for_mode`)
- **AND** it does not raise an errno-less `RuntimeError`
  (`test_expanduser_residue_preserve_final_component_helper_matches_db_free_arm`).

#### Scenario: The storage preflight still classifies the unusable root structurally

- **GIVEN** a configuration constructed from such a value
- **WHEN** the storage preflight runs
- **THEN** it returns its structured blocked result rather than raising, the
  unusable root being admitted by the tolerance arm for a merely missing path
  and classified through the consequential root blockers
  (`test_expanduser_residue_preflight_classifies_structurally`).
