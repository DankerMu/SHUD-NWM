---
status: archived
current_authority: "openspec/specs/single-map-shell-routing/spec.md; openspec/specs/legacy-display-page-retirement/spec.md; docs/runbooks/display-readonly-live-mvt.md; docs/runbooks/two-node-deployment-overview.md"
superseded_by: "openspec/specs/single-map-shell-routing/spec.md"
status_since: 2026-06-24
archive_scope: whole-document
retained_for: "audit evidence for the archived M26 inplace overview/basin detail delta"
---

## ADDED Requirements

### Requirement: 全国总览与流域详情在同一页按 basinId 切换

单页地图 SHALL 以 `M11QueryState.basinId`（取自 URL query，非路由 param）决定模式：`basinId` 为空 → 全国总览模式（`loadOverview`，渲染 basin 可见性树与全国图层）；`basinId` 非空 → 流域详情模式（`loadBasinDetail(basinId, query)`，渲染该流域河段/选中河段/趋势/RP，地图 `fitBounds` 至 basin bbox）。模式切换 MUST NOT 触发整页路由跳转（pathname 恒为 `/`）。

#### Scenario: 选中流域进入详情（同页 zoom-in）
- **WHEN** 用户在全国总览点击某 basin 或可见性树"进入分析"
- **THEN** query 写入 `basinId`，单页切流域详情模式并 `fitBounds` 到该 basin bbox，pathname 仍为 `/`

#### Scenario: 返回全国总览
- **WHEN** 处于流域详情模式的用户触发"返回总览"
- **THEN** `basinId` 与 `segmentId` 清空，单页回全国总览模式，pathname 仍为 `/`

#### Scenario: 详情态可分享可后退
- **WHEN** 用户直接访问 `/?basinId=basins_qhh`
- **THEN** 单页直接进入 QHH 详情模式；浏览器后退回到上一 basinId 状态

### Requirement: 数据 store 的 basinId 来源由路由 param 改为 query

`stores/overviewData.ts` 的流域详情取数 MUST 以 query 内 `basinId` 为输入，snapshot 匹配函数 MUST 按 query 内 basinId 判定"当前数据是否匹配当前查询"。store 内部取数键与既有 honest-display 数据契约（latest published run 解析、`flood_product_ready` 门控、ready 过滤、partialErrors、source=best 解析、river-segment 分页上限）MUST 保持不变。

#### Scenario: basinId 来自 query 正确取数
- **WHEN** query `basinId=basins_heihe`
- **THEN** store 加载 Heihe 详情快照，匹配函数判定该快照匹配当前查询，不串到其他流域

#### Scenario: store 内部契约不回归
- **WHEN** 对比改造前后同一 query 的取数键与 ready/partialErrors 判定
- **THEN** 取数键派生与 honest 契约行为一致（仅 basinId 入口从 param 变为 query）

### Requirement: 移除 BasinDetailPage 独立路由

`/basins/:basinId` 路由与 `BasinDetailPage` 组件 SHALL 被移除，其详情面板能力（河段列表、选中河段详情、趋势、对比、RP 三态）MUST 迁入单页（可抽至 `components/m11/`）且行为不丢失。

#### Scenario: 旧 basins 路由不再独立渲染
- **WHEN** 访问 `/basins/basins_qhh`
- **THEN** 重定向到 `/?basinId=basins_qhh` 由单页详情模式承载，无独立 `BasinDetailPage` 渲染

#### Scenario: 详情面板能力保留
- **WHEN** 单页处于某流域详情模式且选中一河段
- **THEN** 河段详情（RP 三态/quality/lineage）、趋势、GFS/IFS 对比等能力与原 BasinDetailPage 一致

### Requirement: 河网/q_down 瓦片 overlay 诚实展示，不伪造

单页地图的河网/q_down/重现期 MVT overlay MUST 沿用 M11 既有诚实展示行为：当 `/api/v1/layers` 注册了对应图层（且具备 run/valid_time）时自动渲染 overlay；当图层未注册或缺 MVT metadata 时 MUST 显示"Layer is not registered"类提示且 MUST NOT 渲染伪造 overlay。当前规模的河网渲染 MAY 由 M11 既有 GeoJSON primitive 兜底。`display_readonly` 环境 MVT 瓦片不可用（river-network 424 / hydro 409）的根因排查 MUST NOT 在本变更内进行（属解耦平行 issue）。

#### Scenario: 图层注册则自动点亮
- **WHEN** `/api/v1/layers` 返回了 discharge/flood-return-period 图层且具备 run/valid_time
- **THEN** 地图渲染对应 MVT overlay

#### Scenario: 图层未注册显示未注册且不伪造
- **WHEN** `/api/v1/layers` 为空或缺该图层 metadata（如当前 node-27 display_readonly）
- **THEN** 显示"Layer is not registered"类 honest 提示，不渲染任何伪造 overlay；河网可由 GeoJSON 兜底渲染
