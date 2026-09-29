# Tasks — m11-map-tile-retry-hover-and-layer-order

Fixture level：standard。Risk packs：frontend-map-interaction；external-contract（与 #2346 约定的 503 契约）。

## Must preserve

- `buildMvtTileUrlTemplate`、`buildM11RegisteredOverlay(...).source.tiles` 和全国河网 `tiles` 数据保持普通 `https` URL；`nhms-mvt://` 前缀只在 primitive 的 `<Source tiles>` 处添加。
- `sourceKey` 身份和 `_mvt_cache_version` 参数不变。
- 404 仍按空瓦片处理；非 503 的错误照常触发 error 事件和 mapSourceError 横幅。
- `onOverlayHover` 预取（`prefetchHydroMetLatestProducts`）、手形光标、`resolveM11ClickTarget` 的点击优先级、选中高亮都不变。
- overlay 相对流域面、全国河网线、降水栅格的栈序：保持现状，不得劣化。
- 降水栅格的 `beforeId`（全国河网线）不变；gated river-click evidence hook 的三方法接口不变（`frontend-river-click-live-evidence`）。

## 1. #2537 瓦片重试

- [x] 1.1 新增 `m11MvtRetryProtocol.ts`（D1），在 surface 模块中注册；overlay vector 分支与 `M11NationalRiverPrimitive` 渲染 `<Source tiles>` 时经 `withM11MvtRetryProtocol` 包装。
- [x] 1.2 单测，全部使用注入的 fetch / sleep / random：
  - 503 + Retry-After → 按间隔重试，最终成功，返回 data 为第二次响应；
  - 只有 body code、没有 Retry-After 头 → 也会重试；
  - 超过 3 次 → 抛 maplibre `AJAXError`，status 为 503；
  - 404、500 以及不带标记的 503 → 只发一次请求，`AJAXError` 保留 status；
  - 等待期间 abort → 抛 AbortError，不再发请求；
  - Retry-After 按上下限夹取；
  - 前缀幂等且正确剥离；
  - 注册幂等。
- [x] 1.3 扩展 `MaplibreSourceStub`，输出 `data-source-tiles`；断言两个 vector source 渲染出的 tiles 带前缀，而 `buildM11RegisteredOverlay` 的输出不带前缀。

## 2. #2628 悬停高亮

- [x] 2.1 `M11OverlayPrimitive` 增加 hover 两层（D2），vector 与 geojson 两个分支都加。
- [x] 2.2 新增 `resolveM11HoveredSegmentId` 与 `createM11HoverSegmentTracker`；surface 状态接线；输出观测面 `data-hovered-segment-id`；overlay 变化时经 tracker `update(null)` 置空。
- [x] 2.3 扩展 `MaplibreLayerStub`，输出 `data-layer-filter`。单测：
  - 两个分支都输出 hover 两层，filter 为 `segmentFilter(id)`，顺序在 hit 之后、selected 两层之前；
  - null 时不匹配任何要素；
  - 悬停 A → A，悬停 B → B，移到空白、站点或流域面 → null，mouseleave → null；
  - 同一河段内连续 move 只回调一次；
  - `onOverlayHover` 与预取仍被调用；
  - 切换图层置 null 后，切回再悬停同一河段仍会高亮。

## 3. #2650 层序

- [x] 3.1 在 `m11MapPrimitives.tsx` 实现 `ensureM11StationLayersOnTop(map)`（D3），surface 通过 `<Map onStyleData>` 接线。
- [x] 3.2 用有状态 map stub（记录 `getLayersOrder`、`addLayer`、`moveLayer`）做单测：
  - overlay 层在代站之后加入（模拟 sourceKey 重挂载，以及从 null 恢复）→ 纠偏后代站 5 层按规范顺序位于栈顶，overlay 层与 basin/national/precip 的相对顺序不变；
  - 已在栈顶 → 零次 `moveLayer`（幂等，不会循环）；
  - 没有代站层 → 零次调用；
  - 只注册了部分代站层 → 只移动已注册的那些；
  - 模拟换底图后全部重建、代站先于 overlay 加入 → 纠偏后代站在栈顶；
  - 流域面或全国河网晚于代站加入 → 代站仍被移回栈顶。
- [x] 3.3 surface 单测：`Map` 收到 `onStyleData`，调用时会对传入的 map 执行纠偏（扩展 `MaplibreMapStub` 以暴露这个 prop）。
- [x] 3.4 `met-station-cluster-layer` 中 cluster 的措辞改为条件式（经 spec delta）。

## 4. 验证

- [x] 4.1 `cd apps/frontend && pnpm test`（全量）、`pnpm typecheck`、`pnpm build`（只跑 vite，不含 tsc，所以 typecheck 单独执行）、`pnpm check:api-types`（仓库没有 lint 脚本）。
- [x] 4.2 `openspec validate m11-map-tile-retry-hover-and-layer-order --strict --no-interactive`。
- [ ] 4.3 node-27 部署后，用真实浏览器在 `https://test.nwm.ac.cn` 做 Playwright receipt，结果写入文件：
  - 重试：记录网络日志，同一瓦片 URL 出现 503（`MVT_COLD_GENERATION_BUSY`）后接 200，且没有 mapSourceError 横幅或 AJAXError 控制台错误。如自然流量复现不出来，在 display.env 临时把 `NHMS_DISPLAY_MVT_COLD_LIMIT` 调小（事后恢复，并记录前后值）。
  - 悬停：鼠标移到 discharge 河段后，`data-hovered-segment-id` 等于该河段 id，截图可见青线；移开后置空；点击后打开河段曲线窗，截图可见橙线压在青线之上。与 yd（`nwm.ac.cn/yd/`）的观感对照截图。
  - 层序：开启代站并走 3 步时间轴，每一步截图，代站点和聚合簇始终压在河段线之上（生产环境没有 map 读取面，层序由 3.2 的单测保证，这里用截图作为 live 证据）。

## Evidence Floor

- [x] `cd apps/frontend && pnpm test && pnpm typecheck && pnpm build`
- [x] `openspec validate m11-map-tile-retry-hover-and-layer-order --strict --no-interactive`
- [ ] node-27 浏览器 receipt（4.3）
