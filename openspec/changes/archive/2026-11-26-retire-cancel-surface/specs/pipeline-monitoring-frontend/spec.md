## MODIFIED Requirements

### Requirement: Role-Based Access Control

The pipeline monitoring page SHALL be accessible only to users with authorized roles.

#### Scenario: Authorized user accesses monitoring page

- **WHEN** a user with `operator`, `model_admin`, or `sys_admin` role navigates to the monitoring page
- **THEN** the page SHALL render the full monitoring dashboard including its interactive controls (filters, sorting, the log modal); no retry control and no cancel control is rendered for any role (#3179, #3394)

#### Scenario: Unauthorized user attempts access

- **WHEN** a user without `operator`, `model_admin`, or `sys_admin` role navigates to the monitoring page
- **THEN** the page SHALL display an access denied message and MUST NOT render any monitoring data

#### Scenario: Retry and cancel actions require operator+ role

- **WHEN** a user with `viewer` or `contributor` role somehow reaches the monitoring page
- **THEN** no cancel action and no retry action is rendered, for this or any other role (scenario title kept for archive continuity)

### Requirement: Center Job List Table

The monitoring page SHALL display a job list table in the center area with columns, filtering, and sorting capabilities.

#### Scenario: Job table columns

- **WHEN** the job list table is rendered
- **THEN** the table SHALL display the following columns:
  - `run_id` — truncated UUID with copy-on-click
  - `model_id` — full model identifier
  - `run_type` — e.g., `forecast`, `analysis`
  - `scenario` — e.g., `GFS`, `IFS`, `best_available`
  - `status` — displayed as a colored badge (`succeeded` green, `failed` red, `running` blue, `submitted` gray, `cancelled` dark gray)
  - `slurm_job_id` — Slurm-assigned job identifier
  - `submitted_at` — formatted as `YYYY-MM-DD HH:mm:ss`
  - `duration` — formatted as `Xm Ys` or `--` if not finished
  - `actions` — containing the "View Logs" button and the diagnostics actions; there is no "Retry" button and no cancel control

#### Scenario: Filter by status

- **WHEN** a user selects a status filter (e.g., `failed`)
- **THEN** the table SHALL show only jobs matching the selected status
- **THEN** the filter SHALL be applied as a query parameter to `GET /api/v1/jobs`

#### Scenario: Filter by run_type and scenario

- **WHEN** a user selects `run_type` or `scenario` filter values
- **THEN** the table SHALL show only jobs matching the selected filters

#### Scenario: Sort by submitted_at

- **WHEN** a user clicks the `submitted_at` column header
- **THEN** the table SHALL toggle sort order between ascending and descending, defaulting to descending (newest first)

#### Scenario: Sort by duration

- **WHEN** a user clicks the `duration` column header
- **THEN** the table SHALL toggle sort order between ascending and descending

#### Scenario: Retry action from table

- **WHEN** an authorized operator views a `failed`, `submission_failed`, `partially_failed` or `permanently_failed` job row
- **THEN** the table SHALL NOT render a "Retry" button and SHALL NOT issue `POST /api/v1/runs/{run_id}/retry`
- **AND** failed-run recovery is the node-22 manual-repair marker lane (`scripts/node22_manual_retry_failed_runs.py`, runbook `node22-control-plane-manual-recovery`), not a page action

#### Scenario: View Logs action from table

- **WHEN** a user clicks the "View Logs" button on any job row
- **THEN** the frontend SHALL open a modal or side panel displaying the log content from `GET /api/v1/jobs/{job_id}/logs`
- **THEN** long log content SHALL be displayed in a monospaced scrollable container with line numbers
