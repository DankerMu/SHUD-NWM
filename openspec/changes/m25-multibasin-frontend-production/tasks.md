## 0. 前置与基线

- [ ] 0.1 用 `openspec validate m25-multibasin-frontend-production --strict --no-interactive` 校验本 change 4/4 complete 后再开实现。
- [ ] 0.2 grep 清点 `QHH_BASIN_ID` 与 `basins_qhh` 在 `packages/common/forecast_store.py` 的全部引用点（查询 + 错误响应），列入 Epic 作为去硬编码 checklist（防遗漏退回 QHH）。
- [ ] 0.3 记录向后兼容基线：`/api/v1/mvp/qhh/latest-product` 旧路径 + M22 cross-plane 默认 QHH 行为，作为回归基准。
- [ ] 0.4 核实底层字段可用性：`core.river_segment` 是否含 `stream_order`、站点表/QC 是否含 QC 状态字段；据此确定河段/站点高级筛选是落地还是标注不可用（不为筛选改 DB schema）。

## 1. 后端：流域动态发现（multibasin-product-discovery）

- [ ] 1.1 `packages/common/model_registry.py::list_basins` 增加 `has_display_product: bool` 参数；为 true 时 `JOIN hydro.hydro_run` + `core.basin_version`，按 `QHH_LATEST_READY_RUN_STATUSES` 过滤 distinct basin_id；默认 false 保持现有全量行为。
- [ ] 1.2 `apps/api/routes/models.py::list_basins` 暴露 `has_display_product` 查询参数并透传 store。
- [ ] 1.3 更新 `openapi/nhms.v1.yaml` 的 `GET /api/v1/basins` 参数声明；regen 前端类型（`apps/frontend/src/api/types.ts`）。
- [ ] 1.4 后端测试：覆盖 has_display_product=true 仅返回有 ready run 的流域、缺省全量向后兼容、ready 口径与 latest-product 一致（同一 `QHH_LATEST_READY_RUN_STATUSES`）。

## 2. 后端：latest-product 去硬编码 + basin_id（latest-product-multibasin）

- [ ] 2.1 `packages/common/forecast_store.py` 删除 `QHH_BASIN_ID` 写死，latest-product store 方法增加 `basin_id` 参数，SQL `WHERE bv.basin_id = %(basin_id)s` 改为传参（含候选查询 :1081/:1529、:1622/:1633 及错误响应 :993/:2359/:2395）。
- [ ] 2.2 `apps/api/routes/forecast.py` latest-product 路由（`:114`）增加可选 `basin_id` 查询参数，缺省默认 `basins_qhh`；保留 `/api/v1/mvp/qhh/latest-product` 旧路径向后兼容。
- [ ] 2.3 保持 strict identity：basin_id 与 `source/cycle_time/run_id/model_id` 联合精确匹配，拒 historical fallback。
- [ ] 2.4 更新 `openapi/nhms.v1.yaml` latest-product 参数 + regen 前端类型。
- [ ] 2.5 后端测试：覆盖 heihe basin 取数成功、缺省默认 QHH 向后兼容、**M22 cross-plane 旧调用（不带 basin_id + strict identity）返回不变**、目标流域无产品返回 unavailable 不串流域、basin+strict identity 精确匹配、错误响应不写死 qhh。

## 3. 后端：洪水重现期可用性独立字段（return-period-availability）

- [ ] 3.1 latest-product 候选查询复用 `_flood_product_quality_join/_select`（`forecast_store.py:3241` 起）`LEFT JOIN flood.return_period_result`，统计**非空 peak 行** `flood_return_period_rows`（与 best-available/`/runs` 同口径）。
- [ ] 3.2 在响应 `availability` 下新增**独立 supplemental 字段** `return_period_status`（ready/unavailable，unavailable 带 reason code `RETURN_PERIOD_RESULT_UNAVAILABLE`）；**MUST NOT** 把该状态加入 `_qhh_latest_unavailable_reasons()` 的 blocking reasons（避免有 q_down 无洪频基线的产品掉 ready/404）。
- [ ] 3.3 更新 `openapi/nhms.v1.yaml` 的 `QhhLatestProduct.availability` schema 新增 `return_period_status` + regen 前端类型。
- [ ] 3.4 后端测试：①**回归**——有 q_down 但无非空 peak 行时产品仍 ready 且正常返回（不 404），`return_period_status=unavailable`；②有非空 peak 行→ready；③仅非 peak/timestep 行→unavailable（口径反例）；④既有 `ready`/`unavailable_reasons` 取值不变；⑤与 best-available 对同一 run 判定一致。

## 4. 后端：河段/站点列表生产契约（hydromet 列表前置依赖）

- [ ] 4.1 `GET /api/v1/basin-versions/{id}/river-segments`（`forecast.py:30`）增加 `search` 参数（按 segment 标识/名称），保持 limit/offset 分页；`stream_order` 过滤仅在 `0.4` 确认字段存在时实现。
- [ ] 4.2 站点 inventory 接口增加 `search` + `variable` 覆盖筛选参数；QC 状态筛选仅在字段存在时实现。
- [ ] 4.3 更新 `openapi/nhms.v1.yaml` 两接口参数 + regen 前端类型。
- [ ] 4.4 后端测试：河段 search 命中/分页不全量、站点 search/variable 覆盖筛选、stream_order/QC 在字段缺失时优雅降级（标注不可用而非报错）。

## 5. 前端：/hydro-met 多流域主展示（hydromet-multibasin-display）

- [ ] 5.1 流域选择器组件：消费 `GET /api/v1/basins?has_display_product=true`，数据驱动渲染，无硬编码白名单；切流域以 `basin_id` 重拉 latest-product/河段/站点（`apps/frontend/src/pages/hydroMet/`）。依赖 #1、#2。
- [ ] 5.2 河段列表走后端 search/limit/offset（依赖 #4.1），选中高亮加载 q_down，不全量加载；stream_order 过滤项按字段可用性显隐。
- [ ] 5.3 站点列表走后端 search/variable 筛选（依赖 #4.2）+ forcing 六变量展示或明确 unavailable；QC 筛选项按字段可用性显隐。
- [ ] 5.4 产品状态条：消费 latest-product availability，展示 q_down/forcing 的 ready·degraded·unavailable 与 return-period 的独立 `return_period_status`（依赖 #3）。
- [ ] 5.5 strict identity 前端一致性：所有请求参数派生自同一 latest-product 产品身份（basin_id + 现有 basin_version_id/segment_id/issue_time），不手输、不绘假曲线、不新增后端 identity 参数。
- [ ] 5.6 前端单测：流域选择器数据驱动/新流域自动出现、河段 search 分页、站点 search/variable 筛选、状态条三态（含 return_period_status）、degraded 显示、strict identity 一致（`apps/frontend/src/pages/hydroMet/__tests__/`）。

## 6. 前端：洪水重现期静态图例区（return-period-legend-preview）

- [ ] 6.1 `/hydro-met` 内嵌 return-period 区块：`return_period_status=unavailable` 时显示"暂未发布正式产品" + 静态分级图例（2y…100y），不渲染产品数据。
- [ ] 6.2 守红线：无真实数据时不出现"正式产品已发布"文案、不渲染假河段、不调用 preview/status 排除接口。
- [ ] 6.3 前端测试：unavailable 占位 + 图例展示、断言无"已发布正式产品"文案、断言不请求排除接口。

## 7. 前端：/ops + /monitoring display 降级（ops-display-downgrade）

- [ ] 7.1 `NavBar.tsx` 按 runtime config `display_readonly` 隐藏 `/ops` 与 `/monitoring` 入口；compute/dev 保持原导航；**MUST NOT 误删/绕过 `/meteorology` 的 `hasMinimumMeteorologyContracts()` 门控**。
- [ ] 7.2 `/ops` 路由保留 + role-gated 内部访问，文案从"系统运维"改为"内部诊断"；`/monitoring` 路由保留、只读语义不变（不删代码、不动 display_readonly 后端边界）。
- [ ] 7.3 前端测试：display_readonly 隐藏 /ops + /monitoring 入口、compute/dev 保留、角色来源用 runtime config、/meteorology 门控保持、display_readonly 边界防护测试保持通过。

## 8. 集成与验收

- [ ] 8.1 可扩展性验证：构造一个新注册流域（fixture/集成）走"注册→published→发现接口→前端选择器出现"，断言前后端零代码改动。
- [ ] 8.2 全量校验：`uv run ruff check . && uv run pytest -q`；`cd apps/frontend && corepack pnpm test && corepack pnpm exec tsc --noEmit && corepack pnpm run check:api-types && corepack pnpm build`。
- [ ] 8.3 `openspec validate m25-multibasin-frontend-production --strict --no-interactive` 通过；更新 `progress.md` 与 `docs/runbooks/node-27-bringup-checklist.md` 衔接说明（多流域展示、/ops 降级、return-period 诚实状态）。
