## Context

- **Governing invariant.** A river-click PASS receipt is produced only when every accepted sample's t0 is the timestamp of a trusted browser pointer event on the map canvas at the located river's point, and the product's own MapLibre click path then loaded exactly the located segment's GFS and IFS series. No code path in the shipped bundle can inject a feature into the product click handler.
- **Sibling surfaces.**
  - Hook producer: `hook.ts`, and its wiring in `M11MapLibreSurface.tsx:200-260`.
  - Lane: attempt, lane and terminal modules.
  - Evidence: the validator and publisher in `playwright.river-click-evidence*.ts`, the schema with its examples, and the binder `scripts/river-click-receipt-binder.mjs`.
  - Live-spec guards: `assertLiveDisplaySpecsDoNotMockApis` and the exact live-spec matcher.
  - Runbooks: checklist C4 ④⑤ and tier §4.9.
  - Unaffected lanes, for comparison: `live-c4-display`, `/monitoring`, and the mocked specs.

**Existing mechanism** (PR #2002). The gated global exposes `selectRenderedRiver(input)`:
1. wait up to 15 s for map and overlay readiness;
2. `fitBounds(bbox, {padding:48, duration:0, maxZoom:14})`;
3. wait for idle;
4. `project(anchor)`;
5. `queryRenderedFeatures` over a 16×16 box in the discharge hit layer, capped at 64 results, requiring exactly one identity match;
6. `t0 = performance.now()`;
7. `onOverlayClick({layerId:'discharge', event:{lngLat:anchor}, feature})`.

The lane then waits for GFS and IFS series 2xx plus the chart, and reads `t1 = performance.now()`.

**Live facts (node-27, 2026-09-26, read-only).** These come from a diagnostic probe against `https://test.nwm.ac.cn` that did no mutation:
- At the fitted anchor of `basins_qhh_shud_reach_000001`, the rendered features are `m11-discharge-line`, its casing, and `m11-national-river-line` with `river_segment_id = basins_qhh_shud_shud_riv_000001`. The `…_shud_reach_…` id appears only on a second national-layer feature.
- The hook therefore rejects `HOOK_FEATURE_MISMATCH` for every `shud_reach` pin tried, and resolves for `…_shud_shud_riv_000001` on `basins_qhh` and `basins_shj`.
- The segment-detail API returns 200 for both id families.
- Product networks with GFS+IFS latest products, by `core.river_network_version.segment_count`: largest `basins_shj` (29 428), a mid-size `basins_haihe_ziyahe` (8 275), smallest `basins_tailanhe` (63).

**How the frontend is served.** `test.nwm.ac.cn` `/` proxies to the display API on `:8080` (`/etc/nginx/conf.d/test.nwm.ac.cn.conf`), which serves `apps/frontend/dist` (`apps/api/startup_wiring.FRONTEND_DIST_DIR`). Deploying the frontend therefore means building `dist` in `/home/nwm/NWM`.

## Goals / Non-Goals

**Goals**

- t0 is a trusted pointer event on the map canvas, dispatched by Playwright's real mouse input at the located point, and the product's own click path handles it.
- The hook can no longer inject a feature into the product click handler.
- A P95 is measured on the public site for three network sizes, with receipts that cannot be confused with the old mechanism.
- Hook failure causes become visible: the closed codes are propagated.

**Non-Goals**

- Changing ordinary user behaviour or the product click semantics.
- A multi-pin lane. Three single-pin runs are kept simpler.
- API or DB changes.
- Re-measuring the SQL and API gates already PASSed in D11 (#2481).
- Station popups.

## Decisions

### D1 — Hook: `locateRenderedRiver` plus trusted pointer capture; direct dispatch removed

The gated global becomes exactly:

```ts
window.__nhmsRiverClickEvidence = {
  locateRenderedRiver(input): Promise<{ basinId, riverSegmentId, basinVersionId, riverNetworkVersionId, clientX, clientY }>,
  armPointerCapture(): void,
  takePointerCapture(): { timeStamp: number, clientX: number, clientY: number, isTrusted: true } | { error: RiverClickHookCode },
}
```

**`locateRenderedRiver`** keeps the existing input grammar and the readiness, fit, idle and 16-px exactly-one-match logic, with the same closed codes. After the match it:
1. projects the anchor;
2. converts the result to viewport client coordinates with the canvas's `getBoundingClientRect()`;
3. runs a **DOM check**: `document.elementFromPoint(clientX, clientY)` must be the map canvas. DOM elements above the canvas include the layer and basemap switchers (`z-[120]`), the control bar, the navigation, scale and attribution controls, basin label markers and status overlays. A real click on one of them would activate that control; a layer switch, for example, clears the popup (`OverviewPage.tsx:399-402`). Otherwise the hook rejects with `HOOK_POINT_OCCLUDED`;
4. runs a **product-hit check**, which must be the product's own priority walk and not "first result overall". Extract a pure resolver from `handleM11MapClick` (`m11MapInteractions.ts:82-108`): station cluster → station point (only when stations are shown) → the first feature in the overlay hit layer → basin fill. Both `handleM11MapClick` and the hook then use it, so they cannot drift apart.
   - The hook feeds it what react-map-gl would pass for a click at that point: `queryRenderedFeatures(point, {layers: interactiveLayerIds.filter(id => map.getLayer(id))})`. react-map-gl 7.1.9 filters the same way (`mapbox.js:473`), and an absent layer id makes MapLibre 4.7.1 raise an `ErrorEvent` that the map's error banner would show.
   - `interactiveLayerIds` and the station flag are render-local, so the hook reads them through refs kept current each render, like `overlayRef`.
   - The resolver must return the overlay target with the matched river identity. Otherwise the hook rejects with `HOOK_POINT_OCCLUDED`, because a real click at that point would open something else.

It never calls `onOverlayClick`.

**Pointer capture.**
- `armPointerCapture()` clears any previous capture and installs one capture-phase `pointerdown` listener on the map canvas.
- The listener **observes every** `pointerdown` whose target is the canvas, trusted or not, and records its `isTrusted`, `timeStamp` and client point.
- `takePointerCapture()` removes the listener and classifies what arrived:
  - exactly one trusted event: the result;
  - none, or the capture was never armed: `HOOK_POINTER_MISSING`;
  - more than one, or any untrusted one: `HOOK_POINTER_INVALID`.
- `timeStamp` is `event.timeStamp`: a DOMHighResTimeStamp from the same time origin as `performance.now()`.

**Removal.** `selectRenderedRiver` and the `onOverlayClick` parameter of `createRiverClickEvidenceHook` are deleted. The surface wiring passes no product callback to the hook.

**Unchanged:** the gating, generation-token ownership and cleanup (listeners are removed on cleanup), no map ref exposed, no fetch, no body or credential exposure, and bounded redacted messages.

### D2 — Lane: a real click per attempt

Each attempt, warmup and samples 1-20:
1. arm response observation, as today;
2. `armPointerCapture()`;
3. `locateRenderedRiver(input)`. Locate time is before t0 and is not part of the sample;
4. `page.mouse.click(clientX, clientY)`. Playwright moves the mouse there, then presses and releases, which gives trusted CDP input;
5. `takePointerCapture()`. An error, or a captured point more than 2 CSS px away on either axis from `(clientX, clientY)`, is FAIL `CLICK_DISPATCH_INVALID` at that attempt's stage and index;
6. `t0 = capture.timeStamp`;
7. the existing t1 conditions, series matching (the exact located segment path and identities), panel close and quiet interval, all unchanged.

The existing 15 s per-attempt and 360 s whole-run deadlines apply unchanged.

- **Why `pointerdown`.** It is the first event of the user's click, so t0 is conservative: it includes the down/up interval and MapLibre's click synthesis.
- **Why `isTrusted`.** Synthetic `dispatchEvent` and constructed events are untrusted, so this proves the input came from the browser's input pipeline.
- **Hook rejection codes.** Wherever the lane catches a hook rejection, it maps a closed hook code into the failure message as `hook <CODE>`, keeping the failure code `HOOK_SELECTION_FAILED`. An unknown value stays redacted.

### D3 — Receipt schema 1.1

- `schema_version` has the constant `"1.1"`.
- A new required top-level field `click_dispatch` has the constant `"trusted_pointer_event"`, and it is added to the exact top-level field list.
- The new FAIL code `CLICK_DISPATCH_INVALID` joins the closed set.
- The PASS / FAIL / BLOCKED rules are otherwise unchanged.

The Node semantic validator (`playwright.river-click-evidence.ts`), the JSON schema, the three examples and the binder all move to 1.1 together. The binder rejects 1.0 with a fixed `BINDER:` line.

No compatibility path is kept for 1.0. The only consumer is the binder recipe in the runbooks.

1.0 PASS receipts **do exist**. The three hook-dispatch baseline runs of 2026-09-26 (tasks §1.3) published them; they are kept in `/home/nwm/tmp/q/receipts/` on node-27, as the "before" only. They measure the retired mechanism. That is exactly why the binder must refuse 1.0: such a receipt is not evidence for this gate.

### D4 — Three pins and the pin rule (runbook)

For each of the three networks, the operator records the discovery evidence in the receipt directory and runs the unchanged single-pin command once:
- **Networks:** from `/api/v1/basins`, keep the basins whose GFS and IFS `latest-product?identity_only=true` are both 200. Rank them by `core.river_network_version.segment_count`, read-only, and take the largest, the one nearest the median, and the smallest.
- **Pin:** the network's `<basin>_shud_shud_riv_000001`, the discharge-layer id family. Check that segment detail returns 200. Never use a `…_shud_reach_…` id: it is not the id the discharge layer renders, and it fails with `HOOK_FEATURE_MISMATCH`.
- **Acceptance:** three binder-PASS receipts.

### D5 — Verification

- **Local:**
  - `cd apps/frontend && pnpm test`: unit tests for the hook (locate-only, product-hit occlusion, capture trusted / untrusted / missing / duplicate / unarmed, no `onOverlayClick` reachable), lane attempt (t0 from the capture, displacement FAIL, hook-code propagation), validator, schema and binder 1.1 (1.0 rejected);
  - `pnpm build`, `pnpm typecheck`, `pnpm check:types`, `pnpm check:api-types`, `pnpm check:bundle`;
  - the root schema-example loop;
  - `openspec validate --strict`.
- **node-27 pre-merge, on a throwaway stack.**
  1. Build the branch in a disposable worktree under `/home/nwm/tmp/`.
  2. Start a second display API from that worktree's code.
     - Env: `set -a; . /home/nwm/NWM/infra/env/display.env; set +a`, by absolute path. The file is gitignored, so it is absent in a worktree.
     - Then export `NHMS_MVT_FILE_CACHE_DIR=<private dir under /home/nwm/tmp>`. The production value `/home/nwm/.cache/nhms/mvt` is written by `services/tiles/mvt.py`, `apps/api/routes/basemap.py` and `apps/api/routes/precip.py`, and is shared with the retention timer.
     - Record a redacted check after sourcing and before launch: the `DATABASE_URL` user is `nhms_display_ro`, and `NHMS_SERVICE_ROLE=display_readonly`. The display API opens its connection from `DATABASE_URL` only (`apps/api/routes/hydro_display.py:238`, `apps/api/routes/pipeline.py:148`). `NHMS_DISPLAY_READONLY_DATABASE_URL` is read only by the validation tooling, so exporting it would prove nothing.
     - Run `/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port <free> --workers 1`, with cwd and `PYTHONPATH` set to the worktree. Never use `scripts/ops/start-display-api.sh`, which manages the production unit and port 8080.
     - Record `apps.api.startup_wiring.FRONTEND_DIST_DIR` as the process resolves it (it is repo-relative to the imported code, `startup_wiring.py:12-13`), and check that the served `index.html` hash equals the worktree `dist/index.html`.
  3. Run the lane three times against `http://127.0.0.1:<port>` with the three pins.
  4. Stop the process by its recorded PID and delete the private cache directory.

  The production `:8080` process, `/home/nwm/NWM` files and the production MVT cache are not touched. The DB is only read, through the read-only role, with one worker's pool.
- **Pre-merge gate.** For all three pins: rc 0, status PASS, `click_dispatch=trusted_pointer_event`, one warmup plus 20 complete samples, and no `CLICK_DISPATCH_INVALID`, `HOOK_*` or `SERIES_REQUEST_INVALID` failure.
  - A mechanism failure blocks the merge and is fixed.
  - A `THRESHOLD_EXCEEDED` FAIL with complete samples is a product finding, not a mechanism defect. It is reported to the user before merge, not tuned away.
- **node-27 post-merge.**
  1. `git pull --ff-only` on `/home/nwm/NWM`.
  2. `corepack pnpm@10.11.0 --dir apps/frontend install --frozen-lockfile`, then build to a sibling out-dir (`vite build --outDir dist.new`) and swap it in: first move any existing `dist.old` aside to a timestamped name, then two renames with `mv -T` (`dist` → `dist.old`, `dist.new` → `dist`). `emptyOutDir: true` would otherwise leave `dist` empty, and the public site broken, for the whole build. Check that the served `index.html` hash matches the new `dist`. The running API serves `dist` by path, so no restart is needed; if it still serves the old hash, restart it through the documented display restart and record that.
  3. Run the three public-site receipts plus the binder.
  4. Commit the receipt in the archive PR.

  If a receipt FAILs, the result is recorded as FAIL, not re-run until green. A P95 of 2000 ms or more is a real product finding and is reported.

## Risks / Trade-offs

- **Pointer-down t0 is slightly earlier than the old pre-dispatch t0.** That is conservative against the 2 s gate. The old number is not comparable, and the receipt says so through `click_dispatch`.
- **Headless WebGL hit determinism.** The product-hit check fails closed (`HOOK_POINT_OCCLUDED`) instead of clicking somewhere ambiguous. The fitted `maxZoom 14` view keeps a single pin segment large.
- **Hover side effects from the mouse move.** Playwright moves the mouse before pressing, so `handleMapOverlayHover` runs first: it prefetches the latest-product GETs and changes the cursor (`OverviewPage.tsx:458-465`). The lane classifies those requests as "other", so they compose with the lane. But the pre-t0 prefetch warms the latest-product cache, which is one more reason the old and new P95 are not comparable.
- **The #1342 wording names SHJ-NJ.** That basin no longer has a product (both sources 404 on 2026-09-26), so the largest current product network stands in. This is recorded, not hidden.
- **Throwaway stack fidelity.** It serves the same API code and DB as production, but over loopback HTTP without nginx or TLS. The post-merge public run is the authoritative receipt.
