# node-22 operator-action surface + targeted re-entry (#1186 + #1543 + #1555 + #1768 + #1820)

## Why

node-22 db-free scheduler 会发出几类「需要人工」的终态决策，本批把这类承诺补成真的：

- **#1186**：四类决策在 evidence 里写着 `manual_retry_required: true`，但节点上没有列举面：
  - `permanent_failure`
  - `cancelled_manual_retry_required`
  - `blocked_strict_warm_start_init_state_mismatch`
  - `blocked_journal_predecessor_identity_quarantine`

  display API 的 409 还指向不存在的 runbook slug `node22-control-plane-manual-recovery`。
- **#1555 / #1768**：breaker fail-stop 与 strict warm-start 预算耗尽这两个 blocked 决策，都是从 completed-skip 降级而来。现有 manual-retry marker 对 completed 行结构性失效：
  - `record_manual_repair` 对 terminal-success 行抛 `RetryNotFoundError`；
  - completed-skip early return 早于 `manual_retry_requested`。

  唯一出路是抬全局 `NHMS_SCHEDULER_RETRY_LIMIT`（#1767），或者带外伪造提交身份。
- **#1543**：`operator_action_required` 只看 predecessor 槽位，不看 §8.6 这一趟是否真的发射了。三条非瞬时 skip 臂会让证据说「无需介入」，而缺口永不闭合；runbook 的兜底在 summarized pass 上也读不到。
- **#1820**：`recover-released-identity-blocked-reservation` 的发现半边只要遇到一行畸形 flat pipeline-job 记录，整个命令就中止。这个命令恰恰是救火时用的。

## What Changes

- **A（#1186）**：
  - 新增只读子命令 `list-operator-actions`：扫最近 N 个 pass evidence，按 decision 列出上述四类候选；非空时退出码 1。
  - bounded summarization 白名单补 `retry_policy` 的 attempt / retry_limit / occurrences / manual_retry_required。
  - 在 API 已承诺的 slug 处新建 `docs/runbooks/node22-control-plane-manual-recovery.md`，并有测试钉住 slug 文件存在。
  - `failed-basin-retry.md` 为每类决策写处置段。
- **B（#1543）**：
  - emitter 在本 successor 的非瞬时 skip（manifest not ready / model not available / cap 截断）上写 `predecessor_emission_blocked`，进 bounded 白名单；
  - 截断记录能定位到 successor；
  - runbook 改为两个布尔一起读。
- **C（#1820）**：只在 `query_released_identity_blocked_jobs` 内做逐行、逐 cycle 隔离。
  - 可跳过的原因是显式 allowlist，其余一律 re-raise，包括预算拒绝与 containment fault；
  - 跳过项以 `skipped` 呈现在 receipt 里；
  - 调度热路径（`_cycle_rows` 记忆化链）零改动。
- **D（#1555 + #1768）**：新增 operator 确认物，一次授权一次重入。
  - 写侧：新子命令 `confirm-operator-reentry`（默认 dry-run，`--attest` 才写），经 `insert_pipeline_event(entity_type="forecast_cycle")` 写入新的 `event_type`；
  - 读侧：新 repository accessor；
  - 两个铸造点在 pin 值与现值严格相等时，一次性放行既有 retry 决策。pin 值是 rerun 必然改变的量（breaker 用 occurrences，预算用 attempt），重投后自动失效，再失败时 breaker / 预算重新接管。

## Impact

- 代码：
  - `services/orchestrator/scheduler_candidates.py`
  - `scheduler_evidence_payload.py`
  - `scheduler_backfill_predecessor.py`
  - `file_orchestration_journal.py`
  - `operator_released_reservation_recovery.py`
  - `cli.py`
  - 新模块：`operator_action_listing.py`、`operator_reentry_confirmation.py`
- 文档：
  - `docs/runbooks/node22-control-plane-manual-recovery.md`（新）
  - `failed-basin-retry.md`
  - `scheduler-dbfree-typed-reasons.md`
- 规格：`production-scheduler-orchestration`、`file-state-snapshot-index`。
- 两处 forced-resubmit 白名单、breaker 判定与阈值、预算判定、completed-skip 评估顺序都不改。
