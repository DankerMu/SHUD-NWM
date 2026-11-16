# runtime-storage-source-canonicalization Specification (delta)

## MODIFIED Requirements

### Requirement: Durable artifacts use object-store root

All durable raw, canonical, forcing, run output, state, tile, and persisted log artifacts SHALL be read and written through `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX`, except that aged cycle products MAY be relocated by the archive mover to `NHMS_ARCHIVE_ROOT` as checksum-verified archive objects with manifests, after which the archive tier is the canonical location for those cycles for non-display pipeline and ops tooling (the inventory audit, the archive rebuild drill, and salvage tooling). Display API routes governed by ADR 0001 are explicitly exempt from archive resolution: they SHALL keep their disk-only read semantics and not-found contracts unchanged.

#### Scenario: Workspace and object store are separate

- **WHEN** `WORKSPACE_ROOT` and `OBJECT_STORE_ROOT` point to different directories
- **THEN** a forecast pipeline MUST still find raw, canonical, forcing, runtime output, parser input, and state snapshot artifacts through object-store URIs

#### Scenario: Temporary workspace remains local

- **WHEN** a worker needs scratch files or Slurm execution directories
- **THEN** it MUST use `WORKSPACE_ROOT` only for temporary or HPC-local execution files, not as the durable object-store root

#### Scenario: Real Slurm templates propagate object-store settings

- **WHEN** a real sbatch template launches a worker that reads or writes durable artifacts
- **THEN** the worker environment MUST include `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX` in addition to `WORKSPACE_ROOT`

#### Scenario: Aged cycle products relocate to the archive tier

- **WHEN** the archive mover relocates an aged cycle product directory out of
  the object store
- **THEN** the archive object MUST live under `NHMS_ARCHIVE_ROOT` with a
  manifest and sha256 checksums verified before source deletion
- **AND** the non-display rebuild/reingest/ops tooling introduced by this
  change (inventory audit, archive rebuild drill, salvage tooling) MUST
  resolve a needed rotated cycle via archive provenance instead of treating
  it as a missing object-store artifact

#### Scenario: Display routes keep ADR 0001 disk-only semantics for rotated cycles

- **WHEN** a display API route governed by ADR 0001 (such as the station
  forcing series route) is asked for a cycle whose products the archive mover
  has rotated out of the object store
- **THEN** the route MUST keep its disk-only contract and return its existing
  not-found error (`STATION_FORCING_FILE_NOT_FOUND`)
- **AND** archive provenance MUST NOT be consulted as a silent fallback for
  display responses; any archive-backed history surface requires its own
  explicit API boundary per ADR 0001
