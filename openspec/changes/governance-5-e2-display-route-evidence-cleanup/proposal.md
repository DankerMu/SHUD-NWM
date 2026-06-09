## Why

M26 consolidated the display frontend into a single-map entrypoint, but current docs, mocked e2e evidence, and legacy page tests still make pre-M26 routes look active. This creates node-27 governance entropy and can cause mocked regression evidence to be mistaken for live display proof.

## What Changes

- Establish current display route authority: `/` is the single-map entrypoint; legacy display routes are compatibility redirects unless a later product decision retires them.
- Update current docs and runbooks so `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`, `/basins/:id`, and `/segments/:id` are not described as primary active pages.
- Update current entrypoint status docs including `progress.md`, because it currently remains a cross-session source of truth.
- Keep node-27 live proof separate from mocked Playwright regression specs.
- Consume Governance-2/#365 mocked-vs-live classification rather than recreating that governance split.
- Create a staged node-27 execution plan for old frontend page retirement: first migrate URL handoff and tests, then delete pages.
- Mark frontend old-page retirement implementation as node-27/display_readonly work; this node-22 governance pass creates the change and issues only.

## Capabilities

### New Capabilities

- `display-route-evidence-cleanup`: Provides display route authority, mocked-vs-live evidence cleanup, and staged node-27 old-page retirement planning.

### Modified Capabilities

<!-- No existing product capability is modified by this OpenSpec planning change. -->

## Impact

- Docs: `README.md`, `progress.md`, `CLAUDE.md`, `docs/runbooks/node-27-bringup-checklist.md`, `docs/runbooks/two-node-production-e2e-plan.md`, `docs/runbooks/qhh-mvp-production-like-e2e-checklist.md`, validation docs as needed.
- Frontend node-27 execution scope: `apps/frontend/src/App.tsx`, old URL handoff helpers, `apps/frontend/e2e/**`, `apps/frontend/src/__tests__/**`, old page components, M11/M15 visual evidence files.
- Evidence: `live-display.spec.ts` remains the live display lane; mocked specs remain deterministic regression unless explicitly retired.
- Non-goals: no node-22 frontend implementation, no removal of legacy route redirects without a separate compatibility decision, no station-MVT or #389 live-click closure.
