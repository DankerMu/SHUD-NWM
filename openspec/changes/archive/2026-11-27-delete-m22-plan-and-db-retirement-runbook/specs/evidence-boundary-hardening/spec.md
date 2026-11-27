## REMOVED Requirements

### Requirement: Current runbooks follow M26 route authority

**Reason**: One of its four scenarios is about `docs/runbooks/two-node-production-e2e-plan.md`, which
is deleted (#3406, owner decision of 2026-11-27: runbooks that are history only are deleted). A
MODIFIED block cannot drop a scenario, so the requirement is removed and added back under a new
title with its body and its three other scenarios word for word.

**Migration**: None. What the dropped scenario said about live browser proof (the current display
entrypoint plus the operations page as proof, the old alias only as a redirect smoke) is what the
scenario "node-27 checklist includes browser steps" requires of
`docs/runbooks/node-27-bringup-checklist.md`, where that rule is written.

## ADDED Requirements

### Requirement: Current runbooks follow the single-map route authority

Current runbooks SHALL describe `/` as the active display entrypoint and SHALL
describe `/overview`, `/hydro-met`, `/forecast`, `/meteorology`,
`/basins/:id`, and `/segments/:id` only as legacy redirect
aliases, compatibility references, or historical evidence.

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
