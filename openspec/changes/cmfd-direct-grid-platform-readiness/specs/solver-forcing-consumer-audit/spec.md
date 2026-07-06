## ADDED Requirements

### Requirement: Solver forcing consumers are audited on the pinned pin
The platform SHALL audit the production SHUD solver at the pinned submodule commit to identify every consumer of forcing inputs before any basin migration, and record the audit as an immutable report bound to the readiness manifest.

#### Scenario: Audit enumerates all sp.att FORC readers
- **WHEN** the solver forcing-consumer audit is performed on the pinned solver commit
- **THEN** it enumerates every code path that reads `.sp.att` `FORC`
- **THEN** it determines whether any river or lake element uses an independent forcing index distinct from element `FORC`
- **THEN** the findings cite the solver source locations at the pinned commit.

#### Scenario: Audit determines whether station X/Y/Z participate in computation
- **WHEN** the audit inspects station coordinate usage
- **THEN** it determines whether station `X`, `Y`, and `Z` participate in any numeric computation
- **THEN** it determines whether any elevation correction is applied to forcing values
- **THEN** the finding for `Z` states explicitly whether elevation is numerically used or unused.

#### Scenario: Audit inventories non-weather tsd inputs
- **WHEN** the audit inspects non-weather time-series inputs
- **THEN** it inventories the `*.tsd.*` inputs including `tsd.lai`, `tsd.mf`, and `tsd.rl` present in all 13 basin packages
- **THEN** it determines whether `Prcp_Correction`, LAI, and MF series are consumed independently of the weather forcing series
- **THEN** it lists which legacy forcing-directory files must be preserved for the solver to run correctly.

### Requirement: Solver audit issues an explicit z_policy verdict
The audit report SHALL conclude with an explicit `z_policy` verdict, and SHALL approve the `sentinel` policy only when the audit proves station `Z` is not used in numeric computation.

#### Scenario: Sentinel is approved only when Z is proven unused
- **WHEN** the audit finds that station `Z` does not participate in any numeric computation and no elevation correction depends on it
- **THEN** the `z_policy` verdict may be `sentinel`
- **THEN** the verdict cites that `Z=-9999` already occurs in live baselines (source-of-truth appendix A) as consistent with sentinel use.

#### Scenario: Sentinel is rejected when Z is used or unproven
- **WHEN** the audit finds that station `Z` participates in numeric computation, or cannot prove `Z` is unused
- **THEN** the `z_policy` verdict is not `sentinel`
- **THEN** the verdict selects an explicit elevation source (`canonical_orography` or `model_dem_at_cell_center`)
- **THEN** the verdict records the evidence that forced the elevation-source choice.

### Requirement: Solver audit report is immutable and bound to the pin
The audit report SHALL be a committed, immutable deliverable bound to the exact pinned solver submodule commit and to the readiness manifest checksum.

#### Scenario: Audit report references the pinned solver identity
- **WHEN** the audit report is finalized
- **THEN** it records the SHUD-OpenMP outer repository commit and the SHUD solver submodule exact commit it audited
- **THEN** it references the `shud_omp` build provenance evidence (build-tree submodule HEAD, build log, or checksum comparison against a rebuild from the pin) confirming the audited submodule commit is the commit the production binary was built from
- **THEN** it references the readiness manifest checksum it was produced against
- **THEN** the report is stored as a committed evidence file and is not edited in place after publication.
