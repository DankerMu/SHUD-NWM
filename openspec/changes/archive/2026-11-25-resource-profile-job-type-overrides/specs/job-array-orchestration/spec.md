## MODIFIED Requirements

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
