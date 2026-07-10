## ADDED Requirements

### Requirement: The preflight resolves the registered snapshot and fails closed on missing or superseded snapshots
The direct-grid producer preflight SHALL resolve "the registered grid snapshot" from the run's verified direct-grid contract identity — the normalized `source_id`, the `grid_id`, and the `grid_signature` — via the registry's supersession-aware current-version query (`find_snapshot_by_identity` semantics: rows with non-NULL `superseded_at` are excluded), reading the snapshot's four bbox corners and its `superseded_at` through a DB-only read that requires no object-store access. WHEN no registered snapshot resolves for that identity, the preflight SHALL fail closed with zero direct-grid production side effects. WHEN the snapshot read for production use carries a non-NULL `superseded_at`, the preflight SHALL fail closed — this lands the producer-preflight half of the cross-change contract in `grid-drift-lifecycle` §"Consumers of a superseded snapshot fail closed", mirroring the mapping-asset build's G2 enforcement (`SupersededGridSnapshotError` in `workers/mapping_builder/algorithm.py`).

#### Scenario: No registered snapshot resolves for the contract identity
- **WHEN** the direct-grid contract's (`source_id`, `grid_id`, `grid_signature`) matches no current (non-superseded) snapshot in the registry
- **THEN** the preflight fails closed and zero direct-grid production side effects occur (no repository/store write, no forcing output).

#### Scenario: A superseded snapshot fails closed
- **WHEN** the snapshot read for the run carries a non-NULL `superseded_at`
- **THEN** the preflight fails closed with zero direct-grid production side effects, per the `grid-drift-lifecycle` §"Consumers of a superseded snapshot fail closed" cross-change contract (the producer-preflight enforcement half assigned to this change).

### Requirement: The direct-grid producer verifies deployment bbox before any production side effect
The direct-grid production path SHALL call `verify_download_bbox_matches_registry` against the resolved registered snapshot at the top of the direct-grid branch of `workers/forcing_producer/producer.py::produce` — in every case before the branch's first repository write (`ensure_direct_grid_met_stations`, `upsert_interp_weights`, or any forcing-version write). On a bbox match the preflight SHALL pass and production SHALL proceed. On a bbox mismatch the preflight SHALL fail closed with `BboxMismatchError`, and no direct-grid production side effect (no repository/store write, no forcing output) SHALL occur. Note on scope: `workers/forcing_producer/` performs no raw met-product downloads — downloads are issued upstream by `workers/data_adapters` (a cycle-ingest stage shared with legacy IDW basins, clipped from the same `NHMS_DOWNLOAD_BBOX_*` env). Guarding that upstream download stage is explicitly out of scope for this change; this preflight's fail-closed guarantee covers direct-grid forcing-production side effects, not upstream downloads.

#### Scenario: Matching bbox passes the preflight
- **WHEN** the deployment `NHMS_DOWNLOAD_BBOX_*` env bbox equals the registered snapshot bbox field-for-field
- **THEN** the preflight returns without error and production proceeds.

#### Scenario: Mismatched bbox fails closed before any production side effect
- **WHEN** the deployment env bbox differs from the registered snapshot bbox in any of `{south, north, west, east}`
- **THEN** the preflight raises `BboxMismatchError` carrying `expected_bbox`, `actual_bbox`, and `grid_snapshot_id`
- **THEN** zero direct-grid production side effects occur: no repository/store write and no direct-grid production output.

#### Scenario: Preflight failures abort production and are never swallowed
- **WHEN** the preflight raises any failure — `BboxMismatchError`, or a `ValueError` propagated from the env reader (malformed `NHMS_DOWNLOAD_BBOX_*` value) or from the guard's finiteness gate (non-finite bbox value on either side, per `packages/common/grid_registry_bbox_guard.py`'s documented `ValueError` channel)
- **THEN** the production run aborts with zero direct-grid production side effects and the error propagates to the caller — the producer SHALL NOT catch-and-continue past a preflight failure.

### Requirement: The preflight reuses the pinned guard, not a re-implementation
The producer preflight SHALL import and call `packages/common/grid_registry_bbox_guard.py::verify_download_bbox_matches_registry` with its `env_reader` defaulting to `workers/data_adapters/region.py::china_buffered_bbox_from_env`. The producer SHALL NOT re-implement bbox comparison, finiteness checks, or the mismatch error.

#### Scenario: Preflight delegates to the shared guard
- **WHEN** the producer runs its bbox preflight
- **THEN** the comparison is performed by the shared `verify_download_bbox_matches_registry` guard (same function the registry change pinned), not a producer-local re-implementation.

### Requirement: The producer owns the deployment-bbox longitude convention
The producer preflight SHALL treat `workers/data_adapters/region.py`'s -180..180 longitude convention as the canonical deployment convention when comparing the deployment bbox to the registered snapshot. A deployment longitude expressed in the 0..360 convention that does not equal the registered -180..180 snapshot value SHALL fail closed rather than silently clip a shifted region.

#### Scenario: In-convention env bbox is compared directly
- **WHEN** the deployment env bbox uses the -180..180 convention matching the registered snapshot
- **THEN** the preflight compares the four corners in that convention and passes on equality.

#### Scenario: Cross-convention longitude fails closed
- **WHEN** the deployment env expresses a longitude in the 0..360 convention (e.g. an `east` of 200) that does not equal the registered -180..180 snapshot value
- **THEN** the preflight fails closed with `BboxMismatchError` rather than proceeding to clip a shifted region.
