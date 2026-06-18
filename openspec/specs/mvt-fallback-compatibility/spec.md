# mvt-fallback-compatibility Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: MVT fallback compatibility
GeoJSON compatibility SHALL remain bounded and truthful without replacing national MVT acceptance.

#### Scenario: Small bbox fallback
WHEN user requests a small/degraded view
THEN GeoJSON path can render with explicit compatibility status

#### Scenario: National view
WHEN user opens national hydrology layer
THEN tests assert MVT path is used or a release-blocking unavailable state is shown

#### Scenario: Compatibility endpoint remains bounded
WHEN bounded GeoJSON compatibility is used
THEN bbox/feature/coordinate/payload limits are enforced and over-budget responses are explicit

#### Scenario: Legacy redirect honesty
WHEN an old `.pbf` compatibility route is requested before real MVT metadata is available
THEN it does not imply true z/x/y vector-tile semantics unless the MVT contract is actually satisfied

