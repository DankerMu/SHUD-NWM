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
and the log root. Every other root field aborts earlier in the deliberate
re-raise, which is an existing design ruling and is out of scope; a third bare
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
