# Spec Delta

## Purpose
`packages/` is the bottom layer: it never imports `workers`, `services`, `apps` or `scripts`, the contract subsets the shared layer needs live inside it, and a test keeps the layering from regressing.

## ADDED Requirements

### Requirement: Contract subsets SHALL live in `packages/common/contracts`
The repository SHALL provide `packages/common/contracts/direct_grid.py` (the current `workers/forcing_producer/direct_grid_contract.py` content), `packages/common/contracts/time_normalization.py` (`ensure_utc`, `parse_cycle_time`, `parse_cycle_date`, `format_cycle_time`, `cycle_id_for`, `valid_time_for`, `parse_resolution_segments`, `generate_segmented_forecast_hours`, `validate_forecast_hours` from `workers/data_adapters/base.py`, plus `check_three_way_time_consistency` from `services/orchestrator/time_consistency.py`), `packages/common/contracts/region.py` (the current `workers/data_adapters/region.py` content) and `packages/common/contracts/hydrologic_fingerprint.py` (the complete transitive closure of `verify_hydrologic_core_fingerprint_equal` inside `rewrite.py`, computed by AST in the PR: today `SpAttRewriteError`, `MissingPackageFileError`, `HydrologicCoreFingerprintMismatchError`, `UnknownCategoryError`, `MissingCategoryError`, `HydrologicCoreFingerprint`, `compute_hydrologic_core_fingerprint`, `_validate_category_files`, `NON_SP_ATT_CATEGORIES`, `HYDROLOGIC_CORE_FINGERPRINT_LABELS`, `STATE_COMPATIBILITY_SURFACES` and any further private helpers the closure reaches; `rewrite.py` re-exports every moved public name, imports moved private helpers it still calls under their original names so existing `monkeypatch.setattr(rewrite, "_sha256_file", …)` seams keep intercepting the calls that stay in `rewrite.py`, and its remaining exception classes inherit from the contract base). Modules under `packages/common/contracts/` SHALL import only the standard library, third-party packages and `packages.*`.

#### Scenario: Contract module has no upward import
- **WHEN** the layering guard parses every file under `packages/common/contracts/`
- **THEN** no import resolves to `workers`, `services`, `apps` or `scripts`

### Requirement: Original modules SHALL become re-export shims during the expand window
During expand, `workers/forcing_producer/direct_grid_contract.py` and `workers/data_adapters/region.py` SHALL re-export every public name from the contract module; `workers/data_adapters/base.py` SHALL keep the adapter base class and dataclasses and re-export the moved helpers; `workers/mapping_builder/rewrite.py` SHALL import the moved exception classes and fingerprint symbols from the contract module and re-export them; `services/orchestrator/time_consistency.py` SHALL re-export `check_three_way_time_consistency`. Every existing importer SHALL keep working without edits, including module-attribute access (`import workers.data_adapters.base as m; getattr(m, name)`), and the existing test suites of the four workers, of `packages/common` and of `services/orchestrator` SHALL pass unchanged.

#### Scenario: Importer keeps working through the shim
- **WHEN** a module imports `ensure_utc` from `workers.data_adapters.base`
- **THEN** it receives the same function object as `packages.common.contracts.time_normalization.ensure_utc`

### Requirement: A pytest AST guard SHALL forbid upward imports
`tests/test_import_layering_guard.py` SHALL parse every `.py` under `packages/` (scan roots are the repository's `apps packages services workers scripts tests` directories only, never `.venv`, worktrees or `openspec/`) and fail on any import whose top-level package is `workers`, `services`, `apps` or `scripts`, and every `.py` under `workers/` importing `services` or `apps`. `tests/import_layering_allowlist.json` is a list of `{"file": <repo-relative path>, "module": <full dotted module>}` entries, deduplicated per pair, and is the only tolerated exception; the guard SHALL fail when an entry's import is absent from that file's AST (stale entry). The initial allowlist lists the current upward imports; after the expand step it SHALL be `[]` (shim re-exports are downward imports and never need an entry). The test SHALL be selected by `scripts/select_ci_tests.py` whenever a path under `packages/**` or `workers/**` changes, implemented as an additive loop after the stop-rule loop in the shape of the #1656/#2185 supplemental routings (no `matched`, no stop-rule participation), so a `stop_on_match` owner of the changed path cannot shadow it; the exact-set assertions in `tests/test_select_ci_tests.py` that the new rider changes are updated in the same PR, as those precedents did.

#### Scenario: New upward import fails the guard
- **WHEN** a file under `packages/` gains `from workers.x import y` not present in the allowlist
- **THEN** the guard fails and names the file and the import

#### Scenario: Stale allowlist entry fails the guard
- **WHEN** the allowlist names a file and module that the file no longer imports
- **THEN** the guard fails and names the stale entry

#### Scenario: Downward import needs no entry
- **WHEN** `workers/data_adapters/gfs_adapter.py` imports `packages.common.contracts.region`
- **THEN** the guard reports nothing

#### Scenario: Guard runs for package changes
- **WHEN** the changed-path list contains `packages/common/state_manager.py`
- **THEN** `select_tests` includes `tests/test_import_layering_guard.py`

#### Scenario: Guard runs even under a stop-rule owner
- **WHEN** the changed-path list contains only `packages/common/contracts/direct_grid.py` (owned by a `stop_on_match` rule)
- **THEN** `select_tests` still includes `tests/test_import_layering_guard.py`

### Requirement: Contract SHALL remove the shims and empty the allowlist
After every repository importer has been migrated to `packages.common.contracts.*`, the shim modules for `direct_grid_contract`, `region` and `time_consistency` SHALL be deleted, `base.py` and `rewrite.py` SHALL no longer re-export the moved names (their `__all__` lists none; they import only the moved names their remaining code calls, so the rewrite private-helper seam stays), `services/orchestrator/chain_compat_runtime.py` SHALL take its cycle-alias owner module from the contract module, and no file under the repository SHALL import or attribute-access the moved names through their old module paths. The identity assertions in `tests/test_orchestration_chain.py` that compare against the old owner module SHALL be repointed to the contract module, and `CHAIN_COMPATIBILITY_INVENTORY.md` and `SCHEDULER_COMPATIBILITY_INVENTORY.md` SHALL name the contract modules as owners (Guard Hook Seed coverage for the repointed facade imports), with the test's expected-string tuple for the inventory updated alongside, in the same PR (the allowed test edits of the contract step). The contract check is a second test function added to `tests/test_import_layering_guard.py` by the contract step. `import services.orchestrator.chain` SHALL succeed.

#### Scenario: No old-path importer remains
- **WHEN** the guard's contract check scans the repository AST for `from`/`import` statements and `import … as` module aliases (relative imports resolved to absolute names, and `importlib.import_module` string constants included) naming `workers.forcing_producer.direct_grid_contract`, `workers.data_adapters.region` or `services.orchestrator.time_consistency`, and for the moved names imported or attribute-accessed from `workers.data_adapters.base` or `workers.mapping_builder.rewrite`
- **THEN** there are no matches under `apps packages services workers scripts tests`, and the three deleted shim files do not exist
