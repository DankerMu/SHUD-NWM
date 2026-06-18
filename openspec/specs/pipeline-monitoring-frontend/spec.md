# pipeline-monitoring-frontend Specification

## Purpose
TBD - created by archiving change m3-slurm-nationalization. Update Purpose after archive.
## Requirements
### Requirement: Role-Based Access Control

The pipeline monitoring page SHALL be accessible only to users with authorized roles.

#### Scenario: Authorized user accesses monitoring page

- **WHEN** a user with `operator`, `model_admin`, or `sys_admin` role navigates to the monitoring page
- **THEN** the page SHALL render the full monitoring dashboard including all interactive controls (retry, cancel)

#### Scenario: Unauthorized user attempts access

- **WHEN** a user without `operator`, `model_admin`, or `sys_admin` role navigates to the monitoring page
- **THEN** the page SHALL display an access denied message and MUST NOT render any monitoring data

#### Scenario: Retry and cancel actions require operator+ role

- **WHEN** a user with `viewer` or `contributor` role somehow reaches the monitoring page
- **THEN** the retry and cancel action buttons MUST NOT be rendered

---

### Requirement: Top Summary Bar

The monitoring page SHALL display a top summary bar showing current cycle status and aggregate job counts.

#### Scenario: Summary bar renders current cycle info

- **WHEN** the monitoring page loads with data for a selected (source, cycle_time)
- **THEN** the top summary bar SHALL display:
  - Current cycle identifier (source + cycle_time formatted as human-readable string)
  - Total job count
  - Success count — displayed in green
  - Failed count — displayed in red
  - Running count — displayed in blue
  - Waiting/pending count — displayed in gray

#### Scenario: Slurm queue depth donut chart

- **WHEN** the monitoring page loads
- **THEN** the top summary bar SHALL include a donut chart (rendered via ECharts) showing Slurm queue depth with three segments:
  - `running` — colored blue
  - `pending` — colored amber/yellow
  - `idle` — colored gray
- **THEN** the donut center SHALL display the total active job count (running + pending)

#### Scenario: Queue depth data unavailable

- **WHEN** the `GET /api/v1/queue/depth` endpoint returns HTTP 503
- **THEN** the donut chart SHALL display a "Slurm Unavailable" placeholder with a gray-out state

---

### Requirement: Left Pipeline View

The monitoring page SHALL display a vertical pipeline view on the left side showing 7 stage cards connected by directional arrows.

#### Scenario: Stage cards layout

- **WHEN** the monitoring page renders the pipeline view
- **THEN** 7 stage cards SHALL be displayed in vertical order, connected by downward arrows:
  1. download
  2. canonical
  3. forcing
  4. shud_forecast
  5. parse
  6. frequency
  7. publish

#### Scenario: Stage card content

- **WHEN** each stage card is rendered
- **THEN** the card SHALL display:
  - Stage name as the card header
  - Status icon reflecting the stage's `status`:
    - `succeeded` — green checkmark icon
    - `failed` — red cross icon
    - `running` — blue spinning/pulsing dot icon
    - `pending` — gray hollow circle icon
    - `partially_failed` — amber warning icon
    - `skipped` — gray dash icon
  - Duration — formatted as `Xm Ys` (e.g., `3m 42s`), or `--` if not started
  - Basin completion rate — displayed as `completed/total = percentage` (e.g., `85/128 = 66%`), only shown for per-basin stages

#### Scenario: Stage duration bar chart

- **WHEN** the pipeline view is rendered below the stage cards
- **THEN** a horizontal bar chart SHALL display the duration of each stage as a stacked or grouped bar, enabling visual comparison of stage durations within the current cycle

#### Scenario: Click failed stage card to expand failed basin list

- **WHEN** a user clicks on a stage card with `status` = `failed` or `partially_failed`
- **THEN** an expandable panel SHALL open below the card showing a per-basin breakdown table sourced from the stage's `basin_results` array, with each row showing:
  - `model_id` — basin model identifier
  - `status` — per-basin status (`succeeded`, `failed`, `running`, `submitted`) displayed as a colored badge
  - `error_code` — error code if failed (empty otherwise)
  - `error_message` — truncated to 120 characters with tooltip for full message
  - A "View Logs" link that navigates to the job log for the corresponding basin job

#### Scenario: Click succeeded or pending stage card

- **WHEN** a user clicks on a stage card with `status` = `succeeded`, `pending`, or `skipped`
- **THEN** the card SHALL NOT expand (no additional detail panel)

---

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
- **THEN** the frontend SHALL call `POST /api/v1/runs/{run_id}/retry` and display a confirmation toast on success or an error toast on failure
- **THEN** the table SHALL refresh to show the newly submitted retry job

#### Scenario: View Logs action from table

- **WHEN** a user clicks the "View Logs" button on any job row
- **THEN** the frontend SHALL open a modal or side panel displaying the log content from `GET /api/v1/jobs/{job_id}/logs`
- **THEN** long log content SHALL be displayed in a monospaced scrollable container with line numbers

---

### Requirement: Right Trends Panel

The monitoring page SHALL display a trends panel on the right side with performance and success rate charts.

#### Scenario: Performance trend chart

- **WHEN** the trends panel is rendered
- **THEN** a 7-day line chart SHALL be displayed (rendered via ECharts) showing average stage duration per stage over time
- **THEN** each of the 7 stages SHALL be represented as a separate line series with distinct colors
- **THEN** the X-axis SHALL show dates and the Y-axis SHALL show duration in minutes
- **THEN** data SHALL be fetched from `GET /api/v1/metrics/stage-duration?source=<current_source>&days=7`

#### Scenario: Success rate trend chart

- **WHEN** the trends panel is rendered below the performance chart
- **THEN** a 7-day line chart SHALL be displayed showing per-cycle success rate over time
- **THEN** the X-axis SHALL show cycle times and the Y-axis SHALL show success rate as a percentage (0-100%)
- **THEN** data SHALL be fetched from `GET /api/v1/metrics/success-rate?source=<current_source>&days=7`

---

### Requirement: Auto-Refresh

The monitoring page SHALL automatically refresh data at a regular interval.

#### Scenario: Periodic polling

- **WHEN** the monitoring page is active and visible
- **THEN** the frontend SHALL poll `GET /api/v1/pipeline/stages` and `GET /api/v1/jobs` every 10 seconds
- **THEN** the UI SHALL update in place without full page reload, preserving scroll position and filter selections

#### Scenario: Polling paused when tab is hidden

- **WHEN** the browser tab is not visible (user switched to another tab)
- **THEN** polling SHOULD be paused to reduce unnecessary network traffic
- **THEN** polling SHALL resume immediately when the tab becomes visible again

#### Scenario: Polling error handling

- **WHEN** a polling request fails (network error or HTTP 5xx)
- **THEN** the UI SHALL display a non-intrusive warning indicator (e.g., amber dot in the header) but MUST NOT disrupt the currently displayed data
- **THEN** the next poll SHALL proceed on schedule

---

### Requirement: Responsive Layout

The monitoring page SHALL adapt to different screen sizes.

#### Scenario: Wide screen (>=1440px)

- **WHEN** the viewport width is 1440px or greater
- **THEN** the page SHALL display the three-column layout: left pipeline view, center job table, right trends panel

#### Scenario: Medium screen (1024px-1439px)

- **WHEN** the viewport width is between 1024px and 1439px
- **THEN** the trends panel SHALL collapse into a toggleable drawer or accordion below the job table
- **THEN** the pipeline view and job table SHALL share the available width

#### Scenario: Narrow screen (<1024px)

- **WHEN** the viewport width is less than 1024px
- **THEN** the stage cards SHALL collapse into a compact horizontal scrollable strip or accordion
- **THEN** the job table SHALL take full width with horizontal scroll for overflow columns
- **THEN** the trends panel SHALL be accessible via a dedicated tab or expandable section

