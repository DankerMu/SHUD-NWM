# Tasks

## 0. Risk triage

```text
Issue type: bugfix (CI/production-config)
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high
Fixture level: expanded (mandatory)
Upstream suggested level: absent (hand-written issue)
Repair intensity: high
Why:
- Touches `services/orchestrator/` -- a mandatory domain expanded-trigger in the
  active project profile ("orchestrator", "pipeline", state machine)
- Changes production config semantics: it removes an unconditional safety floor
  from `__post_init__` and re-establishes it on one specific path
- The floor guards a production Slurm-gateway poll loop; losing it means
  busy-spinning against sacct
- `OrchestratorConfig` is a shared helper: 10 test files plus 2 production
  construction sites depend on its normalization behavior
- The invariant is currently UNDEFENDED -- zero tests assert the clamp
Selected risk packs:
- Config / project setup
- Concurrency / shared state / ordering
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Error handling / rollback / partial outputs
- Slurm production lifecycle / mock-vs-real parity (domain)
OpenSpec change: scope-orchestrator-poll-floor-to-env (generated)
Evidence floor:
- Every Invariant Matrix regression row has a test (design.md)
- Measured before/after wall clock for each of the 11 affected files
- `uv run pytest -q` full local suite, before/after comparison of pass counts
- A `workflow_dispatch` `Unit Tests (full)` run reaching a natural pytest
  terminal state, with its measured duration (issue acceptance criterion 1)
- `uv run ruff check .`
- `openspec validate scope-orchestrator-poll-floor-to-env --strict --no-interactive`
```

### Risk pack selection (every core pack considered)

- Public API / CLI / script entry — **not selected**: no CLI, route, or public
  entrypoint reads `poll_interval_seconds`; `scheduler_core` is internal.
- Config / project setup — **SELECTED**: this change relocates a validation rule
  inside the production configuration object. Evidence: T5, T6, T7.
- File IO / path safety / overwrite — **not selected**: no path or file surface;
  `__post_init__`'s path normalizations are explicitly left untouched (T3).
- Schema / columns / units / field names — **not selected**: the field keeps its
  name, type, and default; only where it is floored changes.
- Auth / permissions / secrets — **not selected**: no such surface.
- Concurrency / shared state / ordering — **SELECTED**: the field controls a
  polling loop's wait. A zero value on a production path means a busy-spin
  against the Slurm gateway. Evidence: T6, T7, T8.
- Resource limits / large input / discovery — **SELECTED**: the floor is a rate
  limit on an external service (`sacct`). Removing it from a reachable path is a
  resource-exhaustion regression. Evidence: T6, T7.
- Legacy compatibility / examples — **SELECTED**: 10 test files and 2 production
  sites consume this helper; sibling config classes with the same field name
  must be shown unaffected. Evidence: T4, T9, T10.
- Error handling / rollback / partial outputs — **SELECTED**: the floor's
  behavior on invalid input (clamp up, never raise) must be preserved exactly;
  turning a mis-set env var into a startup crash would be a worse failure mode.
  Evidence: T6.
- Release / packaging / dependency compatibility — **not selected**: no
  packaging surface.
- Documentation / migration notes — **not selected**: no operator-visible
  behavior change (production keeps the same effective interval); the rationale
  lives in this change and in the spec delta.
- Geospatial / CRS / basin geometry — **not selected**: unreachable from a poll
  interval.
- Hydro-met time series / forcing windows — **not selected**: same.
- SHUD numerical runtime / conservation / NaN — **not selected**: same.
- PostGIS / TimescaleDB domain behavior — **not selected**: same.
- Slurm production lifecycle / mock-vs-real parity — **SELECTED**: the floor
  exists to protect the real Slurm accounting interface, and the tests that go
  fast are exactly the mock-parity suites. Evidence: T7, T8, T10.
- External hydro-met providers / snapshot reproducibility — **not selected**:
  the adapter configs are a different class and are shown unaffected (T9).
- Run manifest / QC provenance — **not selected**: the field reaches no evidence
  artifact (design.md Invariant Matrix, Evidence surface = none).
- Published NHMS artifacts / display identity — **not selected**: same.

## 1. Implementation

- [x] T1 In `services/orchestrator/chain_config.py`, replace the
      `poll_interval_seconds` line in `__post_init__` (currently `:111`) with a
      bare `float()` coercion, dropping only the `max(..., 1.0)` floor:
      `object.__setattr__(self, "poll_interval_seconds", float(self.poll_interval_seconds))`.
      **Corrected during Phase 2**: this task originally said "delete the line",
      which silently dropped the field's type coercion along with the floor and
      let a `str` reach `time.sleep` (design.md D6). The Invariant Matrix always
      specified `-> 0.0`, a float, so the deletion diverged from the fixture.
- [x] T2 In the same file's `from_env` (`:147`), wrap the environment read so
      the floor is applied there:
      `poll_interval_seconds=max(float(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "30")), 1.0)`.
      Keep `max` semantics exactly — clamp up, never raise (design.md D2).
- [x] T3 Change nothing else in `__post_init__`. The other eleven
      normalizations (workspace_root, object_store_root, source_id,
      forecast_warm_start_required_from, scenario_id/scenario_id_explicit,
      terminal_stage, templates_dir, slurm_job_type_templates, slurm_env,
      target_python_runtime, reconcile_slurm_user/account) must be byte-identical.
      Prove it with a diff of that method.
- [x] T4 Edit **no test file** to obtain the speedup (design.md D3). If any
      existing test fails after the change, do not "fix" it by restoring a
      sleep — report it: a test that needs a real 1-second wait is a finding.
- [x] T5 Do not touch `.github/workflows/ci.yml` (design.md D4), nor
      `workers/data_adapters/*`, nor `services/production_closure/*`.

## 2. Tests — Invariant Matrix regression rows

Every row in design.md's Invariant Matrix needs a test. Put the new tests where
the existing `chain_config` / orchestrator config tests live; do not create a
new top-level file if a natural home exists.

- [x] T6 `from_env` floor rows, all five, each asserting the resulting
      `poll_interval_seconds`:
      unset -> `30.0`; `"0"` -> `1.0`; `"0.001"` -> `1.0`; `"5"` -> `5.0`
      (**not** raised — the floor is a minimum, not a normalization);
      `"-3"` -> `1.0`. Assert no exception is raised in the below-minimum cases.
- [x] T7 Both `scheduler_core._default_orchestrator_for` propagation rows, with
      `ORCHESTRATOR_POLL_INTERVAL_SECONDS="0"` in the environment:
      the `slurm_execution_enabled` branch (`scheduler_core.py:446`) and the
      `config.source_id != source_id` branch (`:470`) each yield an orchestrator
      whose `config.poll_interval_seconds` is `1.0`. This is the test that makes
      the selected design safe rather than merely small — it fails if a future
      refactor stops propagating the floored value.
- [x] T8 The explicit-construction row: `OrchestratorConfig(poll_interval_seconds=0)`
      yields `0.0`. This is the new behavior and must be pinned, because it is
      what the ten test files rely on.
- [x] T8b The type-coercion row (design.md D6):
      `OrchestratorConfig(poll_interval_seconds=0).poll_interval_seconds` is a
      `float` (`isinstance(..., float)`, not just `== 0`), and
      `OrchestratorConfig(poll_interval_seconds="7").poll_interval_seconds ==
      7.0`. Without this the field can hold a `str` that raises `TypeError` at
      `time.sleep`. Do **not** add an assertion that an explicit negative is
      clamped -- design.md D6 states explicit negatives are honored verbatim by
      design; clamping them would reinstate the defect this change removes.
- [x] T9 Unchanged-sibling rows: `GFSAdapterConfig(poll_interval_seconds=0)` and
      `IFSAdapterConfig(poll_interval_seconds=0)` still yield `0` (they never
      floored), and `services/production_closure/slurm_validation.py`'s own
      config default is unchanged. These may be assertions in one test; the
      point is that a reader can see the sibling classes were considered.
- [x] T10 Red-proof (mandatory, batched): run T6 and T7 against the
      **pre-change** source and show they behave as the old code does — T6's
      unset/`"5"` rows pass, and the `"0"`/`"0.001"`/`"-3"` rows pass too
      (the old code floored them, just in a different place), while **T8 fails**
      (old code turns explicit `0` into `1.0`). Then, on the changed source,
      delete the new floor from `from_env` and show **T6's below-minimum rows
      and T7 both fail**. Paste all runs. The second half is the one that
      matters: it proves the new tests actually defend the invariant rather than
      passing vacuously.

      **Evidence (added after review found this box ticked with nothing pasted).**
      Run in a disposable `git worktree` detached at `3c4e6019`, so the shared
      working tree was never mutated. Mutation: `max(float(os.getenv(...)), 1.0)`
      in `from_env` reduced to `float(os.getenv(...))`.

      ```
      FAILED tests/test_orchestration_chain.py::test_orchestrator_config_from_env_floors_sub_minimum_poll_interval[0]
      FAILED tests/test_orchestration_chain.py::test_orchestrator_config_from_env_floors_sub_minimum_poll_interval[0.001]
      FAILED tests/test_orchestration_chain.py::test_orchestrator_config_from_env_floors_sub_minimum_poll_interval[-3]
      FAILED tests/test_production_scheduler.py::test_slurm_enabled_orchestrator_rebuild_propagates_env_poll_interval_floor
      FAILED tests/test_production_scheduler.py::test_source_mismatch_orchestrator_rebuild_propagates_env_poll_interval_floor
      5 failed, 5 passed, 2266 deselected in 7.45s
      ```

      Restored (`git diff --stat` empty): `10 passed, 2266 deselected in 0.45s`.

      **Honest gap:** this is T10's *second* half only -- the one the task text
      itself calls "the one that matters". The first half (running T6/T7/T8
      against pre-change source to show T8 fails) was performed during
      implementation but its output was not preserved, and it is not
      reconstructed here. T8's equivalent red-proof does exist and is pasted in
      the PR body. Recording the gap rather than quietly reticking.
- [x] T10b Second red-proof mutation, on the propagation rather than the floor:
      in `services/orchestrator/scheduler_core.py`, replace
      `poll_interval_seconds=config.poll_interval_seconds` (`:452` and `:476`)
      with a hardcoded sub-floor literal, and show **T7 fails on both branches**;
      then restore and show it passes, proving restoration with
      `git diff --stat services/orchestrator/scheduler_core.py`. This is the
      mutation design.md D1's safety argument actually rests on — D1 claims the
      design is safe *because* those two sites propagate an already-floored
      value, so the test that defends that claim must be shown to bite.

      **Evidence (added after review).** Same disposable worktree at `3c4e6019`.
      Mutation: both `poll_interval_seconds=config.poll_interval_seconds` sites
      (`scheduler_core.py:452`, `:476`) replaced with the literal `0.001`
      (`git diff --stat` = `1 file changed, 2 insertions(+), 2 deletions(-)`).

      ```
      E       AssertionError: assert 0.001 == 1.0
      E       AssertionError: assert 0.001 == 1.0
      FAILED tests/test_production_scheduler.py::test_slurm_enabled_orchestrator_rebuild_propagates_env_poll_interval_floor
      FAILED tests/test_production_scheduler.py::test_source_mismatch_orchestrator_rebuild_propagates_env_poll_interval_floor
      2 failed, 1884 deselected in 1.08s
      ```

      Restoration proved as the task demands -- `git diff --stat
      services/orchestrator/scheduler_core.py` produces no output, and the tests
      return `2 passed, 1884 deselected in 0.36s`. D1's safety argument is
      therefore defended by a test that demonstrably bites.

## 3. Measurement — this is the deliverable, not a side effect

- [x] T11 Per-file wall clock, before and after, for all ten affected files
      (`test_analysis_pipeline`, `test_e2e_ifs`, `test_e2e_m3`,
      `test_ifs_forecast_integration`, `test_orchestration_chain`,
      `test_orchestrator`, `test_pipeline_logs_artifacts`,
      `test_production_scheduler`, `test_warm_start`, `test_warm_start_chaining`),
      run under the full lane's marker expression
      `-m "not e2e and not grib and not integration"`. Report a table: file,
      before, after, delta, and pass/fail counts before and after. **Pass counts
      must be identical** — a changed count is a finding, not a win.

      **Measured** (warm runs; `after` = `3c4e6019` in the main tree, `before` =
      `5063747c` in a pre-synced `git worktree`; both under
      `-m "not e2e and not grib and not integration"` with a `time.sleep`-counting
      pytest plugin). Durations are pytest's own reported figures.

      | file | before | after | delta | sleep before | sleep after | passed before / after |
      |---|---|---|---|---|---|---|
      | test_analysis_pipeline | 44.50s | 0.37s | -44.13s | 44.18s / 44 calls | 0.00s / 44 | 8 / 8 |
      | test_e2e_ifs | 0.33s | 0.36s | +0.03s | 0.00s / 0 | 0.00s / 0 | 2 / 2 |
      | test_e2e_m3 | 0.47s | 0.51s | +0.04s | 0.00s / 0 | 0.00s / 0 | 3 / 3 |
      | test_ifs_forecast_integration | 1.26s | 1.67s | +0.41s | 0.00s / 0 | 0.00s / 0 | 10 / 10 |
      | test_orchestration_chain | 874.92s | 15.87s | **-859.05s** | 854.11s / 872 | 0.00s / 872 | 382 / **390** |
      | test_orchestrator | 18.35s | 0.23s | -18.12s | 18.07s / 18 | 0.00s / 18 | 5 / 5 |
      | test_pipeline_logs_artifacts | 23.13s | 1.00s | -22.13s | 22.10s / 22 | 0.00s / 22 | 21 / 21 |
      | test_production_scheduler | 162.20s | 131.62s | -30.58s | 17.42s / 99 | 1.16s / 91 | 1884 / **1886** |
      | test_warm_start | 64.71s | 0.30s | -64.41s | 64.27s / 64 | 0.00s / 64 | 32 / 32 |
      | test_warm_start_chaining | 33.74s | 1.36s | -32.38s | 32.13s / 32 | 0.00s / 32 | 110 / 110 |
      | **total** | **1223.61s** | **153.29s** | **-1070.32s** | **1052.28s** | **1.16s** | **2457 / 2467** |

      **Pass counts are identical everywhere except the two files this change
      adds tests to** (+8 and +2 = the +10 of T14). The task text says "pass
      counts must be identical -- a changed count is a finding": read literally
      that is violated, but the change is the added tests themselves, which is
      the intended and separately reconciled delta. No pre-existing test changed
      outcome.

      **Known limit of this instrument (found by the final review, recorded
      rather than papered over).** The plugin wraps the `time.sleep` attribute,
      so a test that monkeypatches `time.sleep` itself evades the counter.
      `tests/test_e2e_m3.py:27/:66/:104` does exactly that, and its retry case
      genuinely reaches a sleep at `chain_forecast_orchestrator_cycle.py:222/230`.
      For that file, `calls=0` therefore supports only "no sleep was observed",
      **not** "it never enters a poll loop" -- the earlier wording overstepped
      its evidence. The totals and the seven/three split are unaffected: that
      call site waits on `backoff_seconds` with a `[0]` schedule, so it is
      `sleep(0)` in both states and unrelated to the floor this change moves.

      **Correction this measurement forced (the third to the file count).** Only
      **seven** of the ten files actually get faster. `test_e2e_ifs`,
      `test_e2e_m3`, and `test_ifs_forecast_integration` record **zero**
      `time.sleep` calls in *both* states: they pass `poll_interval_seconds=0` to
      `OrchestratorConfig` but their selected tests never enter a poll loop, so
      the floor never cost them anything. "Passes the argument" and "gets faster"
      are different sets, and the earlier text conflated them. The count of files
      *passing the argument* remains ten (AST-verified); the count that *gets
      faster* is seven.

      **A discarded bad measurement, recorded so it is not repeated.** A first
      pass ran `before` in a freshly created worktree and reported
      `test_ifs_forecast_integration` at 36.14s -> 1.83s -- a plausible-looking
      20x win. It was an artifact of cold bytecode/import caches in the new
      worktree; re-run warm, the same file is 1.26s -> 1.67s. The only thing that
      contradicted the false row was its own `sleep calls=0` column: a file with
      no sleeps cannot lose 34 seconds of sleep. Had the table carried wall clock
      alone, the bogus row would have shipped as evidence.
- [x] T12 Sleep-attribution re-measurement on `tests/test_orchestration_chain.py`
      after the change, using the same accounting-proxy technique as issue
      #1671's comment (an out-of-tree pytest plugin wrapping `time.sleep`; do not
      add it to the repo). Expected: the 844s at
      `chain_stage_execution.py:1026` collapses to approximately zero. Paste the
      totals block.

      **Totals block** (`tests/test_orchestration_chain.py`, warm, same lane
      marker, `time.sleep` counted by pytest plugin):

      ```
      before (5063747c): 382 passed in 874.92s (0:14:34)   SLEEPPROF calls=872 total=854.11s
      after  (3c4e6019): 390 passed in  15.87s             SLEEPPROF calls=872 total=  0.00s
      ```

      **The call count is unchanged at 872 in both states** -- the change removes
      no polling, it removes the *duration* of each poll. 854.11s / 872 calls =
      0.98s per call, i.e. the 1.0s floor, confirming the mechanism directly
      rather than by inference. This file alone accounts for 854.11s of the
      1052.28s of sleep removed across all ten files (T11).
- [x] T13 (orchestrator-owned) Full local suite before and after:
      `uv run pytest -q -m "not e2e and not grib and not integration"`, run
      serially on one machine from the same `.venv`.

      | | before (`5063747c`) | after (`3c4e6019`) |
      |---|---|---|
      | wall | 2278.33s (37:58) | 1230.13s (20:30) |
      | result | 13679 passed, 13 skipped, 183 deselected | 13689 passed, 13 skipped, 183 deselected |
      | rc | 0 | 0 |

      **Prediction corrected.** This task originally asserted that the
      pre-existing #1707 red in `tests/test_entropy_audit_script.py` would
      appear in *both* runs. It appeared in **neither** -- both runs are `rc=0`
      with zero failures, so that red does not reproduce on this lane at all.
      The claim is retracted rather than quietly dropped: a fixture that tells
      reviewers to expect a failure they cannot find wastes a review round.
- [x] T14 (orchestrator-owned) Confirm no test was skipped, deselected, or turned into a collect-only
      smoke as a side effect (issue acceptance criterion 3, "覆盖不降级"). The
      reconciliation is **not** "the counts match" -- this change adds tests, so
      the selected count must rise. It must rise by *exactly* the number added:
      **+10** (nine from T5-T10 plus one from T8b's type-coercion regression).
      The **deselected** count must be identical between the two runs, and the
      failure set must be **empty in both runs** (see T13's retraction: the
      #1707 red this criterion originally expected does not reproduce here). A
      selected delta other than +10, any deselected drift, or a collect-only
      degradation is a finding.

> Ownership note: T13/T14 are executed by the orchestrator, not the implementer.
> Each full-suite run takes roughly 45 minutes and the before/after pair must run
> **serially** on one machine or the two wall clocks contaminate each other —
> wall clock being the entire point of the measurement. Running them outside the
> implementer's session also keeps a session-length failure from discarding an
> hour and a half of measurement. The "before" run is taken at the change's merge
> base (`5063747c`) via a detached checkout of the main working tree once it is
> clean, so both runs reuse the same `.venv` -- a separate worktree would need its
> own `uv sync` and the bootstrap cost would land inside one of the two wall
> clocks being compared.

## 4. Verification

- [x] T15 `uv run ruff check .` -> zero findings.
- [x] T16 `openspec validate scope-orchestrator-poll-floor-to-env --strict --no-interactive`
      -> strict-valid.
- [x] T17 (orchestrator-owned, after the branch is pushed) Trigger
      `workflow_dispatch` on `Unit Tests (full)` for this branch and capture the
      receipt. This is issue #1671's acceptance criterion 1 and the change's
      oracle. Receipt (run `32625258977`, head `3c4e6019`):

      ```
      13686 passed, 16 skipped, 183 deselected, 1 warning in 2334.62s (0:38:54)
      job wall 07:19:47Z -> 07:59:38Z = 39m51s, conclusion=success
      ```

      Headroom against `timeout-minutes: 45` is **5m06s (11.3%)**. The lane no
      longer times out, which is what this change owed. The thin margin is
      recorded as a risk, not fixed here: raising the timeout and sharding the
      lane are both explicitly out of scope per the issue.

      The task originally instructed that any failure conclusion be attributed
      to the pre-existing #1707 red. There was no failure -- `conclusion=success`
      -- so that attribution is moot; see T13 for why the #1707 expectation was
      wrong in the first place.

## Non-goals

- Raising `timeout-minutes` (design.md D4; issue acceptance criterion 4 stays
  open, to be decided from T17's measurement).
- Sharding the full lane.
- `tests/test_entropy_audit_script.py`'s genuine CPU cost.
- Fixing the pre-existing #1707 red.
- The adapter configs' own poll intervals.
