## 1. Token and Component Baseline
- [x] 1.1 Audit 06B token mapping for nav, panels, cards, buttons, inputs, tags, tables, timeline, warning colors, chart defaults, shadows, focus rings, and typography; document the mapping in code comments, tests, or governance docs.
- [x] 1.2 Update shared CSS/components before page-specific styling. Expected output: shared tokens/components carry the final nav height, panel dimensions, timeline height, warning palette, focus styling, control height, spacing, z-index, and radius/shadow baseline used by all required routes; button/card/badge/select/tabs/dialog/toast share the same M15 token roots.
- [x] 1.3 Add practical token/color assertions or e2e checks for warning/status color consistency across overview, basin detail, and flood alerts.

## 2. Page Layout and States
- [x] 2.1 Bring `/overview`, `/basins/basin-demo` or equivalent deterministic basin detail route, `/flood-alerts`, and `/monitoring` into the map-first layout/state conformance matrix at 1920x1080, 1440x900, and 1280x900.
- [x] 2.2 Include extended route evidence for `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1`, `/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62`, `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237`, and `/system/model-assets`; document any unavailable extended route as a remaining surface with reason.
- [x] 2.3 Add loading, empty, error, restricted, RBAC-denied, partial-data, hover/focus, and icon-accessibility checks. Expected output: required and feasible extended non-happy states have canonical `1440x900` screenshot evidence, and key icon controls have accessible names.
- [x] 2.4 Add no-overlap assertions for supported desktop viewports. Expected output: 56px nav, 64px timeline where present, stable panel widths/collapse, no horizontal body scroll, and no incoherent panel/map/timeline overlap.
- [x] 2.5 Preserve existing behavior tests, URL restore behavior, RBAC navigation behavior, and M14 redaction behavior; avoid backend/API contract changes.
- [x] 2.6 Preserve frontend fixture field compatibility. Expected output: tests or e2e fixtures still cover `validTime`, `cycle`, `source`, `basinVersionId`, `riverNetworkVersionId`, `segmentId`, meteorology `source`/`variable`, warning level colors, unit labels, and state labels without backend schema mutations.
- [x] 2.7 Preserve valid-time and timeline behavior. Expected output: map-first timeline still uses API-provided `valid_times[]`, flood/forecast links keep restored valid time, meteorology restricted grid keeps the valid-time control disabled, and no visual test fabricates a fixed hour series as production data.

## 3. Screenshot Evidence
- [x] 3.1 Capture 1920x1080 and 1440x900 full-layout screenshots for each required route in loaded state.
- [x] 3.2 Capture 1280x900 collapsed/default-left screenshots for each required route and optional <1280 unsupported-state evidence if implemented.
- [x] 3.3 Capture or generate state evidence for loading, empty, error, restricted/RBAC-denied, and partial-data states. Expected output: each required state label has canonical `1440x900` screenshot evidence and an automated e2e assertion tied to a deterministic fixture; loaded required route states keep 1920/1440/1280 viewport evidence.
- [x] 3.4 Store evidence under `.codex/evidence/issue-176/screenshots/` and create `.codex/evidence/issue-176/manifest.json` or documented markdown with route, viewport, fixture mode, real commit SHA, state label, command, and artifact path. Placeholder SHA values are rejected.
- [x] 3.5 Ensure screenshot/evidence generation is bounded and repeatable: scoped output path, no unbounded artifact discovery, no production credentials, deterministic stubs for known external map tile/style/font requests, blocked unexpected non-local network, and clear failure output if capture fails.

## 4. Governance
- [x] 4.1 Document visual review checklist, acceptable deltas, no-overlap criteria, and when visual regressions block a PR.
- [x] 4.2 Document blocking criteria: text overflow, horizontal scroll, panel/timeline overlap, missing accessible names, lost focus states, inconsistent warning colors, fake success data in error/restricted states, missing required screenshot metadata/state labels, and placeholder SHA values.
- [x] 4.3 Run frontend tests, E2E checks, build, and screenshot capture commands. Minimum expected outputs: `openspec validate m15-frontend-visual-conformance --strict --no-interactive` exits 0; `cd apps/frontend && corepack pnpm test` exits 0; `cd apps/frontend && corepack pnpm build` exits 0; the focused Playwright/e2e screenshot command exits nonzero if required evidence, no-overlap assertions, accessible names, required state labels, real SHA metadata, or shared token assertions are missing.
- [x] 4.4 Update `progress.md` with visual conformance status, required/extended routes covered, evidence manifest path, real commit SHA, final-head rerun requirement, and remaining surfaces.
- [x] 4.5 Gate M15 visual evidence in CI with `corepack pnpm run test:e2e:m15-visual` and upload `.codex/evidence/issue-176/**` as a CI artifact without committing screenshots or manifests.

## Non-Goals and Guardrails
- [x] 5.1 Do not add backend endpoints, change OpenAPI contracts, or change production data schemas.
- [x] 5.2 Do not claim pixel-perfect conformance; this issue gates measurable layout/state/token evidence and documented acceptable deltas.
- [x] 5.3 Do not commit volatile screenshot binaries unless the repository policy requires it; local `.codex/evidence/issue-176/` artifacts may be referenced in PR evidence.
- [x] 5.4 Do not change production time-series semantics, forcing metadata semantics, or backend response fields; use deterministic frontend fixtures only for visual states.
