## ADDED Requirements

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
