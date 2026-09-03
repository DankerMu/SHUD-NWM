# 全国展示 V2.0：标题/河网密度/降水叠加/时间轴与起报时次

> 状态：**设计定稿（Stage 1 oracle）**，2026-09-03。本文件是 `stage-change-pipeline` Stage 3/4.5 的判定基准，change 文件向本文件对齐，不反向改写本文件来消 finding。对应 OpenSpec change：`display-v2-national-timeline-precip-overlay`。

## Goal

在不改动 node-22 计算链路与流量产品语义的前提下，把 `https://test.nwm.ac.cn` 的全国展示升级为：

1. 标题「全国水文模拟系统（V2.0）」加粗放大，赞助商图放大。
2. 全国比例尺下静态河网更密、更显眼。
3. 叠加「过去 24h 累积降水」栅格图层（气象局六级色标），不显著拖慢加载。
4. 底部时间轴 + 起报时次选择 + GFS/IFS 源开关，全国流量与降水按同一 `source/cycle/valid_time` 同步渲染。

## 前提纠正（用户原始假设 vs 代码事实）

| 用户假设 | 代码事实 | 证据 |
|---|---|---|
| 河段颜色用 lead=0 | 默认渲染的是 100h 滚动窗口的**最后一个**有效时次，且不带起报时次 | `apps/frontend/src/lib/m11/overviewDataContracts.ts:1173` `pickCurrentValidTime` 取末项；`services/tiles/mvt.py:1544-1640` `national_discharge_valid_times` 只返回 `common_end` 前 100 个整点 |
| 气象代站图层已有降水，渲染即可 | 代站要素只有 `station_id/name/basin_id`，降水值按站逐 CSV 读取，没有格点场 | `apps/frontend/src/pages/m11/useStationLayer.ts:38-56`；`apps/api/routes/data_sources.py:139` 逐站 series |
| 全国瓦片按当前源渲染 | 全国流量 SQL 不看 source，每个河网按 `cycle_time DESC, run_id DESC` 取一个 run；同一周期 gfs/IFS 各 38 个 run，同图混源 | `services/tiles/mvt.py:654,692` `latest_runs` CTE |

## 实机事实（2026-09-03 核查）

- 38 个 active 流域；周期 00Z/12Z，每天 2 个，gfs 与 IFS 同周期各 38 run；lead 0–167h（3h 步长，57 步）。
- canonical `prcp_rate_or_amount`：每 3h 步一份 `.nc`，值为**该步平均雨强 mm/day**（`workers/canonical_converter/converter.py:725-800` 去累积后乘 `24/step_hours`）；74025 点 = 225 lat × 329 lon，0.25°，bbox 63–145E / 8–64N，`grid.json` 的 `latitudes` 降序。GFS 无 f000 降水（56 个文件 f003–f168）；IFS 有 f000。每源每周期约 23 MB。
- canonical 只存在于 node-22 本地 `/scratch/frd_muziyao/nhms-prod/object-store/canonical/{gfs,IFS}/<cycle>/prcp_rate_or_amount/`（各 26 个周期）；NFS `/home/ghdc/nwm/object-store/` 下**没有** `canonical/`。
- node-27 venv：netCDF4 1.7.4 + numpy 可用；cfgrib/eccodes 不可用；无 Pillow。display API :8080 进程 cwd `/home/nwm/NWM`。
- NFS 卷是 node-27 的 `/home`（1.7 TB，与 PG 数据同卷），镜像 canonical 的保留必须有界。
- `hydro.hydro_run.source_id` 引用 `met.data_source(source_id)`，实际值 `gfs` / `IFS`（大小写不一致，路由层要做大小写无关映射）。

## 决策清单（grill 门禁，最终共识）

| # | 分支 | 结论 | 决定者 |
|---|---|---|---|
| 1 | 标题/header/赞助商 | 「全国水文模拟系统（V2.0）」全角括号，加粗放大；header 高度可抬高；赞助商图放大 | user |
| 2 | 交付节奏 | 前后端一轮做完，不拆两轮 | user |
| 3 | 全国起报时次列表 | 各活动河网交集，fail-closed（任一河网缺该周期 display-ready run 则不列） | user |
| 4 | 时间轴 | 3h 步长，覆盖 lead 0–167h 全范围；**默认停在 lead=0** | user |
| 5 | 降水图层 | 跟随水文源（同 source/cycle/valid_time），默认开，URL 可关 | user |
| 6 | 降水图例 | 气象局 24h 六级（mm/24h）：<0.1 透明；0.1–10 淡绿；10–25 绿；25–50 蓝；50–100 深蓝；100–250 紫；≥250 深紫。图层透明度 0.55 | user |
| 7 | 降水数据路径 | node-22 发布 copyback 镜像 canonical 降水 `.nc` + `grid.json` 到 NFS；node-27 display API 渲染 Web-Mercator PNG，文件缓存下发 | user |
| 8 | 全国数据源 | 显式 GFS/IFS 开关，默认 GFS；全国尺度不再提供 Best Available；全国瓦片新路由带 `{source}/{cycle}`，旧无源路由保留为别名 | user |
| 9 | 降水累积窗口 | **过去 24h**（8 个 3h 切片求和），窗口前段跨起报时次从同源更早周期取切片 | user |
| 10 | 静态河网密度 | 后端 `stream_type` 阈值每级下移一档 + 查询版本递增；前端 z<6 不打折并加粗；node-27 z3/z4 坐标数 <50k 为 go/no-go | fact-check |
| 11 | 降水在前端的形态 | 布尔开关（URL `precip=0` 关），不是 `M11Layer` 枚举值 | fact-check |
| 12 | 预热范围 | 只预热 z3–4 × 双源 × 最新周期全部 57 步 + 全部降水 PNG；更深缩放按需 | fact-check |
| 13 | 镜像保留 | canonical 镜像纳入 `node27_raw_retention.py` 同水位剪枝 | fact-check |
| 14 | 回填 | 一次性回填现存保留周期（node-22 执行，钉住解释器） | fact-check |

开放项：无。

## 技术设计

### A. 标题与赞助商（前端，本地验证）

- `apps/frontend/src/components/layout/SiteHeader.tsx`：标题文案改为 `全国水文模拟系统（V2.0）`，`text-[22px] font-bold` → `text-[28px] font-extrabold tracking-wide`；header `h-[68px]` → `h-[84px]`；赞助商 `h-9` → `h-14`（sponsors.png 1013×170，等比 83px 宽度充裕）。
- 唯一引用点，无现有测试断言标题文本；新增一条 vitest 断言标题文案与全角括号。

### B. 静态河网密度

后端 `services/tiles/mvt.py` river-network-national（~350–450）`stream_type` 阈值表：

| zoom | 现值 | 新值 |
|---|---|---|
| ≤4 | 5 | 4 |
| 5 | 4 | 3 |
| 6 | 3 | 2 |
| 7 | 2 | 1 |
| ≥8 | 1 | 1 |

- `NATIONAL_RIVER_NETWORK_QUERY_VERSION` `stream-type-aggregate-v2` → `-v3`（缓存换代）。
- z≤8 仍按 network×Type 合并要素，风险在**单要素坐标数**；go/no-go：node-27 对覆盖中国的 z3/z4 瓦片实测 `coordinate_count`（改前/改后），全部 < `MVT_MAX_COORDINATES=50_000` 才允许合并；超限则该级阈值回退一档并记录 receipt。
- 前端 `apps/frontend/src/components/map/m11MapPrimitives.tsx:149-197` `m11NationalRiverPaint`：`dimmed` 折扣仅在 zoom ≥6 生效（用 `interpolate` 表达式而不是常量 `opacityScale`）；z3–5 线宽 stops 上调（主干 Type≥4 在 z3 ≥1.4px、z5 ≥2.2px）。

### C. 全国流量瓦片带 source/cycle

- 新路由 `GET /api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf`，`source` 枚举 `gfs|ifs`（路由层小写），SQL 用 `lower(h.source_id) = :source AND h.cycle_time = :cycle` 过滤 `latest_runs`；`cycle` RFC3339。
- 旧路由 `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` 保留、行为不变（别名，仅供外部旧链接）。
- 缓存 key / ETag：`source_version` 与 file-cache key 加入 `source:cycle`；`NATIONAL_DISCHARGE_QUERY_VERSION` `fair-network-budget-v4` → `-v5`。
- `/api/v1/layers` 的 `discharge` 目录条目 `tile_url_template` 改为 `/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf`，`required_placeholders = [source, cycle, valid_time]`：
  - `metadata.default_source="gfs"`、`metadata.default_cycle=<最新交集周期>`、`metadata.valid_times` = 默认源默认周期的列表。`_layer_source_refs` 的 discharge 断言保持。
- 受影响断言：`tests/test_api_contract.py:1409`、`tests/test_hydro_display_mvt_scaling.py:175`、`apps/frontend/src/pages/__tests__/M11Shell.test.tsx` 的 `dischargeNationalMvtMetadata` fixture——随 spec MODIFIED 一起改。

### D. 起报时次与有效时次

- `GET /api/v1/layers/discharge/cycles?source=gfs|ifs` → `{source, cycles:[{cycle_time, valid_time_start, valid_time_end}], default_cycle}`。交集语义：周期入列当且仅当**每个** active 河网在该源该周期有 `segment_count>0` 的 display-ready run；空则 `cycles=[]`，前端显示禁用态（fail-closed，不退化到单流域）。
- `GET /api/v1/layers/discharge/valid-times?source=&cycle=`：从 `cycle` 起按 3h 步长到该周期各河网 `river_valid_time_end` 的最小值（57 项，落在现有 100 上限内）。无 `source/cycle` 时保持现行为（默认源、默认周期）。
- 前端陷阱（必须落 tasks）：`apps/frontend/src/components/map/m11MapBuilders.ts` `buildM11RegisteredOverlay` 目前用 `metadataHasValidTime(metadata, validTime)` 校验，而 metadata 只带默认周期列表——切非默认周期会得到 `overlay=null`。改为用 store 里按 `(source,cycle)` 拉取的列表校验。

### E. 降水叠加（过去 24h）

**数据路径（node-22 → NFS）**

- `services/tile_publisher/publisher.py` 在 q_down publish 成功后新增 canonical 降水镜像：`canonical/<source>/<cycle>/prcp_rate_or_amount/*.nc` + `canonical/<source>/grid/<grid_id>/grid.json` → `NHMS_OBJECT_STORE_COPYBACK_ROOT` 同 keyspace：
  - 复用 `_copyback_object_tree_with_rollback` 的 temp-tree + rollback
  - 幂等（目标已存在且大小一致则跳过）
  - 缺源文件 fail loudly 但**不阻塞** q_down 发布（降水是展示附属，写 lineage 记录失败）。
- 回填脚本 `scripts/canonical_precip_copyback_backfill.py`：一次性镜像现存保留周期，node-22 用 `/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill`，只用 `shutil`，无新依赖，不触发 `uv sync`。
- 保留：`scripts/node27_raw_retention.py` 扩展目标到 `canonical/<source>/<cycle>`，同一 keep 水位；`grid/` 不剪。

**渲染（node-27 display API）**

- 新模块 `services/precip/`：
  - `resolve_window(source, cycle, valid_time, available) -> list[Slice]`：对窗口内每个截止时刻 T（`valid_time-21h … valid_time`，8 个），取同源、**最近的**满足 `C ≤ T-3h` 的已镜像周期 C，`lead = T-C`；任一切片无文件 → `PrecipWindowIncomplete`（fail-closed）。
  - `accumulate_24h(slices) -> ndarray[225,329]`：Σ(rate_i × 3/24)，单位 mm/24h。
  - `render_png(field, grid, palette) -> bytes`：先按 Web-Mercator 重采样（输出宽 1316 px = 4×329，高按 Mercator 纵横比；bilinear），再分级到六级索引色，用 **numpy + zlib 写 8-bit 调色板 PNG**（无新依赖；<0.1 透明）。
  - 文件缓存 `NHMS_MVT_FILE_CACHE_DIR/precip/<source>/<cycle>/<valid_time>.<palette_version>.png`，tmp+rename 原子写；HTTP 缓存头同 MVT。
- 路由：
  - `GET /api/v1/precip/{source}/{cycle}/index` → `{source, cycle, window_hours:24, unit:"mm/24h", bounds:[63,8,145,64], image_size:[w,h], legend:[{min,max,color,label}], palette_version, valid_times:[…仅窗口完整的时次…]}`。
  - `GET /api/v1/precip/{source}/{cycle}/{valid_time}.png` → `image/png`；窗口不完整 404 `PRECIP_WINDOW_INCOMPLETE`；周期未镜像 404 `PRECIP_CYCLE_NOT_MIRRORED`。
  - `/api/v1/layers` 新增 `precip` 条目（`layer_type: meteorology`, `tile_format: png`, metadata 含 `image_url_template`、`index_url_template`、`bounds`、`legend`），使 `map-layer-timeline-controls` 的「未实现气象图层禁用」场景对降水改为「已实现」。
- 前端：`M11PrecipOverlayPrimitive`（MapLibre `image` source + `raster` layer，`raster-opacity 0.55`，`raster-resampling linear`，置于全国河网层之下）；`queryState` 新增 `precip: boolean`（默认 true，序列化 `precip=0` 表示关，注意现有 `queryParamsFromState` 只在 true 时序列化布尔）；图例面板叠加降水六级；当前时次不在 `index.valid_times` 内时降水层隐藏并在时间轴旁提示「该时次降水窗口不完整」。

### F. 时间轴 / 起报时次 / 源开关（前端）

- 在 `OverviewPage.tsx` 的 `M11FullscreenMap` 壳底部居中挂载玻璃风格控制条：起报时次 `<select>`（来自 cycles 端点，默认最新）、GFS/IFS 分段开关（默认 GFS，替代 Best）、复用 `apps/frontend/src/pages/m11/M11Controls.tsx:318` `M11Timeline`（播放/暂停/倍速/上一步/下一步，刻度标 `+{lead}h` 与有效时刻）。
- URL 状态：`source=gfs|ifs`（全国尺度 `best` 解析为 `gfs`）、`cycle`、`validTime`、`precip`；`pickCurrentValidTime` 默认改为**首项**（lead=0）。
- 现有浮层位移：图例 `bottom-4` → `bottom-24`，返回按钮同步，notices `bottom-20` → `bottom-40`。
- 流域详情模式沿用同一控制条：周期选项来自该流域 `/api/v1/runs` 的 run 列表，有效时次来自 run metadata（现有逻辑）。

### G. 预热

- `scripts/node27_mvt_prewarm.py`：新增按 `cycles` 端点发现最新周期，对 `gfs`/`ifs` × 全部 57 步预热 z3–4 全国流量瓦片 + 全部降水 PNG；河网 z3–5 预热不变。实测总数写入 receipt，作为容量上界。

## Sketch seams under test

- `postgis_tile_sql("river-network-national", …)` 返回的 SQL 字符串 + node-27 实测 z3/z4 `coordinate_count`：已有 seam（`tests/test_hydro_display_mvt_scaling.py`），阈值表与版本号在此锁定。
- `postgis_tile_sql("hydro-national", …, source, cycle)` SQL 含 `lower(h.source_id)` 与 `cycle_time` 绑定：同上已有 seam。
- `national_discharge_cycles(session, source)` / `national_discharge_valid_times(session, source, cycle)`：fake session rows（现有 valid-times 测试风格），覆盖交集 fail-closed、3h 步长、非矩形覆盖。
- `services/precip` 三个纯函数（`resolve_window` / `accumulate_24h` / `render_png`）+ TestClient 路由用 tmp 镜像目录：最高且最少的 seam，覆盖跨周期取切片、GFS 无 f000、窗口不完整 404、PNG 头/尺寸/调色板。
- 发布 copyback：`tests/test_tile_publisher.py` tmp roots 风格，覆盖镜像成功、幂等跳过、缺文件不阻塞发布。
- 前端 vitest：`queryState` 往返（`source/cycle/precip=0`）、`buildM11RegisteredOverlay` 用 per-cycle 列表、timeline view model 默认 lead=0、`SiteHeader` 标题。

## 验证路由

| 改动 | oracle |
|---|---|
| 标题/图例/URL 状态 | 本地 tsc + vitest |
| 河网阈值、hydro-national source/cycle 瓦片、cycles/valid-times、降水 PNG、前端浏览器 e2e | node-27 live receipt（`docs/runbooks/receipts/`） |
| copyback 镜像 + 回填 | node-22：一次 scheduler pass 或回填脚本（`.venv/bin/python`，禁止 `uv sync` / 裸 `uv run`） |
| ruff / openspec validate | 本地 |

## 风险与缓解

- **NFS 容量**：每源每周期 23 MB × 2 源 × 保留 26 周期 ≈ 1.2 GB，剪枝纳入 retention；receipt 记录 `df -h /home`。
- **冷瓦片放大**：新增 source×cycle 维度使冷路径乘 2×N；预热限定 z3–4，深缩放按需；`NATIONAL_DISCHARGE_QUERY_VERSION` 换代后首个周期需 receipt 记录冷/热耗时。
- **降水跨周期取数在最老周期缺切片**：fail-closed 404 + 前端提示，不渲染部分窗口。
- **旧链接**：旧全国路由保留别名，`_layer_source_refs` 断言不变。

## Not in scope

- 12h 累积、其它气象变量（气温/风）、代站降水值渲染、Best Available 的全国解析、node-22 Slurm/SHUD 链路、DB schema 变更、CLDAS/ERA5。

## 顺带发现（不在本 change 修）

- node-22 `infra/env/compute.env` 的 `OBJECT_STORE_ROOT=/ghdc/data/nwm/workspace/22-e2e/object-store` 目录不存在（stale）；生产根实为 `/scratch/frd_muziyao/nhms-prod/object-store`。
- node-22 / node-27 两个 venv 的 eccodes 均不可用（GLIBCXX / 找不到库）。
- node-27 :8081 是另一个项目（`yd-NWM`），与 CLAUDE.md 的 8080 口径无冲突但易混淆。
