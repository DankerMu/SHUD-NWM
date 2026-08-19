# Proposal: durable-status-name-split (#1155)

## Why

Issue #1155（entropy；上游 body 缺失，口径以 issue comment 重derive记录为准，
零行为变更）：同名常量 `DURABLE_HYDRO_SUCCESS_STATUSES` 存在两套成员——

- `services/orchestrator/retry.py:76`：`{"succeeded", "parsed", "published"}`
  （3 成员）；唯一自用点 `:563`（DB 路径 manual retry 拒绝谓词：durable run
  成功 → `RetryNotFoundError`）。
- `services/orchestrator/scheduler_state_types.py:30`：
  `{"succeeded", "parsed", "published", "complete"}`（4 成员）；消费面
  `scheduler_state_decision.py:214`、`scheduler.py:82`、
  `scheduler_state.py:163`（F401 re-export）、`scheduler_state_compat.py:20`
  （frozen `__all__` 公共 API）。

`file_orchestration_journal.py:73` 从 **retry** 导入 3 成员版用于 `:7863`
`_manual_retry_source_for_run`（file 路径 manual retry 孪生谓词），同一模块
又大量导入 scheduler_state 面符号——一个模块内同名符号两种语义流动。读者
（与 grep）无法从名字区分语义，重构时极易把两套错误合流。

## What Changes

零行为变更的名字拆分：

- `retry.py:76` 的 3 成员集合改名 `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`
  （语义：durable hydro-run 成功、使 run 不可人工重试的状态集），成员逐字
  不变；`retry.py:563` 与 `file_orchestration_journal.py`（import 行 +
  `:7863`）同步改名。旧名从 retry.py 删除（全仓无其他消费者、无测试直接
  引用，不留 alias）。
- `scheduler_state_types.py:30` 的 4 成员版**保持原名不动**（消费面广 +
  compat frozen `__all__` 钉住该名字）。
- 两处定义各加注释：显式声明与对方的成员差（`"complete"`）、语义边界与
  本 change 名，杜绝再次同名合流。
- 新增回归锁测试：断言
  `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES == DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"}`
  （成员关系显式化——任一侧成员漂移或误合流即红）。

## Non-Goals

- **不**统一成员（`"complete"` 是否应阻断 manual retry 属行为变更 + 产品
  裁定，由独立 follow-up issue 跟踪，本单实现期间用 issue-scribe 立案）。
- `scheduler_state_compat.py` frozen `__all__` 不动。
- 两个谓词的判定逻辑、异常类型、调用面均不动。

## Risk triage

- Fixture level: compact（纯 rename + 注释 + 一条成员关系锁）。
- Repair intensity: low。
- Risk packs: entropy/naming selected（rename 完整性：旧名在 retry 模块与
  journal 内零残留，全仓 grep 断言）；其余 not selected（无状态语义、无
  行为变更、无并发面）。

## Must preserve

- manual retry 两条孪生路径（DB `retry.py:563` / file journal `:7863`）行为
  逐字不变：durable status `"complete"` 依旧**不**阻断重试。
- `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` 名字、成员、全部
  消费面不动；`scheduler_state_compat` 公共 API 面不动。
- `tests/test_retry.py`、`tests/test_file_orchestration_journal.py`、
  `tests/test_retry_cancel_consistency.py` 现有断言零改动、全绿。

## Seams under test

- 常量成员关系直测（新回归锁，落 `tests/test_retry.py`）。
- 全仓 grep 残留检查（rename 完整性，orchestrator 验证步执行）。

## Evidence mapping

- 验收 1（rename 零残留 + 成员逐字不变）→ tasks 2.1 + 3.2 grep。
- 验收 2（成员关系锁）→ tasks 2.1。
- 验收 3（两孪生谓词行为不变）→ tasks 3.1 三套件全绿（现有断言即回归锁）。
- Verification：`uv run pytest -q tests/test_retry.py
  tests/test_file_orchestration_journal.py tests/test_retry_cancel_consistency.py`
  + ruff + openspec validate；follow-up issue 立案链接记入 PR。
