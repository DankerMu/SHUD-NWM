## ADDED Requirements

### Requirement: Any grid identity change is a new grid version
The registry SHALL treat any change to a grid's identity as a new grid version.

#### Scenario: Identity-changing edits create a new version
- **WHEN** a grid's cell count, coordinates, latitude order, longitude convention, `grid_cell_id`, flatten order, download bbox, converter cell-identity semantics, or source product changes
- **THEN** the registry registers a new immutable Grid Snapshot version rather than editing the existing one
- **THEN** the new version carries a different `grid_signature` from the prior version.

#### Scenario: Old snapshots are retained immutably and marked superseded
- **WHEN** a new grid version is registered that supersedes an old snapshot for the same `(source_id, grid_id)`
- **THEN** the prior snapshot and its ordered cells remain unchanged and retrievable for historical reproduction
- **THEN** the prior snapshot's `superseded_at` timestamp is set (append-only lifecycle marker; identity fields are still not mutated)
- **THEN** the new version does not overwrite or delete the prior version.

### Requirement: Registry exposes latest-snapshot and supersession queries
The registry SHALL expose queries that let any consumer distinguish current-version from superseded snapshots without inspecting external artifacts.

#### Scenario: latest_snapshot_for(canonical_grid_key) returns only current version
- **WHEN** a caller queries `latest_snapshot_for(canonical_grid_key)`
- **THEN** the registry returns the most recent snapshot whose `superseded_at` is NULL for that key
- **THEN** superseded snapshots are excluded from the result.

#### Scenario: Superseded snapshot is queryable but flagged
- **WHEN** a caller queries by `grid_snapshot_id` for a superseded snapshot
- **THEN** the row is returned intact with its `superseded_at` timestamp populated
- **THEN** the row's `grid_signature` no longer matches the current-version query for the same `canonical_grid_key`.

### Requirement: Consumers of a superseded snapshot fail closed
The registry SHALL declare that any component reading a superseded snapshot for production use MUST fail closed; enforcement in producer/binding/mapping components is the responsibility of those changes, and the registry surface only provides the supersession signal.

#### Scenario: Cross-change contract on supersession
- **WHEN** any downstream component (forcing producer preflight, mapping-asset build, station-binding manifest validator) reads a snapshot whose `superseded_at` is non-NULL for production use
- **THEN** the component MUST fail closed (this is a cross-change contract enforced in the `forcing-mapping-asset-build` and producer-preflight changes; the registry declares the contract, downstream changes implement the block)
- **THEN** the registry itself does not run production, so it cannot enforce the block at runtime; it exposes `superseded_at` and the current-version query as the source of truth for downstream enforcement.

### Requirement: Derived caches on superseded snapshots are marked stale
The registry SHALL propagate supersession into the display-side derived caches (`met.met_station`, `met.interp_weight`) so already-persisted derived rows tied to a superseded snapshot are not served as active.

#### Scenario: met.met_station and met.interp_weight rows are marked stale on supersession
- **WHEN** a snapshot for a `canonical_grid_key` is superseded by a new version
- **THEN** derived rows in `met.met_station` and `met.interp_weight` tied to the superseded snapshot's `grid_signature` (or its `grid_snapshot_id`) MUST be marked stale/inactive (for example via an `active_flag=false` or `superseded_at` column), retaining audit history
- **THEN** stale rows MUST NOT be deleted silently
- **THEN** stale rows MUST NOT be served as active for direct-grid production or display-side active queries; the display-side active_flag policy sourced from this change is the reference contract for the read-only display API.

### Requirement: Registry rejects in-place grid_signature replacement
The registry SHALL forbid any registry-level operation that would replace an existing snapshot's `grid_signature` without creating a new snapshot version.

#### Scenario: Registry API rejects in-place signature replacement
- **WHEN** the registry API receives a request that would replace an already-registered snapshot's `grid_signature` in place
- **THEN** the request is rejected as forbidden
- **THEN** the caller is instructed to register a NEW snapshot version instead
- **THEN** the prior snapshot's identity fields remain unchanged; only `superseded_at` may be set when a new version is added.

Note: enforcement against direct edits to a binding-manifest file on disk (as opposed to a registry API call) is out of scope for this change; it is owned by `forcing-mapping-asset-build`, which will consume the registry's current-version query and reject manifests whose `grid_signature` does not match a current, registered snapshot.
