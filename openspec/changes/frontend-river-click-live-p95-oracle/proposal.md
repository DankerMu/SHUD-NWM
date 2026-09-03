## Why

Issues #1895 and #1342 require a public `/` river-click GFS+IFS curve P95 below
2 seconds, but the shipping live-display lane visits only `/monitoring` and the
MapLibre canvas exposes no deterministic way to select a current rendered river.
The gate is therefore BLOCKED today and cannot be replaced by API latency or a
mocked browser test.

## What Changes

- Add a default-off, read-only browser-test hook that can fit to a validated live
  river geometry, find the matching currently rendered MapLibre feature, and
  dispatch that feature through the existing river `onOverlayClick` path.
- Extend the no-mock live-display Playwright lane to resolve a caller-supplied
  basin/segment pin from current live APIs, discard one warmup, collect exactly
  20 warm dual-source click-to-chart samples, and enforce nearest-rank P95
  strictly below 2000 ms.
- Publish a bounded, mode-0600, no-clobber schema-1.0 JSON receipt containing
  only normalized origins, current feature/run identities, per-sample timings,
  and PASS/FAIL/BLOCKED status; never response bodies, credentials, or URL
  userinfo.
- Update both node-27 live-display and timeseries-tiering runbooks with the exact
  command, required environment, artifact contract, and failure semantics.

## Capabilities

### New Capabilities

- `frontend-river-click-live-evidence`: Deterministic live MapLibre river
  selection, dual-source click-to-chart P95 measurement, and safe browser
  evidence publication.

### Modified Capabilities

- None.

## Impact

- Frontend map surface and overview click integration under an inert test-only
  gate; normal users and routes remain unchanged.
- Live Playwright config/helpers/specs, focused Vitest/component tests, one
  versioned evidence schema family, existing frontend/schema CI ownership, and
  two runbooks; no backend test-selector changes.
- No backend/OpenAPI/database change, no hard-coded run identity, and no node-22
  or Slurm work. Node-27 remains the only live oracle after merge.
