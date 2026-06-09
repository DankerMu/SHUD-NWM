## Context

M26 changed the display frontend from a collection of pages into a single full-screen map. The code keeps legacy routes as redirect aliases to protect deep links, but current docs and tests still preserve old page language. The old pages are not all directly routed in production, yet they remain test-harnessed through Vitest, mocked Playwright specs, and historical visual evidence lanes.

This change is design-and-issue work from the current node. Actual frontend implementation must be performed on node-27 where the display_readonly environment and frontend workflow can be validated.

## Goals / Non-Goals

**Goals:**

- Make current docs say `/` is the display entrypoint and old display routes are compatibility redirects.
- Keep live display proof separate from mocked regression evidence.
- Preserve external deep-link compatibility unless a later product decision explicitly retires aliases.
- Define a safe node-27 staged sequence for old frontend page retirement.
- Ensure old page deletion is blocked until URL handoff and tests are migrated.

**Non-Goals:**

- No implementation of frontend source changes from this node.
- No deletion of `LegacyRedirect` aliases in this change.
- No closure of #342 station-MVT or #389 popup live-click evidence.
- No replacement of live node-27 receipts with mocked Playwright evidence.

## Decisions

### D1. Treat old routes as compatibility aliases, not active pages

The route authority should describe `/` as the active display page and old display paths as redirects that preserve search/query semantics. Docs may mention old routes only as compatibility aliases or historical evidence.

### D2. Stage old-page retirement on node-27

Old page components such as `ForecastPage`, `FloodAlertPage`, `SegmentDetailPage`, and `MeteorologyPage` can be retired only after node-27 work migrates old URL handoff generation, Vitest coverage, mocked Playwright specs, and any M15 visual lane expectations.

### D3. Keep mocked regression valuable but not live proof

Mocked Playwright specs may remain as deterministic frontend regression tests. They must not be cited as node-27 live proof, and live display specs must continue to reject broad API mocks.

This change consumes Governance-2/#365's existing mocked-vs-live classification. It only corrects remaining stale route/page wording, stale evidence references, or missing guard documentation. If implementation discovers a new live-looking broad mock, that belongs in a focused node-27 follow-up rather than reopening the Governance-2 classification work.

### D4. Archive evidence without losing provenance

M11/M15 visual evidence files and screenshots are governed historical evidence. The default action is to update references, index entries, and status wording. Moving or renaming tracked historical assets requires preserving SHA/provenance, old-path pointers, and the manual M15 workflow evidence contract.

## Risks / Trade-offs

- **Risk: breaking external deep links.** Mitigation: retain legacy redirects until a separate compatibility-retirement decision.
- **Risk: deleting useful frontend coverage.** Mitigation: migrate mocked/Vitest coverage before page deletion.
- **Risk: node-22 change cannot verify frontend behavior.** Mitigation: issue bodies mark frontend implementation as node-27 work and require node-27 validation.
- **Risk: live evidence confusion persists.** Mitigation: docs must name `live-display.spec.ts` and live receipts separately from mocked specs.

## Migration Plan

1. Refresh current docs and route authority language.
2. Relabel or index historical visual evidence without moving tracked assets unless provenance is preserved.
3. On node-27, migrate old URL handoff to `/` query form where needed.
4. On node-27, migrate mocked Playwright and Vitest coverage away from old page assumptions.
5. On node-27, delete old page components and `LegacyPagesHarness` only after imports and tests are clean.
6. Leave alias redirect retirement out of scope unless a later issue explicitly accepts deep-link breakage.

## Open Questions

- Whether any external users still rely on legacy display route URLs beyond redirect compatibility.
