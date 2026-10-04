# Tasks

## Risk packs

- Public API / CLI / script entry: selected - `/api/v1/met/stations` and six MVT routes -> tasks 1.x, 2.x.
- Auth / permissions / secrets: selected - role-privilege probe, no GRANT change -> task 3.x.
- Error handling / rollback / partial outputs: selected - 57014 mapping, no cache write on timeout -> 2.x.
- Resource limits / large input / discovery: selected - plan independent of statistics -> 1.x + deferred receipts.
- Concurrency / shared state / ordering: selected - permit release -> 2.2; per-engine probe cache -> 3.3.
  Concurrent first probes are explicitly tolerated (design.md), so no concurrency test: non-goal.
- File IO / path safety / overwrite: not selected - file cache code, `_file_cache_path` and the retention
  path contract are untouched; the file cache is already the only effective write path in production.
- Schema / columns / units / field names: not selected - no schema or response-shape change.
- Config / project setup: not selected - no new env var.
- Legacy compatibility / examples: not selected - sqlite/test path explicitly unchanged (3.4).
- Documentation / migration notes: selected - `docs/runbooks/display-readonly-live-mvt.md` describes the
  two cache modes -> task 3.5.

## 1. #2694 met stations query shape

- [x] 1.1 Replace the `model_id` JOIN with the InitPlan-array filter for COUNT and page SQL.
- [x] 1.2 Convert the `variables` coverage filter to the same shape, semantics unchanged.
- [x] 1.3 Recording-cursor tests: SQL shape, param order, pagination, search, variables, no-basin case.
- [x] 1.4 `integration`-marked PostgreSQL equivalence test in `tests/test_*_integration.py` (old
      JOIN+DISTINCT vs new form on seeded rows: shared station across models, station in other basin,
      duplicate weights, empty model, model_id-only, variables N-coverage). Runs in CI `real-db-integration`.

## 2. #2712 cold-build statement timeout -> 503

- [x] 2.1 SQLSTATE discriminator helper in `apps/api/errors.py`; map 57014 inside the cold gate.
- [x] 2.3 Distinguishing WARNING log on the 57014 mapping, asserted by test.
- [x] 2.2 Tests: positive (503, headers, permit+checkout released, no cache write), negative (08006, None).

## 3. #2716 no privilege probing by failed INSERT

- [x] 3.1 Per-engine `has_table_privilege` probe guarded by `to_regclass`; skip DB write when not writable.
- [x] 3.2 Tests: read-only -> zero INSERT/UPDATE, file cache written, `miss`.
- [x] 3.3 Tests: probe executed once across repeated misses on a cacheable engine; probe raising,
      returning no row or a non-bool -> not writable, zero INSERT, logged once.
- [x] 3.4 Tests: writable PostgreSQL-dialect path and sqlite path unchanged; existing PG-dialect fake suites green.
- [x] 3.5 Runbook `docs/runbooks/display-readonly-live-mvt.md`: mode is decided by a once-per-process
      privilege probe; a GRANT needs a display API restart; a failed probe degrades to file cache with one WARNING.
- [x] 3.6 `scripts/select_ci_tests.py`: route every new test file (and `tests/test_select_ci_tests.py` pins).

## 4. Verification

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 `uv run pytest -q tests/ -k "tile or mvt"` and `uv run pytest -q tests/test_list_search_contract.py
      tests/test_display_mvt_cold_admission.py tests/test_select_ci_tests.py tests/test_forecast_api.py` + new test files.
- [ ] 4.5 CI `real-db-integration` ("SQL Migration Dry Run") PASSED (not skipped) on the non-draft PR head.
- [x] 4.3 `openspec validate harden-display-read-path-statement-timeouts --strict --no-interactive`
- [ ] 4.4 CI green.

## Evidence Floor deviation: node-27 real-DB receipt 待链路恢复后补

node-27 has a link fault; high-IO operations are forbidden. Local results are NOT node-27 PASS.

Deferred until the link recovers (on node-27, `export PATH=$HOME/.local/bin:$PATH TMPDIR=/home/nwm/tmp`):

- `uv run pytest -q tests/test_display_mvt_cold_admission.py tests/test_list_search_contract.py`
- `uv run pytest -q -m integration <the new equivalence test file>` against an isolated scratch DB
- `EXPLAIN (ANALYZE, BUFFERS)` of the new COUNT and page SQL with
  `dg_3b091cccc163690de93cddd9c4c32472` + `basins_hlj_vbasins`, limit 500, without running ANALYZE,
  plus the `variables` filter variant and the `model_id`-only shape (`limit=1`, as `readonly_db_route_smoke.py` calls it)

Allowed after the operator-confirmed deploy (low IO):

- `curl` `/api/v1/met/stations?model_id=dg_3b091cccc163690de93cddd9c4c32472&basin_version_id=basins_hlj_vbasins&limit=500&offset=0` -> 200, `total_count=1314`
- `docker logs --since 1h nhms-db 2>&1 | grep -c "permission denied for table tile_layer"` -> 0, with
  `find /home/nwm/.cache/nhms/mvt -name '*.pbf' -newermt "<cutover UTC>" | wc -l` > 0
- `select has_table_privilege('nhms_display_ro','map.tile_layer','INSERT'), has_table_privilege('nhms_display_ro','map.tile_cache','INSERT');` -> `f|f`
