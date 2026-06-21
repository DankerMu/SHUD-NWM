## Why

Carried follow-up from PR [#602](https://github.com/DankerMu/SHUD-NWM/pull/602) reviewer-1 W1 + reviewer-2 W2/S1 (issue [#603](https://github.com/DankerMu/SHUD-NWM/issues/603)). PR #602 enforced "discharge layer canonical tile URL is always `/api/v1/tiles/hydro-national/...`" via spec `overview-data-contracts: Default discharge tile URL is national across all /api/v1/layers callers` + `mvt-tile-contract: Discharge canonical URL is national across all callers`.

Frontend mock fixture `m11MvtMetadataByLayer['discharge']` ([apps/frontend/src/pages/__tests__/M11Shell.test.tsx:326](apps/frontend/src/pages/__tests__/M11Shell.test.tsx:326)) still maps to legacy `dischargeMvtMetadata` (pre-fix single-run shape with `/api/v1/tiles/hydro/{run_id}/...` URL + `run_id` placeholder + `run_id`-keyed source_refs). The post-#602 fixture `dischargeNationalMvtMetadata` ([line 305-321](apps/frontend/src/pages/__tests__/M11Shell.test.tsx:305)) already exists alongside but isn't routed through the default-discharge test path.

Also: both fixtures pin `min_zoom: 7`, but real backend [`_NATIONAL_DISCHARGE_METADATA`](services/tiles/mvt.py:740-748) returns `min_zoom: 3` (前端初始全国视图 zoom=3.35 对齐). Fixture drift on min_zoom is a second mock-vs-reality gap.

CI didn't catch either drift — the mock is consumed only by frontend unit tests that don't enforce backend shape equivalence.

## What Changes

- **`apps/frontend/src/pages/__tests__/M11Shell.test.tsx:326`**: switch `m11MvtMetadataByLayer['discharge']` from `dischargeMvtMetadata` to `dischargeNationalMvtMetadata` (option A per issue #603 acceptance), so M11 default-discharge unit tests exercise the post-#602 canonical national shape.
- **`apps/frontend/src/pages/__tests__/M11Shell.test.tsx:315`**: fix `dischargeNationalMvtMetadata.min_zoom` from `7` to `3` matching real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom`. Also align comment at line 304 (was "min_zoom=7", now "min_zoom=3").
- **Keep `dischargeMvtMetadata` legacy constant** at line 285-301 with added header comment: legacy deeplink-only shape (the `/api/v1/tiles/hydro/{run_id}/...` route still exists at `apps/api/routes/flood_alerts.py:1059` as a direct-deeplink route; tests at line 821, 926 use this for exercising legacy single-run code paths).

No backend / OpenAPI / production code change.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mvt-tile-contract`: ADD scenario *Frontend M11Shell unit-test default-discharge fixture uses national shape* — anchors the mock-vs-reality alignment for the canonical `/api/v1/layers` discharge entry shape.

## Impact

- **Code**: `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` — ~3-5 line changes (mock map ref + min_zoom + comment).
- **API 契约**: 无变化。
- **OpenAPI**: 无变化。
- **CI**: 路径 scope `apps/frontend/**` → 触发 `frontend-build` job 跑 `pnpm tsc` + `pnpm test --filter=apps/frontend`。
- **Receipts**: 不需 node-27 live receipt（test-only mock fixture，无 deploy-affecting 行为变化）。
