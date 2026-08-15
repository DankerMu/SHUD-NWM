# Tasks: display-coverage-residual-debt

## 1. 实现（implementer）

- [x] 1.1 条 1：`packages/common/forecast_store.py` fallback 重构——candidate_runs SQL 文本提取为模块常量（header 与重查询共享，禁止手抄第二份）；新增 header 语句（7 标量投影）；header 空 → 返回 `[]` 短路；header 非空 → 绑定 NULL-guarded `scan_*`（station 5 参 / river 5 参，谓词形状与 `display_coverage.py` 逐字同构）+ candidate_runs 钉死 `AND h.run_id = %(scan_run_id)s`。
- [x] 1.2 条 1 参数管道（design D1 三条硬约束）：fallback 两语句用命名参数且走 Mapping 兼容执行路径（扩展 `_fetch_all` 接受 `Mapping | Sequence` 或 fallback 直接 `cursor.execute`，不影响其余位置参数调用点）；`_qhh_latest_strict_identity_sql` 加参数风格开关（同一 SQL 模板，禁止复制文本），fast path 与 `_fetch_latest_qhh_display_unavailable_context` 保持位置参数不动。
- [x] 1.3 条 2：`scripts/node27_autopipeline.py` `_publish_display_runs` UPDATE 去掉 `updated_at = now()`（status-only）；`scripts/node27_autopipe_cron.sh:209-213` 兜底注释措辞与新契约核对（不符则同步）。
- [x] 1.4 条 3：`scripts/node27_autopipe_cron.sh` 三阶段各记 `elapsed_sec` 日志行（`phase=<ingest|coverage_backstop|mvt_prewarm> elapsed_sec=<n>`），保留整 tick START/END，不拆锁；preflight rc=2 早退分支的阶段行口径与 spec（per executed phase）一致。
- [x] 1.5 测试：
  - fallback：header 先行且与重查询共享同一 candidate SQL 常量；scan 谓词存在于两个 sample CTE（文本断言）；钉死谓词存在；header 空短路（重查询零执行）；header 标量正确绑定；带绑定校验的 fake cursor（dict ↔ `%(name)s` 匹配）实际执行 fallback 路径。
  - `candidate_limit ≡ 1` 不变量守卫（`QHH_LATEST_SEARCH_LIMIT` >1 时断言失败）。
  - `tests/test_migrations.py:387`：断言重绑定到抽出的 candidate 常量 + fallback 专属切片（终点收窄到 `def _fetch_latest_qhh_display_candidates_fast`），占位符同步命名风格；变异红证（打乱 fallback 索引列序 → 红）。收窄后仅存在于 unavailable-context 的两条断言（test:492 `h.status NOT IN (...)` ↔ :1966、test:495 `QHH_LATEST_CONTEXT_LIMIT` ↔ :1889）改绑到独立切片（`def _fetch_latest_qhh_display_unavailable_context` → `def _fetch_station_for_series`），断言总数不得减少。
  - publish：SQL 文本断言不含 `updated_at`（红证：加回则红）。
  - MVT：`_run_source_version` revision_basis 含 `status` 的断言（若既有测试已覆盖则引用，未覆盖则补）；mvt.py 文本断言 national digest 成员查询与数据侧查询共用同一三态 status 集合。
  - 真实 DB 集成（`integration` marker，node-27 执行）：(a) 强制 fallback（`_run_display_coverage_available` 置 False）实例化 store 走真实绑定路径，结果与 fast path 一致；(b) seed run+coverage 行 → 新形状 publish → `_stale_run_ids` 为空；变异对照：旧形状 publish（带 `updated_at = now()`）→ 非空；(c) refresh 后带外写入（不 bump `updated_at`）→ 记录 backstop 可见性实测结论（进 receipt，不设通过阈值）。
- [x] 1.6 偏离记录：实现与本 fixture 任何出入逐条报告（无偏离须显式声明）。

## 2. 验证（orchestrator）

- [x] 2.1 本地：`uv run pytest -q tests/test_forecast_api.py tests/test_display_coverage_refresh.py tests/test_migrations.py tests/test_node27_autopipeline_handoff.py tests/test_node27_autopipeline_preflight.py <新增测试文件>` 全绿；`bash -n scripts/node27_autopipe_cron.sh`。
- [x] 2.2 本地：`uv run ruff check .` 通过。
- [x] 2.3 本地：`openspec validate display-coverage-residual-debt --strict --no-interactive` 通过。
- [x] 2.4 node-27：定向真实 DB pytest（1.5 集成用例 a/b/c）通过。
- [x] 2.5 node-27：fallback EXPLAIN receipt——新两语句形状对两 hypertable 均 chunk exclusion / index scan（无全表 seq scan）；同一 DB 状态下新旧 fallback 行结果逐列一致（parity）。
- [x] 2.6 node-27：部署后一个 autopipe tick 的 cron 日志 receipt——三阶段 `elapsed_sec` 行齐全；backstop 对刚 publish 的 run 报 0 stale（若窗口内无新 cycle，则以 2.4 集成用例 + 手动触发 backstop 的日志替代，并如实记录口径）。

## 3. 交付（orchestrator）

- [x] 3.1 PR（含偏离记录节、证据包、中文工作总结）→ cross-review → merge gate。

## Evidence Floor（对应 issue #1120 验收标准）

| Issue AC | 证据 | 任务 |
|---|---|---|
| fallback EXPLAIN 两 hypertable chunk exclusion/index scan 且结果一致 | node-27 EXPLAIN + parity receipt + 真实绑定路径集成用例 | 2.4 / 2.5 |
| 完整 tick 后 backstop 对刚 publish run 报 0 stale | node-27 tick 日志或集成用例+手动 backstop（口径如实记录） | 2.4 / 2.6 |
| cron 日志区分三阶段耗时 | node-27 tick 日志 | 2.6 |
| `tests/test_display_coverage_refresh.py` 及 forecast_store 相关测试 + ruff 通过 | 本地测试输出 | 2.1 / 2.2 |
