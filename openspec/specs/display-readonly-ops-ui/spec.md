# display-readonly-ops-ui Specification

## Purpose
Define how `/ops` behaves on the read-only display node (`display_readonly`): it renders as a diagnostic surface with
no control requests, binds runs by strict identity, hands off to the latest published product on the single map, offers
role-neutral diagnostic copy, and reads job logs only from published log artifacts.

## Requirements

### Requirement: Ops display mode is read-only

The `/ops` frontend SHALL render as a read-only diagnostic surface when the service role is `display_readonly`.

#### Scenario: Runtime config drives display mode
- **WHEN** `/ops` initializes
- **THEN** it reads service role and capability flags from the backend runtime config contract
- **AND** it does not rely on a hardcoded production build flag as the source of truth.

#### Scenario: Display mode hides control buttons
- **WHEN** `/ops` renders in display readonly mode
- **THEN** no retry or cancel execution button is rendered for any user role
- **AND** the page does not initiate `POST /api/v1/runs/{run_id}/retry` or `/api/v1/slurm/*` control requests, nor a request to the retired cancel path.

#### Scenario: Compute mode can retain controls
- **WHEN** `/ops` or monitoring renders in `compute_control` or `dev_monolith` mode and the user is authorized
- **THEN** no retry control and no cancel control exists, as in every mode (#3179, #3394; scenario title kept for archive continuity)
- **AND** existing monitoring tests remain valid for non-display roles.

#### Scenario: Display queue depth degradation
- **WHEN** `/ops` renders in display readonly mode and queue depth is unavailable because Slurm gateway access is disabled
- **THEN** the queue widget is hidden or shows a read-only unavailable state
- **AND** the rest of `/ops` remains usable for stages, jobs, logs, and diagnostics.

### Requirement: Ops strict run identity

The `/ops` display SHALL bind jobs, stages, and logs to the same run identity used by latest-product in cross-plane E2E, and the four backend Ops routes SHALL carry that resolved identity in one coherent top-level envelope object on success, as a sibling of `data`, not inside a stage/job row.

#### Scenario: Backend strict ops identity contract

- **WHEN** an API consumer requests pipeline status, stages, jobs, or job logs with strict identity context
- **THEN** the backend returns a top-level `identity` object containing the resolved `source`, `cycle_time`, `run_id`, and `model_id` on success
- **AND** `job_logs` also includes the exact resolved `job_id`
- **AND** the existing `data` container shape is unchanged
- **AND** partial strict identity fails before source/cycle-only evidence lookup
- **AND** unresolved strict run identity returns `PIPELINE_STRICT_IDENTITY_NOT_FOUND` rather than source/cycle-only evidence

#### Scenario: Ops strict filters

- **WHEN** `/ops` has strict identity context with `source`, `cycle_time`, `run_id`, and `model_id`
- **THEN** pipeline status, stages, jobs, diagnostics, and log requests use or validate that identity
- **AND** jobs from another run with the same source and cycle are rejected or rendered as mismatched
- **AND** the returned `data` payload retains its existing shape while the response envelope carries the coherent identity

#### Scenario: Duplicate source cycle runs

- **WHEN** two runs share the same `source` and `cycle_time`
- **THEN** backend and `/ops` cross-plane evidence passes only for jobs and logs matching the selected `run_id` and `model_id`
- **AND** mixed-run evidence marks cross-plane E2E as fail or blocked

#### Scenario: Strict log identity mismatch

- **WHEN** a strict log request names `source`, `cycle_time`, `run_id`, and `model_id` that do not match the requested `job_id`
- **THEN** the backend returns `PIPELINE_STRICT_IDENTITY_MISMATCH`
- **AND** it does not read or return the wrong job's published log content

### Requirement: Single-map display strict latest-product handoff

The current `/` single-map display frontend, including the `/hydro-met` legacy redirect alias, SHALL support a strict cross-plane latest-product handoff without breaking source-only browsing.

#### Scenario: Strict display URL identity
- **WHEN** the current `/` display entrypoint or the `/hydro-met` legacy redirect alias is opened with strict `source`, `cycle_time`, `run_id`, and `model_id` query parameters
- **THEN** the latest-product request includes all four fields
- **AND** downstream station and river bootstrap only proceeds when the returned product matches the strict identity.

#### Scenario: Partial strict display identity
- **WHEN** any strict identity query parameter is present but one or more of `source`, `cycle_time`, `run_id`, or `model_id` is missing or malformed
- **THEN** the display entrypoint renders an invalid or blocked strict handoff state
- **AND** it does not issue a source-only latest-product fallback request for cross-plane evidence.

#### Scenario: Non-strict browsing compatibility
- **WHEN** the current `/` display entrypoint or the `/hydro-met` legacy redirect alias is opened without strict identity query parameters
- **THEN** the existing source-only latest-product browsing mode remains available.

#### Scenario: E2E handoff file boundary
- **WHEN** final E2E starts from `artifacts/two-node-e2e/<run_id>/cross-plane/identity.json`
- **THEN** the E2E harness converts the approved identity file into complete URL parameters
- **AND** the #235 frontend code does not directly read local artifact files.

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
