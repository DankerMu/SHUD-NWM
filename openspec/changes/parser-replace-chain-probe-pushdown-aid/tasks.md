# Tasks（#1681）

## 1. 实现

- [x] 1.1 清零 oracle 拆分断言（design D3）并先跑红（当前代码无 `run_id` 辅助）。
- [x] 1.2 `workers/output_parser/parser.py`：探针与取窗按 design D1 顺序加 marker + `AND run_id = %s`
  （紧跟 `run_key`），新增 `_replacement_read_bindings` 四元 helper 供两处调用；DELETE/INSERT 与 `_replacement_key_bindings` 逐字不动；
  同步更新两条语句上方的 #1442 注释（"Which index" 段：压缩腿走 segmentby `run_id` 索引，
  未压缩腿走 000051 键索引；`run_id` 辅助随 #1342 删）。
- [x] 1.3 真库测试（design D4 1-4，落在 `tests/test_river_ts_dual_write_integration.py`），
  EXPLAIN 阴性对照内置。

## 2. 验证

- [x] 2.1 `uv run ruff check .`
- [x] 2.2 `uv run pytest -q tests/test_river_ts_text_identity_cleanup.py tests/test_timescale_write_guard_wired.py tests/test_output_parser_dual_write.py tests/test_output_parser.py tests/test_output_parser_cli.py tests/test_analysis_pipeline.py tests/test_select_ci_tests.py` 全绿。
- [x] 2.3 `openspec validate parser-replace-chain-probe-pushdown-aid --strict --no-interactive`（issue AC4）。

## Evidence Floor

- [x] E1 本地：2.1/2.2/2.3 输出 + oracle 红→绿。
- [ ] E2 node-27 throwaway DB：`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=<superuser throwaway DSN> uv run pytest -q <integration file> -k <new tests>`：EXPLAIN 正向（压缩侧 `run_id` 索引）+ 内置阴性对照（去辅助 → `Seq Scan on compress_hyper`）；端到端写入 + 重放幂等 + 守卫闭合三项绿；非零 passed。
- [ ] E3 node-27 live（design D5）：34 个 `2026082012` run parse 成功并 publish；连续 ≥2 tick `rc=0`、分钟级；`hydro_run` 中 `failed` 的 2026082012 计数归零。
- [ ] E4 CI：PR Unit Tests 绿（含 SQL Migration Dry Run 若 `database` filter 点亮）。
- [ ] E5 PR body 偏离记录：#1442 D1 组 F 裁定被本单修订的范围（仅探针/取窗），#1342 删列清单需纳入该辅助；要求中保留的"node-27 键收敛 preflight receipt"为 #1442 已满足的继承条款（PR #1655 E4 receipt），本单不重取。
