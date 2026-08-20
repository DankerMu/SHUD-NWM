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

### Requirement: The forcing-copyback backfill same-root guard SHALL be decided by filesystem identity rather than resolved-path string equality

The backfill's same-root rejection SHALL decide sameness by comparing the `(st_dev, st_ino)` identity of the copyback root and the object-store root through the shared no-follow directory-identity probe, rather than by comparing their resolved path strings, so that a same-inode alias cannot be mistaken for a distinct root and copied across.

The existing failure surface SHALL be preserved exactly: the same `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` error with `details.reason` `copyback_root_matches_object_store_root`, and the existing overlap check kept as resolved-path string comparison.

#### Scenario: A same-inode alias copyback root is rejected by the backfill

- **GIVEN** the backfill is configured with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the copyback-root boundary is validated
- **THEN** the backfill fails closed with `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` and `details.reason` `copyback_root_matches_object_store_root`
- **AND** no objects are copied

### Requirement: Each backfill same-root call site SHALL keep its current error posture when the identity probe fails

The backfill's same-root rejection SHALL receive already-computed root identities rather than probing the filesystem itself, so that every call site owns its probe and its failure posture explicitly.

The object-store side identity SHALL be computed once, where the object-store root is already verified, and carried alongside that verified path; a failure of that probe SHALL surface as the existing object-store-root unsafety error, never as a copyback-root diagnosis. Each call site is then responsible only for the copyback side.

The apply path SHALL wrap its probe and raise `COPYBACK_ROOT_UNSAFE`, matching its strict sibling, and SHALL NOT let a filesystem error escape into the CLI's generic handler where it would be reported as a generic backfill failure instead of a copyback-root diagnosis. The raw-path short-circuit, which fires before resolution when both configured strings are equal, SHALL NOT probe at all and SHALL compare the object-store identity with itself. The existing-root pre-check SHALL keep returning silently when its probe fails, exactly as it returns silently today when the copyback root is absent or unreadable. The dry-run path SHALL keep raising `COPYBACK_ROOT_UNSAFE`.

#### Scenario: Absent or unreadable copyback roots keep their current lenient handling

- **GIVEN** the backfill's existing-root pre-check runs against a copyback root that does not yet exist
- **WHEN** the identity comparison would need a probe of that root
- **THEN** the pre-check returns without raising, exactly as before the change
- **AND** a distinct, existing copyback root still passes the boundary check and proceeds

#### Scenario: An apply-path probe failure is diagnosed as an unsafe copyback root

- **GIVEN** the backfill runs in apply mode and the identity probe of the prepared copyback root fails with a filesystem error
- **WHEN** the same-root boundary is evaluated
- **THEN** the backfill raises `COPYBACK_ROOT_UNSAFE`
- **AND** it does not surface as the CLI's generic backfill failure code

#### Scenario: An object-store-root probe failure is diagnosed on the object-store side

- **GIVEN** the identity probe of the object-store root fails with a filesystem error
- **WHEN** the backfill verifies the object-store root
- **THEN** it raises the existing object-store-root unsafety error
- **AND** the reported details name the object-store root, not the copyback root

