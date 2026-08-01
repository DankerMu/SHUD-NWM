# Negative oracle for the drop_chunks identity guard (#1214)

## Why

`_default_drop_chunk`'s identity guard (`scripts/node27_timeseries_retention.py`
`dropped_names != [chunk.qualified_name]` → RuntimeError) is the sole runtime
enforcement of spec invariant **H3 BLOCKING** ("server picks all matching
chunks — bind returned identity AND cardinality"), and PR #1212's runbook
§8.6 item 5 trichotomy explicitly depends on it. Yet coverage probing shows
the raise line was never executed by any test (Missing line 976 at master
22866500): deleting the whole guard leaves the suite green. Any refactor of
the fetchall/identity check can silently erase H3's only runtime evidence on
an irreversible DROP CHUNK path.

Fixture-level note: triaged **compact** despite the issue narrative touching
`drop/delete` vocabulary — the declared change surface is
`tests/test_node27_timeseries_retention.py` ONLY, with a hard acceptance
criterion that `scripts/node27_timeseries_retention.py` has zero diff; the
delete semantics live in unchanged production code. `design.md` exempt at
this level.

## What Changes

- Parametrize/extend the existing real-function test skeleton
  (`test_default_drop_chunk_bounds_exact_physical_interval`'s _FakeCursor /
  _FakeConn / psycopg2 monkeypatch) with the guard's failure directions:
  - `fetchall() == []` — chunk vanished mid-tick (zero rows), the branch
    runbook §8.6 item 5 relies on;
  - `fetchall() == [("_timescaledb_internal.chk-other",)]` — server dropped
    a DIFFERENT chunk (the surprising-range decision H3 defends against);
  - selected chunk PLUS an extra name (round-1 verified finding cand-01) —
    cardinality binds: membership/first-row weakenings must fail;
  - the raise exits THROUGH the `with connection:` context (round-2 verified
    finding cand-02) — pins rollback-on-guard-failure: moving the guard out
    of the transaction block must fail the suite.
  Each row asserts `pytest.raises(RuntimeError)` with `match` binding both
  `expected exact selected chunk` and the selected chunk's qualified name.
- Optional tick-level supplement (recommended in issue, implementer's call
  on cost): real `_default_drop_chunk` wired into `run_retention` via fake
  psycopg2 → outcome `refused`, `refusal_reason` starts
  `RETENTION_DROP_FAILED:`.

## Non-goals

- Zero production-logic change (`scripts/node27_timeseries_retention.py`
  zero diff — the guard is correct today; this is pure test debt).
- No change to the 41 injected-seam tests, gate logic, receipt schema,
  runbook §8.6 text, or CI selector mapping (#1191 tracks that).
