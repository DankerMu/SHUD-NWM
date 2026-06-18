# map-feature-popups Specification

## Purpose
TBD - created by archiving change m26-unified-map-display. Update Purpose after archive.
## Requirements
### Requirement: 点击河段要素弹出 q_down 预报曲线 + 重现期三态

点击地图河段要素 SHALL 弹出 maplibre `Popup`，按要素 `river_segment_id` 经 `loadHydroMetRiverForecast` + `validateHydroMetRiverForecastForChart` 拉取并校验 `q_down` forecast-series，校验通过则渲染 q_down 曲线（echarts `ForecastChart`）与洪水重现期三态（`ReturnPeriodSection`）。身份/契约校验失败（`ok:false`）时 popup MUST 显示原因空态，MUST NOT 绘制曲线（不画假曲线红线）。

#### Scenario: 河段曲线正常渲染
- **WHEN** 点击河段要素且其 forecast-series 通过严格身份与 chart 校验
- **THEN** popup 渲染 q_down 曲线 + 重现期状态（ready/unavailable 按 `return_period_status`）

#### Scenario: 身份不符不画曲线
- **WHEN** 河段 forecast-series 缺任一身份字段或 horizon/point 预算不符（`ok:false`）
- **THEN** popup 显示不可用原因空态，不绘制任何 q_down 曲线

### Requirement: 点击代站弹出六要素 forcing 曲线

点击代站点要素 SHALL 弹出 maplibre `Popup`，按 `station_id` 经 `loadHydroMetStationSeries` + `validateHydroMetStationSeriesIdentity` 拉取并校验，渲染六要素 echarts 曲线（PRCP/TEMP/RH/wind/Rn/Press）。身份不符时 MUST 显示空态而非伪造曲线。

#### Scenario: 代站六要素曲线渲染
- **WHEN** 点击代站点且其 station-series 通过身份校验
- **THEN** popup 渲染六个 forcing 变量的 echarts 曲线

#### Scenario: 代站身份不符空态
- **WHEN** station-series 的 station_id/forcing_version_id/source/cycle_time 与选中产品身份不一致
- **THEN** popup 显示身份不符空态，不绘制曲线

### Requirement: popup 源解析与 honest-display 不变量

popup 拉曲线 MUST 使用解析后的具体源（`best`/`compare` → `sourceSelection.resolvedSource` 落为 GFS/IFS）；未解析时显示"等待 Best Available 解析"空态。`productReady` 门控、`return_period_status` 三态、strict identity 等 honest-display 不变量 MUST 在 popup 中保持。

#### Scenario: best 未解析时 popup 不打具体源接口
- **WHEN** 源为 best 尚未解析
- **THEN** popup 显示"等待 Best Available 解析"空态，不以 `best` 调 station/river forecast 接口

#### Scenario: productReady 门控贯穿 popup
- **WHEN** 产品整体 `availability.ready` 为 false
- **THEN** popup 内 return-period 维度按 `productReady` 门控判为不可用，与既有红线一致

