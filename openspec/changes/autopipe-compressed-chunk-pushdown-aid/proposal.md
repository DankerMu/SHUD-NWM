# autopipe 完整性判据：受批的 run_id 压缩侧下推辅助

关联 issue：#1686。前置：#1442（key-only join 起源）、#1674（权威状态优先）、
#1681（parser 侧同类辅助先例）。终态清理 venue：#1342。

## Why

`scripts/node27_autopipeline.py::_already_ingested_runs` 的第二条语句里
`LEFT JOIN hydro.river_timeseries rt ON rt.run_key = h.run_key` 无条件对每个
run 执行。`run_key` 在压缩侧既非 `compress_segmentby` 也无索引
（`db/migrations/000047_hypertable_compression_settings.sql:16,84`），键谓词无路
可下推，于是**每个 tick 把全部三个压缩 chunk 整块解压**。no-op tick 的
`phase=ingest` 从 #1442 前的 ~240 s 涨到 ~590 s，且每新压缩一个 chunk 更慢一截。

实测（node-27，2026-08-22，只读）：`_hyper_3_51_chunk` 全部 266,091,168 行
`run_key IS NULL` —— 这个 chunk 里**没有一行**能被该 join 匹配上，每 tick 的
整块解压对它是 100% 白烧。这坐实了 issue 正文引用的
`scripts/node27_river_identity_backfill.py:452-475`（回填对压缩 chunk 一律 `skipped`）。

## What Changes

在该 join 的 **ON 子句**里加一条受批的过渡下推辅助
`AND rt.run_id = ANY(%s)`，绑定与 `h.run_id = ANY(%s)` **同一个** run_id 数组，
标记 `-- transitional compressed-chunk pushdown aid, remove with #1342`。

`run_id` 是 `hydro.river_timeseries` 的 `compress_segmentby` 第一列
（`segmentby_column_index = 1`，实测），因此辅助可被压缩侧索引消费。

计划实测（`EXPLAIN (COSTS OFF)`，50-run 样本，未执行）：

```
现状：  ->  Append
              ->  Custom Scan (DecompressChunk) on _hyper_3_32_chunk rt_1
                    ->  Seq Scan on compress_hyper_7_53_chunk

加辅助：->  Custom Scan (ChunkAppend) on river_timeseries rt
              ->  Custom Scan (DecompressChunk) on _hyper_3_32_chunk rt_1
                    ->  Index Scan using compress_hyper_7_53_chunk__compressed_hypertable_7_run_id_river
                          Index Cond: (run_id = ANY ($0))
```

`Seq Scan` -> `Index Cond`，且 `Append` -> `ChunkAppend`（获得 chunk 排除能力）。

同时：
- 改钉两条会变红的 oracle，显式标注为受批过渡辅助并绑定 #1342。
- 对两处兄弟调用点出具实机 EXPLAIN 分诊结论（同病则另单，不默认无害）。
- 更正 #1674 归档 proposal 里已被证伪的运维预期。

## Impact

- 代码：`scripts/node27_autopipeline.py` 一个函数的 SQL 与 params。**零写路径改动。**
- 测试：`tests/test_river_ts_text_identity_cleanup.py` 两条 oracle 改钉 + 新增
  辅助**位置**（ON 而非 WHERE）与 params 绑定顺序的断言。
- 规格：`river-identity-normalization` 修改 #1674 新增的那条 requirement
  （它现有场景钉着「无任何 rt 文本列被引用」，必须一并修改，不能只 ADD）。
- 运维：no-op tick 的 `phase=ingest` 应回到 ~240 s 量级。
