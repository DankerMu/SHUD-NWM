# Tasks

## 1. Implementation

- [x] 1.1 `--kind add_basin`, `--add`, argument rules by kind, `Plan.adds`, plan record.
- [x] 1.2 `preflight` / `publish` / `preview` for `add_basin` (audit, `Operations(add=...)`), continuity,
      dry-run and abort texts; every kind dispatch point the proposal's item 3 names.
- [x] 1.3 Helper parameters (source, two-source workspace, `--add` argv) and the suite
      `tests/test_node22_model_succession_add_basin.py` for the proposal's evidence list.
- [x] 1.4 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`: the new suite on the existing
      rules (extend, add none).
- [x] 1.5 Runbook 5.7.3; pointer in `service-bringup.md` without a new line.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; the new suite; the four existing succession suites; the publish
      tool's suites; `tests/test_select_ci_tests.py` (after staging); the two entrypoint invariant suites;
      `tests/test_entropy_audit_line_references.py`; `openspec validate model-succession-add-basin --strict
      --no-interactive`; `grep -n '5.7.3' docs/runbooks/production-ops/recalibration-and-archive.md`.
- [x] 2.2 CI green on the PR.
- [x] 2.3 node-22 after merge and pull: one `--kind add_basin` dry-run with the exact interpreter and a
      throwaway `--receipt-root`. Expected: the refusal naming the missing `provision-apply.json`, with the
      timer and every file untouched.

Deviation: node-27 is not involved (the tool is DB-free and runs on node-22 only); no node-27 real-DB receipt
applies to this change.
