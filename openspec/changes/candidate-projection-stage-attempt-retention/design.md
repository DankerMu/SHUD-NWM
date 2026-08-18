# Design: candidate-projection-stage-attempt-retention

Fixture level: expanded
Project profile: NHMS

> **round-1 cross-review 后机制修订（v2）**：初版机制是"截断时保留每 stage attempt 上界行"。
> PR #1574 round-1 评审 + 独立 verifier 以差分探针证伪了该机制的可见性安全性
> （S1 latest_job 翻转把已 publish 候选读成 failed、S2 保留行挤掉 completed-stage 成功证据、
> S3 陈旧 ACTIVE 行复活、S4 flat retry_count 被挤压低——S1/S2 与未截断真值矛盾）。
> 机制改为 **attempt-floor 载带**：`pipeline_jobs` 选集回到纯新鲜度 `[:job_limit]`
> （逐字节与改前一致，可见性零变化），截断前从全量投影输入按消费链提取
> `stage_retry_attempt_floors`，stage-scoped attempt 推导读取时并入 floor。
> proposal 的原不变量措辞——"保护的是数值上界，不是行群体"——正是本方向；
> 初版把不变量过度具体化成了行保留机制。

## Change surface

- `services/orchestrator/chain_repository_state.py` `candidate_state_from_rows` 截断块——
  选集回退为纯新鲜度 `[:job_limit]`；截断前构建 floors 并落 state 新键
- `services/orchestrator/scheduler_state_rows.py` `_state_job_retry_attempt`——stage-scoped
  推导并入 floor（stage-less flat-first 逐字节不变）
- `tests/test_production_scheduler.py`：逆序几何回归腿 + 三个已证伪几何的回归钉 +
  既有友好序用例 `test_strict_warm_start_budget_binds_on_the_truncated_production_geometry`
  (:42219) 保持
- 只核对不改码：`scheduler_candidates.py:2225`（L2 预算）、`scheduler_state_failure.py`
  :188/:1444/:1900/:1917、`scheduler_state_manual_retry.py:982`、
  `scheduler_state_evidence_owner.py:110`
- 钉住（不改行为）：`file_orchestration_journal.py` reservation 写点 :1778/:1907
  （`error_code: None` 的真实载体）+ reclaim 重 seed 写点（C2）+ 释放转换两处 + `retry.py`
  `classify_failure`/`should_auto_retry` 链
- 文档：`docs/runbooks/failed-basin-retry.md`（机制描述随 v2 更新）

## D0: 范围裁决——file-journal 投影 only（fixture review P1-1；round-1 C3 补充）

**DB 读路径在 SQL 里就截断**：`chain_repository_state.candidate_state` 的查询
（:519-525 `ORDER BY COALESCE(updated_at, …) DESC … LIMIT %s`，:535 绑定 `job_limit + 1`）
用与 `_pipeline_job_truth_sort_key` 相同的新鲜度序在数据库侧丢弃旧行——逆序几何下携带最大
attempt 的行**根本到不了投影层**，投影层载带救不了它。该路径 live
（`chain_repository.py:126` → 默认 repository @ `chain_forecast_orchestrator_cycle.py:79`）。

裁决：guarantee **收窄到 file-journal 投影路径**（`file_orchestration_journal.py:887` 读
cycle 全行后投影——生产实际路径，#1173 归档 receipt 佐证）；DB 缺口已路由 #1572。

**共享函数的事实澄清（round-1 C3）**：`candidate_state_from_rows` 是两条路径共享的投影函数，
DB 路径喂进来的 `job_limit+1` 行窗口上 floors 同样会被计算——这是**纯数值面**的顺向改良
（+1 行的 attempt 可抬高 floor），**行选集在两条路径上都逐字节不变**（v2 机制不改选集）。
"outside this guarantee" 指的是窗外上界行救不回来的保证边界，不是"DB 路径行为一个比特
都不变"。

## D1: attempt-floor 载带（核心不变量，v2）

**不变量：截断不得改变 attempt 推导的结果——对每个 canonical downstream stage，
`_state_retry_attempt(state, stage=S)` 在截断后投影上的返回值 == 在未截断输入上的返回值。
保护数值上界，不动行群体。**

实现：

1. **选集不变**：`pipeline_jobs` 维持现状纯新鲜度倒序切 `[:job_limit]` 再正序回排，
   逐字节与改前一致。所有由行群体派生的 state 键（`pipeline_status`/`stage`/`failed_stage`/
   `restart_stage`/completed-stage 证据/active 扫描/`retry_count` 聚合/`latest_job`）
   自动与改前全同——S1/S2/S3/S4/C3 的结构性修复。
2. **floors 构建**：截断前对全量（post-filter）行一次线性扫描，得
   `{canonical_stage: max_effective_attempt}`（仅收非零；attempt 0 无信息量）。
   stage/attempt 推导**必须与消费链同构且同函数**：
   `_canonical_downstream_stage(_job_stage_name(job))` + `effective_retry_attempt(job_id,
   retry_count)`（builder 放在 `scheduler_state_rows`，与消费者同模块，杜绝分叉）；
   **禁止** `chain_repository_state._STAGE_ALIASES`（含 download 漏 copyback）、禁止
   纯 job-id 子串解析。round-1 C1 钉死两个必须进腿的行形：attempt 只活在持久化
   `retry_count` 的行（如 `retry.py:1101-1116` mint 的 `_retry_active` + :1013 的
   `retry_count=N`）与 stage 空只有 `job_type` 的行（`_job_stage_name` 回退半边）。
3. **state 新键** `stage_retry_attempt_floors`（恒在，可为空 dict）。新键对既有 Mapping
   消费者惰性（全部走 `.get`/显式键读取，无 key 枚举面）。
4. **读取并入**：`_state_job_retry_attempt(state, canonical_stage)` 在 canonical_stage
   非 None 时把 `floors.get(canonical_stage, 0)` 并入 max。**stage-less flat-first 语义
   逐字节不变**（:440-444 docstring 已钉——floors 绝不渗入无 stage 读取）。非本投影产出的
   state（无 floors 键）行为不变。
5. **DB 路径**：同一 builder 在 `job_limit+1` 窗口上跑（D0 澄清条），无门控——门控会让
   两条路径分叉，代价高于收益（round-1 C3 verifier 口径）。

## D2: 必须保持（v2 后大幅收缩——选集不变使可见性面按构造关闭）

- `state_truncated`（:900 附近）/ `pipeline_jobs_total`（:898）语义不变。
- `event_limit` 截断完全不动；`_record_allowed_for_compute_state_terminal` 过滤先于 floors
  构建（floors 只看进入投影的行——与行扫描的可见域一致）。
- **选集与派生键零变化是本设计的承重墙，必须用 round-1 证伪几何做回归钉**（E-v2 腿）：
  - S1 再入几何（candidate-scoped retry 行 + 更新 run 成功行全窗外 + cycle-scope filler）→
    `pipeline_status`/`failed_stage`/`latest_job` 派生与改前全同（None/None），
    **且** `_state_retry_attempt(state, stage="forecast")` 读出真值；
  - S2 几何（窗外 wedge + 窗内 completed-stage 成功行）→ `restart_stage='forcing'`、
    completed-stage 证据保持；
  - S3 几何（窗外 ACTIVE running + slurm binding 行）→ `_state_active_jobs` 为空、决策不变；
  - S4 几何（retry_count=4 载体行在窗内）→ `state["retry_count"] == 4` 保持。
- 既有 :42219 用例是回归钉，**不得放宽**。

## D3: 消费面核对 + 行为边界（v2 修订）

stage-scoped 调用点逐一核对并在 PR 记录结论：

- `scheduler_candidates.py:2225`（auto L2 预算，stage 是常量 `"forecast"`）：floors 直接
  命中——逆序几何下 `("blocked", "strict_warm_start_retry_budget_exhausted")`（E5）。
  **这是 issue #1179 的目标面，不依赖 `_failed_stage` 行可见性。**
- `scheduler_state_failure.py:188/:1444/:1900`：stage 可解析时（failed 行在窗内——生产
  常态）读出真值；数值记账面变真。
- **`:1917` manual-retry mint 的边界（v2 与初版的差异）**：`:1917` 是
  `_state_retry_attempt(state, stage=_failed_stage(state))`。failed 行在窗内时 mint 今天
  就正确（行扫描直接读到）。geometry B（failed 行在窗外 + terminal-completion filler 使
  三键全空）下 `_failed_stage` 恒为 None（v2 选集不变，行不可见）→ stage-less flat-first
  读 0 → mint `_retry_1` 撞既有键静默 no-op——**维持现状，不在本 change 修**。初版靠行
  可见性顺带修它，但那正是被 S1/S2 证伪的机制。边界钉住（E11-v2 硬断言 + E12-v2 现状钉），
  缺口路由 follow-up issue（编号见 tasks 3.1/PR body）：manual mint 需要 stage 解析不依赖
  行可见性（如 marker 自带 stage 或读 floors 键集）。
- `manual_retry:982`（family floors）：`_fallback_previous_attempt:978-983` 早退语义不变
  （选集不变）。
- 无 stage 调用点走 flat-first，**floors 不得改变其行为**（E12''-v2 变异钉）。

## D4: released 行钉住（不改行为；round-1 C2/C4 修订）

现状保护机制：`error_code` 为空来自 **reservation 写点**（:1778/:1907 显式
`"error_code": None`——:1907 是 reclaim 重 seed 写点，round-1 C2 证实它同样 seed released
行且此前无钉）；释放转换 `apply_accepted_submit_transition`
（accepted_submit_identity.py:310-327）整行拷贝不触碰 `error_code`。
`should_auto_retry` → `classify_failure(None)` → `UNKNOWN_FAILURE` 不可重试
（`SLURM_RESERVATION_LOST` 在 transient 集合 retry.py:37——未来在写点盖瞬时 code
即打开重复提交）。钉法：

- E6a（shape 钉，真实 reserve→release 序列）：释放后行 `status == "reservation_lost"` 且
  `error_code` 为空。
- E6b（判定钉）：该行 `should_auto_retry` 为假。
- **E6c（round-1 C2）**：reclaim 链路钉——reserve → permit（absence_retry_permitted）→
  `reclaim_pipeline_job_reservation`（:1835-1839 前置条件即该形状；:1911 重 seed
  `error_code: None`）→ release，断言 released 行 `error_code` 为空 + `should_auto_retry`
  为假（:1911 塞瞬时 code 的变异必须咬红）。
- 不变量注释：reservation/reclaim 写点 + 释放转换两处。**`permit_pipeline_job_retry`
  处（round-1 C4）措辞必须写成**："本行产出 absence_retry_permitted——spec 显式排除的
  reclaim door 之一，null `error_code` 在此只是转换未引入新码的事实；auto-retry isolation
  契约的主语是 identity_mismatch_released 子形。"不得把 isolation 契约的帽子扣在有意可
  重试的行形上（`_verified_accepted_submit_forecast_retry`
  chain_forecast_orchestrator_cycle.py:899-908 与 reclaim 谓词 :1835-1839 吃的正是它）。
- 与既有 requirement "Lost reservations are not mark sources"（job-retry-mechanism
  spec :1194-1199，两扇 reclaim 门保持开）不冲突：本钉只管 auto-retry 分类决策。

## D5: #1173 tasks-4.1 receipt（不变）

归档的 `openspec/changes/archive/2026-07-27-scheduler-identity-blocked-convergence/tasks.md:39-40`
已含 2026-07-29 receipt（佐证 D4：`error_code=null` 实机成立、released 后无新
`*_retry_N`）。**归档文件不编辑**；PR body 引用即可。

## D6: 性能

floors 构建是一次线性扫描 + 每 stage 一个 max，O(n)；非热循环。Python 3.11 兼容
（#1566 教训：禁 3.12+ API）。

## Seams under test

- 投影 seam：`candidate_state_from_rows`（floors 正确性 / 选集逐字节不变 / 三个证伪几何
  回归钉 / C1 两个行形）。
- 读取 seam：`_state_job_retry_attempt` floor 并入 + stage-less flat-first 不变。
- 预算 seam：`_strict_warm_start_terminal_mismatch_decision` 逆序 + `N >= retry_limit` →
  `("blocked", "strict_warm_start_retry_budget_exhausted")`。
- 边界 seam：geometry B 下 `_failed_stage` 保持 None + mint 现状钉（follow-up issue 路由）。
- 钉住 seam：真实 reserve→release + reclaim 链路行 shape 与 `should_auto_retry`。

## Non-goals

- **DB 读路径的 SQL 截断**（D0 裁决，#1572）；geometry-B 下 manual mint 的 stage 解析
  （follow-up issue，v2 显式边界）；attempt 词表 `Literal`/`Enum` 化；`event_limit` 同类
  问题；跨 pass no-progress 断路器（#1118）；#1173 已合并的 L1/L2 逻辑本身；释放路径的
  行为守卫；截断策略的其它启发式。
