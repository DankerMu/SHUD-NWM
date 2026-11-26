## MODIFIED Requirements

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
