# Proposal: candidate-projection-stage-attempt-retention

## Why

Issue #1179：`candidate_state_from_rows` 的 `job_limit` 截断按纯时间新鲜度排序，不认 stage。
当携带最大 attempt 的 `*_forecast_retry_N` 行比 `job_limit` 条其它行旧（cycle-wide 列表混着
download/convert/forcing/parse/… 与 36 流域 cohort 行，超 100 行不难），它被切掉，
`_state_retry_attempt(state, stage="forecast")` 静默降为 0，#1173 的 L2 预算
（`attempt >= retry_limit` → blocked）永不绑定，36 流域自旋在逆序几何下原样复发。
降级路径不报错、不落证据。缺陷 pre-existing（截断随 #833 落地），#1173 是第一个把安全性
押在该投影上的消费者；issue 已用 read-only 探针实测复现（真值 87 → 读出 0）。

## What Changes

（v2，round-1 cross-review 后机制修订——初版"行保留"被评审差分探针证伪可见性安全性，
详见 design.md 头注）**file-journal 投影路径**（生产实际路径）改为 **attempt-floor 载带**：
`pipeline_jobs` 选集维持纯新鲜度 `[:job_limit]` **逐字节不变**（可见性零变化）；截断前对
全量投影输入按消费链（`_canonical_downstream_stage` + `_job_stage_name` +
`effective_retry_attempt`）提取每个 canonical downstream stage 的最大 effective attempt，
落 state 新键 `stage_retry_attempt_floors`；`_state_job_retry_attempt` stage-scoped 读取时
并入 floor。不变量："截断不得改变 stage-scoped attempt 推导的结果"（保护数值上界，非行
群体——proposal 原措辞即此，v2 使实现忠于它）。stage-less flat-first 语义逐字节不变。
**DB 读路径在 SQL 里就截断（`chain_repository_state.py:519-535`），guarantee 显式排除并
路由 #1572（D0 裁决）；共享投影函数使 DB 路径的 floors 在 `job_limit+1` 窗口上计算——
纯数值面顺向改良，选集同样不变（D0 v2 澄清）**。补逆序几何回归测试 + round-1 四个证伪
几何的回归钉；核对全部 stage-scoped attempt 消费点（geometry-B 下 manual mint 维持现状
撞键 no-op——显式边界，follow-up issue 路由）；把"`identity_mismatch_released` 行不得进入
auto-retry"钉成契约（真实 reserve→release 与 reserve→permit→reclaim→release 两条链 shape
钉 + 判定钉 + 四处不变量注释，不改行为）；PR body 引用 #1173 归档 receipt。

## Impact

- `services/orchestrator/chain_repository_state.py`（投影层截断写点，:667-683 区域；
  同文件 :519-535 的 SQL 截断为显式排除面）
- `tests/test_production_scheduler.py`（逆序几何回归 + 既有友好序用例 :42219 保持）
- 消费面核对（不改码，除非核对发现同构缺陷）：`scheduler_state_failure.py:188/:1444/:1900/:1917`、
  `scheduler_state_manual_retry.py:982`、`scheduler_candidates.py:2225`
- 钉住测试：release 行 shape + `should_auto_retry` 判定（不改行为；不变量注释落
  reservation 写点 `file_orchestration_journal.py:1778/:1903`——`error_code: None` 的真实
  载体——与释放写点 `:2804/:2954` 共四处）
- 文档：`docs/runbooks/failed-basin-retry.md` 补一行（预算在逆序几何下现在真实绑定）

Fixture level: expanded（强制触发词：`retry`、persisted/shared state transitions、`scheduler`）。
Upstream suggested level: absent（issue 早于 0.16.0 契约；按触发词定 expanded）。
