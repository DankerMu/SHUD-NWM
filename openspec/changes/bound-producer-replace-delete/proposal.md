# Bound the DB-mode forcing producer's replace DELETE by its guarded valid_time window

## Why

`workers/forcing_producer/store.py::replace_forcing_timeseries` issues an
unbounded DELETE against `met.forcing_station_timeseries`:

```sql
DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = %s
```

TimescaleDB rejects a DELETE with no time predicate on a hypertable that
holds **any** compressed chunk — even when zero rows match — because the
planner cannot exclude the compressed chunks. This exact failure already
struck the sibling handoff path on the same table in production (36 failed
runs, `cannot update/delete rows from chunk _hyper_1_5_chunk as it is
compressed`); `731eb2a7` fixed that path and left this copy untouched.

The second, quieter defect is the one that makes the fail-closed contract
hollow: the pre-write guard `check_batch_targets_uncompressed` is handed a
window computed **only from the incoming batch**
(`store.py:750-751`), while the DELETE it protects targets **every row of
that `forcing_version_id`**. Existing rows outside the batch window are
deleted without ever having been certified uncompressed. The guard can
therefore return PASS on a target set it never inspected — the guard window
and the DELETE target set are two different sets.

Issue #1119 recorded the user's ruling on 2026-08-19: **arm A — fix the
DELETE**. The retirement arm (deleting the 997-line DB-mode repository) is
out of this issue entirely and MUST NOT be taken during implementation.

## What Changes

- `replace_forcing_timeseries` computes the union window
  `existing ∪ incoming` inside the same transaction, guards that union, and
  bounds the DELETE to it — mirroring the in-repo precedent
  `packages/common/forcing_domain_handoff_apply.py:744-797` (`731eb2a7`).
- `PsycopgForcingRepository._replace_values` gains one optional keyword,
  `delete_parameters_factory`, invoked after the pre-write cursor hook, so
  cursor-derived DELETE parameters can reach the DELETE. Default `None`
  leaves the three other call sites (`store.py:311`, `:386`, `:716`)
  behaviourally identical.
- Unit tests (mock cursor, the established oracle for this wire site) cover
  the union window, the empty-window skip, and unchanged incoming-only
  behaviour.

## Non-Goals

- **Arm B (retiring `PsycopgForcingRepository`) is forbidden here** — it
  requires a behaviour-contract ruling on the unset `NHMS_*_DB_FREE` flags
  and would loosen the write-guard meta-guard. Separate issue, after #845.
- No shared helper extracted between this path and
  `forcing_domain_handoff_apply.py`. Deduplication would widen the blast
  radius into the production-critical handoff path for a path that is
  currently zero-traffic. Recorded as considered-and-rejected in design D6.
- No real-DB test. Arm A is local-only per the ruling; the existing oracle
  for this wire site is the mock cursor in
  `tests/test_timescale_write_guard_wired.py`.
- No change to the handoff path (`731eb2a7`, already bounded), the parser
  path (`workers/output_parser/parser.py:792-898`, already bounded), or
  `replace_forcing_components` (`store.py:716`, targets the plain table
  `met.forcing_version_component`, not a hypertable).
