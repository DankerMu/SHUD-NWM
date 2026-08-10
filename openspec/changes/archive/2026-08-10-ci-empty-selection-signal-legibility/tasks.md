# Tasks: ci-empty-selection-signal-legibility

## 1. 实现

- [x] 1.1 `.github/workflows/ci.yml` else 分支：`::warning` 注解 + `$GITHUB_STEP_SUMMARY` 块（"0 assertions executed"）；collect 输出重定向文件、成功打 tail 统计、失败打全文并 `exit 1`（禁 `| tail` 管道，无 pipefail）；正常分支与 check 名零改动（D1）
- [x] 1.2 `scripts/select_ci_tests.py`：(a) `_test_target_exists` 最终过滤丢弃目标时 stderr 恒告警 + `GITHUB_ACTIONS=true` 时 stdout `::warning` 注解（补 `import os`）；`select_tests()` 签名/返回值语义不变；(b) 删除 `:216` 死目标 `tests/test_worker_chain_smoke.py`（全规则集唯一，零行为变更）（D2）
- [x] 1.3 `instructions/agents/shared.md` CI 一节句尾一句话增量（响亮化事实）；对 `CLAUDE.md`/`AGENTS.md` 应用逐字相同增量并核验三文件该句一致（D4；AC-6 条件款 N/A 已判定）

## 2. 测试

- [x] 2.1 B1-B6：6 类空选择输入 pin `select_tests == []`（注释点明路线 C 契约；`.sh` 类翻转主是 #1138，其余归 A/B 决策）
- [x] 2.2 B7：stale-target 丢弃 → 返回值过滤 + stderr WARNING + `GITHUB_ACTIONS=true` 时 stdout `::warning`（monkeypatch env + tmp repo_root）
- [x] 2.3 B8：全目标实存输入（如 `db/README.md`）零告警（噪音回归防护；不得用 forcing_producer 类输入）
- [x] 2.4 B9：meta-guard `test_every_pinned_node_id_resolves_to_an_existing_test_function` 扩展覆盖文件级规则目标
- [x] 2.5 现有 32 用例除 B9 扩展外零改动全绿；ci.yml YAML 结构核查（else 分支含 warning/step-summary/失败显式 exit 1、不含 `>/dev/null` 与 `| tail`）

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py` 全绿
- [x] 3.2 `uv run ruff check .` 通过
- [x] 3.3 `openspec validate ci-empty-selection-signal-legibility --strict --no-interactive` 通过
- [x] 3.4 PR body 声明路线 C 与理由（AC-1）、AC-3 N/A、AC-6 条件款 N/A 记录
- [x] 3.5 **merge 前实机证据（AC-5）**：从 feature 分支切 probe 分支加一行 `schemas/**` 改动开试探 PR（跑新 workflow 定义），采 Actions run 链接显示 warning 注解 + step summary + tail 统计，关闭不合并；证据入 PR body 并贴 #1182
