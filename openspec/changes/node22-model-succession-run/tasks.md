# Tasks

## 1. Implementation

- [x] 1.1 `scripts/node22_model_succession.py` + `scripts/model_succession/`: plan, step receipts, resume,
      failure receipt, `--abort`, dry-run report.
- [x] 1.2 Steps `copyback`, `preflight`, `begin`, `clone`, `publish`, `refresh`, `finish`; scheduler-not-running
      check before `clone`, `publish`, `refresh`, `finish`; `systemctl` through an overridable binary.
- [x] 1.3 Tests for every item of the proposal's evidence list (fake `systemctl`, fake refresh unit).
- [x] 1.4 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py` for the new script, modules, suites
      and any tests/ support module; node-22 entrypoint guards, including a guard for the new command's
      runbook lines of the same shape as the publish tool's.
- [x] 1.5 Runbooks: `recalibration-and-archive.md` 5.7.1, pointers in `service-bringup.md` and
      `gateway-and-services.md`; no `file.py:NNN` references; no file over 1000 lines.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; the new suites; the publish-tool suites; the clone tool's suites;
      `tests/test_select_ci_tests.py` (after staging); `tests/test_node22_entrypoint_invariant.py`,
      `tests/test_node22_entrypoint_invariant_python_scan.py`, `tests/test_entropy_audit_line_references.py`.
- [ ] 2.2 CI green on the PR.
- [ ] 2.3 node-22 after merge and pull: one dry-run with the exact interpreter and a throwaway
      `--receipt-root`. No provision apply receipt exists in production yet, so the expected result is the
      refusal naming the missing `provision-apply.json`, with the timer and every file untouched. The
      full-chain dry-run and the first `--apply` happen at the first real succession; their receipts go to
      the issue then.
