## ADDED Requirements

### Requirement: The live monitor raises a fixed set of alerts with a runbook each

`nhms-monitor` (`services/orchestrator/monitoring.py`) SHALL derive its alerts from three summaries
(failed cycles, the Slurm queue, the scheduler's latest evidence) and SHALL raise only the alerts
below. Every alert carries `alert`, `severity`, `observed` and `runbook`; `threshold` is present
exactly on the alerts that compare against a configured limit. The `runbook` path SHALL name a file
that exists in the repository.

| `alert` | Raised when | `severity` | `threshold` | `runbook` |
|---|---|---|---|---|
| `failed_cycles` | failed cycles exceed `NHMS_MONITORING_MAX_FAILED_CYCLES` | critical | yes | `docs/runbooks/failed-basin-retry.md` |
| `slurm_monitor_unavailable` | the Slurm queue could not be read | warning | no | `docs/runbooks/slurm-backlog.md` |
| `slurm_queue_backlog` | active Slurm jobs exceed `NHMS_MONITORING_MAX_ACTIVE_SLURM_JOBS` | warning | yes | `docs/runbooks/slurm-backlog.md` |
| `scheduler_evidence_missing` | the scheduler evidence summary is not `ok` | critical | no | `docs/runbooks/source-latency.md` |
| `scheduler_stale` | the latest scheduler evidence is older than `NHMS_MONITORING_MAX_STALE_MINUTES` | warning | yes | `docs/runbooks/source-latency.md` |

`slurm_monitor_unavailable` and `slurm_queue_backlog` are mutually exclusive, and so are
`scheduler_evidence_missing` and `scheduler_stale`.

#### Scenario: A quiet system raises nothing

- **WHEN** no limit is exceeded, the Slurm queue is readable and the scheduler evidence is fresh
- **THEN** the alert list is empty

#### Scenario: Each condition raises its alert

- **WHEN** one of the five conditions of the table holds
- **THEN** the alert list contains that alert with the severity, the `threshold` presence and the runbook of its row

#### Scenario: An unreadable queue is not reported as a backlog

- **WHEN** the Slurm queue could not be read
- **THEN** `slurm_monitor_unavailable` is raised and `slurm_queue_backlog` is not, whatever the last known job count

#### Scenario: Every linked runbook exists

- **WHEN** the runbook paths of the five alerts are resolved against the repository
- **THEN** each one is an existing file

## MODIFIED Requirements

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

- **WHEN** live identity-provider auth is configured but no accepted live proof of backend auth exists, and a user attempts a production-impacting action
- **THEN** the backend answers that request with HTTP 503 and code `RELEASE_BLOCKED`
- **AND** the response details carry the redacted policy decision, the audit record and the `removal_criteria` that lift the block
- **AND** the action does not mutate anything
