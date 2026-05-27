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
- **THEN** the API returns a typed unavailable error
- **AND** details include safe unavailable reasons and the requested identity
- **AND** the response cannot be mistaken for a historical latest success.

### Requirement: Latest product response identity completeness

The latest-product response SHALL carry enough identity fields to bind 22 compute evidence to 27 display evidence.

#### Scenario: Ready product identity fields
- **WHEN** latest-product returns ready
- **THEN** the product includes `run_id`, `source_id`, `cycle_time`, `model_id`, `basin_id` or basin identity, `forcing_version_id`, `basin_version_id`, and `river_network_version_id`
- **AND** station and segment counts remain present when available.

#### Scenario: Frontend bootstrap passes strict filters when known
- **WHEN** `/hydro-met` has URL query parameters `source`, `cycle_time`, `run_id`, and `model_id`, or E2E provides the same four fields from `artifacts/two-node-e2e/<run_id>/cross-plane/identity.json`
- **THEN** it passes `run_id`, `cycle_time`, `source`, and `model_id` to latest-product
- **AND** it renders unavailable state instead of silently reusing a different product.

#### Scenario: Partial strict identity rejected
- **WHEN** any strict identity parameter is present without all of `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** `/hydro-met` and E2E bootstrap treat the identity as invalid
- **AND** they do not fall back to source-only latest-product for cross-plane proof.

### Requirement: Cross-plane E2E latest proof

Cross-plane E2E SHALL prove that 27 consumed the same run identity produced by 22.

#### Scenario: Matching 22 and 27 evidence
- **WHEN** 22 evidence records `run_id/source/cycle_time/model_id`
- **THEN** 27 latest-product API evidence uses those exact filters
- **AND** the returned product identity matches all four values before browser E2E can be marked pass.

#### Scenario: Historical latest cannot pass cross-plane E2E
- **WHEN** source-only latest-product succeeds but strict run identity latest-product fails
- **THEN** cross-plane E2E is marked `BLOCKED`, `PARTIAL`, or `FAIL` according to the runbook
- **AND** it is not reported as pass based on historical data.
