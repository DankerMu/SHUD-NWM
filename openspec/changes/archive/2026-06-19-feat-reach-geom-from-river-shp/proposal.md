## Why

当前 ingestion ([basins_geometry.py:181-189](../../../workers/model_registry/basins_geometry.py:181)) 把 SHUD 模型包里**为 mesh element 索引而生**的 `gis/seg.shp` 当作前端展示用的 reach polyline 几何源。`seg.shp` 把同一条 reach 按 mesh element 切成多个 record、且 record 内部经常是 storage-order ≠ flow-order 的 multi-part Polyline（qhh 实物：330/3738 records 多 part；同一 SHUD 包基准日期 2026-05-14，见 [docs/runbooks/qhh-22-business-bringup.md](../../../docs/runbooks/qhh-22-business-bringup.md)），导致 ingestion 必须做 greedy stitching；当 record 的 part 在源 GIS 里空间不相邻（qhh 最远 1721m），stitching 出来的"最短连接"仍是凭空造出来的跨缝直线——这就是前端看到的"假桥跳变"。

PR #534（commit 17d1c03）引入的 MultiLineString 列 + `_merge_polyline_parts` + `gap_split_multilinestring_wkt` + `scripts/backfill_river_segment_multilinestring.py` + 前端 `splitPositionsAtGaps` 是一整套**在已经收到带 bug 的 seg.shp 之后**的下游兜底。同一个 SHUD 模型包里**已经存在**完美干净的 `gis/river.shp`：1633 records 与 `.sp.riv` reach 一一对应、**0 multi-part**、flow-ordered 单一 polyline、字段比 seg.shp 还全（含 Slope/Length/Depth/Width/Sinuosity/Manning/...）。换源即去病。

## What Changes

- **几何源切换**：`basins-registry-import` ingestion 把 `core.river_segment.geom` 的来源从 `gis/seg.shp` 改为 `gis/river.shp`。新 `core.river_segment` 行数 = `.sp.riv` reach 数（qhh 基线: 3738 → 1633；实际数值在每个 basin 的 ingestion receipt 里取最新值），每行一条 reach 的完整 flow-ordered polyline。
- **保留 segment 粒度（属性 sidecar，复用现有表）**：`gis/seg.shp` 仍然 ingest，但只作为 segment → reach 的映射，写入既有的 `core.river_segment_crosswalk` 表 ([000004_core.sql:56-69](../../../db/migrations/000004_core.sql:56)) 用既有列 `(river_network_version_id, river_segment_id, source='basins_seg_shp', external_id='<iRiv>:<iEle>', properties_json={iRiv,iEle,...})`。不新增 migration、不动 schema、不动 `apps/api/routes/models.py:693` 的 crosswalk API。前端 segment 级 hover/着色仍能命中。
- **DB 行粒度变化（语义内部，对外契约保持）**：`core.river_segment` 行粒度从 segment 改为 reach（qhh: 3738 → 1633）；DB 内 `river_segment_id` 命名规范从 `<model>_seg_<segment_order>_ord_<iRiv>_rec_<iEle>` 简化为 `<model>_reach_<iRiv:06d>`（zero-padded）。
- **API contract 保留 segment-level**（OQ2 / D7 / Path C）：`GET /api/v1/basin-versions/{basin_version_id}/river-segments` 仍返回 segment-level FeatureCollection（qhh: 3738 features），每个 segment feature 的 `river_segment_id` 仍是 segment-level identifier（衍生自 crosswalk `(iRiv, iEle)`），geometry 在 API 层用 PostGIS `ST_LineSubstring(reach_geom, start_fraction, end_fraction)` 从 parent reach polyline 按 `sp.rivseg.Length` 累积比例切片。前端 `apps/frontend/src/components/map/M11MapLibreSurface.tsx` 的 hover/popup/colour/promoteId/forecast 路径无需任何改动。切片几何始终是 reach polyline 子集 → 永不引入合成坐标 / 假桥。
- **废弃下游兜底链路 + 同主题 output-river backfill**：
  - 删 `_merge_polyline_parts` + `gap_split_multilinestring_wkt` + `gap_split_positions` 及辅助常量（[basins_geometry.py:740+](../../../workers/model_registry/basins_geometry.py:740) 整段）
  - 删 `_backfill_output_segment_geometry` 整段（[basins_registry_import.py:601-769](../../../workers/model_registry/basins_registry_import.py:601)）—— **此链路被 reviewer 指出为 `shud_output_river=true` 行的唯一几何来源；新架构下 reach 行的几何由 ingestion 从 river.shp 直接写入，不再需要从 seg.shp 反向 stitch**
  - 删 `scripts/backfill_river_segment_multilinestring.py` + [tests/test_backfill_river_segment_multilinestring.py](../../../tests/test_backfill_river_segment_multilinestring.py)
  - 删前端 `apps/frontend/src/lib/m11/gapAwareGeometry.ts` 的 `splitPositionsAtGaps` + 测试 + 调用点（[M11MapLibreSurface.tsx:1208 / :1273](../../../apps/frontend/src/components/map/M11MapLibreSurface.tsx:1208) 两处）
  - 删后端写路径里为 MultiLineString 多 part 准备的 `line_or_multiline_to_wkt`（[model_registry.py:152+](../../../packages/common/model_registry.py:152)），改用单 part LineString → ST_Multi 升 MultiLineString
- **保留 DB 列形状**：`core.river_segment.geom` 仍为 `geometry(MultiLineString, 4490)`（不再做 schema 回滚 migration），新数据写入永远是单部件 MultiLineString。注意：[docs/spec/03_database_design.md:160](../../../docs/spec/03_database_design.md:160) 仍写 `LineString(4490)` 是 stale 文档（migration 000037 已升 type 但 doc 没同步）—— 本 change 顺手把那行更新到与 live schema + migration 一致。
- **数据迁移**：所有已 ingest 的 10 个 basin（qhh / heihe / hetianhe / kashigeer / keliya / qinyijiang / tailanhe / weiganhe / xinanjiang_upstream / zhaochen）需要重新 ingest，把 `core.river_segment` 替换为 river.shp 派生的 reach 行；crosswalk 表写入 `(iRiv, iEle)` 映射。不写一次性 backfill 脚本，复用现有 ingestion 入口。
- **OpenAPI / 前端契约不变形（schema 不变；description 同步）**：`GET /api/v1/basin-versions/{id}/river-segments` 端点 + 响应字段名 + 类型保留（geometry 仍是 GeoJSON MultiLineString），变化的是行数 + ID 含义；OpenAPI [nhms.v1.yaml](../../../openapi/nhms.v1.yaml) 的相关 description 同步把 "segment"→"reach"。`GeoJsonMultiLineString` `oneOf` 保留（兼容历史负载），但生产语义恒为 single-part。

## Capabilities

### New Capabilities

无新 capability，全部走 `basins-registry-import` modify。

### Modified Capabilities

- `basins-registry-import`：
  - MODIFY Requirement "River segments are imported with geometry and topology metadata"：oracle 从 "`gis/river.shp` or `gis/seg.shp` and SHUD `.sp.riv`" 改为 "`gis/river.shp` and SHUD `.sp.riv`（`segment_count` = `.sp.riv` reach 数）"；`.sp.rivseg` 显式声明**不再用作几何或拓扑来源**，仅供历史 cross-check evidence count；single-part MultiLineString 不变量限定为本 ingestion 路径写入的行（output-river backfill 路径在本 change 内被删，不存在"另一种行"问题）。
  - 新增 Requirement "Segment-to-reach crosswalk is preserved from `gis/seg.shp`"：覆盖 seg.shp 的 (iRiv, iEle) 写入既有 `core.river_segment_crosswalk` schema 的语义（不新增列、不动 API）。
  - 新增 Requirement "Reach geometry has no fabricated cross-gap straight bridges"：声明 ingestion 出来的 reach polyline 任意相邻顶点距离 ≤ `max(300 米绝对阈值, 4 × 该 reach 内相邻边长中位数)`，作为对 #534 兜底链路废弃的形式化承诺。阈值数字在 spec 内写死（不再依赖被删除的 `RIVER_GAP_ABSOLUTE_M` / `RIVER_GAP_RELATIVE` 常量）。
  - 新增 Requirement "Required input files are validated for presence" 覆盖 river.shp 缺失、seg.shp 缺失等边界。
  - 新增 Requirement "Imported river segment IDs follow the reach-level naming convention" 覆盖 ID 命名 BREAKING change。
  - 新增 Requirement "Deprecated cross-gap fallback paths are removed from the codebase"。
  - 新增 Requirement "All basin packages are re-ingested under the reach-source contract"。
  - REMOVE Requirement "seg.shp 当 fallback 几何源"（在 spec.md 的 REMOVED 块里给 Reason + Migration）。
  - 不动 Requirement "Imported models remain inactive until explicitly activated"。

## Impact

- **受影响代码**：
  - `workers/model_registry/basins_geometry.py`（parser）：seg/river 优先级翻转 + 整段废 `_merge_polyline_parts` / `gap_split_*` / `_river_segments_from_layer` 的 seg 路径 + 删 `_shud_count_header(sp_rivseg, ...)` 这条 cross-check（因为 oracle 改 sp.riv）
  - `workers/model_registry/basins_registry_import.py`（ingestion 入口）：删 `_backfill_output_segment_geometry` + `_ensure_output_river_segments` 的 stitching 路径；新增 crosswalk 写入逻辑（复用 [PsycopgModelRegistryStore.create_crosswalk_entries](../../../packages/common/model_registry.py:1187) 或同 schema 的批量 upsert）
  - `packages/common/model_registry.py`（写路径）：废 `line_or_multiline_to_wkt`、`_multilinestring_to_wkt`，改单 LineString → SQL 侧 `ST_Multi`
  - `apps/frontend/src/lib/m11/gapAwareGeometry.ts`（前端兜底）：整文件 + 测试 + 调用点（M11MapLibreSurface.tsx:1208 / :1273）删除
  - `scripts/backfill_river_segment_multilinestring.py` + `tests/test_backfill_river_segment_multilinestring.py` + `tests/test_river_segment_gap_split.py`：整套删除
  - `tests/test_basins_registry_import.py` / `tests/test_real_database_integration.py` / `tests/test_model_registration.py`：oracle 重写
  - `openapi/nhms.v1.yaml`：`/api/v1/basin-versions/{basin_version_id}/river-segments` 路径 description + `GeoJsonMultiLineString` description 把 "segment"→"reach"（schema 不变）
  - `docs/spec/03_database_design.md`：把 `river_segment.geom` 列类型由 `LineString(4490)` 改为 `MultiLineString(4490)`（与 migration 000037 + live schema 对齐）
  - `docs/appendices/A_id_and_versioning_convention.md`：把 `_riv_` 标准段补一条 `<model_id>_reach_<iRiv:06d>` 现行规范
- **受影响 DB**：
  - `core.river_segment` 全表 backfill（10 个 basin，预计行数 5–10× 缩减；qhh 基线 3738 → 1633）
  - `core.river_segment_crosswalk` 现有 schema 复用，每个 basin 新增 ≈ seg.shp record 数行
  - `map.tile_cache` 按 basin 限定 DELETE（不全 TRUNCATE，避免污染未变更 basin 的瓦片）
- **受影响 API**：
  - `GET /api/v1/basin-versions/{id}/river-segments` 行数 + `river_segment_id` 含义变化（schema 不变）
  - 前端 MVT `/api/v1/tiles/river-network/...`（如未来启用）同步降量
- **依赖 / 系统**：
  - 不依赖外部库 / 新工具；**不引入新 migration**（DB schema 完全不动；000038 仍是 master 最新）
  - node-22 真实 DB pytest oracle 必须重跑全套
  - node-27 display_readonly 实拍 receipt 必须覆盖 qhh + heihe + 至少 1 个其它 basin（共 ≥3 个 basin）
- **回滚路径**：
  - 任意阶段失败时，`scripts/backfill_river_segment_multilinestring.py` + parser 旧逻辑 + `_backfill_output_segment_geometry` 保留在 git 历史，可 revert PR
  - DB 列 MultiLineString 形状本身不变所以无需 schema 回滚 migration
  - crosswalk 表 schema 也不动，已写入的 source='basins_seg_shp' 行可单独 `DELETE WHERE source='basins_seg_shp'` 清掉

## Out-of-scope（明确不做，避免 scope creep）

- 不重写 SHUD 模型预处理 R 脚本 / 不修改 SHUD 模型包数据（即便 R 脚本是真正根因）
- 不引入新 capability / spec
- 不动 `core.river_segment.geom` 列类型（不做 schema 回滚 migration）
- 不实现并发 ingest 隔离（多 basin ingest 仍按串行调用）
- 不处理 MVT 路由 `/api/v1/tiles/river-network/...` 的下游消费者（OQ 留待 Stage 4 实施时 explorer 扫一次确认无消费）
