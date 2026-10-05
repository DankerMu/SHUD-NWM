# Handoff apply leaves an existing compatible station row unwritten

Issue: #2300. Fixture level: **compact**. Risk packs: **DB write path / idempotency**, **Identity fail-closed**.

## Why

`met.met_station` on node-27 holds 63,407 rows (about 77 MB of live data) in a 624 MB heap. Autovacuum is
healthy; the table is rewritten far more often than it changes.

Measured on node-27, read-only, 2026-10-05: in the 31 hours after the 2026-10-04T02:15Z postmaster start the
table took 268,328 updates, 0 inserts and 0 deletes. In the same window 1,023 `met.forcing_version` rows were
created with `sum(station_count) = 222,240`. Updates arrive only while the autopipe ingest phase runs (about
830 per minute when sampled). `pg_stat_statements` is not installed, so the attribution is by count and time,
not by statement.

The write is `_upsert_met_stations` in `packages/common/forcing_domain_handoff_apply.py`: every handoff apply
runs `INSERT ... ON CONFLICT (station_id) DO UPDATE SET station_id = met.met_station.station_id WHERE
<identity predicate>` for every station of the forcing version. The `SET` changes nothing, but PostgreSQL
writes a full new tuple version (including `properties_json`) for every row the `WHERE` accepts. The
statement's `WHERE` is also the fail-closed identity check: a row it rejects is not returned, and the
`RETURNING` shortfall raises `HANDOFF_APPLY_STATION_CONFLICT`.

## What changes

- `_upsert_met_stations` no longer creates a new tuple version for a station row that already exists and
  satisfies the identity predicate. A station that does not exist is inserted as today (`active_flag` literal
  `false`). A station that exists and does **not** satisfy the predicate still fails the whole apply with
  `HANDOFF_APPLY_STATION_CONFLICT` and writes nothing.
- The SQL identity predicate stays the authority for that decision, term for term (basin version, coordinate
  tolerance, elevation, name + role, and the `direct_grid_cache` -> `forcing_grid` branch). It is not replaced
  by the Python `_station_rows_compatible` check, whose elevation and NULL handling differ.
- `.github/workflows/ci.yml`: `packages/common/forcing_domain_handoff_apply.py` joins the `database` path
  filter, so the real-PostgreSQL lane runs for this file.

Suggested mechanism, not mandated: keep one statement, negate the predicate in the `DO UPDATE ... WHERE` as
`(<predicate>) IS NOT TRUE` -- never bare `NOT (<predicate>)`, which would read a NULL predicate as compatible
-- and return `(xmax = 0) AS inserted`. A compatible existing row is then neither updated nor returned; an
incompatible one is returned with `inserted = false` and raises the conflict (the transaction rolls back, so
its sentinel write does not persist). `xmax = 0` is undocumented PostgreSQL behaviour; the real-PostgreSQL
tests below are what prove it on PG 15. Any mechanism with the same observable outcome is acceptable.

The conflict reason keeps its `code` and `table`. Its `expected` / `actual` counts lose their meaning under a
new statement shape and no consumer reads them; they may change or go.

## Must preserve

- `active_flag` ownership: a fresh row lands `false`; an existing row's `active_flag` is never changed by the
  apply, and a flag difference alone is never a conflict.
- An existing row's other columns (`station_name`, `geom`, `elevation_m`, `station_role`, `properties_json`,
  `station_key`, `grid_snapshot_id`) are never changed by the apply.
- Every existing conflict case still raises `HANDOFF_APPLY_STATION_CONFLICT` and leaves the tables unchanged:
  basin version, coordinates beyond tolerance, elevation, name/role outside the direct-grid branch, direct-grid
  binding or index drift.
- `_verify_existing_station_rows` (the `SELECT ... FOR UPDATE` pre-check) stays, with its order in the
  transaction: fence, routing refusal, verify, forcing version, stations, timeseries, weights.
- A station inserted by a concurrent transaction between the pre-check and the upsert is still judged by the
  SQL predicate, not silently accepted.
- A predicate that evaluates to NULL (an existing `direct_grid_cache` row without a `direct_grid` key; an
  empty-point geometry) is a conflict, exactly as under today's `WHERE <predicate>`.
- The INSERT template keeps the literal `false` for `active_flag` and the conflict predicate never references
  `active_flag` (pinned by
  `tests/test_direct_grid_variant_registration.py::test_production_run_mirror_stays_inactive_end_to_end`).
- Every station row of the envelope that already exists stays row-locked for the rest of the transaction.
- Apply report shape and `row_counts`.

## Out of scope

- The other `met.met_station` writers the issue lists (`workers/forcing_producer/store.py`, registration,
  bootstrap, `station_set_flip`, `register_grid_snapshot_drift`).
- Reclaiming the 624 MB heap (`VACUUM FULL`) and the re-measurement after it (issue acceptance 4); the owner
  decides that separately after deployment.
- A periodic-reclaim criterion and the runbook update (issue acceptance 5).
- The PR therefore uses `Refs #2300`, not `Closes`.
- Rows of retired model variants that are never deleted (63,407 rows, 4,870 active).
- `fillfactor`.

## Required evidence

- Unit: the existing `tests/test_forcing_domain_handoff_apply.py` suite stays green with its fake connection
  following the new statement shape; every conflict test still asserts the conflict.
- Real PostgreSQL (`integration` marker only, the CI lane), six cases:
  1. applying the same stations twice leaves every `met.met_station` row's `ctid` and `xmin` unchanged after
     the second apply;
  2. an incompatible existing row raises the conflict, and its user columns, `ctid` and `xmin` are unchanged
     (do not compare `xmax`: a row lock and a rolled-back sentinel write both touch it);
  3. a fresh station is inserted with `active_flag = false`;
  4. an existing `active_flag = true` survives a re-apply;
  5. a conflict that passes the Python pre-check and is rejected by the SQL alone: existing `elevation_m`
     differing from the incoming value by less than `STATION_COORDINATE_TOLERANCE` (1e-9) -- Python
     `_numbers_close` accepts, SQL `IS NOT DISTINCT FROM` rejects;
  6. the NULL case above (existing `direct_grid_cache` row without a `direct_grid` key, incoming
     `forcing_grid` with a different name), driven by calling `_upsert_met_stations(cursor, ...)` directly on a
     real cursor, raises the conflict.
  If the full apply cannot run on the CI Timescale image without the `timescaledb_210` marker, the tests may
  drive `_verify_existing_station_rows` + `_upsert_met_stations` on one real cursor in one transaction, in
  production order; the PR states which form was used. The new file carries `integration` only -- never
  `timescaledb_210`, or CI deselects it.
- node-27 after deployment: `n_tup_upd` on `met.met_station` over one full ingest tick, before and after. The
  figure is not expected to reach zero: 268,328 updates against `sum(station_count)` 222,240 leaves about 17%
  that one-apply-per-version does not explain (repeated applies or the other writers). Report the remainder
  as measured; do not attribute it without evidence.
- The raw node-27 probe output behind the Why figures is committed under `evidence/` (issue acceptance 1).
