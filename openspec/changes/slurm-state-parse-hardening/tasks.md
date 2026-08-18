## 1. Implementation（#1509）
- [ ] 1.1 SLURM_STATE_MAP 增 REVOKED/SPECIAL_EXIT → FAILED + 正交注释
- [ ] 1.2 cohort 投影方向测试 ×2（形如 tests/test_gateway_reconcile.py:1159 BOOT_FAIL 用例）：
      raw_state=REVOKED/SPECIAL_EXIT 的 array task → array_task_outcome=="failed"、
      accounting_complete=True、action=="terminal"
- [ ] 1.3 meta 断言：TERMINAL_SLURM_STATES ⊆ SLURM_STATE_MAP 键集合

## 2. Implementation（#1510）
- [ ] 2.1 _normalize_slurm_state 守空：not parts → "UNKNOWN"
- [ ] 2.2 slurm_validation.py:1480 同形副本对齐（守空）
- [ ] 2.3 映射网格（tests/test_real_slurm_gateway.py:1049）补 "" 与 "   " 两格
- [ ] 2.4 三条解析腿空 State 用例：_parse_sacct_status / _parse_sacct_list / array member 聚合
      → 不抛 IndexError，收敛为 UNKNOWN 记录
- [ ] 2.5 map_slurm_error_code("") 返回兜底码不抛异常（用例钉住）

## 3. Verification
- [ ] 3.1 红证：1.2 / 2.3 / 2.4 的新用例在改动前必须红（IndexError 或 task_accounting_incomplete），
      逐条记录命令与输出摘要
- [ ] 3.2 uv run pytest -q tests/test_gateway_reconcile.py tests/test_real_slurm_gateway.py
- [ ] 3.3 uv run pytest -q tests/test_production_closure_slurm_validation.py（若存在该文件，先 ls 确认实名）
- [ ] 3.4 uv run ruff check .
- [ ] 3.5 openspec validate slurm-state-parse-hardening --strict --no-interactive
