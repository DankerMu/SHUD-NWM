# Give the display-catalog warmer explicit stop semantics and harden the bare-next global clocks (#1276)

## Why

`apps/api/display_cache.py`'s warm loop is a daemon thread with no
stop event, no join, and no test-visible reset: `_warm_loop` runs
`while True: time.sleep(45); now = time.monotonic(); ...` and
`_warmer_started` is a module global that
`clear_display_catalog_cache()` (the only test hook) never resets. Any
test that builds a display-readonly app once (test_runtime_mode,
test_monitoring_api, test_retry_cancel_consistency,
test_pipeline_logs_artifacts all do) leaves the thread alive for the
rest of the pytest session, consuming the PROCESS-GLOBAL
`time.monotonic` every 45 s.

Meanwhile 4 cases in `tests/test_orchestration_chain.py`
(:3975-3976/:4012-4013/:4052-4053/:11789-11790) and 1 in
`tests/test_run_qhh_continuous.py` (:267) monkeypatch that global
clock with a 4-element iterator consumed by a BARE `next()` — the
patch target `services.orchestrator.chain.time` (and `runner.time`)
IS the stdlib `time` module, so the patch is process-wide. When the
warmer's tick lands inside a patched window, the thread eats iterator
elements and the main thread hits `StopIteration` — the
run-to-run 4-vs-5-red drift that burned PR #1275's E6 adjudication.
Issue #1276 carries a deterministic reproduction (external `warmfast`
pytest plugin compressing the 45 s interval to 0.02 s; no repo file
changed) plus a thread-liveness probe showing
`display-catalog-warmer` alive at session end.

Production is NOT defective: in the live display_readonly process the
thread is supposed to live until process exit. The defect is
test-lifecycle: the thread is unstoppable and the patched clocks are
exhaustible.

A second, quieter defect the Event-based loop also fixes: the same 5
tests monkeypatch `time.sleep` to a no-op, so inside a patched window
the warmer's `time.sleep(45)` degrades to a busy-spin that eats
iterator elements as fast as the loop can turn. `threading.Event.wait`
is not `time.sleep` — the monkeypatch cannot touch it — so the
Event-based loop keeps real pacing even under those patches (stronger
than sleep, not merely equivalent).

## What Changes

The issue's recommended route (treat the root, both sides), adopted:

1. **Stop semantics in `apps/api/display_cache.py`** (~15 lines):
   a module-level `threading.Event` replaces the bare sleep —
   `_warm_loop` becomes `while not _stop_event.wait(interval): ...`
   (reading `DISPLAY_CATALOG_WARM_INTERVAL_SECONDS` from the module
   global each iteration, so interval-compression via module attr
   keeps working); `start_display_catalog_warmer` records the thread
   handle and clears the event before starting; NEW
   `stop_display_catalog_warmer()` sets the event, joins the thread
   with a bounded timeout, and — only after a successful join —
   resets `_warmer_started`/the handle and clears the event,
   returning `True` (returns `True` too when no thread was running,
   `False` on join timeout without resetting state, so a stuck
   thread fails loudly instead of allowing a silent second thread).
   Lock discipline: never hold `_lock` across the `join()` (the warm
   loop's replay path takes the same lock) — same shape as the
   existing in-repo precedent
   `services/orchestrator/scheduler_lease.py:85-117`. Production
   behavior is unchanged: same daemon flag, same interval, same
   replay semantics; `event.wait(t)` paces like `time.sleep(t)`
   until stop is requested, and unlike `time.sleep` it is immune to
   the tests' sleep-no-op monkeypatches (see Why).
2. **Autouse teardown in `tests/conftest.py`**: a function-scoped
   autouse fixture calls `stop_display_catalog_warmer()` on teardown
   (no-op when nothing started) and asserts it returned `True`, so
   no test can leak the thread into its successors; after a
   successful stop it also calls `clear_display_catalog_cache()` so
   one test's `_hot_paths` cannot steer a later test's replays.
3. **Bare-next clock hardening**: the 5 sites get an explicit
   default — `next(monotonic_values, float("inf"))` — so
   cross-thread over-consumption degrades to "clock pinned past
   every deadline" instead of `StopIteration`. The default is
   `float("inf")`, NOT the terminal value 2.0: these tests no-op
   `time.sleep`, and the chain timeout loops compare
   `monotonic() >= deadline` where `deadline = monotonic() + 1` — a
   finite pin can never satisfy that and would hot-loop forever,
   while `inf` keeps the timeout branch reachable (`inf + 1 == inf`,
   `inf >= inf`). (In-repo default-carrying precedent:
   `tests/test_node27_timeseries_compression_benchmark.py:422`
   uses `next(ticks, 901.0)` — there the pinned value is past that
   test's deadlines.)
4. **Thread-leak guard tests** (in
   `tests/test_display_catalog_cache.py`): (a) lifecycle — start the
   warmer against a throwaway app with the interval compressed via
   module attr, assert the thread is alive and named
   `display-catalog-warmer`, call `stop_display_catalog_warmer()`,
   assert `True`, assert no thread of that name remains in
   `threading.enumerate()`, and assert a subsequent start works
   again (restartability); (b) replay-liveness — a faked
   `_replay_targets` (Event-setting) is actually invoked by the
   loop, closing the today-unasserted loop-body oracle gap; (c)
   loud-timeout — a blocked replay makes `stop(timeout=0.05)` return
   `False` without resetting state, then a released blocker lets a
   second stop return `True`.

Explicitly not adopted: the alternative "avoid the global patch in
tests only" (chain-local `monotonic` import / injected clock) — it
would leave the unstoppable thread replaying ASGI requests across
every later test case, just moving the collision (issue: 止痛不治病).
Out of scope: the cache TTL/stale-while-revalidate semantics, the
production warm behavior itself, and #1272/#1274's deterministic red
sets (already merged fixes).

## Impact

- Affected code: `apps/api/display_cache.py` (stop semantics),
  `tests/conftest.py` (autouse teardown),
  `tests/test_orchestration_chain.py` (4 next-defaults),
  `tests/test_run_qhh_continuous.py` (1 next-default),
  `tests/test_display_catalog_cache.py` (guard test). The final file
  set is checked against `git diff master...HEAD --name-only` at
  evidence time.
- Frozen surfaces (zero diff): `apps/api/startup_wiring.py`,
  `apps/api/main.py`, `services/orchestrator/chain.py`,
  `workers/**`, the cache read/store path inside display_cache
  (`display_catalog_cached`, `_store_value`, `_replay_targets`,
  `clear_display_catalog_cache` semantics).
- Affected specs: `real-integration-test-matrix` (1 ADDED
  requirement: background daemons started by app factories are
  stoppable and test-session-hygienic).
