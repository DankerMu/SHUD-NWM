# Harden three display read paths against stale statistics, cold-build timeouts and privilege probing

## Triage

```text
Issue type: bugfix (batch D1: #2694, #2712, #2716)
Fixture level: expanded
Upstream suggested level: absent (orchestrator selects expanded: public API, error-class mapping,
  role-permission boundary)
Blast radius: /api/v1/met/stations returns wrong/duplicated/missing stations; a DB outage is disguised
  as "busy, retry in 1 s" on all six MVT routes; a writable deployment silently stops using the DB
  tile cache.
Selected risk packs: Public API / CLI / script entry; Auth / permissions / secrets;
  Error handling / rollback / partial outputs; Resource limits / large input / discovery;
  Concurrency / shared state / ordering
Evidence floor: uv run ruff check .; uv run pytest -q tests/test_list_search_contract.py
  tests/test_display_mvt_cold_admission.py plus the new/changed MVT cache-write tests and the
  existing tile/mvt suites (-k "tile or mvt"); CI green incl. real-db-integration PASSED (PostgreSQL
  equivalence test, PR non-draft). node-27 real-DB receipts
  are DEFERRED (link fault, high-IO forbidden) and listed in tasks.md.
```

## Why

- **#2694**: `list_met_stations(model_id=...)` joins `met.met_station` to `met.interp_weight`. With stale
  statistics both sides estimate one row and the planner picks an unparameterised Nested Loop
  (84 s on `basins_hlj_vbasins`, role timeout 30 s, constant 500). The statistics root cause is #2696
  and is out of scope; this change only makes the query shape independent of row estimates.
- **#2712**: a cold tile build cancelled by the role `statement_timeout` (SQLSTATE 57014) escapes the
  cold gate as an unstructured 500, which the #2537 frontend retry protocol does not retry.
- **#2716**: under the production "read-only role + file cache" mode every cold miss issues an
  `INSERT INTO map.tile_layer` that PostgreSQL rejects, logging one ERROR plus the full statement per
  miss (5111 in 6.5 h on 2026-10-04).

## What Changes

- `packages/common/forecast_store.py`: the `model_id` branch filters `met.met_station` with
  `station_id = ANY((SELECT array_agg(DISTINCT station_id) FROM met.interp_weight WHERE model_id = %s)::text[])`
  (an uncorrelated InitPlan, evaluated once) instead of a JOIN; the `variables` coverage filter takes the
  same InitPlan-array shape. COUNT and page share one WHERE.
- `apps/api/routes/hydro_display.py` (+ a discriminator helper in `apps/api/errors.py`): inside the cold
  gate, `sqlalchemy.exc.OperationalError` whose `orig.pgcode == "57014"` is re-raised as the existing
  503 `MVT_COLD_GENERATION_BUSY` (same code, same headers). No new error code, no OpenAPI change.
- `services/tiles/mvt.py`: the DB tile-cache WRITE path asks PostgreSQL once per engine whether the
  current role may write `map.tile_cache` / `map.tile_layer` (`has_table_privilege`, guarded by
  `to_regclass` so a missing table never raises) and, when it may not, skips the DB write entirely.

## Non-goals

- No ANALYZE/VACUUM, no statistics repair (#2696), no role `statement_timeout` change, no new GRANT.
- No 57014 -> 503 mapping on `/api/v1/met/stations` (would mask the plan defect).
- `national_discharge_source_version` (digest query before the gate, `hydro_display.py`) keeps its
  current behaviour: it runs outside the cold gate, was not observed timing out, and "cold generation
  busy" would misdescribe it.
- The DB tile-cache READ path (`_read_cache`) is unchanged: a SELECT-only role reading a cache another
  role populated is a legitimate mode.
- #2694's frontend all-or-nothing behaviour (`stationLayerData.ts`) needs a product decision and is not
  in this batch.
