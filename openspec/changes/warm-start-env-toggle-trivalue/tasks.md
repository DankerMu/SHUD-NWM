## 1. Implementation

- [ ] 1.1 `services/orchestrator/scheduler_generation_gate.py:101-128`：
      `forecast_warm_start_env_enabled -> bool | None`；成功 →
      `bool(require_forecast_warm_start)`；异常 → WARNING（token
      `SCHEDULER_WARM_START_ENV_UNREADABLE`，含 `repr(exc)`）+ 返回 None；
      每 scheduler 实例至多警一次（实例级 guard 属性，替代 `del scheduler`）；
      docstring 三值语义改写（现两值辩护段删除）
- [ ] 1.2 `services/orchestrator/scheduler_core.py:751-754`：短路条件改
      `... is False and candidate_pipeline_already_complete(...)`；:731-732
      wrapper 返回类型同步；grep 其余消费者同步三值口径

## 2. Tests（tests/test_scheduler_generation.py）

- [ ] 2.1 三态 API 直测：env 显式 true → True；显式 false → False；unset →
      False（默认=显式关闭，现状锁）；坏**无关** env
      （`FORECAST_HORIZON_HOURS=abc`）→ None
- [ ] 2.2 红证（e2e 分流）：坏无关 env + journal 已完成 pipeline →
      `strict_warm_start_evidence` 被执行、`_strict_warm_start_for_candidate`
      返回非 None evidence；**改动前该测试红**（折叠为 False → 短路 → None）
- [ ] 2.3 WARNING 可观测性：caplog 断言 token 出现、消息含坏 env 可追线索
      （`repr(exc)`）；同一 scheduler 实例第二次调用不再重复；env 可读时零
      该 token
- [ ] 2.4 回归锁：env 显式 false + 已完成 → 仍 terminal-skip（返回 None，
      D8.9 compat）；env 显式 true + 已完成 → strict 路径行为逐字不变

## 3. Verification

- [ ] 3.1 红证记录：2.2 在改动前红（形状：evidence 为 None / strict 未执行）
- [ ] 3.2 uv run pytest -q tests/test_scheduler_generation.py
      tests/test_production_scheduler.py
- [ ] 3.3 uv run ruff check services tests
- [ ] 3.4 openspec validate warm-start-env-toggle-trivalue --strict --no-interactive
- [ ] 3.5 merge 后 node-27 receipt（定向选择器 + 3.2 两套件；全量红按 #1513
      环境类已知例外口径核对）记入 #1196
