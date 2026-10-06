# Tasks

## 1. Implementation

- [x] 1.1 Public predicate in `packages/common/provider_atomic.py`, used by the destination lock.
- [x] 1.2 Output-registry parent check in `provision_direct_grid_registry`, before the dry-run receipt is
      loaded, the receipt target is prepared and the database is opened.
- [x] 1.3 Tests for the proposal's evidence list.
- [x] 1.4 Runbook text; `scripts/select_ci_tests.py` only if a new test file is added.

## 2. Evidence Floor

- [ ] 2.1 Local: `uv run ruff check .`; `tests/test_provision_direct_grid_dry_run_and_receipt.py`; the
      existing `provider_atomic` suites; `tests/test_select_ci_tests.py` (after staging);
      `tests/test_entropy_audit_line_references.py`; `openspec validate
      provision-output-registry-parent-check --strict --no-interactive`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-27 after merge and pull: one read-only dry-run with `--output-registry` under
      `scheduler/direct-grid-candidates/` (group- and other-writable, owned by another user). Expected: the
      refusal before the database is opened, no receipt.

Deviation: node-27 real-DB receipt (`uv run pytest` against a scratch database on node-27) is pending the
repair of the RAID link; command to run then: `uv run pytest -q
tests/test_provision_direct_grid_dry_run_and_receipt.py`. Local results are not a node-27 PASS.
