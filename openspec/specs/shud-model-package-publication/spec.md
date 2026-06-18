# shud-model-package-publication Specification

## Purpose
TBD - created by archiving change m9-basins-model-assets. Update Purpose after archive.
## Requirements
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

#### Scenario: Required runtime and GIS roles are revalidated before publication

- **WHEN** a selected inventory model claims `status=valid` and `default_publish_eligible=true` but omits canonical required SHUD runtime or GIS sidecar roles from `required_files`
- **THEN** package publication refuses the inventory with stable error code `BASINS_REQUIRED_FILES_MISSING`
- **AND** it does not publish an immutable package or local manifest that omits required runtime/GIS assets
- **WHEN** `required_files` contains extra non-canonical paths or roles after the canonical runtime/GIS files are present
- **THEN** package publication refuses the inventory with stable error code `BASINS_REQUIRED_FILES_NON_CANONICAL`
- **AND** it does not publish package entries or local manifests for the extra inventory-controlled paths

#### Scenario: Package publication is idempotent

- **WHEN** publication is run again for unchanged source files and the same target version
- **THEN** it returns `already_done` or equivalent status without rewriting a different package checksum
- **AND** benign inventory JSON formatting, unrelated inventory fields, unrelated model records, or raw inventory checksum changes SHALL NOT change the package checksum when selected model package material is unchanged
- **AND** the manifest still records `source_inventory_checksum` as evidence separate from the package checksum

#### Scenario: Source checksum changes for same target version

- **WHEN** publication is run for the same target version but source file checksums differ from the existing package manifest
- **THEN** it refuses to overwrite the package, reports stable error code `BASINS_PACKAGE_CHECKSUM_CONFLICT`, and preserves the existing manifest/package
- **AND** #135 provides no force-overwrite behavior; users must choose a new version when source checksums change

#### Scenario: Inventory source paths are revalidated

- **WHEN** a Basins inventory record contains absolute source paths or input/forcing paths that do not match `resolved_root` plus the model root-relative path
- **THEN** package publication refuses the inventory with stable error code `BASINS_INVENTORY_PATH_MISMATCH` or `BASINS_PACKAGE_PATH_UNSAFE`
- **AND** it does not publish package files or a manifest from attacker-controlled absolute paths outside the inventory root

#### Scenario: Publication uses a local object-store lock and verified writes

- **WHEN** a package manifest does not already exist for `models/<model_id>/<version>/manifest.json`
- **THEN** publication SHALL acquire an exclusive `models/<model_id>/<version>/.publish.lock` marker before writing package objects
- **AND** an existing lock SHALL fail deterministically with `BASINS_PACKAGE_PUBLISH_IN_PROGRESS`
- **AND** an existing unchanged manifest SHALL still return `already_done` without requiring the lock
- **AND** package object entries SHALL record size and SHA-256 from the exact bytes written and verified in the object store before the final manifest is written
- **AND** requested local `--output` manifest JSON SHALL only be written after the object-store manifest has been written and verified
- **AND** package object verification SHALL stream from the resolved object path in chunks instead of loading full object bytes into memory

#### Scenario: Publication command failure payload

- **WHEN** `publish-basins` fails
- **THEN** stderr contains JSON with `error_code`, `message`, and relevant `model_id`, `version`, `path`, or `manifest_uri` fields
- **AND** the command does not emit a success payload claiming `status=published`
- **AND** requested local output JSON write failures SHALL use stable error code `BASINS_PACKAGE_OUTPUT_WRITE_FAILED`
- **AND** malformed or non-UTF-8 inventory input SHALL use stable error code `BASINS_INVENTORY_INVALID` rather than an uncaught traceback
- **AND** stale inventory source-file stat/read failures during planning or checksum calculation SHALL fail with structured JSON including `model_id`, `version`, source `path`, and `manifest_uri`

### Requirement: Historical forcing is represented without accidental bulk duplication

The system SHALL record historical forcing CSV metadata separately from the runtime model input package and SHALL only copy forcing CSV payloads when explicitly requested.

#### Scenario: Forcing metadata inventory

- **WHEN** a model has CMFD forcing CSV files under `forcing/` or `focing/`
- **THEN** the package manifest records the forcing directory, CSV count, time coverage when parsable from file headers, and aggregate checksum metadata
- **AND** header/time evidence sampling SHALL be bounded by recorded file/byte/line limits while aggregate count, bytes, and checksum are computed by streaming file metadata and hashes
- **AND** the file sampling limit SHALL count sampled CSV files rather than unique headers, so duplicate headers cannot cause unbounded time-evidence reads

#### Scenario: Runtime package excludes bulk forcing by default

- **WHEN** publication runs without an explicit historical forcing copy option
- **THEN** the runtime model package excludes the full forcing CSV payloads but retains forcing metadata needed for migration planning
- **AND** the package file list contains no `forcing/*.csv` or `focing/*.csv` payload entries

#### Scenario: Historical forcing copy is explicit

- **WHEN** publication runs with an explicit option to copy historical forcing payloads
- **THEN** forcing CSV files are written under a separate object-store prefix and the manifest records forcing payload URI, file count, and checksum evidence
- **AND** copied forcing payloads SHALL be streamed to object storage without reading whole files into memory

#### Scenario: Symlink descendants are rejected during package traversal

- **WHEN** runtime, calibration, forcing, or migration evidence traversal encounters a symlink descendant below the selected source root
- **THEN** the command refuses the traversal with stable error code `BASINS_PACKAGE_PATH_UNSAFE`
- **AND** it emits structured JSON rather than an uncaught traceback

#### Scenario: Explicit package source paths reject symlinks

- **WHEN** `input_dir`, `forcing_dir`, `CALIB`, or required runtime/GIS files are symlinks below the selected model source root, even if they resolve inside the Basins source root
- **THEN** package publication refuses the path with stable error code `BASINS_PACKAGE_PATH_UNSAFE`
- **AND** a symlink Basins discovery root itself remains supported when the selected model source root resolves to real copied data

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
- **AND** requested local report write failures SHALL use stable error code `BASINS_MIGRATION_REPORT_WRITE_FAILED`

