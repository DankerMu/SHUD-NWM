## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: Published package checksums stay reconstructable across schema generations

Any component that reconstructs `package_checksum` from a stored manifest SHALL select the checksum material shape declared by that manifest's own `schema_version`, so manifests published before the forcing-identity migration continue to verify. Where the stored manifest does not carry enough evidence to reconstruct, the component SHALL report a recorded reconstruction limitation rather than a verification failure.

#### Scenario: Pre-migration manifest still verifies

- **WHEN** production-closure validation reconstructs `package_checksum` for a manifest published with the pre-migration schema version
- **THEN** the reconstruction uses the pre-migration forcing material shape and the checksum matches the stored value

#### Scenario: Post-migration manifest verifies with the reduced material

- **WHEN** the same validation runs against a manifest published after the migration
- **THEN** the reconstruction uses the reduced forcing material and the checksum matches the stored value
