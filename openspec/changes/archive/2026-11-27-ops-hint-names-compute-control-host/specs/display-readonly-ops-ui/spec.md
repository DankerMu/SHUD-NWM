## MODIFIED Requirements

### Requirement: Ops diagnostic copy

The display readonly `/ops` page SHALL provide a diagnostic payload that an operator can copy for manual recovery on the compute control host.

#### Scenario: Failed job diagnostic
- **WHEN** a selected job or stage is failed, submission failed, partially failed, or permanently failed
- **THEN** the page exposes a copyable diagnostic payload containing available `source_id`, `cycle_time`, `run_id`, `model_id`, `stage`, `job_id`, `slurm_job_id`, `status`, `error_code`, `error_message`, and `log_uri`
- **AND** absent fields are explicitly omitted or marked unavailable rather than fabricated.

#### Scenario: Manual recovery guidance
- **WHEN** the diagnostic payload is shown
- **THEN** the page includes guidance to follow the recovery runbook on the compute control host
- **AND** any suggested commands are clearly tied to execution on the compute control host, not on node-27.

#### Scenario: Local notified state
- **WHEN** an operator marks a failure as notified in display readonly mode
- **THEN** the UI may show local acknowledged/notified state
- **AND** it does not write pipeline, hydro, met, or audit DB state in MVP.

### Requirement: Display ops consumes published logs

The display readonly `/ops` page SHALL view logs through the backend job logs endpoint backed by published artifacts.

#### Scenario: Log modal success
- **WHEN** a job has a readable published log
- **THEN** the log modal displays bounded log content
- **AND** it does not expose instructions to open private filesystem paths of the compute hosts from a browser viewing the node-27 page.

#### Scenario: Log modal unavailable
- **WHEN** a job log is missing, unsupported, or access denied
- **THEN** the log modal displays the stable backend error reason
- **AND** the diagnostic payload still includes safe job identity for manual investigation.
