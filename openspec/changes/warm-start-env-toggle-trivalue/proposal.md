# Proposal: warm-start-env-toggle-trivalue (#1196)

## Why

Issue #1196（PR #1194 round-5 发现，pre-existing 自 821af66c）：
`services/orchestrator/scheduler_generation_gate.py:101-128`
`forecast_warm_start_env_enabled` 把 `OrchestratorConfig.from_env()` 的**任何**
异常折叠为 `False` 且零日志；唯一决策消费点 `scheduler_core.py:751-754` 的
terminal-skip 短路在 `not env_enabled and already_complete` 时成立——env 实为
`true` 但任一**无关** env 打错字（如 `FORECAST_HORIZON_HOURS=abc`）即静默走
短路：§8 gating 与 #1164 packaged-IC 决策整趟跳过、`strict_warm_start`
evidence 缺席、env 配置错误在 receipt 与日志中完全不可见。非 admission 缺口
（第二合取项 `candidate_pipeline_already_complete` fail-closed 兜住语义），
但违反 `first-cycle-package-ic-consumption` 确立的错误分类不变量：**不可完成
的检查不得折叠为否定结果**（UNREADABLE→fail-closed）。

## What Changes

采 issue 推荐修法（三值化，保 D8.9 compat）：

- `forecast_warm_start_env_enabled` 返回 `bool | None`：`from_env()` 成功 →
  `bool(require_forecast_warm_start)`（unset→默认 False = 显式关闭，现状）；
  异常 → 返回 `None`（不可读）并记 WARNING（token
  `SCHEDULER_WARM_START_ENV_UNREADABLE`，含 `repr(exc)` 以便从日志追出坏
  env）。**每 scheduler 实例至多警一次**（实例级 guard 属性；该函数逐候选
  调用，无 guard 会逐候选刷屏）——现有 `del scheduler` 改为用作 guard 载体。
  docstring 同步三值语义（现注释的两值辩护段改写）。
- 调用点 `scheduler_core.py:751-754`：短路条件改为「env **显式** False」——
  `_generation_gate.forecast_warm_start_env_enabled(self) is False and
  candidate_pipeline_already_complete(...)`；`True` 与 `None` 都走
  `strict_warm_start_evidence`（不可读 → 宁多跑一次 §8 检查，证据不丢）。
- wrapper `scheduler_core.py:731-732` `_forecast_warm_start_env_enabled` 返回
  类型同步 `bool | None`；implementer grep 其余消费者（tests 内若有直接断言
  bool 的用例同步三值口径）。

## Non-Goals

- `OrchestratorConfig.from_env()` 解析语义与 `_env_flag` 白名单不动（issue
  明确 out of scope）。
- `candidate_pipeline_already_complete` 的窄异常收敛（:120-123 区）已
  fail-closed，不动。
- 备选方案「删 try/except 让异常传播」不采：无关 env 打错字会使整个 pass
  停摆（可用性回退），issue 已列 tradeoff。
- evidence 字段级 typed reason（威胁面仅 observability，WARNING 已满足 issue
  的「至少一条」要求；字段化留待有真实消费端时另议）。
- §8.6/§8.7 债（#1152/#1157）与 #1164 主线。

## Risk triage

- Fixture level: compact（单函数三值化 + 单调用点分流 + 测试；issue 已给全案）。
- Repair intensity: low。
- Risk packs: state-semantics selected（三态分流真值表：True/False/None ×
  already_complete 两值——六格中仅 (False, True) 允许短路；unset 默认=显式
  False 的现状保持）；test-evidence selected（红证必须用**无关** env 打错字
  构造——用 flag 自身打错字构造会弱化「不相关解析错误也触发」这一核心断言）；
  其余 not selected。

## Must preserve

- env 显式 `false`（或 unset，默认 False）+ journal 已完成 → terminal-skip
  短路照旧返回 None（D8.9 compat，不破坏 userspace）。
- env 显式 `true` → strict 路径行为逐字不变。
- `candidate_pipeline_already_complete` 与 `strict_warm_start_evidence` 本体
  零改动。
- 其余 6 处 `OrchestratorConfig.from_env()` 调用点（scheduler_core.py:419/
  661/670/775、chain_forecast_orchestrator_cycle.py:65、
  chain_analysis_orchestrator.py:60）异常传播行为不动（本处是全仓唯一宽折叠，
  issue 已核）。
- WARNING 每实例至多一次；env 可读时零新日志。

## Seams under test

- monkeypatch 进程 env（坏无关 env `FORECAST_HORIZON_HOURS=abc`）驱动真实
  `from_env()` 失败；journal 已完成 pipeline 的既有测试构造
  （tests/test_scheduler_generation.py 现有 terminal-skip 用例为模板，
  :1118 邻域）；caplog 断言 WARNING token 与一次性。

## Evidence mapping

- 验收 1（三态 API + 折叠消失）→ tasks 2.1。
- 验收 2（短路仅显式 false 可达；None 必走 strict）→ tasks 2.2 红证。
- 验收 3（可追根因 WARNING）→ tasks 2.3。
- 验收 4（红→绿：坏无关 env + 已完成 pipeline → evidence 非 None）→ tasks
  2.2 + 3.1。
- 验收 5（显式 false/true 回归锁）→ tasks 2.4。
- Verification：`uv run pytest -q tests/test_scheduler_generation.py
  tests/test_production_scheduler.py` + ruff + openspec validate（本地）；
  merge 后 node-27 receipt（定向选择器；全量口径按 #1513 环境类已知例外）。
