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
- `OrchestratorConfig` is a shared helper: 11 test files plus 2 production
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
- Legacy compatibility / examples — **SELECTED**: 11 test files and 2 production
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
      what the eleven test files rely on.
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
- [x] T10b Second red-proof mutation, on the propagation rather than the floor:
      in `services/orchestrator/scheduler_core.py`, replace
      `poll_interval_seconds=config.poll_interval_seconds` (`:452` and `:476`)
      with a hardcoded sub-floor literal, and show **T7 fails on both branches**;
      then restore and show it passes, proving restoration with
      `git diff --stat services/orchestrator/scheduler_core.py`. This is the
      mutation design.md D1's safety argument actually rests on — D1 claims the
      design is safe *because* those two sites propagate an already-floored
      value, so the test that defends that claim must be shown to bite.

## 3. Measurement — this is the deliverable, not a side effect

- [x] T11 Per-file wall clock, before and after, for all eleven affected files
      (`test_analysis_pipeline`, `test_e2e`, `test_e2e_ifs`, `test_e2e_m3`,
      `test_ifs_forecast_integration`, `test_orchestration_chain`,
      `test_orchestrator`, `test_pipeline_logs_artifacts`,
      `test_production_scheduler`, `test_warm_start`, `test_warm_start_chaining`),
      run under the full lane's marker expression
      `-m "not e2e and not grib and not integration"`. Report a table: file,
      before, after, delta, and pass/fail counts before and after. **Pass counts
      must be identical** — a changed count is a finding, not a win.
- [x] T12 Sleep-attribution re-measurement on `tests/test_orchestration_chain.py`
      after the change, using the same accounting-proxy technique as issue
      #1671's comment (an out-of-tree pytest plugin wrapping `time.sleep`; do not
      add it to the repo). Expected: the 844s at
      `chain_stage_execution.py:1026` collapses to approximately zero. Paste the
      totals block.
- [ ] T13 (orchestrator-owned) Full local suite before and after:
      `uv run pytest -q -m "not e2e and not grib and not integration"`.
      Report both durations and both result lines. The pre-existing #1707 red in
      `tests/test_entropy_audit_script.py` is expected in **both** runs — state
      that explicitly so it is not read as a regression. Any *other* difference
      in the failure set is a finding.
- [ ] T14 (orchestrator-owned) Confirm no test was skipped, deselected, or turned into a collect-only
      smoke as a side effect (issue acceptance criterion 3, "覆盖不降级"). The
      reconciliation is **not** "the counts match" -- this change adds tests, so
      the selected count must rise. It must rise by *exactly* the number added:
      **+10** (nine from T5-T10 plus one from T8b's type-coercion regression).
      The **deselected** count must be identical between the two runs, and the
      failure set must differ only by the #1707 red being present in both. A
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
- [ ] T17 (orchestrator-owned, after the branch is pushed) Trigger
      `workflow_dispatch` on `Unit Tests (full)` for this branch and capture the
      receipt: the natural pytest summary line, the job wall clock, and the
      headroom against `timeout-minutes: 45`. This is issue #1671's acceptance
      criterion 1 and the change's oracle. Record explicitly that the run's
      failure conclusion, if any, is the pre-existing #1707 red and not a
      timeout.

## Non-goals

- Raising `timeout-minutes` (design.md D4; issue acceptance criterion 4 stays
  open, to be decided from T17's measurement).
- Sharding the full lane.
- `tests/test_entropy_audit_script.py`'s genuine CPU cost.
- Fixing the pre-existing #1707 red.
- The adapter configs' own poll intervals.
