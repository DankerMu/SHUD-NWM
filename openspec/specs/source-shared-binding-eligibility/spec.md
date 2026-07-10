# source-shared-binding-eligibility Specification

## Purpose
TBD - created by archiving change canonical-source-grid-registry. Update Purpose after archive.
## Requirements
### Requirement: Sharing is keyed on grid signature equality
The registry SHALL decide GFS/IFS shared-binding eligibility by `grid_signature` equality, not by `grid_id` string equality.

#### Scenario: Identical signature across different grid_id strings is shareable
- **WHEN** two source grids have the identical `grid_signature` (for example `ifs_0p25` and `gfs_0p25` sharing `6c008901b8b7…`) while their `grid_id` strings differ
- **THEN** the registry evaluates them as candidates for a shared binding based on signature equality
- **THEN** the differing `grid_id` strings do not disqualify sharing.

#### Scenario: grid_id string equality is never used as the sharing key
- **WHEN** the registry evaluates two source grids for sharing
- **THEN** it does not require their `grid_id` strings to match
- **THEN** because `grid_id` is named per source and can never match across sources, string equality is not a sharing criterion.

### Requirement: A source-agnostic canonical_grid_key maps matching signatures
The registry SHALL derive a source-agnostic `canonical_grid_key` from exactly three inputs: `grid_signature`, pinned download bbox, and `native_resolution`.

#### Scenario: Matching signatures under same bbox and resolution map to one canonical_grid_key
- **WHEN** two source grids share the same `grid_signature` under the same pinned bbox and the same `native_resolution`
- **THEN** both map to the same `canonical_grid_key`
- **THEN** the `canonical_grid_key` derivation does not take `source_id` as an input.

#### Scenario: Differing signatures map to different keys
- **WHEN** two source grids have different `grid_signature` values
- **THEN** they map to different `canonical_grid_key` values
- **THEN** they are not treated as the same canonical grid.

#### Scenario: Same signature under different bbox maps to different canonical_grid_key
- **WHEN** two snapshots share the same `grid_signature` but the pinned bbox differs on any of south/north/west/east
- **THEN** they MUST derive different `canonical_grid_key` values
- **THEN** the bbox difference alone disqualifies sharing regardless of signature equality.

#### Scenario: Same signature under different native_resolution maps to different canonical_grid_key
- **WHEN** two snapshots share the same `grid_signature` but declare different `native_resolution` values (for example 0.25° vs 0.5°)
- **THEN** they MUST derive different `canonical_grid_key` values
- **THEN** the resolution difference alone disqualifies sharing regardless of signature equality.

### Requirement: Shared eligibility requires per-source verification and explicit scope
The registry SHALL grant shared-binding eligibility only when both sources are verified and explicitly scoped.

#### Scenario: Both sources verified on representative cycles
- **WHEN** a shared binding is proposed for two sources mapping to the same `canonical_grid_key`
- **THEN** all required variables are verified on representative cycles for both sources with the matching signature
- **THEN** the snapshot's `applicable_source_ids` explicitly lists both sources (normalized via `packages/common/source_identity.py` `normalize_source_id`)
- **THEN** archived comparison evidence (multi-cycle, dual-source signature comparison) is recorded
- **THEN** eligibility is denied when any of these conditions is unmet.

#### Scenario: Single-source verification does not grant sharing
- **WHEN** only one source's variables have been verified against the shared `canonical_grid_key`
- **THEN** shared-binding eligibility is not granted
- **THEN** the sources are treated as requiring separate bindings until both are verified.

#### Scenario: applicable_source_ids omission denies sharing
- **WHEN** two snapshots share a `canonical_grid_key` but at least one snapshot's `applicable_source_ids` does not list both source ids
- **THEN** shared-binding eligibility is denied
- **THEN** the registry state alone answers the eligibility question without consulting external manifests.

### Requirement: source_id case is normalized consistently
The registry SHALL normalize `source_id` case using `packages/common/source_identity.py` `normalize_source_id`, which is the same rule the contract parser uses.

#### Scenario: Mixed-case live source ids normalize to their canonical asymmetric form
- **WHEN** live data presents inconsistent source-id case such as `IFS` and `gfs`
- **THEN** the registry and `applicable_source_ids` validation call `normalize_source_id` on each `source_id`
- **THEN** `normalize_source_id("ifs")` returns `"IFS"`, `normalize_source_id("IFS")` returns `"IFS"`, `normalize_source_id("GFS")` returns `"gfs"`, `normalize_source_id("gfs")` returns `"gfs"`, `normalize_source_id("era5")` returns `"ERA5"`
- **THEN** "consistently" means each source normalizes to its own canonical asymmetric form (`gfs` lower-case, `IFS`/`ERA5` upper-case), NOT that both normalize to the same case
- **THEN** the registry stores the exact string returned by `normalize_source_id` without further case folding.

#### Scenario: Unknown source id fails normalization
- **WHEN** a source id is not one of `GFS`/`gfs`/`IFS`/`ifs`/`ERA5`/`era5`
- **THEN** `normalize_source_id` raises and registration fails closed
- **THEN** the unknown source id is not silently accepted or coerced.

