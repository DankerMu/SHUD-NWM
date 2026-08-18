# Proposal: sacct-starttime-local-wallclock (#1282)

## Why

Issue #1282（#1117 fixture review 旁支 N1 路由，#1117 的镜像缺陷）：
`services/slurm_gateway/real_backend.py:466`（issue 时 `:442`）`list_jobs` 的默认
`--starttime` 用 tz-aware UTC `self._now() - 24h` 直接 `strftime` 丢 offset，裸串交
sacct 按**宿主本地时区**解释——窗口整体平移宿主 offset：UTC+8（node-22）意外放宽
8h（错得刚好安全）；UTC 以西静默收窄（最近完成作业掉出 `GET /jobs` 与 queue-depth，
零报错）；正确性依赖「部署机在 UTC 以东或恰为 UTC」这一未声明前提。两个消费者
（`routes.py` `GET /jobs`、`apps/api/routes/pipeline.py` queue-depth 兜底）均不传
`start_time`，100% 走该默认分支。#1117 修了 parse 侧（读本地当 UTC），本单修写侧
（写 UTC 当本地）。

## What Changes

- `real_backend.py:466` 采纳 issue 推荐修法：strftime 前显式 `.astimezone()` 转宿主
  本地时区——`(self._now() - timedelta(hours=DEFAULT_LIST_LOOKBACK_HOURS)).astimezone().strftime(...)`。
  一行改动，保留 `_now()` 注入缝，与 sacct 解释口径对齐。
- TZ-pinned 回归测试：**复用 #1117 已落地的 `_pinned_local_timezone(tz_name)`
  contextmanager**（`tests/test_real_slurm_gateway.py:3381-3394`，自带 TZ 设置/tzset/
  finally 恢复），并沿用同文件 `@pytest.mark.skipif(not hasattr(time, "tzset"), ...)`
  惯例；monkeypatch `_now()` 固定 UTC 时刻，在 `Asia/Shanghai`（UTC+8）/
  `America/New_York`（固定 `_now()=2026-07-12T04:00:00Z` → 期望
  `--starttime=2026-07-11T00:00:00`，EDT，now 与 now-24h 同侧、双双避开 DST 过渡）/
  `UTC` 三档断言 `--starttime=` **完整字面串**（期望值写死字面量，禁止用
  zoneinfo/timedelta 现算——否则测试镜像实现，红证失去判别力）。既有 startswith
  弱断言用例不动。
- 显式 `start_time` 传入路径零变化（不二次换算）。

## Non-Goals

- #1117 parse 侧（已修/属该单）；`--endtime` 缺省语义；offset 切片行为。
- **reconcile 兄弟副本**（`services/orchestrator/reconcile.py:382-383`，`_pages` 同模式
  但 start/end 同向平移、窗口整体前移——在 UTC+8 上意味着最近 8h 完成的作业不在扫描窗，
  对 absence-proof 可能是更强失真）：本单只做**只读核实并回报**，不修——它与 #1116
  （absence-proof 不健全，本批在队）同面，路由裁决在核实结果出来后由 orchestrator 定
  （并入 #1116 处理或另立单）；核实报告落 PR body 偏离记录 + #1282 comment，不许悬空。
- 调用方显式时间参数的语义（保持"调用方自负"）。

## Risk triage

- Fixture level: compact（一行修 + 测试；修法 issue 已裁定）。
- Repair intensity: low（单站点，注入缝保留）。
- Risk packs: test-evidence selected（TZ-pinned 强断言 + 负 offset 修复前红）；
  version/env-divergence selected（TZ 是环境轴：tzset 进程级污染、DST、CI TZ 缺省）；
  其余 not selected。

## Must preserve

- `_now()` 注入缝与 `list_jobs` 其余参数渲染逐字不变。
- 显式 `start_time`/`end_time` 传入时的行为逐字不变。
- 宿主 TZ=UTC 时输出与修复前逐字相同（恰好正确的原点不动）。
- 现有 `tests/test_real_slurm_gateway.py` 全绿（含既有 startswith 弱断言用例）。

## Evidence mapping

- 验收 1 → 1.1 + 2.1；验收 2 → 2.1（三档 TZ 完整串断言）；验收 3 → 3.1 红证
  （负 offset 档修复前红）；验收 4 → 2.2（显式 start_time 零变化锁）；验收 5 → 3.2/3.3。
- reconcile 副本核实 → tasks 0.1（只读，报告不修）。
- Verification：`uv run pytest -q tests/test_real_slurm_gateway.py` + ruff（本地）；
  merge 后 node-27 receipt。
