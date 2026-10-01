# Design — ci-gates-batch-ci2

## D1 #2573: install from the lock; regression test; install guard

### Lock install
The three jobs that run `pip install -e ".[dev]"` in `.github/workflows/ci.yml` change their install: SQL Migration Dry Run, Unit Tests (full) and Unit Tests. In each job:
- Add `astral-sh/setup-uv@v5` and keep `actions/setup-python@v5` (3.11).
- Replace the install step with `uv sync --locked --all-extras --dev`.
- Add `echo "$PWD/.venv/bin" >> "$GITHUB_PATH"`. Appending to `$GITHUB_PATH` from a step is the **only** permitted way to change PATH. Job- or workflow-level `env: PATH` / `VIRTUAL_ENV` is forbidden, because existing pins require the real-db job env to be exactly three keys, no workflow-level `env`, the step env on `Run targeted tests` is exactly `{TARGETED_TESTS_JSON: ...}`, and that step has no job env (`tests/test_select_ci_tests.py:14975-15050`).
- Existing bare `pytest`/`python` commands run unchanged inside the locked venv.
  - The targeted job's `Run targeted tests` run scalar stays byte-identical (`AUDITED_TARGETED_RUN`, `tests/test_select_ci_tests.py:8097+`).
  - The selection step keeps using `jq`, never `uv run` (pinned at `:10477`).
- Update the now-false comments that say this job has no setup-uv: `ci.yml:379-380` and `tests/test_select_ci_tests.py:10474`. Change the wording only, not what is asserted.
- `--locked` fails if `uv.lock` is out of date with `pyproject.toml`; that is wanted.
- The JSON Schema job's `pip install check-jsonschema` is a standalone tool install and stays.
- Extras parity, checked against `pyproject.toml`: the only extra is `dev`, and there are no dependency groups. So `--all-extras --dev` gives the same package set as `.[dev]`, but at lock versions.

### Text that becomes stale
- `pyproject.toml:14`, the comment "CI installs from pyproject (not uv.lock)".
- `docs/runbooks/ci-test-routing.md:6`: "installed via `pip install -e`" and "no cwd `.venv`".
- The `database-driver-selection` spec scenario: MODIFIED delta in this change.

### Install guard, new: `tests/test_ci_workflow_locked_install.py`
- Parse ci.yml with yaml.
- Every job whose steps run `pytest`, which covers at least `unit-test`, `unit-test-targeted` and `real-db-integration`, must have:
  - a `uv sync --locked --all-extras --dev` step;
  - a step appending `.venv/bin` to `$GITHUB_PATH`;
  - no `pip install -e` anywhere.
- No job- or workflow-level `env` sets `PATH` or `VIRTUAL_ENV`.
- Route `.github/workflows/ci.yml` to this suite, to the isolation test and to `tests/test_full_regression_watch.py`, because the wiring meta-test reads the ci.yml name, the job name and timeout-minutes. Update the exact pin `test_select_tests_maps_ci_workflow_change_to_the_meta_guard_suite` (`tests/test_select_ci_tests.py:1621`) to the new exact list.

### Regression test, new: `tests/test_native_proj_isolation.py`
It runs `subprocess.run([sys.executable, "-c", CHILD])`. The child script:
1. Tries `import eccodes; eccodes.codes_get_api_version()` and prints whether the eccodes native library loaded, tolerating ImportError/RuntimeError.
2. Runs `import pyproj; pyproj.Transformer.from_crs(4326, 3857, always_xy=True).transform(100.0, 37.0)`.
3. On Linux, prints the distinct real paths of mapped `libproj*` files from `/proc/self/maps`.

The parent asserts:
- the child's return code is 0, so a signal death fails, with stderr attached;
- on Linux, no more than one distinct libproj is mapped, with the message naming #2573 and the mapped paths.

The "eccodes loaded" flag is printed (visible with `-rA`) and included in the messages. If eccodes did not load, the test still asserts; it does not skip.

The test needs no DB and no marker, so it runs in the default lane. It is routed for `pyproject.toml`, `uv.lock` and `.github/workflows/ci.yml`; add pins.

### Evidence
- The isolation test goes red in node-27's floating venv (`/home/nwm/tmp/d2573-venv-float`) and green in the lock venv.
- Before merge, a `workflow_dispatch` CI run on the branch head exercises the lock install in Unit Tests (full) and SQL Migration Dry Run, which never run on PRs. A real-db job that the dispatch skips is recorded as such.
- Parity rerun: the original 59-file selection, three times in a node-27 `uv sync --locked --all-extras --dev` venv. This confirms the lock environment, not new proof.

## D2 #2044: full-regression watcher

### Facts measured on 2026-10-01
The classifier is built from these, not from assumptions.
- The top-level `name:` in ci.yml is `CI`; the job is named literally `Unit Tests (full)` (`jobs.unit-test.name`).
- Run 35759146799 was a master push. Its job 106852569935 has conclusion `cancelled`, and its check-run annotations include, at failure level, `The job has exceeded the maximum execution time of 1h0m0s` and `The operation was canceled.`.

### `scripts/ci/full_regression_watch.py`
Standard library only (`urllib`, `json`), plus yaml from the lock. The token comes from env `GITHUB_TOKEN`; the repo from `GITHUB_REPOSITORY`.

**`check-run --run-id N`** fetches the jobs with `filter=latest`, so the latest attempt is read after a re-run.
- The `Unit Tests (full)` job is absent from a push run → **exit 1**: `::error::` "job not found — renamed? watcher wiring broken".
- `success` → exit 0, with a `::notice::`.
- `skipped` → exit 0 only if the same run's `Detect changed areas` job concluded `success`, which means the path filter skipped it. Otherwise it is a skip caused by an upstream failure → exit 1.
- `cancelled`, `failure` or `timed_out` → exit 1 with `::error title=Unit Tests (full) <conclusion>::sha=<sha> run=<html_url> job=<job html_url> reason=<reason>`. The reason is `wall-timeout` when an annotation matches `exceeded the maximum execution time`; otherwise it is `cancelled` or `failed`.

**`margin`** lists master push CI runs.
- Use `/actions/workflows/ci.yml/runs?branch=master&event=push&status=completed&created=>=<now-30d>&per_page=100`, **page through the whole window** (`page=1..`, until a page returns fewer than per_page or the call cap is reached), then sort client-side by `created_at` descending, and fetch the jobs (`filter=latest`) until 20 successful `Unit Tests (full)` jobs are found.
- **Hard cap: 60 API calls per invocation.** GITHUB_TOKEN allows 1000 per hour.
- Each job's duration is `completed_at - started_at` in minutes; the P95 uses nearest-rank.
- The timeout is `jobs.unit-test.timeout-minutes`, read from `.github/workflows/ci.yml` with yaml.
- If P95 > 0.8 × timeout, print `::warning title=Unit Tests (full) margin::P95=<x>min > 80% of <t>min (n=<k>)`. With fewer than 20 successes, use what was found and state n.
- Always exit 0, because this is a warning.

**Unit tests**, `tests/test_full_regression_watch.py`, with fixture JSON and no network. They cover:
- every classification branch, including absent, path-filter skip and upstream-failure skip;
- P95 nearest-rank;
- the threshold edges;
- annotation parsing;
- reading the timeout from the real ci.yml;
- the API-call cap.

**Workflow-wiring meta-test**, in the same file. It parses both YAML files and asserts:
- the watcher's job-name constant equals ci.yml `jobs.unit-test.name`;
- `workflow_run.workflows == [<ci.yml name>]`, `types == [completed]`, `branches == [master]`;
- the permissions are exactly `actions: read`, `checks: read`, `contents: read`;
- the job `if` requires `event == 'push'` or a dispatch;
- the margin step runs with `if: always()`.

Before merge, this meta-test is the only proof of the wiring.

### `.github/workflows/full-regression-watch.yml`
- Triggers: `workflow_run` with workflows [CI], types [completed] and branches [master], plus `workflow_dispatch` with an optional `run_id` input. Without one, the dispatch checks the newest completed master push CI run.
- Steps:
  1. checkout;
  2. setup-python 3.11;
  3. setup-uv;
  4. `uv sync --locked --all-extras --dev`;
  5. `uv run --no-sync python scripts/ci/full_regression_watch.py check-run --run-id ...`;
  6. margin, with `if: always()`.
- No `pip install`. Read-only; it writes nothing.

### Routing
- Add exact literals `.github/workflows/full-regression-watch.yml` and `.github/workflows/governance.yml` to the `backend` filter, following the #1571 precedent: comment, exact literal only, and the file must exist.
- Route `.github/workflows/full-regression-watch.yml`, `.github/workflows/ci.yml` and `scripts/ci/full_regression_watch.py` to `tests/test_full_regression_watch.py`. `governance.yml` is routed to the hard-gate meta-test in D3.

### Live evidence
- Run the script locally against real runs:
  - 35759146799 → exit 1 with `reason=wall-timeout`;
  - a recent successful master run → exit 0;
  - a master run where full was skipped → exit 0;
  - `margin` → record P95 and n.
- A `workflow_run` fires only from the default branch, so the first real fire happens after merge; record it in the archive PR.

## D3 #2602: hard gate on every PR and every master push

### New `governance.yml` job: `production-topology-hard-gate`
- Display name `Production Topology Hard Gate`.
- Mirrors the existing checkout/setup-python/setup-uv steps.
- Its run step captures the return code before printing:
  ```
  set -u; mkdir -p artifacts/governance
  rc=0; uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json > artifacts/governance/hard-gate.json || rc=$?
  ```
  It then prints the gate-eligible findings from the JSON (check_id, evidence_path, line, description) and does `exit $rc`.
- No `--structural-base-ref`: structural checks are not hard-gate ids, and `structural_budget.py:188-250` has a fallback.
- No path filter: the workflow has none. Docs/openspec-only PRs and master pushes are gated.
- Concurrency note: the workflow's concurrency, `github.ref` with cancel-in-progress, can cancel an older master push run. The newer run still gates the repository state at its SHA, but a red result is attributed to the newer SHA. This is documented in a workflow comment and accepted.

### Existing report-only job stays byte-identical
That includes its `mode == "report-only"` assertion and the `.entropy-baseline/latest.json` no-write guard. A workflow comment states that this job is the docs-only backstop, and that the pytest node `test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings` remains the backend-lane check.

### Meta-test
Parse `governance.yml` and assert:
- the hard-gate job exists, runs `--mode hard-gate`, and is not path-gated;
- the report-only job still has its report-mode assertion and does not pass `--mode hard-gate`.

Put it in an existing `tests/test_entropy_audit_*` partition, or in `tests/test_governance_workflow_hard_gate.py`. If it is new, route it from `governance.yml`.

### Authority text to update
Same wording standard as the MODIFIED spec delta:
- `docs/governance/entropy-budget.md:198-199`, plus its hard-gate id list at `:180-188`, which gains `hard-line-reference`;
- `docs/governance/entropy-report.example.md:22,30`;
- `instructions/agents/shared.md:88`, then regenerate `CLAUDE.md`/`AGENTS.md` with the project generator. Never hand-edit the generated files.

### Evidence, matching the issue's AC shapes
- **AC1, docs-only PR.** Open a stacked **draft** PR whose base is this feature branch and which contains only `openspec/changes/ci-gates-batch-ci2/planted-topology-finding.md`, holding this static guard positive fixture line: `node-22 writes to the PostgreSQL primary via DATABASE_URL for ingest.`.
  - Measured locally on 2026-10-01: the CLI returns rc 1, with `production-topology-node22-db-writer` at line 3.
  - The plant must not sit under a `receipts`/`archive` path segment, which `_topology_path_is_archive_or_generated` excludes.
  - Expected: `Production Topology Hard Gate` red, `Unit Tests` skipped. Record the URLs, then close the draft PR without merging and delete its branch.
  - The main PR's head never contains the plant, and no extra CI push lands on the main PR.
- **AC2, master push.** Archive commits write `.review-gate-issues.json`, which is a backend literal, so an archive push is not a docs-only push. The proof is therefore:
  - the meta-test asserting that the hard-gate job is not path-gated;
  - the `Production Topology Hard Gate` job URL from the first post-merge master push whose `Unit Tests (full)` was skipped. If no such push happens before the archive, record the hard-gate job URL from the archive push and state this shape difference.

## D4 #2648: fix the stale references, plus the line-reference gate

### Fix the stale groups
Replace the line numbers with symbol references, with a grep readback in the PR.
- **`apps/api/openapi_restored_schemas.py`:** all **20** `model_registry.py:N` references (`:49,82,83,116,163,164,183,184,231,271-277,370,371,386,388,429,432`). Every N is past the end of the 288-line `packages/common/model_registry.py`. Each becomes a symbol reference to the function that now owns the behaviour, in `packages/common/model_registry*.py`. The implementer locates each symbol; this is a mechanical but verified replacement.
- **`services/orchestrator/scheduler_state_failure.py`,** the comment above `_RECORDED_FAILURE_CODE_KEYS`: name the writer functions in `retry.py`/`file_orchestration_journal.py`.
- **`docs/runbooks/production-ops/recalibration-and-archive.md` §5.7.1:** the two `scheduler_generation.py:1057…` references name the branch or function instead.

### New check: `scripts/governance/entropy_audit/check_line_references.py`
Check id `hard-line-reference`, gated.

**Scope**
- `*.py` under `apps/`, `packages/`, `services/`, `workers/` and `scripts/`.
- Comments come from `tokenize`; docstrings from `ast` (module, class and function). String literals and code are ignored.
- The module's own docstring examples must not match: write them so the pattern does not fire, or exclude the module path explicitly with a comment.
- Markdown is out of scope (blind spot).

**Patterns**
- `\b[\w./-]+\.py:\d+(?:[-–]\d+)?`
- Backtick-colon-digits (`` `:\d+ ``).

**Baseline**
- `scripts/governance/entropy_audit/line_reference_baseline.json`, a map `path → {reference text: count}` with no line keys.
- A finding is reported when a file's count for a reference exceeds its baseline count.
- Removing references never fails. There is **no freshness assertion**: baseline slack is allowed, because it only shrinks what is permitted.
- Generated once by a dedicated script or flag. The fixed groups are not in it.

**Failure message**
`new hard line reference <ref> in <path>; replace it with a symbol reference (function/class/constant name)`. It does not mention regenerating the baseline.

**Known blind spots and false positives**, recorded in the PR:
- Not caught: `第 N 行`, `lines 40-52`, `Line 42`, bare `:N` without a backtick, and markdown.
- False positive: the backtick form fires on minute notation such as `` `:30` `` (`apps/api/routes/precip.py:190,333`, `services/tiles/mvt.py:2506`). Existing hits are absorbed by the baseline; a new minute-notation comment has to be reworded.

**Accepted risk:** the total-count bound does not stop a deleted entry's slack from being reused under a new key, because a key-subset check would need cross-commit state. This is documented in the PR.

**Count**
- Measure the check on the merge-base before the fixes; the reviewer's scratch extractor found about 232 references in 44 files.
- A test pins the baseline total at no more than that number minus the fixed references.
- The number is written in the PR.

### Registration (all required)
- `constants.py` `CHECK_FAMILIES` and `HARD_GATE_CHECK_IDS`.
- `report.py` `_collect_findings`.
- `scripts/select_ci_tests.py`:
  - `ENTROPY_AUDIT_PACKAGE_MODULES`, plus its `PathTestRule`;
  - the module is routed exactly to `ENTROPY_AUDIT_TESTS`.
- The facade `scripts/governance/audit_repo_entropy.py`: the `_mod_*` import, the `_ENTROPY_AUDIT_PACKAGE_MODULES` tuple and `__all__`.
- Tests:
  - `tests/test_entropy_audit_line_references.py` becomes the **16th** partition and is added to `ENTROPY_AUDIT_TESTS`;
  - rename and update `test_entropy_audit_package_tracked_tree_is_exactly_twenty_one_modules` (21 → 22) and `..._exactly_fifteen_suites_and_one_helper` (15 → 16), at `tests/test_select_ci_tests.py:3085,3137`.
- The baseline JSON is routed to the new partition via an exact `backend` literal.

### Tests
- A new `foo.py:123` comment outside the baseline fails, with the symbol-reference message.
- Deleting a baseline-listed reference from code passes.
- `EPSG:4326`, `http://h:5432` and `f"{x:03d}"` produce no hit.
- A reference inside a string literal produces no hit.
- A docstring reference is caught.
- The current repo passes the hard gate.
- The baseline total is at or below the measured cap.

## Must preserve
- In ci.yml: the `-m` marker expressions, `concurrency`, the targeted selection logic, the `AUDITED_TARGETED_RUN` byte-identity, the selection step's use of `jq` without `uv run`, and the env-key pins on the real-db job and `Run targeted tests`.
- The governance report-only job and its assertions, byte-identical.
- No runtime behaviour change. In the stale-reference files only comments and docstrings change.

## Out of scope / accepted trade-offs
- **Losing the floating-resolution signal.** Floating resolution is how upstream breakage was found in the past (the fastapi `<0.137` cap, #2632). Moving CI onto the lock removes that signal. The follow-up is a scheduled floating-resolve job, as suggested in the #2573 comment; it is filed as an issue and not built here.
- Raising the 60-minute wall or sharding the full suite.
- Changing the selection logic of `unit-test-targeted`.
- The line references in markdown and in in-flight openspec changes.
