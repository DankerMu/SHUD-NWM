# Proposal: river_timeseries 读路径代理键切换（issue #1341）

## Why

#1339 给 `hydro.river_timeseries` 加了 7 个代理列，#1340 让写路径双写零
NULL；但**所有读路径仍按文本身份列过滤**——文本索引是 #1338 实测 162 GB
量级的存储包袱（epic #1336 瘦身的最大头），且 #1378 已实证 valid_times
named-identity 分支存在 1583x 计划翻转。本 issue 是 epic M2 的读取侧：
把边界内读查询的 fact 表谓词切到整型代理键 + 新建整型索引，对外契约
逐字段等价（前端零感知），为 #1342（旧文本列/索引下线 + cutover）扫清
最后的前置。用户已裁定存量行覆盖走**回填路线**（issue #1341 评论在案）：
先把可回填 chunk 的 NULL 键磨到零，再一次性切读。

## What Changes

- **`db/migrations/000051`（新迁移，偏离记录见下）**：整型 discovery 索引
  `(run_key, basin_version_key, river_network_version_key, variable_e,
  valid_time DESC)`——现役文本 discovery 索引的键形等价物，服务切换后的
  全部边界内读形态（含 #1378 的 valid_times 分支）。裸 CREATE INDEX
  （hypertable 拒绝 CONCURRENTLY，000049 实测钉死），node-27 构建排
  cycle 间歇窗口。文本索引**全部保留**（边界外读者 + 回滚安全；删除
  归 #1342）。`tests/test_migrations.py` RETAINED 索引集合同步。
- **四个边界内读面**（issue In-Scope）fact 谓词切键、权威表 join 还原
  文本输出，响应逐字段等价；**同时保留 `run_id` /
  `river_network_version_id` / `variable` 三个冗余文本下推谓词**
  （与键谓词同一合取，压缩 chunk segmentby/orderby 下推辅助，
  round-1 评审 P1 的用户裁定补救，#1342 删列时一并移除，design D1）。
  **round-3 增补**：node-27 部署门 EXPLAIN 拦到 national 两腿 0.77s →
  34.7s 回归（集合 join 丢失逐段探针路径），两腿改 per-segment
  `CROSS JOIN LATERAL (... LIMIT 1)` 探针（实测 0.69s）；探针体内额外
  受批 `river_segment_id` 文本等值——位置性例外，仅限两个 LATERAL 体
  内，体外禁令不变，随 #1342 一并移除（design D1 / spec delta
  round-3 amendment；该扩面提请用户事后复核）：
  - `services/tiles/mvt.py`：hydro 图层 source CTE、hydro-national
    identity stats 探针与 **typed_values / untyped_ranked 两腿**
    （`typed_values` / `untyped_ranked`，UNION ALL 同源，必须同切，禁混
    文本/键谓词；round-3 后两腿改 per-segment `CROSS JOIN LATERAL`
    探针，见 design D1）、
    `valid_times_for_layer` named-identity 分支（#1378 病灶）与无具名
    分支（variable 谓词切 enum，fixture 复审补钉）。`feature_id` 拼接
    `rnv || '::' || segment` 字节不变。
  - `packages/common/display_coverage.py`：river 覆盖扫描按键 GROUP BY
    后 join 还原文本（join-and-reconstruct，非改名）。
  - `apps/api/routes/hydro_display.py`：存在性探针切键。
  - `services/production_closure/`：按实际读形态处置——**实测该目录无
    identity 谓词 fact 查询**（终审 P3 纠偏：原文"identity 谓词查询切键"
    是空集声称）；表级 deny-write 探针不动（列无关，记录）；
    `scale_validation.py` 静态 plan_lines 与新索引计划对齐（现引用
    已不存在的 `river_timeseries_run_valid_idx`，顺带修复在册陈债）。
- **node-27 运维前置（回填战役，用户裁定路线）**：先强制 stop 压缩
  timer（chunk 55 已入压缩候选窗，design D3 死线推导），再用 #1339
  runner 多次 `--enforce` 循环把 chunk 55（terminal，266.1M NULL）
  磨零，chunk 58/62（active，190.1M NULL）走 `--final-sweep`（ingest
  静默窗口），收敛后恢复 timer；收敛判据用**直连 SQL 每 chunk NULL
  COUNT**（#1408 三缺陷在案，不信 receipt totals）。
  压缩 chunk 32/51（595.8M 行全 NULL）不回填不解压，影响面如实记录：
  键读对 valid_time 07-23→08-06 的旧行不可见 ≤11 天，retention
  （21d，enforce，timer 在案）08-20/08-27 自然清除。

## Impact

- Affected specs: `river-identity-normalization`（ADDED requirement：读
  路径按代理键过滤 + 文本还原逐字段等价）。
- Affected code: `services/tiles/mvt.py`、`packages/common/
  display_coverage.py`、`apps/api/routes/hydro_display.py`、
  `services/production_closure/`、`db/migrations/000051`、
  `tests/test_migrations.py`、相关 unit/integration 测试。
- **偏离记录（PR Boundary 字面 vs In-Scope 实质）**：Boundary 写"不改
  迁移"，但 In-Scope 明列"复核索引需求：是否需新建整型索引替代文本
  索引"，且 AC-1"查询计划不退化"没有整型索引在物理上不可满足（键列
  000050 显式零索引，000050:79-84 把索引设计定向给本 issue）。按
  In-Scope 实质交付 000051，偏离进 PR body。
- Not affected（non-goals）：写路径与回填 runner 代码零触碰；
  `packages/common/forecast_store.py`（边界外最大读者，其静态
  `_qhh_latest_query_indexes()` 被 `tests/test_forecast_api.py` 逐字
  钉死，留文本读，payload 保持 TRUE）、`services/tile_publisher/`、
  `scripts/node27_autopipeline.py` 等边界外读者**不改**——随本 change
  由 issue-scribe 立跟踪单，作为 #1342 的 blocker；文本列/文本索引
  删除、cutover 函数执行、压缩 segmentby 切换（全部归 #1342）；
  OpenAPI/前端零变更（响应逐字段等价即前端零感知）。
