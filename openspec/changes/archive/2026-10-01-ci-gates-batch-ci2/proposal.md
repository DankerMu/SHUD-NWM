# ci-gates-batch-ci2

## Why

- **#2573 — targeted `Unit Tests` die at interpreter exit after every test passes** (SIGSEGV/SIGABRT, `double free`). The root cause was confirmed on node-27 on 2026-10-01; the evidence is in the issue comment.
  - CI installs with a floating `pip install -e ".[dev]"`. That resolves eccodes 2.49 together with the `eccodeslib`/`eckitlib` wheels, and `eckitlib` bundles **PROJ 9.8.1**.
  - `findlibs` loads libeccodes with `RTLD_GLOBAL`. pyproj is loaded later, and its own bundled **PROJ 9.5.1** then binds `proj_context_*` to eckit's 9.8.1 (seen with `LD_DEBUG=bindings`). The two PROJ versions share objects, and the heap is corrupted at teardown.
  - Minimal repro, under 1 s: eccodes first, then a pyproj transform, exits 139 in 5 of 5 runs. With the order reversed, or with no eccodes, it exits 0 in 5 of 5. The lock environment (eccodes 2.47.0, no eckitlib) and production's conda GRIB runtime are not affected.
- **#2044 — nothing reports a master `Unit Tests (full)` that was cancelled or failed.** The PR #2609 ruling asks for a `workflow_run` watcher that turns red with the SHA and the run, and that warns when the P95 of the last 20 successful runs exceeds 80% of the timeout.
- **#2602 — the production-topology hard gate never runs for docs/openspec/instructions-only changes,** on the PR or on master: the `backend` filter skips both test jobs.
- **#2648 — hard line-number references in shipped code (`file.py:N`) rot silently.** 147+ of them in 29+ files; 3 groups are measured stale; all 20 `model_registry.py:N` references in openapi_restored_schemas.py point past the end of the file. No gate catches new ones.

## What Changes

- **#2573:** the three Python CI jobs (SQL Migration Dry Run, Unit Tests (full), Unit Tests) install with `uv sync --locked --all-extras --dev` instead of `pip install -e ".[dev]"`. A subprocess regression test pins "eccodes then pyproj exits cleanly with a single PROJ".
- **#2044:** a new workflow, `.github/workflows/full-regression-watch.yml` (`workflow_run` on CI completed on master pushes), plus a tested script, `scripts/ci/full_regression_watch.py`.
- **#2602:** a separate `governance.yml` job runs the hard-gate CLI, with no path filter. Its exit code decides the job. The report-only job is unchanged.
- **#2648:** fix the stale groups (24 references). Add a hard-gate check, `hard-line-reference`, that freezes the existing references as a baseline keyed by (path, reference text) and fails on any new one.

## Impact

- `.github/workflows/{ci,governance,full-regression-watch}.yml`, `scripts/ci/`, `scripts/governance/entropy_audit/`, `scripts/select_ci_tests.py`, and the tests for each.
- Stale references are fixed: all 20 `model_registry.py:N` references in `apps/api/openapi_restored_schemas.py`, plus `services/orchestrator/scheduler_state_failure.py` and `docs/runbooks/production-ops/recalibration-and-archive.md` §5.7.1.
- Spec deltas: MODIFIED `entropy-baseline-burndown` (the report-only job is kept and a separate hard-gate job is added) and MODIFIED `database-driver-selection` (the locked CI install). ADDED `ci-merge-gates`.
- Authority text: `docs/governance/entropy-budget.md`, `docs/governance/entropy-report.example.md`, and `instructions/agents/shared.md`, then regenerate `CLAUDE.md`/`AGENTS.md`. Stale comments: `pyproject.toml`, `docs/runbooks/ci-test-routing.md`.
- No runtime behaviour changes. The `-m` marker expressions, `concurrency` and `unit-test-targeted` selection logic are not weakened.
