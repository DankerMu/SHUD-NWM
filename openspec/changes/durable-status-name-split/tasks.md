## 1. Implementation

- [ ] 1.1 `services/orchestrator/retry.py:76` 改名
      `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`（成员逐字不变）+ 注释声明与
      `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES` 的 `"complete"`
      成员差与语义边界；`:563` 用点同步
- [ ] 1.2 `services/orchestrator/file_orchestration_journal.py` import 行与
      `:7863` 用点同步改名
- [ ] 1.3 `services/orchestrator/scheduler_state_types.py:30` 加对称注释
      （名字/成员不动）

## 2. Tests

- [ ] 2.1 成员关系回归锁（tests/test_retry.py）：
      `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES ==
      scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES - {"complete"}`
      且逐字等于 `{"succeeded", "parsed", "published"}`

## 3. Verification

- [ ] 3.1 uv run pytest -q tests/test_retry.py
      tests/test_file_orchestration_journal.py
      tests/test_retry_cancel_consistency.py（现有断言零改动全绿）
- [ ] 3.2 rename 零残留：`grep -rn "DURABLE_HYDRO_SUCCESS_STATUSES"
      services/orchestrator/retry.py services/orchestrator/file_orchestration_journal.py`
      仅允许出现在注释交叉引用中，不得再作为符号引用
- [ ] 3.3 uv run ruff check services tests
- [ ] 3.4 openspec validate durable-status-name-split --strict --no-interactive
- [ ] 3.5 follow-up issue（`"complete"` 成员差是否统一）已立案并链接于 PR
