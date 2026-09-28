# Tasks

## Risk packs

- [x] Schema / columns / units / field names — selected：索引集合变化（删 3 建 1）；→ 1.1、1.2、1.4、3.2。
- [x] Resource limits / large input / discovery — selected：D11 5000 buffer 门、`hydro_run` 线性增长、空结果 pin 探针成本；→ 2.3、3.3、3.4、4.3。
- [x] Error handling / rollback / partial outputs — selected：CONCURRENTLY 失败留下 INVALID 索引，重跑必须安全；→ 1.3、3.2。
- [x] Legacy compatibility / examples — selected：I1 结果不变、保留索引名被代码/测试/receipt 引用、已应用迁移不改；→ 1.4、2.2、3.4。
- [x] Documentation / migration notes — selected：迁移头注释、autopipeline docstring、语义前提与契约归属写入 design；→ 1.1、1.5、5.1。
- [x] Concurrency / shared state / ordering — selected：生产 apply 与在线写入并存（CONCURRENTLY、DROP/CREATE 间隙）；→ 1.1 注释、4.x 生产 apply 窗口。
- [ ] Public API / CLI / script entry — not selected：API 响应形状不变（由 2.2 等价证明）。
- [ ] Config / project setup — not selected：无配置变化。
- [ ] File IO / path safety / overwrite — not selected：不涉及。
- [ ] Auth / permissions / secrets — not selected：不改角色授权；只读探针使用既有 `nhms_display_ro`。
- [ ] Release / packaging / dependency compatibility — not selected：无依赖变化。

## 1. Migration 000065（#2634 + #2626）

- [x] 1.1 新建 `db/migrations/000065_*.sql`：头注释写明去重理由、保留/删除名单与依据、CONCURRENTLY + autocommit、失败重跑语义；三条 `DROP INDEX CONCURRENTLY IF EXISTS`；新索引 DROP-then-CREATE CONCURRENTLY，无 status 谓词。
- [x] 1.2 node-27 scratch DB（同集群、拷贝 `hydro_run`）上对 `cand` CTE 做列序/INCLUDE 候选 EXPLAIN 对比，取 shared hit 最小者；对比表写进 design.md D2，并确认新索引名在生产不存在。
- [x] 1.3 real-DB 测试：fresh 从零迁移后 `hydro_run` 索引集合 = 期望集合；在人为制造的 INVALID 新索引残留上重跑 000065 → 结果等于干净运行。
- [x] 1.3a `tests/test_schema_ledger_convergence_integration.py` 按 design D1"测试面"改写（索引集合、`broken` 名、000062→000065 整段重放断言终态、新索引 INVALID 残留重跑用例；既有 000063 INVALID 重跑用例改为同时删 000063/000065 账本行后顺序重放）；node-27 real-DB 通过。
- [x] 1.4 `tests/test_migrations.py`：账本清单加入 000065；000063 的六索引 pin 保持（000063 文件不变）；新增 000065 静态 pin（DROP 名单、DROP-before-CREATE、CONCURRENTLY、谓词无 status）；断言已应用迁移文件未改。
- [x] 1.5 `scripts/node27_autopipeline.py:1426-1429` docstring 更新为只提保留的索引。

## 2. 有界探针（#2630）

- [x] 2.1 `_per_source_latest_cycles`：`cand` 带 `h.end_time`，`o` 透传；探针改 `CROSS JOIN LATERAL (... LIMIT 1)`；加 `rt.valid_time >= o.cycle_time AND rt.valid_time <= o.end_time`；#2451 C1 拼写不变；docstring 写明语义前提与证据位置。
- [x] 2.2 形状 pin：`tests/test_latest_cycle_discovery_shape.py`（`fact_probe()`、围栏字符串、注册模板用例）与 river_ts 模板 golden（`tests/test_river_ts_template_golden.py`、`tests/test_river_ts_text_identity_cleanup.py`、`tests/test_river_ts_read_path_surrogate_keys_integration.py` 中相关处）更新，钉住 LATERAL、`LIMIT 1`、两条时间谓词、`OFFSET 0` 围栏与外层 `ORDER BY … LIMIT 1`；新 pin 对 master 源码变红。
- [x] 2.3 行为测试：既有 forecast_series latest 测试全绿；新增（或扩展既有）用例覆盖"候选的行落在窗口内被选中"与"窗口外的行不产生命中"。

## 3. node-27 验证

- [x] 3.1 disposable DB pytest（同集群 scratch DB，禁止生产 `nhms`）：`tests/test_migrations.py`、相关 integration 文件、`tests/test_real_database_integration.py`；先 `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`。
- [x] 3.2 000065 重跑安全在 real DB 上实测（1.3 的 integration 用例在 node-27 通过）。
- [x] 3.3 440-pin 等价回归（另附一份非空 pin 的有界 LATERAL `EXPLAIN (ANALYZE, BUFFERS)`，记录每个 chunk 的索引与 buffers）（生产只读 `nhms_display_ro`，master checkout 对 branch worktree）：0 mismatch；空结果 pin shared hit max ≤ 5000；非空 pin max/p95 ≤ 3301/2530。harness 与输出进 `receipts/`。
- [ ] 3.4 `display_ready_run`、QHH latest-product、display-coverage 选择器在删除重复索引后仍走索引：apply 前在 node-27 scratch DB（从零迁移含 000065）上 EXPLAIN 确认走保留索引（生产上不得 DROP/ROLLBACK 取证，会拿 ACCESS EXCLUSIVE 锁）；apply 后在生产只读复核。

## 4. CI 与生产 apply

- [ ] 4.1 CI 绿，含 SQL Migration Dry Run（非 draft PR）。
- [ ] 4.2 **停：向用户呈报** 000065 SQL、删除/保留依据、预计耗时、重跑/回退方案，取得生产 apply 确认。
- [ ] 4.3 生产 apply 后 receipt：`pg_indexes`（新索引 `indexdef` 与迁移一致、3 个重复索引消失、`indisvalid`）；D1 语句 IFS pin 与 GFS+IFS pin `EXPLAIN (ANALYZE, BUFFERS)`：`cand` 走新索引，shared hit 相对 1386/1464 不回退；3.4 复核。

## 5. 文档与 follow-up

- [ ] 5.1 design.md 记录 1.2 对比表与 4.3 结果。
- [x] 5.2 follow-up issue：ingest 侧强制 `valid_time ∈ [cycle_time, end_time]`（D1 有界探针依赖）→ #2687（另注：parser 的绝对时间自动识别允许首行早于 cycle_time 至多 1 天，当前数据实测 0 违例）。

## Evidence Floor

- [x] `uv run ruff check .`
- [x] `uv run pytest -q tests/test_migrations.py tests/test_latest_cycle_discovery_shape.py tests/test_river_ts_template_golden.py tests/test_river_ts_text_identity_cleanup.py tests/test_forecast_store_routing.py` + forecast store/api 相关测试
- [x] `openspec validate hydro-run-index-dedup-and-latest-cycle-bound --strict --no-interactive`
- [x] node-27 disposable DB pytest（3.1/3.2）
- [x] node-27 440-pin 等价回归 + D11 预算（3.3）
- [ ] CI SQL Migration Dry Run 绿（4.1）
- [ ] 生产 `pg_indexes` + EXPLAIN receipt（4.3，用户确认 apply 之后）
