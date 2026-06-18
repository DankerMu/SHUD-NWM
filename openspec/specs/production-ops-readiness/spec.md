# production-ops-readiness Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.
## Requirements
### Requirement: Production configuration is validated before release

The system SHALL provide production environment templates and validation checks for all deployable services.

#### Scenario: Production config validates required services

- **WHEN** production readiness validation runs
- **THEN** API, orchestrator, Slurm gateway, tile publisher, frontend, database, object store, source adapters, and workspace roots are checked for required settings
- **AND** missing or unsafe settings fail with stable error codes and no secret disclosure

### Requirement: Operator actions are backend gated and audited

The system SHALL enforce or explicitly gate backend-side authorization for production-impacting actions.

#### Scenario: Production action requires authorized role

- **WHEN** a user attempts model activation, rerun, cancel, QC override, source config change, or tile republish
- **THEN** backend authorization verifies the required role or the action is blocked by a documented release gate
- **AND** successful actions write audit evidence with actor, role, target, previous/new state, and redacted lineage

#### Scenario: Unauthorized production action is denied

- **WHEN** a user without the required role attempts a production-impacting action
- **THEN** the backend returns a stable unauthorized or forbidden response
- **AND** the action does not mutate model state, pipeline jobs, QC override state, source config, or tile publication state
- **AND** the audit or security log records the denied attempt without secret values

#### Scenario: Deferred auth is a release blocker

- **WHEN** full backend auth cannot be completed in this change
- **THEN** the issue must emit a release-blocker artifact listing deferred actions, current fallback, required roles, residual risk, and the condition required to remove the gate

### Requirement: Monitoring and alerts cover closure risks

The system SHALL expose metrics and alert rules for production data, compute, object store, API, and publication failures.

#### Scenario: Production closure alerts are testable

- **WHEN** validation injects or observes source latency, Slurm queue backlog, basin failure, object-store write failure, stale analysis state, tile publication error, or API p95 breach
- **THEN** the corresponding metric and alert rule identify severity, target, current value, threshold, and recommended operator action

### Requirement: Runbooks and rollback drills are present

The system SHALL document and verify rollback procedures for common production closure failures.

#### Scenario: Rollback drill records outcome

- **WHEN** a rollback drill is run for bad model activation, failed publish/import, failed source cycle, failed Slurm array, or bad tile release
- **THEN** the runbook records commands, preconditions, expected evidence, recovery result, and residual risk

