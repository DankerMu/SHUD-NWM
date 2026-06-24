# hydromet-multibasin-display Specification

## Purpose
TBD - created by archiving change m25-multibasin-frontend-production. Update Purpose after archive.
## Requirements
### Requirement: 流域选择器数据驱动

当前 `/` 单图展示入口（兼容 `/hydro-met` legacy redirect alias）SHALL 提供流域选择器，其候选流域来自流域发现接口（`GET /api/v1/basins?has_display_product=true`）。前端 MUST NOT 维护硬编码的流域白名单。

#### Scenario: 新流域自动出现在选择器
- **WHEN** 后端发现接口返回新增流域，用户打开当前 `/` 单图展示入口或通过 `/hydro-met` legacy redirect alias 进入
- **THEN** 流域选择器列出该新流域，前端代码无需修改

#### Scenario: 切换流域刷新展示
- **WHEN** 用户在选择器中从 qhh 切到 heihe
- **THEN** 页面以 `basin_id=basins_heihe` 重新拉取 latest-product、河段与站点，展示 heihe 数据

### Requirement: 河段发现迁移到地图 popup

Pre-M26 `/hydro-met` legacy redirect-era segment list/search/pagination evidence SHALL be treated as historical only. Current `/` single-map display SHALL discover river segments through map layers and feature popups, and MUST NOT reintroduce the retired full-list/search/pagination/filter module. Historical `stream_order` filter evidence also remains retired context unless a later change explicitly designs a map-layer filter.

#### Scenario: 地图点选定位河段
- **WHEN** 用户从当前 `/` 单图展示入口点选河段要素
- **THEN** popup 加载该河段的 q_down 曲线，并且不依赖 retired list search

#### Scenario: 历史列表证据不恢复为当前契约
- **WHEN** pre-M26 historical evidence mentions segment list pagination
- **THEN** current `/` display treats that evidence as superseded by map-layer
  discovery and does not restore the retired list module

#### Scenario: stream-order 过滤保持 retired context
- **WHEN** pre-M26 historical evidence mentions stream-order filtering
- **THEN** current `/` display does not require a `stream_order` list filter
  unless a later change explicitly designs a map-layer filter

### Requirement: 站点发现迁移到地图 popup

Pre-M26 `/hydro-met` legacy redirect-era station list/search/variable-filter evidence SHALL be treated as historical only. Current `/` single-map display SHALL discover stations through the meteorology-station map layer and popups, while station series data remains honest: PRCP/TEMP/RH/wind/Rn may render when available, and `Press` is unavailable/omitted unless the current station-series route supports it.

#### Scenario: 地图点选定位站点
- **WHEN** 用户从当前 `/` 单图展示入口点选站点 marker
- **THEN** popup 加载该站点可用 forcing 曲线或明确 unavailable，不依赖 retired list search

#### Scenario: 历史变量筛选不恢复为当前契约
- **WHEN** pre-M26 historical evidence mentions filtering stations by variable
  coverage
- **THEN** current `/` display treats it as superseded by map-layer discovery
  unless a later change explicitly designs a map-layer filter

#### Scenario: QC 筛选保持 retired context
- **WHEN** pre-M26 historical evidence mentions QC list filtering
- **THEN** current `/` display does not require a QC list filter, while any shown
  station series quality flags remain truthful

#### Scenario: 变量缺失明确标注
- **WHEN** 某站点缺少某 forcing 变量
- **THEN** 该变量曲线显示明确 unavailable，不绘制假曲线

### Requirement: 产品状态条诚实展示

当前 `/` 单图展示入口（兼容 `/hydro-met` legacy redirect alias）顶部 SHALL 展示产品状态条，覆盖 q_down、forcing、return-period 三类产品各自的 ready / degraded / unavailable 状态，来源为 latest-product 响应（return-period 取自独立的 `availability.return_period_status`）。

#### Scenario: 三类产品状态可见（含 return-period unavailable）
- **WHEN** latest-product 返回 q_down ready、forcing ready、`availability.return_period_status = unavailable`
- **THEN** 状态条显示流量已发布、forcing 已发布、洪水重现期暂未发布

#### Scenario: degraded 状态可见
- **WHEN** latest-product 的 availability 表明某产品为部分可用（degraded，如站点覆盖不完整）
- **THEN** 状态条对该产品显示 degraded，而非笼统 ready 或 unavailable

### Requirement: strict identity 贯穿多流域（前端一致性）

当前 `/` 单图展示入口（兼容 `/hydro-met` legacy redirect alias）的所有数据请求 MUST 与选中产品身份一致：latest-product 携带选中 `basin_id` 与 strict identity（`source`/`cycle_time`/`run_id`/`model_id`）；河段/站点/forecast-series 复用现有路由参数（如 `basin_version_id`/`segment_id`/`issue_time`，由同一 latest-product 产品身份派生）。前端 MUST NOT 手工输入 identity 或绘制假曲线。本要求 SHALL 以前端一致性校验实现，不要求为 forecast-series/station-series 新增后端 identity 参数。

#### Scenario: 请求参数派生自同一产品身份
- **WHEN** 用户选定流域与产品后查看河段 q_down
- **THEN** forecast-series 请求的 `basin_version_id` 等参数与该 latest-product 产品身份一致，不手输、不串用其他产品身份
