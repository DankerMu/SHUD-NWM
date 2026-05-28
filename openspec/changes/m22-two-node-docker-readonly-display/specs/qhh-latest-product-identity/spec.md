## ADDED Requirements

### Requirement: Latest product strict identity filters

The QHH latest-product API SHALL support strict identity filters for cross-plane E2E without breaking source-only browsing.

#### Scenario: Source-only latest browsing
- **WHEN** a caller requests `/api/v1/mvp/qhh/latest-product?source=GFS`
- **THEN** the API can return the newest ready QHH product for that source using the existing latest selection semantics
- **AND** the response includes product identity fields needed by `/hydro-met`.

#### Scenario: Strict run identity query
- **WHEN** a caller requests latest-product with `source`, `run_id`, `cycle_time`, and `model_id`
- **THEN** the API returns a ready product only if all supplied identity filters match the selected row
- **AND** it does not fall back to a different run, cycle, source, or model.

#### Scenario: Strict identity unavailable
- **WHEN** strict filters do not match a ready product
- **THEN** the API returns `QHH_LATEST_PRODUCT_UNAVAILABLE`
- **AND** details include safe unavailable reasons and the requested identity
- **AND** it does not fall back to source-only latest selection or return a historical latest success.

### Requirement: Latest product response identity completeness

The latest-product response SHALL carry enough identity fields to bind 22 compute evidence to 27 display evidence.

#### Scenario: Ready product identity fields
- **WHEN** latest-product returns ready
- **THEN** the product includes `run_id`, `source_id`, `cycle_time`, `model_id`, `basin_id` or basin identity, `forcing_version_id`, `basin_version_id`, and `river_network_version_id`
- **AND** station and segment counts remain present when available.

#### Scenario: Backend accepts complete strict filters for downstream consumers
- **WHEN** a downstream consumer such as `/hydro-met` or cross-plane E2E has `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** the backend latest-product API accepts those four filters in one request
- **AND** the response identity can be compared by downstream issues without needing source-only fallback.

#### Scenario: Backend partial strict identity rejected
- **WHEN** any strict identity parameter is present without all of `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** the backend latest-product API returns HTTP `422` with code `VALIDATION_ERROR` using the standard error envelope
- **AND** the error details include safe `missing_fields`, `provided_fields`, `required_fields`, and `strict_identity_required=true`
- **AND** the backend does not run source-only latest selection.

### Requirement: Cross-plane E2E latest proof

Cross-plane E2E SHALL prove that 27 consumed the same run identity produced by 22.

#### Scenario: Matching 22 and 27 evidence
- **WHEN** 22 evidence records `run_id/source/cycle_time/model_id`
- **THEN** 27 latest-product API evidence can use those exact filters
- **AND** the returned product identity contains all four values needed by later browser/E2E validation.

#### Scenario: Historical latest cannot pass cross-plane E2E
- **WHEN** source-only latest-product succeeds but strict run identity latest-product fails
- **THEN** the backend exposes the strict failure as a typed latest-product validation or unavailable error
- **AND** later E2E issues can mark the run `BLOCKED`, `PARTIAL`, or `FAIL` instead of reporting pass based on historical data.
