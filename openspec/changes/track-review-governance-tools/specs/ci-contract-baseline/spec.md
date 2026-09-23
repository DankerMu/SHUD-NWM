## ADDED Requirements

### Requirement: review-governance tools and records SHALL select their suites

`scripts/select_ci_tests.py` SHALL carry path-exact rules, with neither `stop_on_match` nor `only_when_any_changed`:

- `scripts/governance/loop_log_audit.py` and `docs/review-loop-log.jsonl` → `tests/test_loop_log_audit_attribution.py`;
- `scripts/review_gate.py` → `tests/test_review_gate_cli.py` and `tests/test_review_gate_issue_memory.py`.

The CI `backend` paths filter SHALL list `docs/review-loop-log.jsonl` as an exact literal, so a log-only diff opens the targeted gate.

#### Scenario: a loop-log diff selects the attribution suite

- **WHEN** the changed paths are exactly `docs/review-loop-log.jsonl`
- **THEN** the CI `backend` filter matches, and the selection contains `tests/test_loop_log_audit_attribution.py`

#### Scenario: a review-gate CLI diff selects both memory suites

- **WHEN** the changed paths are exactly `scripts/review_gate.py`
- **THEN** the selection contains `tests/test_review_gate_cli.py` and `tests/test_review_gate_issue_memory.py`
