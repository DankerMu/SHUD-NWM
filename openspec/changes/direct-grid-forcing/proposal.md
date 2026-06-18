## Why

National-scale multi-basin forcing production currently spends runtime on IDW interpolation from IFS/GFS grids to legacy fixed forcing stations, and that interpolation can introduce avoidable spatial smoothing error. Basin model assets will now precompute the triangle-to-IFS/GFS grid ownership, so runtime forcing production can use deterministic source-grid values directly when a basin declares that contract.

## What Changes

- Add a per-basin/model forcing mapping mode resolver with explicit `direct_grid` and legacy `idw` behavior.
- Add a direct-grid station binding JSON contract for migrated basin assets: each SHUD forcing station is bound to an exact canonical `grid_id` and `grid_cell_id`, with binding identity, model input identity, source scope, and grid signature validation.
- Extend forcing production so `direct_grid` basins read only required canonical grid cells and write SHUD forcing values with no runtime spatial interpolation.
- Preserve current IDW behavior for basin assets that do not explicitly declare `direct_grid`.
- Add fail-closed validation so incomplete, stale, or mismatched direct-grid assets do not silently fall back to IDW.

## Capabilities

### New Capabilities
- `forcing-mapping-mode-resolution`: Resolve and validate whether a basin/model uses legacy IDW or direct-grid forcing mapping.
- `direct-grid-binding-contract`: Define the basin asset contract that binds SHUD forcing stations to IFS/GFS canonical grid cells.
- `direct-grid-forcing-production`: Produce SHUD forcing from canonical products by exact grid-cell lookup for direct-grid basins.

### Modified Capabilities
- None.

## Impact

- `workers/forcing_producer/producer.py`: mode resolution, validation, direct lookup path, lineage metadata, and targeted tests.
- `workers/forcing_producer/store.py` and related repository interfaces: load model/basin forcing mapping metadata and direct-grid asset manifest references.
- Model/input manifests and basin asset validation: record `forcing_mapping_mode`, `applicable_source_ids`, `grid_id`, `grid_signature`, binding identity, model input identity, station `grid_cell_id` bindings, and `.sp.att` `FORC` coverage checks.
- `met.interp_weight` or equivalent persisted mapping: may store direct bindings as `method='direct_grid'`, `weight=1.0` to reuse existing aggregation paths.
- Documentation/runbooks: clarify migration workflow, rollback to IDW by asset version, and the continued need for canonical physical conversions before SHUD output.
