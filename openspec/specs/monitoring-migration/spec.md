# monitoring-migration Specification

## Purpose
TBD - created by archiving change m35-frontend-modernization. Update Purpose after archive.
## Requirements
### Requirement: Monitoring page layout
The system SHALL render the monitoring page with a responsive layout: three columns (stages | jobs | trends) above 1200px, two columns (stages+jobs | trends below) between 800-1200px, single column below 800px.

#### Scenario: Wide screen layout
- **WHEN** the viewport width is greater than 1200px
- **THEN** three columns MUST be rendered side-by-side: stages panel, jobs panel, trends panel

#### Scenario: Narrow screen layout
- **WHEN** the viewport width is less than 800px
- **THEN** all panels MUST stack vertically in a single column

### Requirement: Summary bar with cycle info and badges
The system SHALL display a summary bar containing: current cycle info (source, cycle_time, current_state), job count badges (succeeded/failed/running/pending), and an ECharts queue depth donut chart.

#### Scenario: Badge counts match API data
- **WHEN** the pipeline has 5 succeeded, 2 failed, 1 running, and 3 pending jobs
- **THEN** the summary bar MUST display badges with counts 5/2/1/3 in green/red/blue/gray respectively

### Requirement: Seven-stage pipeline cards
The system SHALL render 7 stage cards vertically with directional connectors, each showing: status icon (✓ succeeded / ✗ failed / ◉ running / ○ pending), stage name, duration, completion rate bar.

#### Scenario: Stage status icon mapping
- **WHEN** a stage has display_status "succeeded"
- **THEN** the stage card MUST show a green checkmark icon (✓)

#### Scenario: Stage with partial failure
- **WHEN** a stage has display_status "partially_failed"
- **THEN** the stage card MUST show a warning icon and the completion rate MUST reflect the partial success ratio

### Requirement: Per-basin failure expansion
The system SHALL allow clicking on a failed/partially_failed stage card to expand and show per-basin failure details: model_id, error_code, error_message.

#### Scenario: Expand failed stage
- **WHEN** a user clicks on a stage card with display_status "failed" or "partially_failed"
- **THEN** a panel MUST expand below the card showing a list of failed basins with their error details

### Requirement: Jobs table with filters and pagination
The system SHALL render a jobs table with columns (run_id, model_id, run_type, scenario, status, slurm_job_id, submitted_at, duration, actions), filterable by status/run_type/scenario, sortable by submitted_at/duration, with server-driven pagination.

#### Scenario: Filter by status
- **WHEN** the user selects "failed" in the status filter
- **THEN** the table MUST show only jobs with status "failed" and the total count MUST reflect the filtered count

#### Scenario: Server-side pagination
- **WHEN** there are 150 total matching jobs and page size is 12
- **THEN** the pagination MUST show correct page numbers derived from the server's `total` field, and changing pages MUST fetch new data from the API

### Requirement: Log modal
The system SHALL display a modal dialog when the user clicks "查看日志" on a job row, loading log content from `/api/v1/jobs/{job_id}/logs`.

#### Scenario: Log fetch error handling
- **WHEN** the log fetch returns 404 or fails
- **THEN** the modal MUST display an error message instead of staying in "loading" state

### Requirement: Retry and cancel actions
The system SHALL render retry buttons for failed jobs and cancel buttons for active jobs, using the actual user role in the X-User-Role header.

#### Scenario: Retry with actual role
- **WHEN** the user with role "operator" clicks retry on a failed job
- **THEN** the request MUST include header `X-User-Role: operator` (not a hard-coded value)

#### Scenario: Viewer cannot retry
- **WHEN** the user role is "viewer"
- **THEN** retry and cancel buttons MUST NOT be rendered

### Requirement: Trend panel with charts
The system SHALL display two ECharts line charts: 7-day average stage duration (one line per stage) and cycle success rate over time.

#### Scenario: Trend data loading
- **WHEN** the monitoring page loads
- **THEN** both trend charts MUST fetch data from `/api/v1/metrics/stage-duration?days=7` and `/api/v1/metrics/success-rate?days=7`

### Requirement: Auto-polling with visibility control
The system SHALL poll pipeline status, stages, and jobs every 10 seconds, pausing when the browser tab is hidden and resuming when visible.

#### Scenario: Tab hidden pauses polling
- **WHEN** the user switches to another browser tab
- **THEN** polling MUST stop and no API requests MUST be made until the tab becomes visible again

#### Scenario: Manual refresh
- **WHEN** the user clicks the refresh button
- **THEN** all monitoring data MUST be re-fetched immediately regardless of the polling timer

