## 1. Implementation

- [ ] 1.1 `services/orchestrator/scheduler_generation_gate.py`：模块级
      `LOGGER = logging.getLogger(__name__)`（现无 logger，风格照
      scheduler_no_progress.py:40）；`forecast_warm_start_env_enabled ->
      bool | None`（:101-128）：成功 → `bool(require_forecast_warm_start)`；
      异常 → 先 WARNING（token `SCHEDULER_WARM_START_ENV_UNREADABLE`，含
      `repr(exc)`）再返回 None；实例级 guard 属性一次性（替代
      `del scheduler`）；docstring 按三值 + 响亮失败终态改写（删两值辩护段）
- [ ] 1.2 `services/orchestrator/scheduler_core.py:751-754`：短路条件改
      `... is False and candidate_pipeline_already_complete(...)`；:731-732
      wrapper 返回类型同步

## 2. Tests

- [ ] 2.1 三态 API 直测（tests/test_scheduler_generation.py）：显式 true →
      True；显式 false → False；unset（`monkeypatch.delenv` 防污染）→
      False；坏无关 env `FORECAST_HORIZON_HOURS=abc` → None
- [ ] 2.2 红证（e2e 分流，db-free 构造——非 db_free 在 scheduler_core.py:742
      提前 return 短路不参与；用 `_set_db_free_scheduler_env` + file journal
      已完成 pipeline）：坏无关 env 下断言三件——(a)
      `strict_warm_start_evidence` 被进入（spy；不得用 evidence 非 None 判据
      ——strict 自身可返回 None，gate:447）；(b) 失败响亮（strict 内
      :430/:679 再读 env 抛 ValueError，按调用层真实收口形状断言）；(c)
      WARNING 已先落。**改动前红**：无异常、静默短路返回 None、无 WARNING
- [ ] 2.3 WARNING 可观测性：caplog（logger
      `services.orchestrator.scheduler_generation_gate`）断言 token + `repr(exc)`
      可追根因；同实例第二次调用不重复；env 可读时零该 token
- [ ] 2.4 回归锁：显式 false + 已完成 → 仍 terminal-skip（None，D8.9）；
      显式 true → strict 行为逐字不变
- [ ] 2.5 backfill 面钉住（有意行为变更）：坏 env 下
      `scheduler_backfill_predecessor.py:476-486` 的吞点从「放行」变
      「`predecessor_gate_failed` 跳过」且 WARNING 已归因——显式回归测试

## 3. Verification

- [ ] 3.1 红证记录：2.2 在改动前红（静默短路形状，非异常）
- [ ] 3.2 uv run pytest -q tests/test_scheduler_generation.py
      tests/test_production_scheduler.py
- [ ] 3.3 uv run ruff check services tests
- [ ] 3.4 openspec validate warm-start-env-toggle-trivalue --strict --no-interactive
- [ ] 3.5 merge 后 node-27 receipt（定向选择器 + 3.2 两套件；全量红按 #1513
      环境类已知例外口径核对）记入 #1196
