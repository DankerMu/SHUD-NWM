# Design: frontier-chunk-stats-analyze-guard

## 风险三元组

- 级别：**standard**（ops 脚本 + 文档；无 API 契约、无迁移、无前端）。
  Issue #1378 为 needs-triage 自裁定——诊断轮已完成，修法即本 change。
- must-preserve：autopipeline tick 的现有 summary schema 消费者（`node27_autopipe_cron.sh`
  只透传 JSON；无 schema 校验器）；压缩 receipt 的
  `schemas/timeseries_compression_receipt.schema.json` 校验（closed schema，须 bump
  版本——见 D3）；两个 timer 的墙钟预算（D2/D3 各自给出最坏情形算术）。
- seams under test：`_analyze_frontier_chunks(database_url) -> dict`
  纯函数化查询+执行+自检；压缩 runner 内 `_analyze_after_compress(cursor, chunk) -> dict`。

## D1: 看护放在 autopipeline tick 末尾，而不是 output_parser 行写入路径

output_parser 每 run 写入一次；一个 tick 可能 ingest 多个 run。tick 末尾一次
判定 + ANALYZE 是幂等且最少执行次数的位置（phase 3 publish 之后，summary 之前）。
失败语义与 coverage refresh 一致：**看护失败不判 tick 失败**，tick rc 不变——
统计漂移是渐进病，下个 tick 重试。失败分两级（cross-review C2 修正）：单 chunk
`ANALYZE` 失败**逐 chunk 隔离**（try 在循环体内，条目记 `status:"failed"`+`error`，
继续尝试剩余 chunk——否则被压缩锁挡住或已消失的 chunk 会吞掉整批，且因失败不清
`n_mod_since_analyze` 而每 tick 置顶复发、饿死 frontier chunk）；guard 级失败
（连接/候选查询）记 `stats_guard.status = "failed"` + `error` 字符串（in-process
psycopg2 调用，无子进程 rc 可引用）。

## D2: 触发条件——机制匹配，不是体量阈值

成因机制是**新值不可见**：每个新 cycle 的 `run_id`/`run_key` 是该 chunk 统计里
不存在的值，planner 估行 ≈0 → 计划翻转。翻转由新 cycle 的**第一批行**触发，与
修改行数体量无关；且 `ANALYZE` 会把 `n_mod_since_analyze` 清零，任何大体量阈值都
会在每次看护后制造一段"新 cycle 已进、统计未见"的必然复发窗口（fixture 首审 P1）。
初稿的 `max(1_000_000, 2%)` 阈值依据也是误读——issue 里的 2,715,324 是坏计划下的
`Rows Removed by Filter`（执行器扫描浪费），不是漂移量实测；无任何证据表明翻转
只发生在高漂移区间。

- **触发**：本 tick `ingested >= 1` 且 chunk `n_mod_since_analyze >= 10_000`
  （模块常量 `STATS_GUARD_MIN_MODS`）。一个真实 run 写入行数 = 段数×时步 ≫ 10⁴，
  所以任何被本 tick ingest 触及的 chunk 必然过槛；下限的唯一作用是跳过仅有零星
  迟到写入、本 tick 未触及的 chunk。无 ingest 的 tick 不查询不执行。
- **目标集合**：`hydro.river_timeseries` + `met.forcing_station_timeseries` 的
  未压缩 chunk（同病同治；压缩 chunk 由 ride-along 覆盖）。
- **预算护栏**：每 tick 至多 ANALYZE `STATS_GUARD_MAX_CHUNKS = 3` 个 chunk（按
  `n_mod_since_analyze` 降序取前 3），被裁掉的 chunk 名单记入
  `stats_guard.deferred`（不允许静默截断；渐进病下个 tick 补上）。每条 ANALYZE
  前置 `SET statement_timeout = 120000`（实测 ~20 s/250M 行，08-19 三 chunk 64 s；
  6 倍余量）。最坏情形 3×120 s = 6 min < autopipe 10 min tick 墙钟；systemd timer
  在 service 仍在跑时跳过本次激活，不会叠加。
- **自检（PG15 非 owner 静默跳过防护）**：PG15 无 `MAINTAIN` 权限位，非 owner 执行
  `ANALYZE` 只发 WARNING 并"成功"返回。guard 在每条 ANALYZE 后回读
  `pg_stat_user_tables.last_analyze` 写入该 chunk 的 summary 条目；若未刷新，
  该条目记 `status: "warning"`（如实上报，不改 tick rc）。
- 环境开关：`NODE27_AUTOPIPE_STATS_GUARD=off` 可停用（与现有 autopipe 开关风格
  一致），默认开启。

## D3: 压缩 runner ride-along 的 receipt 形状

- **触发路径**：凡本次 run 的记账里**到达 compressed 状态**的 chunk 条目都 ANALYZE
  ——覆盖三条内部路径：正常成功、测量失败后成功、`compress_chunk` 抛错但事后对账
  确认 commit 已落地（lost-ack reconciled）。以最终逐 chunk 记账为准，不以
  `compress_chunk` 的返回值为准。
- **receipt 字段**：`{"analyze_seconds": <float>}` 并入该 chunk 条目；失败记
  `{"analyze_seconds": null, "analyze_error": "..."}`。
- **失败语义（收严到 outcome/rc 层）**：ANALYZE 失败 MUST NOT 置 `any_errors`、
  MUST NOT 改变 receipt 顶层 `outcome`（clean/partial 判定）、MUST NOT 改变进程
  返回码——压缩全部成功而 ride-along ANALYZE 失败的 run 仍是 `clean`/rc 0，
  否则每日压缩 systemd unit 会因搭车步骤误报红。
- **schema**：`$defs/selected_descriptor` 是 closed（`additionalProperties: false`），
  新字段会被拒——**无条件**扩 schema，并按 budget 字段先例
  （archive/2026-08-15-compression-receipt-budget-audit：2.0→2.1）bump
  `schema_version` 2.1→**2.2**、加 per-version `not:{required:[...]}` 分支使新字段
  仅在 2.2 合法、同步更新 `schemas/examples/timeseries_compression_receipt.example.json`。
- **执行时序**：全部压缩完成后再批量 ANALYZE、最后发布 receipt——压缩是主目的，
  绝不因搭车统计被牺牲；ANALYZE 结果写入同一 receipt。
- **超时与墙钟护栏**：最坏情形 `PER_TICK_BOUND(4) × 300 s = 1200 s`，而 leg-1
  不变式只保证 `ceil(compress_timeout_ms/1000)+60 ≤ wrapper_wall`（默认
  3660 ≤ 3900），富余段已留给 reconciliation/receipt 发布——无护栏时越墙 TERM 会
  以 rc 124 染红每日 unit，正是上面三条 MUST NOT 管不到的通道。因此每条 ANALYZE
  前先算剩余墙钟 `wrapper_wall_seconds - elapsed - 120 s`（120 s 为 receipt
  发布保留段）：不足 30 s 则跳过剩余 chunk，各记
  `{"analyze_seconds": null, "analyze_error": "wall_budget_exhausted"}`（不静默）；
  足够则 `SET statement_timeout = min(300 s, 剩余)`（300 s 为压缩态 chunk 的
  ANALYZE 上限，实测未压缩 ~20 s/250M 行）。超时/失败即 `analyze_error`，
  不影响压缩记账。该护栏保证 ANALYZE 批永不侵入发布保留段，rc 中立在
  `PER_TICK_BOUND=4` 默认下仍成立。

## D4: 为什么不是 autovacuum 参数 / 物化表

- TimescaleDB chunk 不继承 hypertable 的 reloptions，逐 chunk `ALTER TABLE SET`
  需要在每次 create_chunk 后追赶（另一个守护面），且参数在 DB 里不可 git 审计。
- 物化 valid-times 摘要表引入新写路径与一致性面；当前键形态 1.163 ms，
  病灶是统计不是查询形状——治 statistics，不动 schema（issue 备选方案显式弃选）。

## D5: 测试策略（无真实 TimescaleDB）

- autopipeline：mock psycopg2 connection，(i) ingested=0 时不触发；(ii) 过槛 chunk
  被 ANALYZE 且 summary 含清单与回读的 last_analyze；(iii) 超过 MAX_CHUNKS 时按
  n_mod 降序取前 3、其余进 deferred；(iv) ANALYZE 抛错时 tick rc 不变、summary 记
  `status:"failed"`；(v) last_analyze 未刷新时条目记 warning；(vi) 开关 off 时跳过。
- 压缩 runner：现有测试夹具内 (i) compress 成功后发出 `ANALYZE`；(ii) ANALYZE 失败
  不改变压缩记账、receipt `outcome` 与进程 rc；(iii) 2.2 receipt 含 analyze 字段且
  过 schema 校验（含 example 文件与 live_evidence 测试的 schema 加载路径）。
- 真实 DB 验证在 node-27（Evidence Floor E4，**硬门**）：一个真实**触发了 guard**
  的 tick 的 summary 含 stats_guard 块；`pg_stat_user_tables.last_analyze` 实刷；
  guard 后重跑 issue 验收项 2 的 Q2（当前键形态）EXPLAIN ANALYZE——
  Execution Time < 50 ms、被执行节点无百万级 `Rows Removed by Filter`、
  计划走 selected-identity 键索引；三样都进 receipt。

## 残余风险（记录，不在本 change 解决）

`default_statistics_target = 100` 下，若 7 天 chunk 内 distinct run 数超过 MCV
列表容量，最新 run 落入 uniform-remainder 估计而非 MCV 命中——仍远好于 ≈0，
E4 的计划捕获就是对"ANALYZE 后计划确实回到键索引"的实证闭环；若未来翻转复发
且 E4 式复核显示统计已新鲜，升级手段是列级 `ALTER TABLE ... SET STATISTICS`。
