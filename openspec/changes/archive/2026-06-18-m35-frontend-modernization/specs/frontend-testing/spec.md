## ADDED Requirements

### Requirement: Vitest unit test infrastructure
The system SHALL configure Vitest with jsdom environment and React Testing Library for component unit testing.

#### Scenario: Test runner execution
- **WHEN** developer runs `pnpm test`
- **THEN** Vitest MUST discover and execute all `*.test.tsx` files under `src/`

### Requirement: Component unit test coverage
The system SHALL have unit tests for: StageCard (status icon mapping), JobFilters (filter logic), RBACGate (role guard behavior), format utilities (date/duration formatting).

#### Scenario: StageCard status icon test
- **WHEN** StageCard receives `displayStatus="succeeded"`
- **THEN** the rendered output MUST contain the checkmark icon element

#### Scenario: RBACGate blocks unauthorized
- **WHEN** RBACGate wraps content and the auth store role is "viewer"
- **THEN** the children MUST NOT be rendered and a permission denied message MUST appear

### Requirement: Store unit tests
The system SHALL have unit tests for monitoring store's fetchAll and fetchJobs methods with mocked API client.

#### Scenario: Store fetch updates state
- **WHEN** `fetchAll()` is called with a mocked API returning 3 stages
- **THEN** `useMonitoringStore.getState().stages` MUST contain 3 items

### Requirement: Playwright E2E test infrastructure
The system SHALL configure Playwright with baseURL pointing to the Vite dev server.

#### Scenario: E2E runner execution
- **WHEN** developer runs `pnpm test:e2e`
- **THEN** Playwright MUST launch a browser and execute all `*.spec.ts` files under `e2e/`

### Requirement: Monitoring page E2E test
The system SHALL have a Playwright E2E test covering: page load, stages rendering, failure expansion, jobs filtering, log modal, retry action, permission denial for viewer role.

#### Scenario: E2E monitoring happy path
- **WHEN** the Playwright test navigates to `/monitoring` with mocked API responses
- **THEN** the summary bar, stage cards, jobs table, and trend charts MUST all render with the expected data

### Requirement: Forecast page E2E test
The system SHALL have a Playwright E2E test covering: map rendering, segment click, forecast chart display.

#### Scenario: E2E forecast interaction
- **WHEN** the Playwright test clicks on a river segment on the map
- **THEN** the forecast panel MUST appear and contain a rendered chart element
