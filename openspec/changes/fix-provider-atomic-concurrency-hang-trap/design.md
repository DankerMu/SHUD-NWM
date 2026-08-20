# Design: provider_atomic concurrency hang trap

## Risk triage

- **Fixture tier: `compact`.** Test-only, single file, one function rewritten
  plus one added test, no production module touched, no schema/API/migration
  surface. Size S.
- **Upstream contract note:** #1633 was filed by `issue-scribe` as an
  out-of-scope finding from #1513 / PR #1625. It carries no
  `Suggested fixture level` and no `Minimal mergeable slice` field, so there is
  no upstream triage to start from or diverge from; `compact` is this
  workflow's own triage, recorded here as the baseline.
- **Selected risk packs:** `correctness` (the fix must not make the test pass
  vacuously), `test-oracle-integrity` (the test's job is to catch a real
  concurrency invariant; the fix must not weaken it).
- **Not selected:** `security`, `performance`, `data-migration`,
  `api-compatibility`, `frontend` — no such surface is in reach of a change
  confined to one test function.

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

- `atomic_replace_provider_bytes` (raising vs. succeeding) — the failure
  injection seam for D3.
- `threading.Event` / `Thread.join(timeout=)` — the completion-signalling seam.

## D1 — Conform to the house pattern; do not invent a new one

`tests/test_scheduler_file_provider_refresh.py` already contains the correct
shape, twice:

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

Scope of the borrowing, stated precisely: the `errors` list, the catch-all, the
bounded join, and the two closing assertions are **conformance** — they already
exist here. The `finally`-set sentinel and the spin-loop deadline are **new**;
neither house instance has a completion sentinel at all (both gate on a
`Barrier` and simply join). Calling the whole fix "conforming to the house
pattern" would overstate it.

## D2 — The issue's proposed fix is insufficient; recorded, not silently absorbed

#1633 proposes `finally: finished.set()` and claims this "把任何未来的失败从挂死
转成干净的失败" (turns any future failure from a hang into a clean failure).
**That claim is false**, and the correction is load-bearing for this design.

`threading.Thread` swallows exceptions raised in the thread body. pytest reports
them only as `PytestUnhandledThreadExceptionWarning`, and `pyproject.toml`
`[tool.pytest.ini_options]` declares no `filterwarnings`, so the warning is
non-fatal. A bare `try/finally` therefore converts the hang into a **silent
pass** — arguably worse than the hang, because a hang at least stops the run.

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
   - `tests/test_timescale_write_guard_wire_site_invariant.py` — the tree's only
     AST meta-guard; it targets `workers/forcing_producer/store.py`, not this
     file.
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

## D7 — Scope of the spec requirement, and why it is narrowed twice

The first draft governed any test that "hands work to a thread and waits on a
completion sentinel". Sweeping the tree showed that is **overreach**: the
population is 12 `threading.Barrier(` sites and roughly 20 `threading.Event()`
sites, and a requirement that broad would be false against the tree the moment
it merged.

A second draft scoped it to "blocking dependence" — the main thread cannot
progress until a worker reaches a synchronization point. Still too broad. It
swept in bounded waiters such as `tests/test_gateway_reconcile.py:6297` and
`:7713`, where the main thread does `assert entered.wait(timeout=5)`. Those have
no catch-all and so would violate the "captured exceptions" bullet, yet they
cannot hang: the bounded wait returns, the assert fails, the test ends. Marking
them violations would be false.

The requirement is therefore scoped to **spin-wait on a self-set sentinel**: the
main thread's only exit condition is a sentinel the worker must set, and it
burns CPU until then. Excluded, explicitly:

- bounded `event.wait(timeout=N)` — terminates on its own;
- bare `thread.join()` — a raising worker dies and the join returns, so
  `tests/test_scheduler_generation.py:1196` is out of scope rather than a
  violation.

Under this trigger the only site in the tree today is the one this change fixes,
so **after the fix the tree conforms with zero exceptions**. That is the point:
a spec with no exceptions is worth more than a broader spec carrying a list of
things it silently tolerates.

### Conformance sweep behind the "zero exceptions" claim

The claim that exactly one site in the tree is governed is evidenced, not
asserted. Spin-waits were enumerated by every form they take here, not just the
obvious one:

- `is_set()` busy-loops — 3 total. `tests/test_scheduler_file_provider_refresh.py:842`
  is the target. The other two are **worker** bodies, not main-thread waits:
  `tests/test_file_orchestration_journal.py:7894` (inside `_hammer_until`, which
  already satisfies (a)(b)(c) — deadline-bounded, `except BaseException` into a
  `failures` list, then `stop.set()`), and `tests/test_gateway_reconcile.py:5238`
  (inside a daemon's `_write`).
- Predicate spin-waits not using `is_set()` — `tests/test_shud_runtime.py:6006`,
  `tests/test_node27_timeseries_compression_supervisor.py:974`,
  `tests/test_production_scheduler.py:21748`, `:21766`, `:44149`. All carry
  `and time.monotonic() < deadline`, so all satisfy (b); most spin on a
  subprocess-written file rather than a worker-thread sentinel, placing them
  outside the trigger regardless.
- `while True:` loops — 3 total, none relevant.
  `tests/test_node27_timeseries_compression_benchmark.py:553` is a deliberate
  infinite block inside a fake `close()` whose *bounding* is the test's subject;
  `tests/test_select_ci_tests.py:682` is a fixed-point set iteration with no
  threads; the third is inside a `python -c` string run as a subprocess.

Result: one governed site, which this change fixes. After it, the tree conforms
with no exceptions.

The two `tests/test_gateway_reconcile.py` Barrier strands are real hangs of the
same family but are not spin-waits, so rather than grandfather them the spec
names them as an adjacent hazard routed to #1645, expected to come under a
widened form of the requirement when fixed. #1646 carries the shape-independent
backstops.

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
