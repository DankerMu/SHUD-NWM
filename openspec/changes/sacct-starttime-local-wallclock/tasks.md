## 0. Probe（只读核实，报告不修）

- [ ] 0.1 reconcile 兄弟副本（`services/orchestrator/reconcile.py:341-350`/`:382-392`
      `_pages`）：核实 tz-aware UTC 页边界经裸 strftime 交 sacct 的同模式是否成立、
      UTC+8 下窗口前移的推论是否可由字符串渲染证实；只报告（含建议路由：并入 #1116
      或另立单），不改任何 reconcile 代码

## 1. Implementation

- [ ] 1.1 `real_backend.py:466`：`.astimezone()` 插入 strftime 前（一行）；`:467`
      拼接与显式 start_time 分支零改动

## 2. Tests

- [ ] 2.1 TZ-pinned 三档强断言（`tests/test_real_slurm_gateway.py`）：monkeypatch
      `_now()` 固定 UTC 时刻 + `TZ` env + `time.tzset()`，断言 `--starttime=` 完整
      字符串（Asia/Shanghai=+8h 墙钟 / America/New_York=固定时刻避开 DST 歧义并注明 /
      UTC=与修复前逐字同）；finalizer 恢复原 TZ + tzset
- [ ] 2.2 显式 `start_time` 传入零变化锁（不被二次换算）
- [ ] 2.3 既有 startswith 弱断言用例不动、全绿

## 3. Verification

- [ ] 3.1 红证：负 offset 档在改动前红（断言 `--starttime` 串差 offset 小时），
      UTC 档改动前后均绿（等价锁）
- [ ] 3.2 uv run pytest -q tests/test_real_slurm_gateway.py
- [ ] 3.3 uv run ruff check services tests
- [ ] 3.4 openspec validate sacct-starttime-local-wallclock --strict --no-interactive
- [ ] 3.5 merge 后 node-27（UTC+8 宿主，正好是判别档）oracle receipt：3.2 套件 +
      TZ 三档选择器，记入 #1282
