## Why

Four remaining display follow-ups expose unreachable operational content, obscured attribution, a run-independent precipitation catalog omission, and a stale lineage delivery promise. The user explicitly requests one PR; #2127/#2139/#2140 are already closed and are not reimplemented.

## What Changes

- #2023: give all three non-map operational routes an internal wheel-scroll surface, preserving a non-scrolling map shell and fitting the map into short viewports.
- #2128: reserve the bottom attribution band by moving the 64px control bar to 40px above the map bottom and moving adjacent legend/notices coherently; measure actual rectangles.
- #2142: return the existing precip catalog entry when no display-ready run exists, without fabricating a run or touching explicit run errors.
- #2039: document that display lineage APIs/UI are not implemented; retain model-asset provenance and historical records.

## Capabilities

### New Capabilities
- `run-independent-display-catalog`: precipitation catalog remains discoverable without a hydrological run.

### Modified Capabilities
- `map-first-layout-conformance`: internal operational scrolling, short-map fit, attribution exclusion band and aligned overlay offsets.
- `api-contract-convergence`: current docs distinguish retired/unimplemented display lineage proposals from delivered endpoints.

## Impact

Frontend shell/pages, map overlay geometry and mocked browser regression; API list_layers and precipitation catalog regression; current API design documentation. No database migration, dependency, RBAC, generated API shape or production deployment change.

## Risk triage

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: shared layout and public API)
Blast radius: all operational routes, national map controls, public layer discovery
Selected risk packs: Public API / CLI / script entry; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes; Published NHMS artifacts / display identity
Evidence floor: node-27 red/green wheel and rectangle regressions + catalog route regressions; local frontend tests/typecheck/build/API types + ruff + strict OpenSpec validation; node-27 isolated live browser smoke.

## Deviation

User-authorized single PR for four issues instead of one issue per workflow. #2039 follows its latest comment: frontend deletion already merged; only remaining current documentation/contract claims are in scope.
