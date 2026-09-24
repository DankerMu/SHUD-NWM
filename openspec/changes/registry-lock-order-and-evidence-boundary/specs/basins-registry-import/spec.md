## ADDED Requirements

### Requirement: Registry imports lock parent rows basin-version first

Every transaction that calls `import_basin_into_registry_core` SHALL lock the target
`core.basin_version` row with `FOR NO KEY UPDATE` before it writes or inserts any row
that references it, so that parent-table row locks are always taken in the order
`basin_version → river_network_version`, the same order the QHH production
bootstrap uses. Row-level INSERT/DELETE of `core.river_segment` rows (such as the
legacy-segment cleanup) stays outside this parent-lock invariant, as the #2157
river-segment lock-order boundary already states.

#### Scenario: bootstrap and generic import interleave on an existing basin

- **WHEN** a QHH bootstrap transaction holds its basin-version scope lock and a
  concurrent generic import of the same existing basin reaches
  `import_basin_into_registry_core`
- **THEN** the generic import waits for the bootstrap to finish, neither session
  fails with `deadlock detected`, and both complete within a bounded time.

#### Scenario: first import of a new basin

- **WHEN** the target basin version row does not exist yet
- **THEN** the lock statement returns no row and the import proceeds unchanged.
