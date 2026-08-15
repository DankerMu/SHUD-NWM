# Proposal: retention-pipeline-frontier-exemption (#1307)

## Why

object-store retention 的保留判据是纯墙钟年龄（`retention.py:323`
`cutoff = now - retention_days`，调度 pass 收尾无条件执行，
`scheduler_runtime.py:1387`），与流水线前沿完全脱钩。稳态下前沿≈墙钟、缺陷不可
见；replay 追赶期前沿落后墙钟（#1164 实测 16 天）时，当前正在推进的 cycle 本身
就老于 cutoff——同一趟 pass 前半段刚产出的 forcing 包与 run 工作区在收尾即被
删除，下一趟重建、再删：产出→删除自旋（node-22 三趟连续 pass live receipt：同
一批 `forcing/gfs/2026072300` key 相邻两趟各删一次，每轮白烧 1.2-2.4 GB），失败
现场（SHUD stdout/stderr、checkpoint）随 run 目录蒸发导致
`STATE_CHECKPOINTS_MISSING` 根因不可复盘，且与 #1203 相互独立——两者都修才解锁
`blocked_missing_forcing_package_uri` 死锁。

## What Changes

- `services/orchestrator/retention.py`：`plan_retention` / `run_retention` 新增
  keyword-only `active_lower_bound: datetime | None = None`；两级判定：未到期
  → `within_retention_window`（现状）；到期但 ≥ bound → skipped
  `reason="pipeline_frontier_exempt"`（issue 原文用 `below_pipeline_frontier`，
  字面语义与保护侧相反，具名偏离改名）；豁免项不做 `_dir_size` 全树扫描；
  bound 为 None 时退化为现行纯墙钟判据。receipt 新增 `frontier` 块
  （`active_lower_bound` / `source` / `protected_count`）。
- `services/orchestrator/scheduler_runtime.py`：pass 收尾在既有内存态上计算活
  跃下界（三源取 min：candidates/blocked 候选 ∪ skipped 候选按终态 reason 排
  除法（未知 reason 落保护侧）∪ 按 discovery 双重 floor 公式重算的发现窗口地
  板）并线程化传入 `_run_retention`；`scheduler_core.py` forwarder 同步。零新
  增 I/O。
- `services/orchestrator/scheduler_evidence_payload.py`：`_compact_retention`
  allowlist 补 `frontier` 标量块，尺寸压缩下豁免证据不丢。
- `infra/env/compute.example`：retention×lookback 不变量注释改写——违反不变
  量不再导致产出→删除自旋，改为表现为受控过度保留（receipt 可见）；不变量本
  身仍须保持。
- 稳态零漂移：在 `lookback + lag + 2×max(source interval) ≤
  retention_days×24` 前置不变量下（interval 按 `allowed_cycle_hours_utc` 网格
  `0,12` 即 12h；示例配置 168+6+24=198 ≤ 336 满足），下界恒晚于 cutoff、
  `min` 取 cutoff，逐 key 计划与现状一致；越界配置下漂移方向恒为 fail-safe
  （只多留、绝不多删）。

## Impact

- Affected specs: `production-scheduler-orchestration`（ADDED Requirement：
  object-store retention 尊重流水线前沿）。
- Affected code: `services/orchestrator/retention.py` ·
  `services/orchestrator/scheduler_runtime.py:1387,1780-1818` ·
  `services/orchestrator/scheduler_core.py:243-255` ·
  `services/orchestrator/scheduler_evidence_payload.py:626-650` ·
  `tests/test_retention.py` · `tests/test_production_scheduler.py`（compaction
  pin）· `infra/env/compute.example`。
- 不动：`PROTECTED_PREFIXES={tiles,states}`、enabled/dry_run 门控语义、既有
  skip reason 词表、`scripts/node27_raw_retention.py` 与
  `services/orchestrator/cli.py` `cleanup` 入口（两者均为 pass 外删除面，无
  前沿取值来源，行为不变、无前沿保护——显式披露并同单路由，见 design D5）。
- Live AC（node-22 实机自旋消失 receipt）按 #1316→#1319 先例作为部署后兑现项
  路由 rollout issue（design D7）。
