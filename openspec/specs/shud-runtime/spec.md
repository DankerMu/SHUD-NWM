# shud-runtime Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
### Requirement: Workspace preparation

The SHUD runtime adapter MUST prepare a local workspace directory before execution by pulling all required artifacts from object storage, verifying forcing package checksums, and applying a best-effort assertion that the forcing package's declared `PRCP` unit is `mm/day` before staging forcing into the SHUD `PRCP` column. The unit assertion MUST reuse the package manifest already fetched for checksum verification and MUST NOT introduce an additional network fetch. The assertion MUST fail the run ONLY when `units["PRCP"]` is explicitly present and, after case/whitespace normalisation, is not `mm/day`; every other condition (manifest unreadable, manifest over the read cap, manifest not valid JSON, `units` block absent, `PRCP` key absent, or `PRCP` value `None`) MUST be tolerated and the run MUST proceed, because package content integrity is already guaranteed by the checksum verified before the assertion.

#### Scenario: Staging rejects an explicit non-mm/day PRCP unit

- **WHEN** the forcing package manifest declares `units["PRCP"]` equal to a value other than `mm/day` (for example per-step `mm`)
- **THEN** the adapter MUST raise `SHUDRuntimeError` with error code `FORCING_PRCP_UNIT_MISMATCH`
- **AND** the error message MUST include both the observed unit and the expected `mm/day`
- **AND** no forcing files MUST be staged into the SHUD workspace.

#### Scenario: Staging accepts a mm/day PRCP unit

- **WHEN** the forcing package manifest declares `units["PRCP"]` equal to `mm/day`
- **THEN** the adapter MUST stage the forcing package normally
- **AND** the staged SHUD project forcing files MUST be present in the workspace.

#### Scenario: Staging tolerates missing unit metadata

- **WHEN** the forcing package manifest has no `units` block, or no `PRCP` entry within it
- **THEN** the adapter MUST NOT raise a unit-mismatch error
- **AND** the forcing package MUST be staged normally (backward compatibility with legacy packages).

#### Scenario: Unit assertion reuses the already-fetched package manifest

- **WHEN** the adapter verifies forcing package checksums for a manifest carrying `forcing.package_manifest_uri`
- **THEN** the PRCP unit assertion MUST read the same package manifest URI rather than issuing an additional remote fetch of a different artifact.

#### Scenario: Staging tolerates an unreadable or over-cap package manifest

- **WHEN** reading the package manifest for the PRCP unit peek fails (object missing, transient read error) or the manifest exceeds the read cap (for example a large multi-station manifest)
- **THEN** the adapter MUST NOT raise a unit-related error
- **AND** the forcing package MUST be staged normally, because content integrity is already guaranteed by the checksum verified before the unit peek.

#### Scenario: Staging tolerates a package manifest that is not valid JSON

- **WHEN** the package manifest read for the PRCP unit peek cannot be decoded as UTF-8 JSON
- **THEN** the adapter MUST NOT raise a unit-related error
- **AND** the forcing package MUST be staged normally.

### Requirement: Config generation

The adapter MUST generate or modify a `.cfg.para` configuration file that sets the correct simulation time window and output configuration. The `start_time` and `end_time` MUST match the forcing data coverage. For M1 (cold-start), no `.cfg.ic` initial condition file SHALL be referenced.

#### Scenario: Generate .cfg.para with correct time window

- **WHEN** the adapter prepares a forecast run with forcing covering `2024-01-01T00:00Z` to `2024-01-08T00:00Z`
- **THEN** a `.cfg.para` file MUST be written to `runs/{run_id}/input/`
- **THEN** the `START_TIME` parameter MUST be set to the forcing start time
- **THEN** the `END_TIME` parameter MUST be set to the forcing end time
- **THEN** the `OUTPUT_DIR` parameter MUST point to `runs/{run_id}/output/`
- **THEN** the `MODEL_OUTPUT_INTERVAL` parameter MUST be set to the configured time step (default: 1440 minutes for daily output)

#### Scenario: Cold-start mode sets init_state_id to NULL

- **WHEN** the adapter generates `.cfg.para` for an M1 forecast run
- **THEN** the configuration MUST NOT contain a reference to any `.cfg.ic` file
- **THEN** the `INIT_MODE` parameter (or equivalent) MUST be set to cold-start
- **THEN** the `hydro.hydro_run.init_state_id` column MUST be explicitly set to `NULL` (not omitted)

#### Scenario: Config generation uses template with variable substitution

- **WHEN** the adapter generates `.cfg.para`
- **THEN** it MUST use a base template from the model package and substitute only the time window, output directory, and run-specific parameters
- **THEN** model-specific parameters (mesh resolution, calibration coefficients) MUST be preserved from the original template

### Requirement: SHUD execution

The adapter MUST execute `shud` via `subprocess.run()` (or equivalent) and capture the exit code, stdout, and stderr. A non-zero exit code MUST be treated as a failure. The CLI entry point is `nhms-shud-runtime execute --manifest <manifest.json>`. The Slurm sbatch template `run_shud_forecast.sbatch` MUST define the execution environment.

#### Scenario: Successful shud execution

- **WHEN** `shud` is invoked with the prepared workspace and exits with code 0
- **THEN** stdout and stderr MUST be captured and written to `runs/{run_id}/logs/shud_stdout.log` and `runs/{run_id}/logs/shud_stderr.log`
- **THEN** the `hydro.hydro_run.status` MUST be updated from `running` to `succeeded`
- **THEN** the `hydro.hydro_run.updated_at` timestamp MUST be set

#### Scenario: shud exits with non-zero code

- **WHEN** `shud` exits with a non-zero exit code (e.g., segmentation fault, input error)
- **THEN** the adapter MUST capture stdout and stderr to log files
- **THEN** the `hydro.hydro_run.status` MUST be updated to `failed`
- **THEN** the `hydro.hydro_run.error_code` MUST store the exit code and `hydro.hydro_run.error_message` MUST include the last 50 lines of stderr
- **THEN** the adapter CLI MUST exit with a non-zero code

#### Scenario: CLI invocation with manifest

- **WHEN** a user or Slurm job runs `nhms-shud-runtime execute --manifest manifest.json`
- **THEN** the adapter MUST read the run manifest JSON containing nested structure: `model.model_id`, `model.model_package_uri`, `forcing.forcing_uri`, `outputs.output_uri`, `source_id`, `cycle_time`, and `initial_state.ic_file_uri`
- **THEN** the adapter MUST execute the full sequence: workspace preparation → config generation → shud execution → output verification → result upload

### Requirement: Output completeness verification

After `shud` execution, the adapter MUST verify that the expected output files exist and are complete. The `.rivqdown` file MUST exist in the output directory. The row count of `.rivqdown` MUST match the expected number of time steps based on the simulation time window and output interval.

#### Scenario: Output file exists with correct row count

- **WHEN** `shud` completes successfully for a 7-day forecast with daily output (7 time steps)
- **THEN** the file `runs/{run_id}/output/{basin}.rivqdown` MUST exist
- **THEN** the file MUST contain a header row plus exactly 7 data rows (one per time step)
- **THEN** the verification MUST pass and execution continues to upload

#### Scenario: Output file is missing

- **WHEN** `shud` exits with code 0 but `.rivqdown` is not found in the output directory
- **THEN** the adapter MUST set `hydro.hydro_run.status` to `failed`
- **THEN** the `error_code` MUST be set and `error_message` MUST indicate: "Output verification failed: .rivqdown file not found"

#### Scenario: Output file has incorrect row count

- **WHEN** `.rivqdown` exists but contains only 5 data rows instead of expected 7
- **THEN** the adapter MUST set `hydro.hydro_run.status` to `failed`
- **THEN** the `error_message` MUST indicate the expected vs actual row count

### Requirement: Result upload to object storage

Upon successful execution and output verification, the adapter MUST upload all output files to `runs/{run_id}/output/` and all log files to `runs/{run_id}/logs/` in object storage (MinIO/S3). The upload MUST be atomic per file and the adapter MUST verify upload success.

#### Scenario: Upload output and logs after successful run

- **WHEN** `shud` execution succeeds and output verification passes
- **THEN** all files in the local `runs/{run_id}/output/` directory MUST be uploaded to `s3://nhms-runs/runs/{run_id}/output/`
- **THEN** all files in the local `runs/{run_id}/logs/` directory MUST be uploaded to `s3://nhms-runs/runs/{run_id}/logs/`
- **THEN** the `hydro.hydro_run.output_uri` MUST be set to the S3 URI of the output directory
- **THEN** the `hydro.hydro_run.log_uri` MUST be set to the S3 URI of the logs directory

#### Scenario: Upload retries on transient failure

- **WHEN** an upload to object storage fails with a transient error (e.g., connection timeout)
- **THEN** the adapter MUST retry the upload up to 3 times with exponential backoff
- **THEN** if all retries fail, the `hydro.hydro_run.status` MUST be set to `failed` with `error_code` and `error_message` recorded

#### Scenario: Upload logs even on failed run

- **WHEN** `shud` execution fails (non-zero exit code)
- **THEN** the adapter MUST still upload log files (`shud_stdout.log`, `shud_stderr.log`) to `runs/{run_id}/logs/`
- **THEN** this ensures post-mortem debugging is possible from the central store

### Requirement: Run record management

The adapter MUST create and maintain a `hydro.hydro_run` record. Required columns: `run_id` (PK), `run_type` (hydro.run_type ENUM, value `'forecast'`), `scenario_id`, `model_id` (FK), `basin_version_id` (FK), `forcing_version_id` (FK), `init_state_id` (NULL for M1 cold-start), `source_id` (FK), `cycle_time`, `start_time`, `end_time`, `status` (hydro.run_status ENUM), `slurm_job_id`, `run_manifest_uri`, `output_uri`, `log_uri`, `error_code`, `error_message`, `created_at`, `updated_at`. Status transitions follow the hydro.run_status ENUM: `created` → `staged` → `submitted` → `running` → `succeeded` (or `failed` at any stage). Each status transition MUST update `updated_at`. The `run_id` MUST follow the ID convention.

#### Scenario: Run record lifecycle for a successful forecast

- **WHEN** the adapter starts processing a forecast run
- **THEN** a `hydro.hydro_run` record MUST be inserted with `status = 'created'`, `created_at = now()`, `updated_at = now()`
- **THEN** the record MUST include `run_type = 'forecast'`, `model_id`, `basin_version_id`, `forcing_version_id`, `source_id`, `cycle_time`, `start_time`, `end_time`, and `run_manifest_uri`
- **THEN** `init_state_id` MUST be explicitly set to `NULL` for M1 cold-start runs
- **THEN** when workspace is prepared, status MUST transition to `staged` and `updated_at` MUST be set
- **THEN** when the Slurm job is submitted, status MUST transition to `submitted`, `slurm_job_id` MUST be recorded, and `updated_at` MUST be set
- **THEN** when `shud` begins execution, status MUST transition to `running` and `updated_at` MUST be set
- **THEN** when execution and upload complete, status MUST transition to `succeeded`, `output_uri` and `log_uri` MUST be set, and `updated_at` MUST be set

#### Scenario: Run record lifecycle for a failed forecast

- **WHEN** `shud` fails during execution
- **THEN** status MUST transition to `failed`
- **THEN** `updated_at` MUST be set to the current time
- **THEN** `error_code` MUST store a structured error code (e.g., the exit code) and `error_message` MUST contain a description of the failure (stderr excerpt)

#### Scenario: Run record is created before any work begins

- **WHEN** the adapter receives a manifest and begins workspace preparation
- **THEN** the `hydro.hydro_run` record MUST be inserted with `status = 'created'` BEFORE any file downloads or execution begins
- **THEN** this ensures that even if workspace preparation crashes, the run attempt is recorded in the database

#### Scenario: Run manifest structure

- **WHEN** the adapter writes the run manifest to `run_manifest_uri`
- **THEN** the manifest MUST be a nested JSON structure containing: `model.model_id`, `model.model_package_uri`, `forcing.forcing_uri`, `outputs.output_uri`, `source_id`, `cycle_time`, and `initial_state.ic_file_uri` (NULL for M1)

