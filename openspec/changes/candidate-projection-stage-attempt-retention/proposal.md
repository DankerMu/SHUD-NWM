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

**file-journal 投影路径**（生产实际路径）的截断阶段做 stage-aware 保留：切 `[:job_limit]`
之前，先保留每个"最大 effective attempt 非零"的 canonical downstream stage 上 attempt
最大的那一行（把预算真值锁进投影；受 `job_limit` hard cap 约束），剩余名额按现有时间序
过滤式填充。不变量："被截断的投影必须保留每个 canonical stage
的 attempt 上界"（保护数值上界，非行群体）。**DB 读路径在 SQL 里就截断
（`chain_repository_state.py:519-535`，`ORDER BY … LIMIT job_limit+1`），投影层保留救不了它
——显式排除并路由独立 issue（D0 裁决）**。补逆序几何回归测试；核对全部 stage-scoped attempt
消费点（其中 failure 侧 mint 路径是真实行为变化，两腿钉住——E12）；把
"`identity_mismatch_released` 行不得进入 auto-retry"钉成契约（真实 reserve→release 序列
shape 钉 + 判定钉 + 四处不变量注释，不改行为）；PR body 引用 #1173 归档 receipt。

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
