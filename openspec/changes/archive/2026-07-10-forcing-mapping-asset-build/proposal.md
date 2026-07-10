## Why

SHUD-NWM already implements the runtime consumer side of direct-grid forcing (contract parser in `workers/forcing_producer/direct_grid_contract.py`, producer direct path, and runtime staging via change `direct-grid-forcing`), but no tool exists to produce the immutable, source-specific model assets those consumers require. All 13 live basins remain on the legacy IDW path with no direct-grid contract, and there is no code that turns a baseline basin model package plus a registered grid snapshot into a rewritten `.sp.att`, a direct-grid binding, and an auditable evidence package. This change delivers that offline mapping builder so migration can produce trusted, checksum-bound, per-source direct-grid assets without ever mutating the CMFD baseline.

## What Changes

- Add an offline mapping builder that reads a baseline basin model package and a registered grid snapshot, computes triangle-to-grid ownership, rewrites `.sp.att` `FORC` by element ID, and emits a direct-grid binding, manifest, and evidence package as a new immutable model input package variant.
- Add Gate G0/G1 baseline and geometry integrity verification: baseline checksum, `.sp.mesh` and `.sp.att` parseability, unique/contiguous element IDs, equal ID sets, positive-integer old `FORC`, ancillary `*.tsd.*` inventory, and package-`.prj`-only CRS authority with no global CRS assumption.
- Add the canonical mapping algorithm `nearest_cell_barycenter_geodesic_v1`: element barycenter in package CRS transformed to WGS84, nearest registered cell by geodesic distance, deterministic tie-break by smallest canonical ordinal, used-cell subset extraction, contiguous `shud_forcing_index` assignment, distance QA, and a hard gate refusing basins with fewer than 4 used cells absent explicit approval.
- Add `.sp.att` `FORC` rewrite that copies the baseline, updates only `FORC` by element ID, proves all non-`FORC` columns are byte/semantically unchanged (G4), and never overwrites baseline files.
- Add direct-grid binding + manifest emission that matches the existing parser contract exactly, embeds immutable mapping-asset identity in `station_id`, produces safe pathless `forcing_filename`s, and enforces the coordinate-equality tolerance rule (§7.3). Mapping-stage cycle forcing, weather CSVs, and DB rows are forbidden outputs (§8.1).
- Add an immutable evidence package (§14) binding baseline identity, grid snapshot ref, ownership table, station bindings, asset diff, distance QA, ownership map images, G0–G5 gate results, capacity report, approvals, and rollback target, with an evidence checksum bound to the mapping-asset checksum.

## Capabilities

### New Capabilities
- `mapping-input-integrity`: Verify baseline package and geometry integrity (Gates G0+G1) and classify baseline station/CRS/startdate heterogeneity before any mapping, never mutating the baseline.
- `element-grid-ownership-mapping`: Compute deterministic triangle-barycenter nearest-grid-cell ownership, tie-break, used-cell subset, and contiguous forcing indexes, with a small-basin hard gate.
- `sp-att-forc-rewrite`: Rewrite `.sp.att` `FORC` by element ID while proving all non-`FORC` content is unchanged, without overwriting baseline files.
- `direct-grid-binding-artifact`: Emit a direct-grid binding and manifest that satisfy the existing parser contract and forbid runtime-producer artifacts.
- `mapping-evidence-package`: Produce an immutable, checksum-bound evidence package covering baseline identity, ownership, asset diff, distance QA, capacity, gates, approvals, and rollback.

### Modified Capabilities
- None.

## Impact

- New builder module `workers/mapping_builder/` (integrity, algorithm, rewrite, binding, evidence, and `cli.py`) producing offline model asset variants; no runtime producer/consumer code is modified.
- Depends on `canonical-source-grid-registry` for grid snapshots, `canonical_grid_key`, and the shared grid-signature helper (the builder MUST reuse the producer's signature logic, not reimplement it).
- Depends on `cmfd-direct-grid-platform-readiness` for the `z_policy` verdict and the pinned PROJ/solver release.
- New immutable model input package variants with parent lineage back to the CMFD baseline; baseline packages, `.sp.att`, `.tsd.forc`, and historical forcing versions remain unchanged (INV-1).
- New tests under `tests/test_mapping_builder*.py`, including a keliya integration fixture (484 elements / 32 stations / ~8 cells).
- Runtime dependency `pyproj` (already pinned) for geodesic distance and CRS transforms; PROJ version is pinned by the readiness change.
