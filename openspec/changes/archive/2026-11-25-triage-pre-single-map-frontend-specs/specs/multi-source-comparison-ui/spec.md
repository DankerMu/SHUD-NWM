# multi-source-comparison-ui

## REMOVED Requirements

### Requirement: 多源曲线渲染

**Reason**: 图表不请求也不绘制 analysis 曲线；配色、图例形式与 tooltip 格式都与实现不符；缺失源的图例项是保留并加删除线，而不是隐藏。

**Migration**: 由下面的 "GFS 与 IFS 预报曲线同图渲染" 取代。

### Requirement: 起报时刻信息增强

**Reason**: 曲线窗只有一个对两个源同步生效的起报时次选择器，没有按源分别标注的起报时刻。

**Migration**: `map-feature-popups` 中关于起报时次选择器的各条 requirement。

## ADDED Requirements

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
