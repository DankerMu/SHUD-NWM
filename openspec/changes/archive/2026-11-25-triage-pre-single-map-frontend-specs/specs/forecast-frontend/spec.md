# forecast-frontend

## REMOVED Requirements

### Requirement: Map initialization

**Reason**: `/forecast` is a redirect to the single map page; the map opens on the national extent, not on a demo basin.

**Migration**: the extent is stated by `national-overview-page` "Overview map displays national basin context";
the viewport fill by `single-map-shell-routing` and `map-first-layout-conformance`; the zoom control
per form by `mobile-map-overlay-layout` and `mobile-viewport-shell`. Scale bar and attribution are no longer a stated requirement.

### Requirement: River network layer

**Reason**: the river layers have a vector-tile branch, which this requirement forbade, and no layer is styled by stream order.

**Migration**: `frontend-mvt-layer-consumption` and `hydromet-multibasin-display` state the river layers.

### Requirement: Segment hover interaction

**Reason**: the highlight is stated by `m11-river-hover-highlight`; no hover tooltip exists.

**Migration**: `m11-river-hover-highlight` (highlight and pointer cursor). The hover tooltip is dropped.

### Requirement: Segment click interaction

**Reason**: superseded by the river curve window; clicking empty map does not close it.

**Migration**: `map-feature-popups` "点击河段要素弹出按源独立加载的 q_down 预报曲线窗" and `mobile-curve-sheet`.

### Requirement: Forecast chart display

**Reason**: the chart's only consumer renders the compact variant without a title or an echarts legend, in fixed per-source colours.

**Migration**: `multi-source-comparison-ui` "GFS 与 IFS 预报曲线同图渲染".

### Requirement: Chart-API integration

**Reason**: the request carries the full product identity and one scenario per source; the error text and the `request_id` it prescribed are not rendered.

**Migration**: `map-feature-popups` "点击河段要素弹出按源独立加载的 q_down 预报曲线窗"; strict identity in `hydromet-multibasin-display`.

### Requirement: Loading and error states

**Reason**: `加载中...`, `网络连接失败`, `河网数据加载失败` and the retry control do not exist; `暂无预报数据` survives only as the chart's no-series fallback.

**Migration**: `map-feature-popups` "点击河段要素弹出按源独立加载的 q_down 预报曲线窗" for the curve window; `national-overview-page` "Overview reports missing data in floating notices" for map sources.
