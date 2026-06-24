# evidence-boundary-hardening Specification

## Purpose
TBD - created by archiving change governance-6-entropy-structural-burndown. Update Purpose after archive.
## Requirements
### Requirement: Current runbooks follow M26 route authority

Current runbooks SHALL describe `/` as the active display entrypoint and SHALL
describe `/overview`, `/hydro-met`, `/forecast`, `/meteorology`,
`/flood-alerts`, `/basins/:id`, and `/segments/:id` only as legacy redirect
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
  `/meteorology`, `/flood-alerts`, `/basins/:id`, `/segments/:id`, and
  concrete forms such as `/basins/demo` and `/segments/demo` are each
  classified as historical evidence, redirect alias checks, compatibility
  context, or drift

### Requirement: Historical MVP runbooks are clearly marked

Old MVP runbooks that preserve pre-M26 `/hydro-met` evidence SHALL be marked as
historical or superseded before current agents can mistake them for active
instructions.

#### Scenario: historical checklist remains in docs/runbooks
- **WHEN** a runbook such as
  `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md` keeps M21/MVP
  historical `/hydro-met` redirect-era steps
- **THEN** the file or affected section contains a visible historical or
  superseded notice that points to current M26 route authority

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
- **WHEN** `corepack pnpm run test:e2e:live-display` executes
- **THEN** it uses explicit live base URLs, has no local-dev or
  `https://api.example.test` fallback, and rejects broad API route mocks in
  live-labelled specs

#### Scenario: validation docs describe evidence lanes
- **WHEN** `docs/VALIDATION.md` describes frontend evidence profiles
- **THEN** mocked-regression, preview, visual, and live-display lanes state
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
- **THEN** the document explicitly mentions `.dockerignore` in the artifact
  ownership policy and the corresponding gate-eligible finding is gone

### Requirement: Display runtime rejects compute-only copyback authority

The `display_readonly` runtime role SHALL reject compute-control path
configuration, including `NHMS_OBJECT_STORE_COPYBACK_ROOT`, so display nodes can
read published artifacts without gaining run-product copyback authority.

#### Scenario: display role includes copyback root
- **WHEN** runtime configuration or the Docker entrypoint starts with
  `NHMS_SERVICE_ROLE=display_readonly` and
  `NHMS_OBJECT_STORE_COPYBACK_ROOT` configured
- **THEN** startup/runtime validation blocks the configuration as a
  display-forbidden compute-control path env

#### Scenario: compute role includes copyback root
- **WHEN** runtime/static Docker validation checks a compute-control service
  with `OBJECT_STORE_ROOT` and `NHMS_OBJECT_STORE_COPYBACK_ROOT`
- **THEN** the copyback root is required to be compute-only, mounted for compute
  publication, and rejected if it overlaps `OBJECT_STORE_ROOT` except for exact
  equality semantics governed by publisher copyback validation
