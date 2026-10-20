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
