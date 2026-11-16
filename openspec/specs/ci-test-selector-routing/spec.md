# ci-test-selector-routing Specification

## Purpose
Make sure a PR that changes a single owner still selects the suites that directly assert that owner's behaviour, and keep that mapping under mechanical closure guards so that it cannot silently decay.

## Requirements

### Requirement: Owner diffs select their direct behavioural oracles
For each of the following owners, the CI test selector SHALL select the suites that directly assert its behaviour when only that owner changes:
- `apps/api/routes/pipeline.py` SHALL select `tests/test_retry.py`;
- each `infra/sbatch/*.sbatch` template SHALL select the suites that load the templates from disk;
- `packages/common/station_set_flip.py` and `packages/common/model_registry.py` SHALL select their non-gated direct importer suites;
- `services/orchestrator/{persistence,retry,public_evidence}.py` and `packages/common/{redaction,source_identity}.py` SHALL select both halves of the response-model preservation oracle;
- `packages/common/forecast_store.py` SHALL select `tests/test_river_ts_stats_harness_offline.py`.

#### Scenario: Route-only pipeline diff
- **WHEN** only `apps/api/routes/pipeline.py` changes
- **THEN** the selection includes `tests/test_retry.py`

#### Scenario: Template-only sbatch diff
- **WHEN** only one `infra/sbatch/*.sbatch` template changes
- **THEN** the selection is non-empty and includes `tests/test_object_store_roots.py` and `tests/test_production_slurm_validation.py`

#### Scenario: Preservation-relevant helper diff
- **WHEN** only `packages/common/redaction.py` changes
- **THEN** the selection includes both response-model preservation halves

### Requirement: Guarded owners fail closed on new importers
`apps/api/routes/pipeline.py`, `packages/common/station_set_flip.py` and `packages/common/model_registry.py` SHALL be covered by a mechanical closure guard. A new non-gated suite that imports one of them without a matching rule change SHALL make the selector meta-suite fail.

#### Scenario: New importer without a rule
- **WHEN** a non-gated suite starts importing `packages/common/station_set_flip.py` and the rule is not updated
- **THEN** `tests/test_select_ci_tests.py` fails naming the missing suite

### Requirement: Journal facade and sibling edits SHALL select the test files that patch facade globals
A changed path that is `services/orchestrator/file_orchestration_journal.py` or matches `services/orchestrator/file_journal_*.py` SHALL select every file in `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`, and that set SHALL contain the test files that import the journal facade inside a function body and patch a facade global through it (`tests/test_gateway_reconcile_{file_cohort_comment,identity_release,claimant_exclusivity,inventory,round10,writer_launch,writer_quiescence,writer_receipts,file_submit_barrier}.py` and `tests/test_safe_fs_lock.py`). A test SHALL pin this for every `file_journal_*.py` found on disk, with a non-empty parameter set.

#### Scenario: A sibling edit selects the facade patchers
- **WHEN** `services/orchestrator/file_journal_paths.py` is the only changed path
- **THEN** the selection contains `tests/test_gateway_reconcile_file_cohort_comment.py` and `tests/test_gateway_reconcile_identity_release.py`

#### Scenario: Removing the sibling glob rule turns the pin red
- **WHEN** the `services/orchestrator/file_journal_*.py` rule is removed from the selector rules
- **THEN** every `file_journal_*.py` sibling parameter of the pin fails (the facade parameter keeps its own stop rule)

### Requirement: Compatibility facade guard inputs SHALL select the facade-guard current-repo tests
A change to any compatibility facade guard input SHALL select two test nodes:
- `tests/test_entropy_audit_report_contract.py::test_compatibility_facade_guard_current_repo_passes_with_inventories`
- `tests/test_entropy_audit_baseline_writer_summary.py::test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo`

The inputs are each governed facade path and its inventory path, as listed in `COMPATIBILITY_FACADE_CONFIGS`.

The selection SHALL be additive: it does not stop rule matching, does not count as a backend match, and leaves `meta_guard_only` unchanged. The selector's mirror of the input paths SHALL be pinned equal to `COMPATIBILITY_FACADE_CONFIGS`.

#### Scenario: Facade edit selects the guard nodes
- **WHEN** `services/orchestrator/scheduler.py` is the only changed path
- **THEN** the selection contains both facade-guard current-repo nodes

#### Scenario: Unwired facade config is caught
- **WHEN** `COMPATIBILITY_FACADE_CONFIGS` gains a facade whose paths the selector mirror lacks
- **THEN** the mirror pin fails and names the missing path

### Requirement: Canonical converter diffs SHALL select the converter and adapter suites
A change confined to `workers/canonical_converter/**` SHALL select `tests/test_canonical_converter.py`, `tests/test_ifs_canonical.py`, `tests/test_era5_canonical.py`, `tests/test_e2e_ifs.py`, `tests/test_ifs_adapter.py`, `tests/test_gfs_adapter.py`, `tests/test_era5_adapter.py` and `tests/test_grib_packing_reader.py`, in addition to whatever the same-name derivation selects. Importer suites outside the package SHALL NOT be carried by the rule; each is dispositioned in `INTENTIONAL_RULE_GAP_EXCLUSIONS` with a reason token (`edge-consumer`, or `runtime-budget` with its measurement). `workers/canonical_converter` SHALL be one of the directories audited by the importer-gap guard, so a module-level static importer of the package without a rule or a reasoned exclusion makes the selector meta-suite fail.

#### Scenario: Converter-only diff
- **WHEN** only `workers/canonical_converter/converter.py` changes
- **THEN** the selection includes the eight converter and adapter suites
- **AND** it is not only `CORE_SMOKE_TESTS`

#### Scenario: GRIB packing diff
- **WHEN** only `workers/canonical_converter/grib_packing.py` changes
- **THEN** the selection includes `tests/test_grib_packing.py` and `tests/test_grib_packing_reader.py`

#### Scenario: CLI diff
- **WHEN** only `workers/canonical_converter/cli.py` changes
- **THEN** the selection includes the eight converter and adapter suites
- **AND** it does not include `tests/test_slurm_array_contract.py` or `tests/test_source_identity.py`, which are edge consumers owned by other rules

#### Scenario: Unrouted importer
- **WHEN** a non-gated suite statically imports `workers.canonical_converter` at module level and neither the rule nor `INTENTIONAL_RULE_GAP_EXCLUSIONS` covers it
- **THEN** `tests/test_select_ci_tests.py` fails naming that suite

### Requirement: Selector data tables and logic SHALL be separate modules with identical output
`scripts/select_ci_tests.py` SHALL keep `select_tests`, `main`, `_write_github_output` and the globals the tests patch on the facade (`PATH_TEST_RULES`, `SUPPORT_MODULE_TEST_RULES`, the `*_ROOTS` names and any other facade global a test under `tests/test_select_ci_tests.py` patches, inventoried in the PR that adds the sibling rule rows); `PathTestRule` and the `*_TESTS`/`*_PATH`/`*_ROOTS`/`*_MODULES` constants SHALL be defined in `scripts/select_ci_tests_tables.py`; `CHANGED_TEST_FILE_RULES`, `SUPPORT_MODULE_TEST_RULES` and `PATH_TEST_RULES` SHALL be defined in `scripts/select_ci_tests_rules.py`; the import-graph derivation functions and the `_SUITE_IMPORTER_PARSE_STATS` dict they own SHALL move to `scripts/select_ci_tests_imports.py`. The facade binds the table and rule names by import, and `select_tests` reads them as facade globals, so patches on the facade stay effective. A moved function SHALL NOT read a facade-patched global through its own module binding: it receives the value as a parameter from `select_tests`. Before any move, `PATH_TEST_RULES` SHALL gain a rule row for each sibling and `_collection_smoke_required` SHALL match the siblings as it matches the facade. For the fixed changed-path list recorded in tasks.md, `uv run python scripts/select_ci_tests.py` SHALL produce byte-identical `count`, `tests_json`, `meta_guard_only` and `collection_smoke_required` outputs at the parent commit and at the head of each extraction PR (no committed snapshot; the comparison is run on both commits of the same PR). `tests/test_select_ci_tests.py` SHALL pass with only three kinds of edit to existing tests across the whole decomposition (new scenario tests that the tasks add are not edits): the two source-text pins (node-id scan, `select_tests` exec) and the repoint of every reference to `_SUITE_IMPORTER_PARSE_STATS` (today two tests whose four reference lines reach it through a function-local module alias; the repoint changes that alias's two import lines).

#### Scenario: Routing output unchanged
- **WHEN** the recorded changed-path list is fed to the selector at an extraction PR's parent commit and head
- **THEN** the four GitHub outputs are byte-identical

#### Scenario: Patched facade global still steers a moved function
- **WHEN** a test patches `select_ci_tests.PATH_TEST_RULES` with a single rule
- **THEN** `select_tests` returns the set that rule yields

#### Scenario: Sibling edit selects the meta guard
- **WHEN** `scripts/select_ci_tests_rules.py` is the only changed path
- **THEN** `select_tests` returns the same meta-guard set it returns for `scripts/select_ci_tests.py`
