# Tasks

## 1. Implementation

- [x] 1.1 `--kind cold_start`: step list per kind at every place the proposal names, receipt gating per kind,
      "reached publish" per kind, dry-run, abort text.
- [x] 1.2 Public gate-input function in the clone tool, used by its pair loop and by the kind check of both
      kinds; a comparison that cannot be made is a step failure.
- [x] 1.3 IC audit in `preflight`, `ic-audit.json`, re-check in `publish`.
- [x] 1.4 `continuity` in plan, step receipts and reports.
- [x] 1.5 Suite `tests/test_node22_model_succession_cold_start.py` covering the proposal's evidence list.
- [x] 1.6 `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`: the new suite on the existing rules
      of the succession package, the clone tool and the audit script (extend rules, add none).
- [x] 1.7 Runbook section 5.7.2; no `file.py:NNN` references; no file over 1000 lines.

## 2. Evidence Floor

- [x] 2.1 Local: `uv run ruff check .`; the new suite; the three existing succession suites; the clone
      tool's suites; the audit's suites; `tests/test_select_ci_tests.py` (after staging);
      `tests/test_node22_entrypoint_invariant.py`, `tests/test_node22_entrypoint_invariant_python_scan.py`,
      `tests/test_entropy_audit_line_references.py`; `openspec validate model-succession-cold-start --strict
      --no-interactive`.
- [x] 2.1b `grep -n '5.7.2' docs/runbooks/production-ops/recalibration-and-archive.md` shows the new section.
- [x] 2.2 CI green on the PR.
- [x] 2.3 node-22 after merge and pull: one `--kind cold_start` dry-run with the exact interpreter and a
      throwaway `--receipt-root`. No provision apply receipt exists in production, so the expected result is
      the refusal naming the missing `provision-apply.json`, with the timer and every file untouched.

Deviation: node-27 is not involved (the tool is DB-free and runs on node-22 only); no node-27 real-DB receipt
applies to this change.

Status 2026-10-06: 2.3 ran on node-22 and ended in the expected refusal
(evidence/2026-10-06-node22-cold-start-dry-run.txt). No cold-start step has run against production.
