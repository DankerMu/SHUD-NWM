## 1. Mapping Input Integrity (Gates G0 + G1)

- [x] 1.1 Implement `workers/mapping_builder/integrity.py` baseline verification: recompute baseline package checksum, parse `.sp.mesh` and `.sp.att`, and verify element IDs are unique and contiguous from 1 with equal mesh/att sets and equal counts.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_integrity.py` proves a valid baseline passes and that a checksum mismatch, unparseable mesh/att, non-unique ID, non-contiguous ID, unequal ID sets, or unequal counts each fails closed with no output.
  - Required evidence: tests prove old `FORC` values that are non-positive or non-integer fail closed, and that legal old `.tsd.forc` references pass while illegal references fail.
  - Non-goal for 1.1: no barycenter mapping, no grid matching, no `.sp.att` rewrite, no binding emission.
- [x] 1.2 Implement CRS authority and ancillary inventory: read model CRS only from the package `gis/*.prj` (checksum-bound), reject missing/unparseable/non-WGS84-convertible `.prj`, make no global CRS assumption, and build a complete ancillary `*.tsd.*` dependency inventory.
  - Required evidence: tests prove CRS is read from the package `.prj` only, that a `PROJCS["unknown"]` custom Albers and the qhh Transverse Mercator both parse and transform to WGS84, and that a missing/unparseable `.prj` fails closed.
  - Required evidence: tests prove `.sp.mesh` is never used as a CRS source and that the ancillary `*.tsd.*` inventory is complete.
  - Non-goal for 1.2: no EPSG lookup or global CRS default.
- [x] 1.3 Implement baseline classification: register duplicate-coordinate stations, classify non-grid baselines, record startdate heterogeneity, exclude `domain.shp` as an algorithm input, and never modify the baseline (INV-1).
  - Required evidence: tests prove duplicate-coordinate stations are registered (zhaochen_mc-style 4 stations at identical coords, Z=-9999), a non-grid baseline is classified (zhaochen_wem-style 5 irregular X1..X5 points at 0.02° spacing), and startdate heterogeneity is recorded.
  - Required evidence: tests prove `domain.shp` is never consumed as geometry or element-ID authority and that baseline files are opened read-only with unchanged pre/post checksums.
  - Non-goal for 1.3: no repair of known-harmless baseline deviations (e.g. `.tsd.forc` line-2 absolute paths); record only.
- [x] 1.4 Implement the G1 non-degenerate triangle check in `workers/mapping_builder/integrity.py`: for every element, verify that the three vertex IDs are pairwise distinct and reference existing mesh nodes, and that the triangle formed by their X/Y coordinates in the package CRS has an unsigned planar area strictly greater than a declared numeric tolerance; fail closed as a G1 blocker with no mapping output when any element is degenerate.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_integrity.py::test_g1_non_degenerate_triangles` proves that a valid mesh with all non-degenerate elements passes, and that (a) an element with a repeated vertex ID, (b) an element referencing a non-existent mesh node ID, and (c) an element whose three nodes are collinear producing a zero-area triangle within tolerance each fail closed as G1 blockers with no output.
  - Required evidence: tests prove the barycenter check in Task 2.1 is never reached when a G1 degeneracy is present (the integrity stage blocks before any mapping computation runs).
  - Non-goal for 1.4: no barycenter computation, no CRS reprojection to WGS84, and no grid matching (those are group 2 concerns); the check is purely intra-element geometry validity in the package CRS.

## 2. Element-Grid Ownership Mapping

- [x] 2.0 Implement the G2 Grid Identity precondition in `workers/mapping_builder/algorithm.py` (before any element mapping): load the registered grid snapshot from `canonical-source-grid-registry` by `(source_id, grid_id)`; recompute `grid_signature` via the shared helper `packages/common/grid_signature.grid_signature_hash` and assert it equals the snapshot's stored value; assert every element barycenter (WGS84) lies inside the snapshot coverage bbox with no silent dynamic crop; fail closed on any violation.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_algorithm.py::test_g2_grid_identity` proves (a) the shared `packages/common/grid_signature.grid_signature_hash` helper is invoked on the loaded snapshot cells and its result equals the snapshot's stored `grid_signature`, (b) a signature-mismatch negative test fails closed with no output, (c) any element barycenter outside the registered coverage bbox fails closed as a G2 blocker with no output, and (d) an unregistered `(source_id, grid_id)` lookup fails closed with no output.
  - Non-goal for 2.0: no reimplementation of the grid-signature rule (the shared helper is the sole authority); no CRS/clip auto-correction.
- [x] 2.1 Implement `workers/mapping_builder/algorithm.py` `nearest_cell_barycenter_geodesic_v1`: element barycenter `(v1+v2+v3)/3` in package CRS transformed to WGS84, nearest registered cell by `pyproj` geodesic distance, and per-element distance/tie/candidate recording.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_algorithm.py` proves the barycenter is the mesh three-vertex mean and that nearest selection uses geodesic distance, not undeclared planar-degree distance.
  - Required evidence: tests prove that for a regular lat/lon grid the independent lon/lat rounding fast path yields the identical cell and tie behavior as the geodesic definition.
  - Non-goal for 2.1: no used-cell subsetting, no index assignment, no binding emission.
- [x] 2.2 Implement tie-break and the distance sanity bound: resolve ties within tolerance by smallest canonical ordinal, and reject as a blocker any in-coverage centroid whose nearest-center distance exceeds the local half-cell-diagonal plus numeric tolerance.
  - Required evidence: tests prove a tie is resolved to the smallest canonical ordinal reproducibly, and that an in-coverage centroid beyond the half-cell-diagonal bound fails closed as a blocker.
  - Non-goal for 2.2: no CRS/clip auto-correction; the bound only blocks.
- [x] 2.3 Implement used-cell subset and forcing-index assignment: keep only cells referenced by ≥1 element (every binding cell referenced, one cell = one station), and assign `shud_forcing_index` `1..N` contiguous ordered by canonical ordinal.
  - Required evidence: tests prove zero unused bindings, one-cell-to-one-station, and that `shud_forcing_index` is contiguous, unique, canonical-ordinal-ordered, and reproducible.
  - Non-goal for 2.3: no `.sp.att FORC` writing (that is group 3).
- [ ] 2.4 Implement the small-basin hard gate: refuse by default when used-cell count < 4, and proceed only when an explicit approval flag is supplied, recording the override in evidence.
  - Required evidence: tests prove a 1-cell and a 4-cell basin (zhaochen_wem/zhaochen_mc analogues) are refused with no output by default, and that the explicit approval flag proceeds and records the approver identity as an evidence approval.
  - Non-goal for 2.4: no silent override and no partial output when refused.

## 3. sp.att FORC Rewrite

- [ ] 3.1 Implement `workers/mapping_builder/rewrite.py`: copy the baseline `.sp.att`, update `FORC` by element ID into the variant package, and never overwrite the baseline file.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_rewrite.py` proves `FORC` is updated by element ID (not row order), the new `.sp.att` is written into the variant, and the baseline file checksum is unchanged.
  - Required evidence: tests prove every rewritten `FORC` value is an integer in `1..N` (where N == used-cell count from Task 2.3), that the multiset of rewritten `FORC` values equals the ownership table's mapped `shud_forcing_index` list, and that any out-of-range or unmapped `FORC` fails closed with no variant `.sp.att` written.
  - Non-goal for 3.1: no manifest or binding emission.
- [ ] 3.2 Implement the G4 non-`FORC`-unchanged proof, semantic diff, and checksums: prove `old_att[all columns except FORC] == new_att[...]` at parse level, emit a parse-level semantic diff artifact, and record old/new `.sp.att` SHA-256 checksums.
  - Required evidence: tests prove equal row count/IDs/schema, byte/semantic equality of all non-`FORC` columns, a semantic diff showing only `FORC` changes, and recorded old/new checksums.
  - Required evidence: tests prove a change to any non-`FORC` value fails the G4 proof and blocks output.
  - Non-goal for 3.2: no evidence-package assembly (that is group 5).
- [ ] 3.3 Implement the G4 non-`.sp.att` file-checksum equality proof: assert the variant's `.sp.mesh`, river topology, lake topology, soil, geol, land, and calibration files have SHA-256 checksums byte-identical to the baseline's; fail closed on any inequality before writing the variant.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_rewrite.py::test_g4_non_sp_att_checksums_equal` proves the variant's mesh/river/lake/soil/geol/land/calibration checksums equal the baseline's, and that mutating any one of those files causes a G4 blocker with no output.
  - Non-goal for 3.3: no scientific comparison (Change 6, G11), no runtime staging.
- [ ] 3.4 Implement the `hydrologic_core_fingerprint` computation and equality proof: compute a fingerprint covering mesh topology, river/lake topology, `.sp.att` non-`FORC` fields, soil/geol/land, calibration, state vector schema, and solver-relevant configuration (per docs §Gate G10); prove the variant's fingerprint equals the baseline's; record it in the evidence.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_rewrite.py::test_hydrologic_core_fingerprint` proves the fingerprint is byte-identical between variant and baseline in a green build; a negative test that mutates any covered non-`FORC` file (mesh/river/lake/soil/geol/land/calibration/state-schema/solver-config/att-non-`FORC`) causes the fingerprint to change and the build to fail closed as a G4 blocker.
  - Non-goal for 3.4: no state snapshot compatibility (that is Change 5 / G10 runtime).
- [ ] 3.5 Enforce the "no legacy weather path in active package" assertion: assert the variant's active forcing tree contains no legacy CMFD weather CSV filenames (e.g. `X<lon>Y<lat>.csv` or `X<n>.csv` under active forcing per docs §8.2) and no builder-written cycle `.tsd.forc`; fail closed when any legacy weather path or forbidden file appears in the active variant tree.
  - Required evidence: tests prove a green build emits none of these files, and a negative test that injects a legacy `X<lon>Y<lat>.csv` into the variant's active forcing directory fails closed as a G4 blocker.
  - Non-goal for 3.5: no runtime producer change (§8.1 boundary is enforced against the builder here, not against the producer).

## 4. Direct-Grid Binding Artifact

- [ ] 4.1 Implement `workers/mapping_builder/binding.py` manifest + station-binding emission matching the existing parser contract in `workers/forcing_producer/direct_grid_contract.py`, placed in the `resource_profile.direct_grid_forcing` nested section.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_binding.py` proves the emitted manifest carries `forcing_mapping_mode` (with value `direct_grid`), `binding_uri`, `binding_checksum`, `model_input_package_id`, `sp_att_path`, `sp_att_checksum`, `applicable_source_ids`, `grid_id`, `grid_signature`, and the top-level `station_bindings` array (canonical field name per §7.2), and that each station carries `station_id`/`shud_forcing_index`/`forcing_filename`/`longitude`/`latitude`/`x`/`y`/`z`/`grid_id`/`grid_cell_id`.
  - Required evidence: a test round-trips the emitted binding/manifest through the existing direct-grid contract parser without error.
  - Required evidence: tests prove all emitted `grid_cell_id` values in the binding are pairwise unique and every value is a subset member of the loaded snapshot's ordered `grid_cell_id` set — the snapshot is provided as an in-memory `GridSnapshot` fixture under `tests/fixtures/mapping_builder/` populated via the shared loader that reuses `packages/common/grid_signature.py`, so this task does not construct a registry.
  - Required evidence: tests prove the emitted binding artifact bytes (referenced by `binding_uri` with `binding_checksum`) parse to the identical station rows as the manifest `station_bindings` section — same `station_id`, `shud_forcing_index`, `grid_cell_id`, and lon/lat after 12-decimal rounding — and that any injected divergence between the two artifacts fails closed as a G5 blocker.
  - Required evidence: tests prove `binding_checksum` is the recomputed SHA-256 of the emitted binding artifact bytes and `sp_att_checksum` is the recomputed SHA-256 of the emitted variant `.sp.att` bytes, and that either mismatch fails closed as a G5 blocker.
  - Non-goal for 4.1: no evidence-package assembly and no grid-registry construction (the in-memory `GridSnapshot` fixture is the substitute).
- [ ] 4.2 Implement station identity, filename safety, and the coordinate/derived-field rules: embed immutable mapping-asset identity in `station_id`; produce safe pathless case-fold-unique `forcing_filename`s not colliding with reserved names and not derived from rounded coordinates; set lon/lat equal to the registered cell center under 12-decimal rounding; make `x`/`y` recomputable; set `z` per the approved `z_policy`.
  - Required evidence: tests prove `station_id` is never reused across mapping versions, filenames are case-fold unique and never collide with `qhh.tsd.forc`/manifest/debug/model-input names, and that station lon/lat equal the cell center after 12-decimal rounding (not float-literal equality against ~1e-7° noise).
  - Required evidence: tests prove the binding/manifest declare the WGS84 coordinate basis (docs §7.3) — for example via a `coordinate_reference_system` field — and that a negative test attempting a cross-basis equality assertion against a SRID 4490 (CGCS2000) `met.met_station.geom` mirror row fails closed.
  - Required evidence: tests prove `x`/`y` are recomputable from lon/lat + model CRS and that `z` follows the `z_policy` from `cmfd-direct-grid-platform-readiness`.
  - Non-goal for 4.2: no reimplementation of the grid-signature rule; the shared helper from `canonical-source-grid-registry` is invoked by Task 2.0 (G2 precondition), and this task must never hand-roll signature logic.
- [ ] 4.3 Enforce the forbidden-output rule (§8.1): assert the mapping stage writes no cycle-dated `.tsd.forc`, no station weather CSVs, no `met.interp_weight`/`met.met_station`/`met.forcing_version` rows, and no cycle lineage.
  - Required evidence: tests prove none of the forbidden runtime-producer artifacts are emitted by a successful build.
  - Non-goal for 4.3: no change to the runtime producer, which owns those artifacts.

## 5. Mapping Evidence Package

- [ ] 5.1 Implement `workers/mapping_builder/evidence.py` assembling the §14 evidence: baseline identity (package/att/mesh checksums), grid snapshot ref, ownership table (element_id, old FORC, new FORC, grid_cell_id, distance), station binding rows, asset diff, and mapping algorithm / PROJ identity.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_evidence.py` proves the evidence contains every listed section with the correct row/field content for a fixture build.
  - Required evidence: tests prove the evidence records `algorithm_id='nearest_cell_barycenter_geodesic_v1'` and `proj_crs_database_version` cross-checked against the `cmfd-direct-grid-platform-readiness` readiness manifest, and that mutating either field invalidates the evidence checksum.
  - Required evidence: tests prove the evidence records the `hydrologic_core_fingerprint` computed in Task 3.4 and that a change to any covered non-`FORC` file changes the recorded fingerprint.
  - Non-goal for 5.1: no scientific A/B report (Change 6) and no producer/runtime/state evidence.
- [ ] 5.2 Implement distance QA, capacity report, gate results, ownership images, approvals, rollback target, and checksum binding: distance min/P50/P95/max normalized by cell size with tie/coverage-edge counts; capacity station/timestep/row/file-size vs limits with before/after reduction; G0–G5 gate results; old/new ownership map images; approvals and rollback target; evidence checksum bound to the mapping-asset checksum.
  - Required evidence: tests prove the distance QA and capacity report (including the ~5× station reduction framing) are populated, G0–G5 results are recorded (including G2 grid identity, G4 asset delta with mesh/river/lake/soil/geol/land/calibration checksum equality and `hydrologic_core_fingerprint` equality and no-legacy-weather-path, and G5 cross-artifact consistency), and the evidence checksum invalidates when either the evidence or the mapping asset is altered.
  - Required evidence: tests prove the evidence enumerates any `checksum_excluded_fields` (e.g. build timestamp) so mutating an excluded field never changes any checksum.
  - Non-goal for 5.2: no mutation of any prior evidence package; superseding a variant builds a new immutable one.

## 6. Keliya Integration Fixture and Verification

- [ ] 6.1 Add a compact keliya integration fixture (484 elements / 32 stations / ~8 used cells, per 附录 A) exercising the full builder from G0 through G5 to binding, `.sp.att` rewrite, and evidence, using in-memory/on-disk fixtures without a real grid download or Slurm. The grid snapshot is provided via the same in-memory `GridSnapshot` fixture under `tests/fixtures/mapping_builder/` used by Task 4.1, populated through the shared loader that reuses `packages/common/grid_signature.py`.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder_integration.py` builds the keliya variant end to end, proving G0–G5 pass (G2 grid identity via the shared signature helper, G4 asset delta including `hydrologic_core_fingerprint` equality, and G5 manifest ↔ binding artifact cross-consistency), the emitted binding parses through the existing contract parser, the G4 non-`FORC`-unchanged proof holds, and the evidence checksum binds to the mapping-asset checksum.
  - Required evidence: the fixture proves determinism per the algorithm version — building twice from the same baseline package, the same grid snapshot, and the same algorithm version yields byte-identical binding, `.sp.att`, and evidence bundles (bytes and checksums), while any `checksum_excluded_fields` (e.g. a build timestamp) are enumerated and never enter any checksum.
  - Non-goal for 6.1: no basin activation, no scheduler routing, no state or display change.
- [ ] 6.2 Run the full change verification suite and OpenSpec validation.
  - Required evidence: `uv run pytest -q tests/test_mapping_builder*.py` passes.
  - Required evidence: `uv run ruff check .` passes.
  - Required evidence: `openspec validate forcing-mapping-asset-build --strict --no-interactive` passes.
  - Non-goal for 6.2: no node-27 live receipt is required, since this change builds inert offline assets and touches no display or live DB path.
