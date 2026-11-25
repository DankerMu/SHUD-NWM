# multi-source-comparison-ui Specification

## Purpose

How GFS and IFS forecast curves are drawn together for one river segment: colours, line styles, the legend markers,
tooltip and axes, the IFS short-horizon annotation, and the multi-source metadata the API response carries. Source
selection and the issue-time selector are not part of it: the river curve window always shows both sources, and
`map-feature-popups` states the selector.

## Requirements

### Requirement: IFS 06/18 周期可用时效标注

系统 SHALL 满足「IFS 06/18 周期可用时效标注」要求。

06/18 周期的 IFS 预报仅有 6 天（144h），需在 UI 上明确标注。

#### Scenario: 06/18 周期标注

WHEN 当前显示的 IFS 预报来自 06 或 18 UTC 周期
AND API response 中该 series 的 `available_lead_hours` = 144
THEN IFS 曲线在第 144h（第 6 天）处终止
AND 曲线末端显示虚线垂直标注线
AND 标注文字："IFS 6d"（紧凑标签）

#### Scenario: 00/12 周期无标注

WHEN 当前显示的 IFS 预报来自 00 或 12 UTC 周期
THEN IFS 曲线延伸至第 168h（第 7 天），与 GFS 等长
AND 不显示时效标注

#### Scenario: GFS 不受影响

WHEN IFS 曲线显示 6d 标注
THEN GFS 曲线不受影响，始终显示完整 7 天

### Requirement: API Response 多源元数据

系统 SHALL 满足「API Response 多源元数据」要求。

API response 的每个 series 元素需携带足够的元数据供前端渲染。

#### Scenario: per-series 元数据字段

WHEN API 返回多 scenario 的 forecast-series response
THEN 每个 series 元素包含：
  - `scenario_id`: 场景标识（如 `"forecast_gfs_deterministic"`）
  - `source_id`: 数据源（如 `"GFS"`, `"IFS"`）
  - `cycle_time`: 该 scenario 使用的起报时刻
  - `available_lead_hours`: 该 scenario 的最大可用预报时效（GFS=168, IFS 00/12=168, IFS 06/18=144）
  - `segment_role`: `"past_3_days"` 或 `"future_7_days"`

#### Scenario: multi-source latest 策略

WHEN issue_time=latest 且 scenarios=GFS,IFS
THEN 后端为每个 source 独立选取最新可用周期（per-source latest）
AND 不要求所有 source 使用同一 cycle_time
AND response 中每个 series 的 cycle_time 可能不同

### Requirement: GFS 与 IFS 预报曲线同图渲染

河段曲线窗的图表（`apps/frontend/src/components/charts/ForecastChart.tsx`，由
`apps/frontend/src/components/map/M11RiverForecastPanel.tsx` 以 compact 形态使用）SHALL 在同一时间轴上渲染
GFS 与 IFS 两条 `q_down` 预报曲线：GFS 为实线 `#22d3ee`，IFS 为虚线 `#34d399`。
曲线窗 MUST NOT 请求 analysis 曲线：forecast-series 请求带 `include_analysis=false`，scenario 不是所请求预报 scenario 的
series 使该源校验失败、不画曲线（见 `apps/frontend/src/lib/hydroMet/riverForecast.ts`）。compact 形态不渲染图表标题和 echarts 图例；
图例是曲线窗内图表上方的一行源标记。

#### Scenario: 双曲线配色与线型

- **WHEN** 图表数据含 GFS 与 IFS 两条 series
- **THEN** GFS 曲线为 `#22d3ee` 实线，IFS 曲线为 `#34d399` 虚线

#### Scenario: 图例为源标记行

- **WHEN** 曲线窗渲染出图表
- **THEN** 图表上方显示 GFS 与 IFS 两个源标记，各带该源颜色的色块
- **AND** 图表自身不渲染 echarts 图例

#### Scenario: 只有一个源有曲线

- **WHEN** 只有一个源的 series 可画
- **THEN** 图表只画该源的曲线
- **AND** 另一个源的标记保留并加删除线（降级规则见 `map-feature-popups`）

#### Scenario: Tooltip 按行列出各源

- **WHEN** 用户悬浮在图表某一时间点
- **THEN** tooltip 第一行为 `时间: <YYYY-MM-DD HH:mm>`（UTC），其后每条在该时刻有值的曲线各占一行 `<色标><源>: <值> <单位>`，值保留两位小数
- **AND** 单位为图表数据的单位，按上标显示（`m3/s` 显示为 `m³/s`）

#### Scenario: 坐标轴

- **WHEN** 图表渲染
- **THEN** 时间轴标签为 UTC 的 `MM-DD HH:00`
- **AND** 纵轴名称为 `流量 (m³/s)`
