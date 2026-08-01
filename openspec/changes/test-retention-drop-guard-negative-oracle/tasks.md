# Tasks: test-retention-drop-guard-negative-oracle

Fixture level: compact · Repair intensity: low · Issue #1214

Change surface:
- tests/test_node27_timeseries_retention.py ONLY (near the existing
  real-function test at the `_default_drop_chunk` section)

Must preserve:
- scripts/node27_timeseries_retention.py zero diff
- The 41 injected-seam drop tests unmodified
- Existing real-function happy-path test unmodified in semantics
  (parametrizing it is allowed if the happy row keeps its assertions)

Must add:
- Guard failure-direction coverage: empty fetchall + mismatched identity

Seams under test:
- `_default_drop_chunk(config, chunk)` real function with fake psycopg2
  cursor (no DB) — the same seam the existing happy-path test uses

Risk packs (compact):
- Error handling / rollback / partial outputs: selected — the new tests ARE
  the negative oracle for a fail-closed path.
- Public API / CLI / script entry: not selected — no entrypoint change.
- File IO / path safety / overwrite: not selected — no IO change; the DROP
  semantics live in unchanged production code.
- Schema / columns / units / field names: not selected — no schema change.
- Auth / permissions / secrets: not selected — no secret surface.
- Legacy compatibility / examples: not selected — additive tests only.
- Other packs: not selected — test-only diff, no runtime behavior change.

## Implementation tasks

- [x] 1. Parametrize the fetchall return of the real-function drop test (or
  add sibling tests) with two failure rows: `[]` and a mismatched chunk
  name; `pytest.raises(RuntimeError, match=...)` binding
  `expected exact selected chunk` AND the selected chunk's qualified name.
- [x] 2. Mutation proof: with the guard block temporarily deleted from a
  scratch copy (never the working tree), the suite MUST fail; capture output
  for the PR body; final diff contains no production change.
- [x] 3. Coverage proof: `coverage report -m` Missing no longer contains the
  guard's raise line (master baseline 976; anchor by the `raise RuntimeError`
  line if drifted).

## Required evidence

- Test: `fetchall() == []` → RuntimeError with `expected exact selected
  chunk` + qualified name in message.
- Test: `fetchall() == [("_timescaledb_internal.chk-other",)]` → same
  RuntimeError shape.
- Command: `uv run pytest -q tests/test_node27_timeseries_retention.py` all
  green (baseline on current master: 136 passed / 1 skipped; count grows).
- Command: `uv run ruff check .` clean.
- Mutation output (guard deleted on scratch copy → suite fails) in PR body.
- Coverage output (raise line no longer Missing) in PR body.
- `git diff --stat`: tests file + openspec only; retention script zero diff.

## Non-goals

- Production logic, gate, receipt schema, runbook text, CI selector (#1191).
