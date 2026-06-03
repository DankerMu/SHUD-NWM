## ADDED Requirements

### Requirement: Delivery docs name canonical paths
Implementation planning and README documentation SHALL name the canonical source paths used by package entry points and active tests.

#### Scenario: Frontend path is canonical
- **WHEN** a developer follows project docs for frontend work
- **THEN** the docs MUST point to `apps/frontend` and not to stale `apps/web` paths

#### Scenario: Worker paths are canonical
- **WHEN** a developer follows project docs for worker modules
- **THEN** the docs MUST point to underscore Python packages such as `workers/forcing_producer` unless a hyphen directory is explicitly labeled legacy or placeholder

#### Scenario: Storage root semantics are documented
- **WHEN** a developer follows storage or deployment docs
- **THEN** the docs MUST describe `WORKSPACE_ROOT` as temporary/HPC workspace and `OBJECT_STORE_ROOT` plus `OBJECT_STORE_PREFIX` as durable artifact storage

#### Scenario: Slurm template ownership is documented
- **WHEN** a developer follows orchestration docs
- **THEN** the docs MUST identify `infra/sbatch` as the canonical real Slurm template path or explicitly document any supported legacy template path

### Requirement: OpenSpec task states are evidence-backed
OpenSpec task checkboxes SHALL distinguish implemented, tested, accepted, and deferred work, and completed claims SHALL link to source or test evidence.

#### Scenario: Implemented M4 work has test evidence
- **WHEN** an OpenSpec task is marked complete for IFS or multi-source behavior
- **THEN** the task entry MUST reference the implementation file or test that proves completion

#### Scenario: Incomplete delivery remains unchecked
- **WHEN** a capability has implementation code but lacks contract tests or accepted behavior
- **THEN** the task MUST remain unchecked or be marked as implemented-but-not-accepted

### Requirement: JSON schemas mirror runtime enums and fields
Standalone JSON schemas SHALL include statuses and fields used by runtime persistence and API payloads.

#### Scenario: Pipeline job schema includes M3 statuses
- **WHEN** `ops.pipeline_job.status` or API payloads use `queued`, `submission_failed`, `partially_failed`, or `permanently_failed`
- **THEN** `schemas/pipeline_job.schema.json` MUST include those statuses

#### Scenario: Pipeline job schema includes array metadata
- **WHEN** pipeline jobs include `model_id` or `array_task_id`
- **THEN** the JSON schema MUST include those fields with appropriate types

### Requirement: Verification evidence is recorded for release decisions
The hardening stage SHALL record the exact verification commands and outcomes required for release acceptance.

#### Scenario: Release acceptance cites commands
- **WHEN** the hardening change is considered complete
- **THEN** documentation or issue checklists MUST include Python tests, ruff, frontend tests, frontend build, bundle check, and relevant E2E/contract tests with pass/fail outcomes
