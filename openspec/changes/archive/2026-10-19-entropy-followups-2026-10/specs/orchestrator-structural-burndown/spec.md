## ADDED Requirements

### Requirement: Orchestrator modules SHALL import cleanly as the first import, except an exact allowlist
For every module under `services/orchestrator` discovered from disk, importing it first in a fresh interpreter SHALL succeed. The only exceptions are the modules in the first-import guard's allowlist. The allowlist SHALL be exact: a listed module that imports cleanly fails the guard until it is removed. The scheduler and chain facades SHALL bind the same non-dunder names to the same objects whether they are imported first or after a split module.

#### Scenario: Split module imported first
- **WHEN** a fresh interpreter runs `import services.orchestrator.scheduler_config.path_modes`
- **THEN** the import succeeds, and `services.orchestrator.scheduler.ProductionSchedulerConfig.__module__ == "services.orchestrator.scheduler"`

#### Scenario: A new first-import cycle is caught
- **WHEN** a fixed split module's back-import of its facade is moved above a definition the facade imports from it
- **THEN** the first-import guard fails and names that module

### Requirement: The journal retry helpers SHALL NOT keep an uncalled duplicate of the runtime-root predicate
`services/orchestrator/file_journal_retry_helpers.py` SHALL use `retry._has_runtime_root_field` and SHALL NOT define `_mapping_has_runtime_root_fields`. The journal cluster map SHALL NOT list it.

#### Scenario: Cluster map matches the module
- **WHEN** `tests/test_file_journal_cluster_map.py` runs
- **THEN** it passes with no `retry_helpers` entry for `_mapping_has_runtime_root_fields`
