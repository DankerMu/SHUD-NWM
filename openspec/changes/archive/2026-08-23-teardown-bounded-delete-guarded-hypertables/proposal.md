# Bound the integration teardown's DELETEs against guarded hypertables

## Why

`tests/integration_helpers.py`'s `_clear_issue_126_rows` (`:412-465`) issues two
DELETEs against TimescaleDB hypertables that are registered in the write guard's
`HYPERTABLES_GUARDED` set (`packages/common/timescale_write_guard.py:67-72`),
and neither carries a `valid_time` predicate:

```text
:420-428  DELETE FROM hydro.river_timeseries WHERE run_key IN (SELECT ...)
:434-437  DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id LIKE %s
```

Both tables are hypertables on `valid_time` (`db/migrations/000006_hydro.sql:60`,
`db/migrations/000005_met.sql:112`) and both have compression configured
(`db/migrations/000047_hypertable_compression_settings.sql:82-86` and `:123-127`).
This is the failure class #1119 fixed on the production path: TimescaleDB
refuses a DELETE without a time predicate on a hypertable holding any compressed
chunk — **even when zero rows match**.

It is inert today only because the integration database has no compressed chunks
yet: `000047` deliberately configures compression without an
`add_compression_policy`, and compression is applied out of band by
`scripts/node27_timeseries_compression.py`. Once #845 rolls compression out, this
breaks on node-27 — which is this repository's real-DB oracle — and surfaces as
"test teardown failed", which is a far worse place to debug it than here.

No static check catches this. `tests/test_timescale_write_guard_wire_site_invariant.py`
excludes `tests/` from its scan roots (`:80-97`), and even inside its scope it
only asserts that a guard call exists somewhere in the module, never that the
DELETE literal carries a time predicate — the gap #1642 tracks.

## What Changes

- Both DELETEs gain a `valid_time` lower and upper bound, derived by probing the
  rows actually present under the same identity predicate.
- When the probe finds no rows, no DELETE is issued at all.
- The `hydro.river_timeseries` statement keeps its existing identity predicate
  shape verbatim, including the `WHERE run_key IN (` subquery text that
  `tests/test_river_ts_text_identity_cleanup.py:1110-1119` asserts on.
- `tests/test_river_ts_text_identity_cleanup.py`'s statement register is updated
  to account for the new probe statement, and the probe gets its own shape pin —
  which is exactly the maintenance operation that file's census was built to
  force (design.md D4). This is the one test file edited, and it gains
  assertions rather than losing any.
- No production path changes. No new dependency. No change to the write guard.

## Out of scope

- `#1642`'s static enforcement (make the AST invariant check the DELETE literal
  itself, and widen its scan roots to test helpers).
- The other cleanup statements in the same function — `ops.pipeline_job`,
  `ops.pipeline_event`, `ops.qc_result`, `hydro.state_snapshot`,
  `hydro.hydro_run`, `met.forcing_version_component`, `met.forcing_version`,
  `met.forecast_cycle` — all target ordinary tables, not hypertables.
- `#1119` / PR #1639's production fixes, which are already in.
- `#845`'s compression rollout itself.
