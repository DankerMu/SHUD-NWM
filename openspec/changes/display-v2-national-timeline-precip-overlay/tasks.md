## 1. Header brand (national-overview-page)

- [ ] 1.1 `apps/frontend/src/components/layout/SiteHeader.tsx`：标题改为 `全国水文模拟系统（V2.0）`，`text-[28px] font-extrabold tracking-wide`，header `h-[84px]`，赞助商 `h-14`；检查 `AppShell.tsx` 对 header 高度的布局假设（地图区高度/偏移）。
- [ ] 1.2 vitest：断言标题文案（全角括号）、bold 类与赞助商尺寸类。

Evidence Floor：本地 `pnpm test`（SiteHeader 用例）+ `pnpm build`。

## 2. National river density (national-river-density)

- [ ] 2.1 `services/tiles/mvt.py` river-network-national：阈值表改为 z≤4→4 / z5→3 / z6→2 / z7→1 / z≥8→1；`NATIONAL_RIVER_NETWORK_QUERY_VERSION = "stream-type-aggregate-v3"`。
- [ ] 2.2 `tests/test_hydro_display_mvt_scaling.py`：更新 SQL 形状断言为新阈值与 v3 版本。
- [ ] 2.3 `apps/frontend/src/components/map/m11MapPrimitives.tsx` `m11NationalRiverPaint`：`dimmed` 折扣改为 zoom 插值（z<6 不折扣），z3–5 主干线宽 stops 上调；vitest 断言 line-opacity/line-width 表达式在 z3/z5/z6 的取值。
- [ ] 2.4 node-27 go/no-go：用新 SQL 对覆盖中国的 z3/z4 瓦片实测 `coordinate_count`（改前/改后），全部 <50 000 才保留 v3 表；否则该级回退一档并把数值写入 receipt。

Evidence Floor：本地 `uv run pytest tests/test_hydro_display_mvt_scaling.py -q`；node-27 receipt `docs/runbooks/receipts/<date>-national-river-density.md` 含逐瓦片坐标数。

## 3. National discharge tiles with source/cycle (mvt-tile-contract)

- [ ] 3.1 `services/tiles/mvt.py` hydro-national：`postgis_tile_sql` 增加 `source`/`cycle` 绑定，`latest_runs` 加 `lower(h.source_id) = :source AND h.cycle_time = :cycle`；`NATIONAL_DISCHARGE_QUERY_VERSION = "fair-network-budget-v5"`；file-cache key 与 `source_version` 纳入 `source:cycle`。
- [ ] 3.2 `apps/api/routes/hydro_display.py`：新路由 `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`（`source` 枚举 `gfs|ifs`，`cycle` RFC3339 校验，422 先于 SQL）；旧 5 段路由保留不动。
- [ ] 3.3 `_NATIONAL_DISCHARGE_METADATA` / `_default_layer_catalog`：`tile_url_template` 改为新模板，`required_placeholders = [source, cycle, valid_time]`，metadata 增加 `default_source="gfs"`、`default_cycle`、`cycles_url_template`、`valid_times_url_template`；`metadata.valid_times` 为默认源默认周期列表。
- [ ] 3.4 `national_discharge_cycles(session, source)`：交集 fail-closed；`national_discharge_valid_times(session, source=None, cycle=None)`：给定 source+cycle 时从 cycle 起 3h 步到各河网最小 `river_valid_time_end`，无参保持旧行为。
- [ ] 3.5 路由 `GET /api/v1/layers/discharge/cycles?source=`；`GET /api/v1/layers/discharge/valid-times` 增加 `source`/`cycle` query（cycle 无 source → 422）；`display_catalog_cached` key 纳入 source/cycle。
- [ ] 3.6 测试：fake-session 覆盖交集排除部分周期、某河网无 run 则空、57 项 3h 步长、非矩形覆盖；`tests/test_api_contract.py:1409` 与 `test_hydro_display_mvt_scaling.py:175` 模板断言更新；`_layer_source_refs` discharge 断言用例保留。
- [ ] 3.7 OpenAPI / `pnpm check:api-types` 重新生成前端类型，drift allowlist 同步。

Evidence Floor：本地 `uv run pytest tests/test_hydro_display_mvt_scaling.py tests/test_api_contract.py -q`；node-27 真实 DB：`curl` 新路由 gfs/ifs 同一 cycle/valid_time 各一张 z4 瓦片（ETag 不同、字节非空）、旧路由仍 200、cycles 端点返回交集列表、valid-times 返回 57 项；receipt 记录冷/热耗时。

## 4. Canonical precipitation copyback (canonical-precip-copyback)

- [ ] 4.1 `services/tile_publisher/publisher.py`：q_down copyback 后新增 `_copyback_canonical_precip(source, cycle)`——镜像 `canonical/<source>/<cycle>/prcp_rate_or_amount/*.nc` + `canonical/<source>/grid/<grid_id>/grid.json`，复用 `_copyback_object_tree_with_rollback`，同大小跳过，缺源记 lineage `precip_mirror: failed` 不抛出。
- [ ] 4.2 `tests/test_tile_publisher.py`：tmp roots 覆盖成功镜像、幂等 skipped、缺源不阻塞发布。
- [ ] 4.3 `scripts/canonical_precip_copyback_backfill.py`（仅标准库）：`--source-root/--copyback-root/--dry-run`，JSON 汇总；单测用 tmp 目录。
- [ ] 4.4 `scripts/node27_raw_retention.py`：目标集合扩到 `canonical/<source>/<cycle>`，同 keep 水位，`canonical/<source>/grid/` 永不剪；更新其测试。
- [ ] 4.5 node-22 执行回填：`/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root /scratch/frd_muziyao/nhms-prod/object-store --copyback-root $NHMS_OBJECT_STORE_COPYBACK_ROOT`（禁止 `uv sync` / 裸 `uv run`）；node-27 `ls /home/ghdc/nwm/object-store/canonical/{gfs,IFS}` + `df -h /home` 写 receipt。

Evidence Floor：本地 `uv run pytest tests/test_tile_publisher.py tests/test_node27_raw_retention.py -q`（后者按实际测试文件名）；node-22 回填 JSON 汇总 + node-27 目录清单与 `df -h /home` receipt。

## 5. Precipitation raster service (precipitation-raster-overlay, backend)

- [ ] 5.1 新模块 `services/precip/`：`resolve_window(source, cycle, valid_time, mirror_root)`（最近 `C ≤ T−3h` 规则，缺片抛 `PrecipWindowIncomplete`）、`accumulate_24h(slices)`（netCDF4 + numpy，Σ rate×3/24）、`render_png(field, grid, palette)`（Mercator 重采样 1316 px 宽、bilinear、六级调色板、numpy+zlib 手写 PNG）、`palette_version`。
- [ ] 5.2 文件缓存 `NHMS_MVT_FILE_CACHE_DIR/precip/<source>/<cycle>/<valid_time>.<palette_version>.png`，tmp+rename；HTTP 缓存头同 MVT。
- [ ] 5.3 路由 `apps/api/routes/precip.py`：`GET /api/v1/precip/{source}/{cycle}/index`、`GET /api/v1/precip/{source}/{cycle}/{valid_time}.png`；422（source 非法）、404 `PRECIP_CYCLE_NOT_MIRRORED` / `PRECIP_WINDOW_INCOMPLETE`；镜像根来自 `NHMS_OBJECT_STORE_COPYBACK_ROOT`（node-27 视角路径）。
- [ ] 5.4 `/api/v1/layers` 新增 `precip` 条目（`layer_type: meteorology`, `tile_format: png`, metadata `image_url_template`/`index_url_template`/`bounds`/`legend`/`window_hours`/`unit`）；`SUPPORTED_PUBLIC_LAYER_IDS` 与 valid-times 路由对 `precip` 返回空列表（时次由 index 提供）。
- [ ] 5.5 测试 `tests/test_precip_overlay.py`：tmp 镜像目录合成小 `.nc`，覆盖 lead-0 跨周期 8 片解析（GFS 无 f000）、horizon 内本周期解析、缺片 404、PNG 结构（签名/IHDR/PLTE 7 项/tRNS/IDAT）、阈值下闭区间、36°N 行位置公式、缓存命中不读 NetCDF、index 只列完整窗口。
- [ ] 5.6 OpenAPI 与前端类型再生成。

Evidence Floor：本地 `uv run pytest tests/test_precip_overlay.py -q`；node-27：`curl` index 与一张 PNG（`file` 判定 PNG、尺寸 1316×H），冷生成耗时写 receipt。

## 6. Frontend query state, overlay builder, timeline bar (frontend-mvt-layer-consumption + map-layer-timeline-controls + precipitation-raster-overlay 前端)

- [ ] 6.1 `apps/frontend/src/lib/m11/queryState.ts`：新增 `precip: boolean`（默认 true，`precip=0` 序列化/解析）；`source` 在全国尺度 `best`→`gfs` 的解析放在 selection 层；`M11Layer` 保持 `'discharge'`。
- [ ] 6.2 store：按 `(source, cycle)` 缓存 cycles 与 valid-times；默认周期取 `metadata.default_cycle`，非默认周期请求 `/api/v1/layers/discharge/valid-times?source=&cycle=`；precip index 按 `(source, cycle)` 缓存。
- [ ] 6.3 `m11MapBuilders.ts` `buildM11RegisteredOverlay`：用 store 的 per-cycle 列表校验 validTime，替换 `{source}`/`{cycle}` 占位符，source key 纳入 source+cycle；`pickCurrentValidTime` 默认首项（lead 0）。
- [ ] 6.4 `M11PrecipOverlayPrimitive`（`image` source + `raster` layer，opacity 0.55，linear，置于全国河网层下；url 随三元组更新；当前时次不在 index 内则隐藏）。
- [ ] 6.5 底部控制条：`OverviewPage.tsx` `M11FullscreenMap` 挂载玻璃风格条——起报时次 `<select>`、GFS/IFS 分段（全国无 Best/对比）、复用 `M11Timeline`（刻度 `+{lead}h` + 有效时刻）；cycles 为空时禁用并提示；流域详情模式周期来自 run 列表。
- [ ] 6.6 `M11FloatingControls.tsx`：降水开关进图层面板气象组（默认开）；图例叠加六级降水；图例/返回按钮 `bottom-24`，notices `bottom-40`；窗口不完整提示。
- [ ] 6.7 vitest：queryState 往返（`precip=0`、source/cycle）、`buildM11RegisteredOverlay` per-cycle 列表与 URL 替换、timeline view model 默认 lead 0、`M11Shell.test.tsx` fixture 改为新模板与 `required_placeholders`、控制条禁用态。

Evidence Floor：本地 `pnpm test && pnpm build && pnpm check:api-types`；node-27 浏览器 e2e receipt（`/` 首屏：标题、河网、降水层、控制条默认 lead 0；切换 IFS/周期/时次后瓦片与 PNG URL 变化；`precip=0` 隐藏）。

## 7. Prewarm and deployment receipt

- [ ] 7.1 `scripts/node27_mvt_prewarm.py`：通过 cycles 端点发现最新周期，对 gfs/ifs × 全部 valid-times 预热 z3–4 全国流量瓦片与降水 PNG；河网 z3–5 不变；输出请求总数与耗时。
- [ ] 7.2 node-27 部署：`git pull --ff-only`、重启 display API、跑 prewarm、产出 receipt（河网坐标数、瓦片冷热耗时、PNG 耗时、`df -h / /home`、浏览器截图）。
- [ ] 7.3 文档：`docs/runbooks/` 补降水镜像与回填步骤；`openspec/project-profile.md` 若入口/契约变化则更新。

Evidence Floor：node-27 receipt `docs/runbooks/receipts/<date>-display-v2.md`。
