# Tasks

## Risk packs

- Config / project setup: selected - ci.yml matrix, new workflow -> 1.x, 3.x.
- Public API / CLI / script entry: selected - shard_tests CLI, watcher CLI, report CLI -> 1.1, 2.x, 3.2.
- Concurrency / shared state / ordering: selected - cross-file test coupling under sharding, workflow
  concurrency groups -> 1.4, 4.4.
- Auth / permissions / secrets: selected - `issues: write` only in the floating workflow -> 3.1.
- Error handling / rollback / partial outputs: selected - missing shard, empty shard, wall-kill -> 1.2, 2.1.
- Release / packaging / dependency compatibility: selected - no new dependency; floating resolve isolated -> 3.1.
- Documentation / migration notes: selected - instructions source + regenerated agent docs, spec -> 4.2.
- Resource limits / large input / discovery: not selected beyond the timeout item covered in 1.3.
- Schema / File IO / Legacy compatibility: not selected - no data format, no runtime file writes.

## 1. #2710 shards

- [x] 1.1 `scripts/ci/shard_tests.py` + `scripts/ci/full_test_durations.json`; pure deterministic partition.
- [x] 1.2 Tests: union/disjoint/determinism on synthetic and real trees, default weight, empty shard exits
      non-zero, each shard printed in ascending path order; ci.yml static: no `pytest $(`, `--total` from
      `strategy.job-total`, static job name, single matrix dimension, literal timeout.
- [x] 1.3 `ci.yml`: matrix N=4, `fail-fast: false`, per-shard timeout <= 60 with clear headroom, same pytest flags.
- [ ] 1.4 Dispatch run on the branch head: all shards green, wall times recorded; shard test counts sum to the
      same-SHA `--collect-only` count; `check-run` on the dispatch run rc 0 and on the PR's CI run rc 0 (skipped).
- [x] 1.5 Attribution note from the measured per-file durations (what dominates the suite).

## 2. #2710 watcher

- [x] 2.1 `check-run` over the exact shard-name set derived from ci.yml; missing/failed shard is red and named;
      both skip shapes (one unexpanded skipped job, N skipped shards) pass.
- [x] 2.2 `margin` per shard over sharded runs only; threshold constant unchanged.
- [x] 2.3 Tests in `tests/test_full_regression_watch.py`.

## 3. #2709 floating-resolve report

- [x] 3.1 Workflow: schedule weekly + workflow_dispatch only; permissions; unit-test prerequisites (fetch-depth,
      venv on PATH, scratch root); lock diff artifact; unlocked sync; tests with a step timeout below the job
      timeout; report kinds resolve / tests / timeout / injected.
- [x] 3.2 `scripts/ci/floating_resolve_report.py` + tests (create once, comment after, nothing on success).
- [x] 3.3 Static test: triggers exactly schedule + workflow_dispatch; `ci.yml` three locked installs unchanged.
- [ ] 3.4 Dispatch receipt (after merge if the workflow cannot be dispatched from a branch): run URL.

## 4. Wiring and verification

- [x] 4.1 Selector routes for new scripts/tests and the `backend` path-filter literals for the new workflow and
      the duration table; `tests/test_select_ci_tests.py` green.
- [x] 4.2 `instructions/agents/shared.md` + regenerated AGENTS.md / CLAUDE.md; spec delta.
- [x] 4.3 `uv run ruff check .`; `uv run pytest -q tests/test_full_regression_watch.py tests/test_ci_workflow_locked_install.py
      tests/test_governance_workflow_hard_gate.py tests/test_select_ci_tests.py` + new test files.
- [ ] 4.4 `openspec validate shard-full-regression-and-floating-resolve-report --strict --no-interactive`; CI green.

## Evidence Floor deviation

- node-27 真实 DB receipt 待链路恢复后补: not applicable to this PR. It changes CI configuration and CI
  helper scripts only; there is no node-27 verification surface and no deferred node-27 command.
- #2710 acceptance asks for attribution from `--durations` data of post-#2707 master runs. `--durations=25`
  names 25 tests, not per-file totals, so the per-file table and the attribution note (task 1.5) come from one
  full local run's junit instead; CI wall data (five post-lock runs, pytest step 39-59 min) is quoted beside it.
- Post-merge observations that cannot be produced before merge: #2710 "10 consecutive master push runs with a
  natural pytest end" and "`full_regression_watch.py margin` prints `::notice`"; #2709 first scheduled run.
