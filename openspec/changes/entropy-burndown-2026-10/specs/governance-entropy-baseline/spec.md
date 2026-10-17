# Spec Delta

## ADDED Requirements

### Requirement: The baseline pin test SHALL NOT pin tracked counts as literals
`tests/test_entropy_audit_baseline_writer_summary.py` SHALL drop the literal assertions on `total_test_files` and `total_instruction_files` read from the tracked `.entropy-baseline/latest.json`, SHALL assert only that those tracked values are positive integers consistent with the tracked file's own `modules` aggregation, and SHALL keep its existing comparison of a freshly computed baseline against the HEAD inventory (`v1_summary_* == summary`). Refreshing the baseline with `scripts/governance/write_entropy_baseline.py` SHALL then require no test edit, and adding or removing a test or instruction file afterwards SHALL not fail the test.

#### Scenario: Refresh without test edit
- **WHEN** `uv run python scripts/governance/write_entropy_baseline.py` rewrites `latest.json` and archives the previous snapshot
- **THEN** `uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` passes unchanged and the CI step "Verify entropy baseline was not written" still passes on a PR that does not touch the baseline

#### Scenario: New test file after refresh
- **WHEN** a new `tests/test_*.py` file is added after the refresh
- **THEN** the pin test still passes
