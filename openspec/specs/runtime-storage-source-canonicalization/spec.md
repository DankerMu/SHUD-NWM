# runtime-storage-source-canonicalization Specification

## Purpose
TBD - created by archiving change m6-system-hardening-alignment. Update Purpose after archive.
## Requirements
### Requirement: Durable artifacts use object-store root
All durable raw, canonical, forcing, run output, state, tile, and persisted log artifacts SHALL be read and written through `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX`.

#### Scenario: Workspace and object store are separate
- **WHEN** `WORKSPACE_ROOT` and `OBJECT_STORE_ROOT` point to different directories
- **THEN** a forecast pipeline MUST still find raw, canonical, forcing, runtime output, parser input, and state snapshot artifacts through object-store URIs

#### Scenario: Temporary workspace remains local
- **WHEN** a worker needs scratch files or Slurm execution directories
- **THEN** it MUST use `WORKSPACE_ROOT` only for temporary or HPC-local execution files, not as the durable object-store root

#### Scenario: Real Slurm templates propagate object-store settings
- **WHEN** a real sbatch template launches a worker that reads or writes durable artifacts
- **THEN** the worker environment MUST include `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX` in addition to `WORKSPACE_ROOT`

### Requirement: Logs have explicit storage semantics
Job log endpoints SHALL distinguish Slurm-native workspace logs from persisted object-store log URIs.

#### Scenario: Persisted log URI resolves through object store
- **WHEN** a pipeline job has a log URI under the configured object-store prefix
- **THEN** the API MUST resolve it through the object-store abstraction instead of `LOG_ROOT`

#### Scenario: Raw Slurm log path is constrained
- **WHEN** a pipeline job references a raw local Slurm log path
- **THEN** the API MUST constrain the path to the configured log root and reject traversal outside that root

### Requirement: Source IDs are normalized at storage boundaries
All adapters, converters, forcing producers, orchestrators, repositories, and seeds SHALL use a shared storage source-id normalization policy.

#### Scenario: Canonical storage ids are explicit
- **WHEN** a storage or repository boundary receives a source id
- **THEN** the normalized storage id MUST be `gfs` for GFS, `ERA5` for ERA5, and `IFS` for IFS

#### Scenario: GFS canonical products are discoverable by forcing
- **WHEN** GFS canonical conversion writes products with the normalized GFS storage id
- **THEN** forcing production for GFS MUST query the same normalized id and find those products

#### Scenario: ERA5 fallback can find GFS products
- **WHEN** ERA5 latency fallback needs GFS canonical products
- **THEN** fallback lookup MUST use the same normalized GFS storage id used by canonical conversion

#### Scenario: User input remains case-insensitive
- **WHEN** an operator requests source `GFS`, `gfs`, or `Gfs`
- **THEN** the storage and repository layer MUST receive the same normalized source id

### Requirement: Storage split is regression-tested
The automated tests SHALL include at least one worker-chain or repository-level test with `WORKSPACE_ROOT != OBJECT_STORE_ROOT`.

#### Scenario: Split-root regression test catches path drift
- **WHEN** a future module accidentally initializes durable object storage from `WORKSPACE_ROOT`
- **THEN** the split-root test MUST fail before the change is accepted

