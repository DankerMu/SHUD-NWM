# evidence-boundary-hardening Specification

## Purpose
Harden the boundary between mocked regression and live evidence: current runbooks follow the active route authority, mock detection covers multiline route registrations, artifact ownership wording matches the audit, and the display runtime rejects compute-only copyback authority.

## Requirements

### Requirement: Current runbooks follow M26 route authority

Current runbooks SHALL describe `/` as the active display entrypoint and SHALL
describe `/overview`, `/hydro-met`, `/forecast`, `/meteorology`,
`/basins/:id`, and `/segments/:id` only as legacy redirect
aliases, compatibility references, or historical evidence.

#### Scenario: two-node E2E plan defines browser proof
- **WHEN** `docs/runbooks/two-node-production-e2e-plan.md` describes live
  browser proof
- **THEN** it requires `/` plus `/ops` for current live display proof and records
  `/hydro-met -> /` only as a legacy redirect smoke, not as active display proof

#### Scenario: deployment overview describes node-27
- **WHEN** `docs/runbooks/two-node-deployment-overview.md` describes node-27
  user-facing display behavior
- **THEN** it names `/` as the single-map display entrypoint and `/ops` as the
  operational display path

#### Scenario: node-27 checklist includes browser steps
- **WHEN** `docs/runbooks/node-27-bringup-checklist.md` lists current browser
  validation
- **THEN** current steps use `/` and `/ops`, while old display paths are
  described as redirect compatibility checks

#### Scenario: route-authority grep runs against current runbooks
- **WHEN** route-authority validation scans current docs and runbooks
- **THEN** references to `/overview`, `/hydro-met`, `/forecast`,
  `/meteorology`, `/basins/:id`, `/segments/:id`, and
  concrete forms such as `/basins/demo` and `/segments/demo` are each
  classified as historical evidence, redirect alias checks, compatibility
  context, or drift

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

### Requirement: Broad mock detection covers multiline registrations

The governance audit SHALL detect broad API route mocks even when the
`page.route` call and API glob are split across multiple lines.

#### Scenario: live-looking spec uses multiline broad mock
- **WHEN** a live-labelled or unallowlisted Playwright spec writes
  `page.route(` on one line and `'**/api/v1/**'` on a following line
- **THEN** the entropy audit reports the same broad-mock finding it would report
  for a single-line registration

#### Scenario: mocked-labelled spec uses multiline broad mock
- **WHEN** a mocked, preview, or visual evidence spec uses a multiline broad API
  mock
- **THEN** the audit can classify it consistently with the same allowlist logic
  used for single-line mocked registrations

### Requirement: Artifact ownership wording matches audit expectations

Governed artifact ownership documentation SHALL include the literal ownership
terms required by the entropy audit.

#### Scenario: governance audit checks DOC_STATUS

- **WHEN** the audit checks `docs/governance/DOC_STATUS.md`
- **THEN** the document explicitly mentions every ownership term the audit
  requires (the agent, review, evidence and artifact paths) and no
  gate-eligible ownership finding is reported

### Requirement: The display role rejects compute-only copyback authority

The `display_readonly` runtime role SHALL reject compute-control path
configuration, including `NHMS_OBJECT_STORE_COPYBACK_ROOT`, so display nodes can
read published artifacts without gaining run-product copyback authority.

#### Scenario: display role includes copyback root

- **WHEN** the API starts with `NHMS_SERVICE_ROLE=display_readonly` and
  `NHMS_OBJECT_STORE_COPYBACK_ROOT` configured
- **THEN** startup is refused and the key is reported as a
  display-forbidden compute-control path env
