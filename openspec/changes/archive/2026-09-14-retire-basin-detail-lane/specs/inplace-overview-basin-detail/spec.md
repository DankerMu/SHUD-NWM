## MODIFIED Requirements

### Requirement: 移除 BasinDetailPage 独立路由

`/basins/:basinId` 路由与 `BasinDetailPage` 组件 SHALL 被移除，且单页 SHALL NOT 提供任何流域详情模式：流域详情能力（流域级河段列表/河段层、选中河段详情、趋势、对比、lineage）已退役（#2109 裁决 B），其用户价值由全国总览的河段点击流量弹窗与代站弹窗就地承载。`/basins/:basinId` 仅作为 legacy redirect 保留，落到全国总览且不携带 `basinId`；`basinId` 不再是单页 query state 的键。

#### Scenario: 旧 basins 路由落到全国总览
- **WHEN** 访问 `/basins/basins_qhh?source=ifs&validTime=2026-05-18T06:00:00Z`
- **THEN** query 规范化之前的重定向目标为 `/?source=ifs&validTime=2026-05-18T06:00:00Z`（丢弃路径参数、保留其余 query）；最终 URL 路径为 `/`、保留 `source`、不含 `basinId`（`validTime` 按全国时次校正规则处理），渲染全国总览，无流域详情模式、无独立 `BasinDetailPage` 渲染

#### Scenario: 旧 basinId query 被规范化剥离
- **WHEN** 访问 `/?basinId=basins_qhh&layer=discharge`
- **THEN** URL 以 `replace` 规范化为不含 `basinId` 的 query，渲染全国总览（地图标签为「全国总览地图」、底部控制条在场、无返回总览按钮），且单页与 store 中不存在按 `basinId` 取数的入口

## REMOVED Requirements

### Requirement: 全国总览与流域详情在同一页按 basinId 切换
**Reason**: 流域详情模式按构造不可达且已被产品演进抛弃（#2109 裁决 B）：点流域只做相机 fit、留在全国总览，全国总览已就地提供河段流量弹窗、代站弹窗、底部控制条与降水叠加。
**Migration**: 使用全国总览 `/`：点流域飞到该流域范围，点河段打开流量预报弹窗；旧 `?basinId=` 链接自动落到全国总览（见「移除 BasinDetailPage 独立路由」）。

### Requirement: 数据 store 的 basinId 来源由路由 param 改为 query
**Reason**: `loadBasinDetail` 及其 `basinDetail`/`basinLoading`/`basinError` 切片随流域详情模式一并删除，store 不再有按 `basinId` 取数的入口（#2109 裁决 B）。
**Migration**: 无需迁移：全国总览经 `loadOverview` 取数，河段/代站弹窗按要素自带的 `basin_id` 取 latest-product。
