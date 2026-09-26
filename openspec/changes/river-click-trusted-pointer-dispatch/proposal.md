## Triage

```text
Issue type: test oracle / evidence gap (#1970, reopened for #1342's river-click hard gate)
Fixture level: expanded
Upstream suggested level: issue body L1-L3; the orchestrator selects expanded because the change edits
  a production browser hook, a published evidence schema (1.0 -> 1.1) and its binder, and must end in a
  node-27 live receipt on the public site
Blast radius: the gated hook is compiled into the production bundle (inert unless the pre-start flag
  is set); a wrong change either leaks a test surface to ordinary users or yields a click P95 that is
  not measured through the real input path
Selected risk packs: Public API / CLI / script entry (hook global, lane env, binder), Schema / field
  names (receipt schema 1.1), Legacy compatibility (1.0 receipts), Auth / permissions / secrets (hook
  exposure, receipt redaction), Release / operational (deploy dist + live run), Documentation, File IO
  (publisher, private cache, dist swap), Error handling (new closed codes), Config, Resource limits,
  Concurrency (second API process)
Evidence floor: tasks.md Evidence Floor 1-7; floor 6 (node-27 live) = pre-merge throwaway-stack merge
  gate + post-merge public-site receipts for three pins
```

## Why

Batch Q of the 10-batch serial run. Master is `b48a38599` (after O #2641/#2642).

The original body of #1970 was implemented by PR #2002 / #2045 (2026-09-04). That delivered the gated `window.__nhmsRiverClickEvidence` hook, the `@live-river-click` lane (warmup + 20, nearest-rank P95, a mode-0600 no-clobber receipt, schema 1.0) and the binder. The reopen comment (2026-09-19) carries #1342's river-click gate. Its SQL, index and API parts were closed by #2474 / #2481:
- D11 live receipt: SQL P95 5.85 ms, API P95 150 ms, buffers 569;
- `river_segment_key` appears in the Index Cond.

Two parts are still open.

1. **The measured click is not a real click.** `createRiverClickEvidenceHook` (`apps/frontend/src/lib/riverClickEvidence/hook.ts:505-545`) takes t0 and then **calls `onOverlayClick(...)` directly**, passing a constructed `{layerId, event:{lngLat}}` and the queried feature. MapLibre's input pipeline (`react-map-gl` `onClick` → `handleM11MapClick`, `M11MapLibreSurface.tsx:282`) and the browser's pointer dispatch are bypassed. The batch requirement is explicit: node-27 browser e2e with **real clicks, not mock hooks**.
2. **The public dual-curve click P95 < 2 s has never been measured.** The runbook says so (`docs/runbooks/tier-node27-timeseries-storage.md:6045`: "does not claim live PASS"). The only public-site clicks, task52's `clicks.mjs`, are single samples with a fixed 4 s wait.

Live baseline with the existing lane, run on node-27 against `https://test.nwm.ac.cn` on 2026-09-26 (read-only):
- With the documented `…_shud_reach_…` pins (`basins_shj`, `basins_haihe_ziyahe`, `basins_tailanhe`), every run **FAILs at warmup** with `HOOK_SELECTION_FAILED`.
- A diagnostic probe shows why. The hook rejects with `HOOK_FEATURE_MISMATCH`, because the rendered `m11-discharge-line` feature at the segment anchor carries the `…_shud_shud_riv_NNNNNN` id. The API segment detail accepts both id families, so preflight passes and the lane fails later, in the map.
- The lane then collapses the hook's closed code into the fixed message `hook invocation failed` (`playwright.river-click-lane-attempt.ts:432`), which hid this cause.
- With `…_shud_shud_riv_000001` pins the hook resolves. The hook-dispatch numbers from that rerun are recorded in tasks §1 as the "before".

## What Changes

- **Hook: locate-only, plus trusted pointer capture.** `window.__nhmsRiverClickEvidence` (still gated by the exact pre-start `window.__NHMS_E2E_HOOKS__ === true`) keeps the bounded fit / idle / 16-px query / exactly-one-match logic, but it **no longer calls `onOverlayClick`**. It resolves the matched identity plus the viewport client point of the anchor. It also confirms that the map canvas is the top DOM element at that point, and that the product's own click-target resolution there (station cluster, station, overlay river, basin fill) selects the same river, so a real click at that point reaches that river through the product path.

  The hook also captures the next pointer-down on the map canvas. It accepts only an event with `isTrusted === true` whose client point lies within 2 CSS px of the located point. The event's `timeStamp` becomes t0, in the same clock as `performance.now()`. No function remains that can put a feature into `onOverlayClick` except MapLibre's own click event.
- **Lane: `page.mouse.click`.** Each attempt runs: arm response observation → arm pointer capture → locate → `page.mouse.click(x, y)` (Playwright input, CDP `Input.dispatchMouseEvent`, trusted) → take the capture as t0 → wait for the existing t1 conditions (exactly one matching GFS and one IFS `forecast-series` 2xx for the located segment, chart visible). A missing, untrusted, duplicated or displaced pointer event is a new closed FAIL, `CLICK_DISPATCH_INVALID`. Hook rejections keep their closed code in the failure message instead of `hook invocation failed`.
- **Receipt schema 1.0 → 1.1.** A new required top-level field `click_dispatch` with the constant `"trusted_pointer_event"`, and the new failure code. The binder and validator accept only 1.1, so a hook-dispatch 1.0 receipt can never bind as PASS. The examples are updated.
- **Three pins.** The lane stays single-pin. The runbook runs it for three current product networks chosen by a documented rule: the largest, one mid-size near the median, and the smallest, by `core.river_network_version.segment_count`. Each pin is the network's `…_shud_shud_riv_000001` discharge-layer segment, checked against live `/api/v1` before use.
- **Docs.** `node-27-bringup-checklist.md` C4 ④⑤ and `tier-node27-timeseries-storage.md` §4.9 get the real-click mechanism, the pin rule (the discharge-layer id family, with the failure mode seen above), and the three-receipt command.
- **Live evidence (node-27).**
  - Pre-merge: a throwaway stack serves the branch's `dist` from a second display API on a loopback port, using the read-only role, and the three-pin real-click run executes against it.
  - Post-merge: `dist` is deployed on `/home/nwm/NWM`, then three receipts are produced against `https://test.nwm.ac.cn` and bound by the binder. They land in the archive PR.

## Capabilities

### Modified Capabilities

- `frontend-river-click-live-evidence`: the hook becomes locate-only with trusted pointer capture, t0 comes from the trusted pointer event, the receipt moves to schema 1.1, and live execution belongs to this capability's own receipts, since #1895 is closed.

## Impact

- **Frontend:**
  - `apps/frontend/src/lib/riverClickEvidence/{hook,constants,receipt,…}.ts`;
  - `apps/frontend/src/components/map/M11MapLibreSurface.tsx` (hook wiring and refs only);
  - `apps/frontend/src/components/map/m11MapInteractions.ts` (pure click-target resolver extracted; behaviour unchanged);
  - the rewritten consumers listed in tasks Must-preserve (`runbookContract`, the surface hook test, the fake page, the lane readiness probe, the C4 negative tests);
  - `apps/frontend/playwright.river-click-lane-attempt.ts`, `playwright.river-click-lane.ts` (the 1.1 semantic validator lives in `src/lib/riverClickEvidence/receipt.ts`, which the evidence owner imports, so `playwright.river-click-evidence.ts` and `playwright.river-click-terminal.ts` need no edit);
  - `apps/frontend/scripts/river-click-receipt-binder.mjs`;
  - the unit tests under `src/__tests__/riverClick*` and `src/lib/riverClickEvidence/__tests__/*`.
- **Schema:** `schemas/frontend_river_click_live_evidence.schema.json` and its three examples.
- **Docs:** the two runbooks.
- **No change:** ordinary user behaviour (no flag means no global, no listener, identical pointer, hover, popup and request behaviour); the product click path itself; the API; the DB; the `/monitoring` and C4 lanes.
