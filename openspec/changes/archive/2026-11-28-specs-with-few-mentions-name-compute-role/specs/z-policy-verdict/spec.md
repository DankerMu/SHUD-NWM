## MODIFIED Requirements

### Requirement: z_policy verdict is produced by a narrow three-question solver audit
The change SHALL produce a written `z_policy` verdict from a narrow audit of the pinned SHUD solver commit `3aec65755926c478e13ca7d4fea80715e4e90345` that answers exactly three questions — (a) does the `.tsd.forc` read chain read the station-row `Z` column, (b) what is that `Z` used for, and (c) what is the production `.cfg` correction-switch status — and the verdict value SHALL be one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography`. The audit SHALL NOT expand into a full solver re-audit.

#### Scenario: Audit answers the three questions and records an allowed verdict
- **WHEN** the `z_policy` verdict evidence file is produced
- **THEN** it records an answer to each of the three questions (Z read?, Z used for?, `.cfg` switch?) with line-level citations into the pinned solver source
- **THEN** it records the pinned commit `3aec65755926c478e13ca7d4fea80715e4e90345` and how the audit oracle was obtained (local `SHUD/` tree HEAD and/or the forensic command on the retired node-22 host)
- **THEN** the recorded verdict value is exactly one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography`.

#### Scenario: A verdict outside the allowed set is rejected
- **WHEN** a `z_policy` value that is not one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography` is supplied to the mapping builder
- **THEN** the builder fails closed (per `ALLOWED_Z_POLICIES`) and emits no binding.
