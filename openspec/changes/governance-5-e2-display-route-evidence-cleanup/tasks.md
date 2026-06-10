## 1. Current Route Authority

- [x] 1.1 Update current entrypoint docs so `/` is the active display map entrypoint and legacy display routes are redirect aliases.
- [x] 1.2 Update `README.md` route language to remove pre-M26 multi-page route authority.
- [x] 1.3 Update `progress.md`, `CLAUDE.md`, node-27 runbooks, and validation docs where they still cite `/hydro-met` or old display pages as primary live proof.
- [x] 1.4 Keep #342 station-MVT and #389 popup live-click evidence out of this cleanup scope.
- [x] 1.5 Add a route-authority grep check for current docs so remaining `/hydro-met`, `/forecast`, `/meteorology`, `/flood-alerts`, `/segments/`, or `/basins/` mentions are historical, redirect-alias, or compatibility context.

## 2. Mocked And Live Evidence Boundary

- [x] 2.1 Consume Governance-2/#365's existing mocked-vs-live classification and avoid reopening broad classification work.
- [x] 2.2 Ensure docs still identify `live-display.spec.ts` as the node-27 live display lane unless a later issue adds another live profile.
- [x] 2.3 Update docs so mocked regression logs cannot be cited as live display receipts.
- [x] 2.4 Confirm the existing live no-broad-mock guard is documented; if code changes are needed, split them into a node-27/display_readonly issue. (Guard `assertLiveDisplaySpecsDoNotMockApis` already implemented + wired; no code change needed.)

## 3. Historical Visual Evidence

- [x] 3.1 Relabel or index M11/M15 visual evidence markdown and screenshots as historical mocked evidence without moving tracked assets by default.
- [x] 3.2 If any tracked visual evidence is moved or renamed, preserve old-path references, SHA/provenance notes, and the manual M15 workflow evidence contract. (No assets moved; provenance — PR #160 / `3e6fc48` — recorded in DOC_STATUS.)
- [x] 3.3 Verify new generated visual artifacts remain ignored unless explicitly promoted.

## 4. Node-27 Old Page Retirement Plan

- [x] 4.1 On node-27, migrate old URL handoff generation from old paths to `/` query form where production single-map code still generates `/forecast`, `/segments/...`, `/basins/...`, or `/flood-alerts` URLs. (#407: only live single-map handoff was `overviewDataContracts.ts` `m11QueryHref('/forecast', …)` → `/`; remaining old-route handoffs live only in orphaned old pages = #410 deletion scope.)
- [x] 4.2 On node-27, migrate mocked Playwright specs from old-page assertions to M26 single-map behavior or explicitly retained mocked legacy coverage.
- [x] 4.3 On node-27, migrate Vitest coverage away from `LegacyPagesHarness` and old page imports.
- [ ] 4.4 On node-27, delete `ForecastPage`, `FloodAlertPage`, `SegmentDetailPage`, `MeteorologyPage`, and `LegacyPagesHarness` only after imports and tests are clean.
- [ ] 4.5 Mark all old-page implementation issues as node-27/display_readonly execution items; do not implement frontend source changes from node-22.

## 5. Verification

- [ ] 5.1 Run `openspec validate governance-5-e2-display-route-evidence-cleanup --strict --no-interactive`.
- [ ] 5.2 Run a route-authority grep over current docs and confirm remaining old-route mentions are historical, redirect-alias, or compatibility context.
- [ ] 5.3 For node-27 implementation issues, require `cd apps/frontend && corepack pnpm test && corepack pnpm build`.
- [ ] 5.4 For mocked e2e migration issues, require `cd apps/frontend && corepack pnpm run test:e2e:mocked-regression`.
- [ ] 5.5 For live evidence issues, require explicit node-27 `PLAYWRIGHT_LIVE_BASE_URL` and `PLAYWRIGHT_LIVE_API_BASE_URL`; missing live env is blocked, not mocked.
