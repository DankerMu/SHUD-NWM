## Context

M15 frontend visual conformance follows the completed M11 overview/basin drill-down delivery and turns a documented product gap into implementable, testable work. Existing production-like closure and M11 behavior must remain stable.

## Fixture

Issue type: feature/test/evidence.
Project profile: other - React frontend for the NWM web application.
Blast radius: high user-visible frontend surface.
Fixture level: expanded.
Repair intensity: broad-expanded, because the issue spans shared visual tokens/components, multiple public routes, RBAC-visible navigation, state/error rendering, accessibility checks, and screenshot evidence governance.

Mandatory expanded triggers:

- Public route and navigation entrypoints across `/overview`, basin detail, flood alerts, monitoring, segment detail, meteorology, and model assets.
- Shared visual tokens/components used by multiple business pages.
- Local screenshot artifact and manifest writes.
- RBAC/restricted/error states, including protected system-management surfaces.
- Route/query compatibility for restored URL state and deterministic visual fixtures.
- M12/M13/M14 visual surfaces now available after #173, #174, and #175.
- Visual regression governance that future PRs will use as a review gate.

## Change Surface

- Shared frontend tokens and layout CSS: `apps/frontend/src/index.css` and any shared component styles for nav, panels, cards, controls, tags, tables, focus rings, warnings, charts, map shell, and timeline.
- Shared application shell/navigation surfaces, including RBAC-gated nav visibility and stable top nav height.
- Required core route matrix: `/overview`, `/basins/basin-demo`, `/flood-alerts`, and `/monitoring`.
- Extended route matrix now that dependencies are complete: `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1`, `/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62`, `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237`, and `/system/model-assets`.
- Visual governance docs/evidence manifest and M15 progress tracking.

## Must Preserve

- Existing routing, role/RBAC behavior, fixture data shape, URL restore behavior, map/timeline interactions, and behavior tests.
- Existing backend/API contracts; visual state fixtures may mock frontend responses but must not require backend schema changes.
- Existing M11-M14 page capabilities and redaction guarantees, especially restricted/system pages.
- Supported desktop conformance floor: 1920x1080, 1440x900, and 1280x900.

## Must Add or Change

- 06B token audit and shared token/component alignment before page-specific fixes.
- A route/state/viewport matrix for visual conformance with deterministic evidence capture.
- No-overlap and accessibility oracles for map-first pages.
- Documented screenshot artifact path and manifest metadata.
- A visual review checklist with blocking criteria and acceptable-delta rules.

## Design Decisions

- This is visual/test/evidence work only; remove API/data contract changes unless a state fixture is needed for visual tests.
- Viewport acceptance: 1920x1080 and 1440x900 full layout; 1280x900 collapsible layout with default-left behavior; <1280 may show unsupported prompt if implemented.
- Screenshot artifacts should live under `.codex/evidence/issue-176/screenshots/` with a manifest under `.codex/evidence/issue-176/manifest.json` or a documented markdown equivalent. Each record must include route, viewport, fixture mode, commit SHA, state label, capture command, and artifact path.
- Manifest commit SHA must resolve to a real commit from `M15_EVIDENCE_SHA`, PR head SHA env, `GITHUB_SHA`, `CI_COMMIT_SHA`, or `git rev-parse HEAD`. CI evidence must match `M15_EVIDENCE_SHA` when set; the CI workflow sets it to the pull request head SHA for PR events and to `github.sha` for push/non-PR events. PR evidence must be regenerated after the final commit so it cites the frozen PR head SHA under review. Placeholder values such as `local-uncommitted` are not acceptable.
- M15 evidence blocks unexpected non-local network traffic. Known external map tile/style/font hosts are fulfilled with deterministic neutral stubs so screenshots do not depend on live OpenTopoMap, Esri, or OpenStreetMap responses.
- Pass/fail includes no incoherent overlap, stable panel/timeline dimensions, accessible names for icon controls, visible focus/hover states where tested, and design-token color/spacing checks where practical.
- Playwright is the primary deterministic evidence runner. `agent-browser` may be used as an additional manual/browser smoke tool, especially for exploratory page inspection and screenshot sanity checks.

## Dependency Order

- Token baseline before page layout passes.
- Page layout and states before screenshot matrix.
- Screenshot matrix before governance closeout.

## Route, Viewport, and State Matrix

Required route gates:

| Route | Required viewports | Required states |
|---|---|---|
| `/overview` | 1920x1080, 1440x900, 1280x900 | loaded, loading or skeleton, empty or partial data, API error |
| `/basins/basin-demo` or deterministic basin detail URL | 1920x1080, 1440x900, 1280x900 | loaded, empty segments, partial data, API error |
| `/flood-alerts` | 1920x1080, 1440x900, 1280x900 | loaded, empty alerts, warning levels, API error |
| `/monitoring` | 1920x1080, 1440x900, 1280x900 | loaded, empty jobs, failed job/error, restricted or RBAC-denied |

Extended evidence routes:

| Route | Required when fixture exists | State focus |
|---|---|---|
| `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1` | yes after #173 | loaded segment forecast, missing segment, chart/error state |
| `/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62` | yes after #174 | loaded grid metadata, empty grid, restricted/error, disabled timeline |
| `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237` | yes after #174 | loaded stations, empty stations, station detail/error |
| `/system/model-assets` | yes after #175 | allowed role loaded, restricted role denied, loading/error redaction |

Loaded required routes are captured at all required desktop viewports. Non-happy required and
extended state labels are captured at the canonical `1440x900` review viewport to keep the evidence
gate deterministic and bounded while still proving the full declared route/state axis.

## No-Overlap and Accessibility Oracle

- Top navigation height remains 56px and does not cover page content.
- Bottom timeline height remains 64px where present and does not cover map controls, legends, charts, or panel content.
- Left/right panels retain documented widths or collapse behavior and do not overlap the central map incoherently at 1920, 1440, or 1280 desktop viewports.
- Body and page roots do not create horizontal scroll at supported desktop viewports.
- Key icon-only controls have accessible names, deterministic roles, and visible focus indicators.
- Loading, empty, error, restricted, RBAC-denied, and partial-data states use explicit accessible text and do not display fake success data.
- Warning/return-period colors remain consistent across overview, basin detail, and flood alerts.

## Risk Packs Considered

- Public API / CLI / script entry: selected - public frontend routes and navigation are user entrypoints.
- Config / project setup: selected - frontend test/e2e/screenshot runner wiring may be touched.
- File IO / path safety / overwrite: selected - screenshot evidence and manifests are written to local artifact paths; no production file IO.
- Schema / columns / units / field names: selected - frontend fixture response fields, unit labels, route query fields, and state labels must remain compatible, but backend schema changes are out of scope.
- Geospatial / CRS / shapefile sidecars: not selected - no GIS coordinate, CRS, tile schema, or sidecar format changes.
- Time series / forcing / temporal boundaries: selected - timeline, forecast, flood, and meteorology route states must preserve existing time labels, valid-time restoration, disabled-timeline behavior, and forcing metadata.
- Numerical stability / conservation / NaN: not selected - no solver or numerical computation changes.
- Solver runtime / performance / threading: not selected - no solver/runtime code changes.
- Resource limits / large input / discovery: selected - e2e routes and screenshot capture must be bounded, deterministic, and avoid unbounded artifact discovery.
- Legacy compatibility / examples: selected - existing M11-M14 routes/tests and docs remain compatible.
- Error handling / rollback / partial outputs: selected - visual error/restricted/partial states and failed screenshot capture behavior must be explicit.
- Release / packaging / dependency compatibility: selected - frontend build/test/e2e commands and dependency lock behavior must remain CI compatible.
- Documentation / migration notes: selected - progress and governance docs are part of acceptance.

## Boundary Surface Checklist

- Shared helper roots: CSS variables, shared UI components, app shell/nav, map shell/timeline primitives, test fixture helpers.
- Public entrypoints: route paths in the required and extended matrices plus RBAC-gated navigation.
- Read surfaces: fixture data consumed by frontend tests and visual pages.
- Write surfaces: local screenshot directory and evidence manifest only.
- Producer/consumer evidence boundaries: screenshot runner, manifest, PR evidence comment, governance docs.
- Stale-state/idempotency boundaries: route restoration, RBAC identity changes, loading/error transitions, repeated screenshot capture.
- Unchanged downstream consumers: backend APIs, OpenAPI schemas, model asset redaction contracts, and existing page behavior tests.

## Risks and Mitigations

- Risk: screenshot evidence becomes stale. Mitigation: record route, viewport, fixture mode, real commit SHA, and artifact path; reject placeholder SHA values.
- Risk: visual work mutates behavior. Mitigation: existing route/E2E tests must remain green.
- Risk: one-off fixes. Mitigation: shared tokens/components first.
- Risk: broad visual fixes introduce hidden RBAC or redaction regressions. Mitigation: include restricted/RBAC-denied states and existing M14 behavior tests in verification.
- Risk: evidence capture is flaky. Mitigation: deterministic fixtures, bounded viewport matrix, stable state labels, and documented optional manual `agent-browser` smoke separate from CI.

## Verification

- OpenSpec strict validation.
- Frontend unit/E2E/build checks.
- Screenshot evidence for named routes and viewport matrix.
- Governance/progress docs updated with route, fixture, real SHA, and remaining-surface metadata.

Expected verification outputs:

- `openspec validate m15-frontend-visual-conformance --strict --no-interactive` exits 0.
- `cd apps/frontend && corepack pnpm test` exits 0.
- `cd apps/frontend && corepack pnpm build` exits 0.
- Focused Playwright/e2e visual command exits 0 only when required route evidence, no-overlap assertions, accessibility checks, and manifest metadata are present.
- Existing route compatibility tests continue to prove `validTime`, `cycle`, `source`, `basinVersionId`, `riverNetworkVersionId`, and `segmentId` restoration.

## Review Focus

- Shared tokens/components were updated before page-level styling, and page fixes do not fork the visual language.
- Required route/state/viewport gates are covered by tests or screenshot evidence with explicit expected outputs.
- No-overlap and accessibility oracles are mechanical enough for reviewers to reproduce.
- RBAC/restricted/error states are real frontend states, not only happy-path screenshots.
- Evidence paths are local and bounded, with metadata tied to the exact commit under review.
