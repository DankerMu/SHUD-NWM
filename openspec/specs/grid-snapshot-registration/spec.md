# grid-snapshot-registration Specification

## Purpose
TBD - created by archiving change canonical-source-grid-registry. Update Purpose after archive.
## Requirements
### Requirement: Grid is registered as an immutable Grid Snapshot
The registry SHALL register each usable direct-grid grid as an immutable Grid Snapshot carrying its full ordered-cell geometry, grid signature, definition checksum, and pinned download bbox.

#### Scenario: Snapshot records required identity fields
- **WHEN** a grid is registered from its canonical grid definition
- **THEN** the snapshot records the normalized `source_id`, `grid_id`, `grid_signature`, `grid_definition_uri`, `grid_definition_checksum`, longitude convention, latitude order, flatten order, native resolution, validity window (`valid_from`/`valid_to`), converter version, `applicable_source_ids` (normalized source-id list scoped to this snapshot's `canonical_grid_key`), and the pinned download bbox
- **THEN** the snapshot records, per cell, the `grid_cell_id`, normalized cell-center longitude and latitude, and a canonical ordinal
- **THEN** registration fails when any required snapshot or per-cell field is missing.

#### Scenario: grid_signature reuses the producer algorithm
- **WHEN** the registry computes `grid_signature` for a grid
- **THEN** it uses the shared grid-signature helper extracted from the producer, computing SHA-256 over ordered `(grid_cell_id, longitude rounded to 12 decimals, latitude rounded to 12 decimals)` tuples wrapped as `{"grid_points": [...]}` and serialized via `json.dumps(sort_keys=True, separators=(",", ":"))`
- **THEN** the registry does not reimplement an independent or approximate signature rule
- **THEN** the registered `grid_signature` equals the value the producer computes for the same ordered cells byte-for-byte.

#### Scenario: Both producer and registry import the single shared helper
- **WHEN** either the producer or the registry needs to compute `grid_signature`
- **THEN** it imports `grid_signature_tuples` / `grid_signature_hash` from `packages/common/grid_signature.py`
- **THEN** independent reimplementation of the algorithm in either component is forbidden
- **THEN** a static-import check verifies that `workers/forcing_producer/producer.py` and the registry module both resolve their signature functions to the shared module.

#### Scenario: Longitude is normalized to -180..180
- **WHEN** cell-center longitudes are stored in a snapshot
- **THEN** each longitude is normalized to the `[-180, 180)` convention
- **THEN** the snapshot records that the longitude convention is `[-180, 180)`
- **THEN** latitude order and array flatten order are recorded explicitly on the snapshot.

#### Scenario: grid_definition_uri is checksum-bound
- **WHEN** a snapshot references a `grid_definition_uri`
- **THEN** the snapshot stores the SHA-256 `grid_definition_checksum` of that definition
- **THEN** loading a snapshot verifies the definition content against the stored checksum
- **THEN** a checksum mismatch fails the load and the grid is not treated as registered.

#### Scenario: Per-snapshot grid_cell_id values are unique
- **WHEN** ordered cell rows are inserted for a snapshot
- **THEN** `grid_cell_id` values are unique within the snapshot
- **THEN** an attempt to insert a duplicate `(grid_snapshot_id, grid_cell_id)` is rejected by the unique constraint.

#### Scenario: canonical_ordinal is unique and contiguous 1..N
- **WHEN** ordered cell rows are inserted for a snapshot
- **THEN** `canonical_ordinal` values are unique within the snapshot AND form a contiguous integer sequence `1..N`
- **THEN** the ordinal order matches the deterministic cell ordering used to compute `grid_signature` (the same iteration order the producer applies)
- **THEN** an insert violating uniqueness or a non-contiguous ordinal range is rejected.

#### Scenario: Live signature verification at registration matches producer computation
- **WHEN** a candidate grid is registered
- **THEN** the candidate's `grid_signature` is recomputed via a live producer path on at least one representative canonical product cycle
- **THEN** the registry-computed value and the live-producer-computed value MUST be equal
- **THEN** on mismatch, registration MUST fail closed with both values reported in the structured error
- **THEN** for the backfill of `ifs_0p25` and `gfs_0p25` the acceptance baseline is the live signature `6c008901b8b7…` observed on the 2026-07-06 node-27 baseline.

### Requirement: Snapshot cross-references met.canonical_met_product via FK
The registry SHALL be the single immutable authority for a grid definition, and `met.canonical_met_product` SHALL cross-reference registered snapshots via a nullable foreign key rather than storing an independent grid definition copy.

#### Scenario: canonical_met_product rows reference a registered snapshot
- **WHEN** a `met.canonical_met_product` row is inserted with a non-NULL `grid_snapshot_id`
- **THEN** the value MUST reference a `met.canonical_grid_snapshot.grid_snapshot_id` row that exists in the registry
- **THEN** inserting a `met.canonical_met_product` row referencing an unregistered `grid_snapshot_id` MUST be rejected by the foreign-key constraint.

#### Scenario: Grid definitions are not stored independently in both tables
- **WHEN** a `met.canonical_met_product` row carries `grid_definition_uri` alongside `grid_snapshot_id`
- **THEN** the `grid_definition_uri` value MUST match the referenced snapshot's `grid_definition_uri`
- **THEN** the referenced snapshot's `grid_definition_checksum` is the authoritative content hash; `met.canonical_met_product.grid_definition_uri` is a display/cross-check field, not an independent copy.

#### Scenario: Snapshot grid_definition_uri cannot be modified by canonical_met_product inserts
- **WHEN** a `met.canonical_met_product` insert references an existing snapshot
- **THEN** the snapshot's `grid_definition_uri` and `grid_definition_checksum` are NOT modified by the insert
- **THEN** any attempt by product-row insertion to overwrite snapshot identity fields is rejected.

### Requirement: Download bbox is pinned into snapshot identity
The registry SHALL pin the download bbox that generated the grid into Grid Snapshot identity and fail closed when a deployment's download bbox is inconsistent with the registered snapshot.

#### Scenario: Snapshot pins the bbox that produced the cell ids
- **WHEN** a grid is registered
- **THEN** the snapshot records the download bbox (`NHMS_DOWNLOAD_BBOX_SOUTH`/`NORTH`/`WEST`/`EAST`, defaulting to 63–145°E / 8–64°N) in force when its `grid_cell_id`s were generated
- **THEN** the bbox is part of the snapshot identity because `grid_cell_id` is a flat index string derived from the bbox clip.

#### Scenario: Deployment bbox inconsistent with registry fails closed via reusable guard
- **WHEN** a deployment's `NHMS_DOWNLOAD_BBOX_*` values differ from the bbox pinned in the registered snapshot
- **THEN** the reusable guard function `verify_download_bbox_matches_registry()` fails closed with a structured error identifying expected and actual bbox values
- **THEN** the guard function is stateless: it reads `NHMS_DOWNLOAD_BBOX_*` via `china_buffered_bbox_from_env` and returns/raises without side-effects on the registry or any downstream store
- **THEN** the drifted `grid_cell_id`s are not silently accepted by any caller of the guard.

#### Scenario: Reusable guard is importable by producer preflight and platform readiness
- **WHEN** the producer preflight or the platform-readiness checker needs to validate the deployment bbox
- **THEN** it imports and invokes the same `verify_download_bbox_matches_registry()` function as the registry
- **THEN** all callers observe identical fail-closed behavior on mismatch
- **THEN** an independent reimplementation of the bbox check outside the reusable function is forbidden.

### Requirement: Registration is append-only and immutable
The registry SHALL treat every registered Grid Snapshot as append-only and immutable.

#### Scenario: Snapshots are never updated in place
- **WHEN** a request would modify or delete an already-registered snapshot's identity fields (`grid_signature`, `grid_definition_uri`, `grid_definition_checksum`, bbox, `canonical_grid_key`, per-cell rows)
- **THEN** the registry rejects the mutation
- **THEN** the existing snapshot and its ordered cells remain unchanged.

#### Scenario: Registry API rejects in-place grid_signature replacement
- **WHEN** the registry API receives a request that would replace an existing snapshot's `grid_signature` without creating a new snapshot version
- **THEN** the request is rejected as forbidden
- **THEN** the caller must instead register a NEW snapshot version (see the grid-drift-lifecycle capability), preserving the prior snapshot immutably.

#### Scenario: Cells are inserted atomically with the snapshot
- **WHEN** a snapshot is registered
- **THEN** its ordered cell rows are inserted atomically with the parent snapshot within a single database transaction
- **THEN** a mid-write failure between the snapshot INSERT and the last cell INSERT rolls back the entire transaction
- **THEN** after rollback, zero rows exist in `met.canonical_grid_snapshot` and `met.canonical_grid_cell` for that `grid_snapshot_id`; a partial registration does not leave a snapshot without its complete ordered-cell set.

### Requirement: Backfill of live ifs_0p25 and gfs_0p25 shared canonical_grid_key
The registry initial state SHALL contain snapshots for both `ifs_0p25` and `gfs_0p25` sharing one `canonical_grid_key`.

#### Scenario: Live ifs_0p25 and gfs_0p25 share one canonical_grid_key
- **WHEN** the registry is populated for the first time from the current live grid definitions on node-27
- **THEN** a snapshot is registered for `source_id=IFS`, `grid_id=ifs_0p25`, and a snapshot is registered for `source_id=gfs`, `grid_id=gfs_0p25`
- **THEN** both snapshots MUST share one `canonical_grid_key`
- **THEN** both snapshots MUST carry the observed signature `6c008901b8b7…` on the 2026-07-06 node-27 baseline
- **THEN** both snapshots MUST carry the pinned bbox 63–145°E / 8–64°N and native resolution 0.25°.

