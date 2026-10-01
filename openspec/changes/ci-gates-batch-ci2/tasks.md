# Tasks — ci-gates-batch-ci2

Fixture level: expanded. Risk packs: ci-gate, dependency-environment, governance. The detailed constraints are in design.md, under "Must preserve".

## 1. #2573
- [x] 1.1 Lock install in the 3 ci.yml jobs, with PATH set via `$GITHUB_PATH` only. Update the stale comments: ci.yml:379-380, tests/test_select_ci_tests.py:10474, pyproject.toml:14, docs/runbooks/ci-test-routing.md:6.
- [x] 1.2 Add `tests/test_ci_workflow_locked_install.py` and route it. Update the exact ci.yml selection pin.
- [x] 1.3 Add `tests/test_native_proj_isolation.py`, routed for pyproject.toml, uv.lock and ci.yml, with pins.
- [ ] 1.4 Orchestrator, on node-27: the isolation test is red in the floating venv and green in the lock venv. The 59-file selection exits 0 three times in a lock venv (parity check).
- [ ] 1.5 Orchestrator: a pre-merge `workflow_dispatch` CI run on the branch head exercises the lock install in Unit Tests (full) and SQL Migration Dry Run.

## 2. #2044
- [x] 2.1 Add `scripts/ci/full_regression_watch.py`, and `tests/test_full_regression_watch.py` with unit tests and the wiring meta-test.
- [x] 2.2 Add `.github/workflows/full-regression-watch.yml`. Add backend-filter exact literals and selector routing.
- [ ] 2.3 Live script runs: 35759146799 exits 1 with wall-timeout; a success exits 0; a skipped-full run exits 0; `margin` P95 is recorded.
- [ ] 2.4 Post-merge: record the first `workflow_run` fire on master (archive PR).

## 3. #2602
- [x] 3.1 Add the governance hard-gate job. The report-only job stays byte-identical. Add the meta-test.
- [x] 3.2 Update the authority text: entropy-budget.md, entropy-report.example.md, and instructions/agents/shared.md, then regenerate CLAUDE.md/AGENTS.md.
- [ ] 3.3 AC1: a stacked draft PR containing only the plant shows the hard gate red and Unit Tests skipped. Record the URLs, then close the PR.
- [ ] 3.4 AC2: record the hard-gate job on the archive PR's master push.

## 4. #2648
- [x] 4.1 Fix the 20 references in openapi_restored_schemas.py, plus the scheduler_state_failure.py and recalibration-and-archive.md §5.7.1 groups. Grep readback.
- [x] 4.2 Add the `hard-line-reference` check, its baseline, the 16th partition test, and the full registration list.

## 5. Verification
- [x] 5.1 `uv run ruff check .`. Run `uv run pytest -q` on the touched suites, `tests/test_select_ci_tests.py` and all `tests/test_entropy_audit_*`. The hard-gate CLI exits 0 on the final head.
- [x] 5.2 `openspec validate ci-gates-batch-ci2 --strict --no-interactive`.
- [ ] 5.3 File a follow-up issue for the scheduled floating-resolve job.

## Evidence Floor
- #2573: red and green of the isolation test across the two venvs; the 59-file selection exits 0 three times in the lock venv; the dispatch run URL shows the lock install in full and real-db; the PR's own CI runs on the lock install.
- #2044: the live script runs; the wiring meta-test; the post-merge fire.
- #2602: the stacked docs-only draft PR red URL; the archive master-push hard-gate URL.
- #2648: tests pass; the baseline total is at or below the measured cap; the grep readback.
