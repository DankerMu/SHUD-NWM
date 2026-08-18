# Proposal: warm-start-env-toggle-trivalue (#1196)

## Why

Issue #1196（PR #1194 round-5 发现，pre-existing 自 821af66c）：
`services/orchestrator/scheduler_generation_gate.py:101-128`
`forecast_warm_start_env_enabled` 把 `OrchestratorConfig.from_env()` 的**任何**
异常折叠为 `False` 且零日志；决策消费点 `scheduler_core.py:751-754` 的
terminal-skip 短路在 `not env_enabled and already_complete` 时成立。

**真实可达面（round-0 修正后的口径）**：坏无关 env（如
`FORECAST_HORIZON_HOURS=abc`，chain_config.py:150 抛 ValueError）下——

- **db-free 主路径并不静默**：短路返回 None 后紧邻的
  `successor_state_for_candidate`（scheduler_core.py:780-782 无保护
  `from_env()`）随即抛出，pass 响亮失败。但失败点与根因**不可归因**：崩在
  下游任意一个 from_env 消费者，日志里没有任何线索指向「哪个 env、什么解析
  错误」，且崩溃前该候选已被静默按「toggle=off」短路——语义上把「检查无法
  完成」折叠成了「检查结果为否」，违反 `first-cycle-package-ic-consumption`
  确立的 UNREADABLE→fail-closed 分类不变量。
- **backfill 面真正静默**：`scheduler_backfill_predecessor.py:476-486` 把
  strict 路径异常吞成 `predecessor_gate_failed`，坏 env 在该面完全不可见。
- 直调 seam（测试/未来消费者）同样拿到假 `False`。

## What Changes

三值化 + **「不可读 → 响亮失败」为期望终态**（与其余 6 处 `from_env()` 调用
点的传播语义一致；不发明「strict 未知」平行模式——strict 路径自身在
scheduler_generation_gate.py:430/:679 经 `_db_free_strict_warm_start_required_for`
（scheduler_core.py:668-678）再读 env，必然抛出同一异常，本单不掩盖它）：

- `forecast_warm_start_env_enabled` 返回 `bool | None`：`from_env()` 成功 →
  `bool(require_forecast_warm_start)`（unset→默认 False=显式关闭，
  chain_config.py:154 + `_env_flag`:168-171 已核）；异常 → **先**记 WARNING
  （token `SCHEDULER_WARM_START_ENV_UNREADABLE`，含 `repr(exc)`——根因可从
  日志直接追出）再返回 None。模块新增
  `LOGGER = logging.getLogger(__name__)`（gate 模块现无 logger；风格照
  scheduler_no_progress.py:40）。每实例至多警一次（实例级 guard 属性；生产
  部署为 oneshot 每 pass 新进程，等价每 pass 一次；`run_continuous` 诊断
  路径一辈子一次为已声明取舍）。docstring 按三值语义与「响亮失败」终态改写。
- 调用点 `scheduler_core.py:751-754`：短路条件改
  `forecast_warm_start_env_enabled(self) is False and
  candidate_pipeline_already_complete(...)`；`None` 不再进入短路——随后
  strict 路径在下一个 env 读取点抛出，**失败点带着已落的 WARNING**。
  wrapper `:731-732` 返回类型同步（全仓无其他调用者，仅测试注释提及）。
- **backfill 面行为变更（有意，fail-closed 方向）**：
  `scheduler_backfill_predecessor.py:477` 经 `_strict_warm_start_for_candidate`
  间接受影响——坏 env 从「折叠 False→gate=None→predecessor 放行」变为
  「strict 抛出→吞成 `predecessor_gate_failed` 跳过」，且 WARNING 已归因。
  显式回归测试钉住新行为。

## Non-Goals

- `OrchestratorConfig.from_env()` 解析语义与 `_env_flag` 白名单不动。
- **不**为「不可读」构造能产出证据的降级路径（strict 路径的 :430/:679 再读
  env 必抛；掩盖它需要「strict 未知」平行模式，超出本单且违背与其余调用点
  一致的传播原则）。坏 env 下的正确终态是：不静默短路 + WARNING 归因 +
  响亮失败；修 env 后证据自然回来。
- `candidate_pipeline_already_complete`（fail-closed，:120-123 区）不动。
- 下游各 `from_env()` 消费者的异常传播行为不动。
- §8.6/§8.7 债（#1152/#1157）与 #1164 主线。

## Risk triage

- Fixture level: compact（单函数三值化 + 单调用点分流 + backfill 回归锁）。
- Repair intensity: low。
- Risk packs: state-semantics selected（三态×already_complete 真值表：仅
  (False, True) 短路；None 的传播终点=strict 路径抛出，backfill 面吞点行为
  变更显式钉住）；test-evidence selected（红证判据必须**正向区分**「短路」
  与「strict 已进入」——strict 自身也可返回 None（gate:447），不得用「evidence
  非 None」做判据；用 strict_warm_start_evidence 调用 spy + 异常形状 +
  WARNING token 三件断言）；其余 not selected。

## Must preserve

- env 显式 `false`（或 unset）+ journal 已完成 → terminal-skip 短路照旧
  （D8.9 compat；unset 用例须 `monkeypatch.delenv(...)` 防环境污染）。
- env 显式 `true` → strict 路径行为逐字不变；env 可读时零新日志。
- `candidate_pipeline_already_complete` 与 `strict_warm_start_evidence` 本体
  零改动。
- 其余 6 处 `from_env()` 调用点（scheduler_core.py:426/668/677/782、
  chain_forecast_orchestrator_cycle.py:75、chain_analysis_orchestrator.py:60）
  异常传播不动。
- WARNING 每实例至多一次。

## Seams under test

- monkeypatch 进程 env（`FORECAST_HORIZON_HOURS=abc` + `_set_db_free_scheduler_env`
  先例）驱动真实 `from_env()` 失败；journal 已完成 pipeline 构造（db-free
  file journal——非 db_free 时 scheduler_core.py:742 提前 return，短路不参与，
  tests/test_production_scheduler.py:21263 的非 db_free 用例**不可**作模板；
  fail-closed 探针先例 tests/test_scheduler_generation.py:2731）；
  strict_warm_start_evidence 调用 spy；caplog（logger =
  `services.orchestrator.scheduler_generation_gate`）。

## Evidence mapping

- 验收 1（三态 API + 折叠消失）→ tasks 2.1。
- 验收 2（短路仅显式 false 可达；None 不短路、strict 被进入且失败响亮
  归因）→ tasks 2.2 红证。
- 验收 3（WARNING 根因可追 + 一次性）→ tasks 2.3。
- 验收 4（backfill 面 fail-closed 化钉住）→ tasks 2.5。
- 验收 5（显式 false/unset/true 回归锁）→ tasks 2.4。
- Verification：`uv run pytest -q tests/test_scheduler_generation.py
  tests/test_production_scheduler.py` + ruff + openspec validate（本地）；
  merge 后 node-27 receipt（定向选择器；全量口径按 #1513 环境类已知例外）。
