# map-feature-popups Specification

## Purpose

The two curve windows the single map page opens from a map feature: the river window (GFS and IFS `q_down` forecast
curves of a river segment) and the station window (forcing curves of a met station). It states what each loads and
validates, how a failing source degrades, the issue-time selector, and how the windows are placed, moved and
coexist in desktop form. The mobile sheet form is stated by `mobile-curve-sheet`.

## Requirements

### Requirement: 点击代站弹出当前 station-series forcing 曲线

点击代站点要素 SHALL 打开 M11 代站曲线窗（桌面形态为可拖拽窗，移动形态为 `mobile-curve-sheet` 定义的抽屉），按 `station_id` 经 `loadHydroMetStationSeries` + `validateHydroMetStationSeriesIdentity` 拉取并校验，渲染当前 station-series route 可返回的 echarts 曲线（PRCP/TEMP/RH/wind/Rn）。
`Press` 不得被当作当前 route 的可用曲线；若 UI 暴露该变量，MUST 显示 unavailable/omitted 状态。身份不符时 MUST 显示空态而非伪造曲线。
当前 disk-backed route 的阻断身份字段为 `station_id`、`model_id`、`source_id` 和 `cycle_time`；`forcing_version_id` 是 deprecated/non-blocking provenance，不得单独作为 popup 身份 mismatch gate。
该代站曲线容器 MUST use the same curve-window contract as other M11 curve windows — draggable in desktop form, a non-draggable sheet in mobile form — rather than a MapLibre geographic `Popup` container; "popup" below names that curve window.

#### Scenario: 代站当前变量曲线渲染
- **WHEN** 点击代站点且其 station-series 通过身份校验
- **THEN** popup 渲染 `PRCP`、`TEMP`、`RH`、`wind`、`Rn` 的 echarts 曲线
- **AND** popup 不为 `Press` 绘制可用曲线，除非未来 route 明确重新提供该变量
- **AND** in desktop form the window is draggable by its header or drag handle

#### Scenario: 代站身份不符空态
- **WHEN** station-series 的 station_id/model_id/source_id/cycle_time 与选中产品身份不一致
- **THEN** popup 显示身份不符空态，不绘制曲线

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

- **WHEN** the selected issue time is no longer among the available issue times
  and is shown as retained-disk unavailable
- **THEN** the option MUST be disabled or otherwise non-selectable
- **AND** the unavailable reason MUST remain visible in the dark selector
  without reducing contrast below readable levels.

### Requirement: River and station curve windows can coexist and move

In desktop form (viewport at least 768 CSS px wide and at least 500 CSS px high)
M11 river q_down forecast windows and station forcing windows SHALL be
independent curve windows. Opening one type MUST NOT close the other type, and
each visible window SHALL be draggable by its header or drag handle so users can
compare river flow and forcing-station variables on the same map. In mobile form
the windows SHALL follow the `mobile-curve-sheet` capability instead: they are
not draggable and only one is open at a time.

#### Scenario: River then station leaves both windows open

- **WHEN** a user in desktop form opens a river q_down forecast window
- **AND** then clicks a meteorological station point
- **THEN** the station forcing window opens
- **AND** the river forecast window remains visible until the user closes it.

#### Scenario: Station then river leaves both windows open

- **WHEN** a user in desktop form opens a station forcing window
- **AND** then clicks a river segment that is not covered by a station symbol
- **THEN** the river q_down forecast window opens
- **AND** the station forcing window remains visible until the user closes it.

#### Scenario: Closing one window does not close the other

- **WHEN** both river and station curve windows are visible in desktop form
- **AND** the user activates the close control on one window
- **THEN** only that window closes
- **AND** the other window remains visible with its selected feature and chart
  state intact.

#### Scenario: Dragging a window repositions within the map viewport

- **WHEN** a user in desktop form drags a curve window by its header or drag handle
- **THEN** the window moves with the pointer
- **AND** the final position is clamped so the title, close control, and enough
  chart area remain reachable inside the map viewport.

#### Scenario: Dual windows initially avoid perfect overlap

- **WHEN** both river and station curve windows become visible on a desktop
  viewport
- **THEN** their default positions MUST avoid perfect overlap so both windows
  are discoverable
- **AND** on narrow desktop-form viewports the windows MUST fall back to clamped
  positions that keep headers and close controls reachable.

#### Scenario: Overlapping default positions keep the river window's close control clear

- **WHEN** both curve windows are opened at their default positions in desktop
  form with a map area at least 900px wide and the two default horizontal
  extents overlap
- **THEN** the default positions are vertically staggered, the river window
  above the station window, so that the river window's close control lies
  outside the station window's box and a hit test on it reaches the river
  window even when the station window was opened last

#### Scenario: Non-overlapping default positions share one top offset

- **WHEN** both curve windows are opened at their default positions in desktop
  form with a map area at least 900px wide and the two default horizontal
  extents do not overlap
- **THEN** both windows have the same default top offset and are anchored
  horizontally at 0.28 and 0.72 of the map width

#### Scenario: Active window rises above the other

- **WHEN** both curve windows are visible in desktop form
- **AND** the user focuses, clicks, or drags one window
- **THEN** that window MUST render above the other window
- **AND** the inactive window MUST remain visible and usable.

#### Scenario: Chart interactions do not start window drag

- **WHEN** a user interacts with the chart body, tooltip, data zoom, tabs, or
  issue-time selector inside a curve window
- **THEN** those interactions MUST keep their chart/control behavior
- **AND** they MUST NOT unintentionally start dragging the window.

#### Scenario: Mobile form does not apply dual-window rules

- **WHEN** the viewport is in mobile form
- **THEN** curve windows are sheets governed by `mobile-curve-sheet`, at most one
  is open, and none is draggable.

### Requirement: Desktop-form curve windows have a minimum width

In desktop form a curve window's default width SHALL be the smaller of 44rem and the larger of 42% of the viewport width and 30rem, with its existing 16:9 proportion, and SHALL still be clamped inside the map region. At viewport widths of 1143 CSS px and above the default size SHALL equal the size before this change.

#### Scenario: Tablet portrait

- **WHEN** a river window is opened at 768×1024
- **THEN** the window is 480px wide and 270px high and lies inside the map region

#### Scenario: Wide desktop unchanged

- **WHEN** a river window is opened at 1280×900
- **THEN** the window's width is 42% of the viewport width

### Requirement: The issue-time trigger SHALL keep its value on one line inside its box

The issue-time trigger in a curve window (river and station) SHALL render the selected issue time on a single line that never extends beyond the trigger's box, in desktop and mobile form. When the label does not fit, it SHALL be truncated with an ellipsis, the date-time part (`MM-DD HH:MM UTC`) SHALL remain fully visible at every supported sheet width, and the full label SHALL remain available as the trigger's `title`. The trigger's height SHALL NOT depend on the selected label.

In the river window's issue-time bar, the trigger SHALL NOT be narrowed by the caption next to it: when both do not fit on one line, the caption SHALL be the one that is not shown, and the bar SHALL stay a single row of unchanged height.

#### Scenario: Retained issue time in a portrait sheet

- **WHEN** a curve sheet at 390×664 shows an issue time that is no longer in the available list, labelled with the retention-unavailable suffix
- **THEN** the trigger is 44px high, its content does not overflow it vertically or horizontally, the date-time part is not elided, and the trigger's `title` is the full label

#### Scenario: Retained issue time in a desktop curve window

- **WHEN** the river curve window at 1280×800 shows a retained issue time
- **THEN** the trigger is 28px high, its content does not overflow it, and the trigger's `title` is the full label

#### Scenario: Narrow river sheet

- **WHEN** the river sheet is shown at 568×320 or 320×568 with an ordinary issue time
- **THEN** the issue-time bar is a single row, the trigger shows the whole label without elision, and the caption is either on one line beside the trigger or not shown

#### Scenario: Caption stays where it fits

- **WHEN** the river sheet is shown at 750×342 with an ordinary issue time
- **THEN** the caption is visible on one line beside the trigger

### Requirement: Curve windows SHALL NOT offer a forecast-source switch

The river curve window SHALL show its forecast sources together in one chart and SHALL NOT render a control for switching between forecast sources; the station curve window SHALL NOT render a forecast-source switch either.

#### Scenario: River window has no source switch

- **WHEN** the river curve window is open and its issue times have been resolved
- **THEN** it contains an issue-time selector and no button for choosing GFS or IFS

#### Scenario: Station window has no source switch

- **WHEN** the station curve window is open and its issue times have been resolved
- **THEN** it contains an issue-time selector and no button for choosing GFS or IFS

### Requirement: Curve windows SHALL resolve their issue-time options without a shared popup product hook

The river forecast window and the station forcing window SHALL each obtain the latest-product identity and its available issue times directly from the latest-product fetch, and the frontend SHALL NOT keep a separate popup product hook that requests the same endpoint without a caller.

#### Scenario: A curve window opens

- **WHEN** a river forecast window or a station forcing window opens for a source
- **THEN** the window itself requests the latest-product identity for that source and builds its issue-time selector from the returned available issue times

### Requirement: Station popup issue-time picker handles retained-disk misses

The M11 station forcing popup SHALL treat station-series
`STATION_FORCING_FILE_NOT_FOUND` responses as retained-disk unavailable cycles,
not as generic chart failures and not as permission to fall back to DB history.

#### Scenario: retained-out cycle shows retention-specific empty state

- **WHEN** a user selects an issue time whose latest-product identity exists but
  whose station-series disk file has rotated out of the retained object-store
  window
- **AND** `/api/v1/met/stations/{station_id}/series` returns
  `STATION_FORCING_FILE_NOT_FOUND`
- **THEN** the station popup SHALL render a clear unavailable state for the
  selected retained-disk cycle
- **AND** it SHALL NOT draw station forcing charts for that response.

#### Scenario: a retained-out issue time stays selectable

- **WHEN** an issue time has failed with `STATION_FORCING_FILE_NOT_FOUND` for
  the current station/source popup session and is still among the available
  issue times
- **THEN** the issue-time picker SHALL keep that option selectable without a
  session-level unavailable mark, and selecting it again SHALL show the same
  retention-specific state
- **AND** selecting a different still-available issue time SHALL refetch
  latest-product and station-series normally.

#### Scenario: backend direct-disk contract is unchanged

- **WHEN** the frontend handles a retained-disk miss
- **THEN** it SHALL keep calling the existing direct-disk station-series route
- **AND** it SHALL NOT introduce DB fallback, archive/history reads, or synthetic
  station-series points.

### Requirement: The issue-time selector SHALL mark only a retained selected issue time as unavailable

The curve-window issue-time selector SHALL NOT mark any issue time other than a selected, no-longer-listed one as retained-disk unavailable; every listed issue time SHALL stay selectable, including one that returned a retained-disk miss earlier in the session.

#### Scenario: A listed issue time missed on disk earlier in the session

- **WHEN** an issue time that is still among the available issue times returned a retained-disk miss earlier in the session
- **THEN** its option is shown without the unavailable label and can be selected again

#### Scenario: The selected issue time is no longer listed

- **WHEN** the selected issue time is not among the available issue times
- **THEN** it is kept as the first option, disabled and labelled as retained-disk unavailable

### Requirement: 点击河段要素弹出按源独立加载的 q_down 预报曲线窗

点击地图河段要素 SHALL 打开 M11 河段曲线窗（桌面形态为可拖拽窗，移动形态为 `mobile-curve-sheet` 定义的抽屉；
实现为 `apps/frontend/src/components/map/M11RiverForecastPanel.tsx`）。曲线窗 SHALL 对 GFS 与 IFS 两个源各自独立地
取 latest-product 身份（`fetchHydroMetLatestProduct`），再按要素 `river_segment_id` 经 `loadHydroMetRiverForecast` +
`validateHydroMetRiverForecastForChart` 拉取并校验 `q_down` forecast-series。
通过校验的源各画一条曲线（echarts `ForecastChart`）；任一步失败的源 MUST NOT 画曲线（不画假曲线红线），并保留一条
`<源>：<原因>` 形式的原因。至少一个源通过时曲线窗 SHALL 渲染图表，并在图表下方列出未通过的源的原因；
两个源都未通过时 SHALL 显示逐条列出原因的空态，不绘制任何曲线。曲线窗标题为河段显示名
（`formatRiverSegmentDisplayName`），在有图表和空态时都显示。
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

- **WHEN** 两个源都校验失败（`ok:false`，例如缺身份字段）或请求失败
- **THEN** popup 显示空态（`m11-river-panel-empty`），逐条列出两个源的原因，不绘制任何 q_down 曲线
- **AND** 窗口标题仍为河段显示名

#### Scenario: 所选起报时次已不可用

- **WHEN** 用户选了一个起报时次，而某个源的 latest-product 返回的是另一个时次
- **THEN** 该源不画曲线，其原因为 `<源>：起报 <所选时次> 已不可用`
- **AND** 不以返回的另一个时次的数据代替

### Requirement: 代站曲线窗在源未解析时不发请求

代站曲线窗（`apps/frontend/src/components/map/M11StationForcingPopup.tsx`）已有所属流域而页面尚未给出已解析的源时
SHALL 显示等待空态，MUST NOT 请求 latest-product 或 station-series。源解析后它固定加载 GFS 与 IFS 两个源。

#### Scenario: 源未解析

- **WHEN** 代站曲线窗打开，已有所属流域，而页面的源尚未解析
- **THEN** 窗内显示 `等待 GFS/IFS 源解析后加载 station-series`
- **AND** 不发出 latest-product 或 station-series 请求

### Requirement: On a reach shared by two basin networks the click SHALL open the segment of the network with fewer segments

When more than one feature of the renderable overlay's hit layer lies under the pointer, the product
click-target resolution SHALL choose the first of them under this order, and SHALL NOT depend on the
order in which the map returned them except under rule 4. The network with fewer segments owns the
reach when every network under the pointer has a count; otherwise the network id decides:

1. **Counts, all or nothing.** The count of a feature is the smallest usable `segment_count` among the
   model records the overview has loaded for the feature's `river_network_version_id`; a count is
   usable when it is a finite number greater than zero. When every hit-layer feature under the pointer
   has a count, a feature with a smaller count comes before a feature with a larger one. When any of
   them has none (records not loaded, network absent from them, no usable count, no
   `river_network_version_id`), counts are not used at all.
2. **Network id.** Features with a `river_network_version_id` that rule 1 does not order are ordered
   by descending `river_network_version_id`, compared by UTF-16 code units and not by locale.
3. A feature without a `river_network_version_id` comes after every feature with one.
4. Features equal under 1 to 3 (features of one network) keep the order in which the map returned them.

The station cluster, station point and basin fill steps of the resolution and their priority over or
under the overlay step SHALL be unchanged. Where every hit-layer feature under the pointer belongs to
one network, the chosen feature SHALL be the first the map returned. The same resolution with the same
counts SHALL serve the product click handler and the river-click evidence hook; the station-locate
evidence hook SHALL call it with the same counts as well.

#### Scenario: The nested network owns the shared reach

- **WHEN** the features under the pointer are a segment of network `basins_b…` with 7,971 segments and
  a segment of network `basins_c…` with 25,971 segments (the network with fewer segments has the
  smaller id, so that the id alone would choose the other one)
- **THEN** the click opens the river window of the segment of the network with 7,971 segments
- **AND** it does so in either order in which the map returns the two features

#### Scenario: One network under the pointer behaves as before

- **WHEN** the features under the pointer are two segments of one network
- **THEN** the click opens the first one the map returned

#### Scenario: Equal counts are decided by the network id

- **WHEN** two networks under the pointer have the same segment count
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger by
  code-unit comparison, in either order in which the map returns them

#### Scenario: One network without a count switches the counts off

- **WHEN** one network under the pointer has a usable count and the other is absent from the loaded
  model records
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger,
  whichever of the two has the count

#### Scenario: No counts are loaded yet

- **WHEN** no model record has been loaded and two networks are under the pointer
- **THEN** the click opens the segment of the network whose `river_network_version_id` is larger

#### Scenario: A station still wins over a river

- **WHEN** stations are shown and a station point and two networks' segments are under the pointer
- **THEN** the click opens the station, as before

#### Scenario: The evidence hook follows the owner

- **WHEN** the river-click evidence hook is asked to locate a segment of the network with more segments
  at a point where a segment of a network with fewer segments is also under the pointer, and both
  counts are loaded
- **THEN** it rejects with `HOOK_POINT_OCCLUDED`
- **AND** asked for the segment of the network with fewer segments at the same point, it resolves
