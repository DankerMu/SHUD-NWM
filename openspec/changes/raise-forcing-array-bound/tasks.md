# Tasks: raise the array bound to 64

## Must preserve

- The budgeting of the aggregate bound over the arrays of a pass (`_array_concurrency_budgets`) and its
  tests.
- `--repair-missing-forcing` still refuses a bound above its cap before any submission, with the same
  error class; only the number moves.
- The loader still refuses `max_concurrent` under `job_type_overrides`.
- Model overrides of the profile (`cpus_per_task`, `shud_threads`) are unchanged.
- The code default of the bound (32) is unchanged.

## 1. Fixture

- [x] 1.1 Proposal, tasks, one spec delta; `openspec validate raise-forcing-array-bound --strict --no-interactive`.

## 2. Values

- [ ] 2.1 `config/resource_profiles.yaml`: `max_concurrent` of the default profile 64.
- [ ] 2.2 `infra/env/compute.scheduler-dbfree.env.example`: `NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND=64`.
- [ ] 2.3 The repair cap in `services/orchestrator/scheduler_config/config.py` is 64, as a named
  constant, and its message states the number from that constant.
- [ ] 2.4 Followers: none is expected. Nothing compares the real profile file, the real env template or
  the evidence field `slurm_array_concurrency_bound` with 32 (checked in the fixture review:
  `tests/test_node22_scheduler_env_drift_table.py` pins seven other keys and requires exactly those
  rows; `tests/test_env_templates.py` and the template read in `tests/test_production_scheduler.py`
  read other keys; `tests/test_job_array.py` reads the real profile without pinning `max_concurrent`;
  `services/orchestrator/scheduler_preflight.py`, `scripts/` and `packages/` have none). Confirm with a
  search and list anything found; do not add a row to the README's drift table.

## 3. Tests

- [ ] 3.1 The repair cap: 64 accepted, 65 refused before submission; red run with the constant back
  at 32. The existing test that sets the bound to 33 and matches `repair_missing_forcing.*32`
  (`tests/test_production_scheduler.py`, near line 17142) is rewritten for the new number; it is not
  one of the tests of D3.
- [ ] 3.2 The real profile file resolves `max_concurrent` 64 for a model without override and for the
  models with overrides (they inherit it); red run with the value back at 32.
- [ ] 3.3 The env template states 64.

## 4. Docs

- [ ] 4.1 `docs/runbooks/production-ops/service-bringup.md` (the two passages that state 32 and "the
  bound was not changed") and `docs/runbooks/production-ops/known-issues-pipeline.md` (the receipt
  sentence "sum to at most 32"): the production value is 64 since 2026-11-27 (#3344), under
  measurement; the repair cap is 64.

## 5. Evidence

- [ ] 5.1 Local: the touched test files and every test file that reads the profile file or the env
  template; `ruff check --no-cache .`; whole-tree collection; hard gate 247 / 0.
- [ ] 5.2 node-27, detached worktree at the PR head, no database: the same files.
- [ ] 5.3 Review: fixture review; one code review seat (focus: the repair cap and the followers of 2.4).
- [ ] 5.4 CI green on the PR.

## Evidence Floor

- 5.1 to 5.4.
- After the merge, on the issue (not tasks of this change): the deploy at one pass boundary
  (fast-forward, env line with backup and one-line diff; no restart; gateway health read once); the
  first pass's evidence shows `slurm_array_concurrency_bound` 64; the `sacct` comparison of the first two-source, 132-task 22:00Z pass (D4; a
  second one when the first is inconclusive) against the rollback criteria of the proposal; the conclusion in the runbook.
