Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent (override: public map interaction, geospatial identity, live config, and evidence publication are mandatory expanded triggers)
Minimal mergeable slice: gated hook + local tests; live sampler/receipt; runbook integration, delivered serially in one dependency PR because each later slice consumes the prior contract

Change surface:

- `apps/frontend/src/components/map/M11MapLibreSurface.tsx` and focused frontend
  hook/component tests; the existing `OverviewPage` callback remains the sole
  product selection path.
- Live Playwright helpers/config/spec and pure frontend tests.
- `apps/frontend/playwright.river-click-evidence.ts`, one strict root JSON Schema,
  three examples, and filesystem publication tests.
- Existing frontend package checks, root schema validation, static live no-mock
  guard, and node-27/#1895 runbooks; no backend CI selector changes.

Seams under test:

- Browser global gate + mounted MapLibre owner -> absent hook or bounded actual
  rendered feature dispatched through the existing river callback.
- Explicit live config + current APIs/requests -> stable basin/segment/version and
  GFS/IFS identities or exact BLOCKED/FAIL classification.
- One complete warmup + 20 response/chart-complete samples -> nearest-rank P95
  PASS only below 2000 ms.
- Receipt model + filesystem path -> schema-valid mode-0600 exclusive artifact or
  no-clobber failure.
- Mocked/live profile selection -> local evidence can never emit live PASS.

## 1. Pure evidence and configuration contract

- [x] 1.1 Add pure parsers for `PLAYWRIGHT_LIVE_BASE_URL`,
  `PLAYWRIGHT_LIVE_API_BASE_URL`, `PLAYWRIGHT_LIVE_RIVER_BASIN_ID`,
  `PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID`, and the absolute
  `PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`. Reject missing/blank/userinfo/non-
  HTTP bare origins, relative/aliased receipt paths, pins outside
  `[A-Za-z0-9._:-]{1,96}`, and any configured run/model/version/cycle/scenario
  override before browser work. Fix workers=1, retries=0, warmup=1, accepted
  samples=20,
  threshold=2000 ms, per-map/sample deadline=15,000 ms, and whole-run deadline=
  360,000 ms.
- [x] 1.2 Implement browser-clock nearest-rank P95 and terminal
  PASS/FAIL/BLOCKED construction with the exact schema-1.0 top-level/nested
  fields, stable current feature/GFS/IFS identities, at most 21 actual completed
  sample objects, bounded redacted failures, and no response bodies, raw URLs,
  geometry, secrets,
  skipped samples, or sub-threshold non-PASS claim.
- [x] 1.3 Add
  `schemas/frontend_river_click_live_evidence.schema.json` and PASS/FAIL/BLOCKED
  examples, plus strict schema/example and closed Node-validator tests for
  additional fields, malformed status/count/identity/threshold/timestamps,
  depth>12, object width>64, arrays>64, non-finite values, values over their byte
  bounds, secret-shaped fields, and backward-independent version handling.
- [x] 1.4 Implement the Node-only owner
  `apps/frontend/playwright.river-click-evidence.ts`: same-directory no-follow
  mode-0600 temp in a canonical euid-owned mode-0700 parent, payload <=262,144
  bytes, validate-before-write, immediate `fchmod`, descriptor checks, link-first
  exclusive no-clobber claim, file/parent fsync and identity revalidation. Cover
  existing/raced target, temp collision, symlink/non-regular/foreign/changed
  parent, partial write/fsync/link/unlink failure, and identity-safe cleanup.

## 2. Gated product-path MapLibre hook

- [x] 2.1 Add exactly `window.__nhmsRiverClickEvidence.selectRenderedRiver(input)`
  only behind pre-start `window.__NHMS_E2E_HOOKS__ === true`. Accept only the
  frozen bbox/anchor and four identity fields; fit with padding 48/duration 0,
  wait at most 15,000 ms, query only
  `m11RegisteredOverlayHitLayerId(renderableOverlay)`, reject more than 64
  returned features, and return only the four identities plus
  `dispatchNowMs=performance.now()`.
- [x] 2.2 Require finite ordered non-degenerate WGS84 bbox/anchor and exactly one
  actual rendered feature matching basin/segment/basin-version/river-network-
  version. Pass that feature, product `layerId: "discharge"`, and finite
  event-shaped `lngLat` through the existing `onOverlayClick`; never synthesize
  properties, expose a map/query API, or use the hit-layer ID as product layer.
- [x] 2.3 Add component/interaction tests for exact flag absent/present, ordinary
  click compatibility, input and output closure, fit/query arguments, query
  ceiling/readiness timeout, zero/ambiguous/drifted features, exact callback
  identity/anchor/t0 ordering, unmount cleanup, and generation-token/object-
  identity protection against stale cleanup deleting a newer hook.

## 3. No-mock live P95 lane

- [x] 3.1 Resolve current identity only from both identity-only GFS/IFS
  `latest-product` endpoints and the exact basin-version/segment detail endpoint.
  Require 2xx ready products for the pin, compatible current basin/network
  versions, exact segment identity, and bounded finite `geom` framing; reject
  configured/historical identity and first/smaller fallback.
- [x] 3.2 Extend `live-display.spec.ts` to visit `/`, install the gate before app
  startup, call the exact hook on one rendered feature, require one complete
  discarded warmup, then collect exactly 20 serial samples. Per sample, arm
  response observation before dispatch, use browser `performance.now()` for both
  t0/t1, match exactly one 2xx source-specific forecast-series URL/query for GFS
  and IFS, require chart visible with partial/empty absent, and close/unmount only
  the panel while preserving the map before re-arming the next sample.
- [x] 3.3 Enforce the exact classification table: absent required frontend/API
  URL or receipt-path env, unsupported POSIX runtime, or inability to install the
  pre-start flag is BLOCKED; missing/invalid pin, supplied invalid URL/path,
  missing registered hook after gated readiness, missing/non-2xx/incompatible
  current product or geometry, invalid/ambiguous/absent rendered feature, warmup
  failure, duplicate/missing/unexpected/non-2xx series, per-sample/whole-run
  timeout, identity drift, invalid timing, partial/empty chart, skipped sample,
  or P95 >=2000 ms is FAIL. Once a safe receipt path exists, publish terminal
  FAIL/BLOCKED before rethrowing; unsafe/missing path writes no file, and
  publication failure is terminal failure.
- [x] 3.4 Keep latest-product/detail/runtime/tile reads outside forecast duplicate
  counting; never inspect response bodies for evidence. Preserve the existing
  `/monitoring` live test unchanged on the two-URL `live-display` profile, add
  no `/ops` assertion or inferred PASS, retain the broad-API-mock prohibition,
  prevent Vitest/component/mocked Playwright from importing the publisher or
  writing a live receipt, and keep #1895 on `test:e2e:live-river-click`.

## 4. Operator and CI integration

- [x] 4.1 Update `docs/runbooks/node-27-bringup-checklist.md` and the #1895
  section of `docs/runbooks/tier-node27-timeseries-storage.md` with the exact five
  env keys, merged command, absent absolute path/mode/schema/current-run binder,
  current pin provenance, threshold, and FAIL/BLOCKED/publication stop behavior;
  do not claim live PASS in this implementation PR.
- [x] 4.2 Wire focused tests into existing frontend `pnpm test`/build ownership,
  add the narrow Node/Playwright typecheck to frontend CI with a frozen lockfile,
  include the live spec in the existing static no-mock guard, and rely on the
  existing root schema/example validation loop. Do not route frontend sources or
  tests through backend `scripts/select_ci_tests.py`.

## 5. Verification

- [x] 5.1 Capture one batched pre-implementation red run for new pure/hook/
  publisher/static-live tests without stash, then run focused Vitest/component,
  Node publisher, and schema tests green with no weakened oracle.
- [x] 5.2 Run `cd apps/frontend && corepack pnpm test`, `corepack pnpm build`, and
  the live profile without required env to prove stable BLOCKED with no mocked or
  historical PASS and no artifact when receipt path is absent.
- [x] 5.3 Run strict OpenSpec, changed-file lint/type checks, the root schema/
  example loop, Markdown lint, and `git diff --check`; record no live node-27
  claim.

Risk-pack evidence mapping:

- Public entry/config/auth: 1.1, 2.1, 3.1, 3.4 -> exact gate, URLs/pins/path,
  closed interfaces, and no control mutation or secret-bearing input.
- File IO/schema/error handling/resources: 1.2-1.4, 3.3 -> fixed limits/status
  invariants and exclusive publication under races/failures.
- Concurrency/legacy compatibility: 2.3, 3.2-3.4 -> hook ownership, listener
  ordering, panel-only remount, ordinary click and existing live/mock lanes.
- Geospatial/display/provider identity: 2.2-2.3, 3.1-3.3 -> WGS84 framing,
  actual rendered feature and stable current GFS/IFS identities.
- Documentation/release: 4.1-4.2, 5.1-5.3 -> exact operator command and frontend/
  schema CI closure without a fake live receipt.

Non-goals:

- No backend/OpenAPI/database/MVT-generation change, basin bbox endpoint, station
  click metric, public route or chart contract change, new `/ops` evidence,
  backend test-selector ownership, node-22/Slurm work, or node-27 live execution
  in this implementation PR.
