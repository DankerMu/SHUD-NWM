## MODIFIED Requirements

### Requirement: 点击河段要素弹出 q_down 预报曲线

点击地图河段要素 SHALL 打开 M11 可拖拽河段曲线窗，按要素 `river_segment_id` 经 `loadHydroMetRiverForecast` + `validateHydroMetRiverForecastForChart` 拉取并校验 `q_down` forecast-series，校验通过则渲染 q_down 曲线（echarts `ForecastChart`）。身份/契约校验失败（`ok:false`）时 popup MUST 显示原因空态，MUST NOT 绘制曲线（不画假曲线红线）。
该河段曲线容器 MUST use the same draggable curve-window contract as other M11 curve windows, rather than a MapLibre geographic `Popup` container; "popup" below names that curve window.

#### Scenario: 河段曲线正常渲染
- **WHEN** 点击河段要素且其 forecast-series 通过严格身份与 chart 校验
- **THEN** popup 渲染 q_down 曲线
- **AND** the window is draggable by its header or drag handle

#### Scenario: 身份不符不画曲线
- **WHEN** 河段 forecast-series 缺任一身份字段或 horizon/point 预算不符（`ok:false`）
- **THEN** popup 显示不可用原因空态，不绘制任何 q_down 曲线

### Requirement: 点击代站弹出当前 station-series forcing 曲线

点击代站点要素 SHALL 打开 M11 可拖拽代站曲线窗，按 `station_id` 经 `loadHydroMetStationSeries` + `validateHydroMetStationSeriesIdentity` 拉取并校验，渲染当前 station-series route 可返回的 echarts 曲线（PRCP/TEMP/RH/wind/Rn）。
`Press` 不得被当作当前 route 的可用曲线；若 UI 暴露该变量，MUST 显示 unavailable/omitted 状态。身份不符时 MUST 显示空态而非伪造曲线。
当前 disk-backed route 的阻断身份字段为 `station_id`、`model_id`、`source_id` 和 `cycle_time`；`forcing_version_id` 是 deprecated/non-blocking provenance，不得单独作为 popup 身份 mismatch gate。
该代站曲线容器 MUST use the same draggable curve-window contract as other M11 curve windows, rather than a MapLibre geographic `Popup` container; "popup" below names that curve window.

#### Scenario: 代站当前变量曲线渲染
- **WHEN** 点击代站点且其 station-series 通过身份校验
- **THEN** popup 渲染 `PRCP`、`TEMP`、`RH`、`wind`、`Rn` 的 echarts 曲线
- **AND** popup 不为 `Press` 绘制可用曲线，除非未来 route 明确重新提供该变量
- **AND** the window is draggable by its header or drag handle

#### Scenario: 代站身份不符空态
- **WHEN** station-series 的 station_id/model_id/source_id/cycle_time 与选中产品身份不一致
- **THEN** popup 显示身份不符空态，不绘制曲线

## ADDED Requirements

### Requirement: Forecast issue-time selectors use dark popup surfaces

M11 river forecast and station forcing popups SHALL render forecast issue-time
choices in a controlled dark popup surface whose background, text, hover,
focus, selected, and disabled states remain legible in the dark glass popup
theme across supported browsers.

#### Scenario: River cycle menu is dark and legible

- **WHEN** a user opens the river q_down forecast panel and opens the issue-time
  selector
- **THEN** the selector content MUST use a dark surface consistent with the
  curve popup theme
- **AND** the active, hover, focus, and selected option states MUST remain
  readable without a white native option backdrop.

#### Scenario: Station cycle menu is dark and legible

- **WHEN** a user opens the station forcing popup and opens the issue-time
  selector
- **THEN** the selector content MUST use the same dark issue-time selector
  behavior as the river panel
- **AND** changing the issue time MUST continue to reload both GFS and IFS
  station series for the selected variable.

#### Scenario: Disabled retained-window options remain honest

- **WHEN** an issue-time option is unavailable because the retained disk window
  no longer has that cycle
- **THEN** the option MUST be disabled or otherwise non-selectable
- **AND** the unavailable reason MUST remain visible in the dark selector
  without reducing contrast below readable levels.

### Requirement: River and station curve windows can coexist and move

M11 river q_down forecast windows and station forcing windows SHALL be
independent curve windows. Opening one type MUST NOT close the other type, and
each visible window SHALL be draggable by its header or drag handle so users can
compare river flow and forcing-station variables on the same map.

#### Scenario: River then station leaves both windows open

- **WHEN** a user opens a river q_down forecast window
- **AND** then clicks a meteorological station point
- **THEN** the station forcing window opens
- **AND** the river forecast window remains visible until the user closes it.

#### Scenario: Station then river leaves both windows open

- **WHEN** a user opens a station forcing window
- **AND** then clicks a river segment that is not covered by a station symbol
- **THEN** the river q_down forecast window opens
- **AND** the station forcing window remains visible until the user closes it.

#### Scenario: Closing one window does not close the other

- **WHEN** both river and station curve windows are visible
- **AND** the user activates the close control on one window
- **THEN** only that window closes
- **AND** the other window remains visible with its selected feature and chart
  state intact.

#### Scenario: Dragging a window repositions within the map viewport

- **WHEN** a user drags a curve window by its header or drag handle
- **THEN** the window moves with the pointer
- **AND** the final position is clamped so the title, close control, and enough
  chart area remain reachable inside the map viewport.

#### Scenario: Dual windows initially avoid perfect overlap

- **WHEN** both river and station curve windows become visible on a desktop
  viewport
- **THEN** their default positions MUST avoid perfect overlap so both windows
  are discoverable
- **AND** on narrow viewports the windows MUST fall back to clamped positions
  that keep headers and close controls reachable.

#### Scenario: Active window rises above the other

- **WHEN** both curve windows are visible
- **AND** the user focuses, clicks, or drags one window
- **THEN** that window MUST render above the other window
- **AND** the inactive window MUST remain visible and usable.

#### Scenario: Chart interactions do not start window drag

- **WHEN** a user interacts with the chart body, tooltip, data zoom, tabs, or
  issue-time selector inside a curve window
- **THEN** those interactions MUST keep their chart/control behavior
- **AND** they MUST NOT unintentionally start dragging the window.
