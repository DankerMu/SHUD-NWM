## ADDED Requirements

### Requirement: Valid Basins models are published as immutable packages

The system SHALL publish each validated Basins SHUD model into an immutable object-store package containing runtime input files, selected calibration metadata, GIS sidecars, and a package manifest with checksums.

The manifest schema SHALL include at least these fields:

- `schema_version`
- `model_id`
- `version`
- `model_package_uri`
- `manifest_uri`
- `package_checksum`
- `source_inventory_checksum`
- `source_path`
- `resolved_source_path`
- `source_is_symlink`
- `included_files[]`
- `forcing`
- `calibration`
- `created_at`

Each `included_files[]` entry SHALL include `relative_path`, `object_uri`, `size_bytes`, `sha256`, and `role`. Runtime package files SHALL use object keys under `models/<model_id>/<version>/package/`; manifest JSON SHALL use `models/<model_id>/<version>/manifest.json`; explicit forcing copies SHALL use `models/<model_id>/<version>/forcing/`.

The manifest itself SHALL be represented in `included_files[]` with `role=manifest` and `relative_path=manifest.json`. To avoid recursive manifest checksums, `package_checksum` SHALL exclude the manifest self-entry and cover source package plus forcing evidence; the manifest self-entry `sha256` SHALL cover the deterministic manifest payload before that self-entry is appended, and its `size_bytes` SHALL record the final object-store manifest byte length.

#### Scenario: Package publication writes manifest and checksum

- **WHEN** a validated Basins model is published to `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX`
- **THEN** the output includes a stable `model_package_uri`, a package checksum, a manifest JSON, and per-file checksums for all included runtime input and GIS files
- **AND** the manifest identifies the source inventory schema version, source path, resolved source path, source symlink status, basin/model IDs, package version, included file list, excluded forcing payload policy, and creation timestamp
- **AND** the included file list contains a manifest entry whose URI points at `manifest_uri` and whose byte-size evidence matches the object-store manifest

#### Scenario: Partial models are not published by default

- **WHEN** a Basins inventory model has `status=partial` or `default_publish_eligible=false`
- **THEN** package publication refuses the model with stable error code `BASINS_MODEL_NOT_PUBLISHABLE` unless a later explicit partial-acceptance option is defined

#### Scenario: Package publication is idempotent

- **WHEN** publication is run again for unchanged source files and the same target version
- **THEN** it returns `already_done` or equivalent status without rewriting a different package checksum

#### Scenario: Source checksum changes for same target version

- **WHEN** publication is run for the same target version but source file checksums differ from the existing package manifest
- **THEN** it refuses to overwrite the package, reports stable error code `BASINS_PACKAGE_CHECKSUM_CONFLICT`, and preserves the existing manifest/package
- **AND** #135 provides no force-overwrite behavior; users must choose a new version when source checksums change

#### Scenario: Publication command failure payload

- **WHEN** `publish-basins` fails
- **THEN** stderr contains JSON with `error_code`, `message`, and relevant `model_id`, `version`, `path`, or `manifest_uri` fields
- **AND** the command does not emit a success payload claiming `status=published`

### Requirement: Historical forcing is represented without accidental bulk duplication

The system SHALL record historical forcing CSV metadata separately from the runtime model input package and SHALL only copy forcing CSV payloads when explicitly requested.

#### Scenario: Forcing metadata inventory

- **WHEN** a model has CMFD forcing CSV files under `forcing/` or `focing/`
- **THEN** the package manifest records the forcing directory, CSV count, time coverage when parsable from file headers, and aggregate checksum metadata

#### Scenario: Runtime package excludes bulk forcing by default

- **WHEN** publication runs without an explicit historical forcing copy option
- **THEN** the runtime model package excludes the full forcing CSV payloads but retains forcing metadata needed for migration planning
- **AND** the package file list contains no `forcing/*.csv` or `focing/*.csv` payload entries

#### Scenario: Historical forcing copy is explicit

- **WHEN** publication runs with an explicit option to copy historical forcing payloads
- **THEN** forcing CSV files are written under a separate object-store prefix and the manifest records forcing payload URI, file count, and checksum evidence

### Requirement: Production migration rejects symlink-only evidence

The system SHALL provide a migration report command that distinguishes development symlinks from production data copies and rejects symlink-only production migration evidence.

#### Scenario: Symlink target fails production migration evidence

- **WHEN** production migration evidence is generated for `/volume/data/nwm/Basins`
- **THEN** it states that target environments must contain actual copied data and fails if the target `Basins` path is a symlink
- **AND** the failure payload uses stable error code `BASINS_MIGRATION_SYMLINK_TARGET`

#### Scenario: Copied target passes production migration evidence

- **WHEN** production migration evidence is generated for a real copied `Basins` directory
- **THEN** it exits successfully and records file count, byte count, inventory checksum, source-to-target copy metadata, and `production_ready=true`

#### Scenario: Migration command failure payload

- **WHEN** `basins-migration-report` fails
- **THEN** stderr contains JSON with `error_code`, `message`, and the relevant `path`
