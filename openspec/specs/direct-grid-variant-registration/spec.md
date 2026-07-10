# direct-grid-variant-registration Specification

## Purpose
TBD - created by archiving change source-specific-model-variant-routing. Update Purpose after archive.
## Requirements
### Requirement: Direct-grid variant is registered as a new inactive model_instance row

The registration surface SHALL register a built direct-grid variant as a NEW `core.model_instance` row at basin × `canonical_grid_key` grain, with `active_flag=false` and `lifecycle_state='inactive'`, carrying the direct-grid contract under `resource_profile.direct_grid_forcing`, and SHALL NOT activate or route the variant. Variant identity within the grain SHALL be keyed on the built mapping asset identity (`model_input_package_id` + `binding_checksum` from the §7.2 manifest): the grain deduplicates sources — one variant row per built asset, never per-source rows — while successive built generations (fix-forward M1→M1′) register as distinct rows in the same grain.

#### Scenario: A new inactive variant row is created

- **WHEN** the registration surface registers a built direct-grid variant for a basin
- **THEN** a new `core.model_instance` row is inserted with a new `model_id`, the baseline's `basin_version_id`, and the direct-grid contract placed under `resource_profile.direct_grid_forcing`
- **THEN** the new row has `active_flag=false` and `lifecycle_state='inactive'`
- **THEN** registration performs no lifecycle activation and re-publishes no scheduler manifest, because the variant is inert until a separate activation.

#### Scenario: The grain deduplicates sources, not built generations

- **WHEN** the registration surface selects the variant identity for a `(basin, canonical_grid_key)`
- **THEN** exactly one variant row exists per `(basin_version_id, canonical_grid_key, built mapping asset identity)` — sources that map to the same `canonical_grid_key` share that one row rather than producing per-source rows
- **THEN** multiple rows for one `(basin_version_id, canonical_grid_key)` MAY coexist only as distinct built generations (fix-forward lineage), with at most one of them `active` at a time (the existing partial unique index on `basin_version_id`).

#### Scenario: canonical_grid_key is taken from the registered snapshot and persisted on the variant row

- **WHEN** the registration surface needs the `canonical_grid_key` for the grain
- **THEN** it resolves the registered `met.canonical_grid_snapshot` row for the built variant (via the built manifest's `grid_signature`/`grid_id` identity or an explicit `grid_snapshot_id` registration input) and copies that row's `canonical_grid_key` verbatim, never recomputing the key itself
- **THEN** the copied key is persisted on the variant row at `resource_profile.canonical_grid_key` — top-level, alongside and not inside the parser-validated `direct_grid_forcing` block — so the grain and idempotency lookups are queries over existing columns with no new table or migration.

#### Scenario: Registration is idempotent per built asset

- **WHEN** the same built variant (same `basin_version_id`, `canonical_grid_key`, `model_input_package_id`, and `binding_checksum`) is registered twice
- **THEN** the second registration returns the existing variant identity and inserts no duplicate `core.model_instance` row
- **THEN** the mirror rows are reconciled by `station_id` without duplication.

#### Scenario: A fix-forward successor registers as a new row in the same grain

- **WHEN** a rebuilt direct-grid variant with a different built mapping asset identity (e.g. a new `model_input_package_id` from a §11.2 fix-forward rebuild) is registered for a `(basin_version_id, canonical_grid_key)` that already has a registered variant
- **THEN** registration inserts a NEW `core.model_instance` row with a new `model_id` rather than returning the prior generation's identity
- **THEN** the prior generation's row is left untouched in its current lifecycle state (active or superseded), preserving immutable fix-forward lineage.

#### Scenario: Registration never produces an active or routed variant

- **WHEN** registration completes for a variant
- **THEN** the variant does not appear in the scheduler dispatch candidate set, because it is `lifecycle_state='inactive'`
- **THEN** no `core.model_instance` row for the basin changes its `active_flag` or `lifecycle_state` as a result of registration.

### Requirement: Sources sharing one canonical_grid_key share a single variant

The registration surface SHALL register ONE variant row per built asset for two sources when they resolve to the same `canonical_grid_key`, listing both normalized ids in `resource_profile.direct_grid_forcing.applicable_source_ids`, and SHALL register separate variants when the sources do not share a `canonical_grid_key`.

#### Scenario: IFS and GFS with equal signature share one variant

- **WHEN** IFS and GFS resolve to one `canonical_grid_key` under the `source-shared-binding-eligibility` decision
- **THEN** a single variant is registered whose `applicable_source_ids` lists both normalized source ids (`gfs` and `IFS`)
- **THEN** no second geometrically identical variant is registered for the other source.

#### Scenario: Sources without a shared canonical_grid_key are registered separately

- **WHEN** two sources do not share a `canonical_grid_key` (different `grid_signature`, or sharing not granted by the registry)
- **THEN** each source is registered as its own variant with its own `applicable_source_ids`
- **THEN** the registration surface does not merge them into one variant.

### Requirement: The legacy IDW model row is retained immutably

The registration surface SHALL retain the basin's legacy IDW `core.model_instance` row unchanged when registering a direct-grid variant, never deleting or mutating it (INV-1).

#### Scenario: Legacy row is untouched by variant registration

- **WHEN** a direct-grid variant is registered for a basin whose legacy IDW model exists
- **THEN** the legacy `core.model_instance` row's `model_id`, `active_flag`, `lifecycle_state`, `model_package_uri`, and `resource_profile` are unchanged
- **THEN** the legacy row is retained as calibration/replay lineage and is not deleted.

#### Scenario: Registration does not deactivate the currently active legacy model

- **WHEN** the basin's legacy IDW model is currently `active` and a direct-grid variant is registered
- **THEN** the legacy model remains `active`, because registration is not activation
- **THEN** the direct-grid variant is registered as `inactive` alongside it.

### Requirement: Registration writes the met.met_station cell-station mirror with active_flag false

The registration surface SHALL write the `met.met_station` cell-station mirror rows for the variant with `active_flag` explicitly set to `false`, using `station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"`, with `station_role='direct_grid_cache'` and the derived-cache identity `properties_json` shape that the runtime producer's mirror writer (`workers/forcing_producer/store.py:ensure_direct_grid_met_stations`) requires, SHALL fail closed on a `station_id` collision whose bound identity differs, and SHALL NOT write any other `met.*` runtime-producer rows (§8.1).

#### Scenario: Mirror rows are written inactive with the mapping-asset station identity

- **WHEN** the registration surface writes the cell-station mirror for a variant
- **THEN** each mirror row uses `station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"` (the builder's `assign_station_id_from_mapping_asset_identity` output) and the shared `basin_version_id`
- **THEN** each mirror row has `active_flag=false` set explicitly, not left to the column default (`met.met_station.active_flag` defaults to `true`)
- **THEN** each mirror row's `geom` is written in SRID 4490 (CGCS2000) as the derived mirror of the WGS84 binding coordinate.

#### Scenario: Mirror rows carry the producer's derived-cache identity shape

- **WHEN** the registration surface writes a mirror row for a variant
- **THEN** the row sets `station_role='direct_grid_cache'`, never the `'forcing_proxy'` column default (`db/migrations/000005_met.sql:53`)
- **THEN** the row's `properties_json` carries the derived-cache identity fields the runtime producer's conditional upsert predicate requires — `derived_cache: true`, `forcing_mapping_mode: 'direct_grid'`, `binding_checksum`, `model_input_package_id`, `grid_signature`, `contract_grid_id`, and `grid_id` — plus the binding-derived fields `binding_uri`, `sp_att_path`, `sp_att_checksum`, `grid_cell_id`, `shud_forcing_index`, and `forcing_filename` (the object-store station-series read path resolves stations by `forcing_filename`), matching `workers/forcing_producer/store.py:ensure_direct_grid_met_stations` (lines 347-379)
- **THEN** a subsequent direct-grid production run's mirror upsert recognizes these rows as the same derived direct-grid cache binding and reconciles them instead of failing closed with its collision error.

#### Scenario: Mirror rows are bound to the canonical grid snapshot

- **WHEN** the registration surface writes a mirror row for a variant
- **THEN** the row's `grid_snapshot_id` FK references the variant's registered canonical grid snapshot
- **THEN** legacy IDW stations for the basin retain `grid_snapshot_id IS NULL`, so the two station sets are distinguishable without a `model_id` column.

#### Scenario: A station_id collision with a different bound identity fails closed

- **WHEN** a mirror write targets an existing `station_id` whose bound identity differs from the incoming row (a different `binding_checksum`, `model_input_package_id`, `grid_signature`, `grid_id`, `grid_snapshot_id`, or coordinates)
- **THEN** registration fails closed with no row mutated, using a conditional upsert plus an affected-row-count check (the same fail-closed collision policy as the runtime producer's mirror writer, docs §7.4; `workers/forcing_producer/store.py:400-429`)
- **THEN** an unconditional `ON CONFLICT (station_id) DO UPDATE` overwrite is not used, so a foreign row is never silently clobbered.

#### Scenario: The mirror never lands active at registration

- **WHEN** registration completes and the station-MVT query runs `met.met_station WHERE basin_version_id=… AND active_flag=true`
- **THEN** none of the newly registered variant's mirror rows are returned, because they are `active_flag=false`
- **THEN** the mirror rows remain `active_flag=false` across a subsequent pre-cutover direct-grid production run, because the producer's mirror maintenance preserves the registration-owned flag (see the `fixed-station-forcing-production` delta in this change)
- **THEN** the shadow-window display shows a single station track, not mixed legacy and direct-grid stations.

#### Scenario: Registration writes no runtime-producer rows

- **WHEN** the registration surface writes the variant and its mirror
- **THEN** no `met.interp_weight`, `met.forcing_version`, cycle-dated `.tsd.forc`, or station weather CSV is produced, because those are runtime-producer outputs (§8.1/§8.3)
- **THEN** only the `core.model_instance` variant row and the `met.met_station` mirror rows are written.

