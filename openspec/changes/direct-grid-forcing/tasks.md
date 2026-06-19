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
- [x] 1.3 (#542) Add validation for direct-grid station indexes, filenames, grid identity/signature, source scope, WGS84 longitude normalization, model input identity, and `.sp.att` `FORC` references, with failure tests.
  - Required evidence: contract/parser tests reject missing station fields, duplicate or non-contiguous `shud_forcing_index`, unsafe or duplicate `forcing_filename`, invalid longitude/latitude, and current source not present in `applicable_source_ids`.
  - Required evidence: producer validation tests reject binding checksum mismatch, `model_input_package_id` mismatch, `.sp.att` checksum mismatch, canonical `grid_id` mismatch, canonical `grid_signature` mismatch, and `.sp.att FORC` values that are zero, negative, missing, non-integer, or outside the direct-grid `shud_forcing_index` set.
  - Required evidence: all direct-grid validation failures include structured expected/actual or field/source details, mark the cycle `failed_forcing`, create no ready forcing version/package/timeseries/component records, and do not call IDW station loading or weight computation as fallback.
  - Required evidence: direct-grid validation runs before existing-ready reuse so stale IDW-ready forcing versions cannot return `already_done` when a direct-grid asset is invalid.
  - Non-goal for #542: no successful direct-grid row generation, no `met.interp_weight` direct-grid persistence, no SHUD package writes for direct-grid success, no lineage/idempotency freshness change, and no SHUD runtime staging change.

## 2. Direct-Grid Producer Path

- [ ] 2.1 Add direct-grid mode resolution to `workers/forcing_producer/producer.py` while keeping existing IDW behavior unchanged for legacy assets.
- [x] 2.2 (#543) Check and update persistence compatibility for direct-grid mappings, including `met.interp_weight.method='direct_grid'`, `weight=1.0`, replacement semantics, indexes/constraints, and integration tests.
  - Required evidence: migration/DDL tests prove `met.interp_weight` can represent `method='direct_grid'`, `weight=1.0`, `grid_cell_id`, and `grid_signature` without narrowing existing IDW rows or downstream membership joins.
  - Required evidence: store tests prove `load_interp_weights` round-trips direct-grid rows with `method`, `weight`, `grid_cell_id`, and `grid_signature` intact.
  - Required evidence: store tests prove replacing an existing IDW snapshot with direct-grid rows for the same `(source_id, grid_id, model_id)` removes stale IDW rows and does not leave mixed-method rows in that scope.
  - Required evidence: mixed-scope `upsert_interp_weights` still raises a stable store error before replacement, preserving no-partial-update semantics.
  - Non-goal for #543: no direct-grid binding load/materialization, no producer value generation, no SHUD package writes, no forcing lineage/idempotency change.
- [x] 2.3 (#544) Load validated direct-grid station bindings and materialize them as exact one-cell mappings (`method='direct_grid'`, `weight=1.0`) or equivalent internal bindings, with tests proving IDW neighbor search is not called.
  - Required evidence: producer tests prove a valid direct-grid contract creates exactly one mapping per `(station_id, variable)` using the contract `grid_cell_id`, `grid_id`, canonical `grid_signature`, `method='direct_grid'`, and `weight=1.0`.
  - Required evidence: producer tests prove a valid direct-grid contract persists mappings, writes no ready output/package/timeseries, and fails with a stable #545 value-generation boundary error/state until direct value rows are implemented.
  - Required evidence: producer tests prove explicit `direct_grid` does not call legacy station loading or `compute_idw_weights()` while materializing mappings.
  - Required evidence: producer tests prove direct-grid materialization persists through the existing interpolation-weight store path and replaces the same `(source_id, grid_id, model_id)` scope without mixed IDW/direct-grid snapshots.
  - Required evidence: producer tests prove `upsert_interp_weights` failure during direct-grid materialization leaves no forcing package, forcing version, station timeseries, or ready cycle state, and does not fallback to IDW.
  - Required evidence: validation failure before materialization writes no interpolation weights, no package, no forcing version, no station timeseries, and does not fallback to IDW.
  - Required evidence: IDW mode with absent metadata and explicit `forcing_mapping_mode="idw"` recomputes and replaces same-scope cached `direct_grid` rows instead of reusing them.
  - Required evidence: direct-grid met_station mirror rows are derived cache only, remain excluded from absent/explicit `idw` station loading, and do not replace legacy station authority.
  - Required evidence: direct-grid met_station mirror upsert fails closed before interpolation-weight insertion on station_id collision with non-derived, different-basin, or different-binding station rows, while same-basin same-binding mirror refresh remains idempotent.
  - Required evidence: direct-grid validation checks every canonical product's actual grid definition/order for the run before materialization, failing before `upsert_interp_weights` on same-source/grid metadata with mismatched ordered points.
  - Required evidence: direct-grid mode enforces `ForcingProducerConfig.max_station_count` against contract stations before materialization, with no legacy station load, no IDW fallback, and no ready outputs.
  - Required evidence: existing absent/explicit `idw` tests still pass and continue through IDW station/weight/output behavior.
  - Non-goal for #544: no direct-grid station value rows, no station coordinate revalidation beyond #542 validated contract consumption, no SHUD package writes for direct-grid success, no forcing lineage/idempotency freshness, and no SHUD runtime staging change.
- [x] 2.4 (#545) Generate direct-grid station rows whose values equal bound canonical `grid_cell_id` values, preserving existing canonical physical conversions and adding direct-value fixture tests.
  - Required evidence: producer tests prove generated direct-grid rows use the contract station ids and values from each station's bound canonical `grid_cell_id` for `PRCP`, `TEMP`, `RH`, `Rn`, and `wind`.
  - Required evidence: direct-value fixture uses station `qhh_forc_001 -> grid_cell_id 0` and `qhh_forc_002 -> grid_cell_id 1`; canonical cells provide PRCP `1.0/2.0`, TEMP `10.0/20.0`, RH `0.50/0.75`, Rn `100.0/200.0`, wind U `3.0/6.0`, wind V `4.0/8.0`; expected rows are PRCP/TEMP/RH/Rn/wind `1.0/10.0/0.50/100.0/5.0` and `2.0/20.0/0.75/200.0/10.0`.
  - Required evidence: tests prove wind is derived from bound `wind_u_10m`/`wind_v_10m` canonical cells via existing `wind_speed` logic, and precipitation/radiation use the same conversion factors as IDW.
  - Required evidence: generated direct-grid rows use the same valid-time plan as existing forcing production for the selected source/cycle, and generated components cover the selected canonical product inputs.
  - Required evidence: producer tests prove direct-grid canonical reads retain only the required bound `grid_cell_id` set where the current `_read_canonical_field` path supports selective retention.
  - Required evidence: missing bound `grid_cell_id` in any required canonical product fails closed before package, forcing version, station timeseries, or ready cycle state, and does not fallback to IDW.
  - Required evidence: non-finite canonical value at a bound direct-grid cell fails closed before package, forcing version, station timeseries, or ready cycle state, and does not fallback to IDW.
  - Required evidence: explicit `direct_grid` still does not call IDW station loading, IDW neighbor search, or legacy IDW fallback during row generation.
  - Required evidence: generated direct-grid rows/components stop at a stable #546 package/lineage boundary; #545 does not write SHUD packages, ready forcing versions, station timeseries, lineage/idempotency freshness, or runtime staging.
  - Required evidence: existing absent/explicit `idw` row generation and package output tests still pass unchanged.
  - Non-goal for #545: no SHUD package writes for direct-grid success, no direct-grid forcing lineage/idempotency freshness, no runtime staging validation, and no deeper NetCDF/xarray lazy indexed-read optimization beyond using the existing required-cell retention hook.
- [ ] 2.5 Use direct-grid required `grid_cell_id`s to limit retained canonical values in the existing `_read_canonical_field` path; defer deeper NetCDF/xarray lazy indexed-read optimization to a separate performance task if profiling shows it is needed.

## 3. Persistence, Lineage, and Compatibility

- [ ] 3.1 Extend repository/store interfaces to load direct-grid mapping metadata from authoritative model asset manifests, allowing database mirrors only as derived cache.
- [x] 3.2 (#546) Write SHUD `.tsd.forc` and per-station CSV packages for direct-grid rows, keeping SHUD CSV columns to `Precip`, `Temp`, `RH`, `Wind`, and `RN` while persisting pressure only outside the SHUD CSV contract.
  - Required evidence: producer tests prove successful direct-grid production writes the standard SHUD package, package manifest, debug CSV, and per-station `shud/*.csv` files using contract `shud_forcing_index`/coordinates/filenames.
  - Required evidence: tests prove per-station SHUD CSV files contain `Precip`, `Temp`, `RH`, `Wind`, and `RN` only, while `Press` remains in `met.forcing_station_timeseries` and lineage/metadata when generated.
  - Required evidence: tests prove direct-grid package success persists `met.forcing_station_timeseries` rows for every station, variable, and valid time and writes no duplicate ready forcing versions on rerun.
- [x] 3.3 (#546) Record `forcing_mapping_mode`, binding identity/checksum, model input identity, `.sp.att` checksum, grid signature, source scope, and spatial mapping method in forcing lineage and package manifest metadata.
  - Required evidence: producer tests inspect `met.forcing_version.lineage_json` and the package manifest `lineage` for direct-grid mode, binding checksum/URI, model input package identity, `.sp.att` path/checksum, applicable source ids, grid id/signature, and spatial mapping method.
  - Required evidence: tests prove package file entries and lineage remain bound to the same direct-grid contract identity and canonical grid signature used for validation/materialization.
- [x] 3.4 (#546) Update idempotency/freshness checks so mapping mode, binding identity, grid signature, or model input identity changes invalidate an existing ready forcing version for the same model/source/cycle.
  - Required evidence: rerun with unchanged direct-grid contract returns `already_done` or existing-ready behavior without duplicate ready versions.
  - Required evidence: recompute-able freshness drift, such as binding URI, `.sp.att` path/checksum, applicable source ids/scope, direct-grid station signature, canonical input signature, or mapping mode, invalidates the prior ready output and recomputes/replaces the same forcing version according to existing retry semantics.
  - Required evidence: binding checksum, model input package id, or canonical grid identity/signature drift that collides with existing derived direct-grid `met_station` mirror rows fails closed before weight writes or ready publication, preserving the #544 mirror-collision invariant instead of weakening it into an automatic overwrite.
- [x] 3.5 (#546) Ensure explicit `direct_grid` validation failures never fallback to IDW and leave no ready forcing outputs, with no-ready-output regression tests.
  - Required evidence: direct-grid validation/value/package/lineage/idempotency failures mark `failed_forcing`, do not call legacy station loading or IDW neighbor search, and leave no finalized ready package/version/timeseries beyond existing incomplete-retry semantics.
  - Required evidence: failure-injection tests cover each publication phase separately: parent `met.forcing_version` pending creation, package file writes, package manifest write/checksum, component child rows, station timeseries child rows, lineage persistence in the parent record, and finalization/readiness. Each failure leaves no finalized ready output.
  - Required evidence: retry after each publication-phase failure completes the same forcing version without duplicate ready versions, duplicate component rows, duplicate station timeseries rows, or orphaned stale package/manifest identity.
- [x] 3.6 (#546) Preserve existing IDW tests and behavior for model assets without `forcing_mapping_mode` or with explicit `forcing_mapping_mode="idw"`.
  - Required evidence: existing absent/explicit IDW package output, lineage, idempotency, and finite-value validation tests still pass unchanged.
  - Non-goal for #546: no SHUD runtime staging validation, no `.sp.att` runtime FORC-vs-`.tsd.forc` check, and no runtime fallback rewrite changes; those remain #547.

## 4. SHUD Runtime Integration

- [x] 4.1 Validate that direct-grid forcing packages use standard multi-station SHUD staging and reject fallback single-station `.sp.att` rewrites when lineage declares `forcing_mapping_mode="direct_grid"`.
  - Required evidence: runtime tests prove direct-grid packages with standard `shud/qhh.tsd.forc` stage multi-station forcing files and do not rewrite `.sp.att` to `FORC=1`.
  - Required evidence: runtime tests prove direct-grid packages missing standard SHUD forcing files fail closed even when legacy fallback files exist, and legacy non-direct-grid packages still retain fallback behavior.
- [x] 4.2 Add runtime checks that staged `.sp.att` `FORC` values are within the `.tsd.forc` `ID` column values for direct-grid packages.
  - Required evidence: runtime tests prove out-of-range direct-grid `.sp.att FORC` values fail before SHUD execution/status transition, while valid multi-station ownership stages successfully.

## 5. End-to-End Evidence and Documentation

- [x] 5.1 (#548) Add a compact end-to-end direct-grid fixture covering mode resolution, binding validation, exact value generation, SHUD package formatting, lineage, runtime staging validation, and idempotency.
  - Required evidence: a targeted pytest fixture exercises a valid direct-grid model asset from mode resolution through producer package publication and runtime staging using compact in-memory/object-store inputs, without requiring real GFS/IFS files, Slurm, or a full basin.
  - Required evidence: fixture verifies exact bound-cell values for at least two stations, standard SHUD `.tsd.forc` and station CSV output, direct-grid lineage/manifest identity, `.sp.att FORC` validation against `.tsd.forc` IDs, and unchanged rerun/idempotency behavior.
  - Required evidence: fixture proves explicit `direct_grid` does not call IDW station loading, IDW neighbor search, or legacy runtime fallback `.sp.att` rewrite.
  - Required evidence: the fixture is included in the PR targeted pytest command and remains bounded enough for CI targeted runs.
- [x] 5.2 (#548) Update forcing production and model asset documentation with migration workflow, rollback by asset version, source scope, grid signature rules, Press handling, and the reason canonical conversion remains mandatory.
  - Required evidence: docs explain the dual-mode contract: absent/explicit `idw` keeps legacy interpolation, while explicit `direct_grid` reuses precomputed basin grid ownership and fails closed on stale/incomplete assets.
  - Required evidence: docs describe migration and rollback by publishing/selecting model/input asset versions, not by toggling global runtime config or mutating historical ready forcing versions.
  - Required evidence: docs state that `applicable_source_ids`, `grid_id`, `grid_signature`, binding checksum, model input identity, and `.sp.att` checksum define direct-grid applicability.
  - Required evidence: docs state that canonical conversion remains mandatory for IFS/GFS before direct-grid lookup, including precipitation/radiation de-accumulation, humidity/wind derivation, unit normalization, QC, and lineage.
  - Required evidence: docs state that `Press` may remain persisted metadata/timeseries but is not emitted in SHUD station CSV files.
