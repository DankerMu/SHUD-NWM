## MODIFIED Requirements

### Requirement: Mocked Playwright regression is separate from live proof

Playwright specs that register broad API route mocks SHALL be classified as
mocked regression or rewritten so they cannot be mistaken for live display
receipts.

Broad `page.route('**/api/v1/**')` API mocks SHALL be valid only in specs or
projects classified as mocked-regression, preview, or visual evidence. Mocked
operator, retry, cancel, and single-map routing checks SHALL NOT be accepted as
`display_readonly` live display receipts.

#### Scenario: mocked spec registers broad API mock
- **WHEN** `apps/frontend/e2e/m11-routes.mocked.spec.ts` or
  `apps/frontend/e2e/monitoring.mocked.spec.ts` registers
  `page.route('**/api/v1/**')`
- **THEN** the spec, project configuration, or validation docs classify it as
  mocked regression and the governance audit no longer treats it as active
  gate-eligible drift

#### Scenario: live display profile runs
- **WHEN** `corepack pnpm run test:e2e:live-c4-display` or `corepack pnpm run test:e2e:live-river-click` executes
- **THEN** it uses explicit live base URLs, has no local-dev or
  `https://api.example.test` fallback, and rejects broad API route mocks in
  live-labelled specs

#### Scenario: validation docs describe evidence lanes
- **WHEN** `docs/VALIDATION.md` describes frontend evidence profiles
- **THEN** mocked-regression, preview, visual, live-c4-display, and live-river-click lanes state
  whether API mocks are allowed and whether the lane can produce live receipts
