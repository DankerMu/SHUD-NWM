# Tasks: provider_atomic concurrency hang trap

## 1. Implementation

- [ ] 1.1 Rewrite the writer thread in
  `tests/test_scheduler_file_provider_refresh.py::test_provider_atomic_readers_observe_only_complete_old_or_new_json`
  to the house pattern used at `:796-818` and `:1929-1941`: an
  `errors: list[BaseException]` list, the 40-iteration loop inside `try`, an
  `except BaseException as error: errors.append(error)`, and a
  `finally: finished.set()`.
- [ ] 1.2 Bound the main-thread busy-loop with a deadline
  (`time.monotonic()` based, per the `tests/test_file_orchestration_journal.py:7894`
  precedent) so the loop cannot spin unbounded even if `finished` is never set.
  **Pin the value at >= 30s.** The semantics are a *hang backstop*, not a
  performance assertion: 40 real `atomic_replace_provider_bytes` calls each
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
  **Seam constraint (design D6):** `atomic_replace_provider_bytes` is imported
  directly at `:23`, so patching it on `provider_atomic_module` is INERT and
  would make this test pass vacuously. Patch an inner function the real call
  resolves at call time (precedent `:713-721`, `:689-699`) or drive a raising
  callable directly.
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
  an injected writer exception, run under a shell `timeout 30` so the hang is
  proven by the timeout's non-zero exit, **not** by waiting it out. Do not
  reproduce the hang unbounded; its cost is already measured (10 min, #1513).
- **E2 — Which existing assertion must go red?** *None.* This fix adds
  assertions and preserves all three substantive ones. The evidence is positive
  (new assertions fire on injected failure), not a red-to-green repair. If any
  pre-existing assertion goes red, the implementation is wrong.
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
- **E8 — Anti-vacuity.** The failure-injection test is shown to FAIL with the
  harness fix reverted. A test that only passes-when-present cannot distinguish
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

The class survey behind #1645 and the spec's exception list enumerated **all**
`threading.Barrier(` constructions (12) and `threading.Event()` constructions
(~20) under `tests/`, then classified each by the D7 hazard definition (main
thread blocked until a worker reaches a synchronization point; a bare
`thread.join()` is out of scope). This supersedes an earlier, narrower grep
whose apparent exhaustiveness was an artifact of the search terms.

Confirmed **conforming**, no action — recorded because two of them look
non-conforming at a glance:

- `tests/test_production_scheduler.py:42190` — the `Barrier(2)` has no timeout
  at the construction site, but the wait does: `_BarrierOrchestrator.orchestrate_cycle`
  at `:42128` calls `self._barrier.wait(timeout=5.0)`. Bounded. *This one is
  invisible to any proximity-based grep* — the timeout is ~60 lines from the
  construction, in a different class.
- `tests/test_display_coverage_parallel.py:32` — `threading.Barrier(2, timeout=2)`.
- `tests/test_scheduler_file_provider_refresh.py:788`, `:1926` — the house pattern.
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
