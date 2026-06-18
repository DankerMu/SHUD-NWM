## ADDED Requirements

### Requirement: 展示端为单一全屏地图入口，无顶部导航

展示端 SHALL 以根路由 `/` 渲染单一全屏地图页（`DisplayMapPage`），且 `AppShell` MUST NOT 再渲染顶部 `NavBar`（全国总览/水文气象/气象数据/水文预报/洪水预警/产品监控 等导航链接）。地图区域 SHALL 占满视口高度（去除 `--m11-nav-height` 预留）。

#### Scenario: 根路由渲染全屏单页地图
- **WHEN** 用户访问 `/`
- **THEN** 渲染单页地图（`M11Layout`/`m11-shell` 在场），页面 MUST NOT 含顶部 `NavBar` 导航链接

#### Scenario: 去导航后地图全屏
- **WHEN** 单页地图渲染
- **THEN** 布局高度 MUST 基于全视口（`--m11-nav-height` 归 0），不因移除导航而留白或塌陷

### Requirement: 旧展示路由收敛/重定向到单页

`/overview`、`/hydro-met`、`/meteorology`、`/forecast`、`/flood-alerts`、`/basins/:basinId`、`/segments/:segmentId` SHALL 重定向到单页 `/`。重定向 MUST 用 `replace`（不污染历史回退栈），且 MUST 保留原始 search query（深链状态不丢），同时附加语义映射参数：`/meteorology`→附加 `layer=met-stations`、`/flood-alerts`→附加 `layer=flood-return-period`、`/basins/:basinId`→附加 `basinId=:basinId`、`/segments/:segmentId`→附加 `segmentId=:segmentId`。同名键冲突时 MUST 以原始 search 的值为准（保留用户既有状态）。

#### Scenario: 旧展示路由重定向
- **WHEN** 用户访问 `/overview`、`/hydro-met`、`/forecast` 任一
- **THEN** 浏览器 URL 以 `replace` 落到 `/`，渲染单页地图

#### Scenario: 带语义的重定向保留 query
- **WHEN** 用户访问 `/meteorology`、`/flood-alerts`、`/basins/basins_qhh`、`/segments/seg_001`
- **THEN** 分别落到 `/?layer=met-stations`、`/?layer=flood-return-period`、`/?basinId=basins_qhh`、`/?segmentId=seg_001`

#### Scenario: 深链原始 search 不丢失
- **WHEN** 用户访问带状态的深链（如 `/meteorology?source=IFS&time=2026-06-05T18:00:00Z`）
- **THEN** 重定向落点保留原始 `source`/`time` 等参数并附加 `layer=met-stations`；若语义映射键与原始 search 同名，取原始 search 的值

#### Scenario: 缺 basin 上下文的 segment 深链 honest 处理
- **WHEN** 用户访问 `/segments/:segmentId` 但无法从 query 解析出所属 basin
- **THEN** 落到 `/?segmentId=:segmentId`，单页以 honest 空态提示需选择流域，MUST NOT 伪造选中河段或乱指流域

### Requirement: 运维路由保留且 RBAC 不变

`/ops`、`/monitoring`、`/system/model-assets` SHALL 保持可达，且其 `RBACGate` 角色门控（operator/model_admin/sys_admin 等）MUST 与本变更前一致；这些路由 MUST NOT 被重定向到单页。

#### Scenario: 运维路由仍受 RBAC 门控
- **WHEN** 具备 operator 角色的用户访问 `/ops`
- **THEN** 渲染运维页（非重定向到 `/`），RBAC 行为与变更前一致

#### Scenario: 无权限用户被 RBAC 拒绝
- **WHEN** 无运维角色的用户访问 `/monitoring`
- **THEN** 仍按既有 `RBACGate` 行为拒绝，不因去导航而放宽
