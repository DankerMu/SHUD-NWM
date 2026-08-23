# Scope the orchestrator poll-interval floor to the env-derived construction path

## Why

`services/orchestrator/chain_config.py:111` floors `poll_interval_seconds` to
1.0s **unconditionally**, inside `OrchestratorConfig.__post_init__`:

```python
object.__setattr__(self, "poll_interval_seconds", max(float(self.poll_interval_seconds), 1.0))
```

Ten test files construct `OrchestratorConfig(poll_interval_seconds=0)`
believing they have turned polling delay off. They have not — the floor silently
rewrites their `0` to `1.0`, and the poll loops at
`services/orchestrator/chain_stage_execution.py:1026` and
`services/orchestrator/chain_forecast_execution.py:1219` then sleep a real
second per iteration.

Measured on `tests/test_orchestration_chain.py` (issue #1671, comment
2026-08-22T16:30Z), per-call-site sleep accounting:

```
844.00s  services/orchestrator/chain_stage_execution.py:1026
  6.00s  services/orchestrator/chain_forecast_execution.py:1219
382 passed in 881.80s (0:14:41)
```

**844.0s / 881.8s = 95.7% of that file's wall clock is sleep**; real compute is
about 38s. cProfile on one case: 12.051s in `time.sleep`, 12 calls at 1.004s
each. The `durations` table's plateau at exactly 12.07-12.11s (28 of the top 40)
is "12 polls x 1s", not heavy work. Sleep duration is machine-independent, so
the ~20m26s this file consumed on the CI runner carries the same ~14 minutes of
fake waiting.

The consequence is the whole reason #1671 exists: `Unit Tests (full)` is the
repository's only whole-repo regression lane, and on 2026-08-22 **six of seven
master runs were killed at the 45-minute job wall** (45m14s-45m17s, definitive
`maximum execution time` annotations, not concurrency eviction); the one that
finished took 41m38s, ~8% headroom. Test count grew 13093 -> 13523 in a day.
The post-merge safety net is not "late", it is absent — and silently so.

## What Changes

- The floor moves out of `__post_init__` and into `OrchestratorConfig.from_env`
  (`chain_config.py:147`), applied to the environment-derived value:

  ```python
  poll_interval_seconds=max(float(os.getenv("ORCHESTRATOR_POLL_INTERVAL_SECONDS", "30")), 1.0)
  ```

- `__post_init__` no longer rewrites `poll_interval_seconds`. An explicitly
  passed value is honored verbatim.
- A regression test pins the floor on the env path — **there is none today**;
  `grep` over `tests/` finds no assertion on this clamp at all, so the invariant
  is currently undefended and a silent removal would be invisible.
- No test file is edited to gain the speedup: the ten files already pass
  `poll_interval_seconds=0`, and that argument simply starts working.
- `.github/workflows/ci.yml`'s `timeout-minutes: 45` (`:211`) is **not** touched
  in this change (see Out of scope).

## Why this shape, and not the alternatives

The floor exists to stop the production orchestrator hammering `sacct`, and it
must keep doing that. Three designs were on the table (issue #1671 comment
2026-08-22T16:30Z names the first two); the maintainer selected the first:

1. **Floor scoped to `from_env` (selected).** Smallest diff, zero test edits,
   and the two production construction sites both derive their value from
   `from_env`'s already-floored result (`scheduler_core.py:452`, `:476`), so the
   production path stays floored. Residual risk: a *future* direct
   `OrchestratorConfig(poll_interval_seconds=0.01)` on a production path would
   no longer be caught. The regression matrix and the spec delta pin the
   invariant so that risk is stated rather than latent.
2. **Explicit `poll_interval_floor_seconds` config field.** Keeps the floor
   unconditional but adds a config field and requires editing the helper in
   every affected test file.
3. **Inject the wait through `StageExecutionDependencies`** (which already
   carries `utcnow`, so the seam exists). Production semantics untouched, but
   the diff spans two execution modules plus every affected test helper.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `compute-scheduler-operationalization`: states where the orchestrator's
  minimum Slurm poll interval is enforced, and that the enforcement binds every
  path reaching production while leaving an explicitly constructed test config
  free to disable the delay.

## Impact

- `services/orchestrator/chain_config.py` — the floor's location.
- Ten test files pass `poll_interval_seconds=0` to `OrchestratorConfig`; **seven
  of them actually get faster** (measured, tasks.md T11). The other three --
  `test_e2e_ifs`, `test_e2e_m3`, `test_ifs_forecast_integration` -- record zero
  `time.sleep` calls in both the before and after states: their selected tests
  never enter a poll loop, so the floor never cost them anything. Passing the
  argument and being slowed by it are different sets. The ten:
  `test_analysis_pipeline.py`, `test_e2e_ifs.py`, `test_e2e_m3.py`,
  `test_ifs_forecast_integration.py`, `test_orchestration_chain.py`,
  `test_orchestrator.py`, `test_pipeline_logs_artifacts.py`,
  `test_production_scheduler.py`, `test_warm_start.py`,
  `test_warm_start_chaining.py`.
  All ten contribute tests to the full lane's marker expression
  (`-m "not e2e and not grib and not integration"`), verified by
  `--collect-only`: 8 / 2 / 3 / 10 / 382 / 5 / 21 / 1884 / 32 / 110.

### Correction to the issue's stated blast radius

Issue #1671's comment says thirteen test files pass `poll_interval_seconds=0`
and are "all covered by this clamp". Thirteen files do pass it, but **two of
them — `tests/test_gfs_adapter.py` and `tests/test_ifs_adapter.py` — construct
no `OrchestratorConfig` at all**; their argument goes to `GFSAdapterConfig`
(`workers/data_adapters/gfs_adapter.py:323`) and `IFSAdapterConfig`
(`workers/data_adapters/ifs_adapter.py:207`), which have their own defaults and
their own wait seams (`self.sleeper`, `_bounded_wait`) and are untouched here.
The correct count is **ten**, not eleven.

**This correction was itself wrong on first writing, and was caught by review.**
The original text here said eleven, excluding only the two adapter files. But
`tests/test_e2e.py` had to go too, for exactly the same reason: its only
`poll_interval_seconds=0` (`test_e2e.py:763`) is inside a `GFSAdapterConfig(...)`
construction at `:754`, and the one `OrchestratorConfig` that file builds
(`:718`, imported as `ChainOrchestratorConfig` at `:19`) passes no
`poll_interval_seconds` at all -- it takes the `30.0` default and gains nothing
from this change. Applying a correction methodology to someone else's count and
then not applying it to one's own is the failure mode this note exists to record.

Supporting nuance, not a separate count: of the ten, only **nine** have that
argument inside the *measured* lane. `tests/test_e2e_ifs.py`'s
`OrchestratorConfig(poll_interval_seconds=0)` (`:105`) sits in a
`@pytest.mark.grib` test (`:75`) and is deselected by
`-m "not e2e and not grib and not integration"`; the two tests that file does
contribute to the lane use no poll interval.

## Out of scope

- Raising `.github/workflows/ci.yml:211`'s `timeout-minutes`. Maintainer
  decision on this change: measure first. Raising the wall while ~14 minutes of
  fake waiting is still in the budget would bake the fake waiting into it, and
  at +430 tests/day any fixed new number has a short life. A follow-up decision
  is taken from this change's measured post-fix duration.
- Sharding or partitioning the full lane. Issue #1671 ranks it last while the
  hot spots are this concentrated.
- `tests/test_entropy_audit_script.py`'s cost. Measured and genuinely
  CPU-bound — 16.45s for a single case, several at 15-32s — not sleep. Separate
  problem, not addressed here.
- The pre-existing master red in `tests/test_entropy_audit_script.py`
  (`test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`,
  tracked as #1707). It is unrelated to timing and is not addressed here.

  **Retracted:** this bullet originally predicted that red "will still be red in
  the receipt this change produces". It was not. Both local full-suite runs are
  `rc=0` (13679 and 13689 passed, zero failures) and CI run `32625258977` is
  `conclusion=success`. The prediction is withdrawn rather than deleted, so that
  a reader who saw the earlier text knows it was tested and found false. See
  tasks.md T13/T17.
- The adapter configs' own poll intervals (see the correction above).
