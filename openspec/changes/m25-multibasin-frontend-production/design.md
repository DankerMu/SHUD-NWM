## Context

27 节点 display 前端（`display_readonly`）当前是 M21/M22 遗留的 QHH 单流域形态：

- `packages/common/forecast_store.py:13` 写死 `QHH_BASIN_ID = "basins_qhh"`，latest-product 查询 `WHERE bv.basin_id = 'basins_qhh'`（`:1081/:1529`、`:1622/:1633`），错误响应也硬编码（`:993/:2359/:2395`）。传 heihe 的 model_id 也取不出 heihe。
- `/api/v1/mvp/qhh/latest-product`（`apps/api/routes/forecast.py:114`）参数只有 source/run_id/cycle_time/model_id，无 basin 维度。
- latest-product 的 `availability.unavailable_reasons`（`_qhh_latest_unavailable_reasons()`，`forecast_store.py:2438` 起，q_down/forcing 样本检查在 `:2730` 之后）只覆盖 run status / forcing / station / q_down 样本，**零提及 return-period**。该 reasons 集合是 **blocking**：latest-product 仅返回 `evaluation["ready"]` 的 candidate（`forecast_store.py:982`），而 `ready = not reasons`（`:2407/:2435`）。
- 前端 `/hydro-met` 河段候选 `HYDRO_MET_RIVER_SEGMENT_LIMIT=250`、站点 `HYDRO_MET_STATION_LIMIT=500`，无搜索/分页/筛选，不可用于生产规模。
- `/ops`（role-gated operator/model_admin/sys_admin）与 `/monitoring`（无 role 限制）仍在主导航，对只读节点无控制价值。

已存在、可直接复用的基础设施：`GET /api/v1/basins`（`models.py:362` → `model_registry.py:251` 列 `core.basin`）、`GET /api/v1/basins/{id}/versions`、`workers/model_registry/basins_discovery.py`、`basins_registry_import`、`OverviewPage.tsx:474` 的 basin 分组消费、`core.basin`/`core.basin_version` 注册表、`_flood_product_quality_from_row()`（`forecast_store.py:3185-3238`，`/runs` 产品质量投影使用的私有 helper，已实现 return-period 可用性判断；best-available 与 `/runs` 共用同一口径）及其 JOIN/SELECT 投影 `_flood_product_quality_join/_select`（`:3241` 起）。河段接口 `GET /api/v1/basin-versions/{id}/river-segments`（`forecast.py:30`，现仅 `limit/offset`）、站点 inventory（现仅 `basin_version_id/model_id/limit/offset`）。

## Goals / Non-Goals

**Goals:**

- `/hydro-met` 成为多流域正式业务化主展示，展示真实 forcing + q_down + 诚实的 return-period 状态。
- **可扩展**：新增流域只需 `data/Basins/<流域>` 落地 → `basins_registry_import` 注册 → 22 产出 published，前端流域选择器即自动出现，**前后端零代码改动**。
- 后端改动全部为**扩展现有逻辑**（去硬编码 + 参数化 + 复用判断函数），零新建接口、零造假数据、零 DB schema 改、向后兼容。
- `/ops`、`/monitoring` 从主交付收缩为内部诊断，资源集中到展示主线。

**Non-Goals:**

- 不产出洪水重现期**真实产品**（需 22 侧 hindcast 洪频基线，`flood.flood_frequency_curve` 0 行，属平行数据/科学任务）。
- 不新建 `flood-return-period/status|preview` 平行接口，不造 preview 假河段数据。
- 不删除 `/ops` 代码、不修改 display_readonly 后端边界（retry/cancel 409、queue 503、no-slurm 及其测试保持不变）。
- 不修改 DB schema、不触碰 `flood.return_period_result` 表结构。
- 不在本阶段做实时气象栅格 `/meteorology` 生产化（保持现有 `hasMinimumMeteorologyContracts()` 合同门控）。

## Decisions

### D1: latest-product 去硬编码——扩展现有接口而非新建（方案 B）

给现有 latest-product store 方法与路由参数化 `basin_id`，删除 `QHH_BASIN_ID` 硬编码，SQL `WHERE bv.basin_id = %(basin_id)s` 改为传参。路由默认 `basin_id=basins_qhh` 保持 `/api/v1/mvp/qhh/latest-product` 旧路径与 cross-plane 调用向后兼容。

- **备选**：(A) 前端改用 `/api/v1/runs` 自聚合 latest——丢 quality metadata，前端补逻辑，弃用；(C) 新建通用接口——代码债 + 旧接口迁移含糊，弃用。
- 复用优先（KISS）：扩展 > 平行 > 新建。

### D2: 流域发现——`list_basins` 加 `has_display_product` 过滤

`GET /api/v1/basins?has_display_product=true` 时 `JOIN hydro.hydro_run`（按 `QHH_LATEST_READY_RUN_STATUSES = parsed/frequency_done/published` 同一 ready 判定）+ `core.basin_version` 过滤出 distinct basin_id。默认 `false` 不影响现有调用。前端流域选择器消费此过滤结果，避免列出无产品的空流域。

- 复用 ready 状态定义，禁止前端维护流域白名单（防硬编码回潮）。

### D3: return-period 可用性——独立 supplemental 状态字段（不进 blocking reasons）

latest-product 候选查询 `LEFT JOIN flood.return_period_result` 统计**非空 peak 行**（与 best-available 同口径），在响应中新增**独立的 return-period 可用性字段**（`availability.return_period_status`，取值 `ready` / `unavailable`，unavailable 时带 reason code `RETURN_PERIOD_RESULT_UNAVAILABLE`）。

- **关键约束（P0 修正，审核抓出）**：return-period 可用性 **MUST NOT** 进入 `_qhh_latest_unavailable_reasons()` 的 blocking reasons 集合。否则因 latest-product 只返回 `ready = not reasons` 的 candidate（`forecast_store.py:982/2407/2435`），"有 q_down 但无洪频基线"的产品会从 ready 变 unavailable 甚至 404——而当前所有流域均无 hindcast 基线，等于打掉**全部**展示产品。故 return-period 状态必须是 **supplemental**（仅作展示标注，不影响 ready 判定与产品返回）。
- **代价（修正"零 schema"声明）**：独立字段需扩 OpenAPI `QhhLatestProduct.availability` schema + 前端类型 regen；**非**纯追加 reasons。
- **口径与复用**：`RETURN_PERIOD_RESULT_UNAVAILABLE` 当前是 `_flood_product_quality_from_row()`（`forecast_store.py:3197`）内的 reason code 字符串（**非导出常量**）。复用时连同其 JOIN/SELECT 投影（`_flood_product_quality_join/_select`，`forecast_store.py:3241` 起）一并适配，统计 `flood.return_period_result` 的**非空 peak 行**（`flood_return_period_rows > 0`），与 best-available 对同一 run 判定一致。
- **备选**：复用 best-available 完整 `product_quality` 对象嵌入响应——字段更重，超出最小改，弃用（只取 return-period 单项状态）。

### D4: return-period 前端——诚实状态 + 静态图例，零造假

`/hydro-met` 内嵌 return-period 区块，三态渲染：`unavailable`（来自 D3 的 reason，显示"暂未发布正式产品"）/ `ready`（预留真实产品渲染入口，本阶段不验收）。静态分级图例（2y/5y/10y/20y/50y/100y + 颜色 + 中文标签）是**静态领域知识**，可展示；但**不渲染任何河段产品数据**，不接 preview fixture。

- 守住项目"无伪造洪水位"红线：图例 ≠ 产品；任何"已发布正式产品"文案在无真实数据时禁止出现。

### D5: 前端角色门控——复用 runtime/config，禁止 build-time 硬编码

`/ops`、`/monitoring` 主导航显隐由 `GET /api/v1/runtime/config` 的 `service_role`/`display_readonly` 驱动（复用现有 `isDisplayReadonly`），不依赖编译期角色假设。路由保留 + role-gated 内部访问，display_readonly 边界防护测试不动。

### D6: strict identity 贯穿多流域（前端一致性 + 复用现有参数）

流域选择后，所有展示请求保持与选中产品一致的身份：latest-product 携带 `basin_id` + strict identity；river-segments / station / forecast-series **复用现有路由参数**（`basin_version_id`/`segment_id`/`issue_time` 等，其 `basin_version_id` 已绑定流域与产品谱系）表达身份，前端保证请求参数派生自同一 latest-product 产品身份。**本阶段不为 forecast-series/station-series 新增 `basin_id/run_id/model_id` 后端参数**（M22 已有 strict identity 路由不变）；strict 一致性由前端校验保证。不绘假曲线、不手输 identity。

### D7: 河段/站点列表生产可用——后端搜索+分页，前端不全量

河段（可上千）与站点（数百）列表为生产规模，MUST 走后端 search + limit/offset 分页，前端不全量加载后做客户端过滤。

- river-segments 加 `search`（按 segment 标识/名称）参数；station inventory 加 `search` + `variable` 覆盖筛选参数。
- 高级过滤按底层字段可用性分级：`stream_order`（河段）、QC 状态（站点）**仅在 DB 已有对应字段时**提供，否则该筛选项标注不可用，**不为筛选改 DB schema**（本阶段 Non-Goal）。
- 这些后端契约（含 OpenAPI/types/tests）是前端 hydromet 列表 task 的前置依赖。

## Risks / Trade-offs

- **R1 去硬编码回归风险**：`QHH_BASIN_ID` 被多处引用（查询 + 错误响应），漏改会导致多流域静默退回 QHH。缓解：grep 清点全部引用点，单测覆盖 heihe 路径取数成功 + qhh 默认向后兼容。
- **R2 流域发现与产品状态不一致**：`has_display_product` 用 ready 状态判定，若与 latest-product 的实际可取性口径不一致，会列出"可见但打不开"的流域。缓解：两处共用 `QHH_LATEST_READY_RUN_STATUSES` 同一常量。
- **R3 return-period 误导**：静态图例被误读为真实产品。缓解：D4 强制 disclaimer + 测试断言无产品时不出现"正式产品已发布"文案。
- **R4 cross-plane 旧路径破坏**：去硬编码若改变默认行为，破坏 M22 cross-plane receipt。缓解：默认 `basin_id=basins_qhh` + 保留旧路径，回归测试覆盖。
- **R5 前端规模性能**：多流域 + 河段搜索分页若仍一次性拉全量，大流域会卡。缓解：搜索/分页走后端 limit/offset/search 参数（river-segments 已支持 limit），前端不全量加载。
- **R6 高级筛选依赖底层字段**：`stream_order`（河段）、QC 状态（站点）筛选依赖 DB 已有字段；若字段缺失，强行实现会逼改 DB schema（Non-Goal）或伪造。缓解：实现前核实字段存在性，spec 用"若底层字段可用"表述，缺失则该筛选项标注不可用，不伪造、不改 schema。
- **R7 return-period 字段误进 blocking**：实现者若把 return-period 状态混入 `_qhh_latest_unavailable_reasons()`，复现 P0 回归（全产品掉 ready）。缓解：D3 强制独立字段 + 回归测试断言"有 q_down 无洪频基线"的产品仍 ready 且可返回。
- **Trade-off**：洪水重现期只做"诚实占位 + 图例"，用户暂看不到真实重现期——这是数据现实（无基线），前端造假代价更高（误导 + 违背项目红线），接受占位。
