# Spec Delta

## ADDED Requirements

### Requirement: The baseline pin test SHALL derive expected counts from the tracked baseline
`tests/test_entropy_audit_baseline_writer_summary.py` SHALL read `total_test_files` and `total_instruction_files` from `.entropy-baseline/latest.json` `summary` and compare them to the inventory the writer computes for the same commit semantics, instead of asserting literal integers. Refreshing the baseline with `scripts/governance/write_entropy_baseline.py` SHALL then require no test edit.

#### Scenario: Refresh without test edit
- **WHEN** `uv run python scripts/governance/write_entropy_baseline.py` rewrites `latest.json` and archives the previous snapshot
- **THEN** `uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` passes unchanged and the CI step "Verify entropy baseline was not written" still passes on a PR that does not touch the baseline
