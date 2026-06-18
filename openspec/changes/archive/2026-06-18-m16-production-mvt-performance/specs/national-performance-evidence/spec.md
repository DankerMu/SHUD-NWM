## ADDED Requirements

### Requirement: National performance evidence
The system SHALL produce deterministic and opt-in real-data evidence for national tile performance.

#### Scenario: Deterministic lane
WHEN large fixture validation runs
THEN evidence records query plan/hash, p95, payload size, tile count, memory, and browser timing against thresholds

#### Scenario: Deterministic MVT artifact threshold enforcement
WHEN measured deterministic MVT contract evidence is supplied
THEN payload_bytes, p95_ms, browser_timing_ms, tile_count, feature_count, and coordinate_count are validated against named thresholds or minimum coverage floors before the artifact can pass

#### Scenario: Real-data opt-in
WHEN real PostGIS/national data env is configured
THEN validation records live evidence without claiming readiness when dependencies are missing

#### Scenario: Evidence status semantics
WHEN a dependency is missing or live proof is not configured
THEN evidence records execution_mode, status, blockers, removal criteria, and residual risk without reporting a false pass or setting `production_mvt_readiness_claimed=true`

#### Scenario: Existing summary compatibility
WHEN deterministic MVT checks pass but live PostGIS/national-data/frontend proof is not executed
THEN existing production scale summary remains compatible with `ready`/`blocked` vocabulary and keeps production MVT readiness not claimed while detailed blockers explain removal criteria

#### Scenario: Artifact path safety
WHEN evidence artifacts are written
THEN paths are issue/run scoped, bounded, overwrite-safe, and exclude credentials or connection strings
