# national-scale-performance Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.
## Requirements
### Requirement: National-scale river queries have performance evidence

The system SHALL measure production-scale river-network query behavior using real imported networks or deterministic large fixtures.

#### Scenario: PostGIS query plans and latency are captured

- **WHEN** national-scale validation runs for model listing, river bbox, flood alert map, forecast series, pipeline jobs, and tile metadata queries
- **THEN** query plans, row counts, geometry bounds, p95 latency, and index usage are captured
- **AND** thresholds and failure behavior are documented for oversized bbox and long time-range requests

### Requirement: National-scale thresholds are versioned

The system SHALL define a versioned thresholds artifact before claiming national-scale readiness.

#### Scenario: Threshold artifact defines measurable baselines

- **WHEN** national-scale validation runs
- **THEN** a thresholds artifact records minimum segment count, minimum model count, bbox sizes, p95 API targets, max tile bytes, frontend load/render limits, memory bounds, and oversized-request expectations
- **AND** the validation report states pass/fail against that artifact rather than relying on unstated thresholds

### Requirement: Production tile delivery is explicit

The system SHALL either validate true MVT delivery for production map layers or mark GeoJSON compatibility as a release blocker.

#### Scenario: MVT contract is validated or blocked

- **WHEN** production tile validation requests river/flood tiles with production content type expectations
- **THEN** responses use `application/x-protobuf` MVT with bounded tile size and correct layer metadata
- **OR** the validation report fails with an explicit blocker that current GeoJSON compatibility delivery is not production MVT

### Requirement: Frontend handles large layers within bounds

The system SHALL capture frontend loading and rendering evidence for large river layers and time-series interactions.

#### Scenario: Large frontend map and charts remain usable

- **WHEN** frontend smoke runs against national-scale or large fixture data at desktop and mobile breakpoints
- **THEN** initial load, map interaction, segment selection, timeline movement, and chart rendering stay within documented time/memory thresholds
- **AND** oversized or unavailable layers display recoverable error states rather than breaking the page

