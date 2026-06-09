## ADDED Requirements

Issue #362 implements only the persistent inventory scenarios below. Cleanup,
diagnostic relocation, live e2e separation, and paused CI retirement scenarios
are follow-up work for #363-#366 unless explicitly marked as inventory evidence.

### Requirement: Legacy and diagnostic paths have explicit status

Every governed historical path SHALL be classified as `production`, `diagnostic`, `test-only`, or `archived` before deletion or relocation.

The classification SHALL be recorded in a persistent inventory with exact path, status, owner area, active build/import/deploy evidence, docs/runbook migration, and final action.

#### Scenario: placeholder path is evaluated
- **WHEN** a path such as `apps/web` or a hyphenated worker directory is considered for removal
- **THEN** the cleanup PR includes evidence that it is not part of active build, tests, imports, or deployment

#### Scenario: legacy path inventory is reviewed
- **WHEN** the legacy/dead-code governance issue is implemented
- **THEN** the persistent inventory includes `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, `services/tile-publisher`, QHH diagnostic scripts, mocked e2e specs, and paused CI jobs

#### Scenario: workers/sbatch_templates is evaluated
- **WHEN** `workers/sbatch_templates` is considered for archive or deletion
- **THEN** the inventory records that it contains legacy single-run templates, documents the canonical `infra/sbatch` replacement, and verifies no active Slurm gateway or production scheduler path depends on it

#### Scenario: diagnostic path is evaluated
- **WHEN** a path has `DIAGNOSTIC-ONLY`
- **THEN** it is not deleted as dead code unless production replacement evidence and runbook migration are both present

### Requirement: QHH diagnostic scripts remain isolated from production

QHH diagnostic scripts SHALL remain available for diagnostic bring-up until explicitly retired, but production orchestrator code MUST NOT invoke them.

#### Scenario: diagnostic script inventory is created
- **WHEN** QHH diagnostic scripts are listed in a diagnostic README or manifest
- **THEN** the README states their diagnostic-only status, production replacement, and verification guard

#### Scenario: production scheduler is scanned
- **WHEN** static tests scan scheduler/orchestrator code
- **THEN** QHH diagnostic script tokens are absent

### Requirement: mocked e2e and live e2e are separate evidence classes

Frontend e2e tests that mock API responses SHALL be named and documented as mocked regression. Live display-readonly e2e SHALL use real API endpoints and MUST NOT register broad API mocks.

#### Scenario: mocked Playwright spec uses route mocks
- **WHEN** a spec calls `page.route('**/api/v1/**')`
- **THEN** the spec is classified as mocked regression and is not cited as live receipt

#### Scenario: live display-readonly e2e is added
- **WHEN** a live e2e profile runs against node-27 or a live display API
- **THEN** it forbids broad API route mocks and requires explicit base URL/API URL configuration

#### Scenario: live display-readonly runtime is unavailable
- **WHEN** node-27 or a live display API is not available during implementation
- **THEN** the live e2e config/script and static no-mock guard still land, while runtime execution is recorded as `BLOCKED` with required environment variables

### Requirement: paused CI jobs do not use hidden false conditions

CI jobs SHALL NOT remain indefinitely disabled by conditions such as `&& false`. Historical evidence gates MUST be archived or moved to an explicit manual workflow.

#### Scenario: paused visual job is found
- **WHEN** governance scans workflow files
- **THEN** a job disabled by `&& false` is reported until it is removed, archived, or converted to manual dispatch
