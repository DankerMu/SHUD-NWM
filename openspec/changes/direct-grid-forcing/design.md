## Context

The current forcing producer loads fixed SHUD forcing stations from `met.met_station`, computes or reuses IDW weights from canonical IFS/GFS grid points, and writes SHUD `.tsd.forc` plus per-station CSV files. This is correct for legacy CMFD/LDAS-derived station layouts, but national-scale operation multiplies the cost of runtime interpolation across many basins and cycles.

The new basin model assets will precompute the spatial ownership between SHUD triangles and IFS/GFS 0.25 degree grid cells. In those migrated assets, the station list is no longer a legacy CMFD/LDAS station set; it is a source-grid forcing station set whose `shud_forcing_index` matches the already-rewritten `.sp.att` `FORC` values. Runtime should therefore perform exact canonical grid-cell lookup instead of IDW.

Canonical conversion remains required. IFS/GFS raw products still need precipitation/radiation de-accumulation, humidity derivation, wind speed composition, unit normalization, lineage, and QC before SHUD forcing is written.

## Goals / Non-Goals

**Goals:**

- Select `idw` or `direct_grid` per basin/model asset, not by global deployment toggle.
- Make `direct_grid` opt-in and fail-closed: if declared direct-grid metadata is incomplete or stale, forcing production fails instead of falling back to IDW.
- Keep legacy IDW behavior unchanged for old basin assets.
- Reuse the existing SHUD forcing output contract: contiguous forcing indexes, `.tsd.forc`, per-station CSV files with `Precip/Temp/RH/Wind/RN`, `met.forcing_version`, and `met.forcing_station_timeseries`. Pressure can remain persisted metadata/timeseries but is not added to SHUD station CSV files.
- Avoid loading or processing unnecessary global grid cells once a direct-grid binding provides the required `grid_cell_id` set.

**Non-Goals:**

- Do not change canonical physical conversion rules for GFS/IFS.
- Do not define the offline GIS algorithm that rewrites `.sp.att` `FORC`; that is an upstream model asset build step.
- Do not support implicit direct-grid activation merely because some station rows contain `grid_cell_id`.
- Do not remove IDW support.

## Decisions

### 1. Explicit per-asset mode resolution

`forcing_mapping_mode` is resolved from the model/input asset manifest associated with the selected `model_id` and `basin_version_id`. Supported values are `idw` and `direct_grid`.

- If absent, mode defaults to `idw` for compatibility.
- If present and unsupported, production fails with a contract error.
- If `direct_grid`, all direct-grid validation must pass before any forcing output is marked ready.

Alternative considered: infer mode from station metadata. Rejected because partial or stale station bindings could silently change results.

### 2. Direct-grid bindings are part of the model asset contract

The direct-grid binding is a JSON artifact referenced by the model/input asset manifest. The manifest is authoritative; database rows may mirror it for query performance but cannot override it. The manifest MUST provide `forcing_mapping_mode`, `binding_uri`, `binding_checksum`, `model_input_package_id` or equivalent immutable package identity, `.sp.att` path/checksum, `applicable_source_ids`, `grid_id`, and `grid_signature`.

Each direct-grid forcing station binding MUST provide:

- `station_id`
- `shud_forcing_index`
- `forcing_filename`
- longitude/latitude in WGS84-compatible coordinates
- `x`, `y`, `z` or equivalent SHUD output coordinates
- `grid_id`
- `grid_cell_id`

Source applicability is manifest-level through `applicable_source_ids`, such as `["GFS", "IFS"]` when the same normalized 0.25 degree binding is valid for both sources, or a single-source list when it is source-specific. Per-station source variation is out of scope for this change.

The model asset manifest MUST identify the `.sp.att` file version whose `FORC` values were rewritten to these station indexes.

Alternative considered: keep bindings only in database tables. Rejected as the only source because direct-grid correctness depends on the file package and `.sp.att`; database rows may be refreshed independently. Database persistence can mirror the contract, but the asset manifest remains the traceable source.

### 3. Reuse interpolation infrastructure with direct weights first

The initial implementation should be able to persist direct bindings into the existing weight path as `method='direct_grid'`, `weight=1.0`, and one `grid_cell_id` per station/variable. This minimizes downstream changes to aggregation and persistence. A later cleanup can introduce a separate binding table if needed.

Alternative considered: bypass `met.interp_weight` entirely. Rejected for the first slice because it duplicates row generation and lineage paths that already understand station/variable/grid-cell mappings.

### 4. Grid signature validation protects against stale bindings

Direct-grid mode MUST compare the binding's declared `grid_id` and `grid_signature` with the canonical product grid used for the current cycle/source. The `grid_signature` is the SHA-256 content signature already derived from the canonical grid definition: schema version plus ordered longitude/latitude coordinate arrays or ordered cell list after applying the producer's longitude normalization rules. A mismatch fails production before values are read. GFS `0..360` source longitude and IFS `-180..180` source longitude MUST normalize to the same WGS84 geometry convention for station coordinates, while `grid_cell_id` remains the authoritative lookup key.

### 5. Runtime exact lookup still writes SHUD station outputs

For direct-grid mode, the producer reads canonical values for required `grid_cell_id`s, maps each station to its bound cell value, derives combined output variables such as wind speed through the existing canonical-to-forcing path, and writes the same SHUD package shape as IDW mode. The SHUD station CSV remains the five-variable contract `Precip/Temp/RH/Wind/RN`; pressure may still be persisted in `met.forcing_station_timeseries` and lineage when available. The value differs only by spatial mapping method: exact cell lookup rather than weighted neighboring cells.

## Risks / Trade-offs

- **Risk: direct-grid assets and `.sp.att` get out of sync** → Validate contiguous forcing indexes, station count, referenced `FORC` range, grid signature, binding checksum, model input package identity, and `.sp.att` checksum before publishing a ready forcing version.
- **Risk: source grid definitions drift across canonical products** → Treat grid signature mismatch as a hard blocker and include expected/actual signatures in the failure details.
- **Risk: exact nearest-cell mapping creates blockier forcing than IDW** → Accept this as the explicit scientific trade-off for migrated assets; retain IDW assets where smoothing is desired.
- **Risk: direct-grid path accidentally bypasses physical conversions** → Keep canonical products as the only allowed input; direct-grid applies only after canonical conversion.
- **Risk: large station counts increase SHUD file IO** → Require basin-scoped grid subsets rather than global shapefile ingestion, and record station counts in validation evidence.

## Migration Plan

1. Add metadata parsing and validation for `forcing_mapping_mode`, direct-grid JSON bindings, `applicable_source_ids`, model input identity, `.sp.att` checksum, `grid_id`, and `grid_signature`.
2. Check or update persistence compatibility for `method='direct_grid'`, `weight=1.0`, indexes, and idempotent replacement semantics.
3. Implement direct binding load/persist behavior as one-cell weights with `method='direct_grid'`.
4. Add exact grid-cell value extraction and reuse existing SHUD package formatting.
5. Add model asset validation that checks station indexes and `.sp.att` `FORC` references.
6. Add runtime staging validation so direct-grid packages use the standard multi-station SHUD forcing path and never trigger single-station `.sp.att` rewrites.
7. Migrate basins gradually by publishing new model/input versions that declare `direct_grid`; old versions continue to run with IDW.
8. Rollback by deactivating the direct-grid model/input version and reactivating the prior IDW asset version.
