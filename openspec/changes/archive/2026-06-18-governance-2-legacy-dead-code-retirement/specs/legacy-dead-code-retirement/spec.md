---
status: archived
current_authority: "docs/governance/LEGACY_DEAD_CODE_INVENTORY.md; openspec/specs/legacy-dead-code-retirement/spec.md; docs/governance/DOC_STATUS.md"
superseded_by: "openspec/specs/legacy-dead-code-retirement/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for the archived Governance-2 OpenSpec delta"
---

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

#### Scenario: legacy placeholder paths are retired

- **WHEN** legacy placeholder paths are removed or archived after inventory proof
- **THEN** current source-of-truth docs no longer present those paths as active entrypoints
- **AND** active counterparts for frontend, workers, tile publication, and Slurm templates remain present and documented

#### Scenario: diagnostic path is evaluated

- **WHEN** a path has `DIAGNOSTIC-ONLY`
- **THEN** it is not deleted as dead code unless production replacement evidence and runbook migration are both present

### Requirement: QHH diagnostic scripts remain isolated from production

QHH diagnostic scripts SHALL remain available for diagnostic bring-up until explicitly retired, but production orchestrator code MUST NOT invoke them.

#### Scenario: diagnostic script inventory is created

- **WHEN** QHH diagnostic scripts are listed in a diagnostic README or manifest
- **THEN** the README states their diagnostic-only status, production replacement, and verification guard

#### Scenario: QHH diagnostic manifest is added

- **WHEN** the QHH diagnostic manifest is updated
- **THEN** it lists diagnostic entrypoints, direct helper dependencies, out-of-chain QHH helpers, production replacement commands, and static guard tests
- **AND** existing diagnostic script paths remain stable unless wrappers and runbook migration are added in the same change

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
- **THEN** it forbids broad API route mocks using the same live spec matcher as the profile test matcher
- **AND** it requires explicit base URL/API URL configuration without username/password userinfo
- **AND** a passing receipt requires browser-observed `/api/v1/runtime/config`
  from the configured API binding with `service_role` exactly `display_readonly`
- **AND** runtime config JSON evidence is parsed only inside an explicit body-size boundary
- **AND** a passing receipt requires at least one browser-observed monitoring
  read API response URL/status from that same configured API binding without
  parsing the read API response body

#### Scenario: live display-readonly page is denied or unavailable

- **WHEN** the live display-readonly browser page shows RBAC `权限不足` or runtime config unavailability
- **THEN** the live receipt is not recorded as `PASS`

#### Scenario: live display-readonly page touches control surfaces

- **WHEN** the live display-readonly browser page requests `/api/v1/slurm/*`
- **THEN** the live receipt is not recorded as `PASS`, regardless of HTTP method
- **AND** retry/cancel run endpoints remain forbidden for non-GET mutation requests

#### Scenario: mocked regression lane remains available

- **WHEN** frontend developers run the default/local mocked Playwright lane
- **THEN** specs with broad API mocks remain runnable only as mocked regression
- **AND** their lane/config/docs do not describe the result as live display proof
- **AND** broad-mock specs are not listed under a generic `chromium` project name

#### Scenario: live profile is missing required runtime URLs

- **WHEN** the live display-readonly profile starts without explicit frontend base URL or API base URL
- **THEN** the profile fails before browser execution with a clear configuration error

#### Scenario: live display-readonly runtime is unavailable

- **WHEN** node-27 or a live display API is not available during implementation
- **THEN** the live e2e config/script and static no-mock guard still land, while runtime execution is recorded as `BLOCKED` with required environment variables

### Requirement: paused CI jobs do not use hidden false conditions

CI jobs SHALL NOT remain indefinitely disabled by conditions such as `&& false`. Historical evidence gates MUST be archived or moved to an explicit manual workflow.

#### Scenario: paused visual job is found

- **WHEN** governance scans workflow files
- **THEN** a job disabled by `&& false` is reported until it is removed, archived, or converted to manual dispatch

#### Scenario: historical visual evidence is retained manually

- **WHEN** a historical visual evidence lane is retained
- **THEN** it is exposed through an explicit manual workflow
- **AND** it is not part of automatic PR/push CI
- **AND** it verifies the checked-out evidence SHA before running visual tests
- **AND** docs classify the result as historical mocked visual evidence, not live display-readonly proof

#### Scenario: hidden paused visual job is retired

- **WHEN** governance scans automatic PR/push workflow files after #366
- **THEN** no automatic CI job for M15 visual evidence is disabled behind `&& false`
- **AND** any retained M15 visual evidence command is reachable only from an explicit manual workflow
