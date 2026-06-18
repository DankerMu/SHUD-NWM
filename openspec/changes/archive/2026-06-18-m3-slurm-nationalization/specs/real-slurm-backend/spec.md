# Capability Spec: real-slurm-backend

## Context

M1/M2 使用 MockSlurmGateway 验证了单流域 forecast/analysis 闭环。M3 需要实现 RealSlurmGateway，通过 subprocess 调用 sbatch/sacct/scancel CLI 完成真实 Slurm 集群上的作业管理。RealSlurmGateway 实现 SlurmGateway ABC 的全部方法：submit_job, cancel_job, get_job_status, list_jobs, fetch_logs, reset, health。

安全约束：sbatch 模板白名单（只允许 `infra/sbatch/*.sbatch`），manifest schema 校验，禁止任意 shell 注入。

---

## ADDED Requirements

### Requirement: RealSlurmGateway implements SlurmGateway ABC

RealSlurmGateway SHALL implement all methods defined in the SlurmGateway abstract base class. The factory function `create_gateway()` SHALL instantiate RealSlurmGateway when `slurm_gateway.backend` is set to `"slurm"`.

#### Scenario: Factory selects RealSlurmGateway for slurm backend

- **WHEN** the configuration key `slurm_gateway.backend` is set to `"slurm"`
- **THEN** `create_gateway()` MUST return a RealSlurmGateway instance
- **THEN** the instance MUST be a subclass of SlurmGateway ABC
- **THEN** the previously-raised `NotImplementedError` for backend `"slurm"` MUST no longer be raised

#### Scenario: RealSlurmGateway implements all ABC methods

- **WHEN** RealSlurmGateway is instantiated
- **THEN** it MUST implement: `submit_job`, `cancel_job`, `get_job_status`, `list_jobs`, `fetch_logs`, `reset`, `health`
- **THEN** no `NotImplementedError` SHALL be raised for any ABC method

#### Scenario: MockSlurmGateway remains available

- **WHEN** the configuration key `slurm_gateway.backend` is set to `"mock"`
- **THEN** `create_gateway()` MUST still return MockSlurmGateway
- **THEN** existing mock-based tests MUST continue to pass without modification

---

### Requirement: submit_job resolves template by job_type and submits via CLI

The `submit_job` method SHALL receive `(job_type, manifest)`, look up the sbatch template by `job_type` via a configuration mapping (e.g., `job_type_templates` in config), render the template with manifest + resource profile parameters, and invoke `sbatch` via subprocess. The run manifest remains a pure business document per upstream §6 — it does NOT contain `template_name`.

#### Scenario: Successful job submission

- **WHEN** `submit_job` is called with `job_type` (e.g., `run_shud_forecast_array`) and a valid manifest containing `run_id`, `model_id`, and resource parameters
- **THEN** the system MUST look up the template file from `job_type_templates` config mapping (e.g., `run_shud_forecast_array` → `run_shud_forecast.sbatch`)
- **THEN** the system MUST render the resolved sbatch template using Jinja2 with manifest values and resource profile
- **THEN** the system MUST invoke `sbatch <rendered_script_path>` via subprocess
- **THEN** the system MUST parse the job_id from stdout matching the pattern `"Submitted batch job (\d+)"`
- **THEN** the returned `SlurmJobStatus` MUST be `SUBMITTED` with the parsed integer job_id

#### Scenario: sbatch stdout parsing

- **WHEN** `sbatch` returns stdout `"Submitted batch job 12345\n"` with exit code 0
- **THEN** the system MUST extract job_id `12345` as an integer
- **THEN** the system MUST NOT treat trailing whitespace or newlines as parse errors

#### Scenario: sbatch returns unexpected stdout format

- **WHEN** `sbatch` returns exit code 0 but stdout does not match `"Submitted batch job (\d+)"`
- **THEN** the system MUST raise a `SlurmParseError` with the full stdout content
- **THEN** the error MUST be logged at ERROR level with the `job_type` and manifest `run_id`

---

### Requirement: get_job_status queries sacct

The `get_job_status` method SHALL call `sacct` with parsable output format and parse the result into a structured status object.

#### Scenario: Successful status query for a single job

- **WHEN** `get_job_status` is called with a valid `job_id`
- **THEN** the system MUST invoke `sacct --parsable2 --noheader --format=JobID,State,ExitCode,Start,End --jobs={job_id}`
- **THEN** the system MUST parse the pipe-delimited output into fields: JobID, State, ExitCode, Start, End
- **THEN** the Slurm State MUST be mapped to `SlurmJobStatus` enum: `PENDING`/`REQUEUED` → `SUBMITTED`, `RUNNING` → `RUNNING`, `COMPLETED` → `SUCCEEDED`, `FAILED`/`TIMEOUT`/`NODE_FAIL`/`OUT_OF_MEMORY` → `FAILED`, `CANCELLED` → `CANCELLED`

#### Scenario: sacct returns multiple lines for array jobs

- **WHEN** `sacct` returns multiple lines (e.g., batch step, extern step, sub-jobs)
- **THEN** the system MUST select the line where JobID exactly matches the queried job_id (no `.batch` or `.extern` suffix)
- **THEN** sub-job lines (e.g., `12345.batch`, `12345.extern`) MUST be ignored for top-level status

#### Scenario: sacct returns no output for job_id

- **WHEN** `sacct` returns empty output for the queried job_id
- **THEN** the system MUST raise a `SlurmJobNotFoundError` with the job_id

---

### Requirement: cancel_job invokes scancel

The `cancel_job` method SHALL invoke `scancel` to cancel a running or pending job.

#### Scenario: Successful job cancellation

- **WHEN** `cancel_job` is called with a valid `job_id`
- **THEN** the system MUST invoke `scancel {job_id}` via subprocess
- **THEN** if the subprocess exits with code 0, the method MUST return successfully
- **THEN** a subsequent `get_job_status` call MUST return `SlurmJobStatus.CANCELLED`

#### Scenario: scancel for non-existent job

- **WHEN** `scancel` is invoked for a job_id that does not exist in Slurm
- **THEN** the system MUST raise a `SlurmJobNotFoundError`
- **THEN** the error MUST include the scancel stderr output

---

### Requirement: list_jobs queries sacct with filters

The `list_jobs` method SHALL call `sacct` with optional filters to list jobs.

#### Scenario: List jobs with time range filter

- **WHEN** `list_jobs` is called with `start_time` and `end_time` parameters
- **THEN** the system MUST invoke `sacct --parsable2 --noheader --format=JobID,JobName,State,ExitCode,Start,End --starttime={start_time} --endtime={end_time}`
- **THEN** the result MUST be a list of parsed job status objects

#### Scenario: List jobs with job name filter

- **WHEN** `list_jobs` is called with a `job_name_prefix` parameter
- **THEN** the system MUST invoke `sacct` with `--name={job_name_prefix}*`
- **THEN** only jobs matching the prefix MUST be returned

---

### Requirement: fetch_logs reads log files from workspace

The `fetch_logs` method SHALL read job output and error logs from the configured workspace log directory.

#### Scenario: Fetch stdout log for completed job

- **WHEN** `fetch_logs` is called with a valid `job_id` and `run_id`
- **THEN** the system MUST read the file at `workspace/{run_id}/logs/{job_id}.out`
- **THEN** the returned content MUST be the raw text content of the log file
- **THEN** a field `complete` MUST be `true` if the job is in a terminal state

#### Scenario: Log file does not exist

- **WHEN** the expected log file path does not exist on the filesystem
- **THEN** the system MUST return an empty string with a warning message
- **THEN** the system MUST NOT raise an exception

#### Scenario: Fetch logs for running job returns partial content

- **WHEN** `fetch_logs` is called for a job in `RUNNING` status
- **THEN** the system MUST read the current content of the log file (partial output)
- **THEN** a field `complete` MUST be `false`

---

### Requirement: health checks sinfo availability

The `health` method SHALL verify that the Slurm CLI tools are accessible on the system.

#### Scenario: Slurm CLI is available

- **WHEN** `health` is called and `sinfo --version` exits with code 0
- **THEN** the method MUST return a health response with `status="healthy"`, `backend="slurm"`, and `version` containing the sinfo version string

#### Scenario: Slurm CLI is not available

- **WHEN** `health` is called and `sinfo --version` fails (command not found or non-zero exit)
- **THEN** the method MUST return a health response with `status="unhealthy"` and `error` describing the failure
- **THEN** the system MUST NOT raise an exception

---

### Requirement: Template whitelist enforcement

RealSlurmGateway SHALL only allow sbatch templates from a configured whitelist directory. Template selection is based on `job_type` mapped to a template file via the `job_type_templates` configuration mapping. Any resolved path outside the whitelist directory MUST be rejected.

#### Scenario: job_type maps to a template within whitelist directory

- **WHEN** `submit_job` is called with `job_type="run_shud_forecast_array"`
- **THEN** the system MUST look up the template filename from `job_type_templates` config (e.g., `run_shud_forecast_array` → `run_shud_forecast.sbatch`)
- **THEN** the system MUST resolve the template path to `{configured_template_dir}/run_shud_forecast.sbatch`
- **THEN** the template MUST be loaded and rendered successfully

#### Scenario: Template path traversal in config is rejected

- **WHEN** `job_type_templates` config maps a `job_type` to a path containing traversal (e.g., `../../etc/malicious.sbatch`)
- **THEN** the system MUST reject the request with a `TemplateSecurityError`
- **THEN** the resolved path MUST be validated to be within `{configured_template_dir}/`
- **THEN** no subprocess MUST be invoked

#### Scenario: Template not found for job_type

- **WHEN** `submit_job` is called with a `job_type` that has no entry in `job_type_templates` config or whose mapped file does not exist
- **THEN** the system MUST raise a `TemplateNotFoundError` with the `job_type`
- **THEN** the error MUST NOT reveal the full filesystem path in external-facing responses

---

### Requirement: Command injection prevention

All parameters rendered into sbatch templates MUST be validated against the manifest schema before rendering. No user-supplied string SHALL be passed directly to subprocess without validation.

#### Scenario: Manifest fields are schema-validated before rendering

- **WHEN** `submit_job` receives a manifest with fields `run_id`, `model_id`, `basin_version_id`
- **THEN** each field MUST be validated against the JSON schema (alphanumeric, underscores, hyphens only for identifiers)
- **THEN** fields containing shell metacharacters (`;`, `|`, `&`, `$`, `` ` ``, `\n`) MUST be rejected with a `ManifestValidationError`

#### Scenario: Jinja2 rendering uses sandboxed environment

- **WHEN** a sbatch template is rendered with manifest parameters
- **THEN** the rendering MUST use Jinja2 `SandboxedEnvironment`
- **THEN** the template MUST NOT be able to access Python builtins, file I/O, or os module

#### Scenario: Subprocess arguments are passed as list, not shell string

- **WHEN** `sbatch` is invoked via subprocess
- **THEN** the command MUST be passed as a list (e.g., `["sbatch", script_path]`), not as a shell string
- **THEN** `shell=True` MUST NOT be used in any subprocess call

---

### Requirement: Subprocess error handling

All subprocess calls (sbatch, sacct, scancel, sinfo) SHALL handle timeouts, non-zero exit codes, and parse failures gracefully.

#### Scenario: Subprocess times out

- **WHEN** a subprocess call exceeds the configured timeout (default 30 seconds)
- **THEN** the system MUST raise a `SlurmTimeoutError` with the command name and timeout value
- **THEN** the system MUST kill the subprocess before raising the error

#### Scenario: Subprocess returns non-zero exit code

- **WHEN** `sbatch` returns a non-zero exit code
- **THEN** the system MUST raise a `SlurmCommandError` with the exit code, stdout, and stderr
- **THEN** the error MUST be logged at ERROR level

#### Scenario: Parse failure on subprocess output

- **WHEN** sacct output cannot be parsed (unexpected column count or format)
- **THEN** the system MUST raise a `SlurmParseError` with the raw output
- **THEN** the system MUST NOT silently return incorrect status data
