## Why

- #2129 ruling B (owner, 2026-09-14): the national overview (`/`) data chain trusts a compile-time-only type. `unwrapApiData` is a bare `as T`, so a backend shape drift either throws or silently becomes a different state, and neither is visible to users.
- Verified failure modes on master `a21f25ee1`:
  - `/cycles` with `cycles: [null, …]`: `deriveM11ControlBarModel` throws `TypeError` during render. Since #2347 this is contained by the control-bar region fallback, but the user sees a generic 「此区域加载失败」 instead of a data fault.
  - `/cycles` with `cycles: ["2026-…"]` (string array): no throw. Every entry is filtered out and the select collapses to the catalog default cycle, which looks identical to "cycles not arrived yet".
  - `/basins` or `/layers` returning 200 with a malformed body: normalization throws in both the bootstrap and enrichment phases, which share the same cached promise.
    - `/basins` → `{items: […]}`: `load`'s catch settles `error: '加载总览数据失败'`, the same text as a network failure.
    - `/layers` → `[null]`: the bootstrap snapshot is never written and `bootstrapError` stays null, so the page stays on 「总览数据加载中」 indefinitely.
  - `/valid-times` object without an array `valid_times`: `normalizeLayerValidTimesResponse` resolves `undefined`, `pairRecord` throws inside `writeValidTimes`, and the layer stays on its pending text.
- Only `/layers`, `/cycles`, `/valid-times` and precip index have a backend `response_model`; `/basins` and `/runs` do not (#2348 tracks the backend side).

## What Changes

- One shape-guard module for the six overview endpoints: `/api/v1/layers`, `/api/v1/layers/discharge/cycles`, `/api/v1/layers/{layer_id}/valid-times`, `/api/v1/precip/{source}/{cycle}/index`, `/api/v1/basins`, `/api/v1/runs`.
  - Each validator checks the minimum shape consumers dereference, including element shape, and throws a `DataShapeError` carrying a Chinese endpoint label.
  - Validators run inside each fetcher's `cached()` loader, so a malformed payload is never cached and its rejection rides the existing scoped-error paths.
- A malformed payload degrades to an explicit 「数据异常」 state:
  - store field `dataAnomalies` (endpoint labels, per load) and an overview notice `m11-data-anomaly`;
  - bootstrap and enrichment error text uses 「数据异常」 instead of 「暂不可用」 when the cause is a shape error;
  - scoped states (`cyclesBySource`, `validTimesByCycle`, `precipIndexByCycle`) keep their existing `'error'` variant.
- Regression tests for cycles `[null, …]` and string arrays at store and page level, plus basins/layers/valid-times/precip/runs malformed cases.
- Review convention: a new `unwrapApiData` / `getApi` consumer must ship a malformed-payload test. Recorded in `openspec/project-profile.md`.
- The #2347 control-bar boundary test and spec scenario move to a generic render-throw trigger, because `[null]` cycles no longer reach render.

## Non-goals

- Schema validation inside `unwrapApiData` (#2129 ruling A, deferred).
- Backend `response_model` (#2348).
- Endpoints outside the six (models, pipeline status, queue, versions, hydroMet, monitoring stores).
- Changing the `'error'` unions or the three-state (`pending` / `error` / `fail-closed`) layer text machinery.
- Removing the #2347 region boundaries or the existing precip overlay guards (kept as defence in depth).

## Impact

- Frontend only:
  - new `apps/frontend/src/lib/m11/overviewShapeGuards.ts`;
  - `stores/overviewData.ts`, `pages/OverviewPage.tsx`;
  - tests.
- `openspec/project-profile.md`: one review-convention line.
- Specs:
  - ADDED capability `frontend-overview-data-shape-guards`;
  - MODIFIED `frontend-render-failure-containment` scenario trigger.
- node-27: frontend-only deploy plus browser receipt with client-side injection (same method as #2336 and #2347).
