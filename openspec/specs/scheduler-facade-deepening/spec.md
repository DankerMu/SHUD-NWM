# scheduler-facade-deepening Specification

## Purpose
TBD - created by archiving change governance-8-module-deepening. Update Purpose after archive.

## Requirements

### Requirement: Scheduler facade keeps stable entrypoints
The scheduler facade SHALL keep `ProductionScheduler`,
`ProductionSchedulerConfig`, scheduler CLI/default factory paths, and legacy
`services.orchestrator.scheduler` import and monkeypatch surfaces stable while
implementation families move to owner modules.

#### Scenario: legacy scheduler monkeypatch path is used
- **WHEN** a test or downstream caller monkeypatches an inventoried helper
  through `services.orchestrator.scheduler`
- **THEN** the moved owner module observes the patched behavior or an
  inventory-backed compatibility test fails before merge

#### Scenario: scheduler public entrypoint is constructed
- **WHEN** callers construct `ProductionScheduler` through the existing facade
- **THEN** scheduler state, lease, discovery, candidate construction, evidence,
  execution, and cancellation/status behavior remain equivalent to the
  pre-extraction behavior

### Requirement: Scheduler owner modules own complete behavior families
Scheduler extraction SHALL move implementation by complete owner families:
state, lease, discovery, candidate construction, execution/cohort handling,
evidence write/proof assembly, and cancellation/status proof.

#### Scenario: new scheduler behavior is added
- **WHEN** a PR adds scheduler state, lease, discovery, candidate, execution,
  evidence, or proof behavior
- **THEN** the behavior lands in the matching owner module, or the scheduler
  compatibility inventory is updated with a documented retention reason

#### Scenario: cancellation/status proof remains local glue
- **WHEN** cancellation orchestration code still lives in `scheduler.py`
- **THEN** the inventory identifies it as local glue and tests cover equivalent
  cancellation, status-sync, mutation-proof, and lease-lost behavior

### Requirement: Scheduler extraction is parity tested
Every scheduler extraction PR SHALL include focused tests for the owner module
and compatibility tests for the facade surface it preserves. Each PR SHALL
update `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` in the same PR
when it moves behavior, changes a facade wrapper, or documents retained local
glue.

#### Scenario: scheduler owner family is extracted
- **WHEN** an owner family is moved or narrowed
- **THEN** focused scheduler tests pass for the moved behavior and the
  compatibility facade guard reports no untracked scheduler facade growth

#### Scenario: scheduler inventory is updated with the extraction
- **WHEN** scheduler state, lease, discovery, candidate, execution, evidence, or
  cancellation/status behavior changes ownership or retention classification
- **THEN** the scheduler compatibility inventory records the owner module,
  retained facade surface, removal condition if any, and focused verification
  command in the same PR

### Requirement: Facade patches SHALL reach state and lease owners only through registered seams

Scheduler owner modules that call their own functions directly (`scheduler_state_decision`, `scheduler_state_failure`,
`scheduler_lease`) SHALL observe a monkeypatch on `services.orchestrator.scheduler` only for names registered as facade
seams, through one shared lookup. These three modules hold no facade binding, so no other facade name reaches them;
owner modules that do hold a facade binding keep reading names off it at call time and are not covered here:
- **Registered seams:** `_state_active_jobs`, `_object_manifest_is_missing`, `_default_owner_liveness_probe`,
  `_unlink_lock_file`, `_open_lock_parent_directory`, `_open_regular_guard_file`. A patch on any other facade name is
  not observed inside these owners.
- **Patch detection:** the lookup SHALL decide "patched" by identity against the function recorded where the seam is
  defined, a reference that does not move with a patch on the facade or on any re-exporting module.
- **Precedence:** a patched facade binding wins. While the facade is unpatched or not loaded, the binding of the module
  that holds the call site is used, so a patch on that module is observed.
- **Not observed:** a patch on any other module is not observed and SHALL NOT change which binding the lookup returns.
  That covers a module that merely re-exports the name (the `scheduler_state` aggregate) and the defining module when
  it is not the call-site module (`scheduler_state_rows` for `_state_active_jobs`, `scheduler_state_common` for
  `_object_manifest_is_missing`).
- **Unregistered names:** a lookup for a name that is not a registered seam SHALL fail instead of falling back.
- **Guard:** the facade name-resolution test SHALL collect the literal seam names passed by owner sources, so removing
  a seam's explicit facade import fails before merge, and SHALL reject a call site whose fallback is not the seam's own
  name.

#### Scenario: A facade patch is observed inside the owner

- **WHEN** a test patches a registered seam on `services.orchestrator.scheduler`
- **THEN** the owner calls the patched function, also when the owner module's own binding is patched at the same time

#### Scenario: A call-site module patch is observed while the facade is unpatched

- **WHEN** a test patches a registered seam on the module that holds the call site and leaves the facade untouched
- **THEN** the owner calls the patched function, also when the `scheduler_state` aggregate is patched at the same time

#### Scenario: An aggregate-only patch is not observed

- **WHEN** a test patches a registered state seam only on the `scheduler_state` aggregate, or only on its defining
  module when that is not the call-site module
- **THEN** the owner calls the unpatched function

#### Scenario: A seam whose facade import was removed is caught statically

- **WHEN** the facade no longer exposes a name that an owner passes to the seam lookup
- **THEN** the facade name-resolution test fails

### Requirement: Scheduler owner modules SHALL NOT copy another module's namespace

No module under `services/orchestrator` SHALL copy another module's namespace into its own with a
`globals().update(...)` call. An owner module
that binds its names this way hides what it depends on and takes over the other module's identity attributes.
For `services.orchestrator.scheduler_candidate_runtime`, which used such a copy of the scheduler facade:
- **Identity:** its `__name__`, `__file__` and `__spec__` SHALL be its own, and every function defined in it SHALL
  report it as `__module__`.
- **Explicit imports:** it SHALL import each name it uses at run time from a module that binds the same object the
  facade binds, and SHALL hold no facade binding. Its source SHALL carry no exemption from the undefined-name and
  unused-import lint rules, neither on a `noqa` line nor in the project's per-file ignores.
- **Facade surface:** the names the scheduler facade exposes SHALL NOT change because of the switch.
- **Patches:** a patch on a facade name is not observed inside `scheduler_candidate_runtime`, as it was not under the
  import-time copy.

This requirement does not change how other owner modules read facade names at call time through a facade binding.

#### Scenario: The candidate-runtime owner keeps its own identity

- **WHEN** `services.orchestrator.scheduler_candidate_runtime` is imported after the scheduler facade
- **THEN** its `__name__` is `services.orchestrator.scheduler_candidate_runtime`, its `__file__` is its own source
  file, and every function defined in it reports that module name

#### Scenario: A namespace copy is rejected before merge

- **WHEN** any source file under `services/orchestrator` contains a `globals().update(...)` call
- **THEN** a test fails

#### Scenario: The run-time names are the facade's objects

- **WHEN** the scheduler facade and `scheduler_candidate_runtime` are both imported
- **THEN** each name the module imports for run-time use is the same object as the facade's attribute of that name,
  and every name the facade imports from the module is the module's own attribute
