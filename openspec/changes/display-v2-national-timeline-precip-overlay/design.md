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
- 新路由 `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`；`source` 为 `gfs|ifs`（路由小写），SQL `lower(h.source_id) = :source AND h.cycle_time = :cycle`。旧 5 段路由保留、行为不变（段数不同无路由歧义）。
- 替代：给旧路由加 query 参数。否决：query 参数不进 URL 模板占位符体系，前端 `required_placeholders` 契约要改两套。
- 缓存：file-cache key 与 `source_version` 加入 `source:cycle`；`NATIONAL_DISCHARGE_QUERY_VERSION` → `fair-network-budget-v5`。`_layer_source_refs` discharge 断言不动（目录条目仍短路）。

### D2. 起报时次 = 活动河网交集，fail-closed
- `national_discharge_cycles(session, source)`：对每个 active 河网收集该源 display-ready run 的 `cycle_time` 集合，取交集；任一河网集合为空则整体为空。返回 `cycles[]`（降序）+ `default_cycle`（最新）。
- `national_discharge_valid_times(session, source, cycle)`：从 `cycle` 起 3h 步到该周期各河网 `river_valid_time_end` 最小值；57 项在现 `MVT_VALID_TIME_SAMPLE_LIMIT=100` 内。无参调用保持旧行为（兼容 `metadata.valid_times` 与旧脚本）。
- 替代：并集。否决：用户拍板 fail-closed，避免部分流域无色却看似正常。

### D3. 降水累积在 node-27 服务端求和，跨周期切片规则唯一
- `resolve_window(source, cycle, valid_time, available_cycles)`：窗口截止时刻 T ∈ {valid_time−21h, …, valid_time}（8 个）；每个 T 取同源、`C ≤ T−3h` 的**最近**已镜像周期 C，`lead = T−C`，文件 `canonical/<src>/<C>/prcp_rate_or_amount/<src>_<C>_prcp_rate_or_amount_f<lead:03d>.nc`。lead 0 的窗口全部来自前 1–2 个周期，因此 GFS 无 f000 不构成缺口。
- 替代：只用本周期、lead<24h 显示部分累积。否决：部分窗口是错误数值，且与「lead=0 默认」冲突。
- 累积：Σ(rate_i × 3/24) → mm/24h；实现用 `netCDF4` + `numpy`，不引入 xarray 路径（display API 进程内不依赖 cfgrib）。

### D4. PNG 由 numpy + zlib 直接写调色板 PNG，Web-Mercator 重采样
- 输出宽 1316 px（4×329），高按 Mercator 纵横比取整；对每个输出像素中心反算 lon/lat，对 0.25° 场做 bilinear；再按六级阈值映射到 8-bit 调色板索引（索引 0 = 透明）。PNG 写入为手写 IHDR/PLTE/tRNS/IDAT（zlib）+ CRC，约 60 行，无 Pillow。
- 替代 A：SVG/矢量等值线。否决：需 contour 算法与新依赖，且渲染代价在客户端。替代 B：MVT 格点多边形。否决：74025 格点 × 57 步瓦片体量远大于单张 PNG。替代 C：EPSG:4326 直出交给 MapLibre 拉伸。否决：`image` source 在 Mercator 中线性映射，56° 纬度跨度会把雨带错位数十公里。
- 缓存：`NHMS_MVT_FILE_CACHE_DIR/precip/<source>/<cycle>/<valid_time>.<palette_version>.png`，tmp+rename；生成耗时 100 ms 量级，不加跨 worker 互斥。

### D5. 降水在前端是布尔叠加，不是 `M11Layer` 枚举值
- `M11QueryState.precip: boolean`（默认 true）；序列化 `precip=0` 表示关（现 `queryParamsFromState` 只在 true 时序列化布尔，需要显式处理）。目录 `precip` 条目（`layer_type: meteorology`, `tile_format: png`）提供 `image_url_template` / `index_url_template` / `bounds` / `legend`，前端 `normalizeLayerStates` 的 `requiredLayers` 仍只含 `discharge`。
- 渲染：`M11PrecipOverlayPrimitive` 用 MapLibre `image` source（四角 = bbox 四角）+ `raster` layer，`raster-opacity 0.55`，插入全国河网层之下；切换 valid_time 时更新 url。当前时次不在 `index.valid_times` 内则隐藏并提示。

### D6. copyback 镜像不阻塞 q_down 发布
- `publisher.py` 在 q_down copyback 之后镜像 `canonical/<source>/<cycle>/prcp_rate_or_amount/` 与 `canonical/<source>/grid/<grid_id>/grid.json`，复用 temp-tree + rollback；目标存在且大小一致则跳过；源缺失记 lineage `precip_mirror: failed` 并继续。
- 回填脚本 `scripts/canonical_precip_copyback_backfill.py` 只依赖 `shutil`/`pathlib`，node-22 用钉住解释器执行。
- retention：`node27_raw_retention.py` 目标集合扩到 `canonical/<source>/<cycle>`，同 keep 水位；`canonical/<source>/grid/` 永不剪。

### D7. 河网密度两段式，坐标数为硬门
- 阈值表：z≤4→4，z5→3，z6→2，z7→1，z≥8→1；`NATIONAL_RIVER_NETWORK_QUERY_VERSION` → `stream-type-aggregate-v3`。node-27 实测覆盖中国的 z3/z4 瓦片 `coordinate_count`，任一 ≥50k 则该级回退一档并写 receipt。
- 前端 `m11NationalRiverPaint`：`dimmed` 折扣改为 zoom 插值（z<6 不折扣），z3–5 线宽 stops 上调。

### D8. 时间轴控制条复用 `M11Timeline`
- 底部居中玻璃条：起报时次 `<select>` + GFS/IFS 分段 + `M11Timeline`。`pickCurrentValidTime` 默认改为首项（lead=0）。全国尺度 URL `source=best` 解析为 `gfs`。流域详情模式共用控制条，周期来自该流域 run 列表、时次来自 run metadata（现有逻辑）。
- 浮层位移：图例/返回按钮 `bottom-4`→`bottom-24`，notices `bottom-20`→`bottom-40`。

### D9. 预热有界
- `node27_mvt_prewarm.py`：按 cycles 端点取最新周期，对 gfs/ifs × 57 步预热 z3–4 流量瓦片 + 降水 PNG；河网 z3–5 不变。总请求数写 receipt。

## Sketch seams under test

- `postgis_tile_sql("river-network-national", …)` / `postgis_tile_sql("hydro-national", …, source, cycle)` 的 SQL 字符串（`tests/test_hydro_display_mvt_scaling.py` 已有 seam）——阈值表、`lower(h.source_id)`/`cycle_time` 绑定、版本号在最高的现成边界锁定。
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
3. node-27 跑 prewarm，产出 receipt（河网坐标数、瓦片冷热、PNG 耗时、`df -h /home`）。
4. 前端部署；回滚 = 回退前端 + 目录条目（旧路由仍在）。

## Open Questions

无（14 条分支均已拍板）。
