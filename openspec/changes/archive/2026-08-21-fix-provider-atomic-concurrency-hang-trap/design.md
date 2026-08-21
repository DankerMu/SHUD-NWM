# Design: provider_atomic concurrency hang trap

## Citation frame of reference

Every `tests/test_scheduler_file_provider_refresh.py:NNN` citation in this
fixture is against the **pre-change baseline** — `master` at `b8322301`, the
commit this change branches from — because the fixture describes the edit to be
made. Verify them with `git show master:<path>`, not against the working tree:
the implementation moves these lines (the change is roughly +160 lines in that
file), so a working-tree check will report false drift. Citations to every other
file are frame-independent, since no other file is modified.

## Risk triage

- **Issue type:** bugfix / test harness.
- **Project profile:** NHMS.
- **Blast radius:** medium — one test module, but a shared concurrency harness
  and its archived test-matrix requirement become reusable contracts.
- **Fixture tier: `expanded`.** The original `compact` classification was
  invalid: concurrency/shared-state ordering is a mandatory expanded trigger,
  even though no production module, API, schema, or migration is touched.
- **Repair intensity:** medium. The helper is shared only inside one test module;
  it is not a production shared-helper root and has no high-risk publish/auth/path
  boundary.
- **Upstream suggested level:** absent. #1633 was filed by `issue-scribe` from
  #1513 / PR #1625 and carries neither `Suggested fixture level` nor `Minimal
  mergeable slice`.

### Core risk packs considered

- **Public API / CLI / script entry: not selected** — the helper is private to a
  pytest module; no shipped entrypoint changes.
- **Config / project setup: not selected** — no runtime configuration or setup
  contract changes.
- **File IO / path safety / overwrite: not selected** — production path trust,
  overwrite, and atomic-write semantics are not changed. The existing real file
  operations must remain in the test as oracle-integrity/legacy evidence, but
  preserving a consumer does not make its untouched production pack selected.
- **Schema / columns / units / field names: not selected** — JSON payload shape
  and `generation` assertions are unchanged; no schema is edited.
- **Auth / permissions / secrets: not selected** — no auth, credential, or
  permission boundary changes; #1513's mode-pinned seed is only preserved.
- **Concurrency / shared state / ordering: selected** — completion release,
  deadline, thread lifetime, exception attribution, and assertion order are the
  governing behavior.
- **Resource limits / large input / discovery: selected** — spin wait, join, and
  an external run-termination probe need explicit time bounds; no large-input or
  discovery surface exists.
- **Legacy compatibility / examples: selected** — the existing 40 real writes,
  seed path, observations, and three substantive assertions are compatibility
  constraints.
- **Error handling / rollback / partial outputs: selected** — raising and
  permanently blocked worker paths must fail with the cause and must not leave
  the test process alive; production rollback behavior is unchanged.
- **Release / packaging / dependency compatibility: not selected** — no package,
  dependency, or release metadata changes.
- **Documentation / migration notes: not selected** — the stale in-test comment
  and OpenSpec delta are updated, but there is no user migration or runbook.

### NHMS domain risk packs considered

All are **not selected** because this is a DB-free, provider-fetch-free,
test-local harness change: Geospatial / CRS / basin geometry; Hydro-met time
series / forcing windows; SHUD numerical runtime / conservation / NaN; PostGIS /
TimescaleDB domain behavior; Slurm production lifecycle / mock-vs-real parity;
External hydro-met providers / snapshot reproducibility; Run manifest / QC
provenance; Published NHMS artifacts / display identity. The filename
`manifest-last.json` and `provider_atomic` helper do not make this a manifest
schema, external-provider, or publication change.

## Must-preserve behavior

- **MP1** The test's substantive oracle is unchanged: readers observe only
  complete `old` or `new` JSON, never a torn write. All three original
  assertions (`observed` non-empty, `set(observed) <= {old, new}`, every
  observation parses with a `generation` in `{old, new}`) survive verbatim.
- **MP2** The writer still performs 40 real `atomic_replace_provider_bytes`
  calls alternating `old`/`new`. Reducing the iteration count or replacing the
  real call with a double would weaken the race window and is prohibited.
- **MP3** The seed still goes through `write_provider_destination`, not bare
  `write_text` — that is #1513's fix and must not regress.
- **MP4** No production module is edited.

## Seams under test

- `run_spin_wait_writer_harness(writer_body, observe, ...)` — the public seam
  within this test module. The real case supplies the 40-call writer; injected
  cases supply raising and blocked callables. No production function is patched.
- `threading.Event` / `Thread.join(timeout=)` / daemon worker lifetime — the
  release, bounded-wait, cleanup, and whole-process-termination seam.
- A bounded pytest subprocess invoking the shared harness — the proof that a
  permanently blocked writer cannot strand interpreter shutdown.

## D1 — Reuse the safe house-pattern pieces; do not copy its Barrier hazard

`tests/test_scheduler_file_provider_refresh.py` already contains the useful
exception-capture and bounded-join pieces twice:

```python
errors: list[BaseException] = []

def contender() -> None:
    try:
        ...
    except BaseException as error:  # pragma: no cover - asserted below
        errors.append(error)

for thread in threads:
    thread.join(timeout=5)

assert not errors
assert all(not thread.is_alive() for thread in threads)
```

(`:796-818`, and again at `:1929-1943`.) The target test at `:823-848` is the
odd one out in its own file.

Scope of the borrowing, stated precisely: the `errors` list, catch-all,
bounded join, and closing error/liveness assertions are reused. The
`finally`-set sentinel, spin-loop deadline, and daemon worker are new. Neither
nearby instance has a completion sentinel, and both gate on an unbounded
`Barrier`; #1645 records why that part is unsafe. Calling either nearby test a
complete house pattern would overstate it.

## D2 — The issue's proposed fix is insufficient; recorded, not silently absorbed

#1633 proposes `finally: finished.set()` and claims this "把任何未来的失败从挂死
转成干净的失败" (turns any future failure from a hang into a clean failure).
**That claim is false**, and the correction is load-bearing for this design.

`threading.Thread` does not propagate exceptions to the joining thread. pytest
reports an uncaught worker exception only as
`PytestUnhandledThreadExceptionWarning`, and `pyproject.toml`
`[tool.pytest.ini_options]` declares no `filterwarnings`, so that warning is
non-fatal. A bare `try/finally` therefore loses the cause: a minimal probe can
silently pass, while the real target can either pass after collecting an earlier
observation or fail on a downstream empty/data assertion. Neither is the clean,
attributed failure #1633 requires.

Verified empirically before designing, with a standalone probe reproducing the
proposed fix exactly (writer raises, `finally: finished.set()`, nothing else):

```
1 passed, 1 warning in 0.00s
  PytestUnhandledThreadExceptionWarning: Exception in thread Thread-1 (writer)
  RuntimeError: provider_destination_access_invalid
```

So the `finally` is necessary but not sufficient. The `errors` list plus
`assert not errors` is what actually produces the clean failure the issue asked
for. Implementation must include both halves.

## D3 — Assertion ordering is load-bearing

`assert not errors, errors` MUST come **before** `assert observed`. If the
writer raises on iteration 0, `observed` may be empty, and an `observed`-first
ordering reports `assert observed` — the downstream symptom — instead of the
actual `ProviderAtomicError`. This is precisely the diagnostic-quality failure
that made #1633 expensive in the first place, so it is pinned as a requirement
rather than left to taste.

## D4 — Systemic guards are out of scope (considered and routed)

The issue's "顺带值得考虑" raises two repo-wide options. Both are rejected *for
this change* and routed to a follow-up issue:

- **`pytest-timeout`**: adds a dependency and a global time bound over every
  suite, including the node-27 real-DB and `grib`/`e2e` marker lanes whose
  legitimate runtimes are long and not measured here (that measurement is
  #1632's job). A global timeout tuned without those numbers risks killing
  healthy slow tests — breaking working suites to fix a test-hygiene bug.
- **`filterwarnings = ["error::pytest.PytestUnhandledThreadExceptionWarning"]`**:
  strictly cheaper — zero dependencies — and would kill the silent-pass half of
  this class repo-wide. But it makes every currently-warning thread exception in
  the tree fail, across all 18 thread-using test files, which requires a
  full-suite validation to adopt safely. That is its own change, not a rider.

Filing a decision satisfies "worth considering"; scope creep on a size-S fix
does not.

## D5 — Blast radius

Three legs, per the discipline banked in #1119:

1. **Reverse-import closure:** none. `tests/test_scheduler_file_provider_refresh.py`
   is a leaf test module; nothing imports it.
2. **Content-coupling (files read as data, invisible to an import graph)** —
   swept, result below. Every consumer found is keyed on *paths and selector
   rules*, never on this file's body, so a test-body edit is inert to all of
   them:
   - `.large-file-guard.json:35` — this file is **already** in the exclude
     list. No hook block is expected (contrast #1119, where the missing entry
     blocked the commit).
   - `scripts/select_ci_tests.py:290`, `:574`, `:847`, `:851` — routing rules
     naming this file, keyed on *other* paths (`tests/provider_mode_helpers.py`
     and friends).
   - `tests/test_select_ci_tests.py:243`, `:376`, `:389`, `:402` — assert this
     file gets selected; again keyed on selector rules, not on its body.
   - `openspec/specs/ci-contract-baseline/spec.md:547` — names the file as
     expected selector output.
   - `tests/test_safe_fs.py:23`, `:177`, `tests/test_readonly_db_validation.py:90`,
     `tests/test_publish_scheduler_file_registry.py:1601` — prose comments
     referencing this file. The last one names a *specific* test
     (`::test_full_runner_refresh_lock_is_held_during_precommit_gate`), which is
     NOT the test being changed.
   - AST meta-guards under `tests/` were also searched. The directly relevant
     example, `tests/test_timescale_write_guard_wire_site_invariant.py`, targets
     `workers/forcing_producer/store.py`; none parses or text-couples to this
     test module's body.
   **Derived constraint:** do not rename the target test function, and do not
   touch selector rules.
3. **Config/collection coupling:** `pyproject.toml` testpaths, CI path filters.
   Unchanged by a test-body edit, but the `backend` filter does match
   `tests/**`, so CI's targeted lane will select this file.

## D6 — The failure-injection seam, and the vacuity trap in it

`tests/test_scheduler_file_provider_refresh.py:23` imports the symbol directly:

```python
from packages.common.provider_atomic import (
    atomic_replace_provider_bytes,
    ...
)
```

The name is therefore **bound into the test module at import time**. A
`monkeypatch.setattr(provider_atomic_module, "atomic_replace_provider_bytes", raiser)`
would rebind the attribute on the package module while the test keeps calling
its own already-bound reference — the patch is **inert**, no exception is
raised, the harness is never exercised, and tasks 2.2/2.3 pass **vacuously**.
A green test that proves nothing is a worse outcome than the bug.

**Primary seam — do not patch at all.** Factor the harness into a module-level
helper whose worker body is a **callable parameter**: the real test passes the
body doing 40 real `atomic_replace_provider_bytes` calls; the injection tests
pass a body that raises, or one that blocks. This is the chosen seam because it
solves three problems at once — it removes the vacuity trap by construction (no
patch, so no inert patch), it preserves the injected exception's identity, and
it satisfies the shared-harness requirement that stops a duplicated harness from
drifting green.

**Why the obvious alternative is wrong.** Patching an inner dependency the real
call resolves at call time (`:713`/`:721` patch
`provider_atomic_module.atomic_write_bytes_no_follow`; `:689`/`:699` patch
`read_bytes_limited_no_follow`) does make the call fail — but
`atomic_replace_provider_bytes` **catches inner failures and converts them into
`ProviderAtomicError`**. `:725` in this same file asserts exactly that:
`error_info.value.reason == "provider_restored_previous"`. So the injected
exception never reaches the harness by its own identity, which contradicts the
requirement that the surfaced failure name the worker's exception.

**If any case does end up patch-based**, that case MUST additionally assert the
injected callable was actually invoked (a call counter). Otherwise an inert
patch passes silently in a second way — the same vacuity in different clothing.

**Prohibited:** patching the module attribute for the directly-imported name and
assuming it takes effect.

**Required anti-vacuity evidence:** the injection test must be demonstrated to
**fail** when the harness fix is absent, not merely to pass when it is present.
Absent that demonstration, vacuity is undetectable.

Incidental: `time` is **not** currently imported in this file (imports at
`:1-23`). The D-1 deadline needs it added in isort order — `ruff check .` is the
gate.

## D7 — Scope: test-owned completion harness, not every production-state poll

Earlier drafts tried "any completion sentinel", then "blocking dependence",
then any worker-set value polled by the main thread. All are too broad. They mix
three different things: a test harness's own completion protocol, production
state being asserted, and inter-worker synchronization. Those have different
owners and failure contracts.

This requirement governs only a **test-owned spin-wait completion harness**:

1. the test itself starts the worker;
2. the harness owns a dedicated completion sentinel whose sole purpose is to
   release the main thread's polling loop; and
3. the main thread polls that sentinel before joining and checking worker output.

The target's `finished = threading.Event()` is exactly that shape. A lock-file
heartbeat counter, production `lost` flag, terminal-intent path, subprocess
ready file, or inter-worker `Barrier` is not a dedicated harness-completion
sentinel merely because a test polls it.

Explicitly outside this requirement:

- bounded `event.wait(timeout=N)`, which is not a polling harness;
- bare joins whose workers share no other synchronization point;
- polls of production state that is itself under assertion; and
- inter-worker synchronization such as `Barrier`, tracked separately by #1645.

### Survey of the current trigger

The repository-wide `while` sweep found exactly one current instance of the
three-part trigger: the target at baseline
`tests/test_scheduler_file_provider_refresh.py:823-848`. This change must make
that one harness conform. This population statement does **not** claim that
unrelated polling or Barrier tests are safe.

The nearby loops are classified rather than silently ignored:

- `tests/test_production_scheduler.py:44138` and `:44149` poll the production
  `_LeaseHeartbeat`'s persisted `heartbeat_seq` and `lost` state. In production,
  `_LeaseHeartbeat._run` catches `Exception` from renew and maps it to
  `lost = True`; describing both loops as unhandled-thread-exception paths would
  itself be false.
- `tests/test_node27_timeseries_compression_supervisor.py:974` polls a production
  terminal-intent path created during finalization, not a dedicated completion
  sentinel. Its finalizer-thread diagnostic quality remains tracked by #1648,
  but it is not an exception to this narrower requirement.
- `tests/test_production_scheduler.py:21748` and `:21766` poll subprocess-created
  ready files; `tests/test_shud_runtime.py:6006` is inside the subprocess body.
- `tests/test_file_orchestration_journal.py:7894` and
  `tests/test_gateway_reconcile.py:5238` are worker-body loops, not main-thread
  completion polling.
- the unbounded Barrier sites in this file and `test_gateway_reconcile.py` are
  the separate stranded-peer class routed to #1645.

#1648 therefore remains an adjacent follow-up about symptom-first assertions;
it is no longer described as three violations of this requirement. #1646 owns
shape-independent pytest warning/timeout backstops.

## D8 — The deadline must terminate the process, not only deliver an assertion

Python cannot safely cancel a blocked thread. A deadline plus
`thread.join(timeout=N)` can let the main thread assert while a non-daemon worker
remains alive; `threading._shutdown()` then waits without limit and the pytest
process still hangs. That is exactly the defect obligation (b) forbids.

The harness-owned worker is therefore a **daemon thread**. This is a last-resort
run-termination backstop, not a substitute for cleanup:

- the ordinary and raising paths still join cleanly;
- the in-process blocked-worker test uses a test-controlled release and joins in
  `finally`, so it leaves no thread behind;
- a separate bounded subprocess imports the real shared harness, gives it a
  permanently blocked writer, catches the deadline assertion, and must exit.
  Changing `daemon=True` back to the default non-daemon setting must make that
  subprocess hit the parent's external timeout.

Daemon use is confined to this test-local helper. The real writer touches only
its `tmp_path` destination, so it cannot leave production state partially
mutated. A future caller with non-test or non-temporary side effects is outside
this helper's contract and must use a cancellable process/task abstraction
instead.

## Expected collateral

- **Required positive evidence, not a repair:** the rewritten test body changes.
  There is no existing assertion that must go red — the three substantive
  assertions (MP1) are preserved verbatim and must stay green throughout. If
  any of them goes red, the fix is wrong, not the test.
- **The one line that must change meaning:** the comment at `:827-830` states
  "This one HANGS rather than fails when it is wrong". After this change that
  is false. Leaving it is a stale anchor, so updating it is required, not
  optional.
- No other test file, and no production file, should appear in the diff.
