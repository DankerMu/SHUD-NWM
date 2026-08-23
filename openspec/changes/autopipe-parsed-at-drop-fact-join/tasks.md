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
- [x] T8c **既有集成 seed 必须学会 `parsed_at`**（node-27 `-m integration` 实测暴露的
      4 条红：`tests/test_river_identity_normalization_integration.py`）。旧判据从 seed
      的事实行聚合出解析时间戳，新判据只读权威列，所以"这个 run 在 T 被解析过"的 seed
      必须把同一个 T 写到 `hydro_run.parsed_at`（mtime 比较的相对余量逐字保留）。
      `_seed_run_facts(normalized=True)` 顺带 stamp——与 D4 回填同口径：只有 `run_key`
      可见的行才聚合得出时间戳，NULL-key 行仍留 NULL、仍走退化路径。
      `#1674 (iii)` 那条前提失效（完整性证据是 `parsed_at IS NOT NULL` 而非行存在），
      按新判据重写并改名，负向半边不与
      `tests/test_hydro_run_parsed_at_integration.py` 重复。

## 3. 回填

- [x] T9 一次性回填脚本（design D4 的全表单程 `GROUP BY run_key` 形态，
      `AND h.parsed_at IS NULL` 幂等只填不覆盖）；**不得**使用 `rt.run_id = ANY(...)`
      下推辅助形态。脚本需支持分批 `statement_timeout` 与日志落文件。
- [x] T10 回填脚本单测：空库、有数据、重复执行（幂等）三态。

## 4. 文档与记账

- [x] T11 更正 `openspec/changes/archive/2026-08-21-autopipe-completeness-authority-state/proposal.md:55`
      的失实预期。
- [x] T12 PR body `偏离记录` 含：部署顺序为**硬**约束及其依据（判据 execute 无
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

- [x] E5 迁移已应用：`\d hydro.hydro_run` 显示 `parsed_at`。
- [x] E6 回填 receipt：回填前/后 `parsed_at` NULL 与非 NULL 两个队列的行数、耗时、
      日志路径；**漂移队列拿到非 NULL**（对照 design D4 的纠正）。
- [x] E7 `EXPLAIN (ANALYZE, BUFFERS)` 前后对比：新判据路径上**无** `DecompressChunk`，
      语句在 `statement_timeout` 内完成，前后计划留档。
- [x] E8 一次 no-op tick：`phase=ingest elapsed_sec` 回到 ~240 s 量级，`done rc=0`。
      tick 归属用 `ps -o lstart` 对比拉代码时间确认跑的是新代码（design D8）。
- [x] E9a **D2 的证伪器**：T8b 的真实 DB 集成测试在 node-27 上实跑通过（不得 skip）。
      半修复形状（published 重解析不 bump `parsed_at`）只能由这条确定性测试证伪；
      实机 tick 观测不行——生产上 published 重解析是否落在观测窗内不可控，
      窗内没发生时"零新增 handoff"会**空洞通过**。
- [x] E9b **两个连续 tick** 的实机佐证：两个 tick 均 `done rc=0`，第一 tick 后
      `already_ingested` 稳定，第二 tick 对同一 published 人群**零**新增 forcing
      handoff 尝试。断言必须落在 handoff 尝试记录上，仅看 rc 与行数不足。
      若观测窗内确有 published run 被重解析（用 `parsed_at` 前进识别），
      在 receipt 中标出——那是一次真实的正例，值钱；没有也不阻塞，因为证伪责任在 E9a。
- [ ] E10 真实 DB pytest（`NHMS_RUN_INTEGRATION=1` +
      `NHMS_INTEGRATION_DATABASE_URL`）无 skip。
- [x] E11 两个兄弟探针（`_publish_display_runs`、
      `services/tile_publisher/forcing_copyback_backfill.py`）实机 EXPLAIN 分诊结论；
      同病则 `issue-scribe` 另单记账，不得默认无害。

**不得写死绝对计数**：#1781 期间被挡集合从 74 一路涨到 200；E6/E9 的断言一律以
"相对不变量 + 本次 tick 作用域"表述，不 pin 绝对数。


## 实测落盘（node-27，2026-08-23）

- **E5** 迁移 000056：`parsed_at | timestamp with time zone | YES | (无默认)`；
  `lock_timeout=5s` 未触发（先停 timer、等 21:17:25 那个 tick 自然结束再上，display 面零阻塞）。
- **E6** 回填 pass1 21:18:48→21:22:42（3m54s）`updated=1593`；pass2 `updated=0`
  （幂等 + 暂停窗口零 parse 双证）。按 status 拆分：published 3223（stamped 1593 /
  **unstamped 1630**）、superseded 959（全 unstamped，无条件退休）、succeeded 140
  （不在 status 过滤内）。`hydro_run` 侧 `run_key IS NULL` = **0**。
  聚合阶段临时文件 +385 MiB 后归零，`hydro_run` 表 7296 kB、`n_dead_tup=0`。
- **E7** cost 77,288,419 → **641**；`DecompressChunk` 2 → **0**；`river_timeseries`
  完全不出现；执行 180 s 跑不完 → **5.656 ms**；`shared hit=605` 零 read。
- **E8** no-op tick `phase=ingest elapsed_sec=6`、`done rc=0`（基线 1431–1608 s，
  目标 ~240 s）。tick 归属：进程 `lstart` 21:36:58 / 21:56:21 均晚于 `PULL_AT` 21:25:59。
- **E9a** node-27 真实库 `tests/test_hydro_run_parsed_at_integration.py` **3 passed, 0 skipped**。
- **E9b** 两连 tick 均 rc=0；tick1 `processed=21 already_ingested=2128 declined=0 failed=0`，
  tick2 `processed=0 already_ingested=2128 declined=0 failed=0`——第二 tick 对同一 published
  人群零新增 forcing handoff。
- **E11** 两个兄弟探针**同病确认**，已在既有单补实测（非新立单：#1779 / #1778 已存在）：
  - `_publish_display_runs`：cost **77,199,185**、两个 `DecompressChunk`、Append 估 66.4 亿行。
    `status='parsed'` 不是过滤而是让 hash build 侧为空触发短路，故**只在真正 publish 的 tick 上**
    付费。实测 `published` 7→1489 s、21→1102 s、0→**6 s**，相关性精确。
    **即本单只解决了一半**：无条件解压消失，publish tick 仍付 ~1100 s，且由"常态慢"变"间歇慢"。
  - `forcing_copyback_backfill.py`：cost **79,874,818**，同病；其 `variable`/`variable_e`
    "下推辅助"经计划核对**无效**（`variable` 是 000047 的 orderby 非 segmentby），
    且事实表聚合是 Nested Loop 驱动侧，无空 build 侧短路。
- **部署与本地 HEAD**：node-27 跑 28d399e7；`git diff --stat 28d399e7..HEAD` 仅
  `openspec/.../proposal.md`，非 openspec 文件数 **0**，代码逐字等效。
