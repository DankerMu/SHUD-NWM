# autopipe-completeness-authority-state（#1674）

## Why

#1442（PR #1655）把 `scripts/node27_autopipeline.py` 的 `_already_ingested_runs`
完整性判据从 `rt.run_id = h.run_id` 切成 `rt.run_key = h.run_key`。
`hydro.river_timeseries` 里 2026-07-23→08-13 三个 chunk 在 000051 回填战役前
已压缩，回填 runner 按设计跳过压缩块，其中的行 `run_key` 永久为 NULL（实测被阻
run 277,872 行全 NULL）。键连接对这些早已 `published` 的 run 永远命不中，于是
每 tick 把 544 个 7 月 run 判成"从未入库"，重发 forcing handoff，被
`met.forcing_station_timeseries` 压缩块写保护正确拦下——每 tick 544 次
`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`，tick 从 ~4 min rc=0 劣化为
~3.2 h rc=1（2026-08-20T23:10:40Z 起每 tick 如此，`already_ingested`
2006 → 1428）。

根因不是压缩块拦截（正确），也不是 NULL `run_key`（已记录的有期限排除契约），
而是判据把"run 是否完整"绑在"键过滤能否看见它的 fact 行"上——对 NULL-key 遗留
人口这两件事按契约本就不等价。

## What Changes

- `_already_ingested_runs`：完整性以权威状态为主——`status = 'published'` 的 run
  视为完整，不要求键可见的 fact 行；`status = 'parsed'` 仍要求 ≥1 行键匹配
  （dual-write 之后的 parsed run 必然有键行）。SQL 由 `JOIN` 改为
  `LEFT JOIN ... ON rt.run_key = h.run_key` + `HAVING`；`parsed_at` 仍是
  `MAX(rt.created_at)`——遗留 NULL-key run 上它为 NULL，重算检测退化为仅
  init_state 比对（有界残留，见 design D1）。
  **不引入任何文本 fact join**——#1442 规格"经 join 到达的身份不得携带文本
  fact join"与 `tests/test_river_ts_text_identity_cleanup.py` 的清零 oracle
  原样保留并继续绿。
- `_publish_display_runs` 的 key-only `EXISTS` **不改**：parsed→published 只作用于
  dual-write 之后写入的 run；遗留 NULL-key 人口 2026-08-21T10:0xZ 由 orchestrator
  以 display RO DSN 实测 `SELECT status, count(*) FROM hydro.hydro_run GROUP BY 1`
  = published 3058 / superseded 959 / **parsed 0**（receipt 见 #1674 评论），
  按已记录的收敛契约处置；tasks 0.1 要求实现前复测、E3 要求部署后再测。
- 真实 DB 回归测试：published run + 其 fact 行 `run_key IS NULL` 且位于已压缩
  chunk → 判完整；parsed + NULL-key 行 → 不完整；parsed + 键行 → 完整；
  published 但 fact 行为零 → 完整（语义：retention 已删的 published run 不回灌——
  这是相对 #1442 之前行为的**有意偏离**，PR body 单列）。
- 规格 delta：river-identity-normalization 出界消费者要求新增场景，钉住"自动流水线
  完整性判据对 NULL-key 遗留 run 以权威状态判完整、不重试 handoff"。

## Non-goals

- (b) 压缩块内 `run_key` 解压回填：归 #1342 删列前置 runbook，不在本单。
- (c) 把 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 终态化：D1 落地后风暴源消失，
  YAGNI；若 receipt 仍见基线 12/tick 级别的该错误，另单表征。
- `met`/`hydro` 压缩块写保护：行为正确，不放宽。
- #1446（coverage refresh 对 legacy NULL-key run 的覆盖问题）：同族不同打击面。

## Impact

- 代码：`scripts/node27_autopipeline.py`（一个函数的 SQL 与 docstring）。
- 测试：新增真实 DB integration 用例 + SQL 形态 pin；既有清零 oracle 不动。
- 运维：node-27 下一 tick 恢复 rc=0，新预报入库延迟回到分钟级。
  **更正（#1789，2026-08-23）**："下一 tick 即恢复 ~4 min" 是失实预期，未兑现。
  本单只让 `published` 不再依赖 fact 行可见性，并没有去掉判据里那条
  `LEFT JOIN hydro.river_timeseries` —— 它仍每 tick 为取一个
  `MAX(rt.created_at)` 整块解压全部压缩 chunk，`phase=ingest` 停在 ~590 s 且随
  压缩块增加单调恶化。~240 s 量级要到 #1789 把该 join 删掉、时间戳改由
  `hydro_run.parsed_at` 承载之后才可能恢复。
