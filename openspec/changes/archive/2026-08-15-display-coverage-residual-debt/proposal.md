# Proposal: display-coverage-residual-debt

## Why

Issue #1120 记录了 62824a45（display-coverage refresh 路径下推修复）同一根因排查中的三项残余结构债，均已在当前 master（90dc4a7e）逐行复核确认仍然存在：

1. **request-time CTE fallback 仍是非下推形状（低频高伤）**。`packages/common/forecast_store.py` `_fetch_latest_qhh_display_candidates`（:1202）在 `hydro.run_display_coverage` 不可用/cache miss 时回落的权威 CTE 路径（:1241 起）中，`station_sample_rows`（:1308-1339）对 `met.forcing_station_timeseries`、`river_sample_rows`（:1619-1636）对 `hydro.river_timeseries` 的 run-scoping 都只通过 CTE join 等值（`cr.forcing_version_id = fst.forcing_version_id` / `cr.run_id = rt.run_id`），窗口边界是关联列 `cr.display_start_time`/`cr.display_end_time` 而非字面绑定参数——planner 无法把这些谓词下推进 hypertable chunk scan。这正是 62824a45 修复前 `display_coverage.py` 的形状（同形状实测过 633s wall）。触发条件是 coverage 行缺失/刚 parse 未 refresh/refresh 失败，后果是把本应廉价的降级响应变成 request-time 分钟级全表扫。
2. **publish 阶段 `updated_at = now()` 使每个新 published run 被 backstop 重复计算（恒定小浪费 + 日志失真）**。`scripts/node27_autopipeline.py` phase 2 per-run ingest 末尾已做 inline coverage refresh（:1434-1447），phase 3 `_publish_display_runs`（:1084，调用点 :1802）随后 `SET status = 'published', updated_at = now()`（:1099）；`display_coverage.py` `_stale_run_ids` 的 stale 谓词是 `cov.refreshed_at < h.updated_at`（:707）。于是每个刚 publish 的 run 必然被判 stale，cron backstop 逐个重算（实测一次 pass 重算 18 个 run），且 backstop 日志里的 "stale" 不再指示真实异常。
3. **cron 单 flock 串行三阶段无分阶段耗时（结构性但当前可接受）**。`scripts/node27_autopipe_cron.sh` 只有整 tick 一对 START/END（:192、:232-233），ingest（:195-200）/ coverage backstop（:214-219）/ MVT prewarm（:224-230）三阶段各自耗时不可辨，慢阶段定位只能靠猜。

## What Changes

按 issue 推荐方案（1/2/3）交付，全部收敛在"移植已验证的模式 + 删一个多余动作 + 加日志"，不引入新机制：

- **条 1**：把 62824a45 的 header-预取 + NULL-guarded `scan_*` 下推模式移植到 `_fetch_latest_qhh_display_candidates` 的 CTE fallback。candidate 数恒 ≤1（`QHH_LATEST_SEARCH_LIMIT = 1`），先以同一份 candidate_runs SQL 文本单独执行 header 语句取回标量身份/窗口，header 为空直接返回 `[]`（跳过重查询；store 会话为 REPEATABLE READ readonly，两语句同快照，该短路与旧单语句严格等价），非空则绑定 scan 参数并把重查询的 candidate_runs **钉死到 header 的 run_id**（给 planner 字面谓词、并在结构上保证 scan 标量与 candidate 同源）。行结果与现路径逐列一致（parity 是硬验收）。
- **条 2**：`_publish_display_runs` 的 UPDATE 改为 status-only（去掉 `updated_at = now()`）。`updated_at` 语义收敛为"数据变更时间"（register/parse 仍照常 bump）；MVT tile 修订摘要 `_run_source_version`（`apps/api/routes/hydro_display.py:805-819`）的 revision_basis 已含 `status` 字段，publish 翻状态本身就更换缓存键，翻新语义无损。
- **条 3**：cron 三阶段各自记录 `elapsed_sec`（阶段名可辨的日志行），**不拆锁**（issue 推荐 3 明示现状可接受，拆锁属 cadence 收紧时的后续轴）。

## Impact

- 受影响代码：`packages/common/forecast_store.py`（fallback 路径重构为两语句 + scan 下推）、`scripts/node27_autopipeline.py`（publish UPDATE 一行）、`scripts/node27_autopipe_cron.sh`（日志行）。`packages/common/display_coverage.py` 的 stale 谓词**不改**（条 2 修的是写侧多余 bump，谓词语义本身正确）。
- 受影响 specs：`qhh-latest-display-product`（ADDED：fallback 扫描纪律）、`display-coverage-freshness`（新 capability，ADDED：publish status-only 语义 + backstop 零假 stale + tick 分阶段耗时可观测）。
- 生产面：node-27 request 路径（`GET /api/v1/mvp/qhh/latest-product` 降级分支）、autopipe 10 分钟 tick 行为。node-22 不涉及。
- 回滚：三项相互独立、各自单 commit 可回退；条 1 回退即恢复旧单语句 fallback，条 2 回退恢复双 bump（仅恢复浪费，无数据损坏），条 3 纯日志。

## 兼容性与非目标

- 非目标：forcing handoff / output parse 的 6 min/run 优化（issue 明示下一优化轴）；cron 拆锁；62824a45 已修复的 refresh 路径本身；`hydro.river_timeseries` 代理键切换（#1341/#1342 领域）；fast path `_fetch_latest_qhh_display_candidates_fast` 的形状（已是 coverage 表 LEFT JOIN，非扫描问题）。
- 备选方案"coverage 表缺失时直接 503"被否：现注释（forecast_store.py:1212-1223）明确该路径是 authoritative correctness 兜底，cache miss 仍须正确出数——下推化保留该语义，503 化丧失它。

## Fixture triage

- Issue 无 `Suggested fixture level` 字段（早于该约定，2026-07-25 立项）。Phase 0.5 裁定 **standard**（proposal+design+tasks+spec deltas）：理由是条 1 触生产 request 路径 SQL 重写（parity 关键）、条 2 触生产 pipeline 语义与 MVT 缓存消费方、需 node-27 实机 EXPLAIN/live receipt。
- Minimal mergeable slice（拆分锚点）：条 1 单独成 PR 可合（条 2/3 各自独立、任意子集可合）。
- Readiness 偏离记录：issue 将条 3 标记 needs-triage；本 fixture 按 issue 自身推荐 3（"维持串行但记录分阶段耗时"）收敛为 in-scope 交付，拆锁明确不做——该裁定即条 3 的 triage 结论，不再另行升级。
