## MODIFIED Requirements

### Requirement: Monitoring page E2E test
The system SHALL have a Playwright E2E test covering: page load, stages rendering, failure expansion, jobs filtering, log modal, the absence of a retry action, permission denial for viewer role.

#### Scenario: E2E monitoring happy path
- **WHEN** the Playwright test navigates to `/monitoring` with mocked API responses
- **THEN** the summary bar, stage cards, jobs table, and trend charts MUST all render with the expected data
