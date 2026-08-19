## 1. Implementation

- [x] 1.1 `services/orchestrator/retry.py:76` 改名
      `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`（成员逐字不变）+ 注释声明与
      `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` 的 `"complete"`
      成员差与语义边界；`:563` 用点同步
- [x] 1.2 `services/orchestrator/file_orchestration_journal.py` import 行与
      `:7863` 用点同步改名
- [x] 1.3 `services/orchestrator/scheduler_state_types.py:30` 加对称注释
      （名字/成员不动）

## 2. Tests

- [x] 2.1 成员关系回归锁（tests/test_retry.py，新增 import
      `scheduler_state_types`）三条联立：
      `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES == {"succeeded", "parsed", "published"}`；
      `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES ==
      {"succeeded", "parsed", "published", "complete"}`（单独钉 4 成员侧，
      挡「scheduler 侧被统一成 3 成员」的合流方向）；
      `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES ==
      DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"}`

## 3. Verification

- [x] 3.1 uv run pytest -q tests/test_retry.py
      tests/test_file_orchestration_journal.py
      tests/test_retry_cancel_consistency.py（现有断言零改动全绿）
- [x] 3.2 rename 零残留（全仓正反两向 grep，期望残留写死为清单）：
      (a) `grep -rn "DURABLE_HYDRO_SUCCESS_STATUSES" services packages apps
      workers tests` 命中必须恰为：`scheduler_state_types.py:30` 定义（含
      1.3 对称注释）、`scheduler_state.py:163`、`scheduler.py:82`（经
      `scheduler_state` 转导入的既有消费者）、
      `scheduler_state_decision.py:46`/`:214`、`scheduler_state_compat.py:20`、
      `retry.py`/`file_orchestration_journal.py` 内至多只允许注释交叉引用
      （可为零命中）、
      tests/test_retry.py 新锁中对 `scheduler_state_types` 限定引用；
      (b) 反向：`grep -rn "MANUAL_RETRY_DURABLE_SUCCESS_STATUSES"` 不得出现
      在任何 `scheduler_state*` 模块中
- [x] 3.3 uv run ruff check services tests
- [x] 3.4 openspec validate durable-status-name-split --strict --no-interactive
- [ ] 3.5 follow-up issue（`"complete"` 成员差是否统一）已立案并链接于 PR
