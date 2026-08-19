# Proposal: durable-status-name-split (#1155)

## Why

Issue #1155（entropy；上游 body 缺失，口径以 issue comment 重derive记录为准，
零行为变更）：同名常量 `DURABLE_HYDRO_SUCCESS_STATUSES` 存在两套成员——

- `services/orchestrator/retry.py:76`：`{"succeeded", "parsed", "published"}`
  （3 成员）；唯一自用点 `:563`（DB 路径 manual retry 拒绝谓词：durable run
  成功 → `RetryNotFoundError`）。
- `services/orchestrator/scheduler_state_types.py:30`：
  `{"succeeded", "parsed", "published", "complete"}`（4 成员）；消费面
  `scheduler_state_decision.py:46`（import）/`:214`（用点）、
  `scheduler.py:82`（经 `scheduler_state` 转导入）、
  `scheduler_state.py:163`（F401 re-export，owner module）、
  `scheduler_state_compat.py:20`（`SCHEDULER_STATE_COMPAT_REEXPORT_NAMES`
  元组成员——非 `__all__`；`scheduler.py:228-231` 装到门面，
  `scheduler_state_compat.py:236-240` 有 import-time 守卫，owner 缺名直接
  `RuntimeError`，约束强于 `__all__`，故该名不动）。

`file_orchestration_journal.py:73` 从 **retry** 导入 3 成员版用于 `:7863`
`_manual_retry_source_for_run`（file 路径 manual retry 孪生谓词）；同一模块
`:57` 还以 `COMPLETED_HYDRO_STATUSES` 之名导入了 4 成员集的字面量副本
（`chain_repository.py:19`）——一个模块内两种语义共存，唯 retry 那份与
scheduler_state_types 撞名。读者（与 grep）无法从名字区分语义，重构时极易
把两套错误合流。

## What Changes

零行为变更的名字拆分：

- `retry.py:76` 的 3 成员集合改名 `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`
  （语义：durable hydro-run 成功、使 run 不可人工重试的状态集），成员逐字
  不变；`retry.py:563` 与 `file_orchestration_journal.py`（import 行 +
  `:7863`）同步改名。旧名从 retry.py 删除（全仓无其他消费者、无测试直接
  引用，不留 alias）。
- `scheduler_state_types.py:30` 的 4 成员版**保持原名不动**（消费面广 +
  compat re-export 面（import-time 守卫）钉住该名字）。
- 两处定义各加注释：显式声明与对方的成员差（`"complete"`）、语义边界与
  本 change 名，杜绝再次同名合流。
- 新增回归锁测试（三条联立，缺一不可）：
  `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES == {"succeeded", "parsed", "published"}`、
  `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES ==
  {"succeeded", "parsed", "published", "complete"}`、
  `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES ==
  DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"}`——第二条单独钉死 4 成员侧，
  挡住「把 scheduler 侧统一成 3 成员」这一唯一真正改行为的合流方向（前两条
  联立才能同时挡两侧漂移与双向合流）。

## Non-Goals

- **不**统一成员。`"complete"` 差异是**词汇/域归属缺陷**而非产品判断题：
  `"complete"` 不在 `hydro.run_status` enum 内（`db/migrations/000003_enums.sql:10-24`
  + `000013_enum_remediation.sql:3` 追加 pending 后再无 ADD VALUE），它是
  pipeline/cycle 词汇（`chain_stages.py:11`、`chain.py:524`）；而 4 成员集
  的两个消费点比的都是 hydro_run 状态（`retry.py:562` `_hydro_run_status`、
  `scheduler_state_decision.py:214` 证据 `"terminal_source": "hydro_run"`）
  ——DB 车道上第 4 成员不可达。修复方向连同三份字面量副本
  （`chain.py:216` / `chain_repository.py:19` 的 `COMPLETED_HYDRO_STATUSES`
  ——tests/ 对该名零命中，无 parity 锁）由独立 follow-up issue 跟踪，本单
  实现期间用 issue-scribe 立案并把上述 enum 证据写进 issue body。
- `scheduler_state_compat.py` re-export 面（import-time 守卫）不动。
- 两个谓词的判定逻辑、异常类型、调用面均不动。

## Risk triage

- Fixture level: compact（纯 rename + 注释 + 一条成员关系锁）。
- Repair intensity: low。
- Risk packs: entropy/naming selected（rename 完整性：旧名在 retry 模块与
  journal 内零残留，全仓 grep 断言）；其余 not selected（无状态语义、无
  行为变更、无并发面）。

## Must preserve

- manual retry 两条孪生路径（DB `retry.py:563` / file journal `:7863`）行为
  逐字不变：3 成员逐字不变 ↔ `tests/test_retry_cancel_consistency.py:686-711`
  参数化断言（`["succeeded","parsed","published"]` → `RetryNotFoundError` +
  零 mutation）与 `:714-738`（API 404 面）不变；file 孪生由
  `tests/test_file_orchestration_journal.py` 既有 manual-retry 断言锁。
  （注：`"complete"` 在 DB 车道不可测——hydro.run_status enum 无该值，该
  测试文件自己的 `HYDRO_RUN_STATUS_ENUM`:22-34 也没有；spec Scenario 2 的
  「complete 不阻断」只能在 file 孪生/常量层面断言。）
- `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` 名字、成员、全部
  消费面不动；`scheduler_state_compat` re-export 面（含 import-time 守卫）
  不动。
- `tests/test_retry.py`、`tests/test_file_orchestration_journal.py`、
  `tests/test_retry_cancel_consistency.py` 现有断言零改动、全绿。

## Seams under test

- 常量成员关系直测（新回归锁，落 `tests/test_retry.py`）。
- 全仓 grep 残留检查（rename 完整性，orchestrator 验证步执行）。

## Evidence mapping

- 验收 1（rename 零残留 + 成员逐字不变 + 1.3 对称注释落位）→ tasks 2.1 +
  3.2 全仓 grep（期望残留清单含 1.3 注释）。
- 验收 2（成员关系三条锁）→ tasks 2.1（落 tests/test_retry.py，新增
  `scheduler_state_types` import）。
- 验收 3（两孪生谓词行为不变）→ tasks 3.1 三套件全绿：DB 谓词锁 =
  `test_retry_cancel_consistency.py:686-711`/`:714-738`；file 孪生锁 =
  `test_file_orchestration_journal.py` 既有 manual-retry 断言；
  `test_retry.py` 不覆盖 `retry.py:563`（对 hydro_run 零引用），在本单中
  只承载新的常量关系锁。
- Verification：`uv run pytest -q tests/test_retry.py
  tests/test_file_orchestration_journal.py tests/test_retry_cancel_consistency.py`
  + ruff + openspec validate；follow-up issue 立案链接记入 PR。
