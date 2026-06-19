## 0. Fixture 准备 + 实施前 explorer 验证（不出 PR，本地与 node-27 操作）

- [ ] 0.1 从 node-27 `/home/nwm/NWM/data/Basins/qhh/input/qhh/gis/` 抽 minimal `river.shp` + sidecars（`.dbf`/`.shx`/`.prj`）的子集（任选 5 条 reach，含至少一条 `Down=0` 终端），存入仓库 `tests/fixtures/basins/qhh-sample/gis/river.shp` 等
- [ ] 0.2 同样抽 `seg.shp` 子集（覆盖到 0.1 选的 5 条 reach 对应的全部 seg.shp records，含至少一条 multi-part record 用作 invariant fail-fast 反例），存入 `tests/fixtures/basins/qhh-sample/gis/seg.shp` 等
- [ ] 0.3 抽 `qhh.sp.riv` 头 5 行 + 对应的 `qhh.sp.rivseg` 行，存入 `tests/fixtures/basins/qhh-sample/`
- [ ] 0.4 在仓库 `tests/fixtures/basins/qhh-sample/README.md` 记录抽样规则（含每个 Index 的来源 + 抽样日期 + 来源 SHUD 包基准日期）；CLAUDE.md 已声明 `data/Basins/` 不 sync，但 `tests/fixtures/` 走 git，所以 minimal subset 必须 commit
- [ ] 0.5 用 explorer 验 OQ2：`apps/frontend/src/stores/overviewData.ts` 与 popup/hover 组件是否真的按 `segment_id` 命中，还是已按 `reach_id` 聚合；产出一句话结论 + 文件:行引用，决定 D2 / 任务 1.x 是否需要完整 crosswalk 写入，还是只写占位
- [ ] 0.6 用 explorer 验 OQ1：`workers/model_registry/basins_registry_import.py` 是否已有"按 basin 名重 ingest"CLI；若无，任务 3.1 需新加一个简单 subcommand
- [ ] 0.7 用 explorer 验 OQ3：grep `tiles/river-network`、`MVT` 路由消费者是否真的有前端 / 测试调用，决定是否在本 change scope 内同步处理

## 1. Crosswalk parser + writer 函数（PR 1：纯代码准备，**不接入生产 ingestion 路径**，避免 FK 早爆）

> **PR 1 边界硬约束**：本 PR 只新增"可调用的纯函数"+ 用 fixture 跑的单测，**不在 production ingestion 入口（`workers/model_registry/basins_registry_import.py` 的主流程）调用这些新函数**。生产路径的几何源、ID 命名、crosswalk 写入全部在 PR 2 原子切换。这样可避免 PR 1 单跑时新 crosswalk 行（用 `_reach_<iRiv:06d>` 格式 `river_segment_id`）与 `core.river_segment` 内尚存的旧格式 segment ID 在 `core.river_segment_crosswalk_river_segment_id_river_network_version_id_fkey` ([db/migrations/000004_core.sql:64-65](../../../db/migrations/000004_core.sql:64)) 上的 FK 冲突。

- [ ] 1.1 在 `workers/model_registry/basins_geometry.py` 新增 `parse_seg_shp_crosswalk(layer) -> list[CrosswalkRow]` 函数，从 `gis/seg.shp` 提取 `(iRiv, iEle, segment_order, length_m)` 行清单（不参与几何路径，仅做属性提取）；保留现有 seg.shp 几何路径不动
- [ ] 1.2 在 `workers/model_registry/basins_registry_import.py` 新增 `_build_river_segment_crosswalk_rows(model_id, river_network_version_id, segments)` **纯构造函数**：把 segments 转成 `[{river_network_version_id, river_segment_id=f"{model_id}_reach_{iRiv:06d}", source='basins_seg_shp', external_id=f"{iRiv}:{iEle}", properties_json=...}]`；**不调用** `create_crosswalk_entries`、**不写 DB**。生产 ingestion 主路径（`import_basin_package` 或同等入口）保持调用旧逻辑，本 PR 不接入新构造函数
- [ ] 1.3 **不新增任何 migration**：`core.river_segment_crosswalk` schema 已在 [db/migrations/000004_core.sql:56-69](../../../db/migrations/000004_core.sql:56) 定义；`river_segment_crosswalk_lookup_idx (river_network_version_id, source, river_segment_id)` 已存在且覆盖未来 PR 2 接入后的查询
- [ ] 1.4 单元测试：`tests/test_basins_registry_import.py` 加 case `test_parse_seg_shp_crosswalk_extracts_all_records`（用 qhh-sample fixture，断言行数 = seg.shp record 数 + 字段顺序）
- [ ] 1.5 单元测试：加 case `test_build_crosswalk_rows_format`，断言构造函数输出每行 `source='basins_seg_shp'` + `external_id` 格式 `"<iRiv>:<iEle>"` + `properties_json` 含 `iRiv`/`iEle`/`segment_order`/`length_m`
- [ ] 1.6 单元测试：加 case `test_build_crosswalk_rows_reach_missing_reports_set`，构造 seg.shp 含一条 `iRiv` 不在 reach 列表中的 record，断言构造函数返回（或 raise）含 `BASINS_REGISTRY_CROSSWALK_REACH_MISSING` 信息（**PR 1 不真写**所以是 raise 在构造期；PR 2 接入后变成 ingestion-time fail-fast）
- [ ] 1.7 node-22 上跑 `uv run pytest -q tests/test_basins_registry_import.py -k "crosswalk or seg_shp"`，全绿
- [ ] 1.8 ruff + openspec validate 全绿；**因生产 ingestion 路径未接入**，PR 1 单跑时 `core.river_segment_crosswalk` 行为不变（仍由旧路径写或不写）；PR 1 单跑时也不会触发 FK 冲突

## 2. 几何源切换 + invariant fail-fast + ID 重命名 + crosswalk 接入生产路径（PR 2：原子切换）

> **PR 2 边界硬约束**：本 PR 是**单一原子事务**——`core.river_segment` 行从 `<model>_seg_*` 改成 `<model>_reach_<iRiv:06d>` + `core.river_segment_crosswalk` 同一事务内插入新 `_reach_*` ID 引用的 crosswalk 行，**两者要么同时进 DB，要么同时回滚**。PR 1 准备的纯函数 (`parse_seg_shp_crosswalk` + `_build_river_segment_crosswalk_rows`) 在本 PR 内被 ingestion 主入口调用并接 `create_crosswalk_entries`。

- [ ] 2.1 在 `workers/model_registry/basins_geometry.py` 的 `parse_basins_geometry` 改 layer 优先级：始终先用 `river_layer`（删 seg → river fallback 分支 [basins_geometry.py:162-189](../../../workers/model_registry/basins_geometry.py:162)）；保留 `seg_layer` 仅供 crosswalk 用
- [ ] 2.2 新增 `_validate_river_shp_single_part_invariant(layer, sp_riv_count, required_fields)` 函数：扫描 river.shp 全部 records，若任一 multi-part、part 内顶点 < 2、record 数 ≠ sp_riv reach 数、或缺失任一 required field（`Index`/`Down`/`Type`/`Slope`/`Length`/`BC`/`Depth`/`BankSlope`/`Width`/`Sinuosity`/`Manning`/`Cwr`/`KsatH`/`BedThick`），立即 raise `BasinsGeometryError("BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED", payload={...offending Index, part_count, missing_fields})`
- [ ] 2.3 新增 `_validate_required_files_present(input_dir)` 函数：检查 `gis/river.shp` + `.dbf` + `.shx` 和 `gis/seg.shp` + `.dbf` + `.shx` 存在；缺 river 抛 `BASINS_REGISTRY_RIVER_SHP_MISSING`，缺 seg 抛 `BASINS_REGISTRY_SEG_SHP_MISSING`
- [ ] 2.4 `_river_segments_from_layer` 简化为单 part LineString 提取器：每 record 一条 reach polyline，字段从 river.shp dbf 读 Down/Type/Slope/Length/BC/Depth/BankSlope/Width/Sinuosity/Manning/Cwr/KsatH/BedThick 进 `properties_json`
- [ ] 2.5 `_mapped_downstream_segment_id` 用 `Down` reach Index 解析下游：`Down=0/-1` → null + `terminal_reach=true` flag；其它 → `<model_id>_reach_<Down:06d>`（zero-padded）
- [ ] 2.6 ID 命名：所有 `river_segment_id` 改成 `<model_id>_reach_<iRiv:06d>` 格式（zero-padded 6 位）；删旧 `<model>_seg_<segment_order>_ord_<iRiv>_rec_<iEle>` 命名分支
- [ ] 2.7 写路径 `packages/common/model_registry.py`：`PsycopgModelRegistryStore` 内 reach geom 写入改回 `geometry_to_wkt(..., "LineString")` + SQL 侧 `ST_Multi(ST_GeomFromText(?, 4490))` 包成单 part MultiLineString（仍兼容 `MultiLineString(4490)` 列）。`line_or_multiline_to_wkt` 暂保留（PR 5a 才删，避免本 PR 跨太多文件）
- [ ] 2.8 每 basin ingestion 路径放入单一事务（`BEGIN`/`COMMIT`/`ROLLBACK`），失败时该 basin 的 river_segment + crosswalk 全部回滚；不同 basin 之间相互隔离
- [ ] 2.8a 在 ingestion 主入口（`import_basin_package` 或同等）接入 PR 1 准备的 `_build_river_segment_crosswalk_rows` + `create_crosswalk_entries`：**同一事务内**先 insert/upsert `core.river_segment`（含新 `_reach_*` IDs），再 insert/upsert `core.river_segment_crosswalk`（引用同一 IDs），保证 FK ([000004_core.sql:64-65](../../../db/migrations/000004_core.sql:64)) 永远满足
- [ ] 2.8b 如果旧 `<model>_seg_*` 行在同 basin_version_id 下已存在（再 ingest 场景），先 `DELETE FROM core.river_segment_crosswalk WHERE river_segment_id LIKE '<old_model_id>_seg_%'` + `DELETE FROM core.river_segment WHERE river_segment_id LIKE '<old_model_id>_seg_%'`（同事务内），再插入新 `_reach_*` 行；这样旧 segment-level FK 链不会孤儿化
- [ ] 2.9 单元测试 happy path：`test_reach_count_matches_sp_riv`（qhh-sample，断言 reach 行数 = 5）
- [ ] 2.10 单元测试 invariant：`test_river_shp_invariant_fail_fast_on_multipart`（构造 multi-part record）+ `test_river_shp_invariant_fail_fast_on_missing_field`（构造缺 `BankSlope` field）
- [ ] 2.11 单元测试 ID：`test_reach_ids_are_zero_padded`（Index=1 → `_reach_000001`）+ `test_downstream_id_resolves`（Down=2 → `_reach_000002`，Down=0 → null+`terminal_reach=true`）
- [ ] 2.12 单元测试 invariant geom：`test_reach_geom_no_cross_gap_bridges`（用 qhh-sample 真实 fixture 钉死阈值 `max(300m, 4×median_edge)`）
- [ ] 2.13 单元测试 缺文件：`test_river_shp_missing_fails_fast` + `test_seg_shp_missing_fails_fast`
- [ ] 2.14 单元测试 事务：`test_per_basin_ingest_is_transactional`（构造 seg.shp 写入时抛错，断言同 basin 的 river_segment 写入回滚）
- [ ] 2.14a 集成测试 FK 顺序：`test_river_segment_and_crosswalk_atomic_fk_order`（用 node-22 真实 DB，断言同事务内先写 reach 行再写 crosswalk 行，FK constraint 永远满足；反向写顺序则 FK 报错）
- [ ] 2.14b 集成测试 reingest 替换：`test_re_ingest_replaces_legacy_seg_ids`（构造 basin 含旧 `<model>_seg_*` 行，跑 PR 2 ingestion，断言事务后 `core.river_segment` 行全是 `_reach_*` 格式 + 旧 crosswalk 行全部清除 + 无 FK 孤儿）

### 2c. API segment-slice view (Path C, D7) — 与 PR 2 atomic switch 同 PR

- [ ] 2c.1 在 `apps/api/routes/models.py`（或承载 `/api/v1/basin-versions/{id}/river-segments` 的实际 router）的 endpoint handler 内新增"按 length proportion 切 reach polyline"路径：从 `core.river_segment` 拉 reach geom + 从 `core.river_segment_crosswalk` 拉同 reach 的全部 segment 元数据（`iRiv`, `iEle`, `segment_order`, `length_m`），算每 segment 的 cumulative `(start_fraction, end_fraction)`（按 `segment_order` 累加 `length_m / sum(length_m)`），调用 PostGIS `ST_LineSubstring(reach_geom, start_fraction, end_fraction)` 切片
- [ ] 2c.2 最后一个 segment 的 `end_fraction` 强制 saturate 到 `1.0`，补偿 `sum(sp.rivseg.Length)` 与 reach `Length` 的浮点误差
- [ ] 2c.3 segment-level `river_segment_id` 衍生自 crosswalk `external_id`：`<model_id>_seg_<iRiv>_<iEle>`（保留前端契约 [M11MapLibreSurface.tsx promoteId='river_segment_id'](../../../apps/frontend/src/components/map/M11MapLibreSurface.tsx:987)）；DB 行的 `<model_id>_reach_<iRiv:06d>` ID 不暴露
- [ ] 2c.4 检测 `sp.rivseg` segment_order 是否与 reach polyline 流向一致；若不一致 fail-fast `BASINS_REGISTRY_SEGMENT_ORDER_MISMATCH`（实际中 SHUD 模型通常 flow-ordered，但留 invariant）
- [ ] 2c.5 单测 `test_segment_slice_count_matches_sp_rivseg`：用 qhh-sample fixture (5 reach, 18 segment) 跑 API endpoint，断言返回 18 features
- [ ] 2c.6 单测 `test_segment_slice_geometry_is_subset_of_reach`：用 PostGIS `ST_Within(slice_geom, ST_Buffer(reach_geom, 1e-9))` 断言每个 slice 几何严格在 reach polyline 上
- [ ] 2c.7 单测 `test_segment_slice_last_endpoint_saturates_to_reach_terminus`：构造 sum(length) < reach Length 的 fixture，断言最后 segment 终点 = reach 终点
- [ ] 2c.8 集成测试 `test_segment_slice_river_segment_id_preserves_frontend_contract`：断言 API 返回的 `river_segment_id` 全部匹配 `<model>_seg_<iRiv>_<iEle>` 格式 + 前端 hover/popup 路径仍可命中
- [ ] 2c.9 性能 sanity check：qhh basin (3738 segments) endpoint p95 < 500ms（与当前 baseline 同量级）
- [ ] 2.15 node-22 真实 DB pytest oracle 通过；qhh basin reach 行数从 3738 → 1633；任意 reach geom 内最大边长 ≤ `max(300m, 4× median_edge)`
- [ ] 2.16 ruff + openspec validate 全绿

## 3. 重 ingest 10 个 basin + 产 receipt（PR 3：node-22 数据迁移）

- [ ] 3.1 在 `workers/model_registry/basins_registry_import.py` 加 / 确认 `reingest_basin(basin_name)` CLI subcommand（OQ1 答案为"无"时新加；为"有"时复用现成）；跑通"按 basin 名找 SHUD 模型包 + 完整重 ingest 一遍"
- [ ] 3.2 在 `scripts/` 新增 `reingest_all_basins_receipt.py`：循环 10 个 basin 跑 `reingest_basin`，每个 basin 输出 JSON receipt 到 `artifacts/reingest-receipts/<date>/<basin>.json`
- [ ] 3.3 receipt 字段含：`basin_id`, `old_model_id`, `new_model_id`, `river_shp_record_count`, `sp_riv_reach_count`, `imported_reach_count`, `crosswalk_row_count`, `seg_shp_record_count`, `geom_null_count`, `max_edge_meters_observed`, `multi_part_violation_count`（多 part 检测应永远为 0）, `tile_cache_purged_count`
- [ ] 3.4 在 node-22 真实 DB 上跑 `reingest_all_basins_receipt.py`，10 个 basin 串行；任一 basin 失败立即停 + 上报 failed basin + 不污染已成功 basin（事务边界由任务 2.8 保证）
- [ ] 3.5 跑后**按 basin 限定** purge `map.tile_cache`：每 basin 执行 `DELETE FROM map.tile_cache WHERE basin_version_id IN (<old basin_version_id>, <new basin_version_id>)` 并把 `tile_cache_purged_count` 写进 receipt；**禁用全表 TRUNCATE**（会打穿未变更 basin 的瓦片缓存）
- [ ] 3.6 把 10 个 receipt JSON commit 到 `artifacts/reingest-receipts/<date>/`（CLAUDE.md 声明 `artifacts/` 不 sync 到 node-27，但本 receipt 是 PR evidence 需 git 跟踪——补一条 `.gitignore` 例外 `!artifacts/reingest-receipts/`，或在 `docs/runbooks/reach-geom-rollout-receipt.md` 引用摘要）
- [ ] 3.7 在 PR 3 描述里贴 10 行 receipt 摘要（basin_id / reach 数 / crosswalk 数 / max_edge）

## 4. node-27 display_readonly 全量 live 验证（PR 4：node-27 部署 + 实拍）

- [ ] 4.1 ssh node-27 `cd /home/nwm/NWM && git pull --ff-only`，把 PR 2/3 的代码拉到 node-27
- [ ] 4.2 node-27 nhms-db 上重跑全部 10 basin ingest（步骤同 3.1-3.5，独立 DB 实例 + 独立事务）
- [ ] 4.3 浏览器实拍 qhh basin 河段图层：放大到原"假桥"位置（如 PR #534 时代的 `basins_qhh_shud_seg_1427_ord_003268_rec_003268` sample 区，约 997m gap 处），确认无跨缝直线；截图存 `docs/runbooks/receipts/reach-geom-qhh-<date>.png`，文件名含截图时的 zoom + lng/lat metadata
- [ ] 4.4 浏览器实拍 heihe basin（与 qhh 同步验，作为多 basin 普适性证据），截图存 `docs/runbooks/receipts/reach-geom-heihe-<date>.png`，文件名含 metadata
- [ ] 4.5 浏览器实拍至少 1 个其它 basin（从 hetianhe/kashigeer/keliya/qinyijiang/tailanhe/weiganhe/xinanjiang_upstream/zhaochen 任选），命名同上
- [ ] 4.6 验 segment 级 hover/popup：在任一 reach 上 hover/click，确认 popup 内容仍显示（数据从 crosswalk 取，按 `source='basins_seg_shp'` 过滤）；截图存 `docs/runbooks/receipts/reach-segment-hover-<date>.png`
- [ ] 4.7 并排对照：取 PR #534 evidence 时代的 qhh 截图（若存在）放同一文档侧栏对比；若无可用历史，在 node-27 临时 checkout PR #534 时代 commit 走同样路径产 baseline 截图
- [ ] 4.8 把 receipt 截图 + ingest log 摘要 + 截图 metadata 写入 `docs/runbooks/reach-geom-rollout-receipt.md`

## 5a. 删除下游兜底链路（PR 5a：后端 cleanup，与 5b 解耦便于回退）

- [ ] 5a.1 删 `workers/model_registry/basins_geometry.py` 的 `_merge_polyline_parts` / `gap_split_multilinestring_wkt` / `gap_split_positions` / `_nearest_attachment` / `_point_wkt` / `_edge_meters` / `_median_edge` 整组函数 + 顶部常量 `RIVER_GAP_ABSOLUTE_M` / `RIVER_GAP_RELATIVE` / `_EARTH_RADIUS_M`
- [ ] 5a.2 删 `workers/model_registry/basins_geometry.py` 的 `_shud_count_header(sp_rivseg, ...)` 这条 cross-check（oracle 改 sp.riv reach count）
- [ ] 5a.3 删 `workers/model_registry/basins_registry_import.py` 的 `_backfill_output_segment_geometry` + `_ensure_output_river_segments` + `_output_river_segment_rows`（[basins_registry_import.py:601-769](../../../workers/model_registry/basins_registry_import.py:601) 整段）；qhh production bootstrap ([workers/model_registry/qhh_production_bootstrap.py:700](../../../workers/model_registry/qhh_production_bootstrap.py:700)) 同步切到新 ingestion 入口
- [ ] 5a.4 删 `packages/common/model_registry.py` 的 `line_or_multiline_to_wkt` + `_multilinestring_to_wkt`；任务 2.7 已切到 `geometry_to_wkt(LineString)` + SQL `ST_Multi`，删函数不影响写路径
- [ ] 5a.5 删 `scripts/backfill_river_segment_multilinestring.py` 整文件
- [ ] 5a.6 删 `tests/test_backfill_river_segment_multilinestring.py` 整文件 + 删 `tests/test_river_segment_gap_split.py` 整文件
- [ ] 5a.7 grep audit 后端：`grep -rE "_merge_polyline_parts|gap_split_multilinestring|gap_split_positions|line_or_multiline_to_wkt|_multilinestring_to_wkt|_backfill_output_segment_geometry|_ensure_output_river_segments|_output_river_segment_rows|_shud_riv_|rebackfill_river_segment|backfill_river_segment_multilinestring" -- ':!docs/stage-pipeline-log.jsonl' ':!openspec/changes/feat-reach-geom-from-river-shp/'` 返回 0 个命中
- [ ] 5a.8 node-22 重跑 pytest 全套 + ruff，确认 cleanup 不打破现有契约

## 5b. 删除前端兜底（PR 5b：frontend cleanup，独立 PR 便于 5a 失败时单独回退）

- [ ] 5b.1 删 `apps/frontend/src/lib/m11/gapAwareGeometry.ts` 整文件 + `apps/frontend/src/lib/m11/gapAwareGeometry.test.ts` 整文件（或 `__tests__/gapAwareGeometry.test.ts`，按实际位置）
- [ ] 5b.2 在 `apps/frontend/src/components/map/M11MapLibreSurface.tsx` 删 `gapAwareLineGeometry(...)` 调用点（两处：[:1208](../../../apps/frontend/src/components/map/M11MapLibreSurface.tsx:1208) / [:1273](../../../apps/frontend/src/components/map/M11MapLibreSurface.tsx:1273)），feature.geometry 直接传给 MapLibre Source；删未用的 import
- [ ] 5b.3 grep audit 前端：`grep -rE "gapAwareLineGeometry|splitPositionsAtGaps|gapAwareGeometry" apps/frontend/` 返回 0 个命中
- [ ] 5b.4 前端 vitest 全绿 + tsc + `pnpm check:api-types`

## 6. 测试 oracle 重写 + 文档同步 + archive（PR 6：收尾）

- [ ] 6.1 `tests/test_basins_registry_import.py`：删 `assert ...geometry.type == "MultiLineString"` 旧风格断言；新增三条核心断言：(a) 每行 single-part；(b) reach 行数对 `.sp.riv`；(c) crosswalk 行数对 seg.shp record 数
- [ ] 6.2 `tests/test_real_database_integration.py`：reach 级几何断言对齐；增加 `test_no_cross_gap_invariant_holds_after_ingest` 用 PostGIS `ST_NPoints` + `ST_Length` 算各 reach 内最大相邻顶点距离
- [ ] 6.3 `tests/test_model_registration.py`：删 `line_or_multiline_to_wkt` 相关单测（PR 5a 已删函数）
- [ ] 6.4 同步 OpenAPI yaml description：`openapi/nhms.v1.yaml` 中 `/api/v1/basin-versions/{basin_version_id}/river-segments` path description 改成 "segment-level features sliced from parent reach polyline via ST_LineSubstring (Path C)"（schema 不变）；运行 `pnpm check:api-types` 确认前端 client 仍然 build 通过
- [ ] 6.4a 同步 MVT 路由 audit (OQ3)：检查 `services/tiles/mvt.py:1473-1480` (`build_layer_metadata` / `resolve_tile_layer_identity`) + `apps/api/routes/flood_alerts.py:1434-1487` (`_fetch_river_network_mvt_tile_bytes`) 的 SQL 是否与新 `core.river_segment.geom` MultiLineString shape + reach-level row 粒度一致；`river_network_version_id` 变化触发 tile_cache 自然失效已 OK，但 SQL 表达式（如 `rs.geom` join 或 segment-level 过滤）若假设 segment-level 行需更新为 reach-level 解读 + 后续 ST_LineSubstring 在 MVT 内是否也需切片决策
- [ ] 6.4b MVT 路由的 segment-slice 决策（如 6.4a 发现需要）：MVT vector tile 通常 reach-level 渲染已够（缩放级别 ≤ 13 时 segment 区分不明显）；若决定 MVT 不切片，在 spec.md 加 Non-Goal 声明"MVT 路由按 reach-level 输出"
- [ ] 6.5 同步 `docs/spec/03_database_design.md`：把 `river_segment.geom` 列类型从 `LineString(4490)` 改为 `MultiLineString(4490)`（与 [migration 000037](../../../db/migrations/000037_river_segment_multilinestring.sql) + live schema 对齐；CLAUDE.md DOC_STATUS 顺序：live schema 是 truth）
- [ ] 6.6 同步 `docs/appendices/A_id_and_versioning_convention.md`：把 `_riv_` 标准段补一条 `<model_id>_reach_<iRiv:06d>` 现行规范
- [ ] 6.7 `tests/test_migrations.py` 不变（000037 已是 master 历史，不需调整）
- [ ] 6.8 `openspec validate feat-reach-geom-from-river-shp --strict --no-interactive` 全绿
- [ ] 6.9 全部 PR 1–5b 合并后归档 change：`openspec archive feat-reach-geom-from-river-shp`
- [ ] 6.10 Stage Change Pipeline 跨运行问责：若 `docs/stage-pipeline-log.jsonl` 不存在则创建（schema 见 [stage-change-pipeline skill 跨运行问责节](../../../.claude/skills/stage-change-pipeline/SKILL.md)），追加一行 `{"change":"feat-reach-geom-from-river-shp","date":"<run-date>","rounds":<n>,"gate_net_catch":<n>,...}` 记录本次流水线 catch-rate
