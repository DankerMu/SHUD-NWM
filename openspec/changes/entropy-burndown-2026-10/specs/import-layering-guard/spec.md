# Spec Delta

## Purpose
`packages/` is the bottom layer: it never imports `workers`, `services`, `apps` or `scripts`, the contract subsets the shared layer needs live inside it, and a test keeps the layering from regressing.

## ADDED Requirements

### Requirement: Contract subsets SHALL live in `packages/common/contracts`
The repository SHALL provide `packages/common/contracts/direct_grid.py` (the current `workers/forcing_producer/direct_grid_contract.py` content), `packages/common/contracts/time_normalization.py` (`ensure_utc`, `parse_cycle_time`, `format_cycle_time` and the other pure helpers currently in `workers/data_adapters/base.py`, plus the helper `workers/shud_runtime/runtime.py` takes from `services/orchestrator/time_consistency.py`), `packages/common/contracts/region.py` (the current `workers/data_adapters/region.py` content) and `packages/common/contracts/hydrologic_fingerprint.py` (`verify_hydrologic_core_fingerprint_equal`, `HYDROLOGIC_CORE_FINGERPRINT_LABELS`, `STATE_COMPATIBILITY_SURFACES`, `MissingPackageFileError` and the private helpers they close over). Modules under `packages/common/contracts/` SHALL import only the standard library, third-party packages and `packages.*`.

#### Scenario: Contract module has no upward import
- **WHEN** the layering guard parses every file under `packages/common/contracts/`
- **THEN** no import resolves to `workers`, `services`, `apps` or `scripts`

### Requirement: Original worker modules SHALL become re-export shims during the expand window
During expand, `workers/forcing_producer/direct_grid_contract.py`, `workers/data_adapters/region.py` SHALL re-export every public name from the contract module; `workers/data_adapters/base.py` SHALL keep the adapter base class and re-export the moved helpers; `workers/mapping_builder/rewrite.py` SHALL import the four fingerprint symbols from the contract module and re-export them. Every existing importer SHALL keep working without edits; the existing test suites of the four workers and of `packages/common` SHALL pass unchanged.

#### Scenario: Importer keeps working through the shim
- **WHEN** a module imports `ensure_utc` from `workers.data_adapters.base`
- **THEN** it receives the same function object as `packages.common.contracts.time_normalization.ensure_utc`

### Requirement: A pytest AST guard SHALL forbid upward imports
`tests/test_import_layering_guard.py` SHALL parse every `.py` under `packages/` and fail on any import whose top-level package is `workers`, `services`, `apps` or `scripts`, and every `.py` under `workers/` importing `services` or `apps`. Entries in `tests/import_layering_allowlist.json` (file path plus imported module) SHALL be the only tolerated exceptions; the guard SHALL fail when an allowlist entry names an import that no longer exists. The test SHALL be selected by `scripts/select_ci_tests.py` whenever a path under `packages/**` or `workers/**` changes.

#### Scenario: New upward import fails the guard
- **WHEN** a file under `packages/` gains `from workers.x import y` not present in the allowlist
- **THEN** the guard fails and names the file and the import

#### Scenario: Stale allowlist entry fails the guard
- **WHEN** the allowlist names a file and import that the file no longer contains
- **THEN** the guard fails and names the stale entry

#### Scenario: Guard runs for package changes
- **WHEN** the changed-path list contains `packages/common/state_manager.py`
- **THEN** `select_tests` includes `tests/test_import_layering_guard.py`

### Requirement: Contract SHALL remove the shims and empty the allowlist
After every repository importer has been migrated to `packages.common.contracts.*`, the shim modules for `direct_grid_contract` and `region` SHALL be deleted, `base.py` and `rewrite.py` SHALL no longer re-export the moved names, `tests/import_layering_allowlist.json` SHALL be an empty list, and no file under the repository SHALL import the moved names from their old module paths.

#### Scenario: No old-path importer remains
- **WHEN** the repository is searched for `workers.forcing_producer.direct_grid_contract`, `workers.data_adapters.region` and for the moved helper names imported from `workers.data_adapters.base` or `workers.mapping_builder.rewrite`
- **THEN** there are no matches outside archived documentation
