## Why

M11 delivered functional map-first pages, but effect-image and 06B visual convergence still need measurable gates. The project needs a visual conformance stage with token, layout, state, accessibility, and screenshot evidence criteria rather than ad hoc style fixes.

## What Changes

- Codify visual-token alignment for nav, panels, cards, buttons, tables, inputs, tags, timeline, warning colors, and chart defaults.
- Define map-first layout conformance for overview, basin detail, flood alerts, monitoring, and later M12-M14 pages when present.
- Add measurable loading, empty, error, restricted, RBAC-denied, and partial-data state conformance plus accessibility checks.
- Capture screenshot evidence at 1920x1080 and 1440x900 full layout, 1280x900 collapsed/default-left behavior, and optional `<1280` unsupported-state prompt.
- Define visual regression governance: artifact paths, review checklist, no-overlap assertions, and acceptable deltas.

## Capabilities

### New Capabilities

- `visual-token-alignment`
- `map-first-layout-conformance`
- `state-and-accessibility-conformance`
- `responsive-screenshot-evidence`
- `visual-regression-governance`

## Impact

- Frontend CSS/components/tests/evidence docs; must not add product data contracts.
- May update shared M11 shell, AppShell, flood/monitoring visual surfaces, and M12-M14 page baselines now that #173, #174, and #175 are complete.
- Evidence and governance docs become the review fixture for future visual PRs.

## Non-Goals

- Adding new product features.
- Adding backend endpoints.
- Claiming pixel-perfect conformance without screenshot evidence.
- Committing volatile screenshot binaries unless a repository policy explicitly requires them.
- Expanding mobile support below the documented desktop conformance floor; sub-1280 behavior may be documented as unsupported.
