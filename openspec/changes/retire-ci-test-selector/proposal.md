# Run the full suite on pull requests and retire the test selector (#3219)

## Why

The PR test selector costs more than the minutes it saves (measured 2026-11-19):

- 8,759 lines of selector code with 448 path rules, plus `tests/test_select_ci_tests.py` (25,260 lines, 690 tests), the slowest
  file of the full suite (334 s of 2,484 s);
- 423 of 2,919 commits touch selector paths, 93 of them only to add or fix a route;
- it is fail-open: an unknown backend path selects only core smoke and an empty selection degrades to `pytest --collect-only`;
- 60 of the last 300 master full runs were red, in streaks, against 16 of 300 PR runs. The 2026-11-15/16 streak was a selector
  miss (fixed by #3153, routed afterwards by #3167);
- PR median 525 s against 10–19 min for the full suite on 4 shards; the repository is public, so runner minutes are free.

## What changes

1. `.github/workflows/ci.yml`
   - `unit-test` ("Unit Tests (full)") runs on `workflow_dispatch`, on every push to master whose diff is not only
     `apps/frontend/**`, and on every pull request whose diff is not only `apps/frontend/**`. Marker expression, steps and the
     30-minute per-shard timeout are unchanged. The matrix grows from 4 to 6 shards.
   - `unit-test-targeted` ("Unit Tests") is deleted.
   - The `changes` job gains one output, `pytest`, true iff at least one changed path is outside `apps/frontend/`. The `backend`
     filter (its only consumers were the two pytest jobs) is deleted. `all_files` stays as the input of the new output.
     The `database`, `frontend`, `docs`, `openapi`, `schemas`, `node_tests` and `workflows` filters are unchanged.
2. Deleted: `scripts/select_ci_tests.py`, `scripts/select_ci_tests_rules.py`, `scripts/select_ci_tests_tables.py`,
   `scripts/select_ci_tests_imports.py`, `scripts/ci_selector_snapshot.sh`, `tests/test_select_ci_tests.py`, and the four
   `tests/fixtures/*partition*` JSON files whose only reader was that suite.
3. Every live reference is removed or reworded: test and script comments, scoped `AGENTS.md` files,
   `instructions/agents/shared.md` (regenerated `CLAUDE.md` / `AGENTS.md`), runbooks, `pyproject.toml`, `.large-file-guard.json`,
   the entropy line-reference baseline, `scripts/ci/full_test_durations.json`, the `tests/conftest.py` hook that only served the
   selector suite, and the tests that pin ci.yml (`tests/test_ci_shard_tests.py`, `tests/test_ci_workflow_locked_install.py`,
   `tests/test_floating_resolve_report.py`, `tests/test_full_regression_watch.py`).
4. Capability specs: the selector requirements are removed and selector clauses are cut from requirements about other things
   (15 specs; `ci-test-selector-routing` disappears entirely).

## Fixture

- Level: expanded (merge-gate change). Risk packs: none of the domain packs; CI contract only.
- Must preserve: the marker expression `not e2e and not grib and not integration`; the static job name `Unit Tests (full)` and
  its one-dimensional `shard` matrix (the full-regression watcher derives job names from it); `scripts/ci/shard_tests.py`
  behaviour; the `real-db-integration`, frontend, lint, schema, OpenAPI and workflow-lint lanes and their filters; the
  concurrency block semantics; `docs/runbooks/ci-test-routing.md` as a file (tests and conftest messages cite it).
- Out of scope: pytest-xdist; frontend lanes; the `database` filter's contents; splitting any test file.

## Decisions

- D1. One gate for push and pull request: `needs.changes.outputs.pytest == 'true'`, or `workflow_dispatch`. A docs-only or
  openspec-only diff runs the suite (about 50 test files read `docs/`, 46 read `openspec/`). An empty changed-file list skips it.
- D2. `pytest` is computed in a shell step from the paths-filter `all_files` JSON with `jq`; no second paths-filter step and no
  reliance on `predicate-quantifier`. The JSON reaches the step through `env:`, never through `${{ }}` inside `run:`. Malformed
  JSON or an empty string fails the step; only a well-formed empty array yields `false`. The step does not use the `jq -e` exit
  status as its result.
- D3. 6 shards. Estimated test seconds per shard after the selector suite is gone: 369 (4 shards: 554, 8 shards: 277); the
  largest single file is 227 s.
- D4. The partition-oracle fixtures and the requirements they enforced (frozen collection-identity oracles of finished test
  splits) are removed, not ported.
- D5. The roughly 60 workflow-text pins that lived only in the selector suite (concurrency block, filter literals, real-DB job
  contract) are not ported. The workflow is validated by actionlint and by running; the tests listed in "What changes" 3 keep
  the pins that matter to the watcher and the locked install.
- D6. Requirements whose only enforcement was a selector-suite test and whose subject is the workflow's own text are removed
  rather than kept as unenforced prose (`ci-contract-baseline`: "CI paths-filters MUST NOT carry dead file patterns";
  `ci-merge-gates`: "The frontend CI filter entries SHALL be pinned by a meta-test").
  The line is drawn at the requirement text: one that names a meta-test or pin as its obligation is removed; one that states
  a property of the workflow itself (concurrency grouping, the `database` trigger registry) stays as a true statement even
  though nothing pins it any more.
- D7. Frozen history is not rewritten: `openspec/changes/archive/**`, `docs/runbooks/receipts/**`,
  `docs/review-loop-log.jsonl`, `docs/adr/0003-review-lens-rotation-keep.md`, `docs/governance/entropy-audit-2026-10-16*`.
  The zero-reference check excludes exactly these.
- D8. This PR edits the workflow, so its own PR run already executes the new gate; a `workflow_dispatch` run on the branch is
  the additional proof that every shard is green. The push leg cannot run before the merge, and a mis-wired output would skip
  the job silently (the watcher reads a skipped job as a path-filter skip), so the first master push run after the merge is
  checked by hand (E10).
- D9. A 4-shard master run that finishes after this merge may be reported by the watcher as missing shards 5 and 6 (it reads
  the matrix from master HEAD). That one report is expected and is not a regression.
