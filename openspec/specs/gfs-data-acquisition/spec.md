# gfs-data-acquisition Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
### Requirement: Cycle discovery

The GFS adapter SHALL implement `discover_cycles()` to identify available GFS forecast cycles for a given date. It MUST return the four standard NOAA/NCEP issuance hours (00, 06, 12, 18 UTC) and indicate whether each cycle's data is available on the remote server.

#### Scenario: Discover all four cycles for a given date

- **WHEN** `discover_cycles(date="2026-05-07")` is called
- **THEN** the adapter returns exactly four cycle entries: `2026050700`, `2026050706`, `2026050712`, `2026050718`
- **THEN** each entry includes a `cycle_time` (ISO 8601), `cycle_hour` (0/6/12/18), and `available` boolean flag
- **THEN** for each available cycle, a `met.forecast_cycle` row is upserted with `cycle_id` (PK), `source_id` (FK), `cycle_time`, `issue_time`, and `status='discovered'` (met.cycle_status ENUM)

#### Scenario: Cycle availability reflects remote server state

- **WHEN** `discover_cycles()` is called and the 18Z cycle has not yet been published by NOAA
- **THEN** cycles 00, 06, 12 are marked `available=true`
- **THEN** cycle 18 is marked `available=false`

#### Scenario: Cycle discovery populates met.data_source on first run

- **WHEN** `discover_cycles()` runs and no `met.data_source` record exists for GFS
- **THEN** a new `met.data_source` row is inserted with `source_id` (PK), `source_name="gfs"`, `source_type="forecast"`, `status='enabled'` (met.source_status ENUM), `native_format="GRIB2"`, and `adapter_name="gfs_adapter"`

---

### Requirement: Manifest generation

The GFS adapter SHALL implement `build_manifest()` to produce a download manifest for a specific forecast cycle. The manifest MUST enumerate all required GRIB2 files covering the 7 variables across the configured forecast hour range, including expected file paths, variable names, time range, and expected checksums (when available from the provider).

#### Scenario: Build manifest for a complete cycle

- **WHEN** `build_manifest(cycle_time="2026050700", forecast_hours=[0, 3, 6, ..., 168])` is called (M1 default: 7 days, step 3h)
- **THEN** the manifest contains one entry per forecast-hour per required variable
- **THEN** each entry includes `remote_url`, `local_key` (matching `raw/gfs/2026050700/` prefix), `variable`, `forecast_hour`, and `expected_checksum` (if available)
- **THEN** the manifest is persisted at `manifest_uri` recorded on the `met.forecast_cycle` row

#### Scenario: Manifest includes all 7 required variables

- **WHEN** a manifest is generated for any cycle
- **THEN** the manifest covers variables: `tmp2m`, `apcp`, `rh2m`, `u10m`, `v10m`, `pressfc`, `dswrf`
- **THEN** no extra variables are included beyond the configured set

#### Scenario: Manifest records time range metadata

- **WHEN** the manifest is built
- **THEN** it includes `cycle_time`, `first_forecast_hour`, `last_forecast_hour`, `variable_count`, and `total_file_count` in its header metadata

---

### Requirement: Raw download

The GFS adapter SHALL implement `download_plan()` to download GRIB2 files from NOAA servers according to a manifest. Raw files MUST be stored at `raw/{source}/{cycle_time}/` in the configured S3-compatible object storage. The adapter SHALL poll until all required forecast hours are available, respecting the GFS publication latency.

#### Scenario: Download all files in a manifest

- **WHEN** `download_plan(manifest)` is called with a valid manifest
- **THEN** the `met.forecast_cycle` record transitions to `status='downloading'` (met.cycle_status ENUM)
- **THEN** each GRIB2 file listed in the manifest is downloaded and stored at `raw/gfs/{cycle_time}/{filename}`
- **THEN** the download progress is trackable (files completed / total files)

#### Scenario: Latency-aware polling for incomplete publications

- **WHEN** `download_plan()` is called and some forecast hours are not yet published on the NOAA server
- **THEN** the adapter polls at a configurable interval (default 5 minutes) until the required files appear
- **THEN** polling times out after a configurable maximum wait (default 6 hours) and raises a descriptive error

#### Scenario: Forecast cycle record is written to database

- **WHEN** all files for a cycle are successfully downloaded
- **THEN** a `met.forecast_cycle` record is created or updated with `cycle_id` (PK), `source_id` (FK to met.data_source), `cycle_time`, `issue_time`, `status='raw_complete'` (met.cycle_status ENUM), and `manifest_uri`
- **THEN** `retry_count` is set to 0 on success

---

### Requirement: Download verification

The GFS adapter SHALL implement `verify_manifest()` to verify the integrity of all downloaded files against the manifest. Verification MUST check file existence, file size, and checksum (MD5 or SHA-256). Every output file MUST carry a `quality_flag`.

#### Scenario: All files pass verification

- **WHEN** `verify_manifest(manifest)` is called after a successful download
- **THEN** each file's checksum matches the manifest expectation
- **THEN** the overall verification result is `passed`
- **THEN** `status='raw_complete'` is confirmed on the `met.forecast_cycle` record

#### Scenario: Corrupted file detected during verification

- **WHEN** `verify_manifest()` finds a file whose checksum does not match
- **THEN** the file is flagged and the specific file path and expected vs. actual checksum are reported
- **THEN** the overall verification result is `partial_fail` with a list of failed files

#### Scenario: Missing file detected during verification

- **WHEN** `verify_manifest()` finds that a file listed in the manifest does not exist in object storage
- **THEN** the file is reported as missing
- **THEN** the `met.forecast_cycle` record is updated with `status='failed_download'` (met.cycle_status ENUM), `error_code` and `error_message` describing the missing file

---

### Requirement: Idempotent execution

The GFS adapter MUST be idempotent: re-running any operation for a cycle that has already been successfully processed SHALL NOT re-download or overwrite data, and SHALL NOT create duplicate database records.

#### Scenario: Skip download when files already exist with matching checksum

- **WHEN** `download_plan()` is called for a cycle whose files already exist in object storage with matching checksums
- **THEN** no files are re-downloaded
- **THEN** the adapter returns status `already_done` for each file
- **THEN** total download byte count is zero

#### Scenario: Skip cycle record creation when already exists

- **WHEN** the adapter processes a cycle that already has a `met.forecast_cycle` record with `status='raw_complete'`
- **THEN** no new database record is inserted
- **THEN** the existing record is not modified

#### Scenario: Re-download triggered only when checksum mismatch detected

- **WHEN** a file exists in object storage but its checksum does not match the manifest
- **THEN** the file is re-downloaded and overwritten
- **THEN** the `met.forecast_cycle` record is updated to reflect the re-verification result

---

### Requirement: Error handling

The GFS adapter MUST handle common failure modes gracefully: source unavailability, network errors, partial downloads, and corrupt files. Errors MUST be logged with sufficient context for diagnosis and MUST NOT leave the system in an inconsistent state.

#### Scenario: Source server is unreachable

- **WHEN** the NOAA/NCEP server is unreachable during `download_plan()`
- **THEN** the adapter retries with exponential backoff (configurable max retries, default 3)
- **THEN** if all retries fail, the adapter raises a descriptive exception including the URL and error details
- **THEN** the `met.forecast_cycle` record is set to `status='failed_download'` (met.cycle_status ENUM) with `retry_count` reflecting total attempts, `error_code` and `error_message` capturing the failure details
- **THEN** the failure is logged to `ops.audit_log`

#### Scenario: Partial download due to network interruption

- **WHEN** a download is interrupted mid-transfer
- **THEN** the partially downloaded file is not committed to object storage (or is cleaned up)
- **THEN** the `met.forecast_cycle` record remains in `status='downloading'` (met.cycle_status ENUM)
- **THEN** a subsequent `download_plan()` call re-attempts only the incomplete files

#### Scenario: HTTP 404 for expected file

- **WHEN** a specific GRIB2 file returns HTTP 404 during download
- **THEN** the adapter logs a warning with the file URL and forecast hour
- **THEN** the file is marked as `unavailable` in the download status
- **THEN** the `met.forecast_cycle` record is updated with `status='failed_download'`, `error_code='HTTP_404'`, and `error_message` including the missing file URL

