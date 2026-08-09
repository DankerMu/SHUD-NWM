# Tasks — display-warmer-stop-semantics (#1276)

Anchors verified at master 816245d6: warm loop
`apps/api/display_cache.py:96-123` (`start_display_catalog_warmer` /
`_warm_loop`; `time.monotonic()` consumption at `:111`); module
globals `:35-39` (`_lock`/`_store`/`_hot_paths`/`_warmer_started`);
test hook `clear_display_catalog_cache` `:138-142` (does NOT reset
`_warmer_started`); starter call sites
`apps/api/startup_wiring.py:107-110` ← `apps/api/main.py:277`;
bare-next clock sites `tests/test_orchestration_chain.py:3975-3976 /
4012-4013 / 4052-4053 / 11789-11790` and
`tests/test_run_qhh_continuous.py:259+:267` (all patch the stdlib
`time` module process-globally — measured in the issue: `chain.time
is time is display_cache.time`); safe-precedent
`tests/test_node27_timeseries_compression_benchmark.py:422`
(`next(ticks, 901.0)`); production-semantics lock
`tests/test_runtime_mode.py:217-241` (function body; decisive assert
`warmed_apps == [display_app]` at `:241`; it monkeypatches the
starter so no real thread starts — must stay green untouched).

Risk triage: fixture level **compact** (S-size; ~15 production lines
+ 5 one-token test edits + 1 guard test + 1 conftest fixture). Risk
packs selected: **oracle-discrimination** (the deterministic
reproduction gate must red pre-fix and green post-fix; the guard test
must really detect a leaked thread; the next-defaults must not
weaken what the 5 tests assert) and **concurrency-lifecycle** (stop
must be race-free with start; a stuck thread must fail loudly, never
silently double-start). Not selected: record-forensic (no measured
live values), performance/UI/migration (n/a). Node-27 untouched;
everything is hermetic local.

Must-preserve behavior:

- Production warm semantics byte-compatible in effect: display_readonly
  starts the warmer (`tests/test_runtime_mode.py:217-241` green
  untouched); same daemon flag, same thread name
  `display-catalog-warmer`, same interval constant read from the
  module global EACH iteration (the issue's `warmfast`
  interval-compression method must keep working), same hot-path
  replay via `_replay_targets`, same swallow-and-retry error posture.
- `clear_display_catalog_cache()` keeps its current semantics
  (cache/hot-path clear only); stop is a NEW separate hook.
- The 5 patched tests keep asserting exactly what they assert today —
  the ONLY change at those sites is the added `next()` default
  `float("inf")`. NOT the terminal value 2.0: the chain timeout
  loops (`chain_stage_execution.py:959-961`,
  `chain_forecast_execution.py:947-949`) compute `deadline =
  monotonic() + 1` and spin `while: if monotonic() >= deadline`, with
  `time.sleep` patched to a no-op in these tests — a clock pinned at
  2.0 can never reach a deadline derived from a later read (2.0 + 1),
  turning a cross-thread over-consumption into an infinite hot loop
  instead of a clean failure. `float("inf")` keeps the timeout branch
  reachable (`inf + 1 == inf`; `inf >= inf` is True).
- Every currently-green test stays green; full-suite red set stays
  empty (post-#1274 baseline: 0 failed).

Seams under test (upstream-declared, consumed not renegotiated): the
process-global stdlib `time` module patch pattern in the 5 tests
(consumed as-is — this change hardens consumption, it does not
re-architect the tests' clock injection); `startup_wiring`'s
starter call contract (frozen); pytest autouse fixture mechanics.

Non-goals: cache TTL / stale-while-revalidate logic; chain.py or
runner clock refactors (the issue's rejected alternative); making the
production process stop its own warmer (it should live to process
exit); #1272/#1274 red sets; the CI `Unit Tests (full)` gate shape
(#1182/#1254).

Minimal mergeable slice: all four pieces together — stop semantics
without the conftest teardown still leaks the thread in suites that
never import the guard test; next-defaults alone is the issue's named
"止痛不治病" anti-goal when shipped WITHOUT the root fix (as the only
delivery), but shipped together they are the defense-in-depth layer.

## 1. Production stop semantics (apps/api/display_cache.py)

- [x] 1.1 Module-level `_stop_event = threading.Event()` and
  `_warmer_thread: threading.Thread | None = None`. `_warm_loop`
  loops on `while not _stop_event.wait(DISPLAY_CATALOG_WARM_INTERVAL_SECONDS):`
  — reading the interval from the module global each iteration —
  with the existing body otherwise unchanged (hot-path snapshot
  under `_lock`, `asyncio.run(_replay_targets(...))`,
  swallow-and-continue). `start_display_catalog_warmer` clears the
  event and records the handle under `_lock` before starting; keeps
  returning the thread (or None when already started).
- [x] 1.2 NEW `stop_display_catalog_warmer(timeout: float = 5.0) ->
  bool`: set `_stop_event`; if a thread is recorded, `join(timeout)`;
  on successful join (or no thread) reset `_warmer_started = False`,
  `_warmer_thread = None`, clear the event, return `True`; on join
  timeout return `False` WITHOUT resetting state (a stuck thread
  must fail loudly — resetting would allow a silent second thread;
  the event stays set so the stuck thread still exits at its next
  wait). LOCK DISCIPLINE (deadlock trap): read the thread handle
  under a brief `_lock` acquisition, but NEVER hold `_lock` across
  the `join()` — the warm loop's replay path takes the same lock
  (`_store_value` / hot-path snapshot), so joining under it deadlocks
  until the timeout; reacquire the lock only after a successful join
  to reset state. Same-shape in-repo precedent to follow:
  `services/orchestrator/scheduler_lease.py:85-117` (`while not
  self._stop.wait(interval)` loop; stop() sets the event then joins
  OUTSIDE the lock). Docstring states this contract, including the
  accepted cascade: a thread stuck inside an ASGI replay (per-path
  httpx timeout is 120 s at `display_cache.py:133`) makes this and
  every subsequent teardown fail loudly BY DESIGN until the thread
  exits — the assert/docstring should name the thread so a reader
  does not misdiagnose stop() as broken.
- [x] 1.3 Idempotence + restartability: stop-when-never-started
  returns `True` and is a no-op; start after successful stop spawns
  a fresh thread (the guard test pins both).
- [x] 1.4 (round-1 verified CL-1) Start-failure state coherence:
  `thread.start()` raising (OS thread exhaustion) must restore
  module state under `_lock` (`_warmer_started = False`, handle
  `None`) and re-raise — otherwise every later stop() raises
  `RuntimeError: cannot join thread before it is started` and the
  teardown cascade never heals. Pinned by a guard test that patches
  the thread's `start()` to raise. (Round-1 verified CL-4 rides
  along: the loud-timeout guard's blocking fake uses an UNBOUNDED
  `release.wait()` — a 0.5 s self-release reopens a measured ~13%
  duty-cycle flake window; the `finally` always sets `release`, so
  no leak path.)

## 2. Test-suite hygiene

- [x] 2.1 `tests/conftest.py`: function-scoped autouse fixture that
  yields, then calls `stop_display_catalog_warmer()` and asserts the
  return is `True` — so any test that (transitively) built a
  display-readonly app cleans its thread before the next test, and a
  stuck thread fails THAT test loudly instead of poisoning
  neighbors (accepted cascade: while a stuck thread lives, EVERY
  subsequent teardown asserts — loud by design, documented in the
  assert message). After a successful stop the teardown also calls
  `clear_display_catalog_cache()` so `_hot_paths` recorded by one
  test cannot steer a later test's warmer replays (cross-test bleed).
  (No-op cost when nothing started: one Event check + one dict
  clear.)
- [x] 2.2 The 5 bare-next clock sites get the default
  `float("inf")`: `next(monotonic_values, float("inf"))` at
  `tests/test_orchestration_chain.py` :3976/:4013/:4053/:11790 and
  `tests/test_run_qhh_continuous.py:267` (rationale in
  must-preserve above — a pinned finite value would starve the
  deadline loops). Nothing else at those sites changes.
- [x] 2.3 Thread-leak guard tests in
  `tests/test_display_catalog_cache.py`:
  (a) lifecycle: monkeypatch
  `display_cache.DISPLAY_CATALOG_WARM_INTERVAL_SECONDS` small
  (e.g. 0.02), start against a throwaway FastAPI app, assert a live
  thread named `display-catalog-warmer` exists; stop → `True`;
  assert no thread of that name in `threading.enumerate()`; start
  again → new thread; stop again → `True` (restartability); finally
  ensure module state is reset for neighbors.
  (b) replay-liveness oracle (the loop BODY must still do its job —
  today nothing asserts the loop replays at all): monkeypatch
  `display_cache._replay_targets` with a fake that sets a
  `threading.Event` (`called.set()`), seed `_hot_paths` with one
  entry timestamped `time.monotonic()` taken at seed time (a stale
  0.0 falls outside `DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS =
  1800`, `display_cache.py:31`/filter `:116`, and never replays),
  interval 0.02, start, `assert called.wait(2.0)`; stop and heal
  module state before exit.
  (c) loud-timeout pin: monkeypatch `_replay_targets` with a fake
  that first signals entry (`entered.set()`) then blocks on an
  UNBOUNDED release-Event wait (delivered form per 1.4 — a ~0.5 s
  self-release reopens a measured flake window), seed `_hot_paths`
  (fresh-timestamped as in (b)), start with interval 0.02,
  `assert entered.wait(2.0)` — the stop call MUST land while the
  thread is inside the blocking replay, else the first
  `_stop_event.wait(0.02)` absorbs it and join succeeds — then
  `stop_display_catalog_warmer(timeout=0.05)` → assert returns
  `False`, `_warmer_started` still `True`, stop event set; then
  release the blocker and call stop again → `True`. MUST heal module
  state (final successful stop) before the test ends, or the autouse
  teardown assert reds this very test.

## 3. Spec + validation

- [x] 3.1 Spec delta: ADDED requirement in
  `real-integration-test-matrix` — background daemons started by app
  factories SHALL be stoppable/joinable with test-session hygiene, 3
  scenarios (stop-and-join semantics incl. loud timeout;
  no-leak-across-tests via autouse teardown; exhaustible patched
  clocks carry defaults).
- [x] 3.2 `openspec validate display-warmer-stop-semantics --strict
  --no-interactive` green.

## Evidence Floor

- [x] E1 Deterministic reproduction gate (the issue's Verification
  #1): recreate the `warmfast` plugin in the session scratchpad
  (pytest_configure sets
  `display_cache.DISPLAY_CATALOG_WARM_INTERVAL_SECONDS = 0.02`; no
  repo file changed), then
  `PYTHONPATH=<scratch> uv run pytest -q -p warmfast
  tests/test_runtime_mode.py
  "tests/test_orchestration_chain.py::test_round10_forecast_poll_timeout_stops_chain_as_reconciling"`
  → green post-fix. RED PROOF (pre-implementation, already
  captured): the same command was run on the PRE-fix tree (master
  `816245d6` + this openspec fixture only, BEFORE any touch of
  `display_cache.py`) and reproduced the red — `1 failed, 74 passed`
  with `FAILED
  tests/test_orchestration_chain.py::test_round10_forecast_poll_timeout_stops_chain_as_reconciling`.
  Red is judged by CAUSE, not exit code: the traceback must show
  `StopIteration` raised at a patched-clock consumption site (with
  the warmer thread as the co-consumer — e.g. the
  `PytestUnhandledThreadExceptionWarning` naming
  `display-catalog-warmer` / `_warm_loop`), not merely "1 failed".
  Had the pre-fix run come up green (timing miss), the protocol is:
  compress the interval further (0.005) and/or repeat up to 5 runs,
  recording each; a scratch-copy revert AFTER implementation is NOT
  an acceptable substitute for the pre-implementation capture.
- [x] E2 Thread-leak probe (issue Verification #2 + threadprobe): a
  scratchpad plugin printing `threading.enumerate()` at session
  finish over `uv run pytest -q tests/test_display_catalog_cache.py
  tests/test_runtime_mode.py tests/test_orchestration_chain.py` →
  no `display-catalog-warmer` in the live set (pre-fix probe shows
  it alive — pasted both).
- [x] E3 `uv run pytest -q tests/test_display_catalog_cache.py
  tests/test_runtime_mode.py tests/test_orchestration_chain.py
  tests/test_run_qhh_continuous.py` green (issue Verification #3),
  counts reported.
- [x] E4 Red proofs (discrimination), backup-copy + cmp restore:
  (i) guard test with `stop_display_catalog_warmer()` mutated to a
  no-op (e.g. never setting the event) → guard 2.3(a) reds (it
  really detects the leak); red-proof command scoped to
  `tests/test_display_catalog_cache.py` ONLY (under this mutation
  every warmer-starting test's teardown would burn 5 s and red —
  suite-wide runs are noise, not evidence);
  (ii) next-default load-bearing, TWO-ARM compound (post-fix the
  conftest teardown stops the thread before chain tests run, so a
  bare default-removal alone can NOT red — both arms disable the
  teardown, e.g. make the autouse fixture yield-only via a temporary
  mutation): arm A = teardown disabled + `:11790` default removed +
  warmfast combo → StopIteration red returns; arm B = teardown
  disabled + default kept + same command → no StopIteration (the
  default alone absorbs the over-consumption). Arms differ ONLY in
  the default;
  (iii) stop() mutated to reset state even on join timeout → guard
  2.3(c) reds (the loud-timeout pin is real);
  (iv) `_warm_loop` body mutated to skip replay (loop body →
  `continue` before the `_replay_targets` call) → guard 2.3(b) reds
  (the replay-liveness oracle is real).
- [x] E5 Full-suite stability (issue Verification #4): TWO
  consecutive `uv run pytest -q -m "not e2e and not grib and not
  integration"` runs with IDENTICAL red sets — expected: 0 failed
  both times (post-#1274 baseline).
- [x] E6 `uv run ruff check .` green; openspec strict green.
- [x] E7 Surface check: `git diff master...HEAD --name-only` = the 5
  named files + this openspec change, nothing else; frozen surfaces
  zero diff via the branch-scoped form.
- [x] E8 CI `Unit Tests` green on the PR head (Linux oracle;
  changed test files selected directly; no node-27).
