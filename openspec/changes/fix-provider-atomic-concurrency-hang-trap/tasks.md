# Tasks: provider_atomic concurrency hang trap

## 1. Implementation

- [ ] 1.1 Rewrite the writer thread in
  `tests/test_scheduler_file_provider_refresh.py::test_provider_atomic_readers_observe_only_complete_old_or_new_json`
  as follows. **From the house pattern** at `:796-818` and `:1929-1943`: an
  `errors: list[BaseException]` list, the 40-iteration loop inside `try`, and an
  `except BaseException as error: errors.append(error)`. **New, not house
  pattern** — neither house instance has a completion sentinel at all, both
  simply gate on a `Barrier` and join: a `finally: finished.set()`.
- [ ] 1.2 Bound the main-thread busy-loop with a deadline
  (`time.monotonic()` based, per the `tests/test_file_orchestration_journal.py:7894`
  precedent) so the loop cannot spin unbounded even if `finished` is never set.
  **Pin the value at >= 30s.** The semantics are a *hang backstop*, not a
  performance assertion. Note `time` is NOT imported in this file (`:3-13`); add
  `import time` in isort order — `ruff` has `I` enabled, so a misplacement fails
  E7. Rationale for the wide bound: 40 real `atomic_replace_provider_bytes` calls each
  fsync, and a loaded CI runner must never trip this. A tight value would make
  the new `assert not thread.is_alive()` (1.4) a fresh flake source — this fix
  must not introduce one.
- [ ] 1.3 Replace the bare `thread.join()` with a bounded
  `thread.join(timeout=...)`, using the same >= 30s hang-backstop value as 1.2.
- [ ] 1.4 Add `assert not errors, errors` and `assert not thread.is_alive()`,
  **before** the three existing substantive assertions (design D3).
- [ ] 1.5 Update the `:827-830` comment: after this change the test fails
  rather than hangs, so the existing text is false. State what the seed pins
  (`SHARED_PROVIDER_MODE`, #1513) and that the harness is now fail-fast.
- [ ] 1.6 **Prohibition:** do not weaken the oracle. The 40 iterations, the real
  `atomic_replace_provider_bytes` call, the `write_provider_destination` seed,
  and all three substantive assertions stay as they are (MP1/MP2/MP3).
- [ ] 1.7 **Prohibition:** do not edit any production module, any other test
  file, `pyproject.toml`, or CI config (design D4, MP4).

## 2. Tests

- [ ] 2.1 The rewritten
  `test_provider_atomic_readers_observe_only_complete_old_or_new_json` still
  passes, with its three substantive assertions unchanged.
- [ ] 2.2 New failure-injection test: with `atomic_replace_provider_bytes`
  patched to raise, the same harness shape surfaces a **clean, bounded
  failure** — the assertion fires, the exception identity is visible in the
  failure, and the call returns within the deadline rather than hanging.
  **Required shape: one shared harness.** Factor the harness into a single
  module-level helper parameterized by the worker body, and have the real test
  (2.1) and the injection tests (2.2/2.3c) all call it. Inline duplication is
  rejected: a copied harness drifts, and then deleting the `finally` or the
  `errors` assertion from the real test leaves the injection test green — the
  guard would stop guarding the thing it exists to guard. The shared helper must
  not disturb MP2 (the real test still makes 40 real calls).
  **Seam constraint (design D6):** do NOT patch. `atomic_replace_provider_bytes`
  is imported directly at `:23`, so patching it on `provider_atomic_module` is
  INERT (vacuous pass); and patching an inner function instead converts the
  injected exception into `ProviderAtomicError` (see `:725`), defeating 2.3.
  Pass the worker body to the shared helper as a callable. If any case is
  nevertheless patch-based, it MUST assert the injected callable actually ran.
- [ ] 2.3 The failure-injection test must prove the *ordering* of D3: when the
  writer raises on the first iteration, the surfaced failure names the writer
  exception, not an empty-`observed` symptom.
- [ ] 2.3c **Blocked-worker deadline test** (covers the spec's "A blocked worker
  is caught by the loop deadline" scenario, which 2.2/2.3 do NOT reach). 2.2/2.3
  inject an *exception*, which runs the `finally` and sets the sentinel — so the
  deadline never fires and 1.2 would be untested. Add a case where the worker
  **blocks instead of raising** (e.g. waits on a test-controlled `Event`), so
  the `finally` is never reached and the sentinel is never set; assert the
  spin-loop exits at its deadline and the failure is attributed to the timeout.
  The test must release the blocking Event and join the worker before returning
  so no thread outlives it. Use a short, explicitly-passed deadline for this
  case rather than the 30s production value, so the test stays fast.
- [ ] 2.3b **Anti-vacuity:** demonstrate the injection test FAILS when the
  harness fix is absent, not merely that it passes when present (design D6).
- [ ] 2.4 Whole-file green: `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`.

## 3. Verification matrix

| Check | Command | Where |
|---|---|---|
| Target suite | `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` | local |
| Wall-clock sanity | same, timed — must finish in seconds, not minutes | local |
| Lint | `uv run ruff check .` | local |
| Spec | `openspec validate fix-provider-atomic-concurrency-hang-trap --strict --no-interactive` | local |
| Content-coupling | grep meta-guards/selector tooling that read this file as text (design D5 leg 2) | local |
| Backend oracle | targeted pytest on node-27 if CI's selection is non-trivial | node-27 |

## 4. Evidence Floor

- **E1 — Red-before, bounded.** Demonstrate the trap on the *pre-fix* code with
  an injected writer exception, under an **external** time bound, so the hang is
  proven by the bound firing rather than by waiting it out. Do not reproduce the
  hang unbounded; its cost is already measured (10 min, #1513).
  **Do not use `timeout 30`** — neither `timeout` nor `gtimeout` exists on this
  machine (macOS, no coreutils; verified this session). Use a Python subprocess
  bound instead:
  `subprocess.Popen([...]); p.communicate(timeout=30)` catching
  `subprocess.TimeoutExpired`. Note `uv run` spawns a child, so `p.kill()` may
  not reap the inner pytest — kill the process group, or `pkill -f` the probe
  path afterwards and confirm it is gone.
- **E2 — Which existing assertion must go red?** *None.* This fix adds
  assertions and preserves all three substantive ones. The evidence is positive
  (new assertions fire on injected failure), not a red-to-green repair. If any
  pre-existing assertion goes red, the implementation is wrong.
  **Refinement:** the risk to E2 is no longer design, it is refactor slip. Task
  2.2 now moves the harness into a shared helper, which is a much larger edit
  than prepending assertions. The three substantive assertions must still
  observe the same thing they observe today: `observed` must still be filled by
  the same main-thread polling loop, reading the same destination, while the
  same 40 real writes run (MP1/MP2 unchanged).
- **E3 — Green-after.** Target suite passes, with wall-clock time recorded.
- **E4 — Silent-pass is closed.** Show that the injected-failure case now
  *fails* rather than passing with a `PytestUnhandledThreadExceptionWarning`
  (design D2 — this is the half the issue's proposed fix misses).
- **E5 — Diff boundary.** `git diff --stat` shows exactly one file changed:
  `tests/test_scheduler_file_provider_refresh.py`, plus the
  `openspec/changes/**` fixture.
- **E6 — Citations by anchor content.** Every `file:line` cited in the PR body
  and fixture is verified by grepping for the anchor *text* at that line, not
  by a line-number range check. Range sweeps are a known false negative once a
  change shifts its own cited lines.
- **E7 — Lint + spec clean.**
- **E8 — Anti-vacuity.** Revert each half of the harness fix separately and
  record what is observed. The two halves do not both produce a literal pytest
  FAIL, so name the observable per half rather than asserting "it fails":
  - **(i) remove `finally: finished.set()`, keep the loop deadline.** Observable:
    the spin-loop exits at its deadline and `assert not errors` reports the
    injected exception — a clean FAIL. If the deadline is *also* removed the
    observable becomes a hang instead, which is E1's instrument, not this one.
  - **(ii) remove the `errors` capture and its assertion.** Observable: the
    injection test's expected failure never materialises and the run reports
    `1 passed, 1 warning` with `PytestUnhandledThreadExceptionWarning` — the
    silent-pass signature from E4, not a FAIL.
  Both observables must be recorded. A single "it went red" line does not
  distinguish these and is not acceptable evidence. A test that only passes-when-present cannot distinguish
  "harness works" from "patch was inert" (design D6).

## 5. Report, don't fix — filed, do not fix in this PR

All three are already filed; the implementer must not touch them.

- **#1645** — `tests/test_gateway_reconcile.py:3574` and `:10028`: same failure
  class, weaker trigger. Both reach an unbounded `threading.Barrier` *after* a
  constructor that can raise (`_StoreRepo(PipelineStore(Session(engine)))` and
  `FileOrchestrationJournalRepository(...)` respectively), with an unbounded
  `join()` / `ThreadPoolExecutor` exit. Under the narrowed trigger (design D7)
  these are *adjacent* to the requirement rather than governed by it — a Barrier
  strand is not a spin-wait — so the spec routes them to #1645 explicitly
  instead of grandfathering them.
- **#1646** — the repo-wide guard decision: `filterwarnings =
  ["error::pytest.PytestUnhandledThreadExceptionWarning"]` (cheap, closes the
  silent-pass half) versus `pytest-timeout` (closes the hang half, but needs
  #1632's marker-lane timings first). Design D4.
- **#1644** — unrelated, found while validating this branch's CI: `OpenAPI
  Validate` fails on master with 164 redocly errors (`nullable: true` in a
  `3.1.0` spec), long masked by the `openapi` path filter. Nothing to do with
  this change; recorded so it is not rediscovered as a regression here.

### Survey method and its bounds

Two sweeps were run, because they answer different questions and neither alone
is sufficient:

1. **Barrier/Event enumeration** — all 12 `threading.Barrier(` and ~20
   `threading.Event()` constructions under `tests/`. This finds the
   *synchronization-strand* class (#1645).
2. **`while`-loop enumeration** — all 33 `while` loops under `tests/`, each
   classified by whether its exit condition depends on a worker thread. **This
   is the only sweep that can find the governed set**, and sweep 1 structurally
   cannot: the sentinels at `tests/test_production_scheduler.py:44138` and
   `tests/test_node27_timeseries_compression_supervisor.py:974` are an integer
   inside a lock file and a filesystem path respectively — there is no `Event`
   object to enumerate. An earlier draft of this section claimed exhaustiveness
   on the strength of sweep 1 alone; that claim was unsupported.

Sweep 2 result: 33 `while` loops, of which most are docstring prose, pure
computation (`while stack:`, `while index < length:`), or shell strings inside
subprocess payloads. Four are main-thread polls on a worker-set sentinel: the
target test, plus the three routed to #1648.

Known caliper limits of the sweep, stated so the next reader can widen it:
it enumerated `threading.Event()` and `threading.Barrier(` textually, so it
does **not** cover `multiprocessing` synchronization (e.g.
`tests/test_file_orchestration_journal.py:2702` uses `context.Event()`),
sentinels obtained from a factory or passed in as parameters, or spin-waits on
plain attributes. The separate spin-wait sweep in design D7 covers the
main-thread side by loop shape rather than by sentinel type, which is what makes
the "one governed site" claim robust to this gap.

**Outside the trigger** — not main-thread polls on a worker-set sentinel — so
these were never evaluated against (a)(b)(c) and no conformance is claimed for
them:

- `tests/test_production_scheduler.py:42190` — the `Barrier(2)` has no timeout
  at the construction site, but the wait does: `_BarrierOrchestrator.orchestrate_cycle`
  at `:42128` calls `self._barrier.wait(timeout=5.0)`. Bounded. *This one is
  invisible to any proximity-based grep* — the timeout is ~60 lines from the
  construction, in a different class.
- `tests/test_display_coverage_parallel.py:32` — `threading.Barrier(2, timeout=2)`.
**Adjacent hazard, routed to #1645** — these were previously mislabelled here as
a conforming control group:

- `tests/test_scheduler_file_provider_refresh.py:788` (`Barrier(20)`, unbounded
  wait at `:798`) and `:1926` (`Barrier(40)`, unbounded wait at `:1931`). The
  main thread's wait IS bounded, and the assertions DO fire — but stranded
  non-daemon peers block `threading._shutdown()`, so the process never exits.
  Reproduced: a reduced replica reports failure in ~2s then hangs past a 25s
  bound. Risk window is narrow (each `wait()` is the first statement in its
  `try`) but not empty. Not fixed here: correcting them changes barrier
  semantics, outside this change's blast radius.
- `tests/test_file_orchestration_journal.py:7894` — deadline-bounded loop.
- `tests/test_gateway_reconcile.py:5231` — daemon thread with `try/finally`. It
  *is* awaited (`FakeProcess.wait()` at `:5253` calls `self.thread.join(timeout)`
  at `:5254` and sets `reaped` from `is_alive()` at `:5255`), so "not awaited" would be the wrong
  reason; it is out of the class because that join is bounded by the timeout the
  production caller passes.
- `tests/test_display_catalog_cache.py:213` — daemon thread plus a documented
  unconditional `finally` release.
- `tests/test_scheduler_generation.py:1196` — bare `join()`, no synchronization
  point; out of scope by D7, not a violation.
