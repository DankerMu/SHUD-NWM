## Why

node-22 生产的 retention 只扫 `OBJECT_STORE_ROOT` 一个根，而 SHUD run 工作区实际落在**三个互不相同的根**上
（`compute.scheduler-dbfree.env:12/13/15`）。2026-08-19 实机取证：被扫的根 `runs/` 稳定在 8 天跨度（窗口内，
retention 本身工作正常），而 `WORKSPACE_ROOT/runs`（5274 个 run，最老 cycle 2026-05-30）与
`NHMS_OBJECT_STORE_COPYBACK_ROOT/runs`（3375 个 run，最老 cycle 2026-06-30）**从未被回收过一次**。
copyback 根在 NFS 上，物理位于 node-27 的 `/home` 卷 —— 与 PostgreSQL 数据目录同卷（1.7T，free 536G），
所以这条泄漏直接吃数据库余量，且 receipt 照常报 `completed`，容量耗尽前无任何可判读信号（issue #1318）。

## What Changes

- `plan_retention` / `run_retention` 接受一组**额外根**，对每个额外根**只扫 `runs/`**，不扫 cycle 前缀。
- 额外根使用**独立的保留窗口** `NHMS_RETENTION_EXTRA_ROOTS_DAYS`（默认 30 天），与主根的
  `NHMS_RETENTION_DAYS`（14 天）互不影响。
- 额外根由新闸门 `NHMS_RETENTION_EXTRA_ROOTS_ENABLED`（默认 `false`）控制；关闸时行为与变更前逐字节一致。
- **BREAKING（receipt 契约）**：retention receipt `schema_version` 由
  `nhms.production_scheduler.retention.v1` 升为 `...v2`；新增顶层 `extra_roots` 块（窗口 + 根清单），
  且 `planned`/`deleted`/`skipped`/`failed` 每条目新增 `root` 字段。
  升版本的理由：一份 receipt 现在同时承载两个不同保留窗口的判定，沿用 v1 会让读者无法判断某条目按哪个窗口裁定。
- 调用点 `scheduler_runtime` / `scheduler_core` 转发 workspace 根与 copyback 根。

## Capabilities

- **New Capabilities**: 无。
- **Modified Capabilities**: `production-scheduler-orchestration` —— 该 capability 已经约束 pass 侧 retention 的
  前沿豁免与 receipt 形状，本变更改动其**根覆盖面**与 **receipt schema 版本**，属需求级行为变化。

## Impact

- 代码：`services/orchestrator/retention.py`、`services/orchestrator/scheduler_runtime.py`、
  `services/orchestrator/scheduler_core.py`、`services/orchestrator/cli.py`、
  **`services/orchestrator/scheduler_evidence_payload.py`**（`_compact_retention` 白名单必须放行
  新增的 `extra_roots` 块，否则 receipt 一被压缩就不可判读——见 design.md D3）。
- 配置：`infra/env/compute.example`、`infra/env/compute.scheduler-dbfree.env.example` 新增两个旋钮。
- 测试：`tests/test_retention.py`（注意 `:596` 是 skipped 条目的**闭集**断言
  `assert set(entry) == {"key","path","cycle_time","reason"}`，加 `root` 后必红；修它时必须保住
  `:597` `assert "size_bytes" not in entry` 与 `:599` 的「豁免项不被 walk」意图——那是 #1307 的 NFS 成本保护，
  不得用宽松断言绕过）、`tests/test_production_scheduler.py`（三处 `schema_version` 断言）。
- 运行面：闸门默认关，合并即上线为**零行为变化**；真实回收分两步（22 上 dry-run 审清单 → enforce），
  不在本变更的合并门内。
