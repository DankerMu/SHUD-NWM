# Design — autopipe-parsed-at-drop-fact-join (#1789)

## Risk Triage

- Fixture level: **expanded**（issue 的 `Suggested fixture level` 亦为 expanded，无分歧）。
- 选中的 risk packs：
  - `db-migration` —— 新增列 + 一次性回填 + 硬部署顺序。
  - `production-loop-safety` —— 完整性判据是每 tick 执行的生产回路；判据写错的失败形状
    是**永久重投环**（#1781 刚刚修掉一个）。
  - `data-loss-and-staleness` —— `parsed_at` 陈旧 = 重算检测失效 = 数据陈旧不被发现。
  - `state-machine-correctness` —— 写入点紧邻 `hydro_run.status` 状态门，误改会
    downgrade `published`。
- 未选：`security-authz`（无权限面改动）、`api-contract`（无对外契约面改动）。

## Must-Preserve Behavior

1. **#1674 权威状态优先不得回退**：`published` 的完整性**不得**依赖 fact 行可见性。
   判据的 gate 必须是 `h.status = 'published' OR h.parsed_at IS NOT NULL`，
   **绝不能**写成裸的 `h.parsed_at IS NOT NULL`。
2. `superseded` 仍无条件退休；#1781 的 decline 排除集合仍无条件生效（本单不碰
   `_declined_runs` / `_decline_key`）。
3. `hydro_run.status` 的状态迁移语义**逐字不变**：`PARSE_READY_RUN_STATUSES` 不放宽，
   `published` 不被 downgrade 回 `parsed`。
4. `updated_at` 仍不得被当作 parse 时间戳使用（现有负向断言保留）。
5. `_ingested_run_is_current` 在 `parsed_at is None` 时返回 True 的降级行为不变——
   NULL 队列的重算检测退化程度与今天**完全一致**，不更差。

## Decisions

### D1 — 新列形状

`hydro.hydro_run.parsed_at TIMESTAMPTZ`，可空，**无默认值**。迁移
`db/migrations/000056_hydro_run_parsed_at.sql`，`ADD COLUMN IF NOT EXISTS`（可重跑）。
迁移内**不做**回填——回填是一次昂贵的、需要看守的独立操作（D4）。

无默认值是刻意的：`DEFAULT now()` 会让每一个新 register 的 run 一出生就带时间戳，
把 `parsed_at` 变成第二个 `updated_at`（must-preserve #4 正是在防这个）。

### D2 — 写入必须与状态门无关（本单最锋利的坑，issue body 未察觉）

issue 的处方是"在 `mark_run_parsed` 的 `SET status = 'parsed', ...` 里一并写
`parsed_at = now()`"。**这个处方不满足它自己写下的要求**（"重新 parse 必须 bump 它"）：

- `workers/output_parser/parser.py:1176` 的 UPDATE 带 `AND status IN %s`，
  而 `PARSE_READY_RUN_STATUSES = ("succeeded", "parsed", "failed")`
  （`workers/output_parser/parser.py:37`）——**`published` 不在其中**。
- 重算检测的目标人群恰恰就是 published run（#1781 的 blocked 集合里 60 个是 published）。
  一个 published run 被重解析时，这条 UPDATE 匹配 0 行，走
  `_terminal_state_or_missing_row` 原样返回，**静默无写入**。
- 今天之所以能收敛，是因为收敛靠的**不是**这条 UPDATE，而是事实行：
  `upsert_river_timeseries` 先按 replacement window 做键控 `DELETE`
  （`:986-996`）再 `INSERT`（`:1026`），`created_at DEFAULT now()` 且**不在**
  `ON CONFLICT DO UPDATE SET` 列表里，所以重解析产生新的 `created_at`，
  `MAX(rt.created_at)` 前进。
- 若把 `parsed_at` 挂在状态门后面，published run 重解析后 `parsed_at` 永远陈旧 →
  `product_mtime > parsed_at` 恒成立 → 每 tick 重新入库 → **#1781 那个永久重投环
  换个机制重生**，而且 #1781 的 decline 机制只兜住被压缩块挡住的重算，兜不住能成功
  写入的那部分——那部分会安静地每 15 分钟重做一遍，rc 全绿。

**决定**：在 `mark_run_parsed` 内、状态 UPDATE **之前**，于同一事务发一条无条件写：

```sql
UPDATE hydro.hydro_run SET parsed_at = now() WHERE run_id = %s
```

不带 status 谓词。状态 UPDATE 逐字不变，因此 `published` 不被 downgrade
（must-preserve #3），而 `RETURNING *` 返回的行已带新 `parsed_at`。
run 行不存在时该语句匹配 0 行，与既有的 `DATABASE_ROW_MISSING` 路径不冲突。

选择"两条语句"而非"放宽 status 谓词"：放宽会把 `published` 打回 `parsed`，
在 display 面造成状态闪烁；选择"两条语句"而非"把 `parsed_at` 塞进
`_terminal_state_or_missing_row`"：那个 helper 是纯读，写入混进去会污染两个调用点
（`mark_run_parsed` 与 `mark_run_failed`——失败的 parse 绝不能盖时间戳）。

### D3 — 完整性判据新形状

```sql
SELECT h.run_id, h.init_state_id, h.parsed_at
FROM hydro.hydro_run h
WHERE h.run_id = ANY(%s)
  AND h.status IN ('parsed', 'published')
  AND (h.status = 'published' OR h.parsed_at IS NOT NULL)
```

聚合消失，`GROUP BY` / `HAVING` 随之消失（无聚合函数即无需 HAVING）。
`(h.status = 'published' OR h.parsed_at IS NOT NULL)` 逐字承载 must-preserve #1。
消费端 `_ingested_run_is_current`（`:1170-1188`）签名与语义不变——它拿到的仍是
`parsed_at: datetime | None`。

### D4 — 回填形态（纠正 issue body 的一处失实）

issue 建议回填可用 PR #1777 验证过的 `rt.run_id = ANY(...)` 下推辅助形态。
**不采纳**：该辅助会把"存储 run_id 与当前绑定不同"的漂移队列的事实行一并剪掉，
使这批 run 回填出 NULL。而在 master **今天**，key-only join 给这批 run 的是一个真实
时间戳。issue body 声称漂移队列"只能得到 NULL"——那只在它推荐的辅助形态下成立；
用辅助形态回填等于把今天没有的退化引进来。

**决定**：一次全表单程聚合，按 `run_key` 归并：

```sql
CREATE TEMP TABLE _parsed_at_backfill AS
SELECT run_key, MAX(created_at) AS parsed_at
FROM hydro.river_timeseries
WHERE run_key IS NOT NULL
GROUP BY run_key;

UPDATE hydro.hydro_run h
SET parsed_at = b.parsed_at
FROM _parsed_at_backfill b
WHERE h.run_key = b.run_key AND h.parsed_at IS NULL;
```

- 一次顺序解压，正是生产今天**每 tick** 付的那份代价，只付一次。
- 与 master 判据同口径（同为 `rt.run_key = h.run_key`），漂移队列不退化。
- `AND h.parsed_at IS NULL` 使其幂等、只填不覆盖，可安全重跑。
- 拿不到时间戳的队列仍得 NULL——与今天完全一致（must-preserve #5）。
  **实测口径订正（node-27，2026-08-23 回填 receipt）**：`hydro_run` 侧 `run_key IS NULL`
  的行数是 **0**，所以"NULL-key legacy 队列"在权威表这一侧是空的。真实残差是
  **1630 个 published run**（占 published 3223 的 50.6%）的事实行按 `run_key` 在
  `river_timeseries` 里聚合不出任何行——retention 丢块或从未写入。规模不是"小 legacy
  队列"，是一半。但它是 **parity 而非回归**，且是构造性的：回填是全表 `GROUP BY run_key`
  单程聚合、覆盖事实表全部行，master 用同一个键的 LEFT JOIN + `MAX(created_at)`
  对这批同样得 NULL，新旧判据的退化程度逐字相同。
- **需要第二次小回填**：迁移/回填与拉代码之间被解析的 run，在旧代码下不写
  `parsed_at`。拉代码后重跑同一脚本（幂等）。
  **但第二次回填只收口"窗内首次解析"的那部分**（`parsed_at` 仍为 NULL）。
  已被第一次回填打上时间戳、又在窗内被旧代码**重**解析的 run，其
  `parsed_at` 仍陈旧：脚本的 `AND h.parsed_at IS NULL` 是 fill-only 契约
  （防止把 parser 写的时间戳倒退），会跳过它们。代价有界且已知：
  该 run 会多触发**一次** forcing handoff——成功则新代码无条件 stamp、下一 tick 收敛；
  被压缩块挡住则落一条 #1781 终态 decline（一条误报的 decline 记录，非重投环）。
  **不得**改成 `GREATEST(h.parsed_at, b.parsed_at)` 去"修"它：那会毁掉
  fill-only 契约、每次重跑改写全部已 stamp 行。
  运维上真正清零这个子情形的办法是 D5 的 timer 暂停。

### D5 — 部署顺序是**硬**约束

`_already_ingested_runs` 的完整性 `cur.execute`（`scripts/node27_autopipeline.py:1133`）
**没有** savepoint 保护——只有 `_declined_runs` 的读被保护（#1781 D6）。先拉代码后迁移
会让 `UndefinedColumn` 直接从 `_already_ingested_runs` 抛出，整个 tick 死掉。

顺序：**暂停 autopipe timer → 迁移 → 回填 → 拉代码 → 第二次幂等回填 → 恢复 timer**。
回填在拉代码前完成，顺带消掉"新列存在但全为 NULL"的检测空窗。

暂停 timer 有两个理由，都不是可选的润色：
(1) 回填是一次全表顺序扫描，与 tick 自己那条同样重的聚合并发会互相拖垮；
(2) 它使 D4 记录的"窗内被旧代码重解析的已 stamp run"子情形为**空**——
没有 tick 在跑，就没有窗内重解析。

（与 #1781 相反：那单的顺序是软的，PR body 声称硬约束吃了一个 P1。本单是硬的，
PR body 从初稿起就必须这么写，并给出上面这条"无 savepoint"的具体依据。）

### D6 — Oracle 改造

- `tests/test_river_ts_text_identity_cleanup.py:856-861` `_autopipeline_statement()`
  断言 `_already_ingested_runs` 内恰好一条 `river_timeseries` 语句；删 join 后为零，
  helper 自身抛错。改为**负向钉**：断言该函数体内 `hydro.river_timeseries` 出现 0 次。
  `test_autopipeline_ingest_criterion_joins_by_key_with_no_aid`（:865）随之改写为
  "该函数不得触碰 fact 表"；
  `test_autopipeline_ingest_criterion_is_authority_state_first`（:872）保留其
  `updated_at` 负向断言，并新增对 `h.status = 'published' OR h.parsed_at IS NOT NULL`
  的正向钉。
- `RIVER_TABLE_CENSUS`（:164-174）`"scripts/node27_autopipeline.py": 2` → `1`，
  并按 :1392-1398 自身的规则更新 register。
- `scripts/select_ci_tests.py:1030-1046` 的映射与 `tests/test_select_ci_tests.py` 的
  **等值**断言同步核对。
- **新增** oracle（D2 的钉子）：`parsed_at` 由成功 parse 写入且与状态门无关；
  register upsert 与 publish 均不得写该列。

### D7 — 兄弟探针只分诊

`_publish_display_runs`（`:1361-1366`）与
`services/tile_publisher/forcing_copyback_backfill.py` 的 key-only 探针不改，
给出 node-27 实机 EXPLAIN 分诊结论；同病则由 `issue-scribe` 另单记账。
#1686 备选 (c) 的告警继续成立：#1342 落地重压缩时必须保证键面在压缩侧有访问路径。

### D8 — 实机取证方法（沿用 #1781 的教训）

- **tick 归属**：本单不引入任何能区分新旧代码的结构性汇总字段，因此归属只能用
  进程启动时间对比拉代码时间（`ps -o lstart`），不得靠 summary 字段猜。
- **回填是本单唯一的重生产写操作**：`{ setsid nohup ... & }` 分离 + 日志落文件 +
  分批 `statement_timeout` + **有人看守**。（ADR 0003 记着的 900 s 无人值守
  `EXPLAIN` 正是这个形状的错误。）
- EXPLAIN 前后对比与 no-op tick 计时均为只读、廉价。

## Seams Under Test

- `workers/output_parser/parser.py::DbOutputParserRepository.mark_run_parsed` ——
  写入点，可在 fake/真实 repository 两侧观测。
- `scripts/node27_autopipeline.py::_already_ingested_runs` —— 判据，语句可被
  `_sql_constants` 静态抽取（现有 oracle 机制）。
- `scripts/node27_autopipeline.py::_ingested_run_is_current` —— 消费端，签名不变，
  既有单测继续覆盖。
- 一次性回填脚本 —— 独立入口，幂等，可对空库/有数据两态跑。

## Open Residuals（交付时必须仍然成立或被记账）

- 拿不到时间戳的队列 `parsed_at` 为 NULL，重算检测退化为 init-state 比较——
  与今天一致，继续记账，不新增退化。实测规模见上：published 3223 中 1630 个
  （非 NULL-key 成因，而是事实行不可聚合）。
- **回填 receipt 的字段口径会误导**：`unstamped_null_key_after=0` 读起来像"无残差"，
  真实残差是 `unstamped_after`（pass1 后为 2729，其中 published 1630）。
  E6 一律按 `unstamped_after` 加 status 拆分报，不引用 `unstamped_null_key_*` 单独作结论。
- 迁移/回填与拉代码之间的解析窗口：第二次幂等回填只收口窗内**首次**解析的 run；
  窗内被旧代码**重**解析的已 stamp run 保留陈旧 `parsed_at`，代价为一次多余 handoff
  或一条误报 decline（D4）。D5 的 timer 暂停使该子情形为空。
