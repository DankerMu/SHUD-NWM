## Why

The repository has many useful documents, but their authority levels are unclear. Current runbooks, historical OpenSpec worklogs, old implementation plans, stale bug records, and live receipts coexist without a status model, causing developers to treat outdated facts as current guidance.

## What Changes

- Add a document status authority model that marks docs as current entrypoint, current runbook, validation matrix, architecture/spec, historical, superseded, or archived.
- Align high-impact stale docs: `CLAUDE.md`, `progress.md`, node-27 checklist, display live MVT runbook/config, `docs/bugs.md`, and `IMPLEMENTATION_PLAN.md`.
- Turn `docs/bugs.md` into a triaged ledger with status, owner area, retest command, and resolved/superseded evidence.
- Resolve `.agents`/`.codex`/frontend artifacts ownership contradiction between tracked assets and "do not stage" guidance.

## Capabilities

### New Capabilities

- `doc-status-alignment`: Defines document authority, freshness, and ownership rules for governance and onboarding.

### Modified Capabilities

<!-- No product capability is modified. -->

## Impact

- Dependency: starts after `governance-0-ci-contract-baseline` is merged, or with an explicit maintainer waiver that lists current red checks.
- Governance docs: `docs/governance/DOC_STATUS.md`.
- Current docs: `README.md`, `progress.md`, `CLAUDE.md`, `docs/VALIDATION.md`.
- Runbooks: `docs/runbooks/node-27-bringup-checklist.md`, `docs/runbooks/display-readonly-live-mvt.md`, `docs/runbooks/qhh-continuous.md`.
- Historical docs: `IMPLEMENTATION_PLAN.md`, old plans, OpenSpec worklogs.
- Bug ledger: `docs/bugs.md`.
- Ignore/ownership docs: `.gitignore`, `.dockerignore`, `progress.md`, `.agents`, `.codex`, `apps/frontend/artifacts`.
