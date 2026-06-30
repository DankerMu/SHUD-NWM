# forcing-copyback-backfill Specification

## Purpose
TBD - created by archiving change issue-494-forcing-backfill. Update Purpose after archive.
## Requirements
### Requirement: Historical forcing copyback backfill scans q_down-capable runs

The backfill command MUST scan `hydro.hydro_run` rows with display-ready parsed or published status that have q_down publish value, join those rows to `met.forcing_version`, and dedupe candidate work by normalized forcing package key.

#### Scenario: Historical q_down runs are discovered and deduped

- **WHEN** two eligible q_down runs reference forcing rows that normalize to the same `forcing/<source>/<cycle>/<basin>/<model>` key
- **THEN** the report counts both runs
- **AND** the command plans or applies one package action for that normalized key
- **AND** the report retains the related `run_id` and `forcing_version_id` evidence.

### Requirement: Backfill dry-run is the default

The backfill command MUST perform no target writes unless the operator passes an explicit `--apply` flag.

#### Scenario: Dry-run plans without writing

- **WHEN** an eligible run references a valid source package that is missing from `NHMS_OBJECT_STORE_COPYBACK_ROOT`
- **AND** the command is run without `--apply`
- **THEN** the report marks the package as copyable
- **AND** no target directory or file is created.

#### Scenario: Apply writes validated packages only

- **WHEN** the same valid source package is processed with `--apply`
- **THEN** the package is copied under the same normalized `forcing/...` key in `NHMS_OBJECT_STORE_COPYBACK_ROOT`
- **AND** the copied package manifest bytes match the source.

### Requirement: Backfill reuses publish-time forcing package validation

The backfill command MUST reuse the #493 forcing package key, source-tree, manifest, and checksum validation behavior rather than maintaining an independent validation rule set.

#### Scenario: Unsafe package references are rejected

- **WHEN** a candidate forcing package URI is legacy-shaped, absolute, traversal-based, wrong-prefix, wrong segment count, has empty segments, resolves to a symlink-backed source, or resolves to a regular file instead of a package directory
- **THEN** the command reports the candidate as failed or manual-handling
- **AND** the failure includes `run_id`, `forcing_version_id`, `forcing_package_uri`, and `reason`
- **AND** no package is copied.

#### Scenario: Manifest checksum mismatch is not copied

- **WHEN** `forcing_package.json` is missing or its SHA-256 does not match `met.forcing_version.checksum`
- **THEN** the command increments the missing-source or checksum-mismatch count as appropriate
- **AND** the package is not marked copied or already present.

### Requirement: Backfill report is auditable and rerunnable

The backfill command MUST emit an auditable JSON report containing aggregate counts and per-failure details.

#### Scenario: Report contains required aggregate counts

- **WHEN** the command completes
- **THEN** the report includes total run count, forcing version count, copyable package count, already-present checksum-consistent count, missing source count, checksum mismatch count, legacy key rejected count, copied count, and failure count.

#### Scenario: Already-present packages are idempotent

- **WHEN** `NHMS_OBJECT_STORE_COPYBACK_ROOT` already contains a package with a `forcing_package.json` checksum matching `met.forcing_version.checksum`
- **THEN** dry-run and apply both report the package as already present
- **AND** apply does not count it as copied.

### Requirement: Node-22 operator documentation describes execution and recovery

The repository MUST document the node-22 backfill command, required environment variables, dry-run/apply distinction, rerun behavior, and rollback boundaries.

#### Scenario: Operator follows documented command

- **WHEN** an operator reads the documentation
- **THEN** they can identify the dry-run command, the explicit `--apply` command, required env vars, that DB rows are not mutated, and how to rerun or manually roll back packages reported as copied.
