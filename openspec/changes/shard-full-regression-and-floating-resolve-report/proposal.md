# Shard the master full regression and add a weekly floating-resolve report lane

## Triage

```text
Issue type: bugfix + feature (batch CI: #2710, #2709)
Fixture level: expanded
Upstream suggested level: absent (orchestrator selects expanded: merge-gate CI config, operator alerting-lane
  parity for the watcher, a new workflow with issues:write)
Blast radius: a shard partition that silently drops test files turns the only whole-repo regression into a
  partial one; a watcher that does not see every shard reports healthy while a shard was wall-killed; a
  floating lane wired to push/pull_request brings the #2573 nondeterminism back to the merge gate; a report
  step that opens a new issue every week.
Selected risk packs: Config / project setup; Public API / CLI / script entry; Concurrency / shared state /
  ordering; Auth / permissions / secrets; Error handling / rollback / partial outputs;
  Release / packaging / dependency compatibility; Documentation / migration notes
Evidence floor: uv run ruff check .; the watcher, shard and report script tests; tests/test_select_ci_tests.py;
  tests/test_ci_workflow_locked_install.py; CI green on the PR; ONE workflow_dispatch of ci.yml on the branch
  head (the PR lane never runs the full job) with every shard green and its wall time recorded.
  This change has no node-27 verification surface.
```

## Why

- **#2710**: `Unit Tests (full)` runs 39-59 min against a 60-min wall on near-identical commits after the
  locked install (install is ~3 s; the spread is the runner). It was wall-killed on 2026-09-22 and 2026-10-01.
  The operator fixed the remedy axis: shard the job; do not raise `timeout-minutes`.
- **#2709**: since #2707 every CI job installs `uv.lock`, so nothing surfaces upstream breakage until someone
  runs `uv lock --upgrade`.

## What Changes

- `.github/workflows/ci.yml`: `unit-test` becomes a matrix of N shards (`fail-fast: false`), each running the
  files `scripts/ci/shard_tests.py` assigns to it, with a per-shard timeout that leaves clear headroom.
  Marker expression, `--durations=25`, scratch-root step, locked install and `concurrency` are unchanged.
- `scripts/ci/shard_tests.py` (stdlib): deterministic greedy partition of the collected test files by a
  checked-in duration table; every file lands in exactly one shard; unknown files get a default weight.
- `scripts/ci/full_regression_watch.py`: `check-run` requires every shard job and fails on any non-success;
  `margin` computes P95 per shard over runs that carry shard jobs.
- New `.github/workflows/floating-resolve-report.yml` (`schedule` weekly + `workflow_dispatch` only) and
  `scripts/ci/floating_resolve_report.py`: resolve unlocked, upload the `uv.lock` diff, run tests, and on
  failure create or update ONE tracking issue. Report-only; never a merge gate.

## Non-goals

- Raising any `timeout-minutes`; weakening the marker expression; changing `concurrency`; changing
  `MARGIN_THRESHOLD`; touching `unit-test-targeted` or the three `uv sync --locked` installs.
- pytest-xdist / pytest-split (a new dependency changes `uv.lock`).
- Fixing whatever the floating lane finds; auto-committing lock upgrades; making either lane a required check.
- #2710 acceptance "10 consecutive master runs" and "margin prints ::notice": post-merge observations.
