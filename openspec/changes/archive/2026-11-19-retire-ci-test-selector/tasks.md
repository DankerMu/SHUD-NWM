# Tasks

The removal map with file:line for every item is `removal-map.md` in this change; section letters below refer to it. Its line numbers are for master 2038087ed.

## 1. Workflow

- [x] 1.1 `ci.yml` `changes` job: add output `pytest` (D1, D2) from a step with its own `id`, fed `all_files` through `env:`; malformed or empty-string input fails the step; delete the `backend` filter and its output; keep `all_files`.
- [x] 1.2 `ci.yml` `unit-test`: gate `workflow_dispatch || needs.changes.outputs.pytest == 'true'`; matrix `shard: [1..6]`; reword the stale comments (concurrency block, the docs-only sentence near the top, the single-authority comment above the filter, the 4-shard / 621 s arithmetic, real-db job, `-q` note) and the "backend-lane" comment in `governance.yml`.
- [x] 1.3 `ci.yml`: delete `unit-test-targeted`.
- [x] 1.4 `full-regression-watch.yml` header comment and `scripts/ci/full_regression_watch.py` / `scripts/ci/shard_tests.py` docstrings: PRs also run the job; no logic change.

## 2. Deletions

- [x] 2.1 Delete the four selector scripts, `scripts/ci_selector_snapshot.sh`, `tests/test_select_ci_tests.py`, and the four `tests/fixtures/*partition*` JSON files (map B.1, B.2).
- [x] 2.2 `tests/conftest.py`: delete the `pytest_unconfigure` hook that only cleared the selector suite's cache, and the then-unused import.

## 3. References

- [x] 3.1 Tests that pin ci.yml: `tests/test_ci_shard_tests.py` (matrix, gate string, locked-install count), `tests/test_ci_workflow_locked_install.py`, `tests/test_floating_resolve_report.py`, `tests/test_full_regression_watch.py`, `tests/test_governance_workflow_hard_gate.py` (map C.5). Assertions keep their strength; only the expected values change. Add a test for the `pytest` output rule (frontend-only -> false; docs-only, openspec-only, mixed -> true; `[]` -> false; malformed JSON and empty string -> step fails) that extracts the whole step's `run:` script from ci.yml and executes it, asserts the job output `pytest` refers to a step id that exists, and asserts `unit-test`'s `if:` reads that output. The test fails (not skips) when `jq` is missing.
- [x] 3.2 Config and data: `pyproject.toml` per-file ignore, `.large-file-guard.json` entries, `scripts/governance/entropy_audit/line_reference_baseline.json` block, `scripts/ci/full_test_durations.json` entry (and its one stale entry) (map B.4).
- [x] 3.3 Comment/docstring references in tests, scripts, `db/roles/node27_write_roles.sql`, `infra/env/README.md` (map B.4).
- [x] 3.4 Scoped instructions and docs: `scripts/AGENTS.md`, `packages/common/AGENTS.md`, `workers/grid_registry/AGENTS.md`, `workers/model_registry/AGENTS.md`, `instructions/agents/shared.md` (CI 门控要点: PRs run the full sharded suite; only `apps/frontend/**`-only diffs skip pytest; drop "PR 绿 ≠ 全量"), `docs/runbooks/ci-test-routing.md` (shard count, PRs run it), `docs/runbooks/production-ops/stuck-detection.md`, `docs/runbooks/tier-node27-timeseries-storage.md`, `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` (map B.5).
- [x] 3.5 Regenerate `CLAUDE.md` / `AGENTS.md` (orchestrator).

## 4. Specs and archive (orchestrator)

- [x] 4.1 Spec deltas for the 15 capabilities (map A); `openspec validate retire-ci-test-selector --strict --no-interactive`.
- [x] 4.2 Archive in the final push; delete `openspec/specs/ci-test-selector-routing/`; edit the Purpose of `ci-contract-baseline`, `replay-lineage-test-partitioning`, `gateway-reconcile-test-partitioning` and `orchestrator-structural-burndown`; `openspec validate --all --strict --no-interactive`.

## Evidence Floor

- [x] E1 (run after 4.2, when this change directory is archived) `git grep -n -e select_ci_tests -e ci_selector_snapshot -e PathTestRule -e unit-test-targeted -e SUPPORT_MODULE_TEST_RULES -e CHANGED_TEST_FILE_RULES -e GUARDED_MODULE_CLOSURES -e CORE_SMOKE -e 'targeted Unit Tests' -e 'targeted lane' -e 'targeted selection' -e 'focused unit-test' -e 'collect-only` smoke' -e '定向测试' -e 'PR 绿' -- . ':!openspec/changes/archive' ':!docs/runbooks/receipts' ':!docs/review-loop-log.jsonl' ':!docs/adr/0003-review-lens-rotation-keep.md' ':!docs/governance/entropy-audit-2026-10-16*'` returns nothing. The pathspec is D7's exclusion list; the issue body's narrower pathspec is superseded and the PR body says so.
- [x] E2 `uv run pytest -q tests/test_ci_shard_tests.py tests/test_ci_workflow_locked_install.py tests/test_floating_resolve_report.py tests/test_full_regression_watch.py tests/test_governance_workflow_hard_gate.py tests/test_conftest_grib_preflight.py tests/test_node22_entrypoint_invariant.py tests/test_python_environment_truth.py tests/test_role_boundary_static.py` and every `tests/test_entropy_audit_*.py` pass.
- [x] E3 `uv run pytest tests/ -q --collect-only` succeeds (no import of a deleted module anywhere).
- [x] E4 `uv run ruff check . --no-cache`; `actionlint` on `.github/workflows/*.yml`; markdownlint on the changed markdown under `docs/` and the instruction files.
- [x] E5 Sharder: `uv run python scripts/ci/shard_tests.py --shard i --total 6` for i in 1..6 is disjoint and complete over the collected test files.
- [x] E6 On the PR: `Unit Tests (full) (1..6)` all green on the pull_request run; a `workflow_dispatch` run on the branch is green on every shard; no job named `Unit Tests` remains.
- [x] E7 Gate proof from the new test and from run history: a frontend-only diff yields `pytest=false`, a docs-only diff `pytest=true`.
- [x] E8 `CLAUDE.md` / `AGENTS.md` equal header + sources; instruction-guard reports nothing over the limits.
- [ ] E9 (post-merge observation, pending when this change was archived) After merge: PR wall-time median and master red rate over two weeks recorded on #3219 (follow-up observation, not a merge gate).
- [ ] E10 (post-merge observation, pending when this change was archived) After merge: the first master push run executes `Unit Tests (full) (1..6)` (not skipped) and the watcher reports success with 6 shards; recorded on #3219.
