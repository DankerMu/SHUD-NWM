# sp-att-forc-rewrite Specification

## Purpose
TBD - created by archiving change forcing-mapping-asset-build. Update Purpose after archive.
## Requirements
### Requirement: sp.att FORC is rewritten by element ID without changing other content
The mapping builder SHALL produce a new `.sp.att` whose `FORC` column is updated by element ID while every non-`FORC` value remains byte and semantically unchanged, and SHALL never overwrite baseline files.

#### Scenario: FORC is updated by element ID from a baseline copy
- **WHEN** the builder rewrites `.sp.att`
- **THEN** the builder copies the baseline `.sp.att` and updates only the `FORC` value for each element by its element ID
- **THEN** the association is by element ID, never by row order
- **THEN** the builder writes the new `.sp.att` into the variant package and never overwrites the baseline file.

#### Scenario: Row count, IDs, schema, and non-FORC values are unchanged
- **WHEN** the builder compares the baseline and rewritten `.sp.att`
- **THEN** the row count, element IDs, and schema are identical
- **THEN** for all columns except `FORC`, `old_att` equals `new_att` at parse level (G4 proof)
- **THEN** the builder fails closed when any non-`FORC` value differs.

#### Scenario: Rewrite emits a semantic diff and checksums
- **WHEN** the builder finishes the `.sp.att` rewrite
- **THEN** a parse-level semantic diff artifact is produced showing only `FORC` changes
- **THEN** old and new SHA-256 checksums of `.sp.att` are recorded
- **THEN** these artifacts are available to the evidence package.

#### Scenario: FORC values are legal in the variant binding domain
- **WHEN** the builder rewrites `.sp.att FORC`
- **THEN** every rewritten `FORC` value is an integer in `1..N` where `N` equals the used-cell count derived by `element-grid-ownership-mapping`
- **THEN** the multiset of rewritten `FORC` values equals the ownership table's mapped `shud_forcing_index` list (i.e. matches the element-to-cell binding exactly)
- **THEN** the builder fails closed on any out-of-range, non-integer, or unmapped `FORC` before writing the variant `.sp.att`.

### Requirement: Variant package differs from baseline only in the allowed files (G4 asset delta)
The mapping builder SHALL prove the variant model input package differs from the baseline package only in the explicitly allowed files: the rewritten `.sp.att`, the direct-grid binding artifact, and the manifest. All other package contents SHALL be byte-identical to the baseline, the model-core fingerprint SHALL equal the baseline's, and no legacy CMFD weather path SHALL appear in the active forcing tree.

#### Scenario: Mesh, river, lake, soil, geol, land, and calibration files are byte-identical to baseline
- **WHEN** the builder assembles the variant package
- **THEN** the SHA-256 checksum of the `.sp.mesh` file in the variant equals the baseline's `.sp.mesh` checksum
- **THEN** the SHA-256 checksums of the river and lake topology files in the variant equal the baseline's
- **THEN** the SHA-256 checksums of the soil, geol (geology), and land files in the variant equal the baseline's
- **THEN** the SHA-256 checksum of the calibration file in the variant equals the baseline's
- **THEN** the builder fails closed on any inequality before writing the variant.

#### Scenario: hydrologic_core_fingerprint equals the baseline's
- **WHEN** the builder assembles the variant package
- **THEN** the builder computes a `hydrologic_core_fingerprint` covering mesh topology; river/lake topology; `.sp.att` non-`FORC` fields; soil/geol/land; calibration; state vector schema; and solver-relevant configuration (per docs §Gate G10)
- **THEN** the computed `hydrologic_core_fingerprint` equals the baseline package's `hydrologic_core_fingerprint`
- **THEN** the fingerprint is recorded in the evidence package
- **THEN** mutating any covered non-`FORC` file (mesh/river/lake/soil/geol/land/calibration/state-schema/solver-config/att-non-FORC) changes the fingerprint, and the builder fails closed as a G4 blocker on any mismatch (negative test evidence).

#### Scenario: No legacy CMFD weather path appears in the active forcing tree
- **WHEN** the builder assembles the variant package
- **THEN** the variant's active forcing tree contains no legacy CMFD weather CSV filenames (e.g. `X<lon>Y<lat>.csv` or `X<n>.csv` under the active forcing directory per docs §8.2)
- **THEN** the variant contains no cycle-dated `.tsd.forc` written by the builder
- **THEN** the only files added or updated relative to baseline are the rewritten `.sp.att`, the direct-grid binding artifact, and the manifest with the `resource_profile.direct_grid_forcing` nested section
- **THEN** the builder fails closed when any legacy weather path or forbidden file appears in the active variant tree.

