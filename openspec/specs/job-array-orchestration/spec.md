# job-array-orchestration Specification

## Purpose

How per-basin work is submitted as Slurm job arrays: array submission and task-to-basin selection, resource profile configuration and resolution, input validation, sbatch template rendering, fail-closed array accounting, and the array stage manifest index.

## Requirements

### Requirement: Job array submission

The `submit_job_array` method SHALL generate and submit an sbatch command with `--array` parameter to schedule multiple basins as parallel tasks within a single Slurm job array.

#### Scenario: Submit array job for multiple basins

- **WHEN** `submit_job_array` is called with a manifest containing N basins (N ≥ 2) and `max_concurrent` = M
- **THEN** the system MUST invoke `sbatch --array=0-{N-1}%{M} <rendered_template>`
- **THEN** the system MUST parse the master job_id from stdout (e.g., `"Submitted batch job 12345"`)
- **THEN** individual array tasks SHALL be identified as `{master_job_id}_0`, `{master_job_id}_1`, ..., `{master_job_id}_{N-1}`

#### Scenario: Single basin falls back to non-array submission

- **WHEN** `submit_job_array` is called with a manifest containing exactly 1 basin
- **THEN** the system MUST submit a regular (non-array) sbatch job
- **THEN** the `--array` flag MUST NOT be included in the sbatch command

#### Scenario: max_concurrent limits parallel execution

- **WHEN** an array job is submitted with N=20 basins and max_concurrent=4
- **THEN** the sbatch command MUST include `--array=0-19%4`
- **THEN** Slurm SHALL run at most 4 array tasks simultaneously

---

### Requirement: Array task reads SLURM_ARRAY_TASK_ID to select basin

Each array task SHALL use the `SLURM_ARRAY_TASK_ID` environment variable to index into the manifest index file and retrieve its specific basin parameters.

#### Scenario: Array task selects correct basin from manifest index

- **WHEN** an array task starts with `SLURM_ARRAY_TASK_ID=3`
- **THEN** the task MUST read the manifest index file at the path specified by the `NHMS_MANIFEST_INDEX` environment variable
- **THEN** the task MUST select the entry at index 3 from the JSON array
- **THEN** the task MUST use that entry's `model_id`, `basin_version_id`, `run_id`, and other parameters for execution

#### Scenario: SLURM_ARRAY_TASK_ID exceeds manifest index length

- **WHEN** `SLURM_ARRAY_TASK_ID` is 10 but the manifest index contains only 8 entries
- **THEN** the task MUST exit with a non-zero exit code
- **THEN** the task MUST log an error: `"SLURM_ARRAY_TASK_ID {id} out of range for manifest with {len} entries"`

---

### Requirement: Resource profile configuration

Resource profiles SHALL be defined in a YAML configuration file with a `default` profile, optional
per-model overrides (`overrides`, keyed by model id) and optional per-job-type overrides
(`job_type_overrides`, keyed by the gateway job type). A per-job-type override MUST set only
`cpus_per_task`, `memory_gb` and `shud_threads`, with values that pass the profile's own value
validation, and MUST set `cpus_per_task` and `shud_threads` together or neither; a file whose
`job_type_overrides` is not a mapping, or that has an entry which is not a mapping or breaks one of
these rules, MUST be refused with a `ConfigurationError`. The set of model ids used to split cohorts MUST be derived from `overrides`
only. Each profile specifies Slurm resource parameters: `partition`, `nodes`, `ntasks` (default 1),
`cpus_per_task`, `memory_gb`, `walltime`, `max_concurrent`, and `shud_threads` (default =
`cpus_per_task`).

#### Scenario: Default resource profile is applied

- **WHEN** a job is submitted for a model_id with no per-model override, with a job type that has no entry in `job_type_overrides` (or without a job type)
- **THEN** the system MUST use the `default` resource profile values: `partition`, `nodes`, `ntasks`, `cpus_per_task`, `memory_gb`, `walltime`, `max_concurrent`, `shud_threads`
- **THEN** the rendered sbatch template MUST include `#SBATCH --partition={partition}`, `#SBATCH --nodes={nodes}`, `#SBATCH --ntasks={ntasks}`, `#SBATCH --cpus-per-task={cpus_per_task}`, `#SBATCH --mem={memory_gb}G`, `#SBATCH --time={walltime}`

#### Scenario: Per-model override takes precedence

- **WHEN** a job is submitted for `model_id="yangtze_shud_v12"`, with a job type that has no entry in `job_type_overrides`, and the resource profile config contains:
  ```yaml
  overrides:
    yangtze_shud_v12:
      cpus_per_task: 64
      memory_gb: 256
      walltime: "12:00:00"
  ```
- **THEN** the resolved profile MUST use `cpus_per_task=64`, `memory_gb=256`, `walltime="12:00:00"` from the override
- **THEN** fields not overridden (e.g., `partition`, `nodes`) MUST fall back to the `default` profile values

#### Scenario: Missing default profile is an error

- **WHEN** the resource profile YAML file does not contain a `default` section
- **THEN** the system MUST raise a `ConfigurationError` at startup
- **THEN** no jobs SHALL be accepted until the configuration is corrected

#### Scenario: A job-type override outside the closed key set is refused

- **WHEN** the resource profile YAML file contains `job_type_overrides: {produce_forcing_array: {cpus_per_task: 1, max_concurrent: 8}}`
- **THEN** loading the file MUST raise a `ConfigurationError` that names the job type and the key `max_concurrent`
- **THEN** no job SHALL be submitted from that file

#### Scenario: A job-type override that sets cores without threads is refused

- **WHEN** the resource profile YAML file contains `job_type_overrides: {produce_forcing_array: {cpus_per_task: 1}}`
- **THEN** loading the file MUST raise a `ConfigurationError` that names the job type

#### Scenario: Job-type overrides do not change the cohort split

- **WHEN** `job_type_overrides` is added to a resource profile file
- **THEN** the set of override model ids read by the scheduler MUST be the same as without it
- **THEN** the cohorts of a pass and their array concurrency budgets MUST be the same as without it

#### Scenario: The shipped forcing entry asks for one core and one thread

- **WHEN** a `produce_forcing_array` job is rendered from the repository's `config/resource_profiles.yaml` for any model, including a model with a per-model override
- **THEN** the rendered script MUST contain `#SBATCH --cpus-per-task=1`
- **THEN** the exported `SHUD_THREADS` and `OMP_NUM_THREADS` MUST be `1`
- **THEN** a `run_shud_forecast_array` job rendered from the same file MUST keep the model's resolved `cpus_per_task` and `shud_threads`

### Requirement: Resource profile resolution

The system SHALL resolve resource profiles by merging, in this order, the default profile, the
model-specific override, and the job-type override of the job being submitted; each later layer
takes strict precedence over the earlier ones. The job type MUST be applied at every place where the
gateway resolves a profile for a submission (array submit and single-job submit). A job type without
an entry, or a resolution without a job type, MUST resolve exactly as default plus model override.

#### Scenario: Full resolution chain

- **WHEN** the default profile specifies `{partition: compute, nodes: 1, ntasks: 1, cpus_per_task: 32, memory_gb: 128, walltime: "06:00:00", max_concurrent: 4, shud_threads: 32}`
- **THEN** for a model with override `{cpus_per_task: 64, shud_threads: 64}` and a job type that has no entry in `job_type_overrides`, the resolved profile MUST be `{partition: compute, nodes: 1, ntasks: 1, cpus_per_task: 64, memory_gb: 128, walltime: "06:00:00", max_concurrent: 4, shud_threads: 64}`
- **THEN** resolution MUST be a shallow merge (override keys replace default keys, no deep merge)

#### Scenario: Override-only model with no default fallback needed

- **WHEN** a model override specifies all 8 resource fields
- **THEN** none of the default values SHALL be used for that model
- **THEN** the system MUST still validate all fields are present after resolution

#### Scenario: A job-type override wins over the model override

- **WHEN** the default profile has `cpus_per_task: 4, shud_threads: 4`, the model override has `cpus_per_task: 12, shud_threads: 12`, and `job_type_overrides.produce_forcing_array` is `{cpus_per_task: 1, shud_threads: 1}`
- **THEN** a `produce_forcing_array` job for that model MUST resolve `cpus_per_task=1` and `shud_threads=1`
- **THEN** every field the job-type override does not set (`memory_gb`, `walltime`, `max_concurrent`, `partition`, `nodes`, `ntasks`) MUST keep the value resolved from default plus model override
- **THEN** a `run_shud_forecast_array` job for the same model MUST resolve `cpus_per_task=12` and `shud_threads=12`

#### Scenario: An unknown job type falls back

- **WHEN** a job is submitted with a job type that has no entry in `job_type_overrides`
- **THEN** the resolved profile MUST equal default plus model override

### Requirement: Array job input validation

The orchestrator SHALL validate array job parameters before submission to prevent invalid Slurm commands.

#### Scenario: Zero tasks are rejected

- **WHEN** `submit_job_array` is called with an empty basin list (N = 0)
- **THEN** the system MUST raise a `ValidationError` with message `"Cannot submit array job with 0 tasks"`
- **THEN** no sbatch command SHALL be invoked

#### Scenario: Zero max_concurrent is rejected

- **WHEN** the resolved resource profile has `max_concurrent = 0`
- **THEN** the system MUST raise a `ValidationError` with message `"max_concurrent must be ≥ 1"`
- **THEN** no sbatch command SHALL be invoked

#### Scenario: max_concurrent exceeds task count is clamped

- **WHEN** the array has N=5 tasks and `max_concurrent=20`
- **THEN** the effective `max_concurrent` MUST be clamped to N (5)
- **THEN** the sbatch command MUST include `--array=0-4%5`

---

### Requirement: sbatch template rendering with resource profile

The sbatch template MUST be rendered with both manifest parameters and resolved resource profile values.

#### Scenario: Template receives all resource profile values

- **WHEN** a sbatch template is rendered for an array stage
- **THEN** the Jinja2 context MUST include: `partition`, `nodes`, `ntasks`, `cpus_per_task`, `memory_gb`, `walltime`, `max_concurrent`, `shud_threads`, `run_id`, `cycle_id`, `stage_name`, `manifest_index_path`
- **THEN** the rendered script MUST contain valid `#SBATCH` directives reflecting the resolved resource profile

#### Scenario: Template sets NHMS_MANIFEST_INDEX environment variable

- **WHEN** a sbatch template for an array stage is rendered
- **THEN** the rendered script MUST export `NHMS_MANIFEST_INDEX={manifest_index_path}`
- **THEN** the worker script MUST use this variable to locate the manifest index file

### Requirement: Array accounting handles absent Slurm state fail-closed
The orchestrator array-accounting normalizer SHALL return only `succeeded`, `cancelled`, or `failed`. Empty or whitespace-only state SHALL map to `failed`, because `UNKNOWN` belongs to a different raw-state normalization contract and is not a valid task accounting status. Neither sacct rows nor gateway task payloads with absent state SHALL leak a bare `IndexError`.

#### Scenario: Empty sacct task state becomes a failed task
- **WHEN** a field-count-valid array sacct row such as `<master>_0||1:0` has an empty State field
- **THEN** parsing MUST produce a failed task record through the existing error-classification path
- **AND** no bare `IndexError` or other unclassified exception escapes

#### Scenario: Missing gateway task state becomes a failed task
- **WHEN** an array task payload has neither a usable `status` nor a `state`
- **THEN** coercion MUST produce a failed task record in the existing return domain
- **AND** no bare `IndexError` escapes

#### Scenario: Existing state vocabulary is preserved
- **WHEN** the normalizer receives `COMPLETED`, `CANCELLED`, or any other non-empty Slurm state, including suffix or annotation forms already supported
- **THEN** it MUST preserve the existing succeeded, cancelled, or failed mapping respectively

### Requirement: Array stage manifest index file

The orchestrator SHALL generate a manifest index file for each array stage. The file maps each array task_id to its basin-specific parameters.

#### Scenario: Manifest index file is generated before array submission

- **WHEN** the orchestrator prepares an array stage for N basins
- **THEN** it MUST write a JSON file at `workspace/{cycle_id}/manifests/{stage_name}_index.json`
- **THEN** the file MUST contain a JSON array of N objects, each with at minimum: `task_id`, `model_id`, `basin_version_id`, `run_id`, `workspace_dir`
- **THEN** the `task_id` field MUST equal the array index (0 to N-1)

#### Scenario: Manifest index includes all registered basins for the cycle

- **WHEN** 10 basins are registered for the forecast cycle
- **THEN** the manifest index MUST contain exactly 10 entries
- **THEN** each entry MUST reference a valid `model_id` and `basin_version_id` from the basin registry

#### Scenario: Manifest index is immutable after submission

- **WHEN** an array job has been submitted with a manifest index file
- **THEN** the orchestrator MUST NOT modify the manifest index file while the array job is running
- **THEN** any re-submission (e.g., retry) MUST generate a new manifest index file with a versioned filename
