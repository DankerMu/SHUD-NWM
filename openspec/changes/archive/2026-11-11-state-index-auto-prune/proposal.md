# Proposal: node-22 state-index prune-retention 自动 enforce（#3121）

Issue type: feature
Fixture level: expanded
Upstream suggested level: absent（expanded：无人值守地删 index 条目；在生产上停/启调度器 timer；新增 systemd unit；与 cycle-once wrapper、copyback、provider-refresh 并发）
Blast radius:
- timer 停了没恢复 → 调度永久停摆，追赶冻结；
- 窗口过短 → warm-start 找不到 state（只从 index 删条目，state 对象不删）；
- 与 writer 并发 → index 丢更新。enforce 在锁内会重新规划，但 runbook 8.9 要求冻结 copyback/provider-refresh；
- 剪到一半（exit 3）→ 两条 lane 不一致；
- 反复失败 → 每天都停一次调度。
Selected risk packs: Concurrency / shared state / ordering；Error handling / rollback / partial outputs；Config / project setup；Public API / CLI / script entry（新 unit 与 entrypoint）；Documentation / migration notes；Resource limits（等待上限、timeout）；Legacy compatibility（watch 与人工流程不变）；File IO / path safety（receipt 根）；Schema / Release（新 receipt schema）
Evidence floor: `uv run pytest -q tests/test_node22_state_index_auto_prune.py tests/test_node22_entrypoint_invariant.py tests/test_node22_unit_tracking.py tests/test_node22_unit_failure_alert.py tests/test_node22_entrypoint_invariant_python_scan.py tests/test_select_ci_tests.py tests/test_node22_state_index_capacity_watch.py` + selector 选集 + `uv run ruff check .`；node-22 手动触发一次的 receipt（timer 恢复为 active）

## Why

state index 从不自动裁剪。按 #2653 Stage 2 的决定，超过 0.70 时由 watch 报警、人工 enforce。2026-11-11 实测：追赶期间（lag 294 → 窗口 29 天）剪完只降到 0.697（实际 enforce 结果是 0.6967；issue 写的 0.693 是更早一次 dry-run 的数字），约每 1–2 天就要剪一次；追赶结束后约 10 天一次。人工节奏过于频繁。owner 2026-11-11 决定推翻 Stage 2，改为自动（D1–D4 全按推荐，见 issue）。

## What Changes

- 新模块 `scripts/node22_state_index_auto_prune.py`，以及新的 user unit `nhms-scheduler-state-index-prune.service` / `.timer`（每日 06:30 UTC，`Persistent=true`）。流程：
  1. dry-run。没有 lane 达到阈值 → 写 `skip` receipt，exit 0。
  2. 检查 cycle-once wrapper：在跑 → 写 receipt，exit 1，不碰 timer。
  3. 停调度器 timer 和 provider-refresh timer（只停原本 active 的），等两个 service 都离开运行态，等待有上限。
  4. 再查一次 wrapper 和两个 service 的状态，跑最终 dry-run 闸门。
  5. enforce。
  6. 不论结果如何，恢复本次停掉的 timer。
- 窗口用 #3118 的 `minimum_state_index_retention_days`，阈值用 `STATE_INDEX_CAPACITY_WARNING_THRESHOLD`，不新增配置。
- receipt 与 watch 同风格：owner-private 目录，`latest.json` 加时间戳文件，保留 30 份。
- runbook 8.12 改写：Stage 2 决策改为自动，人工流程保留作兜底；`stuck-detection.md` 的 `timer_stopped` 处置里加上 auto-prune 持有 timer 的情况。

## 对 issue 的澄清

issue 写的闸门是"两个 lane 都 `removed_count > 0`"。两条 lane 不是镜像：exit 3 之后 reference lane 可能已经剪完，也可能只有一条 lane 有可剪条目。所以闸门细化为：**每个超过阈值的 lane 都必须 `removed_count > 0`**，没超阈值的 lane 允许为 0。

这个闸门在冻结之前，先拿初次 dry-run 评估一遍；不满足就不碰 timer、直接非 0 退出。这样避免"每天冻结一次调度器、然后 gate_failed"。exit 3 之后由人工兜底。

## Non-goals

- 不改 repair 的 enforce 语义（锁内重新规划、锁序、archive、receipt）和 `_state_index_retention_window`。
- 不改 watch（继续保持只读、只报警）。lag 解析直接 import watch 的 `_retention_window`，不改 watch 文件。
- 不改调度器的锁语义，也不给 cycle-once wrapper 加互斥（用前后两次 pgrep 检查加上 enforce 的锁内重新规划来兜底，见 design.md）。
- 不自动重试；不修 #3123 的注入字段问题。
