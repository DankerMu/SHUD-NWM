# Tasks

## 0. Risk triage

```text
Issue type: test
Project profile: NHMS (openspec/project-profile.md)
Blast radius: low
Fixture level: compact
Upstream suggested level: absent (hand-written issue). `none` was considered and
  rejected: the fix introduces a monkeypatch of a stdlib-backed name, so it has a
  real test-isolation failure mode that `none` (which skips review entirely)
  would not catch.
Repair intensity: low
Why:
- One test file, one assertion replaced; zero production files
- No shared entrypoint, schema, format, publish/delete, auth, or domain surface
- The one real hazard is monkeypatch scope leakage into sibling tests
Selected risk packs:
- Concurrency / shared state / ordering (the stub must not outlive its test or
  leak into the real-DB fixtures that consume the same generator)
- Legacy compatibility / examples (`_integration_database_name()` has two other
  consumers that must keep receiving real, distinct uuid4-based names)
OpenSpec change: fix-integration-gate-generator-property (generated)
Evidence floor:
- `uv run pytest -q tests/test_integration_gate.py` -> 5 passed
- 1000 consecutive rounds of that file, zero failures (issue acceptance criterion)
- Deterministic pass under a 1-digit PID (the ~86% failure case today)
- Stub-leak evidence: T8's two execution orderings. Note that the real
  consumers of the generator (`tests/conftest.py:144`, `:172`, reached from
  `tests/test_grid_registry_store.py`, `tests/test_real_database_integration.py`
  and siblings) are integration-gated and skip in the fast lane, so they cannot
  serve as the leak oracle — the unstubbed test running in the same process is
  the operative leak oracle, not a sibling test file
- `uv run ruff check .` -> zero findings
- `openspec validate fix-integration-gate-generator-property --strict --no-interactive`
```

### Risk pack selection (every core pack considered)

- Public API / CLI / script entry — **not selected**: no production entrypoint
  is edited.
- Config / project setup — **not selected**: no config, env, or dependency change.
- File IO / path safety / overwrite — **not selected**: no path or file surface.
- Schema / columns / units / field names — **not selected**: the database name's
  shape is unchanged; only how it is asserted changes.
- Auth / permissions / secrets — **not selected**: no such surface.
- Concurrency / shared state / ordering — **SELECTED**: the fix stubs a
  stdlib-backed randomness source. If the stub leaks past the test, every later
  test drawing a "random" name gets the sentinel, and the real-DB fixtures
  (`tests/conftest.py:144`, `:172`) would collide on one database name.
  Evidence: T8 (both orderings). The real-DB consumers are integration-gated
  and skip in the fast lane, so the unstubbed test in the same process is the
  operative leak oracle, not a sibling test file.
- Resource limits / large input / discovery — **not selected**: no sizing or
  discovery behavior.
- Legacy compatibility / examples — **SELECTED**: `_integration_database_name()`
  is consumed by the real-DB create/drop fixtures; the generator itself must
  remain byte-for-byte unchanged. Evidence: T3, T8.
- Error handling / rollback / partial outputs — **not selected**: no error path
  added or removed.
- Release / packaging / dependency compatibility — **not selected**.
- Documentation / migration notes — **not selected**: no user-facing behavior.
- Domain packs (geospatial/CRS, forcing time series, SHUD numerical, PostGIS/
  Timescale, Slurm lifecycle, provider snapshots, run-manifest provenance,
  published-artifact identity) — **not selected**: none is reachable from a test
  of a database-name string.

## 1. Implementation

- [x] T1 In `tests/test_integration_gate.py`, delete the line
      `assert str(os.getpid()) not in first_name` (currently `:50`) from
      `test_integration_database_name_uses_high_entropy_uuid`. Keep the other
      three assertions in that test exactly as they are (`:48` distinctness,
      `:49` `re.fullmatch(r"nhms_it_[0-9a-f]{32}")`, `:51` `uuid.UUID(hex=...)`).
- [x] T2 Add a new test in the same file that pins the generator's randomness
      and asserts exact equality — this is where the removed intent is
      re-expressed. Stub the `uuid4` the generator actually calls
      (`tests/conftest.py:225` resolves it through the module-level
      `import uuid` at `tests/conftest.py:7`) so it returns an object whose
      `.hex` is a fixed 32-char sentinel, then assert
      `conftest._integration_database_name() == "nhms_it_" + SENTINEL`.
      Choose a sentinel that is **not** a plausible uuid4 hex — it must make a
      PID-derived or otherwise contaminated implementation fail loudly rather
      than coincidentally match.
- [x] T3 Use pytest's `monkeypatch` fixture for the stub so it is undone at test
      teardown. Do not assign to the stdlib `uuid` module or to
      `tests.conftest.uuid` in a way that survives the test. Do not edit
      `tests/conftest.py`.
- [x] T4 Do not change `tests/conftest.py::_integration_database_name`
      (`:224-225`) or any other production/test file. The generator is correct.
- [x] T5 Do not weaken any surviving assertion. If an existing assertion appears
      to conflict with the new test, stop and report rather than relaxing it.

## 2. Tests

- [x] T6 `uv run pytest -q tests/test_integration_gate.py` -> **5 passed**
      (4 today plus the new generator-property test). Paste the output.
- [x] T7 Determinism under the failure case that motivates the issue: run the
      file with the process reporting a 1-digit pid — the condition under which
      today's assertion fails ~86% of the time — and show it passes. Either run
      inside a PID namespace (`docker run` with pytest as pid 1) or, if no
      container is available, monkeypatch `os.getpid` to return `7` for a
      throwaway probe run and paste that output, stating which method was used.
      A probe that only reruns the file under the ambient host PID does **not**
      satisfy this task.
- [x] T8 Stub-leak evidence: `uv run pytest -q tests/test_integration_gate.py`
      run in the same session/process as a consumer of the real generator shows
      the consumer still gets distinct real names. Concretely, add to the new
      test's file-level run: after the stubbed test, the unstubbed
      `test_integration_database_name_uses_high_entropy_uuid` still passes its
      `first_name != second_name` and 32-hex-shape assertions. Demonstrate this
      with **both** orderings, e.g. `-p no:randomly` plus an explicit
      `pytest tests/test_integration_gate.py::<stubbed> tests/test_integration_gate.py::<unstubbed>`
      invocation and the reverse. Paste both outputs.
- [x] T9 Issue acceptance criterion — 1000 consecutive rounds, zero failures.
      Run the file 1000 times in a loop and paste the loop's exit summary
      (e.g. a counter of non-zero exits, which must be `0`). Note in the report
      whether the rounds were fresh processes (preferred, since it also
      re-samples the PID) or in-process repeats.
- [x] T10 Red-proof for the new test: with the sentinel stub in place, mutate
      `tests/conftest.py::_integration_database_name` locally to something
      PID-derived (e.g. `f"nhms_it_{os.getpid():032d}"`), confirm the new test
      **fails**, then restore `tests/conftest.py` and confirm it passes. Paste
      both runs and confirm `git diff tests/conftest.py` is empty afterwards.
      This proves the new test can actually catch the property it claims — the
      thing the old assertion could not do reliably.

## 3. Verification

- [x] T11 `uv run ruff check .` -> zero findings.
- [x] T12 `openspec validate fix-integration-gate-generator-property --strict --no-interactive`
      -> strict-valid.

## Non-goals

- Changing `_integration_database_name()` itself (issue: out of scope).
- A repo-wide sweep for other probabilistic assertions; the issue records that
  this one has no sibling copy.
- `#1671`, tracked separately.
