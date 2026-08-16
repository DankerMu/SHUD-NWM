# Tasks: river_timeseries 读路径代理键切换（issue #1341）

## 0. 实机前置（回填战役 + pre-flight，先于实现完成或并行推进）

- [x] 0.1 pre-flight（node-27 只读）：读出部署 env 实测 lag（172800
      已读，receipt 双确认），按实测值现场分类各 chunk
      active/terminal 并据此定每 chunk 的 flag（55 → `--enforce`；
      58/62 → `--final-sweep`）；读出压缩 timer 下一 tick 时刻与 55
      的候选状态（今日 receipt 已示边界跳过）；
      `has_table_privilege('nhms_display_ro', <hydro.hydro_run /
      core.basin_version / core.river_network_version /
      core.river_segment / core.model_instance>, 'SELECT')` 全真（缺则
      out-of-band GRANT 并记录 receipt）；每网络 selected latest run 是
      否含 NULL 键行（若有流域将因切键变暗 → 升级 merge 门决策项）；
      首跑 `--probe` 实测单批吞吐推算总时长
- [x] 0.2a **强制前置**：stop 压缩 timer（runbook §4.5 user-scope；
      起止时刻进 receipt）——design D3 死线推导：55 已入候选窗，
      08-17 12:25 CST tick 即可能压死 266.1M NULL 行
- [ ] 0.2b 回填战役：chunk 55 `--enforce` nohup 循环（terminal，不经
      静默闸，安全性=lag 判据+写守卫）；chunk 58/62 `--final-sweep`
      排 12h cycle 间歇窗口（安全闸=runner 每 chunk 写计数静默断言；
      不覆写 lag）；receipt 路径固定兼 resume cursor
- [ ] 0.2c 收敛核验后恢复压缩 timer（起止进 receipt）
- [ ] 0.3 收敛判据：直连 SQL 每 chunk NULL COUNT——可回填 chunk 集合
      全零（不采 receipt totals，#1408 在案）；等值审计（回填域定向
      SQL 口径）零背离
- [ ] 0.4 000051 在 node-27 应用：cycle 间歇窗口裸 CREATE INDEX，起止
      时刻/时长/索引大小进 receipt

## 1. 实现（implementer）

- [x] 1.1 `db/migrations/000051_river_ts_surrogate_key_read_index.sql`：
      整型 discovery 索引（design D2 形状），头注释含运维约束
      （CONCURRENTLY 被拒/SHARE 锁/窗口要求，照 000049 模式）；
      `tests/test_migrations.py` RETAINED_RIVER_TIMESERIES_INDEXES +1；
      **改钉** `test_selected_run_valid_time_discovery_migration_
      matches_strict_identity_predicates`（tests/test_migrations.py:
      354-383）：索引列元组断言改指 000051 的 `(run_key,
      basin_version_key, river_network_version_key, variable_e,
      valid_time DESC)`，源码谓词断言改为键解析子查询形态，并加
      negative pin——文本谓词 `run_id = :run_id` 等不再出现于
      `valid_times_for_layer` 切片（防回潮；禁止以删钉代改钉）
- [x] 1.2 `services/tiles/mvt.py`：hydro source CTE、hydro-national
      identity stats + **typed_values 与 untyped_ranked 两腿同切**
      （mvt.py:603-652，UNION ALL 禁混文本/键谓词，design D1 不变量）、
      valid_times named-identity 分支切键 + 无具名分支 variable 谓词切
      enum（design D1 处置）（D1 形态：InitPlan 键解析 + enum_range
      variable 谓词 + join 还原 + ORDER BY 落文本表达式 + feature_id
      拼接字节不变）
- [x] 1.3 `packages/common/display_coverage.py`：river 扫描 fact 侧四键
      GROUP BY + COUNT(DISTINCT river_segment_key)，join-and-reconstruct
      还原文本身份；scan_* 过滤参数语义不变
- [x] 1.4 `apps/api/routes/hydro_display.py`：存在性探针切键
- [x] 1.5 `services/production_closure/`：identity 谓词读查询切键；表级
      deny-write 探针不动（逐文件记录切/不切+理由）；
      `scale_validation.py` plan_lines 对齐新索引计划
- [x] 1.6 unit + integration 测试（design D5；含 NULL 键行不可见显式
      契约、OOV 空结果、红证配对、**同一 national 身份在 z<9 与 z>=9
      两分支对 NULL 键行可见性一致**（两腿同切回归））

## 2. 验证（Evidence Floor）

- [ ] 2.1 `uv run pytest -q` 定向全绿；`uv run ruff check .` 通过
- [ ] 2.2 `openspec validate river-ts-read-path-surrogate-keys --strict
      --no-interactive` 通过
- [ ] 2.3 diff 自证：写路径/回填 runner/forecast_store/tile_publisher/
      autopipeline 零触碰；OpenAPI 零变更；文本索引零删除
- [ ] 2.4 node-27：pre/post 快照逐字段等价（JSON 字节等；MVT 解码
      feature 集合等，采样 ≥2 流域 ×2 tile + national ≥2 zoom）（AC-2）
- [ ] 2.5 node-27：EXPLAIN (ANALYZE, BUFFERS) 四形态 before/after——走
      000051 索引、无 Seq Scan、latency 不退化；valid_times 形态对照
      #1378 基线（AC-1）
- [ ] 2.6 node-27：`/` 与 `/ops` 浏览器 e2e + MVT 渲染正常（AC-4）；
      deny-write 校验通过（AC-5）；定向真实 DB pytest（integration 子集）
- [x] 2.7 issue-scribe 立"边界外 river_timeseries 文本读者改造"跟踪单，
      标注为 #1342 blocker（forecast_store.py / tile_publisher /
      node27_autopipeline / seeds）——**#1442 已立**（附加发现：
      parser.py DELETE 谓词残留文本身份、compression live_evidence
      required_query_tokens 钉死 curve 查询文本，均记入该单）

## 3. 交付记录

- [ ] 3.1 PR body：偏离记录（000051 vs Boundary 字面、production_closure
      逐文件处置、MVT tile 等价口径为解码集合、压缩 chunk 32/51 影响面
      与 retention 收敛时间线）+ AC 逐条覆盖声明
- [ ] 3.2 PR body/评论：回填战役 receipt 摘要（per-chunk 计数前后、
      final-sweep 窗口）、000051 构建 receipt、快照/EXPLAIN 摘要

## Risk packs considered（core + domain）

- Public API / CLI / script entry: selected——display API/MVT wire 契约
  逐字段等价是本 change 的核心不变量（D1/D4）
- Config / project setup: not selected——零配置变更
- File IO / path safety / overwrite: not selected——无文件面
- Schema / columns / units / field names: selected——000051 迁移 +
  enum::text 还原 + RETAINED 索引集合（D2/D1）
- Auth / permissions / secrets: selected——nhms_display_ro 对权威表
  SELECT 权限 pre-flight（0.1）
- Concurrency / shared state / ordering: selected——回填 vs ingest、
  SHARE 锁构建窗口、retention timer 时间线（D3/D2）
- Resource limits / large input / discovery: selected——十亿行索引构建
  与回填批次墙（D2/D3）
- Legacy compatibility / examples: selected——NULL 键旧行不可见窗口 +
  文本索引保留回滚安全（D3）
- Error handling / rollback / partial outputs: selected——OOV/未知身份
  空结果契约 + revert 即回滚（D1）
- Release / packaging / dependency compatibility: not selected——零依赖
  变更
- Documentation / migration notes: selected——000051 头注释运维约束
- Geospatial / CRS: not selected——几何管线零触碰（join 键改，geom 源
  不变）
- Hydro-met time series / forcing windows: not selected——不动时序语义，
  只动谓词形态
- PostGIS / TimescaleDB domain behavior: selected——hypertable 索引/
  压缩 chunk/active chunk 语义贯穿 D2/D3
- Published NHMS artifacts / display identity: selected——display
  identity 逐字段等价即本 change 主契约
- 其余 domain packs（SHUD/Slurm/providers/manifest）: not selected——
  零触碰

Seams under test: display HTTP 端点响应、MVT tile 解码集合、
display_coverage 计算结果、production_closure 校验通过性（上游
In-Scope 四面即席位，最少最高）。
