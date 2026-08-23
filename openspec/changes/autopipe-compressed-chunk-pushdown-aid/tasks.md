# Tasks

## Risk triage

```text
Issue type: refactor (perf)
Project profile: NHMS
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent (issue predates the pipeline contract; triaged here)
Why:
- 命中 profile 强制 domain trigger：`Timescale` / `hypertable`
- 判据结果驱动是否重跑 ingest -> 持久/共享状态转移（core trigger）
- 触及 #1674 已记账的 legacy 兼容残差，可能扩大之
Selected risk packs:
- Concurrency / shared state / ordering
- Resource limits / large input / discovery
- Legacy compatibility / examples
- Documentation / migration notes
- PostGIS / TimescaleDB domain behavior
- Hydro-met time series / forcing windows
- Run manifest / QC provenance
OpenSpec change: autopipe-compressed-chunk-pushdown-aid (generated)
Evidence floor:
- E1 uv run pytest -q tests/test_river_ts_text_identity_cleanup.py tests/test_node27_autopipeline_handoff.py tests/test_river_identity_normalization_integration.py
- E2 uv run ruff check .
- E3 openspec validate autopipe-compressed-chunk-pushdown-aid --strict --no-interactive
- E4 node-27 只读 EXPLAIN (ANALYZE, BUFFERS) 前后对比，压缩腿出现 Index Cond
- E5 node-27 一次 no-op tick 的 phase=ingest elapsed_sec 回到 ~240 s 量级，done rc=0
- E6 node-27 实测 run_id/run_key 漂移计数 + 覆盖范围声明
- E7 两处兄弟调用点的实机 EXPLAIN 分诊结论
- E8 注入 run_id/run_key 漂移的真实 DB 用例：published run 的 rt 行 run_key 匹配但
  run_id 不在绑定数组内 + 产物 mtime 更新 -> 断言重算检测**未被静默跳过**
  （tests/test_river_identity_normalization_integration.py，套件已有 _seed_run /
  _seed_run_facts / _compress_all_river_chunks 与既有 _already_ingested_runs 断言）
```

## Must-preserve behavior

- `published` 的完整性判定不依赖 rt 行可见性（#1674，不得回退）。
- `parsed` 仍要求至少一条键可见行。
- `superseded` 仍无条件退役。
- join 条件本身仍是 `rt.run_key = h.run_key`；`rt.run_id = h.run_id` 文本 fact join 仍禁止。
- 零写路径改动。

## Seams under test

- `scripts/node27_autopipeline.py::_already_ingested_runs` 的 SQL 文本与 params 元组。
- `scripts/node27_autopipeline.py::_ingested_run_is_current` 的 `parsed_at is None` 分支。
- `tests/test_river_ts_text_identity_cleanup.py` 的 `_assert_switched_surface` shape oracle
  （`tests/test_sql_shape_helpers.py:150-151` 的 `TEXT_AID_COUNTERPARTS` 已含 run_id->run_key，
  但 `outer_predicates` / `assert_text_fact_columns` **都不区分 ON 与 WHERE**，D2 的位置断言
  须新写）。
- `tests/test_river_identity_normalization_integration.py:913-1150` 真实 DB 套件：
  唯一能对 `_already_ingested_runs` 的**返回值**与 `parsed_at` 驱动的重算检测下断言的地方。

## Non-goals

- #1674 的完整性语义（不回退）。
- #1681 的 parser 侧辅助（已落地，另单）。
- #1342 的删列与重压缩本体。
- 写路径、`variable_e` / 枚举面。
- 兄弟调用点的**修复**（只出分诊结论，同病另单）。

## Steps

- [x] T0 前置实测：方案 (a) 计划 A/B、chunk 清单、语义安全性（`.workplans/queue/1686-premise-probe.md`）
- [ ] T1 fixture review（只读 reviewer）+ `openspec validate --strict --no-interactive`
- [ ] T2 implementer：在 ON 子句加受批辅助 + params 双绑定 + 注释标记绑定 #1342
- [ ] T3 implementer：改钉 `test_autopipeline_ingest_criterion_joins_by_key_with_no_aid`
      与 `test_autopipeline_ingest_criterion_is_authority_state_first`，
      新增辅助**位置**（ON 非 WHERE）与 params 顺序的断言
- [ ] T3b implementer：在 tests/test_river_identity_normalization_integration.py 新增
      漂移注入用例（E8），覆盖 D3 那条唯一的非安全路径
- [ ] T4 E1/E2/E3/E8 本地绿
- [ ] T5 node-27：E4 前后 EXPLAIN (ANALYZE, BUFFERS) 留档
- [ ] T6 node-27：E6 漂移计数 + 覆盖范围声明
- [ ] T7 node-27：E7 两处兄弟调用点 EXPLAIN 分诊，同病则 issue-scribe 立单
- [ ] T8 node-27：E5 no-op tick 计时
- [ ] T9 更正 #1674 归档 proposal 的失实运维预期（D5）
