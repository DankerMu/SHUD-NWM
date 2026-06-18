# job-array-orchestration Specification

## Purpose
TBD - created by archiving change m3-slurm-nationalization. Update Purpose after archive.
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

### Requirement: Manifest index file

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

---

### Requirement: Resource profile configuration

Resource profiles SHALL be defined in a YAML configuration file with a `default` profile and optional per-model overrides. Each profile specifies Slurm resource parameters: `partition`, `nodes`, `ntasks` (default 1), `cpus_per_task`, `memory_gb`, `walltime`, `max_concurrent`, and `shud_threads` (default = `cpus_per_task`).

#### Scenario: Default resource profile is applied

- **WHEN** a job is submitted for a model_id with no per-model override
- **THEN** the system MUST use the `default` resource profile values: `partition`, `nodes`, `ntasks`, `cpus_per_task`, `memory_gb`, `walltime`, `max_concurrent`, `shud_threads`
- **THEN** the rendered sbatch template MUST include `#SBATCH --partition={partition}`, `#SBATCH --nodes={nodes}`, `#SBATCH --ntasks={ntasks}`, `#SBATCH --cpus-per-task={cpus_per_task}`, `#SBATCH --mem={memory_gb}G`, `#SBATCH --time={walltime}`

#### Scenario: Per-model override takes precedence

- **WHEN** a job is submitted for `model_id="yangtze_shud_v12"` and the resource profile config contains:
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

---

### Requirement: Resource profile resolution

The system SHALL resolve resource profiles by merging model-specific overrides on top of the default profile, with the override taking strict precedence.

#### Scenario: Full resolution chain

- **WHEN** the default profile specifies `{partition: compute, nodes: 1, ntasks: 1, cpus_per_task: 32, memory_gb: 128, walltime: "06:00:00", max_concurrent: 4, shud_threads: 32}`
- **THEN** for a model with override `{cpus_per_task: 64, shud_threads: 64}`, the resolved profile MUST be `{partition: compute, nodes: 1, ntasks: 1, cpus_per_task: 64, memory_gb: 128, walltime: "06:00:00", max_concurrent: 4, shud_threads: 64}`
- **THEN** resolution MUST be a shallow merge (override keys replace default keys, no deep merge)

#### Scenario: Override-only model with no default fallback needed

- **WHEN** a model override specifies all 8 resource fields
- **THEN** none of the default values SHALL be used for that model
- **THEN** the system MUST still validate all fields are present after resolution

---

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

