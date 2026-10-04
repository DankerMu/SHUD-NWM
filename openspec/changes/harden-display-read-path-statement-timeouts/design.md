# Design

Change surface: `ForecastStore.list_met_stations` (`packages/common/forecast_store.py`), route
`GET /api/v1/met/stations` (`apps/api/routes/data_sources.py`, no contract change expected);
`_cached_or_generated_mvt_response` (`apps/api/routes/hydro_display.py`) and a SQLSTATE discriminator in
`apps/api/errors.py`; `_safe_write_cache` / `_write_cache` / `_ensure_tile_layer` (`services/tiles/mvt.py`).

Must preserve:
- `list_met_stations` result contract: same rows, no duplicates, `ORDER BY ms.station_id`, LIMIT/OFFSET,
  `total_count`, `filters_applied`, search ILIKE escaping, the no-`model_id` branch (incl. `active_flag`),
  and `model_id` without `basin_version_id`. `met.met_station.station_id` is the PRIMARY KEY
  (`db/migrations/000005_met.sql:48`), so one row per station holds without DISTINCT.
- Cold gate: permit and DB checkout released on every exit path; a failed producer never reaches
  `build_raw_tile_response` (no cache write); every non-57014 exception propagates unchanged, in
  particular `OperationalError` with pgcode `08006`/`None` (DB unreachable) stays a 500.
- 503 body code `MVT_COLD_GENERATION_BUSY`, headers `Retry-After: 1`, `Cache-Control: no-store`
  (consumed by `apps/frontend/src/components/map/m11MvtRetryProtocol.ts`).
- Writable role: DB tile-cache write behaviour unchanged. sqlite (tests): unchanged, no probe.
- `cache_status` stays `miss` when the file cache write succeeds, `bypass` when neither does.
- The existing PostgreSQL-dialect fake suites (`tests/hydro_display_mvt_helpers.py::_Session` and its
  users) stay green with no assertion weakened. If one asserts a DB cache INSERT, the shared fake may
  learn to answer the probe as writable; nothing else in those tests changes.
- A 57014 mapped to 503 is distinguishable from gate saturation in the log: one WARNING naming the
  statement timeout, layer and tile; body code and headers unchanged.

Must add/change: see proposal "What Changes".

Governing invariants:
1. #2694: the `model_id` branch reads `met.interp_weight` only through uncorrelated scalar subqueries,
   so its cost does not depend on row estimates; its result set equals the JOIN+DISTINCT form.
2. #2712: only SQLSTATE 57014 raised inside the cold gate becomes 503 busy.
3. #2716: a role without INSERT+UPDATE on the cache tables sends zero INSERT/UPDATE to them; the
   privilege question is asked a bounded number of times per engine (once), never per request.

Probe semantics (#2716): PostgreSQL dialect only. Writable = both tables: table absent
(`to_regclass` NULL) keeps today's behaviour for that table (tile_cache absent -> no DB write;
tile_layer absent -> allowed), table present requires INSERT and UPDATE (the statement is
`INSERT ... ON CONFLICT DO UPDATE`). The probe itself must not be able to produce a PG ERROR in the read-only mode.
Cache mechanism: a module-level dict keyed by `id(bind)`, guarded by a lock for dict access only, with
the entry dropped by `weakref.finalize(bind, ...)`; a bind that cannot be weak-referenced (the
`SimpleNamespace` bind of `tests/hydro_display_mvt_helpers.py::_Session`) is probed without caching.
A module-level reset helper clears the dict for tests. The probe query is NOT run under the lock:
after the first probe for an engine completes no further probe runs; concurrent first probes may each
run (idempotent, read-only) and that is accepted.
Probe outcome: writable only when the row yields a real `bool` True. `SQLAlchemyError` (rolled back),
a missing row, a missing key or a non-bool value all mean not-writable, logged at WARNING once per
engine and cached (file cache covers; a GRANT takes effect on display API restart).

Sibling surfaces:
- `variables` coverage subquery in `list_met_stations` (also scans `interp_weight`): converted to the
  same shape; it was not in the issue's measurements, so its plan is in the deferred receipt list.
- `build_tile_response` and `build_raw_tile_response` both call `_safe_write_cache`: one gate covers both.
- Six MVT routes share `_cached_or_generated_mvt_response`: one mapping point. The under-lock
  `read_cached_tile_response` inside the gate is covered by the same try (57014 there is also busy).
- Consumers of `/api/v1/met/stations`: `services/production_closure/readonly_db_route_smoke.py:123`
  (`model_id` WITHOUT basin, `limit=1`, part of the deploy receipt) and
  `apps/frontend/src/pages/hydroMet/bootstrap.ts:168`; response contract unchanged for both.
- `_read_cache`, `national_discharge_source_version`, `services/tile_publisher/publisher.py` (writer
  role): none - explicit non-goals / different role.
- Frontend retry protocol: none - contract reused unchanged.

Seams under test: `ForecastStore.list_met_stations` via the recording cursor (SQL shape + params);
an `integration`-marked PostgreSQL test (`tests/test_*_integration.py`, CI job `real-db-integration`,
PostgreSQL 15) for result equivalence; `_cached_or_generated_mvt_response`; `build_raw_tile_response` / `_safe_write_cache` with a
fake PostgreSQL-dialect session that records statements.

Required evidence:
- model_id + basin: neither COUNT nor page SQL contains `JOIN met.interp_weight`; both contain the
  `= ANY((SELECT array_agg(DISTINCT station_id) FROM met.interp_weight WHERE model_id = %s)::text[])`
  filter; params order matches placeholders; page keeps `ORDER BY ms.station_id LIMIT %s OFFSET %s`.
- variables filter: station must carry every requested variable (count-of-distinct = N preserved).
- producer raises OperationalError(orig.pgcode="57014") -> ApiError 503, code/headers equal the busy
  constants, checkout count 0, next request admitted under limit 1, `build_raw_tile_response` not called.
- producer raises OperationalError(orig.pgcode="08006" and None) -> same OperationalError propagates.
- 57014 mapping emits the distinguishing WARNING (asserted via caplog); saturation does not.
- integration test: old JOIN+DISTINCT SQL and new SQL return identical ids/order/total on seeded rows
  (station shared by two models, station of another basin, duplicate weight rows, empty model,
  model_id-only, variables N-coverage with a station carrying only N-1). CI job must be PASSED, not
  skipped (PR must be non-draft).
- probe returns non-bool / no row / raises -> not writable, zero INSERT, one WARNING.
- read-only probe result -> zero statements matching INSERT/UPDATE on map.tile_layer/map.tile_cache,
  file cache written, `cache_status == "miss"`; N misses -> exactly one probe.
- writable probe result -> INSERT statements issued as before.
- Each new-behaviour test is shown red on pre-change source and green after.

Non-goals: see proposal.

Review focus:
1. Result-set equivalence of the new SQL incl. empty model (array_agg -> NULL -> no rows) and
   `model_id` without `basin_version_id`.
2. The 57014 discriminator cannot match connection failures; `raise ... from exc`; finally-block intact.
3. Probe cache keying/lifetime, thread-safety under 2 uvicorn workers x threadpool, no PG ERROR path.
4. No weakening of existing tests.
