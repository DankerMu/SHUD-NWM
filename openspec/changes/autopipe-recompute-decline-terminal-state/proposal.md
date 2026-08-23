# Terminal state for a compressed-chunk-blocked recompute

## Why

node-27 的 autopipe 每个 tick 都以 `rc=1` 结束，已持续多日，日志里累计 39,342 行
`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`（issue #1781）。机制已实测闭合：

1. node-22 于 2026-08-22 17:56 重算了三个 init 周期（`2026080712` / `2026080800` /
   `2026080812`，gfs+ifs 共 **88** 个 run）的产物。
2. `_ingested_run_is_current`（`scripts/node27_autopipeline.py:1020`）**正确**检出
   `product_mtime > parsed_at + 1`，把这些 run 排除出 `already_ingested`。
3. tick 重新 ingest，forcing handoff 写向 `met.forcing_station_timeseries` 的
   `_hyper_1_52_chunk`（2026-08-06..08-13）。
4. 该 chunk 已压缩，`check_batch_targets_uncompressed` **正确**拒写，
   `_process_run` 返回 `outcome=failed, stage=forcing_handoff`。
5. 没有任何状态推进，下个 tick 完全重复。永久环。

**判据没错，守卫也没错。缺的是终态**：一次被正确检出、但补救路径在物理上不可达的
重算，应当被**记账并终止**，而不是无限重试。

排干（解压后重放）已实测评估并被否决：需要同时解压 met `_hyper_1_52_chunk`
（250 MB → 11.9 GB）与 river `_hyper_3_55_chunk`（7.0 GB → **196 GB**），
`/home` 仅剩 521 GB，再加数小时重压——代价严重失衡于"刷新一批有效期已过的
2026-08-07/08 起报预报"。排干降级为按需 ops 动作，本变更不执行。

系统性根因：那次**手工**分层压缩（无 compression policy job，`timescaledb_information.jobs`
只有 `policy_job_error_retention`）把仍在产物重算地平线内的窗口压掉了。本变更按用户
决定一并给 tier runbook 补上压缩前置检查清单。

上下游：本变更是 #1686 / PR #1777 拿到干净 tick 基线的前置——那条 PR 因为
"AC2 需要一个 rc=0 的 tick"而被 park 成 draft。

## What Changes

- 新增 `ops.ingest_recompute_decline` 表（迁移 `000055`），键为
  `(run_id, init_state_id, product_mtime)`，记录一次被压缩块挡住的重算决定。
- `_process_run` 在 forcing 阶段拿到 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
  reason code 时写入 decline 记录并返回新 outcome `declined`（而非 `failed`）。
- tick 汇总新增 `runs.declined` / `runs.declined_runs` / `declines_active`，
  `rc` 判据排除 `declined`，故 tick 收 `rc=0`。
- `_already_ingested_runs` 批量取回本 tick run_ids 的 decline 记录，键完全匹配者
  并入返回集——与已有的 `retired`（`status='superseded'`）并列的**状态无关**排除项。
  实测这 88 个 run 里 60 个是 `published`、**28 个是 `succeeded`**，后者从不进入
  完备性查询，所以抑制必须装在这里而不是 `_ingested_run_is_current`（见 design D2）。
  **任一键分量变化（新的 init_state 或更新的产物 mtime）自动重开决定**。
- `docs/runbooks/tier-node27-timeseries-storage.md` 新增压缩前置检查清单，含可执行 SQL。

## Impact

- Affected specs: `hypertable-compression`
- Affected code: `scripts/node27_autopipeline.py`、`db/migrations/000055_*.sql`、
  `docs/runbooks/tier-node27-timeseries-storage.md`
- Affected tests: `tests/test_node27_autopipeline_handoff.py`、
  `tests/test_river_identity_normalization_integration.py`
- 生产影响：node-27 autopipe 从每 tick `rc=1` 恢复为 `rc=0`；88 个 run 的数据保持
  2026-08-22 之前的状态，并以可查询记录问责。
