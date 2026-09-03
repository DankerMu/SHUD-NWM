## Context

The active display route is `/`, rendered by `OverviewPage` and
`M11MapLibreSurface`. A river curve opens only after a MapLibre feature reaches
the existing `onOverlayClick` handler. URL `segmentId` selects data/highlight
state but does not open the curve; the WebGL canvas has no DOM feature handle.
The current live-display Playwright spec visits `/monitoring` only. Issue #389
documented the missing framing/hit-test automation but shipped no hook.

Issue #1970 is a prerequisite for #1895/#1342, not their live execution. It must
add a deterministic read-only browser seam while preserving ordinary map
behavior and the no-mock distinction between local regression and node-27 live
evidence.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.
Upstream suggested level: absent; expanded is mandatory because this changes a
public map interaction seam, geospatial feature identity, configuration, and a
published evidence format.

## Goals / Non-Goals

**Goals:**

- Select one caller-supplied basin/river pin from current live identity and an
  actual currently rendered feature, then use the existing product click path.
- Measure the complete user wait: click dispatch through two valid GFS/IFS
  `forecast-series` responses and a non-partial visible chart.
- Emit reproducible, bounded, secret-free, no-clobber live evidence and fail
  closed when any prerequisite or identity drifts.

**Non-Goals:**

- No backend/OpenAPI/bbox endpoint, database, MVT-generation, route, popup data,
  chart, auth, or normal click behavior change.
- No station-popup performance gate, pixel-coordinate guessing, broad API mock,
  hard-coded run/model/version identity, or local/mock claim of live PASS.
- This PR does not execute node-27 or close the #1895 live gate; #1895 consumes
  the merged command and produces the live receipt.

## Decisions

### D1 — Gate one read-only product-path hook; do not create a second selector

`M11MapLibreSurface` exposes exactly one global,
`window.__nhmsRiverClickEvidence`, only when the page has set the exact boolean
`window.__NHMS_E2E_HOOKS__ === true` before application startup. The global has
one method, `selectRenderedRiver(input)`, where `input` is:

```text
{
  bbox: [[minLon, minLat], [maxLon, maxLat]],
  anchor: [lon, lat],
  basinId, riverSegmentId, basinVersionId, riverNetworkVersionId
}
```

All coordinates are finite WGS84 values (`lon` in `[-180,180]`, `lat` in
`[-90,90]`), bounds are ordered and non-degenerate, and every identity is a
nonempty value of at most 256 UTF-8 bytes. The method returns a Promise. It
requires the current renderable overlay to have product `layerId="discharge"`,
calls the underlying MapLibre map's
`fitBounds(input.bbox, {padding: 48, duration: 0, maxZoom: 14})`, and waits at
most 15,000 ms for the post-fit loaded/idle render. It projects `input.anchor`,
queries the 16-by-16 CSS-pixel box centered there using only
`m11RegisteredOverlayHitLayerId(renderableOverlay)`, refuses more than 64 total
results, and requires exactly one whose `basin_id`, `basin_version_id`, and
`river_network_version_id` properties equal the input. Segment identity is
`river_segment_id` with `segment_id` as the legacy fallback; when both are
present they must be equal, and the normalized value must equal the input. It
does not synthesize or rewrite feature properties.

Immediately before dispatch, the method records browser
`performance.now()` and invokes the existing `onOverlayClick` with the matched
rendered feature, product `layerId: "discharge"`, and an event-shaped object
whose `lngLat.lng/lat` are the finite input anchor. This is the exact shape
`OverviewPage.handleMapOverlayClick` already consumes; the MapLibre hit-layer ID
is used only for querying and is never passed as the product layer ID. The
Promise resolves only to the identities re-read from that feature plus t0:
`{basinId, riverSegmentId, basinVersionId, riverNetworkVersionId,
dispatchNowMs}`. It rejects with one closed code:
`HOOK_INVALID_INPUT`, `HOOK_MAP_UNAVAILABLE`, `HOOK_WRONG_LAYER`,
`HOOK_MAP_TIMEOUT`, `HOOK_QUERY_FAILED`, `HOOK_QUERY_LIMIT`, or
`HOOK_FEATURE_MISMATCH`; messages are bounded and contain no raw object or URL.

The flag is a test seam, not authorization: the hook may only fit/query the
current map and dispatch the existing callback. It exposes no map ref, generic
query method, API body, credential, or mutation surface. Each mounted owner gets
a monotonically scoped generation token; effect cleanup deletes the global only
when both object identity and token still match, so stale cleanup cannot delete
a newer instance. With the flag absent, no hook global exists and existing
mouse/touch dispatch is unchanged.

The live test obtains current GFS/IFS product identity and requested segment
geometry through existing read-only APIs. Each preflight response must be 2xx and have absent or `identity`
`content-encoding`; the requester sends `Accept-Encoding: identity`. A declared
`content-length`, when present, must not exceed 262,144 bytes, and a streaming
reader aborts before retaining byte 262,145 even when the length is absent or
false. The bounded bytes must be valid UTF-8 and decode as the shipping
`{status:"ok",data:{...}}` envelope within JSON depth 12/object width 64/array
length 10,000. It applies the existing
`getM11SelectedSegmentGeometryBudgetStatus` contract (at most 10,000 coordinates,
three dimensions, and 250,000 serialized geometry bytes) to payload-root
`data.geom`. Bbox uses all sanitized lon/lat coordinates; anchor is the coordinate
at `floor((coordinateCount - 1) / 2)` in flattened source order, not an arbitrary
bbox center. Empty, duplicate, wrong-version, unrendered, over-ceiling,
invalid-geometry, or out-of-bounds results fail; there is no basin bbox, static
fallback, smaller-basin, or first-feature fallback.

Rejected alternatives: canvas pixel guessing is nondeterministic; URL-only
panel opening bypasses click semantics; a permanently exposed debug global
changes the production surface; synthesizing feature properties can pass when
the MVT is stale or missing.

### D2 — Resolve current identity, then time the complete dual-source result

The only configurable live values are existing `PLAYWRIGHT_LIVE_BASE_URL` and
`PLAYWRIGHT_LIVE_API_BASE_URL`, plus
`PLAYWRIGHT_LIVE_RIVER_BASIN_ID`,
`PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID`, and the absolute
`PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`. Both URL values must be bare HTTP(S)
origins (only pathname `/`, no userinfo/query/fragment); the receipt stores their
normalized `.origin` values. Pin values match the shipping M11 identifier grammar
`[A-Za-z0-9._:-]{1,96}`. The parser has no run/model/version/cycle/scenario input
and rejects
`PLAYWRIGHT_LIVE_RIVER_RUN_ID`, `_MODEL_ID`, `_BASIN_VERSION_ID`,
`_RIVER_NETWORK_VERSION_ID`, `_CYCLE_TIME`, or `_SCENARIO` if any is present.
An absent required frontend/API URL or receipt path, unavailable supported
POSIX Node/Playwright/browser runtime, or inability to install the pre-start hook
flag is `BLOCKED`. A missing/invalid pin, a supplied malformed URL/path, or a hook
object still absent after a flag-enabled page reaches the bounded map deadline is
`FAIL`. Once the publisher has validated a safe receipt path, any terminal
condition owned by the test is published before a nonzero exit; launcher
failures before test ownership can only emit a bounded diagnostic and no file.

Preflight uses only current 2xx responses from:

1. `GET /api/v1/mvp/qhh/latest-product?source=GFS&identity_only=true&basin_id=<pin>`
   and the equivalent `source=IFS`; and
2. `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}`
   with query `river_network_version_id=<current value>`.

The two products may have different `run_id`, `model_id`, and `cycle_time`, but
must both report the pinned `basin_id`, `status=ready`, and the same current
`basin_version_id` and `river_network_version_id`. The detail response must
report the exact `river_segment_id` and `river_network_version_id`; bbox/anchor
come only from its finite bounded `geom`. Missing/non-2xx/current-incompatible
product or segment data is `FAIL`, never `BLOCKED` and never a historical or
first-result fallback.

Each sample registers response observers before selection. Both `t0` and `t1`
are browser `performance.now()` values in the same page clock domain. `t0` is
the hook's `dispatchNowMs` immediately before the actual rendered feature enters
the callback. Playwright observes responses only to classify identity/status;
it evaluates `performance.now()` in the browser for `t1`, after exactly one
matching GFS and one matching IFS `forecast-series` response have returned 2xx
and `m11-river-panel-chart` is visible while
`m11-river-panel-partial` and `m11-river-panel-empty` are absent.

A matching forecast request has API origin exactly equal to the configured API
origin, method GET, decoded path
`/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`,
and exactly one value for each required query key. Values equal the preflight
product for `run_id`, `model_id`, normalized `issue_time`, and
`river_network_version_id`; fixed values are `variables=q_down`,
`include_analysis=false`, plus source-specific
`scenarios=forecast_gfs_deterministic` or `forecast_ifs_deterministic`.
`run_types` and unknown/duplicate query keys are forbidden. Latest-product,
segment-detail, runtime-config, tile, and unrelated read requests are not series
duplicates. Any forecast-series request in the sample window that is repeated,
for a different identity, or otherwise unexpected is `FAIL`. A 2xx response
must also complete `response.finished()` without network error. Series response
bodies and headers are never read or retained for receipt evidence.

The spec visits exactly `/` with its shipping default `source=best` and
`layer=discharge`; it does not place basin, segment, cycle, or version identity in
the page URL. One initial sample must itself complete both sources and the full
chart within 15,000 ms; it is recorded as warmup and discarded from the
percentile. Failure of warmup is `FAIL`. Then exactly 20 samples run serially,
each within 15,000 ms, under one internal 360,000-ms whole-run deadline (the
Playwright test timeout may add only cleanup/publication margin), with workers
fixed to 1 and retries fixed to 0. Between samples the test scopes the existing
`关闭面板` button to `m11-river-forecast-panel`, clicks it, and proves only that
panel unmounted while the same `m11-map-surface` and hook object remain mounted.
It keeps the response listener through panel cleanup, rejects every observed
unexpected series request, removes the listener, then re-arms a fresh listener
before the next dispatch. Hover prefetch and latest-product requests do not
define t0/t1.

All warm samples must retain the same source-specific current identities. The
accepted set is exactly 20 finite non-negative durations. P95 is nearest-rank:
sort ascending and select `ceil(0.95*N)-1` (index 18 for N=20). PASS requires
`p95_ms < 2000`; equality fails. Any missing/extra/non-2xx series, timeout,
identity drift, invalid duration, empty/partial chart, skipped sample, or
threshold breach is `FAIL`. `BLOCKED` is restricted to the local prerequisites
listed above; neither status can be converted to PASS by skipping a test.

### D3 — Publish one strict, exclusive live receipt

The Node-20-compatible POSIX owner is
`apps/frontend/playwright.river-click-evidence.ts`; React/browser code never
imports `node:fs`. The caller supplies
`PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`, an absolute lexically normalized JSON
path. Its final basename must match
`nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}.json`; `.`/`..`,
empty components, and aliases are rejected. The publisher caps serialized output
at 262,144 bytes, JSON depth at 12, object width at 64 keys, array length at 64,
every identity/origin at 256 UTF-8 bytes, and failure code/message at 64/512
bytes. At most 21 sample objects exist (one warmup plus 20 accepted).

Node 20 has no path-relative `openat` API. The explicit trust boundary is
therefore the receipt's already-existing canonical parent: it must have no
symlink path component, resolve byte-for-byte to the requested parent, be a
private directory owned by `process.geteuid()` with mode exactly 0700, and keep
one unchanged `(dev,ino,uid,mode)` identity across every preflight, link, and
post-publication recheck. This is a no-clobber evidence directory, not an
attacker-writable interchange directory; violating that premise is BLOCKED.

Publication rejects any existing final path. It opens one random same-directory
basename matching `.<final>.tmp-<32 lowercase hex>` with
`O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC|O_WRONLY`, requests and immediately
`fchmod`s mode 0600, and then requires `fstat` regular-file, link-count 1,
current euid, and exact mode 0600.
It writes all bounded UTF-8 JSON bytes through the descriptor, fsyncs, and
rechecks the descriptor. It calls `link(temp, final)` as the exclusive commit;
`EEXIST` and every other uncertainty fail without touching the winner. It opens
and fsyncs the parent directory, proves final `lstat` is the same regular
`(dev,ino)` as the descriptor with mode 0600/owner euid/link-count 2, unlinks the
temp, fsyncs the parent again, and proves final link-count 1 and unchanged
identity. Success returns only after this terminal readback parses the exact
published bytes and revalidates schema/invariants. Cleanup unlinks a temp only
when its current `lstat(dev,ino)` still matches this invocation's descriptor;
otherwise it preserves the uncertain object and fails. A destination race
preserves the winner. Publication failure always fails the Playwright test and
never deletes, replaces, or reuses an older receipt. The #1895 runbook supplies
a unique absent final path inside a freshly created private mode-0700 run
directory and brackets the command; this is the current-run binding.

Checked-in `schemas/frontend_river_click_live_evidence.schema.json` plus
`schemas/examples/frontend_river_click_live_evidence.example.json` (PASS),
the `.error.example.json` sibling (FAIL), and the `.partial.example.json` sibling
(BLOCKED) use Draft 2020-12. Those suffixes deliberately use the repository's
existing schema-family loop, so all three are independently checked rather than
warning as unmatched examples. Before any write, the Node owner also runs a
closed-field semantic validator implementing the same required/type/status/
count/identity/finite-number bounds; no new runtime schema package is added.

Artifact name is `nhms-frontend-river-click-live-evidence`; schema version is
`1.0`. The exact top-level fields are:

```text
artifact, schema_version, status, generated_at, started_at, ended_at,
threshold_ms, percentile_method, warmup_count, accepted_count,
origins, requested_feature, rendered_feature, gfs, ifs,
warmup, samples, p95_ms, failure
```

`threshold_ms=2000` and `percentile_method="nearest-rank"` are constants.
`warmup_count` is the actual 0 or 1 completed warmup; `accepted_count` equals
`samples.length` and is the actual 0..20 completed warm samples. `origins`
contains exactly nullable normalized `frontend` and `api` origins. Requested and
rendered feature values are nullable until observed; a non-null feature object
contains only `basin_id`, `river_segment_id`, `basin_version_id`, and
`river_network_version_id`. `gfs`/`ifs` are nullable until preflight and otherwise
contain only `source_id`, `basin_id`, `basin_version_id`,
`river_network_version_id`, `run_id`, `model_id`, `cycle_time`, and `scenario`.
Each completed sample contains only 1-based `index`, finite non-negative
`duration_ms`, and actual integer `gfs_status`/`ifs_status` in 200..299. `warmup`
is null or the same shape with `index:0` and `discarded:true`. Failure is null or
exactly `{code,stage,sample_index,gfs_status,ifs_status,message}`; nullable status
and sample-index fields retain the failed boundary without inventing a complete
sample.

Raw URLs/query strings, headers, bodies, geometry, map objects, DSNs, tokens,
signed URLs, URL userinfo, unsupported fields, deep/wide/oversized values,
non-finite numbers, and arbitrary exception text are forbidden. PASS requires
non-null origins/features/source identities, `warmup_count=1`, one complete
discarded warmup, exactly 20 indexed samples, stable identities, finite
`p95_ms` exactly recomputed from all durations and strictly below 2000, and
`failure=null`. BLOCKED requires `p95_ms=null`, `failure!=null`, and no sample
claims. FAIL requires `failure!=null`; normally `p95_ms=null`, but when all 20
samples completed and `failure.code="THRESHOLD_EXCEEDED"`, it carries the exact
recomputed `p95_ms >= 2000`. No non-PASS document can carry a sub-threshold P95.
RFC3339 UTC timestamps must be ordered `started_at <= ended_at == generated_at`.

Classification is closed. An absent required frontend/API URL or receipt-path
environment value, unsupported POSIX runtime, or failed pre-start-flag
installation before page work is BLOCKED; a missing/unsafe receipt path writes
no file. Missing or invalid basin/segment pins, supplied invalid URL/path,
unavailable/non-current product or geometry, and a gate-enabled page that lacks
the registered hook after its bounded readiness wait are FAIL. A harness that
failed to install the pre-start boolean is BLOCKED. Every hook rejection,
rendered-feature miss/ambiguity, incomplete warmup/sample, unexpected/duplicate/
non-2xx series, network error, deadline, chart partial/empty state, identity
change, invalid timing, or threshold breach is FAIL. No Playwright skip/fixme or
retry maps to any receipt status.

The Playwright test owns one terminal `try/catch/finally`: after config supplies
a safe receipt path, it builds and publishes PASS or the classified FAIL/BLOCKED
receipt before throwing so the process exits nonzero for every non-PASS. If the
receipt path itself is missing or unsafe, preflight emits one bounded redacted
`BLOCKED:` diagnostic to stderr and writes no file. If terminal publication
fails, that publication error is the test failure and no historical artifact is
accepted.

Rejected alternative: Playwright screenshots/traces may supplement but cannot
replace the machine receipt. Direct overwrite can leave stale or clobbered PASS
evidence; a generic test-results path does not bind the operator's current run.

### D4 — Keep live and mocked lanes distinct

The existing `live-display` profile remains the only browser oracle. For this
metric it visits the viewer-accessible `/` route only and retains the static
prohibition on broad `page.route('**/api/v1/**')` mocks. The existing
`/monitoring` test remains unchanged and separate. This issue does not add an
`/ops` visit: `/ops` is operator-RBAC protected, and neither `/monitoring` nor
`/ops` PASS may be inferred from the `/` receipt. The live config sets workers
exactly 1 and retries exactly 0 for this serial metric.

The new pure timing/config/receipt logic has local Vitest and component
coverage; mocked Playwright may test UI compatibility but never imports the Node
publisher or emits a live receipt. Node-27 runs the exact merged command and
owns the only PASS accepted by #1895.

## Risks / Trade-offs

- [Hook accidentally becomes a user API] -> exact boolean gate, read-only
  methods, default-absence and unmount tests, no auth/data exposure.
- [Feature pin passes against stale/synthetic data] -> resolve current API
  identity, query an actual rendered feature, require exact basin/segment/version
  equality, no fallback.
- [Network listeners count stale or duplicate requests] -> arm per sample before
  dispatch, require exact scenarios/identity and exactly two accepted responses,
  close/unmount between samples.
- [Cache makes the metric meaningless] -> discard one explicit warmup, document
  warm P95, remount for every accepted click, retain all durations.
- [Receipt leaks or clobbers evidence] -> normalized origins only, bounded fields,
  schema validation before exclusive mode-0600 publication, no response bodies.
- [Live test flakes forever] -> fixed per-stage and whole-run timeouts, one worker,
  no retries that discard samples, failures remain FAIL/BLOCKED evidence.

## Risk Packs

Core packs considered:

- Public API / CLI / script entry: selected — window hook and live command are
  callable boundaries.
- Config / project setup: selected — live URLs, basin/segment pin, receipt path,
  and one-worker profile must fail closed.
- File IO / path safety / overwrite: selected — mode-0600 no-clobber receipt.
- Schema / columns / units / field names: selected — versioned receipt and MVT/
  request identity fields.
- Auth / permissions / secrets: selected — production browser and evidence must
  expose no credentials or response bodies.
- Concurrency / shared state / ordering: selected — response listeners, hook
  lifecycle, panel remount, and t0/t1 ordering.
- Resource limits / large input / discovery: selected — bounded feature query,
  response metadata, samples, timeouts, and artifact bytes.
- Legacy compatibility / examples: selected — default users, existing map click,
  monitoring live evidence, and mocked tests remain unchanged.
- Error handling / rollback / partial outputs: selected — FAIL/BLOCKED and
  publication failures cannot leave a false PASS.
- Release / packaging / dependency compatibility: not selected — no new package
  or build target; existing React/MapLibre/Playwright/Node APIs suffice.
- Documentation / migration notes: selected — two runbooks become the operator
  contract consumed by #1895.

Domain packs considered:

- Geospatial / CRS / basin geometry: selected — live WGS84 bbox, rendered MVT
  feature, and exact river identity.
- Hydro-met time series / forcing windows: selected — current GFS/IFS cycle and
  q_down dual-series completion define t1.
- SHUD numerical runtime / conservation / NaN: not selected — no model compute
  or numerical values change.
- PostGIS / TimescaleDB domain behavior: not selected — existing read APIs/MVT
  are consumed without database changes.
- Slurm production lifecycle / mock-vs-real parity: not selected — no node-22 or
  scheduler surface.
- External hydro-met providers / snapshot reproducibility: selected — both GFS
  and IFS identities must remain stable for every sample.
- Run manifest / QC provenance: not selected — existing run IDs are observed but
  manifests/QC are unchanged.
- Published NHMS artifacts / display identity: selected — the receipt binds the
  feature and dual-source run identities actually displayed.

## Invariant Matrix

- Governing invariant: PASS exists only when one current rendered river feature
  traverses the normal click path for 20 complete warm GFS+IFS chart loads and
  nearest-rank P95 is below 2000 ms; otherwise the lane is FAIL/BLOCKED and
  cannot publish or preserve a false PASS.
- Source of truth: explicit live origins and basin/segment pin; current live API
  product/segment identities; actual MapLibre feature properties; observed
  forecast-series request identities; schema version 1.0.
- Producers: gated `M11MapLibreSurface` hook; live Playwright sample collector;
  strict receipt builder/publisher.
- Validators/preflight: URL/config/pin/path checks, WGS84 bbox and feature
  identity guards, response classifier, exact-source/stable-identity checks,
  percentile and receipt-schema validators.
- Storage/cache/query: one warmup plus 20 bounded samples; latest-product cache
  may warm but every sample issues fresh GFS/IFS forecast-series requests;
  exclusive JSON artifact only.
- Public routes/entrypoints: `/` live map, existing readonly APIs, `pnpm
  test:e2e:live-display`; hook absent unless explicitly enabled by the test.
- Frontend/downstream consumers: ordinary map pointer click, river panel,
  unchanged `/monitoring` live evidence, no `/ops` claim, and #1895/#1342
  runbooks.
- Failure paths/rollback/stale state: unmount removes hook; each failed sample
  closes panel/listeners; bounded timeout; temp cleanup; existing receipt never
  overwritten; no application or backend mutation to roll back.
- Evidence/audit/readiness: local unit/component/build checks plus node-27
  no-mock schema-valid mode-0600 receipt; mocked/local success is never live PASS.

Regression rows:

- Gate absent + ordinary map click -> no hook global and existing selection/
  dual-series behavior unchanged.
- Gate enabled + current exact rendered feature + 1 warmup/20 complete samples ->
  two stable source identities per sample, nearest-rank P95, PASS only below
  2000 ms, mode-0600 receipt.
- Missing/wrong/duplicate feature, source, response, identity, path, or threshold
  input -> stable FAIL/BLOCKED, no skipped sample and no false PASS.
- Existing/symlink/oversized receipt lane or publication interruption -> no
  clobber of prior evidence and test failure.
- Existing monitoring/no-mock guards and mocked regression -> unchanged; neither
  can claim the new live gate.

Boundary-surface checklist:

- Shared helper roots: Playwright config/evidence helpers only; no backend shared
  helper.
- Public entrypoints: exact gated window hook and `test:e2e:live-display`.
- Read surfaces: current live product/segment/MVT feature/forecast response
  metadata, all bounded.
- Write/overwrite surfaces: one explicit receipt and private temp; no-clobber.
- Producer/consumer evidence boundary: hook -> live spec -> schema validator ->
  receipt -> #1895 runbook.
- Stale/idempotency boundaries: hook cleanup ownership, per-sample listener
  teardown, source identity stability, unique artifact path.
- Unchanged consumers: normal map users, API contracts, charts, station popup,
  mocked tests, node-22.

## Migration Plan

1. Add pure config/timing/receipt helpers and schema tests.
2. Add the inert hook plus component/default-absence/lifecycle tests.
3. Extend live-display with current identity resolution, 1+20 sample collection,
   and terminal evidence publication.
4. Update both runbooks, existing frontend checks, live no-mock guard, and root
   schema/example validation ownership in the same PR; do not add backend test
   selector rules.
5. Merge and deploy; #1895 supplies current pins and captures node-27 live PASS.

Rollback is a frontend code revert: removing the gated hook and live test leaves
ordinary product behavior unchanged. Receipts are immutable evidence and are not
rewritten or deleted by rollback.

## Open Questions

None. The runbook must provide live basin/segment IDs; run/model/version/cycle
identity is always observed from the current live APIs and requests rather than
configured or copied from historical evidence.
