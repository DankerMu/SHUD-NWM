## ADDED Requirements

### Requirement: National river-network tiles use the denser stream_type threshold table
The national river-network MVT SQL SHALL filter `core.river_segment.stream_type` with the threshold table z≤4 → ≥4, z5 → ≥3, z6 → ≥2, z7 → ≥1, z≥8 → ≥1 (one class denser than the previous z≤4 → 5 / z5 → 4 / z6 → 3 / z7 → 2 table), and `NATIONAL_RIVER_NETWORK_QUERY_VERSION` SHALL be bumped to `stream-type-aggregate-v3` so cached tiles are regenerated.

#### Scenario: Threshold table is encoded in SQL
- **WHEN** `postgis_tile_sql("river-network-national", zoom=z)` is generated for z ∈ {3, 4, 5, 6, 7, 8}
- **THEN** the SQL contains the stream_type lower bound 4, 4, 3, 2, 1, 1 respectively

#### Scenario: Cache generation changes
- **WHEN** the national river-network cache key is computed after this change
- **THEN** it includes `stream-type-aggregate-v3` and differs from the v2 key for the same tile

### Requirement: Denser national river tiles stay within the coordinate budget
Before the threshold change is enabled in production, node-27 SHALL measure `coordinate_count` for every z3 and z4 tile intersecting the China bounds with the new SQL; every tile MUST stay below `MVT_MAX_COORDINATES` (50 000). If any tile exceeds the budget the affected zoom level MUST fall back one class (e.g. z≤4 back to ≥5) and the receipt MUST record the measured values and the fallback.

#### Scenario: Go decision
- **WHEN** all z3/z4 China tiles measure below 50 000 coordinates
- **THEN** the v3 threshold table ships unchanged and the receipt lists per-tile counts before/after

#### Scenario: No-go decision
- **WHEN** any z3 or z4 tile reaches 50 000 coordinates
- **THEN** that zoom level's threshold is raised by one class in the shipped table
- **AND** the receipt and the spec comment record which level was rolled back and why

### Requirement: Frontend national river paint is not dimmed at low zoom
`m11NationalRiverPaint` SHALL apply the `dimmed` opacity discount only at zoom ≥ 6 via a zoom-interpolated expression, and SHALL use wider z3–z5 line-width stops for trunk classes (`Type ≥ 4`: ≥1.4 px at z3, ≥2.2 px at z5) so the national network is legible beneath the discharge overlay.

#### Scenario: Low zoom ignores dimming
- **WHEN** `m11NationalRiverPaint({ dimmed: true, satellite: false })` is evaluated
- **THEN** the `line-opacity` expression yields the undimmed value at zoom 3–5.99 and the 0.42-scaled value at zoom ≥ 6

#### Scenario: Trunk width stops
- **WHEN** the paint is evaluated for a feature with `Type = 5` at zoom 3 and zoom 5
- **THEN** `line-width` is ≥ 1.4 px and ≥ 2.2 px respectively
