## 1. Mapping Mode and Asset Contract

- [ ] 1.1 Define the authoritative direct-grid JSON asset contract and repository interface, including manifest path/fields, binding checksum, model input package identity, `.sp.att` checksum, `applicable_source_ids`, `grid_id`, `grid_cell_id`, and grid signature algorithm.
- [ ] 1.2 Add model/basin asset metadata parsing for `forcing_mapping_mode`, defaulting absent values or explicit `idw` to the legacy IDW path and rejecting unsupported values, with resolver unit tests.
- [ ] 1.3 Add validation for direct-grid station indexes, filenames, grid identity/signature, source scope, WGS84 longitude normalization, model input identity, and `.sp.att` `FORC` references, with failure tests.

## 2. Direct-Grid Producer Path

- [ ] 2.1 Add direct-grid mode resolution to `workers/forcing_producer/producer.py` while keeping existing IDW behavior unchanged for legacy assets.
- [ ] 2.2 Check and update persistence compatibility for direct-grid mappings, including `met.interp_weight.method='direct_grid'`, `weight=1.0`, replacement semantics, indexes/constraints, and integration tests.
- [ ] 2.3 Load direct-grid station bindings and materialize them as exact one-cell mappings (`method='direct_grid'`, `weight=1.0`) or equivalent internal bindings, with tests proving IDW neighbor search is not called.
- [ ] 2.4 Generate direct-grid station rows whose values equal bound canonical `grid_cell_id` values, preserving existing canonical physical conversions and adding direct-value fixture tests.
- [ ] 2.5 Use direct-grid required `grid_cell_id`s to limit retained canonical values in the existing `_read_canonical_field` path; defer deeper NetCDF/xarray lazy indexed-read optimization to a separate performance task if profiling shows it is needed.

## 3. Persistence, Lineage, and Compatibility

- [ ] 3.1 Extend repository/store interfaces to load direct-grid mapping metadata from authoritative model asset manifests, allowing database mirrors only as derived cache.
- [ ] 3.2 Write SHUD `.tsd.forc` and per-station CSV packages for direct-grid rows, keeping SHUD CSV columns to `Precip`, `Temp`, `RH`, `Wind`, and `RN` while persisting pressure only outside the SHUD CSV contract.
- [ ] 3.3 Record `forcing_mapping_mode`, binding identity/checksum, model input identity, `.sp.att` checksum, grid signature, source scope, and spatial mapping method in forcing lineage and package manifest metadata.
- [ ] 3.4 Update idempotency/freshness checks so mapping mode, binding identity, grid signature, or model input identity changes invalidate an existing ready forcing version for the same model/source/cycle.
- [ ] 3.5 Ensure explicit `direct_grid` validation failures never fallback to IDW and leave no ready forcing outputs, with no-ready-output regression tests.
- [ ] 3.6 Preserve existing IDW tests and behavior for model assets without `forcing_mapping_mode` or with explicit `forcing_mapping_mode="idw"`.

## 4. SHUD Runtime Integration

- [ ] 4.1 Validate that direct-grid forcing packages use standard multi-station SHUD staging and reject fallback single-station `.sp.att` rewrites when lineage declares `forcing_mapping_mode="direct_grid"`.
- [ ] 4.2 Add runtime checks that staged `.sp.att` `FORC` values are within the `.tsd.forc` `ID` column values for direct-grid packages.

## 5. End-to-End Evidence and Documentation

- [ ] 5.1 Add a compact end-to-end direct-grid fixture covering mode resolution, binding validation, exact value generation, SHUD package formatting, lineage, runtime staging validation, and idempotency.
- [ ] 5.2 Update forcing production and model asset documentation with migration workflow, rollback by asset version, source scope, grid signature rules, Press handling, and the reason canonical conversion remains mandatory.
