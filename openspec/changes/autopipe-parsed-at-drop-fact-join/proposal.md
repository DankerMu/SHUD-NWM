# 给 hydro_run 记 parse 时间戳，删掉 autopipe 完整性判据的 fact-表 join

Issue: #1789（取代 #1686 / PR #1777，owner 裁决关闭不合并）

## Why

`scripts/node27_autopipeline.py::_already_ingested_runs`（`:1135-1153`）每 tick 执行一条
`LEFT JOIN hydro.river_timeseries rt ON rt.run_key = h.run_key` 的聚合，只为拿到
`MAX(rt.created_at) AS parsed_at` 这一个 per-run 时间戳。`run_key` 在压缩侧既非
`compress_segmentby` 也无索引（`db/migrations/000047_hypertable_compression_settings.sql`），
于是每 tick 整块解压全部压缩 chunk。该语句在 180 s `statement_timeout` 内跑不完，
no-op tick 的 `phase=ingest` 从 ~240 s 涨到 ~590 s，且**单调恶化**：每多压缩一个
chunk，被白解压的数据面就多一块。

根因是 `hydro.hydro_run` 没有 parse 时间戳列。`updated_at` 已被明确排除
（`scripts/node27_autopipeline.py:1102-1104` 的 docstring 与
`tests/test_river_ts_text_identity_cleanup.py:872-894` 的负向断言）：每 tick 的
register upsert 都 bump 它，而 publish 故意不动它。

join 的另一半（存在性 `HAVING ... OR COUNT(rt.run_key) > 0`）只对 `parsed` 的 run
起作用，而 node-27 实测（2026-08-23）状态分布为 published 3174 / superseded 959 /
succeeded 140 / **parsed 0**——`parsed` 是 parse 与 publish 之间的瞬态，publish 每
tick 都跑，这条腿长期空转。

## What Changes

- 迁移新增 `hydro.hydro_run.parsed_at timestamptz`（可空，无默认）。
- `workers/output_parser/parser.py` 的 DB `mark_run_parsed` 在成功 parse 时写入该列，
  **与 `status` 状态门无关**（见 design D2——这是本单最锋利的坑）。
- 一次性回填脚本 + node-27 回填 receipt。
- 完整性判据重写为只读 `hydro_run.parsed_at`，`_already_ingested_runs` 内**零**处
  `hydro.river_timeseries` 引用。
- 随之的 oracle 改造：`_autopipeline_statement()` 改为负向钉、`RIVER_TABLE_CENSUS`
  2→1 + register、`scripts/select_ci_tests.py` 等值映射同步。
- 更正 `openspec/changes/autopipe-completeness-authority-state/proposal.md:55` 的失实预期。

## Impact

- 受影响代码：`db/migrations/`、`workers/output_parser/parser.py`、
  `scripts/node27_autopipeline.py`、`scripts/`（一次性回填）、
  `tests/test_river_ts_text_identity_cleanup.py`、`scripts/select_ci_tests.py`。
- 受影响 spec：`river-identity-normalization`（完整性判据 requirement 改写 + 新增
  parse 时间戳写入契约）。
- 部署顺序**硬约束**：迁移 → 回填 → 拉代码（design D5）。
- 不改动：#1674 的完整性语义、#1342 的删列本体、两个兄弟 key-only 探针（只分诊）。

## Non-Goals

- 不给 `river_timeseries` 加任何压缩侧下推辅助（#1686/#1777 的做法，已被裁决否决）。
- 不改 `_publish_display_runs` 与 `services/tile_publisher/forcing_copyback_backfill.py`
  的探针——只给实机 EXPLAIN 分诊结论，同病另单记账。
- 不改 `PARSE_READY_RUN_STATUSES` 与任何 `hydro_run.status` 状态迁移语义。
