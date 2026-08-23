# Design

## Risk triage

- **Fixture level: `expanded`.** 生产环路改写 + 新迁移 + 需要 node-27 实机双 tick
  验收。上游 issue #1781 未标注 suggested fixture level（它是本会话内 scribe 立的
  运维单，不来自 stage-change-pipeline），故 triage 从零起，记录于此。
- **Risk packs selected**: `state-machine-correctness`（新终态进入 tick outcome 词表）、
  `data-loss-and-staleness`（终态化 = 承认陈旧，必须可问责且可重开）、
  `db-migration`（新表 + 新写路径）、`production-loop-safety`（改的是每 tick 都跑的环）。
- **Not selected**: `security-boundary`（无新凭据、无新对外面）、
  `frontend-contract`（无前端面）、`performance-regression`（新增查询是单次批量
  `WHERE run_id = ANY(%s)` 的窄表查，与 #1686 的压缩块扫描不同量级）。

## D1 — 终态记录放哪：新窄表，不复用 `ops.pipeline_event`

`ops.pipeline_event`（`db/migrations/000009_ops.sql:23-34`）是 append-only 的通用事件流，
`details JSONB`。用它就意味着承载全部判断的那次查询——"本 run 在这个
(init_state_id, product_mtime) 键上是否已被终态化"——要靠 JSONB 匹配 + 取最新一条来做。
把最关键的一步做成最脆的一步。

选新表 `ops.ingest_recompute_decline`，只做一件事：

```sql
CREATE TABLE IF NOT EXISTS ops.ingest_recompute_decline (
  run_id          TEXT             NOT NULL,
  init_state_id   TEXT             NOT NULL,
  product_mtime   DOUBLE PRECISION NOT NULL,
  reason_code     TEXT             NOT NULL,
  detail          TEXT,
  declined_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, init_state_id, product_mtime)
);
```

`product_mtime` 用 `DOUBLE PRECISION` 而非 `TIMESTAMPTZ`：它必须原样往返
`os.stat().st_mtime` 的浮点值，等值匹配才成立；转成 timestamp 会引入精度截断，
让"同一次产物"在下个 tick 匹配不上，环就回来了。

三个键列全 `NOT NULL`。写入用 `ON CONFLICT DO NOTHING`——同一 tick 内的并发
worker 与跨 tick 重放都幂等。

迁移无 `GRANT`：全库 `db/migrations/` 现无任何 GRANT 语句，本变更不发明惯例
（见 non-goals）。

## D2 — 闸门装在哪：`_already_ingested_runs` 的状态无关排除集

**Fixture 审查（P1）推翻了初版设计，这里记录被推翻的内容与实测依据。**

初版把 decline 闸门装进 `_ingested_run_is_current`。实测（node-27，2026-08-23）
88 个被挡 run 的 `hydro_run.status`：

| status | 数量 | 是否进入完备性 SQL（`status IN ('parsed','published')`，`:976`） |
|---|---|---|
| `published` | 60 | 是——排除边确为 `:1021` 的 mtime 比较 |
| `succeeded` | 28 | **否**——从未 parsed/published，该 SQL 根本不返回它们 |

即 `_ingested_run_is_current` 对 28 个 run 是死代码：它们的 pending 资格与
完备性判据无关，纯粹是"从未成功 ingest 过"。装在那里的闸门只能治 60 个。

正确落点是 `_already_ingested_runs` 已有的**状态无关排除**并集项。该函数已经这么干过：
`retired`（`:948-953`，`status = 'superseded'` 的 run 无条件跳过，不查时间序列行也不查
manifest 时效）被无条件并入返回集。decline 是同一形状的第三项：

```python
return retired | declined | {…完备性行…}
```

`declined` 的构造：一次 `SELECT run_id, init_state_id, product_mtime
FROM ops.ingest_recompute_decline WHERE run_id = ANY(%s)`，对**返回的 run**（只有它们，
所以 stat 开销与 decline 行数同阶，不与 pending 规模同阶）读 object store 的
manifest `initial_state.state_id` 与 `_run_product_mtime`，三分量全等才排除。

`_ingested_run_is_current` **不改动**——初版提出的 `product_mtime` 计算上提也随之取消。
这比初版少一个改动点，且两个总体用同一条路径。

`--force` 路径不受影响：`done = set() if args.force else …`（`:2124-2131`）已经绕过
整个函数，故也绕过 decline。这是正确行为——显式 force 应当重开一切。

## D3 — fail-closed：拿不到完整键就不终态化

decline 的键需要 manifest 的 `initial_state.state_id` 与 `_run_product_mtime` 两者齐备。
两处规则：

> **写入侧**：`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 发生时，若任一键分量缺失，
> 或 decline 写入本身抛异常，outcome 保持 `failed`，run 继续重试。
>
> **读取侧**：比对时若任一键分量取不到，不排除，run 继续进入 pending。

绝不出现"以为记了账所以不再重试，实际没记上"的静默丢失。这是本设计唯一的
fail-closed 点，也是它必须被测试钉死的原因（T6）。

## D4 — 重开语义：键即重开条件

decline 命中要求 `(run_id, init_state_id, product_mtime)` 三者**全等**。因此：

| 变化 | 键匹配 | 结果 |
|---|---|---|
| 无变化，下个 tick | 命中 | 不进 pending（环终止） |
| node-22 重算产物（mtime 变新） | 不命中 | 重开，正常重试 |
| init_state 变更 | 不命中 | 重开，正常重试 |
| 运维解压该 chunk 后重算 | 不命中（mtime 变新） | 重开，写入成功 |

`init_state_id` 取的是 **manifest 的**（`initial_state.state_id`），不是 DB 的——
它是产物的指纹，而 28 个 `succeeded` run 未必有可信的 DB 侧 init_state。

运维**只解压不重算**的情形不会自动重开——有意为之，需运维显式删除 decline 行；
runbook 清单里写明。

### 浮点等值匹配在 NFS 上安全吗（审查 P2）

相邻的 `:1021` 用了 `+ 1.0` 的容差带，而这里用严格等值。理由：

1. `st_mtime` 由文件已存储的 `st_mtim` 纳秒值确定性转换而来。文件未被改写时，
   两次 `stat()` 拿到的是**同一个存储值**，转换确定，故 IEEE-754 位相同。
   NFS 属性缓存影响的是"多久看到新值"，不是"同一个值转出两个浮点"。
2. 即便万一不等，**失效模式是自愈的、不丢数据**：不匹配 → 重开 → 再被挡一次 →
   以新键再写一行 decline（表是累积的）→ 之后任一取值都能命中某一行。
   最多多付一两个被挡的 tick，不会退化成永久环。

正因为失效方向安全，这里不引入容差——容差会让"真实重算恰好落在容差内"
被误当成同一次产物，那才是不可自愈的方向。

## D5 — 计数器与 rc：多数是"无需改动"，别过度工程

审查确认：`rc = 0 if (not seed_failed and not by("failed")) else 1`（`:2283`）、
`publish_eligible`（`:2194`）、`stats_guard` 的 `ingested_runs`（`:2208`）
**全部按字符串精确匹配 `"failed"` / `"ingested"`**。新增一个 `"declined"` outcome
天然不落入任何一个。

因此 T5 的实质只有"新增汇总字段"，**不要**再加一个冗余的排除条件。
测试仍要钉死这些计数器不被污染（E6）——钉的是不变量，不是新代码。

## D6 — parse 阶段守卫：显式 non-goal

`workers/output_parser/parser.py:976` 挂着同一个守卫，写向
`hydro.river_timeseries`。本变更**不**给它做终态化，理由三条，全部实测：

1. 全日志 `CompressedChunkWriteError` 出现 **0 次**，`stage` 全是 `forcing_handoff`。
2. `_process_run` 在 forcing 失败时直接 return，parse 根本不可达——只要 forcing
   先终态化，parse 侧在当前拓扑下不可达。
3. parse 是子进程（`workers.output_parser.cli`），只有 rc + stderr 文本，
   **没有 machine-readable reason 通道**；给它加一条是真实的额外范围。

唯一可达路径是运维**部分解压**（解了 met 没解 river）。这一条写进 runbook 的
解压小节，而不是用代码去猜。

## D7 — runbook 前置检查（用户明确要求并入本次）

根因是手工分层压掉了仍在产物重算地平线内的窗口。检查清单插在
`docs/runbooks/tier-node27-timeseries-storage.md` §4.1 之后（§4.3 是解压、
反应式的，不是这里）。清单必须可执行，含：

- 对目标窗口 `[S, E)` 查 `ops.ingest_recompute_decline` 是否已有落在窗口内的记录；
- 最近 N 次 tick 汇总的 `declines_active` 与 `forcing_handoff` 失败情况；
- 目标 chunk 的 `range_end` 相对产物重算地平线的年龄判断。

## D8 — 与其它 open change 的所有权重叠

`openspec/changes/tier-node27-timeseries-storage/` 仍 open，拥有该 runbook 与
`specs/hypertable-compression`。本变更编辑同一 runbook、向同一 spec 加 requirement，
合法但需记录：两个 change 并存期间各自 `--strict` 通过；归档顺序以合并顺序为准。

`openspec/changes/autopipe-compressed-chunk-pushdown-aid/`（#1686，PR #1777 draft）
的 delta 落在 `specs/river-identity-normalization`。本变更**刻意避开**那个 spec 文件，
delta 只落 `hypertable-compression`，以免两个 open change 改同一 spec 文件。

## Must-preserve behavior

- 瞬态 forcing 失败（`HANDOFF_APPLY_SQL_FAILURE`、通用异常路径等）**仍然** `rc=1`
  并继续重试。`tests/test_node27_autopipeline_handoff.py:414` 钉的就是这条，必须保持绿。
- `_already_ingested_runs` 现有的 authority-state 语义（#1674 的
  published/parsed 判据、#1442 的 key-only join）不变；decline 判定发生在
  `_ingested_run_is_current` 内，位于那些 SQL 语义的下游。
- `retired`（`status='superseded'` 无条件跳过）语义不变；decline 是并列的第二个
  排除项，不改变它。
- 产物 mtime 与 `parsed_at` 的比较语义不变——`tests/test_river_identity_normalization_integration.py:1122`
  与 `:1201` 必须保持绿。

## Seams under test

- `scripts/node27_autopipeline.py::_process_run` — forcing reason code → outcome 分流。
- `scripts/node27_autopipeline.py::_already_ingested_runs` — decline 作为 `retired`
  之后的第二个状态无关排除并集项（唯一的抑制点，覆盖 published 与 succeeded 两个总体）。
- `scripts/node27_autopipeline.py::main` — 汇总新增字段（rc 判据无需改动，见 D5）。
- `scripts/node27_autopipeline.py::_ingested_run_is_current` — **不改动**（见 D2）。
- `tests/test_node27_autopipeline_handoff.py::_handoff_unavailable` — 已有的
  "报告式 unavailable reason" 模拟入口，新终态测试复用它。
- `tests/test_river_identity_normalization_integration.py::_seed_run_facts` — 已有的
  真实 DB 播种入口，重开测试复用它。

## Non-goals

- 不执行排干（解压 + 重放）。代价实测倒挂，降级为按需 ops 动作。
- 不给 parse 阶段守卫做终态化（D6）。
- 不给迁移加 GRANT（全库无此惯例；`nhms_ingest_rw` 角色本身不存在，属 #1774）。
- 不追查 node-22 为何在 2026-08-22 17:56 重算——未测不猜，且本设计对两种答案都鲁棒。
- 不改压缩选块代码（`scripts/node27_timeseries_compression.py`）；前置检查是 runbook 清单。
