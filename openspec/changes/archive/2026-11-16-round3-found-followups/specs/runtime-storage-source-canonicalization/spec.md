## REMOVED Requirements

### Requirement: Durable artifacts use object-store root

**Reason**: The archive tier, which this requirement exempted from object-store resolution, was retired by ADR 0002 (Revision 2026-08-11) and #1370. Its two archive scenarios describe tooling that no longer exists.

**Migration**: Replaced by the requirement "Durable artifacts use the object-store root with no archive tier", which keeps the remaining obligations unchanged.

## ADDED Requirements

### Requirement: Durable artifacts use the object-store root with no archive tier

All durable raw, canonical, forcing, run output, state, tile, and persisted log artifacts SHALL be read and written through `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX`. There is no archive tier: ADR 0002 (Revision 2026-08-11) and #1370 retired the archive mover, the inventory audit, the archive rebuild drill and the salvage tooling, and no code reads `NHMS_ARCHIVE_ROOT`.

#### Scenario: Workspace and object store are separate

- **WHEN** `WORKSPACE_ROOT` and `OBJECT_STORE_ROOT` point to different directories
- **THEN** a forecast pipeline MUST still find raw, canonical, forcing, runtime output, parser input, and state snapshot artifacts through object-store URIs

#### Scenario: Temporary workspace remains local

- **WHEN** a worker needs scratch files or Slurm execution directories
- **THEN** it MUST use `WORKSPACE_ROOT` only for temporary or HPC-local execution files, not as the durable object-store root

#### Scenario: Real Slurm templates propagate object-store settings

- **WHEN** a real sbatch template launches a worker that reads or writes durable artifacts
- **THEN** the worker environment MUST include `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX` in addition to `WORKSPACE_ROOT`
