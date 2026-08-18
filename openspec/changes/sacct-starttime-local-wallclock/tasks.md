## 0. Probe（只读核实，报告不修）

- [x] 0.1 reconcile 兄弟副本（`services/orchestrator/reconcile.py:341-350`/`:382-392`
      `_pages`）：核实 tz-aware UTC 页边界经裸 strftime 交 sacct 的同模式是否成立、
      UTC+8 下窗口前移的推论是否可由字符串渲染证实；只报告（含建议路由：并入 #1116
      或另立单），落 PR body 偏离记录 + #1282 comment；不改任何 reconcile 代码

## 1. Implementation

- [x] 1.1 `real_backend.py:466`：`.astimezone()` 插入 strftime 前（一行）；`:467`
      拼接与显式 start_time 分支零改动

## 2. Tests

- [x] 2.1 TZ-pinned 三档强断言（`tests/test_real_slurm_gateway.py`）：**复用
      `_pinned_local_timezone`（`:3381-3394`）** + skipif tzset 惯例；monkeypatch
      `_now()` 固定 UTC 时刻，断言 `--starttime=` 完整**字面**串（期望值写死：
      Asia/Shanghai=+8h 墙钟；America/New_York 用 `_now()=2026-07-12T04:00:00Z` →
      `--starttime=2026-07-11T00:00:00`；UTC=与修复前逐字同）；禁止 zoneinfo/timedelta
      现算期望值
- [x] 2.2 显式 `start_time` 传入零变化锁（不被二次换算）
- [x] 2.3 既有 startswith 弱断言用例不动、全绿

## 3. Verification

- [x] 3.1 红证：负 offset 档在改动前红（断言 `--starttime` 串差 offset 小时），
      UTC 档改动前后均绿（等价锁）
- [x] 3.2 uv run pytest -q tests/test_real_slurm_gateway.py
- [x] 3.3 uv run ruff check .（本地 untracked 投影 E501 既知例外照会话惯例记录）
- [x] 3.4 openspec validate sacct-starttime-local-wallclock --strict --no-interactive
- [ ] 3.5 merge 后 node-27（UTC+8 宿主，正好是判别档）oracle receipt：3.2 套件 +
      TZ 三档选择器，记入 #1282
