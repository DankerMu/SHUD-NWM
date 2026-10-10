# basemap-notice-survives-reset

## Why

mocked 车道零重试，`m11-notices.mobile.mocked.spec.ts:203` 偶发在 5 秒内等不到 `m11-map-source-error` 状态条（#2886）。读码得到的机制：`useM11MapSourceError` 在 `resetKey` 每次变化时把提示清空。实际会在挂载后变化的只有两项：图层目录到达后的 `overlay.sourceId`，以及随后的 `validTime` 校正（流域要素数恒为 0——流域边界叠加层是关的；`layer` 恒为 `discharge`）。MapLibre 对同一张已 `errored` 的瓦片不再发 error；它会向父级瓦片回退并带来有限几波新的 error，但那只在某个 source 的 `data` 事件触发 `update()` 时才推进，爬到 z0 就停。底图瓦片的 503 及其父级回退若全部早于图层目录 / 有效时刻落定，“底图服务暂时不可用”就被吞掉且不再恢复。这在生产上同样成立（天地图限流时用户看到灰底地图而没有说明）。机制目前是推断，未复现；helper 也没有记录 `failedBasemapTiles()`，失败时无法判别“没请求”还是“请求了但提示被清”。

## What Changes

分两步，第二步以第一步的结论为前提：

1. 判别与复现（纯 e2e）：`openWithNoticeAndStatus` 保留 mocks 句柄并在等状态条之前先等 `failedBasemapTiles() > 0`；新增一条确定性序列用例（底图 503 已点亮状态条 -> 放行被挂起的 bootstrap -> 状态条在此后从未缺席）。
2. 若该序列在未修复的代码上观察到状态条被清掉过（机制确认）：`useM11MapSourceError` 把底图提示与业务图层错误分成两份状态——底图提示只在 `basemap` 变化时清空，业务图层错误仍随 `resetKey` 清空；显示优先级不变（业务错误优先于底图提示）。补 hook 单测。

若在未修复的代码上状态条从未被清掉（机制被否定）：只交付第 1 步，不动产品代码，在 PR 与 issue 里写明结论，issue 保持打开。

已知取舍：今天时间轴每走一步（`validTime` 变）都会顺手清掉底图提示；修复后只有换底图或刷新才清，底图服务中途恢复时提示会留到换底图为止（平移 / 缩放后成功的新瓦片今天也不清提示，这一点不变）。漏报比多留一条提示更糟，接受。

不放大任何超时，不给车道加 `retries`。design.md 省略（compact）。

## Triage

```text
Issue type: bug (flaky lane + 可能的产品漏提示)
Fixture level: compact
Upstream suggested level: 未给档位（建议用 hook 单测 + mocked 序列用例钉住；agree，取 compact）
Blast radius: 地图状态条 `m11-map-source-error` 何时清空；所有视口
Selected risk packs: Behavior regression（业务图层错误的清空时机与优先级不变；换底图后底图提示清空）
Evidence floor: hook 单测修复前红 / 修复后绿（清空语义的确定性判据）；序列用例修复前红 / 修复后绿（集成证据）；现有三条 hook 单测不改断言；notices 与 control-bar-portrait 两个 spec 三个移动 project `--repeat-each=20`；完整 mocked 车道；node-27 live 守卫（改了产品代码时）
```

## Impact

- 受影响文件：`apps/frontend/e2e/m11-notices.mobile.mocked.spec.ts`、`e2e/m11-control-bar-portrait.mobile.mocked.spec.ts`（只在计数断言顺序需要调整时）；机制确认时另有 `src/components/map/m11MapRuntime.tsx`、`M11MapLibreSurface.tsx`、`src/components/map/__tests__/m11MapRuntime.test.tsx`。
- 受影响规格：`national-overview-page`（状态条的生命周期是全视口行为，不放在只管移动几何的 `mobile-map-overlay-layout`）。
