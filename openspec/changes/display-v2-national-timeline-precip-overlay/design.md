## Context

设计 oracle：`docs/plans/2026-09-03-display-v2-header-river-precip-timeline.md`（前提纠正、实机事实、14 条决策清单）。本文件只承载实现取舍与测试 seam，不重复事实清单。

现状要点：
- 全国流量瓦片 `hydro-national` 的 `latest_runs` CTE 只按 `cycle_time DESC, run_id DESC` 取 run，没有 source/cycle 绑定（`services/tiles/mvt.py:654,692`）；`national_discharge_valid_times` 返回 `common_end` 前 100 个整点，前端默认取末项。
- 时间轴组件 `M11Timeline`、源/图层控件自 M26 起未挂载（`apps/frontend/src/pages/m11/M11Controls.tsx`），全屏地图壳在 `OverviewPage.tsx` 的 `M11FullscreenMap`。
- canonical 降水产品（3h 步平均雨强 mm/day，225×329）只在 node-22 本地生产根；NFS 无 `canonical/`；node-27 有 netCDF4/numpy，无 eccodes/Pillow。
- `hydro.hydro_run.source_id` 实际值 `gfs` / `IFS`。

## Goals / Non-Goals

**Goals:**
- 全国流量与降水按同一 `(source, cycle, valid_time)` 三元组渲染，时间轴 3h 步长覆盖 lead 0–167h，默认 lead=0。
- 降水层是「过去 24h 累积」，跨起报时次取切片，任一切片缺失即 fail-closed。
- 不引入新 Python 依赖、不改 DB schema、不改 node-22 计算链路；旧全国瓦片路由保留别名。
- 全国静态河网更密，且 z3/z4 瓦片坐标数受 50k 上限约束。

**Non-Goals:**
- 12h 窗口、其它气象变量、代站降水值、Best Available 的全国解析、CLDAS/ERA5、DB 变更、Slurm/SHUD 链路。

## Decisions

### D1. 全国瓦片路由带 `{source}/{cycle}`，旧路由别名
- 新路由 `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`；`source` 为 `gfs|ifs`（路由小写），SQL 用 bind 参数 `lower(h.source_id) = :source AND h.cycle_time = :cycle`（`postgis_tile_sql(layer)` 仍只接一个 layer 参数、返回单条 SQL 字符串，source/cycle 与 `:z` 一样走 bind，不进函数签名）。旧 5 段路由保留、行为不变（段数不同无路由歧义）。
- **两处 run 选择必须同时绑定**：`latest_runs` 数据 CTE 与 `source_identity_stats_sql` 身份探针内联的 run 发现子查询（`services/tiles/mvt.py`）都要加同一对谓词——探针决定 `apps/api/routes/hydro_display.py` 的 424 fail-closed，只改 CTE 会让「该 source/cycle 无 run 但别的 source 有」返回空 200 而不是 424。
- 目录 `required_placeholders` 统一为 `["source","cycle","valid_time","z","x","y"]`（后端今天就含 z/x/y，`services/tiles/mvt.py` 的 `_NATIONAL_DISCHARGE_METADATA`），mvt-tile-contract / overview-data-contracts / 前端 fixture 三处同一形状。
- 替代：给旧路由加 query 参数。否决：query 参数不进 URL 模板占位符体系，前端 `required_placeholders` 契约要改两套。
- 缓存：file-cache key 与 `source_version` 加入 `source:cycle`；`NATIONAL_DISCHARGE_QUERY_VERSION` → `fair-network-budget-v5`。`_layer_source_refs` discharge 断言不动（目录条目仍短路）。

### D2. 起报时次 = 活动河网交集，fail-closed
- `national_discharge_cycles(session, source)`：对每个 active 河网收集该源 display-ready run 的 `cycle_time` 集合，取交集；任一河网集合为空则整体为空。返回 `cycles[]`（降序）+ `default_cycle`（最新）。
- `national_discharge_valid_times(session, source, cycle)`：从 `cycle` 起 3h 步到该周期各河网 `river_valid_time_end` 最小值；57 项在现 `MVT_VALID_TIME_SAMPLE_LIMIT=100` 内。无参调用保持旧行为（兼容 `metadata.valid_times` 与旧脚本）。
- 替代：并集。否决：用户拍板 fail-closed，避免部分流域无色却看似正常。

### D3. 降水累积在 node-27 服务端求和，跨周期切片规则唯一
- `resolve_window(source, cycle, valid_time, available_cycles)`：窗口截止时刻 T ∈ {valid_time−21h, …, valid_time}（8 个）；每个 T 取同源、`C ≤ min(请求周期, T−3h)` 的**最近**已镜像周期 C，`lead = T−C`。请求周期是**上界**：只允许用请求周期或更早周期（决策 5/9），镜像里出现更新的周期也不改变解析结果——这既让「窗口落在预报时效内」场景自洽（此时 `min(...)` 就是请求周期本身），也让 PNG 缓存 key 确定（只要被选中的 `≤ 请求周期` 的周期仍在镜像里，结果不变；剪枝由 canonical-precip-copyback 的 keep 水位与 PNG 缓存同水位剪枝兜底）。`C ≤ T−3h` 保证 lead ≥ 3，GFS 无 f000 不构成缺口。
- **路由三元组 → 镜像路径**只用两条规则：source 先过 `{gfs, ifs}` 枚举（422 早于任何文件系统/归一化调用，`normalize_source_id` 也接受 `ERA5`），再经 `packages/common/source_identity.py::normalize_source_id` 得存储 source（`ifs`→`IFS`、`gfs`→`gfs`）；cycle 的 RFC3339 实例渲染成目录 token `%Y%m%d%H`（同 `workers/canonical_converter/converter.py::format_cycle_time`）。切片文件 `canonical/<S>/<K>/prcp_rate_or_amount/<S>_<K>_prcp_rate_or_amount_f<lead:03d>.nc`，grid `canonical/<S>/grid/<grid_id>/grid.json`（`gfs_0p25` / `ifs_0p25`）。
- 替代：只用本周期、lead<24h 显示部分累积。否决：部分窗口是错误数值，且与「lead=0 默认」冲突。
- 累积：Σ(rate_i × 3/24) → mm/24h；实现用 `netCDF4` + `numpy`，不引入 xarray 路径（display API 进程内不依赖 cfgrib）。

### D4. PNG 由 numpy + zlib 直接写调色板 PNG，Web-Mercator 重采样
- 输出宽 1316 px（4×329），高按 Mercator 纵横比取整；对每个输出像素中心反算 lon/lat，对 0.25° 场做 bilinear；再按六级阈值映射到 8-bit 调色板索引（索引 0 = 透明）。PNG 写入为手写 IHDR/PLTE/tRNS/IDAT（zlib）+ CRC，约 60 行，无 Pillow。
- 替代 A：SVG/矢量等值线。否决：需 contour 算法与新依赖，且渲染代价在客户端。替代 B：MVT 格点多边形。否决：74025 格点 × 57 步瓦片体量远大于单张 PNG。替代 C：EPSG:4326 直出交给 MapLibre 拉伸。否决：`image` source 在 Mercator 中线性映射，56° 纬度跨度会把雨带错位数十公里。
- 缓存：`NHMS_MVT_FILE_CACHE_DIR/precip/<storage_source>/<cycle_token>/<valid_time>.<palette_version>.png`，其中 `<storage_source>/<cycle_token>` 与镜像的 `canonical/<S>/<K>` **逐字节同名**（`IFS/2026090212`），`<valid_time>` 用秒精度 RFC3339；tmp+rename；生成耗时 100 ms 量级，不加跨 worker 互斥。同名是为了让 retention 按名字一一对应地同步剪 PNG 缓存（D6）。

### D5. 降水在前端是布尔叠加，不是 `M11Layer` 枚举值
- `M11QueryState.precip: boolean`（默认 true）；约束落在导出面：`parseM11QueryState` 读 `precip=0` 为 false、其余为 true，`serializeM11QueryState` 的白名单在 false 时产出 `precip=0`。实现上要动私有 `queryParamsFromState`（现在丢掉所有 false 布尔，会让 `serializeM11QueryState` 内部那趟 parse 归一把 false 吃回 true）并把 `precip` 加进白名单；其它布尔行为不变。目录 `precip` 条目（`layer_type: meteorology`, `tile_format: png`）提供 `image_url_template` / `index_url_template` / `bounds` / `legend`，前端 `normalizeLayerStates` 的 `requiredLayers` 仍只含 `discharge`。
- 渲染：`M11PrecipOverlayPrimitive` 用 MapLibre `image` source（四角 = bbox 四角）+ `raster` layer，`raster-opacity 0.55`，插入全国河网层之下；切换 valid_time 时更新 url。当前时次不在 `index.valid_times` 内则隐藏并提示。index 返回 404 `PRECIP_CYCLE_NOT_MIRRORED` 时用**另一条**文案（该周期无降水镜像），两种隐藏原因在 UI 与 vitest 里可区分。流域详情里 source 可能是 `best`/`compare`：只用解析出的具体 gfs/ifs 拼 URL，解析不出就隐藏并给原因，绝不请求 `/api/v1/precip/best|compare/...`。

### D6. copyback 镜像不阻塞 q_down 发布
- `publisher.py` 在 q_down copyback 之后镜像 `canonical/<source>/<cycle>/prcp_rate_or_amount/` 与 `canonical/<source>/grid/<grid_id>/grid.json`，复用 temp-tree + rollback；目标存在且大小一致则跳过；源缺失记 lineage `precip_mirror: failed` 并继续。
- 回填脚本 `scripts/canonical_precip_copyback_backfill.py` 只依赖 `shutil`/`pathlib`，node-22 用钉住解释器执行。
- retention：`node27_raw_retention.py` 目标集合扩到 `canonical/<storage_source>/<cycle_token>` 与 `NHMS_MVT_FILE_CACHE_DIR/precip/<storage_source>/<cycle_token>`，同一 cutoff（`display_watermark − retention_days`）；`canonical/<source>/grid/` 永不剪。脚本配置的 source 是小写 `gfs,ifs`，canonical 目录是 `gfs`/`IFS`，路径必须过 `normalize_source_id` 而不是直接拼小写 token。
- keep 水位必须覆盖 cycles 端点能返回的全部周期再往前 24h（`oldest_listed_cycle − 24h ≥ cutoff`），receipt 记录不等式两边；不成立就加大 `retention_days`，不靠前端 404 兜底。

### D7. 河网密度两段式，坐标数为硬门
- 阈值表：z≤4→4，z5→3，z6→2，z7→1，z≥8→1，写在 `postgis_tile_sql("river-network-national")` 返回的**单条 SQL 字符串**里的一个 `CASE`（zoom 是 bind `:z`，不是 Python 参数）；`NATIONAL_RIVER_NETWORK_QUERY_VERSION` → `stream-type-aggregate-v3`（版本在 `national_river_network_source_version` 的字符串里，不在 SQL 文本里）。`hydro-national` 里形状相同的那份 CASE 不动。**注意该 CASE 现在写在 `river-network` 与 `river-network-national` 共用的 `source_cte` 里**（`if layer in {"river-network", "river-network-national"}` 分支）——就地改字面量会顺带把单流域 `river-network` 层 z7 从 Type≥2 放宽到 Type≥1（密集流域单 z7 瓦片本就 >50k 坐标），所以 v3 表只在 `layer == "river-network-national"` 时生成，单流域层保持 v2，并有断言锁住。
- go/no-go 用 `prefilter_stats.feature_coordinate_count`（单要素最大坐标数）+ `feature_coordinate_overflow_count == 0`，`budget_stats.coordinate_count` 只作附加项——后者只累加通过单要素上限的要素，恰好在合并干流超限被过滤成空瓦片时读数偏低。实测 zoom 集合扩到 z3/z4/z6/z7（v3 让 z6 多出 Type 2、z7 多出 Type 1）。这些列是 tile SQL 的输出列，用 psql 直接跑 SQL 读，不走瓦片 HTTP 路由。
- 前端 `m11NationalRiverPaint`：`dimmed` 折扣改为 zoom 插值（z<6 不折扣），z3–5 线宽 stops 上调（Type 4 在 z3 ≥1.4px；Type 5 在 z3 >1.5px、z5 >2.3px，严格高于现值），并给 z6 的 Type 2、z6/z7 的 Type 1 补非零 opacity——现表 z7 的 match 只列 Type 5..2、无 z6 stop，新增的这两类会被取到却画成透明。

### D8. 时间轴控制条复用 `M11Timeline`
- 底部居中玻璃条：起报时次 `<select>` + GFS/IFS 分段 + `M11Timeline`。`pickCurrentValidTime` 默认改为首项（lead=0）。全国尺度 URL `source=best` 解析为 `gfs`。流域详情模式共用控制条，周期来自该流域 run 列表、时次来自 run metadata（现有逻辑）。
- 浮层位移：图例/返回按钮 `bottom-4`→`bottom-24`，notices `bottom-20`→`bottom-40`。

### D9. 预热有界
- `node27_mvt_prewarm.py`：**逐源**按 `cycles?source=` 取该源自己的最新周期（两源最新周期可能不同），对该周期的全部 valid_times 预热 z3–4 流量瓦片 + 降水 PNG；河网 z3–5 不变。某源 cycles 为空则该源零请求，不借用另一源周期、不按墙钟伪造。总请求数与耗时写 receipt。
- 现有 `tests/test_node27_mvt_prewarm.py` 钉住旧无源 URL 与单 valid_time 签名，随本改动一起更新。

### D10. API 时间实例只有一种拼写

- 全部对外时间实例用 `YYYY-MM-DDTHH:MM:SSZ`（秒精度、无小数秒、字面 `Z`），即 `services/tiles/mvt.py::canonical_mvt_time` 现在就产出的形状；覆盖路径段（`{cycle}`/`{valid_time}`）、`valid_times[]`/`cycles[]` 列表、降水 index 与 PNG 缓存文件名。
- 路由**接受**带小数秒或 `+00:00` 的 RFC3339，但入 SQL bind / 缓存 key / ETag 前先归一到上述拼写——否则同一时刻两种拼写会写出两份 PNG 缓存，D3 的确定性就是空话。
- 前端 `normalizeIsoInstant` 归一出的是毫秒形（`2026-09-02T12:00:00.000Z`），所以替换 `{cycle}`/`{valid_time}` 和与 `valid_times[]` 比对前必须转成秒精度形；这是 `buildM11RegisteredOverlay` 与降水 URL 的共同前置。

### D11. OpenAPI 手工维护

- `openapi/nhms.v1.yaml` **没有生成器**，`tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` 按 `app.openapi()` 等值比对，所以 4 条新路由（hydro-national source/cycle、discharge cycles、precip index、precip png）与 `/api/v1/layers`、`valid-times` 的形状变化必须手写进 yaml。
- `INTERNAL_ROUTE_REASONS` 只放宽「公共路由对齐」那条测试，**不是**等值比对的豁免（slurm 路由同时在 yaml 里就是证据），本改动不新增 allowlist 条目。
- `apps/api/openapi_patching.py::_patch_mvt_tile_openapi` 用一个硬编码的 `mvt_paths` 元组给 `.pbf` 路由注入 424 响应、`q_down` 变量枚举与 z/x/y 收窄后的 `maximum`；新的 source/cycle 全国路由是新的 path key，**必须追加进该元组**，否则运行时 schema 与手写 yaml 都缺 424/枚举/上限——两边一致地缺，等值比对不会失败，缺陷是静默的文档缺失（mvt-tile-contract 要求该路由 424 fail-closed 有显式 OpenAPI 行为）。
- 之后 `pnpm generate:api` 刷 `apps/frontend/src/api/types.ts`，`pnpm check:api-types` 验证。

## Sketch seams under test

- `postgis_tile_sql("river-network-national")` / `postgis_tile_sql("hydro-national")` 返回的 SQL 字符串（`tests/test_hydro_display_mvt_scaling.py` 已有 seam；签名仍是 `postgis_tile_sql(layer: str) -> str`，source/cycle/zoom 都是 bind `:source`/`:cycle`/`:z`）——阈值表、`lower(h.source_id) = :source`/`h.cycle_time = :cycle` 在 CTE **与身份探针**两处、版本号经 `national_river_network_source_version` 在最高的现成边界锁定。
- `national_discharge_cycles` / `national_discharge_valid_times(source, cycle)` 用 fake session rows（现有 valid-times 测试风格）——交集 fail-closed、3h 步长、非矩形覆盖一次覆盖。
- `services/precip` 三个纯函数 `resolve_window` / `accumulate_24h` / `render_png` + `TestClient` 路由用 tmp 镜像目录——一个 seam 覆盖跨周期取片、GFS 无 f000、窗口不完整 404、PNG 头/尺寸/调色板。
- 发布 copyback 用 tmp roots（`tests/test_tile_publisher.py` 风格）——镜像成功、幂等跳过、缺文件不阻塞发布。
- 前端 vitest：`queryState` 往返（`source/cycle/precip=0`）、`buildM11RegisteredOverlay` 用 per-cycle 列表、timeline view model 默认 lead=0、`SiteHeader` 标题——纯函数与组件浅渲染，不起 MapLibre。

## Risks / Trade-offs

- [NFS 容量随周期增长] → retention 同水位剪枝；receipt 记 `df -h /home`；总量 ≈ 2 源 × 26 周期 × 23 MB。
- [冷瓦片维度乘 2×N] → 预热限定 z3–4；查询版本换代后首周期记录冷/热耗时。
- [最老周期窗口不完整] → fail-closed 404 + 前端提示，不渲染部分窗口。
- [目录契约 BREAKING] → 旧路由别名保留；`test_api_contract`/M11Shell fixture 随 spec 同步。
- [浏览器多次切换时次的 PNG 请求] → 单张 PNG 调色板约 20–80 KB，HTTP 缓存头同 MVT。

## Migration Plan

1. 后端合并部署 node-27（新路由、cycles、precip、目录条目）；旧路由无感。
2. node-22 合并后执行一次回填脚本（钉住解释器），下一次 scheduler pass 起自动镜像。
3. node-27 跑 prewarm，产出 receipt（河网 z3/z4/z6/z7 的 `feature_coordinate_count`/`feature_coordinate_overflow_count`、瓦片冷热、PNG 耗时、预热请求总数、`df -h / /home` 与 `NHMS_MVT_FILE_CACHE_DIR` 所在卷、keep 水位不等式）。
4. 前端部署；回滚 = 回退前端 + 目录条目（旧路由仍在）。

## Open Questions

无（14 条分支均已拍板）。
