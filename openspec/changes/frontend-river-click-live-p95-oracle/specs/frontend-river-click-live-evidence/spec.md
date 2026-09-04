## ADDED Requirements

### Requirement: The live river hook MUST be absent by default and select one actual rendered discharge feature

`M11MapLibreSurface` SHALL expose exactly
`window.__nhmsRiverClickEvidence.selectRenderedRiver(input)` only when the exact
pre-start boolean `window.__NHMS_E2E_HOOKS__ === true` is present. The global
SHALL register on the gated component mount without exposing a map ref or generic
query/control method. Without the flag it SHALL be absent and ordinary pointer,
hover, selection, popup, and request behavior SHALL remain unchanged.

`input` SHALL contain only
`bbox: [[minLon,minLat],[maxLon,maxLat]]`, `anchor: [lon,lat]`, `basinId`,
`riverSegmentId`, `basinVersionId`, and `riverNetworkVersionId`. Coordinates
SHALL be finite WGS84 values with ordered non-degenerate bounds. Identity values
SHALL be nonempty and bounded. The Promise-returning method SHALL wait at most
15,000 ms for the current map and a renderable `discharge` overlay, fit the bbox
with padding 48, duration 0, and maxZoom 14, project the anchor, and query only
the 16-by-16 CSS-pixel box around it in
`m11RegisteredOverlayHitLayerId(renderableOverlay)`. It SHALL reject more than 64
total results and require exactly one actual rendered feature whose `basin_id`,
`basin_version_id`, and `river_network_version_id` match. Segment identity SHALL
be `river_segment_id` falling back to `segment_id`; if both exist they SHALL be
equal and the normalized value SHALL match the pin.

Immediately before dispatch, the hook SHALL record browser `performance.now()`
and pass that same actual feature, product `layerId: "discharge"`, and finite
input-anchor `event.lngLat` through the existing `onOverlayClick` callback. It
SHALL resolve only the four normalized feature identities and `dispatchNowMs`.
It SHALL never synthesize/modify feature properties, fetch an API, expose a body
or credential, or offer arbitrary application mutation. Rejections SHALL use
only the closed hook codes `HOOK_INVALID_INPUT`, `HOOK_MAP_UNAVAILABLE`,
`HOOK_WRONG_LAYER`, `HOOK_MAP_TIMEOUT`, `HOOK_QUERY_FAILED`, `HOOK_QUERY_LIMIT`,
or `HOOK_FEATURE_MISMATCH`, with bounded redacted messages. Registration and
cleanup SHALL compare both object identity and a generation token so stale
cleanup cannot delete a newer hook.

#### Scenario: Ordinary users have no hook surface

- **WHEN** the map mounts without the exact boolean pre-start flag
- **THEN** `window.__nhmsRiverClickEvidence` is absent and existing pointer-driven river behavior is unchanged

#### Scenario: One current rendered river uses the product click path

- **WHEN** the gate is enabled and exactly one result from the bounded discharge hit-layer query matches the four live identities
- **THEN** the hook dispatches that unmodified rendered feature through `onOverlayClick` with product layer `discharge`, finite anchor, and t0 immediately before dispatch

#### Scenario: Invalid, stale, or ambiguous selection fails closed

- **WHEN** input is invalid, map/overlay readiness exceeds 15,000 ms, the query fails or exceeds 64 results, or zero/multiple/drifted features match
- **THEN** the Promise rejects with one closed hook code without selecting a first feature, synthesizing a panel, exposing the map, or leaving stale global ownership

### Requirement: The live lane MUST resolve current identity and time one warmup plus twenty complete dual-source clicks

The lane SHALL accept only these configuration values:
`PLAYWRIGHT_LIVE_BASE_URL`, `PLAYWRIGHT_LIVE_API_BASE_URL`,
`PLAYWRIGHT_LIVE_RIVER_BASIN_ID`, `PLAYWRIGHT_LIVE_RIVER_SEGMENT_ID`, and
`PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH`. Both URLs SHALL be bare HTTP(S)
origins with root pathname and no userinfo/query/fragment. Pins SHALL match the
existing M11 identifier grammar `[A-Za-z0-9._:-]{1,96}`. The receipt path SHALL
obey the publication requirement below. The parser SHALL reject any supplied
river run/model/basin-version/network-version/cycle/scenario override.

Before browser sampling, a bounded Node request owner SHALL issue, without
redirects or credentials:

1. exactly one current identity-only request for each of GFS and IFS at
   `/api/v1/mvp/qhh/latest-product`, with exactly `source`,
   `identity_only=true`, and `basin_id=<pin>`; and
2. one exact segment-detail request at
   `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}` with
   exactly `river_network_version_id=<current value>`.

Each SHALL be 2xx, request `Accept-Encoding: identity`, reject a non-identity
response encoding, stream no more than 262,144 UTF-8 bytes, and parse the
shipping `{status:"ok",data:{...}}` envelope within depth 12, object width 64,
array length 10,000, and total node count 50,000. Both products SHALL have
`status=ready`, `availability.ready !== false`, exact source and basin, valid
nonempty run/model/cycle/version identity, and the same current basin and river-
network versions; GFS and IFS MAY have different run/model/cycle identities. The
segment payload SHALL match the path/network pin. Its payload-root `geom` SHALL
pass `getM11SelectedSegmentGeometryBudgetStatus` (LineString/MultiLineString,
finite WGS84, at most 10,000 coordinates, at most three dimensions and 250,000
serialized bytes). Bbox SHALL cover all sanitized coordinates and anchor SHALL
be flattened-source coordinate index `floor((count - 1) / 2)`. No configured,
historical, basin-bbox, static, smaller-basin, or first-result fallback is legal.

The spec SHALL set the hook flag with `page.addInitScript` before navigating to
exactly `/`, leaving shipping defaults `source=best` and `layer=discharge` and no
identity query parameters. It SHALL run one complete discarded warmup followed
by exactly 20 serial accepted samples. For every attempt it SHALL arm response
observation before hook dispatch. Both t0 and t1 SHALL be browser
`performance.now()` values in that page; t0 is the hook's immediate pre-dispatch
value, while t1 is read only after:

- exactly one GFS and one IFS `forecast-series` response finish without network
  error and have HTTP status 200..299;
- `m11-river-panel-chart` is visible; and
- `m11-river-panel-partial` and `m11-river-panel-empty` are absent.

A matching series request SHALL be GET to the exact configured API origin and
exact decoded path for the preflight basin-version/segment. It SHALL have exactly
one value for each of `river_network_version_id`, `run_id`, `model_id`,
`issue_time`, `variables=q_down`, `scenarios`, and `include_analysis=false`.
Identity values SHALL equal the appropriate current source product and scenario
SHALL be `forecast_gfs_deterministic` or `forecast_ifs_deterministic`. Unknown or
duplicate query keys, `run_types`, another series identity, and extra/duplicate
series requests in the attempt window SHALL fail. Latest-product, segment detail,
runtime config, tiles, and unrelated read requests SHALL not count as series
requests. Series bodies and headers SHALL never be read or retained as evidence.

Each attempt SHALL reach t1 within 15,000 ms. One internal 360,000-ms deadline
SHALL begin before preflight and cover page load, warmup, all 20 samples, and
receipt construction; the Playwright test timeout SHALL be exactly 390,000 ms to
leave publication/cleanup margin. Workers SHALL be exactly 1 and retries exactly
0. After each t1, the response observer SHALL remain armed while the existing
`关闭面板` button scoped to `m11-river-forecast-panel` closes that panel, the panel
unmounts, and no series request appears for a 250-ms quiet interval. The same
`m11-map-surface` and hook object SHALL stay mounted. The observer SHALL then be
removed and a fresh one armed before the next dispatch.

P95 SHALL sort all 20 finite non-negative warm durations and select nearest-rank
index `ceil(0.95 * 20) - 1`, exactly index 18. PASS requires `p95_ms < 2000`;
2000 exactly fails. The terminal classifier SHALL be closed:

- absent required frontend/API URL or receipt-path env, unavailable supported
  POSIX Node/Playwright/browser runtime, or inability to install the pre-start
  flag is BLOCKED;
- missing/invalid pin, supplied malformed URL/path, a registered hook still
  absent after gated readiness, current product/detail/geometry absence or
  incompatibility, any hook rejection, warmup/sample source/request/network/
  chart/timing/identity error, per-attempt or whole-run timeout, skipped sample,
  internal unexpected error, or `p95_ms >= 2000` is FAIL; and
- Playwright `skip`, `fixme`, retry, discarded outlier, API-only timing, partial
  sample, or historical result SHALL never become PASS.

The closed BLOCKED failure codes SHALL be `REQUIRED_ENV_MISSING`,
`RUNTIME_UNAVAILABLE`, and `HOOK_PREREQUISITE_MISSING`. The closed FAIL codes
SHALL be `CONFIG_INVALID`, `PREFLIGHT_HTTP_ERROR`,
`PREFLIGHT_RESPONSE_INVALID`, `PRODUCT_UNAVAILABLE`, `IDENTITY_MISMATCH`,
`SEGMENT_GEOMETRY_INVALID`, `HOOK_SELECTION_FAILED`,
`SERIES_REQUEST_INVALID`, `SERIES_RESPONSE_ERROR`, `SAMPLE_TIMEOUT`,
`WHOLE_RUN_TIMEOUT`, `CHART_INCOMPLETE`, `TIMING_INVALID`, `IDENTITY_DRIFT`,
`THRESHOLD_EXCEEDED`, and `INTERNAL_ERROR`.

#### Scenario: Twenty complete warm clicks pass below threshold

- **WHEN** one complete warmup is discarded and all 20 serial attempts each observe exactly the two current matching 2xx series plus a complete chart, with nearest-rank P95 below 2000 ms
- **THEN** the lane returns PASS with every duration retained and stable source/feature identity

#### Scenario: Warmup, one source, or chart completion fails

- **WHEN** warmup or any accepted attempt has a missing/extra/unexpected/non-2xx series, network error, timeout, drift, invalid timing, or empty/partial chart
- **THEN** the lane returns FAIL at index 0 or 1..20 without dropping the attempt, substituting API latency, or computing a passing percentile

#### Scenario: Environment prerequisite is absent

- **WHEN** a required value/runtime or the pre-start hook prerequisite is absent
- **THEN** the lane reports BLOCKED and cannot reuse a mocked, local, skipped, retried, or historical PASS

#### Scenario: Threshold equality fails

- **WHEN** all 20 samples complete and nearest-rank index 18 is 2000 ms or greater
- **THEN** the lane returns `THRESHOLD_EXCEEDED` FAIL with the exact recomputed non-passing P95

### Requirement: River-click evidence MUST be schema-closed, private, current-run, and no-clobber

The Node-20-compatible POSIX owner SHALL be
`apps/frontend/playwright.river-click-evidence.ts`; application/browser modules
SHALL NOT import `node:fs`. It SHALL validate before writing one Draft 2020-12
schema-`1.0` document named `nhms-frontend-river-click-live-evidence`. The exact
top-level fields SHALL be:

`artifact`, `schema_version`, `status`, `generated_at`, `started_at`, `ended_at`,
`threshold_ms`, `percentile_method`, `warmup_count`, `accepted_count`, `origins`,
`requested_feature`, `rendered_feature`, `gfs`, `ifs`, `warmup`, `samples`,
`p95_ms`, and `failure`.

All objects SHALL reject additional properties. `status` SHALL be
`PASS|FAIL|BLOCKED`; threshold/method SHALL be `2000`/`nearest-rank`.
`warmup_count` SHALL equal actual completed warmup count 0..1;
`accepted_count` SHALL equal `samples.length` 0..20. Origins SHALL contain only
nullable normalized frontend/API origins. Nullable requested/rendered features,
when present, SHALL contain only basin, segment, basin-version, and network-
version identity. Nullable GFS/IFS products, when present, SHALL additionally
contain exact source, run, model, cycle, and fixed scenario identity. A completed
warmup/sample SHALL contain only its 0 or 1..20 index, finite non-negative
`duration_ms`, and integer GFS/IFS status 200..299; warmup additionally SHALL set
`discarded:true`. A non-null failure SHALL contain exactly closed `code`, `stage`,
nullable `sample_index` (0..20), nullable actual GFS/IFS HTTP status (100..599),
and a bounded redacted `message`. Stages SHALL be `config`, `runtime`,
`preflight`, `map`, `warmup`, `sample`, or `threshold`.

PASS SHALL require non-null origins/features/products, one complete discarded
warmup, exactly 20 consecutively indexed samples, stable identities, exact
recomputed finite `p95_ms < 2000`, and `failure=null`. BLOCKED SHALL require no
warmup/sample/P95 claim and a BLOCKED code. FAIL SHALL require a FAIL code and
normally `p95_ms=null`; only a fully completed `THRESHOLD_EXCEEDED` failure MAY
carry the exact recomputed `p95_ms >= 2000`. No non-PASS document may carry a
sub-threshold P95. UTC RFC3339 timestamps SHALL obey
`started_at <= ended_at == generated_at`.

The validator SHALL cap serialized evidence at 262,144 UTF-8 bytes, depth at 12,
object width at 64, array length at 64, every identity/origin at 256 bytes,
failure code at 64 bytes, and failure message at 512 bytes. It SHALL reject raw
URLs/query strings, URL userinfo, headers, bodies, geometry, map objects, DSNs,
tokens, signed URLs, unsupported fields, non-finite numbers, excessive
complexity, and arbitrary exception text. The checked-in schema and
`frontend_river_click_live_evidence.example.json` PASS,
`.error.example.json` FAIL, and `.partial.example.json` BLOCKED examples SHALL
all pass the repository schema-family validation loop and the same closed Node
semantic validator without a new schema runtime dependency.

`PLAYWRIGHT_LIVE_RIVER_CLICK_RECEIPT_PATH` SHALL be an absolute lexically and
canonically normalized path whose final basename matches
`nhms-frontend-river-click-live-evidence-[A-Za-z0-9._-]{1,96}.json`. Its existing
parent SHALL have no symlink component, be owned by `process.geteuid()`, have
mode exactly 0700, and retain the same `(dev,ino,uid,mode)` through publication;
the final path SHALL be absent. A non-POSIX runtime lacking these observations is
BLOCKED. The runbook SHALL create a new private run directory and unique absent
receipt path before each command; it SHALL never use an attacker/same-UID shared
directory as this Node filesystem trust boundary.

The publisher SHALL create one same-directory
`.<final>.tmp-<32-lowercase-hex>` with
`O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC|O_WRONLY`, request mode 0600, immediately
`fchmod(0600)`, and observe owner euid, exact mode 0600, regular type, and link
count 1. It SHALL write all validated bytes,
fsync, and recheck the descriptor. It SHALL exclusively commit with
`link(temp,final)`, never rename/replace. It SHALL fsync/revalidate the parent;
prove final and descriptor share `(dev,ino)`, owner, mode 0600 and link count 2;
unlink only the matching temp; fsync again; then prove final link count 1,
unchanged identity, exact bytes, valid schema, and semantic invariants. Cleanup
SHALL unlink a temp only if current `(dev,ino)` still matches this invocation.
Destination races SHALL preserve the winner. Any identity, write, fsync, link,
unlink, cleanup, or readback uncertainty SHALL fail nonzero and SHALL NOT delete,
overwrite, or reuse an older artifact.

When a safe receipt path is owned by the running test, it SHALL publish terminal
FAIL/BLOCKED before throwing; every non-PASS exits nonzero. A missing/unsafe path,
launcher/browser failure before test ownership, or publication failure SHALL
emit only a bounded redacted `BLOCKED:`/`FAIL:` diagnostic and no receipt; it
SHALL never accept an old file. #1895 SHALL bind PASS to an absent unique path,
private run directory, command time bracket, successful command, exact schema
readback, and mode 0600.

#### Scenario: Passing evidence is private and current

- **WHEN** a complete PASS is published to an absent unique basename in a canonical euid-owned mode-0700 run directory
- **THEN** exclusive link publication produces one mode-0600 schema-valid final file whose readback exactly reproduces this run's 20-sample P95

#### Scenario: Early failure remains honest

- **WHEN** a safe-path run becomes BLOCKED or FAIL before warmup or after only some accepted samples
- **THEN** the terminal receipt records only actual completed counts and nullable observed identities/statuses, exits nonzero, and cannot carry a passing P95

#### Scenario: Secret, malformed, or excessive evidence is rejected

- **WHEN** evidence has an unsupported/secret-bearing field, userinfo/raw URL, invalid identity/status/count/timestamp/P95, non-finite number, or exceeds any byte/depth/width/array limit
- **THEN** pre-write validation fails and no PASS artifact is published

#### Scenario: Existing, raced, or uncertain output is never clobbered

- **WHEN** the target exists, a symlink/non-private/foreign/changed parent or non-regular temp is observed, another creator wins the final link, or durable publication/readback is uncertain
- **THEN** the command fails while preserving existing/final bytes and removes only a temp still proven to belong to this invocation

### Requirement: Live and mocked browser evidence MUST remain distinct

The dedicated `live-river-click` Playwright profile SHALL run the river-click
receipt producer. The new test SHALL remain in `e2e/live-display.spec.ts` so the
existing exact live-spec matcher and broad `page.route('**/api/v1/**')` mock
prohibition cover it. The dedicated profile SHALL use one worker and zero
retries. The new metric SHALL visit only viewer-accessible `/`; the existing
`/monitoring` test SHALL remain unchanged on the two-URL `live-display`
profile, and this issue SHALL add no `/ops` visit or claim. A two-URL
monitoring-only invocation SHALL NOT run river-click `globalSetup` or infer a
`/` PASS. #1895 SHALL invoke `test:e2e:live-river-click`.

Focused Node publisher/unit tests MAY import the Node owner and write only their
private temporary fixtures. React/component tests and mocked browser specs SHALL
not import it or emit a live receipt. Screenshots, traces, local builds, mocked
responses, `/monitoring`, and `/ops` SHALL not satisfy or imply the `/` click
metric. Existing frontend `pnpm test`/build ownership, the static live no-mock
guard, and the root schema/example loop SHALL cover this change; frontend files
SHALL not be routed through backend `scripts/select_ci_tests.py`. Node-27 live
execution is deferred to #1895, which SHALL use the exact merged command and
current pins before accepting this capability.

#### Scenario: Mocked or sibling evidence cannot satisfy live readiness

- **WHEN** pure helpers, hook components, mocked browser tests, screenshots, `/monitoring`, or `/ops` pass
- **THEN** none emits or implies a live river-click PASS and #1895 remains blocked

#### Scenario: Live spec attempts a broad API mock

- **WHEN** `live-display.spec.ts` registers a broad `/api/v1/**` route mock
- **THEN** the existing static guard fails before browser execution and cannot produce PASS

#### Scenario: Runbook consumes exact merged evidence

- **WHEN** #1895 runs the merged live profile on node-27 with current pins and an absent private receipt path
- **THEN** it accepts the click gate only from the current command's schema-valid mode-0600 PASS with matching origins, feature/products, counts, nearest-rank method, and P95 below 2000 ms
