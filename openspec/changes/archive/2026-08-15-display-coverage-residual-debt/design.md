# Design: display-coverage-residual-debt

## Context（现状锚点，全部为 90dc4a7e 实测行号）

- `packages/common/forecast_store.py`：`QHH_LATEST_SEARCH_LIMIT = 1`（:14），`_fetch_latest_qhh_display_candidates`（:1202），fast path 闸门 `_run_display_coverage_available`（:3631，`to_regclass`），fast path `_fetch_latest_qhh_display_candidates_fast`（:1756），fallback 单语句 `_fetch_all`（:1241）内嵌 candidate_runs + `station_sample_rows`（:1308-1339，join 等值 + 关联窗口列）+ `river_sample_rows`（:1619-1636，同形状）。唯一生产调用方 `latest_qhh_display_product`（:1037→:1054），API 入口 `apps/api/routes/forecast.py:194`。
- **会话/事务语义**：store 连接 `set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)`（:2367），`latest_qhh_display_product` 全程在 `self._transaction()` 单事务内（:1053→:2338-2339）；既有测试固化该语义（`tests/test_forecast_api.py:633,686`）。**同事务内两语句共享同一快照，candidate 不可能在 header 与重查询之间漂移。**
- **参数管道**：`_fetch_all(self, cursor, statement, parameters: Sequence[Any])` 执行 `cursor.execute(statement, tuple(parameters))`（:2315-2316）——传 dict 会被 `tuple()` 退化为 key 名元组，命名占位符 + 现签名直接崩。`_qhh_latest_strict_identity_sql`（:2704-2714）返回位置 `%s` 片段，被 fallback（:1210）与 fast path（:1888）**共享**。
- `packages/common/display_coverage.py`（被移植的已验证模式）：`_SCAN_HEADER_SQL`（:103-117）、NULL-guarded `scan_*` 谓词（station :147-156，river :370-378）、`_SCAN_PARAM_KEYS`（:549-557）、`_refresh` 的 header→绑定→重查询流程（:560-591）。注意：`_refresh` 走**自己的写连接**，快照语义与 forecast_store 的 RR 只读事务不同——移植的是 SQL 形状与流程，不是并发论证。
- `scripts/node27_autopipeline.py`：phase 2 per-run refresh（:1434-1447），phase 3 `_publish_display_runs`（:1084）`SET status = 'published', updated_at = now()`（:1099，**单条集合语句：同 tick 全部 publish 行拿同一个 `now()`**）。`workers/output_parser/parser.py` `mark_run_parsed`（:1053-1069）在 parse 完成时 bump `updated_at`——早于同 run 的 inline refresh，故 phase 2 结束时 `refreshed_at > updated_at` 成立，是 publish 的第二次 bump 破坏了它。
- **`hydro.hydro_run.updated_at` 全部消费方**（repo-wide grep；`db/migrations/*.sql` 无 hydro_run 触发器）：
  1. `packages/common/display_coverage.py:707` stale 谓词 `cov.refreshed_at < h.updated_at`（本 change 受益方）；
  2. `apps/api/routes/hydro_display.py:805-819` `_run_source_version`：revision_basis 七字段（含 **`status`** :813 与 `updated_at`）SHA-256 摘要，作 run-scoped MVT tile 缓存版本；
  3. `services/orchestrator/chain_repository_state.py:481` `candidate_state()`：`ORDER BY CASE WHEN run_id = %s THEN 0 ELSE 1 END, updated_at DESC LIMIT 1` 的 tie-break；
  4. `services/production_closure/readonly_db_validation.py:406` `discover_display_identity()`：`ORDER BY updated_at DESC NULLS LAST, ...` 选代表性展示身份；
  5. `services/tiles/mvt.py:1126-1163` `national_discharge_source_version()`：digest basis 含 `h.updated_at`、**不含 `status`**，但其成员集与 national 数据侧查询同用三态过滤 `status IN ('succeeded','parsed','published')`（mvt.py:544/565/1117/1151/1298）；
  6. `apps/api/routes/pipeline.py:1485,1547 → :309-312`：hydro_run 的 `updated_at` 作为 pipeline 状态响应的用户可见 `updated_at` 字段。
  7. **forecast API + 前端**（review round 1 补录；纯 grep 漏抓 `SELECT h.*` 星号投影）：`apps/api/routes/forecast.py:82-94+` `get_run`/`list_runs` → `forecast_store.py:717-742`/`:781-804`（`SELECT h.*`）→ `:3761` `_hydro_run_response` 原样透出 `updated_at`；前端 `apps/frontend/src/stores/overviewData.ts:371-373` 以 `updated_at ?? created_at` 做降序 tie-break，`apps/frontend/src/lib/m11/overviewDataContracts.ts:436-441` `latestUpdate` / `:297` freshness reference（`validTime ?? updatedAt ?? cycleTime`）。
  8. **orchestrator scheduler hydro truth time**（review round 1 补录）：`chain_repository_state.py:459-473` 的 candidate_state SELECT 投影 `updated_at`（**不投影** `finished_at`/`created_at`）→ `scheduler_state_identity_filter.py:705` `_first_state_datetime(hydro_run, "updated_at", ...)` 为 DB lane 下 hydro truth time 的唯一来源 → `:711` 与 pipeline failure 时刻比较 → `scheduler_state_decision.py:213-217` skip/terminal_hydro_success 判定；`'published'` ∈ `DURABLE_HYDRO_SUCCESS_STATUSES`（`scheduler_state_types.py:30`）。
- `scripts/node27_autopipe_cron.sh`：单 flock（:185-189），整 tick 一对 START/END（:192、:232-233），三阶段（ingest :195-200 / backstop :214-219 / prewarm :224-230）各有起始行、无耗时；preflight rc=2 早退分支（:203-207）；:209-213 注释声称 backstop 兜底带外 seed。
- 既有测试地雷：`tests/test_migrations.py:387` 对源码做**宽切片**（`_fetch_latest_qhh_display_candidates` 起点 → `_fetch_station_for_series` 终点，:400-404），区间同时含 fast path 与 unavailable-context——多数被断言的字符串在未改动函数里也存在，fallback 重构后该测试**大概率静默变空转而非变红**。

## Goals / Non-goals

- Goals：fallback 两 hypertable 扫描获得 chunk exclusion / index scan 且行结果与旧路径逐列一致；publish 后 backstop 0 假 stale；cron 日志分阶段耗时可辨。
- Non-goals：见 proposal 兼容性与非目标节（fast path、refresh 路径、拆锁、6 min/run 轴、代理键切换均不动）。

## D1 — fallback 下推：两语句 header 预取 + run_id 钉死（条 1）

**决策**：将 fallback 从单语句改为两语句，移植 `display_coverage._refresh` 的 SQL 形状与流程：

1. **header 语句**：以与重查询**同一份** candidate_runs SQL 文本（提取为模块级常量，f-string 组装，禁止手抄第二份——手抄即重造本 issue 的 drift 根因）单独执行，投影 7 个标量：`run_id, forcing_version_id, basin_version_id, river_network_version_id, LOWER(source_id), display_start_time, display_end_time`。`LIMIT` 恒为 1（`QHH_LATEST_SEARCH_LIMIT = 1`，identity 路径 `candidate_limit = 1`），header 至多一行。
2. **header 为空 → 直接返回 `[]`**，重查询完全不执行。RR 同快照下这是**严格等价**（header 空 ⟺ 旧单语句 candidate_runs 空 ⟺ 结果空），且是降级路径的额外止血。
3. **header 非空 → 绑定 NULL-guarded scan 参数并执行重查询**。station CTE 追加 `scan_forcing_version_id / scan_basin_version_id / scan_source_id_lower / scan_display_start / scan_display_end`，river CTE 追加 `scan_run_id / scan_basin_version_id / scan_river_network_version_id / scan_display_start / scan_display_end`，谓词形状与 `display_coverage.py` 逐字同构（`(%(scan_x)s IS NULL OR col op %(scan_x)s)`，窗口列用 `>= scan_display_start` / `<= scan_display_end` 字面绑定）。
4. **candidate_runs 钉死**：重查询的 candidate_runs WHERE 追加 `AND h.run_id = %(scan_run_id)s`。理由有二：(a) 给 planner 一个可用于 `hydro_run` 索引访问/裁剪的**字面谓词**（关联列做不到）；(b) 结构上保证 scan 标量与被服务的 candidate 恒同源（零成本纵深防御）。**不是并发修复**——RR 同快照下 candidate 本就不可能漂移（见 Context），parity 口径为严格逐列一致，无任何"旧几毫秒"让步。
5. **钉死等价性前提**：`candidate_limit ≡ 1`。该不变量以测试守卫（常量 >1 时断言失败），未来调大 `QHH_LATEST_SEARCH_LIMIT` 必须先重新设计 per-candidate 循环，不允许静默截断。

**参数管道改造（爆炸半径显式化）**：

- 重查询与 header 语句用**命名参数**（在巨型 f-string 里按位置序追加参数是脆弱源；psycopg2 不允许混用）。
- `_fetch_all` 现签名会把 dict 退化为 key 元组（Context）：实现须让 fallback 的两条语句走 **Mapping 兼容**路径——扩展 `_fetch_all` 接受 `Mapping | Sequence`（isinstance 直传，不 `tuple()` 化）或 fallback 处直接 `cursor.execute`；二选一，禁止悄悄影响其余 17 个位置参数调用点。
- `_qhh_latest_strict_identity_sql` 被 fast path 共享：helper 增加参数风格开关（同一 SQL 模板产出位置或命名占位符 + 对应参数容器），fast path 继续取位置版本；**禁止复制 identity SQL 文本**。
- fast path 语句与 `_fetch_latest_qhh_display_unavailable_context`（:1880）保持位置参数不动。

**否决的备选**：
- 单语句自查询下推（LATERAL/子查询 hoist）：planner 对关联列下推无保证，等于赌优化器版本行为；62824a45 已证明 header 预取是该库的已验证解法。
- coverage miss 直接 503：丧失 authoritative 兜底语义（proposal 已述）。

**Parity 口径**：新旧 fallback 在同一 DB 状态下对同一请求返回**逐列相同**的行（列集、值、排序）。node-27 实机以 live 最新 candidate 各跑一次直接 SQL 对比 + EXPLAIN 证明两 hypertable 均 chunk exclusion / index scan（AC-1）。**Python 绑定路径必须实打实执行**：node-27 真实 DB 集成测试实例化 store、强制 fallback（`_run_display_coverage_available` 置 False），断言与 fast path 结果一致——EXPLAIN/文本断言拦不住参数绑定崩溃，这条拦得住。

**既有测试处置**：`tests/test_migrations.py:387` 的宽切片在重构后会静默空转（Context）。处置：把断言绑定到**抽出的 candidate SQL 常量 + fallback 专属切片**（终点收窄到 `def _fetch_latest_qhh_display_candidates_fast`），占位符字面量同步为命名风格；并给一条变异红证（故意打乱 fallback 索引列序 → 测试红）。不得删除断言意图。

## D2 — publish status-only：删 `updated_at = now()`（条 2）

**决策**：`_publish_display_runs` 的 UPDATE 改为 `SET status = 'published'`。`updated_at` 语义收敛为"run 数据/产物变更时间"：register、`mark_run_parsed`（数据真变了）照常 bump；status-only 的展示转换不 bump。

**消费方逐条裁定**（清单见 Context，共 8 项；第 7/8 项为 review round 1 补录）：
1. **stale 谓词**：`refreshed_at`（phase 2 refresh 时刻）> `updated_at`（parse 时刻）在 publish 后继续成立 → backstop 0 假 stale。谓词本身不改。
2. **`_run_source_version`（run-scoped MVT）**：basis 已含 `status`，parsed→published 翻转本身更换摘要 → tile 缓存照常翻新。
3. **`candidate_state()` tie-break**：旧行为下同 tick publish 的多 run 拿**同一个** `now()`（单条集合语句）——tie-break 本就在同 tick 内失效；新行为下 parse 时刻互异且与 publish 顺序同向，tie-break 反而更可辨。跨 tick 排序由 parse/publish 同向性保持不变。已 publish run 与"其后新 register 未 parse"的同 identity run 相比时胜者会换（新行为选最近一次尝试）——但该 tie-break 仅在 identity 回落分支生效（首键 `CASE WHEN run_id = %s THEN 0` 已把点名 run 顶前），且"最近尝试"语义更合理。裁定：无损，略优。
4. **`discover_display_identity()`**：选"代表性展示身份"的工具查询，parse 时刻排序与 publish 时刻排序在生产节奏下同向（parse 与 publish 同 tick 内完成）；且它是 validation 工具、非展示正确性契约。裁定：可接受。
5. **`national_discharge_source_version()`**：basis 不含 `status`，但**成员集与数据侧同用三态过滤（含 `parsed`）**——run 在 parse+coverage 时刻即进入 national 图层与 digest，publish 转换不改变该图层任何内容。旧 bump 在 publish 时转动 digest 是**虚假翻转**（内容未变、缓存被白 bust）；删除后 digest 只随真实内容变化（新 run 进 ranked 集合）转动。裁定：严格更优。前提（两侧 status 集合一致）以文本断言守卫。
6. **pipeline API `updated_at` 字段**：用户可见语义从"最后 parse/publish 时刻"变为"最后数据变更时刻"，publish 不再推进。裁定：可接受，作为语义收敛在 PR 偏离记录与本 design 显式声明。
7. **forecast API + 前端（`/api/v1/runs`、`/api/v1/runs/{run_id}`、overview 页）**：`updated_at` 原样透出且前端参与排序/新鲜度。裁定：可接受，同第 6 条语义收敛，三条无损理由——(a) 前端排序首键是 `cycle_time`（`overviewData.ts:365-369`），`updated_at` 仅同 cycle_time 的 tie-break，而旧行为下同 tick publish 是单条集合语句（同一个 `now()`），tie-break 本就退化（与第 3 条论证同构）；(b) freshness reference 优先 `validTime`（`overviewDataContracts.ts:297`，`validTime ?? updatedAt ?? cycleTime`——注意 `cycleTime` 仅在 `updatedAt` 亦为空时兜底，不缓解 `updatedAt` 冻结本身，无损结论主要靠 (a)/(c)）；(c) parse→publish 在同一个 autopipe tick 内完成，位移分钟级，对 `staleAfterHours = 6` 判定无实质影响。无测试固化 publish-time 语义（`overviewData.test.ts:107` 仅 fixture 值）。PR 偏离记录同步扩到这两个端点。
8. **scheduler `_terminal_hydro_truth_supersedes_failure`**：DB lane 下 `updated_at` 是 hydro truth time 的唯一来源（投影无 `finished_at`/`created_at` 回退），parse 时刻语义使"publish 之后记录的旧 failure"不再被 supersede（T_parse < T_fail < T_publish 场景由 skip 翻为 retry 分支）。裁定：**latent，不改代码**——生产 scheduler 是 DB-free 契约（`docs/runbooks/current-production-ops.md:32,50,129-135`：`NHMS_SCHEDULER_DB_FREE_REQUIRED=true`、无 `DATABASE_URL`、state backend=file，hydro_run 来自文件 journal 而非 node-27 PG；`infra/env/compute.scheduler-dbfree.env.example:10,22,36`），且 node-27 autopipeline 不写 `ops.pipeline_job`/journal——publish 写入的行与该读路径今天不在同一 state 平面，混合场景不可构造。若 DB-backed scheduler lane 复活，须先重审此条（已记为显式危害注记）。

**失去的副作用（如实记录）**：旧 publish bump 附带一次"coverage 行存在但过时"的一次性自愈机会（refresh 成功后、publish 前发生的带外数据写入会被 backstop 补算一次）。新契约下：**任何变更 run 数据的写者必须自行 bump `updated_at`**（生产写者 `mark_run_parsed` 已如此）；不 bump 的带外写入在旧行为下也只有 publish 一瞬的偶然补算窗口，本就不是可依赖的机制。cron :209-213 的兜底注释若与此不符由实现同步措辞。真实 DB 集成用例记录"refresh 后带外写入（不 bump）→ backstop 是否可见"的实测结论进 receipt。

**否决的备选**：
- 调整 publish 与 refresh 的顺序（issue 推荐 2 前半）：publish 是 phase 3 集合语句、refresh 是 phase 2 per-run 内联，改序意味着 per-run publish 或 refresh 后置整批——前者改变"整 cycle 原子可见"的展示时序，后者把 refresh 挪进发布关键路径。删一行 vs 重排两个 phase，KISS 取前者。
- stale 谓词侧豁免（比较 `data_updated_at` 新列）：加列加迁移，YAGNI。

## D3 — cron 分阶段耗时（条 3）

**决策**：`node27_autopipe_cron.sh` 为 ingest / coverage backstop / MVT prewarm 三阶段各记 epoch 差值日志行，格式统一 `[ts] autopipe: phase=<ingest|coverage_backstop|mvt_prewarm> elapsed_sec=<n>`，保留现有整 tick START/END。**不拆锁**（issue 推荐 3 明示现状可接受）。preflight rc=2 早退分支（:203-207）下 ingest 阶段行的有无须与 spec 口径（"per executed phase"）一致，不得留下判读歧义。纯 bash 追加，无控制流变更。

## 风险与验证映射

| 风险 | 缓解/验证 |
|---|---|
| fallback 重构改变行结果（parity 破坏） | node-27 实机新旧 SQL 同态对比；真实 DB 集成测试强制 fallback 与 fast path 结果一致（执行真实 Python 绑定路径）；单测断言两语句共享同一 candidate SQL 常量 |
| 命名参数绑定在生产崩溃（`_fetch_all` dict 退化 / identity_sql 风格混用） | D1 参数管道改造三条硬约束；带绑定校验的 fake cursor 单测（dict ↔ `%(name)s` 匹配）；node-27 集成测试实打实走 fallback |
| scan 谓词写错列/漏 NULL guard | 单测逐字断言谓词形状（模式同 `test_display_coverage_refresh.py:111`）；EXPLAIN receipt 显示 chunk exclusion |
| `candidate_limit` 未来 >1 静默截断 | 不变量守卫测试（D1.5） |
| `test_migrations.py` 切片空转 | 断言重绑定到 candidate 常量 + 收窄切片 + 变异红证（D1 既有测试处置） |
| publish 去 bump 后 run-scoped MVT 缓存不翻新 | `_run_source_version` basis 含 `status` 的断言测试（若无则补） |
| national digest 前提（两侧 status 集合一致）漂移 | mvt.py 文本断言：digest 成员查询与数据侧查询共用同一三态集合 |
| 有未知 `updated_at` 消费方 | Context 八项清单（grep + 迁移触发器核查 + review round 1 补录）；**方法论注记**：纯文本 grep 对 `SELECT h.*` 星号投影与"投影列喂下游决策函数"两类读路径是盲的——第 7/8 项即为此盲区实例，由 review 席位以数据流追踪补齐并逐条裁定（结论：全部文档层收敛，无代码改动） |
| 真实 DB 上 stale 语义回归 | node-27 真实 DB 集成测试：publish（新形状）后 `_stale_run_ids` 为空；变异对照：旧形状 publish → 非空 |
| cron 改动破坏 tick | `bash -n` + node-27 实机一个 tick 的日志 receipt（三阶段 elapsed 行齐全） |

## 验证入口（Phase 2 / Evidence Floor 消费）

- 本地：`uv run pytest -q tests/test_forecast_api.py tests/test_display_coverage_refresh.py tests/test_migrations.py tests/test_node27_autopipeline_handoff.py tests/test_node27_autopipeline_preflight.py` + 新增测试文件；`uv run ruff check .`；`openspec validate display-coverage-residual-debt --strict --no-interactive`；`bash -n scripts/node27_autopipe_cron.sh`。
- node-27（oracle）：fallback EXPLAIN + parity receipt；真实 DB 集成测试（强制 fallback 走真实绑定路径 + stale 语义 + 带外写入可见性记录）；部署后一个 autopipe tick 的 cron 日志（分阶段 elapsed + backstop 对刚 publish run 报 0 stale）。
- node-22：不涉及。
