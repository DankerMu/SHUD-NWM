## MODIFIED Requirements

### Requirement: Station coordinates and derived fields obey the tolerance rule
The mapping builder SHALL set station coordinates equal to the registered cell center under the grid-signature rounding rule and SHALL make `x`/`y` recomputable and `z` policy-driven.

#### Scenario: Station lon/lat equal the registered cell center under rounding
- **WHEN** the builder sets a station `longitude` and `latitude`
- **THEN** they equal the registered cell center where equality is compared after the same 12-decimal rounding used for the grid signature
- **THEN** float-literal equality is never used, because live coordinates carry ~1e-7° noise.

#### Scenario: Station coordinates declare an explicit WGS84 basis and cross-basis equality is forbidden
- **WHEN** the builder emits station `longitude` and `latitude` and any equality assertion against derived mirrors
- **THEN** the binding and manifest declare and record the coordinate basis as WGS84 (matching the registry basis per docs §7.3), for example via a `coordinate_reference_system` field or an equivalent explicit declaration
- **THEN** the equality assertion between station lon/lat and the registered cell center is performed in the WGS84 basis (both operands WGS84, compared after 12-decimal rounding)
- **THEN** cross-basis equality assertions between binding/registry coordinates (WGS84) and the `met.met_station.geom` mirror (SRID 4490 / CGCS2000) are forbidden — the DB mirror is derived (INV-3) and its numeric coordinates MUST NOT be used as the source-of-truth in any builder equality check without an explicit transform.

#### Scenario: x/y are recomputable and z follows the approved policy
- **WHEN** the builder sets station `x`, `y`, and `z`
- **THEN** `x` and `y` are recomputable from `longitude`/`latitude` and the model CRS
- **THEN** `z` follows the `z_policy` recorded in the `z_policy` verdict evidence file of change `direct-grid-build-enablement` (the authoritative narrow solver-audit verdict, whose value is one of `sentinel`, `model_dem_at_cell_center`, or `canonical_orography`), committed at `openspec/changes/direct-grid-build-enablement/evidence/z-policy-solver-audit-verdict.md` and living, after that change archives, at the same file under the archive relocation `openspec/changes/archive/<archive-date>-direct-grid-build-enablement/evidence/z-policy-solver-audit-verdict.md`
- **THEN** the builder resolves that verdict through its pinned SHA-256 constant (fail closed on checksum or verdict-value mismatch, per the `z-policy-verdict` capability), so the pinned file — not any path-supplied substitute — is the sole `z_policy` authority, superseding the descoped `cmfd-direct-grid-platform-readiness` (Epic #886) solver audit that was intentionally removed and never produced a verdict.
