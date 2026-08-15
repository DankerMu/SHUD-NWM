# Tasks: INVOCATION_ARGV 死岛删除（issue #1240）

## 1. 实现

- [x] 1.1 删除 `scripts/node27_timeseries_compression_live_evidence.py:377-445`
      三符号（`INVOCATION_ARGV` 整 Mapping、`_TIMEOUT_PREFIX` 含 frozen-wall
      注释 :404-409、`_invocation_execution_identity` **整函数**——其返回 dict
      闭合在 :445，:443 只是中间行；随手规整 :448 前空行）；实现前 grep 确认
      #1351 的 `EXPECTED_TIMEOUT_SECONDS` 冻结钉面零触碰（design D3）
- [x] 1.2 `tests/...:247-264` `_invocation()` 精简：删 argv / launcher_argv /
      resolved_* / artifact_bindings / receipt_sha256 字段（design D2 最小形状；
      保留 exit_code / timeout_seconds——legacy 哨兵的变异叙事依赖既有键），
      **同时删除 `bindings` 形参与 5 处调用点实参（:792/:828/:841/:856/:866）**
- [x] 1.3 两处 receipt 变异测试的 `artifact_bindings.receipt_sha256` 重盖行
      （`:2796`、`:3282` 附近）随字段删除
- [x] 1.4 `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`
      保留、负向断言逐字不变；若其行内假字段名落入 AC-3 grep 清单则换等价假名

## 2. 验证（Evidence Floor）

- [x] 2.1 `grep -rn "INVOCATION_ARGV\|_invocation_execution_identity\|_TIMEOUT_PREFIX" --include="*.py" .`
      0 命中（输出记入 PR body）
- [x] 2.2 `grep -rnE "\b(launcher_argv|resolved_interpreter|resolved_wrapper|resolved_env_file|resolved_repo_path|resolved_script|artifact_bindings)\b" --include="*.py" .`
      0 命中（输出记入 PR body；**必须词边界形式**——裸子串会撞上无关活代码
      `tests/test_select_ci_tests.py` 的 `_resolved_script_modules`，见 design D3）
- [x] 2.3 `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py`
      全绿；相对 436 基线的计数变化在 PR body 逐条说明
- [x] 2.4 `git diff` 自证：`_validate_exact_command_argv`/`_concrete_argv` 零触碰；
      schema `database_audit_proof` 两处 `{"const": false}` 零触碰
- [x] 2.5 `uv run ruff check .` 通过
- [x] 2.6 `openspec validate cleanup-invocation-contract-dead-island --strict --no-interactive` 通过

## 3. 交付记录

- [x] 3.1 PR body 记录方案裁决（A）与 B 的三条否决理由（issue AC-1；引 design D1）
- [x] 3.2 node-27 零实机变更声明（无 bundle 契约变化）
