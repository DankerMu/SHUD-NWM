## ADDED Requirements

### Requirement: Canonical national tile refuses a partially covered identity
The canonical source/cycle national discharge tile route `GET /api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` SHALL, on a tile-cache miss, refuse a `(source, cycle)` identity whose set of covering active river networks is non-empty but not equal to the set of active river networks, responding HTTP 424 with error code `MVT_NATIONAL_IDENTITY_INCOMPLETE` and the covered and active network counts. The coverage rule MUST be the one the per-cycle `/api/v1/layers/discharge/valid-times` branch applies, evaluated by the same helper, as a set comparison. An identity covered by no active network MUST keep the existing HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, and a fully covered identity MUST keep its response bytes, cache key and headers. The legacy source-less national route MUST NOT evaluate this rule.

#### Scenario: Partially covered cycle is refused
- **WHEN** live PostGIS MVT is enabled, the tile is not cached, and active networks `{rn-a, rn-b, rn-c}` exist while only `rn-a` and `rn-b` have a display-ready run for `(gfs, C)`
- **THEN** the route responds HTTP 424 with `error.code = MVT_NATIONAL_IDENTITY_INCOMPLETE`, `details.covered_network_count = 2` and `details.active_network_count = 3`
- **AND** the tile SQL is not executed and no cache entry is written

#### Scenario: Fully covered cycle is unchanged
- **WHEN** every active network has a display-ready run for `(gfs, C)`
- **THEN** the route responds 200 with the same bytes, cache key and headers as before this requirement, regardless of whether `C` is inside the `/cycles` lookback window

#### Scenario: Uncovered cycle keeps the no-run verdict
- **WHEN** no active network has a display-ready run for `(ifs, C)`
- **THEN** the route responds HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` exactly as before this requirement

#### Scenario: Membership mismatch at equal size fails closed
- **WHEN** the active set is `{rn-b, rn-c1, rn-c2}` and the covering set is `{rn-a, rn-c1, rn-c2}`
- **THEN** the route responds HTTP 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE`

#### Scenario: Cache hit and disabled live PostGIS issue no coverage statement
- **WHEN** the tile is already cached, or live PostGIS MVT is disabled
- **THEN** no coverage statement is executed, and the response is the cached tile or the existing `MVT_LIVE_POSTGIS_UNAVAILABLE` 424 respectively
