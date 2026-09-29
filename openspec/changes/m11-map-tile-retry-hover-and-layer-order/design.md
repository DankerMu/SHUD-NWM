## Context

- MapLibre 4.7.1 与 react-map-gl 7.1.9。
- `makeRequest` 对 `http(s)` URL 只走内置 fetch，不能拦截；只有 `<scheme>://` 形式的 URL 在 `REGISTERED_PROTOCOLS` 中找得到处理器时才走自定义协议。worker 线程里的请求经 `GR` 消息转到主线程执行。
- vector tile 加载失败时：`err.status === 404` 按空瓦片处理，其他错误触发 map `error` 事件（`maplibre-gl-dev.js:38594`）。
- react-map-gl 的 `Layer`：
  - 首次渲染（style 已加载）时调 `addLayer(options, beforeId)`；
  - 之后只在 `beforeId` 变化时调 `moveLayer`；
  - `styledata` 事件会让所有 Layer 重新渲染，缺失的层被重新添加。但 `<Source>` 的子层要到 source 注册之后（`source.js:68` 的 `setTimeout(0)`）才补上，而各个 source 又按数据到达的先后挂载，所以实际栈序不等于 JSX 树序（见 D3）。

## Goals / Non-Goals

目标：
- 冷生成繁忙时瓦片自动补齐。
- discharge 悬停高亮。
- discharge overlay 在任何重挂载后都不压住代站层。

非目标：
- 后端改动；
- `sourceKey` 身份语义；
- 悬停 tooltip；
- 站点和流域面的悬停；
- 触屏；
- 全国河网或流域面晚到时的层序（既有行为，另报）。

## Decisions

### D1 瓦片重试走自定义协议 `nhms-mvt`

- 新模块 `apps/frontend/src/components/map/m11MvtRetryProtocol.ts`，导出以下成员：
  - `M11_MVT_RETRY_PROTOCOL = 'nhms-mvt'`
  - `withM11MvtRetryProtocol(url)`：加 `nhms-mvt://` 前缀；已有前缀时幂等。
  - `m11MvtRetryLoad(params, abortController, deps?)`：可注入 `fetch`、`sleep`、`random` 以便测试。
  - `registerM11MvtRetryProtocol()`：幂等，调用 `maplibre-gl` 的 `addProtocol`。
- 处理器剥掉前缀后用原 URL 发 `fetch(url, { signal })`：
  - `ok` → 返回 `{ data: arrayBuffer, cacheControl, expires }`，与内置 `makeFetchRequest` 相同。
  - 满足以下条件时可重试：`status === 503`，并且带 `Retry-After` 头，或响应体 JSON 的 `error.code === 'MVT_COLD_GENERATION_BUSY'`。
  - 延迟：`Retry-After` 取十进制秒数，解析不出时按 1 s；夹在 [0.25 s, 5 s] 内，再加 [0, 250 ms) 的抖动。最多重试 3 次，即最多 4 次请求。
  - 不可重试或重试用完：抛 `new AJAXError(status, statusText, url, blob)`，其中 url 为原 URL，从 `maplibre-gl` 导入。这样 404 语义和错误事件都与改前一致。
  - abort：`signal.aborted` 时立即清掉等待定时器，抛 `new Error('AbortError')`（maplibre 的 `isAbortError` 按 message 判定），不再发下一次请求。
- 生效范围：只包装 M11 surface 注册的 MVT vector source，即 `M11OverlayPrimitive` vector 分支的 `tiles` 和 `M11NationalRiverPrimitive` 的 `tiles`。`buildMvtTileUrlTemplate` 与 `sourceKey` 都不改，所以 URL 身份、缓存版本参数和 e2e 路由拦截（主线程 `fetch`）都不受影响。
- **包装位置写死**：`nhms-mvt://` 前缀**只**在 primitive 渲染 `<Source tiles>` 时加。`buildMvtTileUrlTemplate`、`buildM11RegisteredOverlay(...).source.tiles` 和全国河网的 `tiles` 数据都保持普通 `https` URL，因为 `M11Shell.test.tsx` 等处用 `new URL(tiles[0])` 解析它们。
- 必须抛 maplibre 导出的 `AJAXError`：它已注册跨线程序列化，`status` 能回到 worker。自定义 Error 经 structured clone 会丢掉 status，404 就会变成 error 事件。
- 注册时机：`M11MapLibreSurface` 模块加载时调用一次（幂等）。
- 备选方案：`transformRequest` 做不了重试；在 source 的 `error` 事件里重载整个 source 会冲掉所有瓦片。两者都否决。

### D2 悬停状态留在 surface 内，按 id 去重

- `m11MapInteractions.ts` 新增：
  - `resolveM11HoveredSegmentId(interaction, renderableOverlay)`：只在 `interaction.layerId === 'discharge'` 且 overlay 是 discharge 时，取 `river_segment_id ?? segment_id`（非空字符串）；否则返回 null。
  - `createM11HoverSegmentTracker(onChange)`：`update(id)` 只在 id 变化时回调。
- surface 暴露观测面 `data-hovered-segment-id`，做法与现有 `data-*` 属性相同，供单测与 node-27 浏览器 receipt 读取。
- 置 null 必须经过 tracker（`update(null)`）：否则 tracker 记住的上一个 id 不会清掉，切回图层后再悬停同一河段不会高亮。
- surface 的 `handleMouseMove` / `handleMouseLeave` 包一层：先照旧把 interaction 交给 `onOverlayHover`，再更新 tracker。
- `setHoveredSegmentId` 只在变化时触发；overlay 的 layerId 或 sourceId 变化时置 null。
- `M11OverlayPrimitive` 新增 `hoveredSegmentId` prop。vector 和 geojson 两个分支都在 hit 与 selected-halo 之间插入 hover 两层；样式照搬 yd。hoveredSegmentId 为 null 时，`segmentFilter(null)` 匹配空串，不命中任何要素。

### D3 代站层保持在 style 栈顶（`onStyleData` 纠偏），不用 beforeId 锚点

**为什么不用锚点层**（fixture review 第 1 轮）：
- react-map-gl 中，不包在 Source 里的 `<Layer>` 在 `styledata` 后同步 `addLayer`；而每个 `<Source>` 的子层要在 `setTimeout(0)` 之后才补上（`source.js:68`）。
- 全国河网、降水、流域面又都要等数据到达才挂载。
- 所以哨兵层实际会落在自定义层的最底部。overlay 一旦 `beforeId=哨兵`，就会被插到河网线、降水栅格和流域面之下，比现状更糟。
- 锚在 `clusters` 上则有同轮首挂时指向不存在的层的竞态（#2650 自己指出的）。

**方案**：
- 新增纯函数 `ensureM11StationLayersOnTop(map)`，放在 `m11MapPrimitives.tsx`，与代站层 id 同处。
  - 只依赖 `getLayersOrder()`、`getLayer(id)`、`moveLayer(id)` 三个方法。
  - 代站层的规范顺序：`clusters`、`cluster-count`、`met-stations-point`、`met-stations-selected-halo`、`met-stations-selected-point`。只取当前已注册的那些。
  - 如果栈顶 k 层恰好就是它们、且顺序正确，直接返回，**不调用 moveLayer**。
  - 否则按规范顺序逐个 `moveLayer(id)`（不带 beforeId，即移到栈顶）。
  - 没有代站层时什么都不做。
- surface 给 `<Map>` 传 `onStyleData={(e) => ensureM11StationLayersOnTop(e.target)}`。
  - maplibre 的 `addLayer` / `moveLayer` / `setStyle` 都会在下一帧的 `style.update` 中触发 `styledata`。
  - 因此下列情况都会在变化后的那一帧被纠正：(a) 首屏代站已开、其它数据晚到；(b) 之后才开代站（代站本来就追加到栈顶，纠偏为 no-op）；(c) overlay 重挂载（sourceKey 变化或从 null 恢复）；(d) 换底图后全部重建；以及流域面、全国河网晚到时被追加到代站之上。
  - 终止性：纠偏后的顺序满足前置条件，下一次 `styledata` 时 no-op，不会循环。
  - 代站 Layer 不传 `beforeId`，react-map-gl 的 `updateLayer` 不会把它们移回去。
- overlay 相对流域面、全国河网线、降水栅格的位置保持现状：overlay 仍按挂载时序追加，本批不改变、不劣化。全国河网或流域面晚到时压住 overlay 属于既有行为，已作为 out-of-scope 发现上报。
- 成本：每次 `styledata` 读一次 `getLayersOrder()`（O(层数) 的 id 数组，不序列化 style），通常为 no-op。

## Risks / Trade-offs

- 自定义协议的请求在主线程执行 fetch（经 worker 的 `GR` 转发）。可以接受：MapLibre 自身对自定义协议就是这么做的，瓦片解析仍在 worker。
- 重试会延长 gate 占满时的尾部请求。上限是每块瓦片最多 4 次、最长约 15.75 s 的等待；视口移开时立即 abort。
- 纠偏发生在变化后的那一帧，理论上可能有一帧 overlay 在代站之上，人眼不可见。
- 跨域部署时，`Retry-After` 不在 CORS 默认可读的响应头里（后端没有 expose）。此时靠响应体 code 触发重试，默认 1 s，与后端给的值一致。同源生产环境不受影响。
