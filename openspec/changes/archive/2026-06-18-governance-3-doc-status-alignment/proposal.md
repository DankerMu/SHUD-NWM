## Why

The repository has many useful documents, but their authority levels are unclear. Current runbooks, historical OpenSpec worklogs, old implementation plans, stale bug records, and live receipts coexist without a status model, causing developers to treat outdated facts as current guidance.

## What Changes

- Add a document status authority model that marks docs as current entrypoint, current runbook, validation matrix, architecture/spec, historical, superseded, or archived.
- Link the document status model from a current entrypoint.
- Mark `IMPLEMENTATION_PLAN.md` as historical/superseded at the repository root, or archive it with a root pointer to current entrypoints.
- Align high-impact stale node-27 live MVT facts in current entrypoints and runbooks.
- Route the remaining node-27 bbox/framing popup live-click evidence gap to #389.
- Add and pass through `NHMS_ENABLE_LIVE_POSTGIS_MVT` in display readonly example config and compose.
- Convert `docs/bugs.md` into a triaged governance ledger with status, owner area, evidence, and retest commands for governed historical bugs.
- Define `.agents`, `.codex`, `apps/frontend/artifacts`, and root `artifacts/` ownership so contributor guidance, Git ignore behavior, Docker context, and tracked historical assets no longer contradict each other.

## Out of Scope

- Editing skill contents except ownership/ignore consequences.
- Regenerating visual evidence or reclassifying runtime production artifacts.

## Capabilities

### New Capabilities

- `doc-status-alignment`: Defines document authority, freshness, and ownership rules for governance and onboarding.

### Modified Capabilities

<!-- No product capability is modified. -->

## Impact

- Dependency: starts after `governance-0-ci-contract-baseline` is merged, or with an explicit maintainer waiver that lists current red checks.
- Governance docs: `docs/governance/DOC_STATUS.md`.
- Current entrypoint docs: `README.md` or an equivalent current entrypoint that links `DOC_STATUS.md`.
- Historical docs: `IMPLEMENTATION_PLAN.md` or a root pointer plus archived copy.
- Current node-27 docs: `CLAUDE.md`, `progress.md`, `docs/runbooks/node-27-bringup-checklist.md`, `docs/runbooks/display-readonly-live-mvt.md`.
- Display config: `infra/env/display.example`, `infra/compose.display.yml`.
- Bug ledger: `docs/bugs.md`.
- Agent/artifact ownership: `.gitignore`, `.dockerignore`, `progress.md`,
  `docs/governance/DOC_STATUS.md`, and tracked path families under `.agents`,
  `.codex`, and `apps/frontend/artifacts`.
