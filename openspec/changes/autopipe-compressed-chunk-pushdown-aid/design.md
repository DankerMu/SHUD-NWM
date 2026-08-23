# Design

## D1 —— 选方案 (a)，不选 (b)/(c)

issue 列了三条。选 (a)：与 #1681 已落地的先例同形、代价最小、债务与 #1342 同期清理。

- 不选 (b)（把 join 收窄到 `parsed` 子集）：issue 自己标了 tradeoff ——
  它会把 `_ingested_run_is_current` 的重算检测残差从 legacy NULL-key 队列
  悄悄扩大。见 D3，那正是本单必须**避免**而非引入的方向。
- 不选 (c)（等 #1342）：#1342 是 `priority:low` 且依赖 #1340/#1341，本单要一直带病等。

## D2 —— 辅助必须在 ON 子句，不能在 WHERE

这是本改动唯一的真语义陷阱。若辅助落到 `WHERE`，LEFT JOIN 的 NULL-extended 行会
被过滤掉：一个 rt 行数为 0 的 **published** run 会整个从结果里消失、被重新 ingest。
那是真正的语义变更。

=> 必须有一条 oracle 断言辅助的**位置**（在 `ON` 与 `WHERE` 之间），
不能只断言「SQL 里出现了 `rt.run_id`」。

`run_ids` 数组现在绑定两次（ON 一次、WHERE 一次），params 元组顺序必须对应。

## D3 —— 失效方向分析：主体安全，有一个必须钉住的例外

辅助在 ON 子句里只能**减少** rt 侧匹配行，`COUNT(rt.run_key)` 单调不增。

走 `HAVING h.status = 'published' OR COUNT(rt.run_key) > 0`：

- `parsed` 的 run：最多从「有行」翻成「无行」-> 判定未 ingest -> **重新 parse**。
  写入路径 replay-convergent（#1442 "replay heals"，
  `workers/output_parser/parser.py:1049-1073` 的 `ON CONFLICT ... DO UPDATE`），
  所以最坏结果是一次冗余的幂等重跑。**安全。**
- `published` 的 run：走第一个析取项，永远在结果集里，与 rt 侧无关。**判定不受影响。**

**例外（本单的核心风险）**：`published` run 若被辅助剔光 rt 行，
`MAX(rt.created_at) AS parsed_at` 会变成 NULL，而
`scripts/node27_autopipeline.py:1001-1002`：

```python
if product_mtime is None or parsed_at is None or not hasattr(parsed_at, "timestamp"):
    return True
```

`parsed_at is None` 直接返回 `True`（视为当前）-> 该 run 留在 already_ingested ->
即使产物 mtime 更新了也**不会重算**。方向是「漏跑」，不是「多跑」。

这等于把 #1674 已记账的那条残差（"legacy 无键可见行的 run 只剩 init-state 比对"）
从 legacy NULL-key 队列扩大到「任何 run_id 与 run_key 漂移的 published run」。

**风险边界（实测 + 结构）**：该扩大量等于 run_id ↔ run_key 漂移的行数。

- 结构：`river_timeseries.run_id` NOT NULL 且是 PK 第一列；`hydro_run` 4133 行
  run_id 与 run_key 双射、run_key 无 NULL。
- 写入路径：`parser.py:1000-1024` 每批 `row.run_id` 逐行、`run_key` 整批同一标量，
  同一条 INSERT 写入 => 同批同源。
- 实测：`_hyper_3_91_chunk`（最小未压缩，4.4 GB）8,145,648 行，
  run_key 全部已填充，orphan 0、divergent 0。
- 反面诚实记录：`_hyper_3_51_chunk`（压缩，5.1 GB）266,091,168 行 run_key **全为 NULL**，
  该 chunk 的一致性结论是**空洞的**，不作为证据。
- 库里从未审计过这条：migration 000050 的
  `hydro.verify_river_identity_normalization()` 审计了
  variable_e/unit_e/quality_flag_e/basin_version_key，**唯独没有 run_key ↔ run_id**。

=> 结论：漂移为 0 的证据是「结构 + 写入路径 + 一个非空洞 chunk 实测」，
不是全库实测。这条残差扩大**必须写进规格 delta 与交付 receipt**，不得默默带过。

## D4 —— 兄弟调用点分诊（验收标准第 6 条）

本单不扩范围，但必须给出实机 EXPLAIN 结论：

- `scripts/node27_autopipeline.py:1153-1161` `_publish_display_runs` 的
  `AND EXISTS (... WHERE rt.run_key = h.run_key)`：同为 key-only 无辅助。
- `services/tile_publisher/forcing_copyback_backfill.py:73-85`：已带
  `rt.variable = 'q_down'` 辅助，但 `variable` 是 000047 的 **orderby** 而非
  segmentby，能否真剪掉压缩批次未实测。

同病则记账另单（issue-scribe），不得默认无害。

## D5 —— 更正 #1674 的失实预期（验收标准第 5 条）

`openspec/changes/archive/2026-08-21-autopipe-completeness-authority-state/proposal.md`
的 Impact 段写「node-27 下一 tick 即恢复 ~4 min rc=0」，已被实测证伪
（部署后 tick 落在 591–1071 s）。本单落地时以「更正」形式补记，不改写历史结论。

## 风险包选择

| 包 | 选? | 理由 |
|---|---|---|
| Public API / CLI / script entry | 否 | 无入口签名或 CLI 变更 |
| Config / project setup | 否 | 无配置面 |
| File IO / path safety / overwrite | 否 | 无文件写 |
| Schema / columns / units / field names | 否 | 无 DDL、无列语义变更 |
| Auth / permissions / secrets | 否 | 无 |
| Concurrency / shared state / ordering | **是** | 判据决定是否重跑 ingest，属持久状态转移；见 D3 |
| Resource limits / large input / discovery | **是** | 本单动机即资源；300+ GB hypertable 的访问路径 |
| Legacy compatibility / examples | **是** | NULL-key legacy 队列与 #1674 已记账残差 |
| Error handling / rollback / partial outputs | 否 | 无新错误路径；失效方向见 D3 |
| Release / packaging / dependency | 否 | 无 |
| Documentation / migration notes | **是** | D5 的归档更正 |
| PostGIS / TimescaleDB domain behavior（domain） | **是** | 压缩 chunk segmentby 下推是本单主体 |
| Hydro-met time series / forcing windows（domain） | **是** | river_timeseries 完整性判据 |
| Geospatial / CRS（domain） | 否 | 不触几何 |
| SHUD 数值运行时（domain） | 否 | 不触求解器 |
| Slurm 生产生命周期（domain） | 否 | 不触调度 |
| 外部气象源快照（domain） | 否 | 不触 provider |
| Run manifest / QC provenance（domain） | **是** | `_ingested_run_is_current` 消费 manifest 的 init_state 与 mtime |
| 已发布 NHMS 产物 / display identity（domain） | 否 | 不触 display 读路径 |
