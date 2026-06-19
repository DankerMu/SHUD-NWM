## Context

`basins-registry-import` 把 SHUD 模型包 (`/home/nwm/NWM/data/Basins/<basin>/input/<basin>/`) 里的 GIS 数据导入 `core.basin_version` / `core.river_network_version` / `core.river_segment` / `core.river_segment_crosswalk`。前端 (`apps/frontend/src/components/map/M11MapLibreSurface.tsx`) 通过 `GET /api/v1/basin-versions/{id}/river-segments` 拿 GeoJSON 渲染河段图层 `m11-basin-river-line`。

当前实现 ([basins_geometry.py:162-189](../../../workers/model_registry/basins_geometry.py:162)) 把 `gis/seg.shp` 设为几何源（仅 fallback 用 `gis/river.shp`）。`seg.shp` 的设计目的是 **SHUD segment → mesh element 索引表**（字段就两列 `iRiv, iEle`），不是为前端展示的 reach polyline 设计。同一 SHUD 模型包里 `gis/river.shp` 才是 reach 级 polyline：1633 records、与 `.sp.riv` 一一对应、0 multi-part、字段完整。

此外，[basins_registry_import.py:601-769](../../../workers/model_registry/basins_registry_import.py:601) 的 `_backfill_output_segment_geometry` 也共享同一套 `_merge_polyline_parts` + `gap_split_multilinestring_wkt` helpers，是 `shud_output_river=true` reach 行的当前几何来源（reviewer 在 Stage 3 P0 显式点出）。新架构下 reach 行的 geom 由 ingestion 从 river.shp 直接写入，**该 backfill 路径整段被新链路替代而非保留**——删除而不留 dead code。

PR #534（commit 17d1c03）通过列升 MultiLineString + parser greedy stitching + gap_split 剪桥 + 前端 splitPositionsAtGaps 兜底，把"假桥"问题压到肉眼不可见，但本质是在错的源上加层补丁。本 change 把源换正，把那层补丁全部拆除。

**核验证据**：node-27 nhms-db 上同一条 reach `iRiv=1` 在两份里实物对比：river.shp record#0 起点 (439639.66, 4206130.44) = seg.shp seg#4 pt0；river.shp record#0 终点 (441686.30, 4205838.18) = seg.shp seg#5 pt-1（同一 SHUD 包基准日期 2026-05-14）。两者覆盖同一组坐标，仅分组方式不同，且 river.shp 是 flow-ordered single-part，零 stitching 风险。

## Goals / Non-Goals

**Goals:**

- ingestion 出来的 `core.river_segment.geom` **物理上不可能含有"跨缝假桥直线"**——以"任意单 reach 内相邻顶点距离 ≤ `max(300m 绝对阈值, 4× 该 reach 边长中位数)`"作为可测试不变量（阈值数字在 spec 内写死，不依赖被删除的常量）
- 前端 reach 级渲染不再需要任何"防御性再拆"或 client-side gap detection；`gapAwareLineGeometry` 文件 + 调用点全部删除
- segment 级 hover/着色/属性 popup **不退化**：保留 crosswalk 链路把 `(iRiv, iEle)` 写入既有 `core.river_segment_crosswalk` 表（用现有列 `source='basins_seg_shp'` + `external_id='<iRiv>:<iEle>'` + properties_json），前端可仍按 segment id 命中（语义改为"reach 内子段"）
- 10 个 basin 全部完成重 ingest，含可审计的"reach 行数 = `.sp.riv` reach 数 + crosswalk 行数 = seg.shp record 数"receipt
- 整套补丁链路 + 同主题 `_backfill_output_segment_geometry` 一起从代码库完全删除，不留"以防万一"留尾

**Non-Goals:**

- 不动 `core.river_segment.geom` 的列类型（保持 `geometry(MultiLineString, 4490)`），不做 schema 回滚 migration
- 不动 `core.river_segment_crosswalk` schema（不加列、不动 PK/UNIQUE、不动 [API](../../../apps/api/routes/models.py:693)）
- **不引入任何新 migration**（master 最新 000038 保持）
- 不动 OpenAPI `GET /api/v1/basin-versions/{id}/river-segments` 响应字段名 + 类型（仍是 GeoJSON MultiLineString FeatureCollection；只同步 description 文字）
- 不重写 SHUD 模型预处理 R 脚本 / 不修改 SHUD 模型包数据
- 不引入新的 capability / spec，全部走 `basins-registry-import` modify
- 不支持并发 ingest（多 basin 仍串行）
- 不处理 MVT 路由 `/api/v1/tiles/river-network/...` 的下游消费者（OQ3，Stage 4 实施时 explorer 验证一次再决策）

## Decisions

### D1：几何源用 `gis/river.shp` 而非 `gis/seg.shp`

**Why**：`seg.shp` 字段仅 `(iRiv, iEle)`，本质是 segment → mesh element 索引；qhh 实物 330/3738 records (8.8%) 是 multi-part 且 storage order ≠ flow order；ingestion 必须 stitching，且 part 间最远 1721m 假桥不可避免。`river.shp` 字段全（Index/Down/Type/Slope/Length/BC + Depth/BankSlope/Width/Sinuosity/Manning/Cwr/KsatH/BedThick），与 `.sp.riv` reach 数一一对应（1633 vs 1633），qhh 实物 0 multi-part、flow-ordered single-part polyline。

**Alternatives considered**：
- **(B) 用 `.sp.rivseg + .sp.mesh` 重构坐标**：`.sp.rivseg` 只有 `(Index, iRiv, iEle, Length)` 拓扑列、无坐标；`.sp.mesh` 第二区块含三角顶点坐标，理论可拼出 element 边几何，但拿到的是 **mesh 三角元素的边**而非 reach polyline，几何形状与 GIS 不一致、且重建复杂。
- **(C) 修 `seg.shp` 的源头（R 预处理脚本）**：碰 rSHUD 工具链 + 重出 10 个 basin 的全部 seg.shp，影响面跨 repo 跨数据；本 change 不走。
- **(D) 双源并行（保 seg.shp 几何 + 加 river.shp 几何为可选展示源）**：架构最重，需新表 / 新列 / 前端图层切换；不符合"一个源做对"的原则。

### D2：复用现有 `core.river_segment_crosswalk` schema 写入 segment 映射

**Why**：[现有表 schema](../../../db/migrations/000004_core.sql:56) 是 `(crosswalk_id BIGSERIAL PK, river_network_version_id TEXT, river_segment_id TEXT, source TEXT, external_id TEXT, properties_json JSONB)`，UNIQUE `(river_network_version_id, river_segment_id, source)`，并有 [既存 upsert API](../../../packages/common/model_registry.py:1187) `create_crosswalk_entries`。把 `(iRiv, iEle)` 映射写成：
- `river_network_version_id = <new rnv id>`
- `river_segment_id = <model_id>_reach_<iRiv:06d>`（指向新 reach 行）
- `source = 'basins_seg_shp'`（新增的 source 值）
- `external_id = f"{iRiv}:{iEle}"`
- `properties_json = {"iRiv": <int>, "iEle": <int>, "segment_order": <int>, "length_m": <float|null>}`

完全不需要新 migration、不动 API、不动 schema。前端要 segment 级 hover 时按 `(rnv_id, source='basins_seg_shp')` 拉 crosswalk 行，再 join 回 reach 行。

**Alternatives considered**：
- 新加列 `basin_version_id` / `reach_segment_id` / `source_segment_index`（Stage 3 reviewer 指出是最初 spec 的假设）：需新 migration + 改 schema + 改既有 API + 数据迁移，工作量净增、收益小（现有列足以表达）。

### D3：DB 列保留 `geometry(MultiLineString, 4490)`，不做 schema 回滚

**Why**：避免再做一次 schema migration（000039 风险 + 测试矩阵 + 数据丢失风险）。新数据 ingestion 写入 `MULTILINESTRING((<river.shp polyline>))`——单部件 MultiLineString，外观与 `LineString` 等价但 schema 兼容旧链路。注意 [docs/spec/03_database_design.md:160](../../../docs/spec/03_database_design.md:160) 仍写 `LineString(4490)` 是 stale，需在本 change 顺手把那行更新到与 [migration 000037](../../../db/migrations/000037_river_segment_multilinestring.sql) + live schema 一致；live DB schema 是 truth。

**Alternatives considered**：
- 列回 `LineString(4490)`：最干净，但需新 migration + 数据迁移 + OpenAPI schema 同步降级，工作量净增、风险净增、收益小。

### D4：废弃下游兜底链路 + 同主题 output-river backfill

**Why**：源头不再产生假桥，下游兜底无意义。`_backfill_output_segment_geometry` 是 reviewer 在 Stage 3 P0 显式提出的"同主题但被忽略"路径——它读 seg.shp、按 iRiv 分组、走相同 stitching + gap_split helpers 来填 `shud_output_river=true` 行的 geom。新架构下 reach 行的几何由 ingestion 从 river.shp 直接写入，该 backfill 路径整段废除（不是修补、不是迁移）。

保留兜底意味着：
- 代码留 dead path（增加维护成本 + 阅读者困惑）
- 测试矩阵双倍
- 未来如果新增 basin 真的有"应该是多部件"的几何（比如真实的离散河段），兜底会把它误剪

全部删除 + 在 spec 里加 "no fabricated cross-gap" invariant，单元测试用 qhh 真实数据 fixture 钉住。

**适用范围**：spec 的 "single-part MultiLineString" 不变量限定为本 change 之后 `basins-registry-import` 路径写入的全部 `core.river_segment` 行（不再有"另一种"写路径，因为 output-river backfill 已删）。

**Alternatives considered**：
- 保留兜底作 "defense in depth"：架构反映"我们不信任自己的 ingestion"，本身就是设计气味；既然有形式化不变量 + 测试覆盖，应该信任源。
- 只删 ingestion 兜底但保留 `_backfill_output_segment_geometry`：剩下半个"两套写路径"，矛盾未消。

### D5：ID 命名 `<model_id>_reach_<iRiv:06d>`（zero-padded）

**Why**：行粒度从 segment 改 reach，ID 含义需同步变更。原 ID 含 `(segment_order, iRiv, iEle)` 三元组对 segment 唯一，新 ID 只需 `iRiv` 对 reach 唯一。zero-padded 6 位是为了与 [现有 `_shud_riv_<index:06d>` 风格](../../../workers/model_registry/basins_registry_import.py:780) 对齐 + 字典序排列正确。crosswalk 表 `external_id` 仍用未 padded 的 `"<iRiv>:<iEle>"` 与源数据保持一致。

[docs/appendices/A_id_and_versioning_convention.md](../../../docs/appendices/A_id_and_versioning_convention.md) 当前规范是 `{river_network_version_id}_riv_{zero_padded_index}`——本 change 顺手补一条 `<model_id>_reach_<iRiv:06d>` 现行规范进附录。

**Alternatives considered**：
- 不 zero-padding（如 `<model_id>_reach_1`）：字典序排列错乱（reach_1 < reach_10 < reach_2）；不取。
- 复用既有 `_shud_riv_<iRiv:06d>` 命名（替换 `reach` 关键字）：但 `_shud_riv_` 路径目前是 `shud_output_river=true` 行的命名，与本 change 删除该路径有歧义；用 `reach` 更清晰。

### D6：迁移走"重 ingest 每个 basin"而非新写 backfill 脚本

**Why**：ingestion 入口 `workers/model_registry/basins_registry_import.py` 本身就是幂等写（`Changed package checksum is not silently overwritten` Scenario 已经覆盖），重跑会自然覆盖旧行。专门的 backfill 脚本（如刚删除的 `scripts/backfill_river_segment_multilinestring.py`）属于一次性产物，不再需要。

**Alternatives considered**：
- 写新 backfill 脚本 `scripts/rebackfill_river_segment_from_river_shp.py`：与"重 ingest"功能重复，且让 git 历史多一条短命脚本。spec 在 "Deprecated cross-gap fallback paths are removed" Requirement 的 grep 黑名单里把 `rebackfill_river_segment` 模式也列入，永久禁止此类脚本。

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| 其它 9 个 basin 的 `river.shp` 可能也带 multi-part / 与 `.sp.riv` reach 数不一致（只在 qhh 实物验过 0 multi-part）→ ingestion 出现 single-part assumption 违反 | parser 加 invariant：若 river.shp record 出现 multi-part 或与 `.sp.riv` reach 数不一致或字段集 (Index, Down, Type, Length, Slope, BC, Depth, BankSlope, Width, Sinuosity, Manning, Cwr, KsatH, BedThick) 任一缺失，**fail-fast** + 报相应结构化错误码（不 fallback、不静默修补）；单测 fixture 用 qhh + heihe + 1 个其它 basin 各抽 sample；**1 个 basin 失败不阻塞其他 9 个**（隔离在 per-basin 事务，已 ingest basin 保留旧数据不动） |
| `_backfill_output_segment_geometry` 是 `shud_output_river=true` 行的当前几何来源，直接删可能打穿 production reach geometry | 顺序：(a) 先实现"ingestion 从 river.shp 直接写 reach 行"路径 + 验证 reach 行 geom 已正确写入 → (b) 再删 `_backfill_output_segment_geometry`；qhh production bootstrap ([qhh_production_bootstrap.py:700](../../../workers/model_registry/qhh_production_bootstrap.py:700)) 同步切换到新路径；spec 把 "ingestion-written reach geom" 作为 single-part 不变量的唯一适用范围 |
| `_shud_riv_<index:06d>` 既存路径与新 `_reach_<iRiv:06d>` 行可能在同表共存 / 含义冲突 | 删 `_output_river_segment_rows` 时同时迁移已有 `_shud_riv_*` 行：(a) 在新 ingestion 写 `_reach_*` 时按 (rnv_id, iRiv) 一致原则可与旧 `_shud_riv_*` 行互换；(b) 旧 `_shud_riv_*` 行通过 per-basin reingest 自然被覆盖删除（旧 basin_version_id 下的所有 segment 行整体替换）；(c) spec 的 grep 黑名单包含 `_shud_riv_` 路径写入逻辑名（如 `_output_river_segment_rows`）|
| 前端 segment 级缓存（如按 segment_id key）被 ID 命名变更打穿 | basin_version_id / model_id 在 ingestion 重跑后会换新，前端按 model_id 缓存自然失效；显式在 ingestion receipt 记录 (basin, old_model_id, new_model_id) |
| `core.river_segment_crosswalk` 写入新增 ingestion 路径，可能与现有读取路径（[`apps/api/routes/models.py:693`](../../../apps/api/routes/models.py:693)）冲突 | Stage 3 reviewer 已点过——`source='basins_seg_shp'` 是新值，与现有 source 值（若有）不冲突；写入用既有 `create_crosswalk_entries` upsert 路径保证幂等 |
| segment 级 hover 性能：crosswalk 表查询新加 (rnv_id, source='basins_seg_shp') 过滤 | 既有索引 `river_segment_crosswalk_lookup_idx (river_network_version_id, source, river_segment_id)` 已覆盖；不需新加索引 |
| 10 个 basin 重 ingest 出错 / 部分失败 | 在 node-22 上按 basin 逐个 ingest，每个 basin 一个事务；任一失败立即停、记录失败 basin、不污染已成功 basin 的数据；retry 走幂等；不自动回退到 seg.shp 路径 |
| qhh fixture 不在 git 仓库（CLAUDE.md 明确不 sync `data/Basins/`）；单测无 fixture 可用 | 新增 task：从 qhh 真实包抽 minimal subset（含 multi-part record + flow-ordered record + .sp.riv 摘要）拷到 `tests/fixtures/basins/qhh-sample/` 并 git commit；single-part invariant 单测走 fixture，全量 ingest 走 node-22 真实包 |
| 浏览器实拍 receipt 判断标准不刚性（肉眼判断"无桥"）| receipt 截图加 metadata（zoom level + center lng/lat）；同位置贴 PR #534 时代的 master 截图作并排对照（PR #534 evidence 路径或重跑生成）|
| 同一 basin 重 ingest 中途失败的事务行为 | spec 显式 Scenario "Per-basin ingest is transactional"——单 basin 内 river_segment + crosswalk 在同一事务，失败回滚到 pre-ingest 状态 |
| `map.tile_cache` 全 TRUNCATE 影响未变更 basin | 改成 per-basin DELETE：`DELETE FROM map.tile_cache WHERE basin_version_id IN (<basin's old + new ids>)`；purged_count 写入 receipt |

## Migration Plan

**实施顺序（每步 ≈ 一个 PR 边界）**：

1. **PR 1 — Crosswalk parser + writer 函数（纯代码准备，不接入生产路径）**：新增 `parse_seg_shp_crosswalk` + `_build_river_segment_crosswalk_rows` 两个**纯函数** + fixture-based 单测，**不在 production ingestion 主入口调用**、不动几何源、不改 ID 命名。PR 1 单跑时不写一条 `core.river_segment_crosswalk` 行——避免新 `_reach_<iRiv:06d>` 格式 crosswalk 行引用尚不存在的 reach ID 时打穿 [000004_core.sql:64-65](../../../db/migrations/000004_core.sql:64) 的 FK 约束。验证只跑单测。
2. **PR 2 — 原子切换（几何源 + ID 重命名 + crosswalk 接入 + invariant fail-fast）**：单一事务内同时完成：(a) 几何源 seg → river；(b) `core.river_segment.river_segment_id` 改 `<model_id>_reach_<iRiv:06d>`；(c) 在 ingestion 主入口调用 PR 1 准备的函数 + `create_crosswalk_entries` 写 crosswalk；(d) 同事务内**先 insert/upsert `core.river_segment` 再 insert/upsert `core.river_segment_crosswalk`**，FK 永远满足；(e) 同 basin reingest 时同事务内先 `DELETE` 旧 `<model>_seg_*` 行避免 FK 孤儿；(f) `_validate_river_shp_single_part_invariant` + `_validate_required_files_present` 在事务起点先跑。验证 oracle：node-22 pytest 全套（含新 `test_river_segment_and_crosswalk_atomic_fk_order` + `test_re_ingest_replaces_legacy_seg_ids`）+ node-27 实拍 qhh 没桥。
3. **PR 3 — 重 ingest 10 个 basin（node-22）+ 产 receipt**：在 node-22 真实 DB 上按 basin 逐个 ingest，每个 basin 出 receipt（reach 行数、crosswalk 行数、geom NULL 数、part 分布、max edge meters、tile_cache purged count）；按 basin DELETE `map.tile_cache`。
4. **PR 4 — node-27 全量 live 验证**：node-27 nhms-db 同步执行 ingest + crosswalk 写入；浏览器实拍 qhh + heihe + 至少 1 个其它 basin 确认无桥 + segment 级 hover 正常；截图含 metadata + 与 PR #534 时代并排对照。
5. **PR 5a — 后端 cleanup**：删 `_merge_polyline_parts` + `gap_split_multilinestring_wkt` + `gap_split_positions` + `_backfill_output_segment_geometry` + `_ensure_output_river_segments` + `scripts/backfill_river_segment_multilinestring.py` + 对应 tests；写路径 `line_or_multiline_to_wkt` 删除；grep audit。
6. **PR 5b — 前端 cleanup**：删 `apps/frontend/src/lib/m11/gapAwareGeometry.ts` 全文件 + 测试 + M11MapLibreSurface.tsx 两处调用点 + 未用 import；前端 vitest 全绿。
7. **PR 6 — 测试 oracle 重写 + 文档同步 + archive**：更新 `test_basins_registry_import` / `test_real_database_integration` 的断言、对齐两个不变量；同步 `docs/spec/03_database_design.md` 列类型 + `docs/appendices/A_id_and_versioning_convention.md` 新规范 + `openapi/nhms.v1.yaml` description；`openspec validate --strict` 通过；`openspec archive` 收尾；`docs/stage-pipeline-log.jsonl` 追加 catch-rate 记录。

**回滚策略**：任意 PR 失败立即 revert；DB schema 不动所以**无需任何 schema 回滚 migration**；如果 PR 3 重 ingest 出现脏数据：(a) `DELETE FROM core.river_segment WHERE basin_version_id IN (<successful new ids>)` + `DELETE FROM core.river_segment_crosswalk WHERE source='basins_seg_shp' AND river_network_version_id IN (<new rnv ids>)` 清新数据；(b) 旧 PR #534 的 backfill 脚本仍在 git 历史，可短期 revert 恢复旧 reach geom。

## Open Questions

- **OQ1**：`workers/model_registry/basins_registry_import.py` 的 CLI / orchestration 入口是不是已有"按 basin 名重 ingest"命令？（影响 PR 3 任务粒度）需要 Stage 4 实施时确认；若无，PR 3 顺带加一个简单的 CLI subcommand。
- **OQ2**：前端 segment 级 hover/着色现状是否真的在用 segment_id？还是已经按 reach_id 聚合？（影响 D2 的必要性）Stage 4 实施时让一个 explorer 明确扫一下 `apps/frontend/src/stores/overviewData.ts` + popup 组件。**若答案是"前端早已按 reach_id 聚合"，则 D2 + crosswalk Requirement 简化为"占位写入不强求消费方对接"**。
- **OQ3**：MVT 路由 `/api/v1/tiles/river-network/...`（[flood_alerts.py:1143](../../../apps/api/routes/flood_alerts.py:1143)）是否需要同步降量？Stage 4 实施前 explorer 跑一次 grep 验证无其它消费者。
