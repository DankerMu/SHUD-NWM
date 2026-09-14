# inplace-overview-basin-detail Specification

## Purpose
TBD - created by archiving change m26-unified-map-display. Update Purpose after archive.
## Requirements
### Requirement: 移除 BasinDetailPage 独立路由

`/basins/:basinId` 路由与 `BasinDetailPage` 组件 SHALL 被移除，且单页 SHALL NOT 提供任何流域详情模式：流域详情能力（流域级河段列表/河段层、选中河段详情、趋势、对比、lineage）已退役（#2109 裁决 B），其用户价值由全国总览的河段点击流量弹窗与代站弹窗就地承载。`/basins/:basinId` 仅作为 legacy redirect 保留，落到全国总览且不携带 `basinId`；`basinId` 不再是单页 query state 的键。

#### Scenario: 旧 basins 路由落到全国总览
- **WHEN** 访问 `/basins/basins_qhh?source=ifs&validTime=2026-05-18T06:00:00Z`
- **THEN** query 规范化之前的重定向目标为 `/?source=ifs&validTime=2026-05-18T06:00:00Z`（丢弃路径参数、保留其余 query）；最终 URL 路径为 `/`、保留 `source`、不含 `basinId`（`validTime` 按全国时次校正规则处理），渲染全国总览，无流域详情模式、无独立 `BasinDetailPage` 渲染

#### Scenario: 旧 basinId query 被规范化剥离
- **WHEN** 访问 `/?basinId=basins_qhh&layer=discharge`
- **THEN** URL 以 `replace` 规范化为不含 `basinId` 的 query，渲染全国总览（地图标签为「全国总览地图」、底部控制条在场、无返回总览按钮），且单页与 store 中不存在按 `basinId` 取数的入口

### Requirement: 河网/q_down 瓦片 overlay 诚实展示，不伪造

单页地图的河网/q_down MVT overlay MUST 沿用 M11 既有诚实展示行为：当 `/api/v1/layers` 注册了对应图层（且具备 run/valid_time）时自动渲染 overlay；当图层未注册或缺 MVT metadata 时 MUST 显示"Layer is not registered"类提示且 MUST NOT 渲染伪造 overlay。当前规模的河网渲染 MAY 由 M11 既有 GeoJSON primitive 兜底。`display_readonly` 环境 MVT 瓦片不可用（river-network 424 / hydro 409）的根因排查 MUST NOT 在本变更内进行（属解耦平行 issue）。

#### Scenario: 图层注册则自动点亮
- **WHEN** `/api/v1/layers` 返回了 discharge 图层且具备 run/valid_time
- **THEN** 地图渲染对应 MVT overlay

#### Scenario: 图层未注册显示未注册且不伪造
- **WHEN** `/api/v1/layers` 为空或缺该图层 metadata（如当前 node-27 display_readonly）
- **THEN** 显示"Layer is not registered"类 honest 提示，不渲染任何伪造 overlay；河网可由 GeoJSON 兜底渲染

