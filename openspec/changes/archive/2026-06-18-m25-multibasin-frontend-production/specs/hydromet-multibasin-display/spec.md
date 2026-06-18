## ADDED Requirements

### Requirement: 流域选择器数据驱动

`/hydro-met` SHALL 提供流域选择器，其候选流域来自流域发现接口（`GET /api/v1/basins?has_display_product=true`）。前端 MUST NOT 维护硬编码的流域白名单。

#### Scenario: 新流域自动出现在选择器
- **WHEN** 后端发现接口返回新增流域，用户打开 `/hydro-met`
- **THEN** 流域选择器列出该新流域，前端代码无需修改

#### Scenario: 切换流域刷新展示
- **WHEN** 用户在选择器中从 qhh 切到 heihe
- **THEN** 页面以 `basin_id=basins_heihe` 重新拉取 latest-product、河段与站点，展示 heihe 数据

### Requirement: 河段列表生产可用

`/hydro-met` 河段列表 SHALL 经后端 `search` + `limit/offset` 分页加载，MUST NOT 一次性全量加载全部河段；并 SHALL 支持选中河段高亮加载其 q_down 曲线。`stream_order` 过滤为可选增强，仅在底层河段数据含该字段时提供，否则该筛选项标注不可用（不伪造、不改 DB schema）。

#### Scenario: 搜索定位河段
- **WHEN** 用户在河段搜索框输入某 segment 标识
- **THEN** 列表经后端 search 过滤出匹配河段，选中后加载其 q_down 曲线

#### Scenario: 分页不全量加载
- **WHEN** 流域河段数超过单页上限
- **THEN** 列表按页加载（后端 limit/offset），显示总数与当前页，不一次拉全量

#### Scenario: stream-order 过滤（字段可用时）
- **WHEN** 底层河段数据含 stream_order 字段且用户按某 stream order 过滤
- **THEN** 列表仅显示匹配该 stream order 的河段；若底层无该字段，则该过滤项以不可用呈现

### Requirement: 站点列表筛选

`/hydro-met` 站点列表 SHALL 支持后端 `search` 与变量覆盖筛选，并可查看站点 forcing 六变量（PRCP/TEMP/RH/wind/Rn/Press）或明确 unavailable。QC 状态筛选为可选增强，仅在站点数据含 QC 字段时提供。

#### Scenario: 站点搜索
- **WHEN** 用户在站点搜索框输入站点标识
- **THEN** 列表经后端 search 过滤出匹配站点

#### Scenario: 按变量覆盖筛选
- **WHEN** 用户筛选"含 PRCP 的站点"
- **THEN** 列表仅显示具备 PRCP 覆盖的站点

#### Scenario: QC 筛选（字段可用时）
- **WHEN** 站点数据含 QC 状态字段且用户按 QC 状态筛选
- **THEN** 列表按 QC 状态过滤；若无该字段则该筛选项以不可用呈现

#### Scenario: 变量缺失明确标注
- **WHEN** 某站点缺少某 forcing 变量
- **THEN** 该变量曲线显示明确 unavailable，不绘制假曲线

### Requirement: 产品状态条诚实展示

`/hydro-met` 顶部 SHALL 展示产品状态条，覆盖 q_down、forcing、return-period 三类产品各自的 ready / degraded / unavailable 状态，来源为 latest-product 响应（return-period 取自独立的 `availability.return_period_status`）。

#### Scenario: 三类产品状态可见（含 return-period unavailable）
- **WHEN** latest-product 返回 q_down ready、forcing ready、`availability.return_period_status = unavailable`
- **THEN** 状态条显示流量已发布、forcing 已发布、洪水重现期暂未发布

#### Scenario: degraded 状态可见
- **WHEN** latest-product 的 availability 表明某产品为部分可用（degraded，如站点覆盖不完整）
- **THEN** 状态条对该产品显示 degraded，而非笼统 ready 或 unavailable

### Requirement: strict identity 贯穿多流域（前端一致性）

`/hydro-met` 所有数据请求 MUST 与选中产品身份一致：latest-product 携带选中 `basin_id` 与 strict identity（`source`/`cycle_time`/`run_id`/`model_id`）；河段/站点/forecast-series 复用现有路由参数（如 `basin_version_id`/`segment_id`/`issue_time`，由同一 latest-product 产品身份派生）。前端 MUST NOT 手工输入 identity 或绘制假曲线。本要求 SHALL 以前端一致性校验实现，不要求为 forecast-series/station-series 新增后端 identity 参数。

#### Scenario: 请求参数派生自同一产品身份
- **WHEN** 用户选定流域与产品后查看河段 q_down
- **THEN** forecast-series 请求的 `basin_version_id` 等参数与该 latest-product 产品身份一致，不手输、不串用其他产品身份
