## ADDED Requirements

### Requirement: Production job type mappings are complete

Every Forecast M3, Analysis, and Hindcast production Slurm stage SHALL map to a real-gateway job type with a canonical `infra/sbatch` template.

#### Scenario: Production mappings are checked

- **WHEN** mapping tests inspect orchestrator stages and Slurm settings
- **THEN** every production stage job type MUST exist in the default job type mapping
- **AND** every mapped template MUST exist under `infra/sbatch`
- **AND** the mapping file and default settings MUST agree for production job types

### Requirement: Analysis uses the real Slurm production path

Analysis orchestration SHALL submit real-gateway-compatible job types and manifests instead of relying on ignored rendered `script` payloads.

#### Scenario: Analysis mapping is explicit

- **WHEN** Analysis production mapping tests run
- **THEN** the mapped job types MUST be documented in tests
- **AND** the default/preferred mapping SHOULD be `analysis_download_source_cycle`, `analysis_convert_canonical`, `analysis_produce_forcing`, `run_shud_analysis`, `parse_analysis_output`, and `save_state_snapshot`
- **AND** any alternative mapping MUST still use canonical `infra/sbatch` templates and avoid ambiguous legacy Forecast job type names

#### Scenario: Analysis stages submit production manifests

- **WHEN** an Analysis pipeline is submitted through the real gateway path
- **THEN** each stage MUST carry a mapped production `job_type`
- **AND** the manifest MUST include run, model, source, time-window, object-store, and stage fields required by the template
- **AND** successful, failed, and timed-out stages MUST map to stable persisted statuses/error codes
- **AND** real Slurm execution MUST NOT require a rendered `script` manifest field

### Requirement: Hindcast runtime requires real forcing

Hindcast SHUD runtime SHALL NOT start from metadata-only forcing placeholders.

#### Scenario: Metadata-only forcing is unavailable

- **WHEN** the forcing producer cannot create a real forcing package
- **THEN** Hindcast runtime submission MUST fail before SHUD execution
- **AND** the failure MUST have a stable error code such as `HINDCAST_FORCING_PACKAGE_UNAVAILABLE`
- **AND** tests MUST assert where the error is surfaced or persisted

#### Scenario: Hindcast array manifests are renderable

- **WHEN** Hindcast jobs are submitted to the real gateway
- **THEN** the array manifest/template MUST include model, source, year, run id, forcing package, object store, workspace, and resource context
- **AND** fake Slurm tests MUST cover submission and status parsing for the Hindcast job type

### Requirement: Legacy template use is explicit

Legacy `workers/sbatch_templates` SHALL NOT be a silent production dependency.

#### Scenario: Production template directory is canonical

- **WHEN** production defaults or mapping tests run
- **THEN** the template directory MUST resolve to `infra/sbatch`
- **AND** any remaining `workers/sbatch_templates` use MUST be documented as legacy/test-only or removed
- **AND** rendered `script` payload support MUST be documented as non-production or replaced with an explicit safe mode

### Requirement: Fake Slurm coverage exercises production gateway commands

The real gateway test suite SHALL cover the fake command matrix needed to validate production Slurm behavior without a live cluster.

#### Scenario: Fake Slurm command matrix is covered

- **WHEN** the issue #124 verification suite runs
- **THEN** fake or monkeypatched `sbatch`, `sacct`, `scancel`, and `sinfo` behavior MUST be covered
- **AND** tests MUST cover submit, status, array task status, log fetch, and cancel behavior for production job types
