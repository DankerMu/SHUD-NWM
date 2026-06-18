# display-route-evidence-cleanup Specification

## Purpose
TBD - created by archiving change governance-5-e2-display-route-evidence-cleanup. Update Purpose after archive.
## Requirements
### Requirement: Display route authority is current

Current docs SHALL describe `/` as the active display map entrypoint and describe pre-M26 display paths as compatibility redirects or historical references.

#### Scenario: contributor reads README
- **WHEN** a contributor reads the current frontend route section
- **THEN** `/` is presented as the single-map display entrypoint and old display paths are not presented as independent active pages

#### Scenario: contributor reads progress index
- **WHEN** a contributor reads `progress.md` for current cross-session status
- **THEN** display live proof language names `/` as the current display entrypoint and treats `/hydro-met` only as historical or redirect compatibility context

#### Scenario: runbook mentions `/hydro-met`
- **WHEN** a current runbook mentions `/hydro-met`
- **THEN** the text states that it is a legacy redirect alias or historical evidence, not the current primary display page

### Requirement: Governance-2 mocked-vs-live split is not duplicated

Display cleanup SHALL consume the existing Governance-2/#365 mocked-vs-live evidence classification and SHALL NOT recreate that governance split as new work.

#### Scenario: existing mocked regression spec is found
- **WHEN** a spec already classified by Governance-2/#365 is found during cleanup
- **THEN** E2 updates stale route/page wording or references only, unless a separate node-27 issue is needed for behavior migration

#### Scenario: new live-looking broad mock is discovered
- **WHEN** cleanup discovers a broad API mock in a live-looking profile not covered by existing policy
- **THEN** the issue records it as a focused node-27 follow-up instead of broadening E2 into a second #365

### Requirement: Mocked e2e evidence is separate from live proof

Display evidence docs SHALL distinguish deterministic mocked regression specs from node-27 live display proof.

#### Scenario: mocked Playwright spec uses API route mocks
- **WHEN** a Playwright spec uses broad API route mocks
- **THEN** docs and config classify it as mocked regression evidence rather than live node-27 proof

#### Scenario: live display spec is executed
- **WHEN** `test:e2e:live-display` runs against node-27
- **THEN** it does not rely on broad API mocks and uses explicit live base URL configuration

### Requirement: Old frontend page retirement is staged on node-27

Old display page components SHALL NOT be deleted until old URL handoff, Vitest coverage, Playwright coverage, and visual evidence expectations have been migrated or explicitly retired.

#### Scenario: old page component is still test-harnessed
- **WHEN** `LegacyPagesHarness` or mocked specs still import or assert an old page
- **THEN** deletion of that old page is blocked

#### Scenario: old URL handoff still targets legacy page paths
- **WHEN** production single-map code still generates handoff URLs such as `/forecast`, `/segments/...`, `/basins/...`, or `/flood-alerts`
- **THEN** deletion of the corresponding old page or redirect alias is blocked until node-27 migrates the handoff to `/` query form or explicitly keeps compatibility

#### Scenario: Vitest, Playwright, or M15 visual lane still depends on old pages
- **WHEN** any Vitest harness, mocked Playwright spec, or M15 visual evidence lane still depends on an old page component
- **THEN** node-27 must migrate or explicitly retire that dependency before deleting the old page or harness

#### Scenario: node-27 completes migration
- **WHEN** old URL handoff and tests no longer depend on an old page component
- **THEN** node-27 implementation may delete the old page and its dedicated harness

### Requirement: Frontend implementation belongs to node-27

Issues that change `apps/frontend` old pages SHALL mark implementation ownership as node-27/display_readonly.

#### Scenario: issue is created for old page retirement
- **WHEN** the GitHub issue body is created
- **THEN** it states that implementation is performed on node-27 and this node only produced governance planning

### Requirement: Legacy redirects remain until explicitly retired

Legacy display route redirects SHALL remain unless a later issue explicitly accepts the compatibility impact.

#### Scenario: cleanup issue edits route aliases
- **WHEN** a cleanup issue proposes removing redirect aliases
- **THEN** it must include a compatibility decision and migration evidence before implementation

### Requirement: Historical visual evidence preserves provenance

Tracked M11/M15 visual evidence SHALL remain traceable when relabeled, indexed, or moved.

#### Scenario: tracked visual evidence is moved
- **WHEN** a cleanup issue moves or renames tracked historical visual evidence
- **THEN** it preserves old-path references, SHA/provenance notes, and the manual M15 workflow evidence contract

