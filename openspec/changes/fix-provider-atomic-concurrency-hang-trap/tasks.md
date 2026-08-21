# Tasks: provider_atomic concurrency hang trap

## 0. Expanded fixture and risk closure

Fixture level: **expanded** (corrected from compact because concurrency/shared
state/ordering is a mandatory expanded trigger). Repair intensity: medium.

Selected packs and evidence:

- **Concurrency / shared state / ordering** — tasks 1.1-1.4, 2.2-2.3d: release,
  ordering, bounds, cleanup, and whole-process termination.
- **Resource limits / large input / discovery** — tasks 1.2-1.3, 2.3c-2.3d,
  E1/E3: bounded spin, join, subprocess, and suite wall clock; large input and
  discovery are absent.
- **Legacy compatibility / examples** — MP1-MP4 and E2: all existing assertions,
  iterations, calls, seed behavior, and selectors remain compatible.
- **Error handling / rollback / partial outputs** — tasks 2.2-2.3d and E4/E8:
  raising and blocked workers produce bounded attributed failures; no production
  rollback semantics change.

Not selected: Public API/CLI/script entry; Config/project setup; File IO/path
safety/overwrite (real file calls are preserved oracle evidence, not changed
production semantics); Schema/columns/units/field names; Auth/permissions/
secrets; Release/packaging/dependencies; Documentation/migration notes. Each has
no touched runtime or consumer surface. All eight NHMS domain packs are not selected: no geospatial,
hydro-met window, SHUD numerical, PostGIS/Timescale, Slurm, external-provider,
run-manifest/QC, or published-display identity behavior changes.

## 1. Implementation

- [x] 1.1 Rewrite the writer thread in
  `tests/test_scheduler_file_provider_refresh.py::test_provider_atomic_readers_observe_only_complete_old_or_new_json`
  as follows. **From the house pattern** at `:796-818` and `:1929-1943`: an
  `errors: list[BaseException]` list, the 40-iteration loop inside `try`, and an
  `except BaseException as error: errors.append(error)`. **New, not house
  pattern** — neither house instance has a completion sentinel at all, both
  simply gate on a `Barrier` and join: a `finally: finished.set()`.
- [x] 1.2 Bound the main-thread busy-loop with a deadline
  (`time.monotonic()` based, per the `tests/test_file_orchestration_journal.py:7894`
  precedent) so the loop cannot spin unbounded even if `finished` is never set.
  **Pin the value at >= 30s.** The semantics are a *hang backstop*, not a
  performance assertion. Note `time` is NOT imported in this file (`:3-13`); add
  `import time` in isort order — `ruff` has `I` enabled, so a misplacement fails
  E7. Rationale for the wide bound: 40 real `atomic_replace_provider_bytes` calls each
  fsync, and a loaded CI runner must never trip this. A tight value would make
  the new `assert not thread.is_alive()` (1.4) a fresh flake source — this fix
  must not introduce one.
- [x] 1.3 Replace the bare `thread.join()` with a bounded
  `thread.join(timeout=...)`, using the same >= 30s hang-backstop value as 1.2.
- [x] 1.3b Start the harness-owned worker with `daemon=True`. This is the
  last-resort guarantee that a permanently blocked writer cannot strand
  `threading._shutdown()` after the deadline assertion (design D8). Do not use
  daemon status as a cleanup substitute: every controllably blocked test still
  releases and joins its worker.
- [x] 1.4 Add `assert not errors, errors` and `assert not thread.is_alive()`,
  **before** the three existing substantive assertions (design D3).
- [x] 1.5 Update the `:827-830` comment: after this change the test fails
  rather than hangs, so the existing text is false. State what the seed pins
  (`SHARED_PROVIDER_MODE`, #1513) and that the harness is now fail-fast.
- [x] 1.6 **Prohibition:** do not weaken the oracle. The 40 iterations, the real
  `atomic_replace_provider_bytes` call, the `write_provider_destination` seed,
  and all three substantive assertions stay as they are (MP1/MP2/MP3).
- [x] 1.7 **Prohibition:** do not edit any production module, any other test
  file, `pyproject.toml`, or CI config (design D4, MP4).

## 2. Tests

- [x] 2.1 The rewritten
  `test_provider_atomic_readers_observe_only_complete_old_or_new_json` still
  passes, with its three substantive assertions unchanged.
- [x] 2.2 New failure-injection test: with a callable writer body that raises,
  the same harness shape surfaces a **clean, bounded failure** — the assertion
  fires, the exception identity is visible in the
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
- [x] 2.3 The failure-injection test must prove the *ordering* of D3: when the
  writer raises on the first iteration, the surfaced failure names the writer
  exception, not an empty-`observed` symptom.
- [x] 2.3c **Blocked-worker deadline test** (covers the spec's "A blocked worker
  is caught by the loop deadline" scenario, which 2.2/2.3 do NOT reach). 2.2/2.3
  inject an *exception*, which runs the `finally` and sets the sentinel — so the
  deadline never fires and 1.2 would be untested. Add a case where the worker
  **blocks instead of raising** (e.g. waits on a test-controlled `Event`), so
  the `finally` is never reached and the sentinel is never set; assert the
  spin-loop exits at its deadline and the failure is attributed to the timeout.
  The test must release the blocking Event and join the worker before returning
  so no thread outlives it. Use a short, explicitly-passed deadline for this
  case rather than the 30s production value, so the test stays fast.
- [x] 2.3d **Whole-run terminability test.** In a bounded subprocess, import the
  real shared harness, pass a permanently blocked writer and a short deadline,
  catch the expected deadline `AssertionError`, and let the process exit without
  releasing that writer. The subprocess must exit before its external timeout.
  The mutant changing `daemon=True` back to the default non-daemon setting must
  hit the external timeout after printing/catching the assertion. Kill the
  process group on timeout and prove no probe process remains.
- [x] 2.3b **Anti-vacuity:** demonstrate the injection test FAILS when the
  harness fix is absent, not merely that it passes when present (design D6).
- [x] 2.4 Whole-file green: `uv run pytest -q tests/test_scheduler_file_provider_refresh.py`.

## 3. Verification matrix

| Check | Command | Where |
|---|---|---|
| Target suite | `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` | local |
| Wall-clock sanity | same, timed — must finish in seconds, not minutes | local |
| Lint | `uv run ruff check .` | local |
| Spec | `openspec validate fix-provider-atomic-concurrency-hang-trap --strict --no-interactive` | local |
| Content-coupling | grep meta-guards/selector tooling that read this file as text (design D5 leg 2) | local |
| Permanent-block process exit | focused bounded subprocess test from 2.3d; mutant `daemon=False` must externally time out | local |
| Backend oracle | targeted pytest on node-27 only if CI selection is non-trivial or local/CI results diverge; this test-only DB-free change otherwise uses local + CI targeted pytest | node-27 conditional |

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
- **E8 — Anti-vacuity mutation matrix.** Mutate each independent obligation and
  record its distinct observable; do not call a full pre-fix rollback a
  half-revert:
  - **Release mutant:** remove `finally: finished.set()` and invoke the helper
    from a bounded probe with a no-op writer that returns normally plus a short
    deadline. Correct code returns successfully; the mutant must FAIL on
    `spin-wait deadline`. The existing blocked-worker test cannot prove this —
    it expects that same deadline failure with or without the `finally`.
  - **Attribution mutant:** remove the catch-all/errors assertion but keep
    `finally` and both bounds. Run the shipped raising-writer injection test.
    It must report `1 failed, 1 warning`: the failure names the downstream
    empty-observation symptom while `InjectedWriterFailure` appears only in
    `PytestUnhandledThreadExceptionWarning`. A standalone minimal probe can be
    `1 passed, 1 warning`, but that is not the shipped test's observable.
  - **Deadline mutant:** remove the spin-loop deadline. The blocked-worker case
    must hit an external process timeout instead of a pytest assertion.
  - **Run-termination mutant:** change the harness worker from daemon to
    non-daemon. The permanent-block subprocess must catch/print the deadline
    assertion and still hit the parent's external timeout during interpreter
    shutdown.
  Every mutant must be run under an external bound and restored cleanly. These
  observables jointly prove release, attribution, deadline delivery, and whole-
  process termination; a generic "it went red" is insufficient.

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

Two sweeps were run because they find different hazards:

1. **Barrier/Event enumeration** — all 12 `threading.Barrier(` and roughly 20
   `threading.Event()` constructions under `tests/`. This finds the separate
   synchronization-strand class routed to #1645, but cannot find integer/path
   state polls.
2. **`while`-loop enumeration** — all 33 `while` loops under `tests/`, classified
   first by worker/process dependency and then by the requirement's three-part
   ownership trigger.

Sweep 2 finds four main-thread polls whose observed value is affected by a
worker thread: the target plus the three #1648 sites. Only the target is governed
here because only it polls a dedicated completion sentinel owned solely by the
test harness. The #1648 sites poll production state under assertion and remain
an adjacent diagnostic-quality follow-up, not exceptions to this requirement.
The two production-scheduler ready-file loops poll subprocess output and are
outside the worker-harness trigger.

Known caliper limits: the Barrier/Event sweep is textual and does not enumerate
`multiprocessing` synchronization, factory-supplied objects, or plain-attribute
state. The independent all-`while` sweep covers the main-thread polling side by
loop shape, then applies the ownership trigger; neither sweep is presented as a
proof that unrelated concurrency tests are safe.

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
