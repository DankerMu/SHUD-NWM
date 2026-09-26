## MODIFIED Requirements

### Requirement: 旧展示路由收敛/重定向到单页

`/overview`、`/hydro-met`、`/meteorology`、`/forecast`、`/basins/:basinId`、`/segments/:segmentId` SHALL 重定向到单页 `/`。重定向 MUST 用 `replace`（不污染历史回退栈），且 MUST 保留原始 search query（深链状态不丢），同时附加语义映射参数：`/meteorology`→附加 `metStations=1`、`/segments/:segmentId`→附加 `segmentId=:segmentId`。`/basins/:basinId` MUST NOT 附加 `basinId`：流域详情模式已退役（#2109 裁决 B），该路径参数被丢弃，原始 search 里的 `basinId` 键由单页 query 规范化剥离。同名键冲突时 MUST 以原始 search 的值为准（保留用户既有状态）。

#### Scenario: 旧展示路由重定向
- **WHEN** 用户访问 `/overview`、`/hydro-met`、`/forecast` 任一
- **THEN** 浏览器 URL 以 `replace` 落到 `/`，渲染单页地图

#### Scenario: 带语义的重定向保留 query
- **WHEN** 用户访问 `/meteorology`、`/segments/seg_001`
- **THEN** 分别落到 `/?metStations=1`、`/?segmentId=seg_001`

#### Scenario: 旧 basins 路由丢弃路径参数
- **WHEN** 用户访问 `/basins/basins_qhh?source=ifs`
- **THEN** query 规范化之前的重定向目标为 `/?source=ifs`
- **AND** 最终 URL 路径为 `/`、保留 `source=ifs`、不含 `basinId` 键，渲染全国总览

#### Scenario: 深链原始 search 不丢失
- **WHEN** 用户访问带状态的深链（如 `/meteorology?source=IFS&time=2026-06-05T18:00:00Z`）
- **THEN** 重定向落点保留原始 `source`/`time` 等参数并附加 `metStations=1`；若语义映射键与原始 search 同名，取原始 search 的值
- **AND** 重定向落点 MUST NOT 附加已退役的主图层状态 `layer=met-stations`

#### Scenario: 缺 basin 上下文的 segment 深链 honest 处理
- **WHEN** 用户访问 `/segments/:segmentId` 但无法从 query 解析出所属 basin
- **THEN** 落到 `/?segmentId=:segmentId`，单页以 honest 空态提示需选择流域，MUST NOT 伪造选中河段或乱指流域
