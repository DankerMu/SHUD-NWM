# Design

## Explicit Quality Reads

API and forecast-store code should prefer explicit `run_product_quality`
fields:

- `quality_state`
- `unavailable_products`
- `residual_blockers`
- expected/meaningful/result counters
- no-curve/no-usable/warning-threshold counters

Historical count fallback may remain for old databases, but it must fail closed
for flood products and must not mark q_down/discharge unavailable.

## Route Gate Ordering

Flood return-period and warning-level routes should check run-level flood
quality before relying on `return_period_result` source-row identity. For an
explicit unavailable run with zero result rows, the route should return stable
409 `FLOOD_PRODUCT_UNAVAILABLE` with quality details rather than a misleading
source identity 404.

`valid_times_for_layer("flood-return-period" | "warning-level")` can return an
empty list when no result rows exist; that is a valid discovery outcome, not an
internal error.

## Latest Ready And Catalog Semantics

`latest_ready_run()` should select only explicit `quality_state='ready'` flood
runs when explicit quality is available. `latest_frequency_ready_run()` remains
independent so `/api/v1/layers` can expose discharge/water-level/river-network
for QHH/Heihe even when flood products are unavailable.

Layer catalog annotations for flood layers should include explicit quality
state, unavailable products, residual blockers, and useful counters.

## Forecast Store

Forecast-store SQL should select the explicit quality fields when
`flood.run_product_quality` exists. `_flood_product_quality_from_row()` should
preserve explicit unavailable/degraded states instead of deriving state only
from result counts.

QHH latest-product readiness remains ready when q_down is available; only
`availability.return_period_status` and reasons reflect flood unavailability.

## Compatibility

Compatibility cases are explicit:

- Table present with explicit fields and a row for the run: use explicit
  quality fields and counters.
- Table present with explicit fields but no row for the run: flood quality is
  unavailable/missing explicit quality; q_down remains available.
- Table present but explicit fields are absent: use the legacy count fallback
  because the replica has not applied #487 yet; q_down remains available.
- Table absent: retain the existing lightweight result-row existence fallback
  for forecast-store/read-replica compatibility; q_down remains available.

Legacy fallback cannot override explicit quality when the table and fields are
present.

If response schemas or OpenAPI references change, synchronize generated frontend
types and tests.

## Tests

Required coverage:

- API layer catalog keeps discharge available for a frequency-ready no-curve
  run and annotates flood layers unavailable from explicit quality.
- Flood tile/GeoJSON route returns 409 `FLOOD_PRODUCT_UNAVAILABLE` with explicit
  details for unavailable run quality before source-row 404.
- `valid_times_for_layer()` for a concrete no-curve flood run returns empty
  valid-times without 500.
- Forecast latest QHH product remains ready while
  `return_period_status='unavailable'` and reasons come from explicit quality.
- Partial-curve explicit quality is not ready even when meaningful result rows
  exist.
- Full-curve explicit ready quality still selects and serves the flood run.
