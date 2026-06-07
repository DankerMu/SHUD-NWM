# M25 P2 Carry-Forward Ledger

> 评审遗留的**非阻塞 P2**条目，供后续优化批次处理；**不在 #318（集成验收）内修**。
> 严重度统一 P2（UX 不一致 / 测试完备性 / 契约表达），均不破坏后端、不阻塞上线。
> 来源 commit 见各条；行号以合并时 master 为准，后续可能漂移。
> 记录日期：2026-06-07（#318 capstone 收尾）。

| id | 来源 | 文件:行 | 问题 | 建议 | 严重度 | 状态 |
|---|---|---|---|---|---|---|
| #315-P2-a | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/HydroMetPage.tsx`（河段 fetch effect，~:471–:482，依赖 `debouncedMin/debouncedMax`） | stream_order 控件按字段可用性隐藏时，残留的 min/max state 仍随 debounce 发往后端（fetch effect 不按 availability 门控） | fetch effect 在拼 query 前按 stream_order 字段可用性门控 min/max，控件隐藏即不发该参数 | P2 | open |
| #315-P2-b | `ed60f80` (m25-315) / `dce266f` (m25-313) | `openapi/nhms.v1.yaml`（river-segments `stream_order`）+ `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx`（`as number` 逃逸） | 契约 `stream_order: number` 必填，前端"字段缺失→标注不可用"分支在当前类型下不可达；测试用 `as number` 逃逸构造缺失态 | 二选一：后端契约改 `number \| null`（真实反映可缺失），或在 spec 注明该分支为前端防御性兜底、契约保持必填 | P2 | open |
| #315-P2-c | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx` | debounce 合并请求无显式断言（连续输入只发一次后端请求未被测） | 补一条 fake-timer 测试：连续输入多次、推进 debounce 后断言后端只被调用一次 | P2 | open |
| #315-P2-d | `ed60f80` (m25-315) | `apps/frontend/src/pages/hydroMet/__tests__/ListProduction.test.tsx` | 翻页只测 next，未测 prev / filter 变更后 offset 复位 | 补 prev 翻页断言 + search/variable 变更后 offset 归零断言 | P2 | open |
| #314-P2 | `5fb209b` (m25-314) | `apps/frontend/src/pages/hydroMet/HydroMetPage.tsx`（流域切换 → latest-product 重拉链路） | 流域切换时 stale basin_id select 边界：旧产品 basin_id 在新产品到达前短暂残留 | 切换瞬间清空/置 loading 旧产品身份，或以 selectedBasinId 为唯一来源派生展示，避免旧 basin_id 闪现 | P2 | open |
| #316-P2 | `d93c979` (m25-316) / `bd6e491` (m25-312) | `apps/frontend/src/pages/hydroMet/ReturnPeriodSection.tsx` + `__tests__/ReturnPeriodSection.test.tsx` + AppRoutes fixture（`Record<string, unknown>` 类型逃逸） | q_down degraded 分支无测试；缺页面级集成断言；AppRoutes fixture 用 `Record<string, unknown>` 掩盖 `return_period` 字段漂移 | 补 q_down degraded 分支单测 + 页面级 return_period_status 三态集成断言；fixture 换强类型（`components['schemas'][...]`）暴露字段漂移 | P2 | open |

## 深度 review 修复批次（2026-06-07，分支 M25-code-review）

61-agent max 强度深度复审（10 维度 × 3 视角对抗验证 + 2 critic）后，**已在分支直接修**以下条目（本地 ruff 0 / 前端 608 passed / 后端 338 passed + 5 真DB集成 skip / 无 types.ts drift）：

| id | 严重度 | 修法 | 状态 |
|---|---|---|---|
| M-1 | P1 | `model_registry.list_basins(has_display_product)` EXISTS 补 `run_type='forecast' AND cycle_time IS NOT NULL`，对齐 latest-product 可用口径（source 维有意不下推）；改正"never diverge"注释 | fixed |
| T-1 | P1 | 补 `/basins?has_display_product` 路由→store 透传的 TestClient 契约测试（记录入参 fake store 断言 True/False） | fixed |
| T-2 | P1 | 新建 `tests/test_return_period_integration.py`（@integration）真 DB 执行 `_flood_product_quality_join` SQL，断言 return_period_status ready/unavailable + 不进 blocking reasons（红线执行级 oracle） | fixed（CI/node-22 跑） |
| T-3 | P2 | `test_openapi_drift.py` 补 met-stations / river-segments 新参数 runtime↔static 一致性断言 | fixed |
| T-4 | P2 | `data_sources` variables 类型改 `list[str]\|None`；main.py runtime patch 归一 oneOf；补重复参数绑定 TestClient 测试 | fixed |
| m25-05 | P3 | river-segments `stream_order_min>max` 入口校验抛 422 + 测试 | fixed |
| B-1 | P3 | 去掉 latest-product basin_id 的 openapi `minLength:1`（如实反映空串=省略，无 types.ts drift） | fixed |
| ta-02 | P3 | 扩展性测试 docstring 精简如实 + 加非恒真断言（生产 id 不作字面量白名单） | fixed |
| M-2 | P2 | 状态条 ready 以 `availability.ready===true` 为前置，未知 reason code 归 unavailable（修虚假肯定红线） | fixed |
| F-1 | P2 | stream_order 控件可用性粘滞 + "清除筛选"按钮，筛空不再锁死 | fixed |
| F-2 | P2 | 翻页/筛选后选中失效自动重选当前页首项（基于解析对象存在性） | fixed |
| F-3 | P3 | BasinSelector 陈旧 id 占位 option + "默认流域"常驻可回退 null + 空态 | fixed |
| F-4 | P3 | offset 复位与查询合并单 effect，消除多余被取消请求 | fixed |
| m25-07 | P3 | AppRoutes 河段筛选 mock 对齐后端 NULL stream_order 语义（min/max 任一存在即排除） | fixed |

> 既有 ledger 的 **#315-P2-a**（stream_order 残留发后端）与 **#316-P2**（状态条/集成断言）部分被本批 F-1/M-2 覆盖；剩余纯测试补强（#315-P2-c debounce 单次断言、#315-P2-d prev 翻页断言）仍 open。

### m25-06（响应 required 收紧）— 闭环，无需改代码
`QhhLatestAvailability` 的 `required` 由 4→6（新增 `return_period_status`/`return_period_reasons`，#312）。核实：后端 `forecast_store.py:2554` **无条件** emit 两字段（fresh 响应必带）；仓内**无固化响应 JSON/snap 快照**；前端**宽松读取**（`availability?.return_period_status ?? 'unavailable'`）。→ 当前部署**无活的破坏面**。结论：此收紧为**有意**（后端总是发），仅需本记录闭环；若 node-27 存在仓库外的严格响应 schema 校验/快照，迁移时同步加这两字段即可。

### S-1 / bk-05 — 独立跟踪 **#334**（DB 性能/索引，需迁移 + node-22 oracle）
- **S-1**：站点/河段 search ILIKE 前导通配无 pg_trgm GIN；has_display_product EXISTS 的 `(basin_version_id, status)` 无索引、`parsed` 不被覆盖；`status::text` 强转阻碍索引。（sql-safety 维度多数票认定**非注入、实为性能**。）
- **bk-05**：`_flood_product_quality_join` 全表 `GROUP BY run_id` 无 run 维下推，latest-product 高频路径随表增长有放大风险（建议下推候选 run 集合或 LATERAL + 确认 return_period_result 有 run_id 索引）。
- 二者均需新迁移 + node-22 真 DB EXPLAIN，生产运行期 BLOCKED → 独立 **#334** 跟踪，不在本批修。
  > 原拟并入 #330，但 #330（及 #321/#329/#331/#332 整批 B 层）已由 @DankerMu 关闭、在生产环境接管；#330 为 #334 的 parsed 索引子集。
