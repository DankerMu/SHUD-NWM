## ADDED Requirements

### Requirement: Binding and manifest match the existing parser contract
The mapping builder SHALL emit a direct-grid binding and manifest whose fields satisfy the existing contract parser in `workers/forcing_producer/direct_grid_contract.py` exactly, placed in the `resource_profile.direct_grid_forcing` nested section.

#### Scenario: Manifest carries all required identity fields
- **WHEN** the builder emits the manifest
- **THEN** the manifest provides `forcing_mapping_mode` (with value `direct_grid`), `binding_uri`, `binding_checksum`, `model_input_package_id`, `sp_att_path`, `sp_att_checksum`, `applicable_source_ids`, `grid_id`, `grid_signature`, and `station_bindings`
- **THEN** the manifest exposes the station bindings under the canonical field name `station_bindings` (per §7.2), which is non-empty
- **THEN** the contract is placed in the `resource_profile.direct_grid_forcing` nested section
- **THEN** the emitted manifest parses cleanly through the existing direct-grid contract parser.

#### Scenario: Station bindings carry all required fields
- **WHEN** the builder emits station bindings
- **THEN** each binding provides `station_id`, `shud_forcing_index`, `forcing_filename`, `longitude`, `latitude`, `x`, `y`, `z`, `grid_id`, and `grid_cell_id`
- **THEN** `shud_forcing_index` values are 1-based, contiguous, and unique
- **THEN** each `grid_cell_id` is unique within the binding and exists in the registered grid snapshot.

#### Scenario: Manifest and binding artifact are cross-consistent (G5)
- **WHEN** the builder emits both the manifest and the separately-emitted binding artifact referenced by `binding_uri`
- **THEN** the manifest's `binding_checksum` equals the SHA-256 of the binding artifact bytes referenced by `binding_uri`, recomputed at build time from the emitted bytes
- **THEN** the manifest's `station_bindings` row set equals the binding artifact's row set element-for-element (same `station_id`, `shud_forcing_index`, `grid_cell_id`, and coordinates after 12-decimal rounding)
- **THEN** the manifest's `sp_att_checksum` equals the SHA-256 of the emitted variant `.sp.att` bytes at `sp_att_path`
- **THEN** the builder fails closed as a G5 blocker on any mismatch, writing no output.

### Requirement: Station identity and filenames are safe and immutable
The mapping builder SHALL embed the immutable mapping-asset identity in each `station_id` and produce safe, pathless `forcing_filename`s that never collide with reserved names.

#### Scenario: station_id embeds immutable mapping-asset identity
- **WHEN** the builder assigns a `station_id`
- **THEN** the `station_id` embeds the immutable mapping-asset identity and is never reused across mapping versions
- **THEN** the identity is chosen so the database mirror fails closed on collision rather than reusing an id across versions.

#### Scenario: forcing_filename is safe, pathless, and collision-free
- **WHEN** the builder assigns a `forcing_filename`
- **THEN** the filename is safe, pathless, and case-fold unique across the binding
- **THEN** the filename does not collide with `qhh.tsd.forc`, the manifest, debug artifacts, or model-input filenames, including on case-insensitive filesystems
- **THEN** the filename is not derived from rounded coordinates.

### Requirement: Station coordinates and derived fields obey the tolerance rule
The mapping builder SHALL set station coordinates equal to the registered cell center under the grid-signature rounding rule and SHALL make `x`/`y` recomputable and `z` policy-driven.

#### Scenario: Station lon/lat equal the registered cell center under rounding
- **WHEN** the builder sets a station `longitude` and `latitude`
- **THEN** they equal the registered cell center where equality is compared after the same 12-decimal rounding used for the grid signature
- **THEN** float-literal equality is never used, because live coordinates carry ~1e-7° noise.

#### Scenario: Station coordinates declare an explicit WGS84 basis and cross-basis equality is forbidden
- **WHEN** the builder emits station `longitude` and `latitude` and any equality assertion against derived mirrors
- **THEN** the binding and manifest declare and record the coordinate basis as WGS84 (matching the registry basis per docs §7.3), for example via a `coordinate_reference_system` field or an equivalent explicit declaration
- **THEN** the equality assertion between station lon/lat and the registered cell center is performed in the WGS84 basis (both operands WGS84, compared after 12-decimal rounding)
- **THEN** cross-basis equality assertions between binding/registry coordinates (WGS84) and the `met.met_station.geom` mirror (SRID 4490 / CGCS2000) are forbidden — the DB mirror is derived (INV-3) and its numeric coordinates MUST NOT be used as the source-of-truth in any builder equality check without an explicit transform.

#### Scenario: x/y are recomputable and z follows the approved policy
- **WHEN** the builder sets station `x`, `y`, and `z`
- **THEN** `x` and `y` are recomputable from `longitude`/`latitude` and the model CRS
- **THEN** `z` follows the `z_policy` approved by the solver audit in change `cmfd-direct-grid-platform-readiness`.

### Requirement: Mapping stage does not emit runtime-producer artifacts
The mapping builder SHALL NOT emit any artifact that belongs to the runtime forcing producer (§8.1).

#### Scenario: Forbidden runtime outputs are never produced
- **WHEN** the builder writes the mapping variant
- **THEN** no cycle-dated `.tsd.forc` is produced
- **THEN** no per-station weather CSVs are produced
- **THEN** no `met.interp_weight`, `met.met_station`, or `met.forcing_version` database rows are written
- **THEN** no cycle lineage is produced, because those belong to the runtime producer.
