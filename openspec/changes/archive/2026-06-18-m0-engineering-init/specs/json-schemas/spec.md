# Capability Spec: json-schemas

## Context

The system uses JSON manifests and status documents to decouple the HPC compute plane from the business control plane. Four JSON Schema files are required in the `schemas/` directory to validate these documents at CI time and runtime. The schemas must conform to JSON Schema draft-07 or later. The run manifest schema is based on the contract defined in `docs/appendices/B_run_manifest_schema.md`. The QC result and pipeline job schemas must align with the `ops.qc_result` and `ops.pipeline_job` table structures in `docs/appendices/C_database_schema_draft.md`.

---

## ADDED Requirements

### Requirement: `run_manifest.schema.json` covers the manifest contract

The run manifest schema must validate the HPC job input contract as defined in Appendix B, ensuring all required fields are present and correctly typed.

#### Scenario: Schema file exists and is valid JSON Schema

WHEN examining `schemas/run_manifest.schema.json`
THEN the file is valid JSON
AND `$schema` references `http://json-schema.org/draft-07/schema#` or a later draft
AND `type` is `object`
AND `title` or `description` identifies it as the NHMS run manifest schema.

#### Scenario: Required top-level fields are enforced

WHEN validating a JSON document against `run_manifest.schema.json`
THEN the following top-level fields are required: `schema_version`, `run_id`, `run_type`, `scenario_id`, `start_time`, `end_time`, `model`, `forcing`, `outputs`
AND omitting any required field causes validation failure.

#### Scenario: Required nested fields use structured objects

WHEN validating a JSON document against `run_manifest.schema.json`
THEN the `model` object MUST require `model.model_id` and `model.model_package_uri`
AND the `forcing` object MUST require `forcing.forcing_uri`
AND the `outputs` object MUST require `outputs.output_uri`
AND the `runtime` object, when present, MUST require `runtime.executable`
AND these nested required fields MUST cause validation failure when absent from their parent objects.

#### Scenario: Nested model object is required with mandatory fields

WHEN validating a JSON document against `run_manifest.schema.json`
THEN a `model` object is required
AND within `model`, `model_id` and `model_package_uri` are required
AND `basin_version_id` is defined (required or within model)
AND `model_package_uri` must be a string matching URI format.

#### Scenario: Nested forcing object is required with mandatory fields

WHEN validating a JSON document against `run_manifest.schema.json`
THEN a `forcing` object is required
AND within `forcing`, `forcing_uri` is required
AND `forcing_uri` must be a string matching URI format.

#### Scenario: Nested outputs object is required with mandatory fields

WHEN validating a JSON document against `run_manifest.schema.json`
THEN an `outputs` object is required
AND within `outputs`, `output_uri` is required.

#### Scenario: Forecast runs require additional fields

WHEN validating a JSON document against `run_manifest.schema.json` with `run_type` equal to `"forecast"`
THEN the document MUST additionally require `source_id` (string), `cycle_time` (string, date-time format), and `initial_state.ic_file_uri` (string, URI format)
AND omitting any of these fields when `run_type` is `"forecast"` MUST cause validation failure
AND these fields are optional when `run_type` is `"analysis"` or `"hindcast"`.

#### Scenario: `run_type` is constrained to valid enum values

WHEN validating a JSON document against `run_manifest.schema.json`
THEN `run_type` must be one of `["analysis", "forecast", "hindcast"]`
AND any other value causes validation failure.

#### Scenario: Time fields enforce ISO 8601 format

WHEN validating a JSON document against `run_manifest.schema.json`
THEN `start_time`, `end_time`, `cycle_time`, and `issue_time` must conform to `date-time` format
AND a value like `"2026-04-30T00:00:00Z"` passes
AND a value like `"2026/04/30"` fails.

#### Scenario: Valid example manifest passes validation

WHEN validating the example manifest from `docs/appendices/B_run_manifest_schema.md` section 2 against `run_manifest.schema.json`
THEN validation passes with zero errors.

#### Scenario: Runtime object has correct field types

WHEN validating a JSON document with a `runtime` object against `run_manifest.schema.json`
THEN `runtime.executable` must be a string
AND `runtime.threads` must be a positive integer
AND `runtime.init_mode` must be an integer.

---

### Requirement: `run_status.schema.json` covers HPC job status reporting

The run status schema must validate status update messages sent from HPC jobs back to the control plane.

#### Scenario: Schema file exists and is valid JSON Schema

WHEN examining `schemas/run_status.schema.json`
THEN the file is valid JSON
AND `$schema` references draft-07 or later
AND `type` is `object`.

#### Scenario: Required fields for status reporting are enforced

WHEN validating a JSON document against `run_status.schema.json`
THEN the following fields are required: `run_id`, `status`, `timestamp`
AND omitting any required field causes validation failure.

#### Scenario: Status field is constrained to valid enum values

WHEN validating a JSON document against `run_status.schema.json`
THEN `status` must be one of `["created", "staged", "submitted", "running", "succeeded", "parsed", "frequency_done", "published", "failed", "cancelled", "superseded"]`
AND these values match the `hydro.run_status` enum defined in the database schema.

#### Scenario: Optional error fields are typed correctly

WHEN validating a JSON document with error details against `run_status.schema.json`
THEN `error_code` is an optional string
AND `error_message` is an optional string
AND `exit_code` is an optional integer
AND these fields are only required when `status` is `"failed"`.

#### Scenario: Progress reporting fields are supported

WHEN validating a JSON document against `run_status.schema.json`
THEN optional fields `progress_pct` (number, 0-100), `current_step` (string), and `log_uri` (string, URI format) are accepted
AND these fields do not cause validation failure when absent.

#### Scenario: Valid status update passes validation

WHEN validating a document like `{"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "status": "running", "timestamp": "2026-05-01T02:30:00Z", "progress_pct": 45.2}`
THEN validation passes with zero errors.

---

### Requirement: `qc_result.schema.json` matches ops.qc_result table structure

The QC result schema must validate QC check result documents that are stored in or reported to the `ops.qc_result` table.

#### Scenario: Schema file exists and is valid JSON Schema

WHEN examining `schemas/qc_result.schema.json`
THEN the file is valid JSON
AND `$schema` references draft-07 or later
AND `type` is `object`.

#### Scenario: Required fields match qc_result table columns

WHEN validating a JSON document against `qc_result.schema.json`
THEN the following fields are required: `qc_checkpoint`, `target_type`, `target_id`, `passed`, `severity`, `checks_json`
AND `passed` must be a boolean
AND `severity` must be one of `["info", "warning", "error"]`.

#### Scenario: Optional fields match qc_result table columns

WHEN validating a JSON document against `qc_result.schema.json`
THEN the following fields are optional: `run_id`, `cycle_id`, `message`, `qc_id`
AND each has the correct type (string for IDs, string for message, integer for qc_id).

#### Scenario: checks_json structure is validated

WHEN validating a JSON document against `qc_result.schema.json`
THEN `checks_json` must be an object
AND it must contain a `checks` array
AND each element in `checks` must have `name` (string) and `passed` (boolean)
AND each element may optionally have `detail` (string)
AND `checks_json` may contain a `summary` string.

#### Scenario: Valid QC result from design doc passes validation

WHEN validating the `checks_json` example from `docs/spec/07_devops_ops_security.md` section 6.4.3 against `qc_result.schema.json`
THEN validation passes with zero errors.

#### Scenario: QC result with all checks failed is valid

WHEN validating a QC result where `passed = false` and all items in `checks_json.checks` have `passed = false`
THEN validation passes (the schema validates structure, not business logic).

---

### Requirement: `pipeline_job.schema.json` matches ops.pipeline_job table structure

The pipeline job schema must validate job tracking documents that correspond to the `ops.pipeline_job` table.

#### Scenario: Schema file exists and is valid JSON Schema

WHEN examining `schemas/pipeline_job.schema.json`
THEN the file is valid JSON
AND `$schema` references draft-07 or later
AND `type` is `object`.

#### Scenario: Required fields match pipeline_job table columns

WHEN validating a JSON document against `pipeline_job.schema.json`
THEN the following fields are required: `job_id`, `job_type`, `status`
AND `job_id` must be a string
AND `job_type` must be a string
AND `status` must be a string.

#### Scenario: Optional fields match pipeline_job table columns

WHEN validating a JSON document against `pipeline_job.schema.json`
THEN the following fields are optional and correctly typed:
- `run_id` (string)
- `cycle_id` (string)
- `slurm_job_id` (string)
- `stage` (string)
- `submitted_at` (string, date-time format)
- `started_at` (string, date-time format)
- `finished_at` (string, date-time format)
- `exit_code` (integer)
- `retry_count` (integer, default 0)
- `error_code` (string)
- `error_message` (string)
- `log_uri` (string).

#### Scenario: Job status values are a fixed enum

WHEN validating a JSON document against `pipeline_job.schema.json`
THEN `status` MUST be defined as an enum with exactly these values: `"pending"`, `"submitted"`, `"running"`, `"succeeded"`, `"failed"`, `"cancelled"`
AND any value not in this enum MUST cause validation failure.

#### Scenario: Timestamps use ISO 8601 format

WHEN validating a JSON document with timing fields against `pipeline_job.schema.json`
THEN `submitted_at`, `started_at`, and `finished_at` must conform to `date-time` format
AND a value like `"2026-05-01T02:30:00Z"` passes.

#### Scenario: Valid pipeline job document passes validation

WHEN validating a document like:
```json
{
  "job_id": "job_download_gfs_2026050100",
  "run_id": "fcst_gfs_2026050100_yangtze_shud_v12",
  "job_type": "download",
  "status": "succeeded",
  "stage": "raw_download",
  "submitted_at": "2026-05-01T00:05:00Z",
  "started_at": "2026-05-01T00:05:02Z",
  "finished_at": "2026-05-01T00:45:00Z",
  "exit_code": 0,
  "retry_count": 0
}
```
THEN validation passes with zero errors.

---

### Requirement: Example files exist for CI validation

Each JSON Schema must have at least one corresponding example file that CI can validate against the schema.

#### Scenario: Example files are in a discoverable location

WHEN examining the repository
THEN example files exist in a directory like `schemas/examples/` or `tests/fixtures/schemas/`
AND there is at least one example file per schema:
- `run_manifest.example.json`
- `run_status.example.json`
- `qc_result.example.json`
- `pipeline_job.example.json`

#### Scenario: All example files pass schema validation

WHEN running the CI json-schema-validate job
THEN each example file is validated against its corresponding schema
AND all validations pass with zero errors.

#### Scenario: Example files contain realistic data

WHEN examining example files
THEN `run_manifest.example.json` contains the Yangtze demo manifest from Appendix B (or equivalent)
AND `run_status.example.json` contains a realistic status update
AND `qc_result.example.json` contains a realistic QC result with multiple checks
AND `pipeline_job.example.json` contains a realistic job tracking record.

---

### Requirement: Schema files are valid and well-structured

All four schema files must themselves be valid JSON Schema documents and follow consistent conventions.

#### Scenario: All schemas pass meta-validation

WHEN validating each `*.schema.json` file against the JSON Schema meta-schema
THEN all four files are valid JSON Schema documents
AND no syntax errors or structural issues are reported.

#### Scenario: Schemas use consistent conventions

WHEN examining all four schema files
THEN all use the same `$schema` draft version (draft-07 or later)
AND all define `title` and `description` at the root level
AND property descriptions are provided for required fields
AND `additionalProperties` behavior is explicitly defined (either true or false).

#### Scenario: Schema files are in the correct directory

WHEN listing files in `schemas/`
THEN the following files exist:
- `schemas/run_manifest.schema.json`
- `schemas/run_status.schema.json`
- `schemas/qc_result.schema.json`
- `schemas/pipeline_job.schema.json`
AND no other `*.schema.json` files exist at M0 stage.
