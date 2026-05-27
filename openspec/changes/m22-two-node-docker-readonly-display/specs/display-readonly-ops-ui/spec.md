## ADDED Requirements

### Requirement: Ops display mode is read-only

The `/ops` frontend SHALL render as a read-only diagnostic surface when the service role is `display_readonly`.

#### Scenario: Runtime config drives display mode
- **WHEN** `/ops` initializes
- **THEN** it reads service role and capability flags from the backend runtime config contract
- **AND** it does not rely on a hardcoded production build flag as the source of truth.

#### Scenario: Display mode hides control buttons
- **WHEN** `/ops` renders in display readonly mode
- **THEN** real retry and cancel execution buttons are hidden or disabled for all user roles
- **AND** the page does not initiate `POST /api/v1/runs/{run_id}/retry`, `POST /api/v1/runs/{run_id}/cancel`, or `/api/v1/slurm/*` control requests.

#### Scenario: Compute mode can retain controls
- **WHEN** `/ops` or monitoring renders in `compute_control` or `dev_monolith` mode and the user is authorized
- **THEN** existing retry/cancel controls can remain available according to RBAC
- **AND** existing monitoring tests remain valid for non-display roles.

#### Scenario: Display queue depth degradation
- **WHEN** `/ops` renders in display readonly mode and queue depth is unavailable because Slurm gateway access is disabled
- **THEN** the queue widget is hidden or shows a read-only unavailable state
- **AND** the rest of `/ops` remains usable for stages, jobs, logs, and diagnostics.

### Requirement: Ops strict run identity

The `/ops` display SHALL bind jobs, stages, and logs to the same run identity used by latest-product in cross-plane E2E.

#### Scenario: Ops strict filters
- **WHEN** `/ops` has strict identity context with `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** pipeline status, stages, jobs, diagnostics, and log requests use or validate that identity
- **AND** jobs from another run with the same source and cycle are rejected or rendered as mismatched.

#### Scenario: Duplicate source cycle runs
- **WHEN** two runs share the same `source` and `cycle_time`
- **THEN** `/ops` cross-plane evidence passes only for jobs and logs matching the selected `run_id` and `model_id`
- **AND** mixed-run evidence marks cross-plane E2E as fail or blocked.

### Requirement: Ops diagnostic copy

The display readonly `/ops` page SHALL provide a diagnostic payload that an operator can copy for manual 22-node recovery.

#### Scenario: Failed job diagnostic
- **WHEN** a selected job or stage is failed, submission failed, partially failed, or permanently failed
- **THEN** the page exposes a copyable diagnostic payload containing available `source_id`, `cycle_time`, `run_id`, `model_id`, `stage`, `job_id`, `slurm_job_id`, `status`, `error_code`, `error_message`, and `log_uri`
- **AND** absent fields are explicitly omitted or marked unavailable rather than fabricated.

#### Scenario: Manual recovery guidance
- **WHEN** the diagnostic payload is shown
- **THEN** the page includes guidance to follow the 22 compute-control recovery runbook
- **AND** any suggested commands are clearly tied to 22-node execution, not 27.

#### Scenario: Local notified state
- **WHEN** an operator marks a failure as notified in display readonly mode
- **THEN** the UI may show local acknowledged/notified state
- **AND** it does not write pipeline, hydro, met, or audit DB state in MVP.

### Requirement: Display ops consumes published logs

The display readonly `/ops` page SHALL view logs through the backend job logs endpoint backed by published artifacts.

#### Scenario: Log modal success
- **WHEN** a job has a readable published log
- **THEN** the log modal displays bounded log content
- **AND** it does not expose instructions to open 22 private filesystem paths from the 27 browser.

#### Scenario: Log modal unavailable
- **WHEN** a job log is missing, unsupported, or access denied
- **THEN** the log modal displays the stable backend error reason
- **AND** the diagnostic payload still includes safe job identity for manual investigation.
