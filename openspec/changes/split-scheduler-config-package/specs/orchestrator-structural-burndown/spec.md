## ADDED Requirements

### Requirement: Scheduler configuration package split preserves all contracts

The production scheduler configuration owner SHALL be split into responsibility-
focused Python modules that are each below the repository's 1,000-line limit.
The historical owner-package and scheduler-facade imports, constructor and
frozen-dataclass contract, module helper attributes used by compatibility tests,
normalization behavior, exceptions, and DB-free evidence/blockers SHALL remain
unchanged. The deleted monolithic path SHALL no longer be excluded from the
large-file guard, and no replacement module SHALL receive an exclusion.

#### Scenario: existing callers import and construct scheduler configuration

- **WHEN** callers import `ProductionSchedulerConfig` through either
  `services.orchestrator.scheduler_config` or `services.orchestrator.scheduler`
  and construct it from direct values or environment defaults
- **THEN** the class signature, dataclass fields/defaults, normalized attributes,
  methods, exceptions, evidence, and downstream behavior are equivalent to the
  pre-split owner
- **AND** every package module is below 1,000 lines and the old exact guard
  exclusion is absent.

#### Scenario: compatibility and safety oracles inspect the package

- **WHEN** tests access historical private scheduler-config attributes or inspect
  which scheduler-config functions call `Path.resolve`
- **THEN** the package barrel exposes the same attribute surface and
  `_functions_calling_resolve` scans one module file or every `.py` beneath the
  owner package rather than only `__init__.py`
- **AND** `test_db_free_normalization_modules_call_resolve_only_where_allowlisted`
  reports scheduler-config callers exactly `{_safe_preserve_final_component}`
  and retry callers exactly `set()`
- **AND** temporarily adding an unallowlisted `.resolve()` to non-barrel
  `path_modes.py` or `db_free.py` makes that test fail before the mutation is
  reverted.
