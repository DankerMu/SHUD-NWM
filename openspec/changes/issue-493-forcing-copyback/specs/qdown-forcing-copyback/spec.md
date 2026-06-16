## ADDED Requirements

### Requirement: q_down publish mirrors forcing packages

The q_down publisher MUST mirror each successfully published run's referenced forcing package from `OBJECT_STORE_ROOT` to `NHMS_OBJECT_STORE_COPYBACK_ROOT` under the same normalized `forcing/<source>/<cycle>/<basin_version_id>/<model_id>` keyspace.

#### Scenario: Successful forcing package copyback

- **WHEN** q_down publish selects `run-a` with `forcing_version_id=forcing-1`, and `met.forcing_version` references `forcing/gfs/2026061400/basin-1/model-1/` with a `checksum` equal to the SHA-256 of source `forcing_package.json`
- **THEN** the shared object-store contains `forcing/gfs/2026061400/basin-1/model-1/forcing_package.json` with bytes identical to the source
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
