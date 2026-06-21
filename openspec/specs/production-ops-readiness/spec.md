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

### Requirement: Display API restart is reproducible from a single command

The node-27 display API uvicorn restart path SHALL be a single, repo-committed shell script (`scripts/ops/start-display-api.sh`) that:

1. Sources `infra/env/display.env` via `set -a; . ...; set +a` so every key is exported to the relaunched process environment.
2. Asserts required env keys (`DATABASE_URL`, `NHMS_ENABLE_LIVE_POSTGIS_MVT`) are present before launch and aborts non-zero with an explicit missing-keys list if not (does not leak values).
3. Gracefully replaces the prior `apps.api.main:app` uvicorn process (SIGTERM with bounded wait, SIGKILL fallback) before relaunching detached via `setsid` so the new process survives SSH disconnect.
4. Runs a post-launch smoke check (`curl /api/v1/models?limit=1` → `jq .data.items[0].basin_id != null`) that surfaces env-drift contract regressions (the exact failure mode PR #596 fixed) before user-facing breakage.

The runbook `docs/runbooks/display-readonly-live-mvt.md` SHALL reference this script for the "restart service" step and SHALL NOT reference dangling host-only paths (e.g. `/tmp/start_display.sh`) that are not committed to the repo.

The systemd-unit alternative (`/etc/systemd/system/nhms-display-api.service`) MAY be added in a follow-up change when operator-account sudo on node-27 is available; that alternative is out of scope for this requirement.

#### Scenario: Restart from single command sources env file and asserts contract

- **WHEN** the operator runs `bash scripts/ops/start-display-api.sh` on node-27 from any working directory
- **THEN** the script resolves the repo root and sources `infra/env/display.env` with `set -a` export
- **AND** the script aborts non-zero before launch if `DATABASE_URL` or `NHMS_ENABLE_LIVE_POSTGIS_MVT` is missing
- **AND** the script gracefully terminates any prior `apps.api.main:app` uvicorn before relaunching detached via `setsid`
- **AND** the script asserts `curl /api/v1/models?limit=1 | jq .data.items[0].basin_id` is non-null and exits non-zero on null/missing/parse error
- **AND** the relaunched uvicorn process environment contains `DATABASE_URL`

#### Scenario: Restart smoke check catches env drift before user-facing breakage

- **WHEN** the operator launches via `bash scripts/ops/start-display-api.sh` and the env-sourced `DATABASE_URL` resolves to a database where `core.basin_version` JOIN cannot populate `basin_id` for any active model
- **THEN** the smoke check `jq .data.items[0].basin_id != null` returns false and the script exits non-zero
- **AND** the operator is alerted in the same restart command output, not after frontend popup breakage in production

#### Scenario: Restart smoke check tolerates empty model registry

- **WHEN** the operator launches via `bash scripts/ops/start-display-api.sh` and `/api/v1/models?limit=1` returns `data.items` with length 0 (no active models registered yet — typical on a fresh DB)
- **THEN** the script logs a warning ("/api/v1/models returned 0 items; basin_id smoke check skipped (DB may be empty)") and exits 0
- **AND** the operator-visible message instructs separate model-registration verification before declaring restart healthy

