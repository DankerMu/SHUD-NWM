# qdown-forcing-copyback Specification

## Purpose
TBD - created by archiving change issue-493-forcing-copyback. Update Purpose after archive.
## Requirements
### Requirement: q_down publish mirrors forcing packages

The q_down publisher MUST mirror each successfully published run's referenced forcing package from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same normalized `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` keyspace.

#### Scenario: Successful forcing package copyback

- **WHEN** q_down publish selects `run-a` with `forcing_version_id=forcing-1`, and `met.forcing_version` references `forcing/gfs/2024060112/basin-1/model-1/` with a `checksum` equal to the SHA-256 of source `forcing_package.json`
- **THEN** the shared object-store contains `forcing/gfs/2024060112/basin-1/model-1/forcing_package.json` with bytes identical to the source
- **AND** the run product copyback under `runs/<run_id>` still occurs.

#### Scenario: Shared forcing package deduplication

- **WHEN** multiple published q_down runs reference the same normalized forcing package key
- **THEN** the publisher copies that package once
- **AND** copyback lineage records one forcing package entry for that key.

### Requirement: forcing package copyback fails loudly on missing or unsafe metadata

The q_down publisher MUST preserve q_down run discovery even when forcing metadata is missing, then fail copyback validation with a stable `PublishError` before publishing display artifacts.

#### Scenario: Missing forcing metadata fails publish

- **WHEN** a selected q_down run lacks `met.forcing_version`, `forcing_package_uri`, or checksum
- **THEN** publish fails with details containing `run_id`, `forcing_version_id`, and the missing field such as `forcing_version`, `forcing_package_uri`, or `checksum`
- **AND** no new stable q_down display artifact is written.

#### Scenario: Unsafe forcing key fails publish

- **WHEN** a forcing package reference normalizes outside the exact `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` shape or uses traversal, absolute path, wrong prefix, empty segment, symlink-backed source tree, or a regular file where the source directory is expected
- **THEN** publish fails with a stable copyback error
- **AND** error details contain `run_id`, `forcing_version_id`, and the normalized `object_key` when it is known
- **AND** no forcing package is written under `NHMS_PUBLISHED_ARTIFACT_ROOT`.

### Requirement: forcing package manifest integrity is verified before copyback

The q_down publisher MUST validate `forcing_package.json` exists and its SHA-256 checksum matches `met.forcing_version.checksum` before copying a forcing package.

#### Scenario: Manifest checksum mismatch fails publish

- **WHEN** `forcing_package.json` exists but its SHA-256 does not match `met.forcing_version.checksum` or lineage `forcing_package_manifest_checksum`
- **THEN** q_down publish fails before display artifact writes
- **AND** the error details identify the normalized forcing package key.

#### Scenario: Same-package manifest files are present

- **WHEN** `forcing_package.json` lists files within the same forcing package
- **THEN** copyback validation verifies those files exist before copying the package.

### Requirement: Publisher copyback same-root branches SHALL be decided by filesystem identity rather than resolved-path string equality

The publisher's run-product copyback branch and its q_down copyback branch SHALL decide "the copyback root and the object-store root are the same storage location" by comparing the `(st_dev, st_ino)` identity of the two directories through the shared no-follow directory-identity probe, rather than by comparing their resolved path strings, so that a same-inode alias cannot be mistaken for a distinct root and mirrored across.

Both branches SHALL keep returning their existing skip result with reason `copyback_root_matches_object_store_root`, and a failure of the identity probe SHALL surface through the publisher's existing copyback-failure error rather than being swallowed.

The publisher's existing evaluation order, in which copyback-root preparation performs its overlap checks before either same-root branch is reached, SHALL be preserved rather than reordered. A typical alias pair — two parallel mount points — is neither string-equal nor string-contained, so it reaches the identity branch; and where a containing alias could be constructed, the overlap refusal that fires first is itself fail-closed, so no bypass results from the retained ordering.

Both operands at both branches are already-resolved paths, which is the precondition the shared probe requires.

#### Scenario: A same-inode alias copyback root takes the publisher run-product skip branch

- **GIVEN** the publisher is asked to copy back run products with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root branch is evaluated
- **THEN** the call returns its existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** no run products are copied to the aliased root

#### Scenario: A same-inode alias copyback root takes the publisher q_down skip branch

- **GIVEN** the publisher is asked to copy back q_down products with an object-store root and a copyback root whose resolved path strings differ
- **AND** both directories report the same `(st_dev, st_ino)` filesystem identity
- **WHEN** the same-root branch is evaluated
- **THEN** the call returns its existing skip result with reason `copyback_root_matches_object_store_root`
- **AND** no q_down products or forcing packages are copied to the aliased root

#### Scenario: Equal configured roots keep their current skip behavior

- **GIVEN** the copyback root and the object-store root are configured as the same path
- **WHEN** either copyback branch is evaluated
- **THEN** the identity comparison holds trivially and the same skip result is returned as before the change

