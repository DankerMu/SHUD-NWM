# Proposal: CI 空选择 collect-only 分支信号可辨识化（路线 C + 选择器静默丢弃告警）

## Why

Issue #1182：PR 上唯一的单测 job `unit-test-targeted`（check 名 **"Unit Tests"**）在 `select_ci_tests.py` 选空时退化为 `pytest tests/ -q --collect-only >/dev/null`——执行 **0 条断言**、收集输出也被丢弃，且与"跑了 25 个 test 文件"的正常分支共用同一 check 名，PR checks UI 无法区分。空选择的产地是结构性的：`ci.yml` `backend` paths-filter 域（含 `**/*.py`、`schemas/**`、`infra/**`、`tests/**` 等）远大于选择器 `_is_backend_python_path` 的五前缀兜底域，6 类输入（issue 实测表）会让 backend 门开着而选择为空。另外 `_test_target_exists`（`scripts/select_ci_tests.py:434`）静默丢弃指向已删除/重命名文件的 test 目标，选择结果可能无告警地收缩到空。

历史扫描 0/55 merge 命中（潜在而非已发生的漏检），但缺口随时可被一次 test 文件重命名激活。

## 路线决策（issue 为 needs-triage 的 A/B/C 策略选择题）

**选 C（只改信号、不改行为）+ 必做项 AC-4**，理由：

- issue 自身将 C 标注为"无争议的即刻改进"：零 runner 成本、一行级改动、消除"绿得没有信息量"的欺骗性。
- A（选择器保证非空）与 B（空选择 exit 1）触及 **CI 成本纪律取向与门控强度**——issue 明示这是 maintainer 策略决策轴；C 不预设也不封死 A/B，两者保留为后续独立决策。
- AC-4（`_test_target_exists` 静默丢弃告警）是 issue 的无条件验收项，与路线选择正交，一并落地。

## What Changes

1. **`.github/workflows/ci.yml` else 分支可辨识化**（`:245-254`）：
   - 去掉 `>/dev/null`，collect 输出重定向到文件：成功打 tail 统计行进 job log，失败打全文并显式 `exit 1`（禁 `| tail` 管道，无 pipefail 会吞失败）；
   - 输出 `::warning` workflow 注解（title 点明 "Unit Tests executed 0 assertions"）；
   - 写 `$GITHUB_STEP_SUMMARY`：明示本次为 collect-only 冒烟、0 断言执行、原因（选择器未映射到任何 test 文件）与 issue 引用。
   - check 名、退出语义（collect 成功即绿）**不变**——门控强度是 A/B 的轴，本 change 不碰。
2. **`scripts/select_ci_tests.py` 静默丢弃改为告警**：最终过滤（`:434`）丢弃不存在的 test 目标时，向 stderr 输出人读告警，并在 GitHub Actions 环境（`GITHUB_ACTIONS=true`）向 stdout 输出 `::warning` 注解，点名被丢弃的目标路径；返回值语义不变（仍过滤）。顺带删除全规则集唯一的已实存死目标 `tests/test_worker_chain_smoke.py`（`:216`，零行为变更卫生）。
3. **`tests/test_select_ci_tests.py`**：pin 6 类空选择输入（`schemas/**`、非映射 `infra/**`、非五前缀 `.py`×2、后端目录下非 `.py`、`tests/` 下非 `.py`、`scripts/**/*.sh`）在路线 C 下选择结果为空（现状即契约，`.sh` 类翻转主是 #1138，其余留给 A/B）；新增 stale-target 丢弃告警用例；既有 meta-guard 扩展覆盖文件级规则目标（规则集内死目标即测试失败）。
4. **指令源与投影**：AC-6 条件款判 N/A（路线 C 下 `instructions/agents/shared.md` CI 一节现句仍字面为真、无冲突）；自愿做一句话增量并对 `CLAUDE.md`/`AGENTS.md` 投影产物应用逐字相同增量（手工程序 + 一致性核验，无生成脚本）。

## Non-Goals

- 方向 A（选择器域泛化 + `CORE_SMOKE_TESTS` 兜底）与方向 B（空选择 exit 1）：maintainer 策略轴，不在本 change 决断。
- 选择器映射准确性改进（`.sh` 类归 #1138）。
- 全量 `unit-test` 在 PR 上运行 / draft-ready 门控调整（#1129 备选方案轴）。

## Impact

- Affected specs: `ci-contract-baseline`（ADDED 一条 requirement）。
- Affected code: `.github/workflows/ci.yml`（else 分支）、`scripts/select_ci_tests.py`（告警）、`tests/test_select_ci_tests.py`、`instructions/agents/shared.md` + 投影产物。
- 运行时/DB/display 面零改动；无需 node-27 receipt。CI 行为面改动的实机证据在 **merge 前**以 schemas-only 试探 PR（自 feature 分支切出，跑新 workflow 定义）的 Actions run 采集，关闭不合并（AC-5）。
