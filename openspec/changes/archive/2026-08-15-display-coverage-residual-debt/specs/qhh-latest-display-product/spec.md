# qhh-latest-display-product Delta

## ADDED Requirements

### Requirement: Fallback candidate query scan discipline

The authoritative CTE fallback of latest-QHH candidate discovery SHALL bound its hypertable scans with literal per-candidate predicates so the planner can exclude chunks, while preserving row-level parity with the pre-existing fallback semantics.

#### Scenario: Header prefetch and pushdown binding

- **WHEN** the display-coverage cache is unavailable or misses and the fallback path executes
- **THEN** a header statement built from the same candidate SQL text first resolves the candidate run's scalar identity and display window
- **AND** the heavy statement binds those scalars as NULL-guarded scan predicates on both `met.forcing_station_timeseries` and `hydro.river_timeseries` and pins its candidate selection to the header's `run_id`.

#### Scenario: Empty candidate short-circuit

- **WHEN** the header statement returns no candidate
- **THEN** the fallback returns an empty result without executing the heavy statement.

#### Scenario: Result parity

- **WHEN** the pushdown fallback and the previous single-statement fallback run against the same database state
- **THEN** they return identical rows (columns, values, ordering).
