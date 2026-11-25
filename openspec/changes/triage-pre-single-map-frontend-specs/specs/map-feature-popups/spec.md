# map-feature-popups

## REMOVED Requirements

### Requirement: 点击河段要素弹出 q_down 预报曲线

**Reason**: 场景 "身份不符不画曲线" 规定任一 `ok:false` 即整窗空态；实现按源独立加载，一个源失败时仍画另一个源。

**Migration**: 由下面的 "点击河段要素弹出按源独立加载的 q_down 预报曲线窗" 取代。

### Requirement: popup 源解析与 honest-display 不变量

**Reason**: 两个曲线窗都固定加载 GFS 与 IFS，不存在以 `best` 解析单一源的路径；`productReady` 门控在前端已不存在。

**Migration**: 源的选择见 "Curve windows SHALL NOT offer a forecast-source switch"；严格身份校验见河段与代站两条 requirement；代站窗的等待空态见下面的 "代站曲线窗在源未解析时不发请求"。

## ADDED Requirements

### Requirement: 点击河段要素弹出按源独立加载的 q_down 预报曲线窗

点击地图河段要素 SHALL 打开 M11 河段曲线窗（桌面形态为可拖拽窗，移动形态为 `mobile-curve-sheet` 定义的抽屉；
实现为 `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`）。曲线窗 SHALL 对 GFS 与 IFS 两个源各自独立地
取 latest-product 身份（`fetchHydroMetLatestProduct`），再按要素 `river_segment_id` 经 `loadHydroMetRiverForecast` +
`validateHydroMetRiverForecastForChart` 拉取并校验 `q_down` forecast-series。
通过校验的源各画一条曲线（echarts `ForecastChart`）；任一步失败的源 MUST NOT 画曲线（不画假曲线红线），并保留一条
`<源>：<原因>` 形式的原因。至少一个源通过时曲线窗 SHALL 渲染图表，并在图表下方列出未通过的源的原因；
两个源都未通过时 SHALL 显示逐条列出原因的空态，不绘制任何曲线。
该河段曲线容器 MUST use the same curve-window contract as other M11 curve windows — draggable in desktop form
(viewport at least 768 CSS px wide and at least 500 CSS px high), a non-draggable sheet in mobile form — rather than
a MapLibre geographic `Popup` container; "popup" below names that curve window.

#### Scenario: 双源曲线正常渲染

- **WHEN** 点击河段要素且 GFS 与 IFS 的 forecast-series 都通过严格身份与 chart 校验
- **THEN** popup 在同一图表内渲染两条 q_down 曲线
- **AND** in desktop form the window is draggable by its header or drag handle

#### Scenario: 一个源失败时仍画另一个源

- **WHEN** 一个源的 forecast-series 校验失败（`ok:false`）或请求失败，而另一个源通过
- **THEN** 图表只画通过的源的曲线
- **AND** 失败源的图例标记加删除线
- **AND** 图表下方（`m11-river-panel-partial`）列出失败源的原因

#### Scenario: 两个源都失败时不画曲线

- **WHEN** 两个源都缺身份字段、horizon/point 预算不符或请求失败
- **THEN** popup 显示空态（`m11-river-panel-empty`），逐条列出两个源的原因，不绘制任何 q_down 曲线

#### Scenario: 所选起报时次已不可用

- **WHEN** 用户选了一个起报时次，而某个源的 latest-product 返回的是另一个时次
- **THEN** 该源不画曲线，其原因为 `<源>：起报 <所选时次> 已不可用`
- **AND** 不以返回的另一个时次的数据代替

### Requirement: 代站曲线窗在源未解析时不发请求

代站曲线窗（`apps/frontend/src/components/map/M11StationForcingPopup.tsx`）在页面尚未给出已解析的源时 SHALL 显示
等待空态，MUST NOT 请求 station-series。源解析后它固定加载 GFS 与 IFS 两个源。

#### Scenario: 源未解析

- **WHEN** 代站曲线窗打开而页面的源尚未解析
- **THEN** 窗内显示 `等待 GFS/IFS 源解析后加载 station-series`
- **AND** 不发出 station-series 请求
