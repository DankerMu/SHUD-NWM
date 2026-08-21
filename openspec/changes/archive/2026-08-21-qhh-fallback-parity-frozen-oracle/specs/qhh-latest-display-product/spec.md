## MODIFIED Requirements

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

- **WHEN** the pushdown fallback and the previous single-statement fallback — frozen in-repo as `tests/fixtures/legacy_qhh_fallback_pre_1413.sql`, never updated alongside production SQL — run against the same database snapshot with the same inputs
- **THEN** they return identical rows (values and ordering) on the frozen statement's column set — the production-only #1442 surrogate-key columns (`run_key`, `basin_version_key`, `river_network_version_key`) being an explicitly asserted, pinned exclusion, so a further production-only column reddens the comparison rather than widening it
- **AND** the comparison is reproducible by a real-database test in the repository against that frozen statement (not against the fast path, whose coverage cache shares the pushdown idiom), covering a covered candidate, a candidate whose `forcing_version_id` is NULL (the `scan_* IS NULL` guard branch), and the empty-header state.

#### Scenario: Parity oracle is independent of the production SQL

- **WHEN** the candidate SQL text is mutated so the display window changes
- **THEN** the parity comparison against the frozen statement fails.
