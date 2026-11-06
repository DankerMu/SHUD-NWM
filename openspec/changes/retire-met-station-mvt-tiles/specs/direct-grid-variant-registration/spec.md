## MODIFIED Requirements

### Requirement: Registration writes the met.met_station cell-station mirror with active_flag false

The registration surface SHALL write the `met.met_station` cell-station mirror rows for the variant with `active_flag` explicitly set to `false`, using
`station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"`, with `station_role='direct_grid_cache'` and the derived-cache identity `properties_json` shape
that the runtime producer's mirror writer (`workers/forcing_producer/store.py:ensure_direct_grid_met_stations`) requires, SHALL fail closed on a `station_id`
collision whose bound identity differs, and SHALL NOT write any other `met.*` runtime-producer rows (§8.1).

#### Scenario: Mirror rows are written inactive with the mapping-asset station identity

- **WHEN** the registration surface writes the cell-station mirror for a variant
- **THEN** each mirror row uses `station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"` (the builder's `assign_station_id_from_mapping_asset_identity` output) and the shared `basin_version_id`
- **THEN** each mirror row has `active_flag=false` set explicitly, not left to the column default (`met.met_station.active_flag` defaults to `true`)
- **THEN** each mirror row's `geom` is written in SRID 4490 (CGCS2000) as the derived mirror of the WGS84 binding coordinate.

#### Scenario: Mirror rows carry the producer's derived-cache identity shape

- **WHEN** the registration surface writes a mirror row for a variant
- **THEN** the row sets `station_role='direct_grid_cache'`, never the `'forcing_proxy'` column default (`db/migrations/000005_met.sql:53`)
- **THEN** the row's `properties_json` carries the derived-cache identity fields the runtime producer's conditional upsert predicate requires —
  `derived_cache: true`, `forcing_mapping_mode: 'direct_grid'`, `binding_checksum`, `model_input_package_id`, `grid_signature`, `contract_grid_id`, and
  `grid_id` — plus the binding-derived fields `binding_uri`, `sp_att_path`, `sp_att_checksum`, `grid_cell_id`, `shud_forcing_index`, and `forcing_filename` (the
  object-store station-series read path resolves stations by `forcing_filename`), matching `workers/forcing_producer/store.py:ensure_direct_grid_met_stations`
  (lines 347-379)
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

- **WHEN** registration completes and the active station set `met.met_station WHERE basin_version_id=… AND active_flag=true` is read
- **THEN** none of the newly registered variant's mirror rows are returned, because they are `active_flag=false`
- **THEN** the mirror rows remain `active_flag=false` across a subsequent pre-cutover direct-grid production run, because the producer's mirror maintenance preserves the registration-owned flag (see the `fixed-station-forcing-production` delta in this change)
- **THEN** the shadow-window display shows a single station track, not mixed legacy and direct-grid stations.

#### Scenario: Registration writes no runtime-producer rows

- **WHEN** the registration surface writes the variant and its mirror
- **THEN** no `met.interp_weight`, `met.forcing_version`, cycle-dated `.tsd.forc`, or station weather CSV is produced, because those are runtime-producer outputs (§8.1/§8.3)
- **THEN** only the `core.model_instance` variant row and the `met.met_station` mirror rows are written.
