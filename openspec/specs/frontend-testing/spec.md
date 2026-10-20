# frontend-testing Specification

## Purpose
Define how the frontend is tested: the Vitest unit and Playwright E2E infrastructure, the required component, store and page coverage, the mocked-regression lane as an automatic CI gate, strict type checks, and keeping test files under the large-file limit without exemptions.

## Requirements

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

### Requirement: The mocked-regression Playwright lane SHALL be an automatic CI gate on frontend changes

The deterministic mocked-regression lane SHALL execute automatically in CI on every pull request whose changed files match the workflow's `frontend` path filter,
and SHALL fail the run when any of its tests fails. The lane SHALL NOT be configured with test retries:
this project has no recorded flakiness for it, and a retry would convert a real failure into a green run.

This requirement governs only the mocked lane. The `live-display` and `live-river-click` lanes bind to a real
runtime and keep node-27 as their oracle; the M15 visual-conformance lane stays manually dispatched under the
existing governance decision. A change that is not matched by the `frontend` filter SHALL NOT pay the lane's cost. A change that IS matched pays
it even when it touches no frontend source — the filter also covers the OpenAPI contract directory — and that cost
is accepted rather than avoided by a second, narrower filter, because a second filter would be a second path truth
to keep correct.

#### Scenario: A frontend pull request executes the lane

- **GIVEN** a pull request whose changed files match the CI `frontend` path filter
- **WHEN** the frontend CI job runs
- **THEN** the mocked-regression lane executes its specs and the run's log records a non-zero passed count,
  rather than skipping the lane or collecting it without executing assertions

#### Scenario: A broken mocked assertion turns the gate red

- **GIVEN** a branch in which one assertion in a mocked-regression spec has been inverted so the spec must fail
- **WHEN** the frontend CI job runs on that branch
- **THEN** the job fails, so the gate is proven to bite rather than exiting zero

#### Scenario: A backend-only or docs-only pull request does not run the lane

- **GIVEN** a pull request whose changed files match neither the `frontend` path filter nor any of its members
- **WHEN** CI runs
- **THEN** the frontend job is skipped and the lane costs nothing

### Requirement: Frontend type checks SHALL reject unused locals and parameters

The frontend type-check gates (`pnpm typecheck` and `pnpm run check:types`) SHALL run with `noUnusedLocals` and `noUnusedParameters` enabled, so a non-exported local declaration, an import binding or a parameter that nothing reads fails the existing Frontend Build lane without any additional tool or CI step. This complements, and does not replace, the requirement that `tsc` runs as its own gated step.

#### Scenario: A change leaves an unused local behind

- **WHEN** a source file covered by the app type-check declares a local or imports a binding that is never read
- **THEN** `pnpm typecheck` fails with the unused-declaration diagnostic

#### Scenario: A parameter must stay for its position

- **WHEN** a function has to keep a parameter it does not read because of its position or a callback signature
- **THEN** the parameter is named with a leading underscore and the type-check passes

### Requirement: Frontend test files SHALL stay under the large-file limit without exemptions
A frontend test file SHALL be split by topic instead of being exempted when it reaches the 1000-line limit. Shared fixtures go into a non-collected helper module. Each split file declares its own `vi.mock` calls. The set of collected test titles and every assertion SHALL stay unchanged across the split.

#### Scenario: Popup test split
- **WHEN** `apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx` is split
- **THEN** every resulting file is under 1000 lines, `.large-file-guard.json` no longer exempts it, and the collected `it` titles equal the 24 before the split

#### Scenario: Model assets store test split
- **WHEN** `apps/frontend/src/stores/__tests__/modelAssets.test.ts` is split
- **THEN** every resulting file is under 1000 lines, `.large-file-guard.json` gains no exclude, the 36 collected cases (with `it.each` expanded) equal those before the split, and the `expect(` multiset is unchanged
