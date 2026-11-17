## MODIFIED Requirements

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
  - `actions` — containing "View Logs" button and, for failed jobs, a "Retry" button

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

- **WHEN** a user with `operator+` role clicks the "Retry" button on a failed job row
- **THEN** the frontend SHALL call `POST /api/v1/runs/{run_id}/retry` and display an error toast carrying the API error message, because the endpoint answers `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED` and submits nothing
- **THEN** because the endpoint never answers with success, the page only ever reaches its error path for this action; the success toast it still contains is unreachable until #3179 removes the button

#### Scenario: View Logs action from table

- **WHEN** a user clicks the "View Logs" button on any job row
- **THEN** the frontend SHALL open a modal or side panel displaying the log content from `GET /api/v1/jobs/{job_id}/logs`
- **THEN** long log content SHALL be displayed in a monospaced scrollable container with line numbers
