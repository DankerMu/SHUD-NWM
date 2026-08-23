# Tasks — autopipe-parsed-at-drop-fact-join (#1789)

## 1. 迁移与写入点

- [x] T1 `db/migrations/000056_hydro_run_parsed_at.sql`：`ALTER TABLE hydro.hydro_run
      ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ`（可空、无默认、可重跑）。
- [x] T2 `workers/output_parser/parser.py` DB repository `mark_run_parsed`：在既有
      状态 UPDATE **之前**、同一事务内加一条**无条件**
      `UPDATE hydro.hydro_run SET parsed_at = now() WHERE run_id = %s`（design D2）。
      状态 UPDATE 与 `PARSE_READY_RUN_STATUSES` **逐字不变**。
- [x] T3 `mark_run_failed` 路径确认不写 `parsed_at`。

## 2. 判据重写与 oracle

- [x] T4 `scripts/node27_autopipeline.py::_already_ingested_runs` 判据改为 design D3
      的形状；`_ingested_run_is_current` 签名与语义不变；docstring 同步（含
      `parsed_at` 来源已从聚合改为列、NULL 队列残差措辞更新）。
- [x] T5 `tests/test_river_ts_text_identity_cleanup.py`：**先参数化共享 helper**
      `_autopipeline_statement()`（:855-861）——它硬写 `assert len(statements) == 1`，
      删 join 后 `_sql_constants(..., needle="hydro.river_timeseries")` 返回空列表，
      helper 自身先抛错，负向断言根本到不了。同一个 helper 仍被
      `test_autopipeline_publish_criterion_correlates_by_key_with_no_aid`（:895，
      `_publish_display_runs`，仍恰好一条）使用，**不得**把计数直接改成 0：
      加一个期望计数参数，或另起一个零计数专用 helper。
      随后 :865 / :872 两条测试按 design D6 改写：:865 变为"该函数体内
      `hydro.river_timeseries` 出现 0 次"的负向钉，:872 保留 `updated_at` 负向断言
      并新增 `h.status = 'published' OR h.parsed_at IS NOT NULL` 正向钉。
- [x] T6 同文件 `RIVER_TABLE_CENSUS` `scripts/node27_autopipeline.py` 2 → 1，
      并按 :1392-1398 规则更新 register。
- [x] T7 `scripts/select_ci_tests.py:1030-1046` 映射与 `tests/test_select_ci_tests.py`
      等值断言同步核对（不得漏选、不得变红）。
- [x] T8 新增 D2 的静态/单测 oracle：成功 parse 写 `parsed_at` 且与 status 无关；
      失败 parse 不写；register upsert 与 publish 不引用该列。
- [x] T8b 新增**真实 DB 集成测试**，确定性证伪 D2 的半修复形状：构造一个
      `status = 'published'` 且带旧 `parsed_at` 的 run → 成功重解析一次 →
      断言 (a) `parsed_at` 前进，(b) `status` 仍为 `'published'`，
      (c) 以新判据求值 `_already_ingested_runs` 时该 run 被判为 current、
      不再触发 handoff。这条测试是 D2 的**证伪器**：E9b 的实机观测只能佐证，
      不能证伪（生产上 published 重解析是否在观测窗内发生不可控）。

## 3. 回填

- [x] T9 一次性回填脚本（design D4 的全表单程 `GROUP BY run_key` 形态，
      `AND h.parsed_at IS NULL` 幂等只填不覆盖）；**不得**使用 `rt.run_id = ANY(...)`
      下推辅助形态。脚本需支持分批 `statement_timeout` 与日志落文件。
- [x] T10 回填脚本单测：空库、有数据、重复执行（幂等）三态。

## 4. 文档与记账

- [x] T11 更正 `openspec/changes/autopipe-completeness-authority-state/proposal.md:55`
      的失实预期。
- [ ] T12 PR body `偏离记录` 含：部署顺序为**硬**约束及其依据（判据 execute 无
      savepoint，`scripts/node27_autopipeline.py:1133`）；以及本单对 issue body 两处的
      纠正（D2 状态门、D4 回填形态）。

## Evidence Floor

### E-local（本地，合并门前必须绿）

- [x] E1 `uv run pytest -q tests/test_river_ts_text_identity_cleanup.py
      tests/test_node27_autopipeline_handoff.py tests/test_select_ci_tests.py` 全绿。
- [x] E2 `uv run pytest -q tests/test_output_parser_dual_write.py` 及 parser 相关单测全绿。
- [x] E3 `uv run ruff check .` 干净。
- [x] E4 `uv run openspec validate autopipe-parsed-at-drop-fact-join --strict
      --no-interactive` 通过。

### E-node27（真实库，只读取证 + 一次看守写操作）

- [ ] E5 迁移已应用：`\d hydro.hydro_run` 显示 `parsed_at`。
- [ ] E6 回填 receipt：回填前/后 `parsed_at` NULL 与非 NULL 两个队列的行数、耗时、
      日志路径；**漂移队列拿到非 NULL**（对照 design D4 的纠正）。
- [ ] E7 `EXPLAIN (ANALYZE, BUFFERS)` 前后对比：新判据路径上**无** `DecompressChunk`，
      语句在 `statement_timeout` 内完成，前后计划留档。
- [ ] E8 一次 no-op tick：`phase=ingest elapsed_sec` 回到 ~240 s 量级，`done rc=0`。
      tick 归属用 `ps -o lstart` 对比拉代码时间确认跑的是新代码（design D8）。
- [ ] E9a **D2 的证伪器**：T8b 的真实 DB 集成测试在 node-27 上实跑通过（不得 skip）。
      半修复形状（published 重解析不 bump `parsed_at`）只能由这条确定性测试证伪；
      实机 tick 观测不行——生产上 published 重解析是否落在观测窗内不可控，
      窗内没发生时"零新增 handoff"会**空洞通过**。
- [ ] E9b **两个连续 tick** 的实机佐证：两个 tick 均 `done rc=0`，第一 tick 后
      `already_ingested` 稳定，第二 tick 对同一 published 人群**零**新增 forcing
      handoff 尝试。断言必须落在 handoff 尝试记录上，仅看 rc 与行数不足。
      若观测窗内确有 published run 被重解析（用 `parsed_at` 前进识别），
      在 receipt 中标出——那是一次真实的正例，值钱；没有也不阻塞，因为证伪责任在 E9a。
- [ ] E10 真实 DB pytest（`NHMS_RUN_INTEGRATION=1` +
      `NHMS_INTEGRATION_DATABASE_URL`）无 skip。
- [ ] E11 两个兄弟探针（`_publish_display_runs`、
      `services/tile_publisher/forcing_copyback_backfill.py`）实机 EXPLAIN 分诊结论；
      同病则 `issue-scribe` 另单记账，不得默认无害。

**不得写死绝对计数**：#1781 期间被挡集合从 74 一路涨到 200；E6/E9 的断言一律以
"相对不变量 + 本次 tick 作用域"表述，不 pin 绝对数。
