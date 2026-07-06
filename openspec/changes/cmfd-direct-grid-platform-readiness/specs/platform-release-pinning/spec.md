## ADDED Requirements

### Requirement: Readiness release manifest pins all migration-relevant identities
The platform SHALL publish a single readiness release manifest that records the exact identity of every software component, database schema, coordinate database, and algorithm version on which direct-grid migration evidence depends.

#### Scenario: Manifest records every required component identity
- **WHEN** the readiness release manifest is produced for a candidate migration baseline
- **THEN** it records its own `manifest_version` matching the committed versioned filename, and its `created_utc` creation timestamp
- **THEN** it records the SHUD-NWM repository commit or tag as `baseline_commit`
- **THEN** it records the forcing producer version
- **THEN** it records the canonical converter versions for gfs (`m1.4`), ifs (`m4.1`), and era5 (`m2.0`) as read from `workers/canonical_converter/converter.py`
- **THEN** it records the SHUD runtime commit or tag and the SHUD executable name (`shud_omp`)
- **THEN** it records the repository DB schema migration head (`db_schema_migration_repo_head`, the highest migration file in `db/migrations/`) and, as a distinct field, the deployment-applied DB schema migration version (`db_schema_migration_version`) resolved by live query on node-27's active primary database
- **THEN** a mismatch between the repository migration head and the deployment-applied version renders the schema identity unresolved
- **THEN** it records the PROJ/CRS database version
- **THEN** it records the mapping-builder algorithm version `nearest_cell_barycenter_geodesic_v1`
- **THEN** it records the forcing producer resource limits and the SHUD runtime direct-grid staging limits in effect on the deployment host.

#### Scenario: SHUD-OpenMP outer repo and solver submodule are recorded separately
- **WHEN** the manifest records the SHUD solver identity
- **THEN** it records the SHUD-OpenMP outer repository commit
- **THEN** it records the SHUD solver git-submodule exact commit as a distinct field
- **THEN** both commits are resolved on the deployment/build host that owns the production solver checkout (`git rev-parse HEAD` on the outer repository, `git submodule status` for the solver pin), not assumed from a remote default branch
- **THEN** the manifest does not treat the outer repository commit alone as sufficient to identify the solver.

#### Scenario: Missing required identity blocks readiness
- **WHEN** any required component, schema, CRS, or algorithm identity is absent, empty, or unresolved
- **THEN** the manifest is not accepted as a readiness baseline
- **THEN** a committed manifest completeness check (read-only script or per-field checklist) fails on the missing field and its failure blocks all evidence tasks
- **THEN** no readiness evidence run may be declared valid against that incomplete manifest.

### Requirement: Readiness manifest is immutable and checksum-bound
The readiness release manifest SHALL be an immutable, committed evidence file whose content is bound by a SHA-256 checksum so that later evidence can be verified against the exact pinned baseline.

#### Scenario: Manifest is committed with a content checksum
- **WHEN** the readiness release manifest is finalized
- **THEN** it is stored as a committed evidence file in the change's evidence package
- **THEN** a SHA-256 checksum of the manifest content is recorded
- **THEN** any evidence artifact that claims readiness references the manifest checksum it was produced against.

#### Scenario: Manifest edits require a new version
- **WHEN** any pinned identity must change after publication
- **THEN** a new versioned manifest is produced rather than editing the published manifest in place
- **THEN** the new manifest's `manifest_version` matches its new committed filename
- **THEN** the prior manifest and its checksum remain available for audit.

### Requirement: Pinned identities are read from authoritative sources
The manifest values SHALL be derived from the authoritative in-repository, deployment-host, or declared-specification sources for each component, not hand-entered or approximated, and each identity's derivation source SHALL be recorded.

#### Scenario: Component versions trace to authoritative sources
- **WHEN** a pinned identity is recorded in the manifest
- **THEN** the converter versions trace to `workers/canonical_converter/converter.py`
- **THEN** the producer version and resource limits trace to `workers/forcing_producer/producer.py` (including any deployment env overrides in effect)
- **THEN** the runtime identity and staging limits trace to `workers/shud_runtime/runtime.py`
- **THEN** the repository schema migration head traces to the highest migration file in `db/migrations/`, and the deployment-applied schema version traces to a live query on node-27's active primary database
- **THEN** `shud_openmp_outer_commit` and `shud_solver_submodule_commit` trace to `git rev-parse HEAD` and `git submodule status` executed on the deployment/build host that owns the production solver checkout
- **THEN** `proj_crs_database_version` traces to the PROJ installation on the deployment host (PROJ release string plus `proj.db` layout/build metadata queried there), not to documentation
- **THEN** `mapping_builder_algorithm_version` traces to its declared authority — the migration source-of-truth §6.1 algorithm identifier (`docs/ForcingReplace/CMFD 建模资产向 IFSGFS Direct-Grid 的安全迁移.md`) — which remains the authoritative source until the `forcing-mapping-asset-build` change lands an in-repository implementation source
- **THEN** the recorded source location for each identity is captured in `source_locations` in the evidence so a reviewer can re-derive the value.
