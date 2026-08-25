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

The manifest itself SHALL be represented in `included_files[]` with `role=manifest` and `relative_path=manifest.json`. To avoid recursive manifest checksums, `package_checksum` SHALL exclude the manifest self-entry and cover the source package contents; the manifest self-entry `sha256` SHALL cover the deterministic manifest payload before that self-entry is appended, and its `size_bytes` SHALL record the final object-store manifest byte length.

Package identity SHALL be derived only from what the package contains plus the declared policy governing it. Concretely, the forcing contribution to `package_checksum` and to `content_sha256` SHALL be limited to the declared `policy` and `payload_copied` values, and SHALL NOT include `csv_count`, `byte_count`, `aggregate_checksum`, `copied_file_count`, or `copied_byte_count`. When forcing payloads are copied, those payloads SHALL enter identity as ordinary `included_files[]` entries with `role=forcing`, exactly as calibration content does. The version-string source material SHALL NOT include `forcing_dir_original_name`.

#### Scenario: Package publication writes manifest and checksum

- **WHEN** a validated Basins model is published to `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX`
- **THEN** the output includes a stable `model_package_uri`, a package checksum, a manifest JSON, and per-file checksums for all included runtime input and GIS files
- **AND** the manifest identifies the source inventory schema version, source path, resolved source path, source symlink status, basin/model IDs, package version, included file list, excluded forcing payload policy, and creation timestamp
- **AND** the included file list contains a manifest entry whose URI points at `manifest_uri` and whose byte-size evidence matches the object-store manifest

#### Scenario: Excluded forcing payloads do not move package identity

- **WHEN** a model is published without the historical forcing copy option, its `forcing/` directory is then emptied of CSV payloads while the directory itself is retained, discovery is re-run, and the model is published again
- **THEN** `content_sha256`, the version-string source hash, `package_checksum`, and `source_inventory_checksum` are all identical to the first publication
- **AND** the same holds when forcing CSV bytes are mutated in place rather than removed

#### Scenario: Structural forcing changes remain visible

- **WHEN** the `forcing/` directory is removed outright rather than emptied, or its legacy `focing/` spelling is renamed
- **THEN** package identity changes, because `forcing_dir` and `forcing_dir_original_name` are structural source facts rather than payload evidence

#### Scenario: Copied forcing payloads still bind to identity

- **WHEN** publication runs with the explicit historical forcing copy option and a copied forcing CSV's bytes differ
- **THEN** `package_checksum` differs, because the copied payload is an `included_files[]` entry whose `sha256` is covered by the package checksum material

### Requirement: Historical forcing is represented without accidental bulk duplication

The system SHALL record historical forcing CSV metadata separately from the runtime model input package and SHALL only copy forcing CSV payloads when explicitly requested. When payloads are not copied, the system SHALL NOT read forcing CSV payload bytes to produce an aggregate payload checksum, because that checksum has no consumer once forcing leaves the identity material.

#### Scenario: Forcing metadata inventory

- **WHEN** a model has CMFD forcing CSV files under `forcing/` or `focing/`
- **THEN** the package manifest records the forcing directory, CSV count, byte count, and time coverage when parsable from file headers
- **AND** header/time evidence sampling SHALL be bounded by recorded file/byte/line limits while aggregate count and bytes are obtained from file metadata without reading payload bytes
- **AND** the file sampling limit SHALL count sampled CSV files rather than unique headers, so duplicate headers cannot cause unbounded time-evidence reads

#### Scenario: Runtime package excludes bulk forcing by default

- **WHEN** publication runs without an explicit historical forcing copy option
- **THEN** the runtime model package excludes the full forcing CSV payloads but retains forcing metadata needed for migration planning
- **AND** the package file list contains no `forcing/*.csv` or `focing/*.csv` payload entries
- **AND** no forcing CSV payload is read end-to-end, so publication cost SHALL NOT scale with historical forcing volume

#### Scenario: Historical forcing copy is explicit

- **WHEN** publication runs with an explicit option to copy historical forcing payloads
- **THEN** forcing CSV files are written under a separate object-store prefix and the manifest records forcing payload URI, file count, and checksum evidence
- **AND** copied forcing payloads SHALL be streamed to object storage without reading whole files into memory

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

### Requirement: Package refusal payloads carry the model's cause keys

The package publication refusal SHALL carry the refused model's health
causes in its structured payload: when a model is refused as not
publishable, the error payload includes the model's status and its
missing, invalid, and unreadable required-file collections, copied from
the inventory model record (empty when a caller-supplied inventory
predates a key) — the first three key names match the scheduler registry
publish channel, the fourth matches the discovery payload; no new
aliases.
The refusal predicate, error code, and message text stay byte-for-byte
unchanged, and every pre-existing payload key keeps its value, so receipt
consumers remain backward compatible; error instances raised without
cause details keep their existing payload byte-for-byte.

#### Scenario: a malformed-IC-header model's refusal names the file

WHEN a model whose IC header failed the discovery shape gate is refused
as not publishable
THEN the refusal payload's invalid-required-files entry names the
offending `*.cfg.ic` file alongside the model's status

#### Scenario: an unreadable-required-file model's refusal names the file

WHEN a model carrying an unreadable required file (partial status) is
refused as not publishable
THEN the refusal payload's unreadable-required-files entry names that
file

#### Scenario: pre-existing payload keys survive unchanged

WHEN any package refusal is raised
THEN error_code, message, model_id, version, and path keep their existing
values, and refusals raised without details keep their payload
byte-for-byte

### Requirement: Published packages never rewrite calibrated values

The system SHALL publish calibration files byte-identical to their source. No
publication path may alter a calibrated parameter value on the grounds that it
falls outside an operational bound.

Publication MAY still repair a *missing* required file by supplying a template
into a private staging copy, because that path adds an absent artifact rather
than overriding a value a human chose. Any such repair SHALL be recorded in the
publication receipt's `repairs` list. (The package manifest carries no repair
field for either repair kind; `publish_basins_package` takes no repair argument.
The receipt is the only recording seam that exists.)

#### Scenario: A calibration multiplier outside any historical bound is published unchanged

- **WHEN** a Basins model's `cfg.calib` declares `SOIL_ALPHA` or `GEOL_DMAC`
  whose product with the corresponding `para.*` column maximum exceeds any
  previously enforced operational bound
- **THEN** the published package's `cfg.calib` SHALL be byte-identical to the
  source `cfg.calib`
- **AND** publication SHALL NOT refuse on the grounds of that bound
- **AND** the publication receipt's `repairs` list SHALL contain no
  calibration repair entry

#### Scenario: Publication is a pure copy with respect to calibration

- **WHEN** a Basins model is published twice from an unchanged source
- **THEN** both packages' calibration files SHALL be byte-identical to the
  source and to each other

#### Scenario: A missing radiation template is still supplied and recorded

- **WHEN** a Basins model is missing only `*.tsd.rl` and template repair is
  requested
- **THEN** the package SHALL contain the supplied template
- **AND** the publication receipt's `repairs` list SHALL record the repair
- **AND** the model's calibration files SHALL remain byte-identical to source

### Requirement: Published package checksums stay reconstructable across schema generations

Any component that reconstructs `package_checksum` from a stored manifest SHALL select the checksum material shape declared by that manifest's own `schema_version`, so manifests published before the forcing-identity migration continue to verify. Where the stored manifest does not carry enough evidence to reconstruct, the component SHALL report a recorded reconstruction limitation rather than a verification failure.

#### Scenario: Pre-migration manifest still verifies

- **WHEN** production-closure validation reconstructs `package_checksum` for a manifest published with the pre-migration schema version
- **THEN** the reconstruction uses the pre-migration forcing material shape and the checksum matches the stored value

#### Scenario: Post-migration manifest verifies with the reduced material

- **WHEN** the same validation runs against a manifest published after the migration
- **THEN** the reconstruction uses the reduced forcing material and the checksum matches the stored value

