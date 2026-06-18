## 1. Mapping Mode and Asset Contract

- [x] 1.1 (#540) Define the authoritative direct-grid JSON asset contract and repository interface, including manifest path/fields, binding checksum, model input package identity, `.sp.att` checksum, `applicable_source_ids`, `grid_id`, `grid_cell_id`, and grid signature algorithm.
  - Required evidence: unit tests parse a valid manifest and preserve `binding_uri`, `binding_checksum`, `model_input_package_id`, `sp_att_path`, `sp_att_checksum`, `applicable_source_ids`, `grid_id`, `grid_signature`, and every station `grid_cell_id`.
  - Required evidence: unit tests reject missing manifest fields, missing station fields, empty source scope, current source not in `applicable_source_ids`, unsupported `forcing_mapping_mode`, unsafe `forcing_filename`, and non-contiguous `shud_forcing_index` with structured contract errors.
  - Required evidence: repository/store interface tests or protocol tests prove manifest-backed contracts are read through a single authoritative entrypoint and DB mirrors are not treated as authoritative direct-grid sources in #540.
  - Non-goal for #540: no producer behavior switch, no direct-grid value generation, no `met.interp_weight` persistence change, no SHUD runtime staging change.
- [x] 1.2 (#541) Add model/basin asset metadata parsing for `forcing_mapping_mode`, defaulting absent values or explicit `idw` to the legacy IDW path and rejecting unsupported values, with resolver unit tests.
  - Required evidence: producer tests prove legacy assets with no contract still use the existing IDW station/weight/output path.
  - Required evidence: producer tests prove explicit `forcing_mapping_mode="idw"` uses the same IDW path and does not require direct-grid bindings.
  - Required evidence: producer tests prove explicit `forcing_mapping_mode="direct_grid"` enters a fail-closed direct-grid validation gate in #541 and does not call IDW station loading, IDW weight computation, or ready output writing.
  - Required evidence: unsupported/malformed mapping mode errors update failure state and do not create a ready forcing version.
  - Non-goal for #541: no successful direct-grid value generation, no grid signature comparison, no `.sp.att` validation, no `met.interp_weight` direct-grid persistence, no package/lineage/idempotency changes.
- [ ] 1.3 (#542) Add validation for direct-grid station indexes, filenames, grid identity/signature, source scope, WGS84 longitude normalization, model input identity, and `.sp.att` `FORC` references, with failure tests.
  - Required evidence: contract/parser tests reject missing station fields, duplicate or non-contiguous `shud_forcing_index`, unsafe or duplicate `forcing_filename`, invalid longitude/latitude, and current source not present in `applicable_source_ids`.
  - Required evidence: producer validation tests reject binding checksum mismatch, `model_input_package_id` mismatch, `.sp.att` checksum mismatch, canonical `grid_id` mismatch, canonical `grid_signature` mismatch, and `.sp.att FORC` values that are zero, negative, missing, non-integer, or outside the direct-grid `shud_forcing_index` set.
  - Required evidence: all direct-grid validation failures include structured expected/actual or field/source details, mark the cycle `failed_forcing`, create no ready forcing version/package/timeseries/component records, and do not call IDW station loading or weight computation as fallback.
  - Required evidence: direct-grid validation runs before existing-ready reuse so stale IDW-ready forcing versions cannot return `already_done` when a direct-grid asset is invalid.
  - Non-goal for #542: no successful direct-grid row generation, no `met.interp_weight` direct-grid persistence, no SHUD package writes for direct-grid success, no lineage/idempotency freshness change, and no SHUD runtime staging change.

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
