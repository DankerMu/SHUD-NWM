# Tasks

- [x] Update `apps/api/routes/flood_alerts.py` to read explicit
  `run_product_quality` fields in `_flood_product_quality()`.
- [x] Update flood route gates so explicit unavailable quality returns stable
  409 `FLOOD_PRODUCT_UNAVAILABLE` with details before misleading source-row
  404.
- [x] Keep flood valid-time discovery empty-but-successful for no-result runs.
- [x] Update `services/tiles/mvt.py::latest_ready_run()` to select explicit
  ready flood runs while preserving `latest_frequency_ready_run()`.
- [x] Update `packages/common/forecast_store.py` select/from-row helpers to
  carry explicit quality fields and preserve q_down product readiness.
- [x] Update layer catalog annotations to expose explicit unavailable/degraded
  quality for flood layers without hiding discharge.
- [x] Preserve legacy/missing-table fallback semantics without overriding
  explicit quality when present.
- [x] Resolve OpenAPI/generated frontend type boundary:
  - if response schemas change, update OpenAPI/generated types and run affected
    frontend/type tests;
  - if response schemas do not change, record PR evidence that existing schema
    shapes already permit the explicit fields or are internal-only.
- [x] Add/update focused tests.
- [x] Run verification:
  - `uv run --no-sync pytest -q tests/test_flood_alerts_api.py tests/test_forecast_api.py tests/test_forecast_store_product_quality_sql.py`
  - plus MVT/tile publisher tests touched by implementation
  - `uv run --no-sync ruff check <touched-python-files>`
  - `openspec validate issue-489-explicit-flood-quality-api --strict --no-interactive`

## Evidence Mapping

- API catalog/q_down boundary: frequency-ready no-curve fixture with one q_down
  row and explicit unavailable flood quality:
  `expected_result_rows=2`, `meaningful_result_rows=0`,
  `no_frequency_curve_rows=2`, `no_usable_frequency_curve_rows=0`,
  `unavailable_products=["frequency_curves","return_period_result"]` ->
  `/api/v1/layers` includes discharge metadata/valid-times and flood metadata
  with the same explicit unavailable fields.
- Flood route gate: explicit unavailable run with zero
  `return_period_result` rows -> flood tile/GeoJSON route returns 409
  `FLOOD_PRODUCT_UNAVAILABLE`, details include `quality_state`,
  `unavailable_products`, `residual_blockers`, `expected_result_rows`,
  `meaningful_result_rows`, and no-curve counters; it must not return source
  identity 404 first.
- Valid-times: concrete no-curve flood run -> `valid_times_for_layer()` returns
  an empty list/truncated false without 500.
- Forecast store: latest QHH candidate with q_down rows and explicit flood
  unavailable quality -> product status remains ready and
  `availability.return_period_status == "unavailable"` with explicit reason;
  product quality exposes explicit counters including `expected_result_rows=2`,
  `meaningful_result_rows=0`, and `no_frequency_curve_rows=2`.
- Partial-curve: explicit `quality_state="degraded"` with expected greater than
  meaningful counts (`expected_result_rows=4`, `meaningful_result_rows=2`,
  `no_frequency_curve_rows=2`, `no_usable_frequency_curve_rows=0`) ->
  API/forecast product quality is not ready and preserves the counters.
- Full-curve: explicit `quality_state="ready"` -> latest ready run selection,
  product quality, and existing MVT paths remain ready with
  `expected_result_rows=3`, `meaningful_result_rows=3`,
  `no_frequency_curve_rows=0`, and `no_usable_frequency_curve_rows=0`.
- SQL/resource guardrail: tests inspect SQL/source for default explicit-quality
  paths so `_flood_product_quality()`, `_flood_product_quality_select()`, and
  `latest_ready_run()` do not aggregate `flood.return_period_result`; legacy
  missing-table fallback may use only lightweight `EXISTS` probes.
- Legacy fallback cases:
  - table absent on read replica + result rows exist -> legacy forecast-store
    fallback reports flood ready/available and q_down ready.
  - table absent + no result rows -> legacy fallback reports flood unavailable
    and q_down ready.
  - table exists with explicit fields but no row for run -> explicit path
    reports flood unavailable/missing quality and q_down ready.
  - table exists without explicit fields -> legacy count fallback remains
    deterministic and q_down ready.
- OpenAPI/generated types: either generated files/tests change with the schema,
  or PR evidence states no schema generation was required and why.
