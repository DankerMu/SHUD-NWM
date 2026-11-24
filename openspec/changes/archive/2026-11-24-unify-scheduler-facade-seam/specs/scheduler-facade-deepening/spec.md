## ADDED Requirements

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
