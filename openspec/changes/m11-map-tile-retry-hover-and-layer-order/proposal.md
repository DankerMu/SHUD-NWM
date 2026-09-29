## Why

M11 首页地图有三处前端缺口，都在同一组地图文件里：

- **#2537**：后端冷生成 gate 满载时返回 `503` + `Retry-After: 1` + `{"error":{"code":"MVT_COLD_GENERATION_BUSY"}}`（#2346）。前端没有对应处理，MapLibre 把它当作失败瓦片：屏幕上留下空洞并报 `AJAXError`，要等用户再平移或缩放才会补上。
- **#2628**：discharge 河段密集时看不出鼠标落在哪一段，只有选中后的橙线。yd-viewer 已有悬停高亮（白色光晕 + 青线），NWM 要对齐。
- **#2650**：discharge overlay 的各层没有传 `beforeId`。以下两种情况会让 overlay 的 Source 子树重新挂载：时间轴每走一步（`sourceKey` 随 `validTime` 变化），以及 overlay 从 null 恢复。重挂载时这些层被 `addLayer` 追加到 style 栈顶，压住气象代站层，违反 `met-station-cluster-layer`「代站渲染在河段上方」这条 MUST。

## What Changes

- **瓦片重试**：注册 MapLibre 自定义协议 `nhms-mvt`。M11 surface 注册的两类 MVT vector source（discharge overlay 和全国河网）的 tile URL 都加上 `nhms-mvt://` 前缀。
  - 协议处理器用 `fetch` 取原始 URL。只有 `503` 且（带 `Retry-After`，或响应体 `error.code == MVT_COLD_GENERATION_BUSY`）时才重试：等待 `Retry-After` 秒（有上下限，加少量抖动），最多重试 3 次。
  - 其他状态、以及重试次数用完时，抛 maplibre `AJAXError`，保留 `status`。404 的空瓦片语义不变。
  - 瓦片被放弃（abort）时，立即取消挂起的等待和请求，抛 `AbortError`。
- **悬停高亮**：`M11OverlayPrimitive` 的 line overlay 增加 `-hover-halo`（白 8 px、0.55）和 `-hover-line`（青 `#22d3ee` 4.5 px）两层，filter 为 `segmentFilter(hoveredSegmentId)`。
  - 图层顺序：casing → main → hit → hover-halo → hover-line → selected-halo → selected-line。
  - 悬停状态放在 `M11MapLibreSurface` 内部，只取 discharge 命中河段，按 id 去重；移出河段、移出地图或切换图层时置 null。
  - 现有的 `onOverlayHover`（预取）、手形光标、点击和选中行为都不变。
- **层序**：新增纯函数 `ensureM11StationLayersOnTop(map)`，经 `<Map onStyleData>` 在每次 style 变化后执行。代站层不在栈顶时，按规范顺序 `moveLayer` 到栈顶；已在栈顶时不做任何事。
  - 覆盖 overlay 重挂载、overlay 从 null 恢复、换底图，以及其它层晚到这几种情况，新增的 hover 层也自动包括在内。
  - 顺带修正 spec 中「`cluster` 开启」的措辞，改为按 `m11StationClusterPolicy` 条件开启。

## Deviations from issue wording

1. **#2650**：没有采用 issue 推荐的「overlay 各层传 `beforeId`（`clusters` 或哨兵锚点）」，改为「代站层保持在栈顶」。
   - 理由：react-map-gl 中 Source 子层在 `setTimeout(0)` 之后才挂载，而其它 source 要等数据到达，哨兵层会落在自定义层最底部，overlay 反而被压到河网线和流域面之下；锚在 `clusters` 则有同轮首挂的竞态（见 design D3）。
   - 相应地，验收「全部层带锚点 beforeId / 关闭时不带」改为：用有状态 map stub 记录 `addLayer` / `moveLayer`，断言重挂载、恢复、换底图之后代站层都在栈顶，且纠偏幂等。
2. **#2537**：重试逻辑放在新模块 `m11MvtRetryProtocol.ts`，issue 原本建议写进 `m11MapRuntime.tsx`，理由是便于注入 fetch、sleep、random 做单测。重试上限定为 3 次（issue 写的是「2~3 次」）。
3. **#2628 mocked e2e**：现有 mocked e2e 对 `/api/v1/**` 一律返回 JSON，`valid_times` 为空，discharge overlay 不会挂载，仓库里也没有 pbf 夹具。因此悬停的交互验收由单测（stub 输出 `filter`）和 node-27 真实浏览器 receipt（读 `data-hovered-segment-id`，另附截图）承担，不新增 mocked e2e 用例。

## Impact

- 代码：`apps/frontend/src/components/map/`（`m11MapPrimitives.tsx`、`M11MapLibreSurface.tsx`、`m11MapInteractions.ts`，以及新增的 tile 重试协议模块）和对应单测；`apps/frontend/src/test/maplibreStub.tsx`，扩展观测面；不新增 mocked e2e（见偏离 3）。
- 规格：`frontend-mvt-layer-consumption`（ADDED）、`m11-river-hover-highlight`（新能力）、`met-station-cluster-layer`（MODIFIED）。
- 不改后端，不改 `sourceKey` 的身份语义（`mvt-tile-contract`）。
