# Design

Change surface: `.github/workflows/ci.yml` (`unit-test` job), `scripts/ci/shard_tests.py` (new),
`scripts/ci/full_test_durations.json` (new), `scripts/ci/full_regression_watch.py`,
`.github/workflows/full-regression-watch.yml` (only if its invocation must change),
`.github/workflows/floating-resolve-report.yml` (new), `scripts/ci/floating_resolve_report.py` (new),
`scripts/select_ci_tests.py` routes, `instructions/agents/shared.md` (source of the generated
AGENTS.md / CLAUDE.md "CI 门控要点"), `openspec/specs/ci-merge-gates`.

Must preserve:
- The full regression runs exactly the same test set as today: `-m "not e2e and not grib and not integration"`
  over every file pytest collects under `tests/`. No file skipped, none run twice.
- `unit-test` keeps its `if` (workflow_dispatch or backend push to master), `needs: changes`, the locked
  install, the venv-on-PATH step, the scratch evidence root step, `--tb=short --durations=25`.
- Workflow-level `concurrency` (#1650) and `unit-test-targeted` are untouched.
- `MARGIN_THRESHOLD = 0.8`, `MARGIN_SAMPLE`, `MARGIN_WINDOW_DAYS`, the watcher's exit-code contract and its
  "skipped by the path filter" handling.
- `tests/test_ci_workflow_locked_install.py` invariants (every Python job installs with `--locked`).
  The floating workflow is the one deliberate exception and lives in its own file, not in `ci.yml`.

Governing invariants:
1. Partition completeness: the union of the N shards equals the collected test-file set and the shards are
   pairwise disjoint, for ANY file set (new files included), deterministically.
2. Watcher parity: the watcher's notion of "the full regression" is "all N shard jobs of the run"; N is read
   from `ci.yml`, never duplicated as a literal.
3. The floating lane can never gate a merge: triggers are `schedule` and `workflow_dispatch` only.

## #2710 sharding

- `scripts/ci/shard_tests.py --shard I --total N` prints the test files of shard I (1-based). File list =
  `tests/**/test_*.py` plus any other pattern pytest collects here (derive it from the repo's pytest config
  `python_files`, do not hardcode a different rule); the script lists files from disk at run time, so a new
  file is picked up without regenerating anything.
- Weights from `scripts/ci/full_test_durations.json` (seconds per file, measured by a full local run; the
  orchestrator supplies the junit file). Missing file -> default weight (the median). Assignment: sort by
  (weight desc, path asc), put each file on the currently lightest shard (ties -> lowest index). Pure
  function, no randomness, no dependence on directory listing order.
- Each shard's files are printed in ascending path order (pytest runs files in command-line order; today's
  order is alphabetical, and the relative cross-file order inside a shard must stay that way).
- The shard list is written to a file by its OWN command (`python scripts/ci/shard_tests.py --shard
  ${{ matrix.shard }} --total ${{ strategy.job-total }} > shard-files.txt`), so a non-zero exit or an empty
  list fails the step before pytest starts; pytest then reads the file (`xargs`/`mapfile`). Command
  substitution in pytest's argument position is forbidden (a failing script would leave pytest with no
  paths and it would run all of `testpaths`); a static test over ci.yml rejects `pytest $(`.
- `--total` comes from `${{ strategy.job-total }}`: the matrix list is the single source of N for the
  workflow and for the watcher. One matrix dimension only (`shard`).
- `jobs.unit-test.name` stays the static string `Unit Tests (full)` (no `${{`); GitHub names the matrix
  jobs `Unit Tests (full) (<value>)`. `timeout-minutes` stays a literal integer on the job.
  `strategy.fail-fast: false`.
- N and the per-shard timeout: N = 4. Timeout chosen from the dispatch-run measurement so that the slowest
  shard's wall is at most ~50% of it (clear headroom; the 80% watcher rule is then far away). The old 60 is
  not raised: the per-shard value must be <= 60 and is expected to be 30.
- Cross-file coupling: whole files stay together, order inside a file is unchanged. A shard that fails in
  the dispatch run while the same files pass in the single-process run is a coupling defect: it gets a
  diagnosis, not a reshuffle of the table.
- Completeness guard in CI itself, not only in a unit test: one cheap job step (or a test selected for
  ci.yml changes) asserts the union/disjoint property against the real tree.

## #2710 watcher

- `check-run`: the expected job-name set is `{f"{JOB_NAME} ({v})" for v in jobs.unit-test.strategy.matrix.shard}`
  read from `ci.yml` (exact names, not a prefix match). A missing name (outside the skip case) -> error
  naming the missing shards. Skip case: the job-level `if` is evaluated before matrix expansion, so a
  path-filter skip may surface as ONE unexpanded skipped job named `Unit Tests (full)` or as N skipped
  shard jobs; both, with `Detect changed areas` success, are rc 0 "skipped by the path filter". Static
  assertions: `jobs.unit-test.name == JOB_NAME` and contains no `${{`; exactly one matrix dimension;
  `timeout-minutes` is an int. Any shard cancelled/failed/timed out -> exit 1 naming the shard, with the same
  wall-timeout annotation detection per shard. All success -> notice.
- `margin`: per shard, P95 over the last `MARGIN_SAMPLE` successful runs that HAVE sharded jobs (runs with
  the old single job are ignored, so the window is not polluted by 50-58 min single-job history). Warn when
  any shard's P95 exceeds `MARGIN_THRESHOLD` x the per-shard timeout; the line names the shard. With zero
  sharded runs the existing "no successful runs found (n=0)" notice applies.
- API call cap (`API_CALL_CAP`) still respected: one jobs call per run as today.

## #2709 floating-resolve report

- Workflow `floating-resolve-report.yml`: `on: schedule` (weekly cron) + `workflow_dispatch` (input
  `inject_failure`, default false, for the failure-path receipt); own `concurrency` group;
  `permissions: contents: read, issues: write`; a job timeout.
- The job repeats the `unit-test` prerequisites that tests rely on: `fetch-depth: 0`, `.venv/bin` on
  `$GITHUB_PATH`, the `Prepare scratch evidence root` step - otherwise it would go red for non-upstream
  reasons and open a false tracking issue.
- Timeouts: the pytest step has its own `timeout-minutes` strictly below the job's, so the `if: always()`
  report step still runs after a slow suite. The report distinguishes three failure kinds: `resolve`
  (`uv lock --upgrade` / `uv sync` failed), `tests` (pytest failed), `timeout` (pytest step timed out).
- `inject_failure: true` makes a dedicated step fail on purpose BEFORE the resolve (so no upstream state is
  involved); expected output: the report step runs with outcome failure/kind `injected` and creates or
  comments on the tracking issue with a line saying the failure was injected.
- Steps: checkout; setup python 3.11 + uv; `cp uv.lock uv.lock.locked`; `uv lock --upgrade`; write the
  package-version diff (`floating_resolve_report.py diff`) to the step summary and upload it with both lock
  files as an artifact; `uv sync --all-extras --dev` (no `--locked`); run
  `tests/test_native_proj_isolation.py` first, then the full marker-filtered suite (single job; this lane has
  no wall pressure worth a matrix, and its timeout is its own); `if: always()` report step.
- `scripts/ci/floating_resolve_report.py` (stdlib + injectable fetch, same pattern as the watcher):
  `diff` (old lock vs new lock -> "name: a -> b" lines); `report --outcome success|failure --run-url ...`:
  on failure search OPEN issues for the fixed title `ci(deps): floating dependency resolve is failing`
  (exact title match after search) -> comment on it, else create it with label set that exists in the repo;
  on success do nothing (no issue is opened; an existing open tracking issue gets a "passing again" comment
  but is not auto-closed). Two consecutive failures -> one issue, two comments.
- Nothing in this workflow commits, pushes, or opens a PR. #2709 does not change the three locked installs
  or the `unit-test` job; it only adds path-filter literals (below).
- `ci.yml` `changes` job: add the exact literals `.github/workflows/floating-resolve-report.yml` and
  `scripts/ci/full_test_durations.json` to the `backend` filter (precedent #2044/#2648) so a PR touching only
  them still starts the targeted gate; matching selector rules.

Sibling surfaces:
- `.github/workflows/governance.yml`, `m15-visual-evidence.yml`: do not reference the full job - none.
- `scripts/select_ci_tests.py` comments/rules mentioning the `unit-test` job name and timeout
  (`:1106`, `:6412`): keep them true.
- `instructions/agents/shared.md` -> regenerate AGENTS.md / CLAUDE.md with the repo's generator; never
  hand-edit the generated files.
- `tests/test_full_regression_watch.py`, `tests/test_ci_workflow_locked_install.py`,
  `tests/test_governance_workflow_hard_gate.py`: extend / keep green. Two existing assertions must stay
  true: the timeout read from `jobs.unit-test.timeout-minutes` and `JOB_NAME == jobs.unit-test.name`.
- Comments at `scripts/select_ci_tests.py` (~L1105, ~L6411) and the description in
  `instructions/agents/shared.md` are updated to the sharded wording.

Seams under test: `shard_tests` pure function + CLI; `classify_run` / `margin_line` with fake job payloads;
`floating_resolve_report` with a fake fetch; static assertions on the two workflow YAML files.

Required evidence:
- partition: any synthetic file set -> union == input, disjoint, stable across shuffled input order; the real
  tree at HEAD -> same; unknown file gets the default weight; empty shard -> non-zero exit.
- watcher: N success -> rc 0; one shard `cancelled` with the wall annotation -> rc 1, shard named,
  reason=wall-timeout; N-1 jobs -> rc 1 naming the missing shard; old single-job run in the margin window ->
  ignored; a shard with P95 > 80% of the per-shard timeout -> `::warning` naming it.
- floating: failure with no open issue -> one create; failure with an open issue -> one comment, zero create;
  success -> zero create; workflow triggers are exactly schedule + workflow_dispatch; `ci.yml` installs
  unchanged.
- dispatch run of `ci.yml` on the branch head: all N shards success; per-shard wall times recorded; the sum
  of passed+skipped+failed over the shards equals `pytest tests/ --collect-only -q -m "<same expression>"`
  on the same SHA (not a historical run's number).
- real-naming receipts before merge (local, read-only API calls): `full_regression_watch.py check-run
  --run-id <dispatch run>` -> rc 0 notice; the same command on this PR's own CI run (full job skipped) ->
  rc 0 "skipped by the path filter".
- watcher skip shapes: one unexpanded skipped job, and N skipped shard jobs -> rc 0 each.
- ci.yml static: no `pytest $(`; `--total ${{ strategy.job-total }}`; shard output ascending by path.

Non-goals: see proposal.

Review focus:
1. Can any test file be dropped or doubled (glob vs pytest collection rule, conftest-only dirs, nested dirs)?
2. Watcher: every way a shard can be missing/renamed/skipped still turns red.
3. Floating workflow: triggers, permissions minimal, no path to the merge gate, dedupe key robust.
4. Per-shard timeout really leaves headroom and 60 was not raised.
