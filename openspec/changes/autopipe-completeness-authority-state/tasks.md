# Tasks（#1674）

## 0. 实现前实测

- [x] 0.1 node-27 display RO DSN 复测 `SELECT status, count(*) FROM hydro.hydro_run GROUP BY 1`
  并贴进 #1674 评论；`parsed > 0` 时按 design D2 升级为阻塞项。

## 1. 实现

- [x] 1.1 `scripts/node27_autopipeline.py::_already_ingested_runs`：按 design D1 改写
  第二条 SQL（LEFT JOIN / HAVING，`parsed_at` 保持 `MAX(rt.created_at)`），docstring
  同步改为"published 以权威状态判完整；parsed 仍要求键可见行；遗留 NULL-key run 的
  重算检测仅 init_state"——**docstring 不得含字符串 `hydro.river_timeseries`**
  （`_sql_constants` 连 docstring 一起数，多一处即清零 oracle 假红）；第一条
  superseded 查询与返回逻辑不动；`_publish_display_runs` 的 SQL 逐字不动，仅
  docstring 去掉"matching the `_already_ingested_runs` completeness predicate"。
- [x] 1.2 真实 DB 回归（design D3 (i)-(vi)）：先在修复前跑 (i) 取红证（粘贴断言失败行），
  再修复取绿——红证见 PR #1676 评论 5368820756（node-27 E2）。
- [x] 1.3 SQL 形态 pin，落在 `tests/test_river_ts_text_identity_cleanup.py`（design D3）。

## 2. 验证

- [x] 2.1 `uv run ruff check .`
- [x] 2.2 `uv run pytest -q tests/test_river_ts_text_identity_cleanup.py tests/test_node27_autopipeline_handoff.py tests/test_node27_autopipeline_preflight.py tests/test_display_publish_status_only.py` 全绿（清零 oracle 不改即绿是 must-preserve 的 oracle）。

## Evidence Floor

- [x] E1 本地：2.1/2.2 输出 + 形态 pin 绿。
- [ ] E2 node-27 throwaway DB（(vi) 补齐后重跑）：`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<superuser throwaway DSN> uv run pytest -q tests/test_river_identity_normalization_integration.py -k already_ingested` (i)-(v) 全绿，receipt 必须显示非零 passed 数（全 skip 不算），附修复前 (i) 的红证。
- [ ] E3 node-27 live receipt（design D4）：连续 ≥2 tick `rc=0`、`already_ingested` 回到全量、阻断计数 ≤ 基线、elapsed 分钟级；`hydro_run` 状态分布复测；**遗留 NULL-key cohort 大小**（published 且键可见行为零的 run 数，RO DSN 只读统计）随 receipt 落账（spec 场景 3 要求）。
- [ ] E4 CI：PR Unit Tests 绿。
- [ ] E5 #1674 AC-5（"若采用 (a) 需登记例外 + 挂 #1342 阻塞"）在 D1 下 moot：不引入文本臂、不需例外、不耦合 #1342——PR body 偏离记录明示。
- [ ] E6 PR body 单列有意偏离：`published` + 零 fact 行（retention 已删）现判完整、不回灌（#1442 之前会回灌）。
