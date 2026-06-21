## Why

`GET /api/v1/met/stations/{station_id}/series` 当前走 `PsycopgForecastStore.station_series()` → `_ensure_forcing_version_finalized()` 强校验 `met.forcing_version.checksum` 非空非 "pending"。node-27 现状：

- 最新 cycle (2026-06-20 12:00) 4 行 `met.forcing_version` 都 `checksum IS NULL`，最新 cycle 系列读全部 HTTP 409 `FORCING_VERSION_NOT_FINALIZED`
- 老 cycle (2026-06-01) `checksum` 写入完整，API 200 + 真数据
- 但 `${OBJECT_STORE_ROOT}/forcing/{src}/{cycle}/{bv}/{model}/shud/X{lon}Y{lat}.csv` 物理 forcing package **就在 disk 上**（forcing_producer 已写盘但 finalize step 没写 checksum）

业务方诉求："文件已经在 disk 就应该能调到，不绕 DB"。

CMFD ingest 已被撤销（commit `ef234f2` revert `1a0c87f`；Epic #614 + 子 #615-#620 全部 close not planned），方向纠偏至本 change。

## Station 粒度上下文（重要约束，非本 spec 修改对象）

- heihe `met_station` 1709 行、qhh `met_station` 386 行不是 bug，是 **rSHUD/AutoSHUD legacy `idw` 模式的代站约定**（`docs/forcing数据处理流程与rSHUD一致性说明.md:13, 257`）
- rSHUD 建模阶段通过 `ForcingCoverage` Voronoi/Thiessen 把 mesh 三角元覆盖到 0.1° per-cell 代站，覆盖关系固化进 `.sp.att FORC` 列；运行期不重算
- forcing_producer 当前 `legacy idw` 路径：把 IFS/GFS 0.25° 原始网格 IDW 到这 1709/386 个 0.1° SHUD 代站，每站写一个 `shud/X{lon}Y{lat}.csv`（heihe shud/ 1709 文件 + qhh shud/ 386 文件，1:1 对应 `met_station.properties_json.forcing_filename`）
- 数据值层面有冗余（多个 0.1° SHUD 代站从同一 0.25° IFS/GFS 源 cell IDW 出非常相近值），但**这是 forcing_producer + rSHUD legacy 设计层面的事**，迁移到 `direct_grid` 模式（station 表换成 IFS/GFS 0.25° 源网格站，heihe 估 ~250 站）由 `openspec/changes/direct-grid-forcing/` 单独 rollout
- **本 spec 不动 station 表数据、不动 forcing_producer、不动 rSHUD 覆盖约定**，仅在读侧把 1709/386 个 shud/CSV 接进 API

## Display role boundary 上下文（本 spec 必须主动调整）

- 现状：`apps/api/runtime_mode.py:27-32` 把 `OBJECT_STORE_ROOT` 列入 `_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS`；`display_boundary_blockers()` (`runtime_mode.py:176-202`) 在启动时检查，若 display.env 内含该 key 即 raise `DISPLAY_BOUNDARY_CONFIG_UNSAFE`，display API 拒启动
- 同等约束在 `tests/test_role_boundary_static.py:19-27 DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS` + line 89 与 `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` 互锁
- 设计原因：早期 display 不应触碰 compute 侧的对象存储路径，避免越权读 compute artifact
- 本 change 主动从禁用清单移除 `OBJECT_STORE_ROOT`：display 现在合法需要读 forcing CSV（disk-only 路径），属于 display 业务面对外只读输出；同步更新 `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` + `tests/test_role_boundary_static.py`，并在 design.md AD-11 文档化该 boundary 调整的理由

## What Changes

- 新增 reader 模块 `packages/common/object_store_forcing.py` 直读 `${OBJECT_STORE_ROOT}/forcing/{source}/{cycle_compact}/{basin_version_id}/{model_id}/shud/{forcing_filename}` 物理 CSV，按 `Time_Day` 列 + cycle_time 计算 `valid_time`
- 改造 `apps/api/routes/data_sources.py:111` `/met/stations/{station_id}/series` 路由：切走 `_ensure_forcing_version_finalized()` finalize gate 和 `met.forcing_station_timeseries` 数据读取；station_id → forcing_filename 仍走 `met.met_station` 单表 lookup（这是合法且必要的 DB 查询）
- 移除 `_ensure_forcing_version_finalized` 在 series 路径上的调用（其他端点不动）
- `met_station.properties_json.forcing_filename` 直接作为文件名解析键（heihe 1709 + qhh 386 = 2095 SHUD per-cell 代站 100% 覆盖；数量级与 0.1° 粒度一致）
- 修改 `apps/api/runtime_mode.py`：从 `_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS` 移除 `OBJECT_STORE_ROOT`；在 `RuntimeConfig` 加 `object_store_root: Path | None` 字段；`load_runtime_config()` 内校验 env 存在且目录可读，否则 raise `RuntimeModeError`
- 同步更新 `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` + `tests/test_role_boundary_static.py:19-27` 移除 `OBJECT_STORE_ROOT`
- `infra/env/display.example` 模板新增 `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store` 一行（注释式默认值；实际 `display.env` 在 node-27 由 ops 配置，gitignored 不入仓）
- 新错误码 `STATION_FORCING_FILE_NOT_FOUND` (404) + `STATION_FORCING_FILENAME_MISSING` (500) + `STATION_FORCING_FILE_MALFORMED` (500) —— 加入 OpenAPI examples 列表；旧 `FORCING_VERSION_NOT_FOUND` / `FORCING_VERSION_NOT_FINALIZED` 在该路径上不再产生
- `STATION_NOT_FOUND` (404) + `MISSING_REQUIRED_FILTER` (422) **复用** `packages/common/forecast_store.py:909, 2099-2104, 2132-2146` 已有错误码定义，shape 和 details 不变

## Capabilities

- `object-store-station-series-read`

## Impact

**Affected code:**
- `packages/common/object_store_forcing.py` (NEW)
- `packages/common/forecast_store.py` (`station_series` 不再被 series 路由调；**保留实现**作为其他路径或日后回退使用；复用其 `STATION_NOT_FOUND` + `MISSING_REQUIRED_FILTER` 错误码 shape)
- `apps/api/routes/data_sources.py` (`get_met_station_series` 切换调用)
- `apps/api/runtime_mode.py` (移除 `OBJECT_STORE_ROOT` 从 `_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS`；扩 `RuntimeConfig` 加 `object_store_root`；`load_runtime_config` 内 fail-fast validation)
- `apps/api/main.py` (将 `load_runtime_config()` 调用结果中的 `object_store_root` 注入到依赖注入容器或 `app.state`)
- `tests/test_role_boundary_static.py` (同步 forbidden env keys 集合)
- `infra/env/display.example` (新增 `OBJECT_STORE_ROOT=` 模板行)
- `openapi/nhms.v1.yaml` (新增 `STATION_FORCING_FILE_NOT_FOUND` / `STATION_FORCING_FILENAME_MISSING` / `STATION_FORCING_FILE_MALFORMED` 加入 `getMetStationSeries` operation 的 error examples；保留 200 `StationSeriesResponse` schema 不变)
- `apps/api/main.py:_patch_station_series_openapi` (新增 4 个错误 code 到 error envelope examples 列表)

**Affected docs:**
- `CLAUDE.md` (技术栈/拓扑速查表添加 forcing object-store 一行)
- `docs/runbooks/object-store-forcing-series-read.md` (NEW runbook)
- `docs/forcing数据处理流程与rSHUD一致性说明.md` (在末尾附 API 直读 disk 段)

**Not affected:**
- `workers/forcing_producer/` 完全不动
- `met.met_station` schema 与行数据完全不动（1709/386 代站粒度保持）
- `met.forcing_version` / `met.forcing_station_timeseries` DB schema 不变；这两张表行内容也不动
- `/api/v1/met/stations`（list）、`/api/v1/tiles/met-stations`（MVT）端点契约不变
- 前端 `apps/frontend/` 代码本次不动
- forcing_producer 写入路径（不修 finalize step；DB 仍写 forcing_version 行，新接口忽略它）

## Out of Scope

- **不查 `met.forcing_version` 和 `met.forcing_station_timeseries`**：series 路径 disk-only。station_id → forcing_filename 的 `met.met_station` 单表 lookup 仍允许且必要
- **不 fallback DB**：disk 上拿不到 cycle 直接 404，不去 `forcing_station_timeseries` 兜底；老 cycle 历史回看能力本次主动丢弃，留 follow-up issue 评估"是否需要单独的 long-term forcing series API 走 DB"
- **不修 forcing_producer**：不补 checksum finalize step，不改 publish 顺序；本 change 仅在读侧绕开 finalize gate
- **不删 DB read path**：`PsycopgForecastStore.station_series` 保留实现+测试，仅断开 series 路由的调用；潜在 cleanup 留 follow-up
- **不扩 basin**：只覆盖 heihe + qhh（disk + DB 都只这两个 basin 的 forcing 数据；其余 basin 留 forcing_producer 扩展工作单独 issue）；基底仍由 met_station 数据驱动，新 basin 若入库 reader 会按相同模板尝试读盘（hit → 200，miss → 404）
- **不动 station 粒度**：1709/386 SHUD per-cell legacy 代站约定本 spec 不改；切到 IFS/GFS 0.25° 源网格站（~250 个）的 `direct_grid` 模式 rollout 留 `openspec/changes/direct-grid-forcing/` 或独立 issue
- **不动前端**：cycle picker / source 选择器 / station 列表点击逻辑保持现状；老 cycle 在前端可能仍可选但调用必 404，前端适配留 follow-up issue
- **不引入 S3 boto3 客户端**：现 `NHMS_ARTIFACT_BACKEND=local` + disk 直读已满足；S3/MinIO 客户端方案留未来 issue
- **不改 `_ensure_forcing_version_finalized` 函数本身**：只断 series 路径调用；其他路径（如 `station_summary` 等）若仍需校验则不受影响
- **不引入缓存**：每次请求直读小型 SHUD CSV（当前 IFS/GFS 样本为 53/56 行量级），后续如有性能问题再加 LRU
- **不动 `met.data_source` / `met.forcing_version` schema 或行内容**
- **本 spec 在 disk-path 层引入 source_id lowercase 归一化（IFS→ifs, GFS→gfs），与现有 forecast_store 内 `LOWER(source_id)` 查询语义对齐；API 入参大小写约定本身不变，归一化的统一抽取交给后续 normalization issue**
- **不验证 IDW 数据值冗余 / source-grid 重合度**：1709 SHUD 代站从 ~250 个 IFS/GFS 0.25° 源 cell IDW 而来，存在数据值冗余，这是 forcing_producer 上游设计层面的事，本 spec 不做去重也不做对比
- **不写新 `STATION_FORCING_FILENAME_INVALID` 错误码**：PR-A reader 仍做 path safety hardening；API-controlled `source_id`/`model_id` unsafe path segment 用既有 `VALIDATION_ERROR` 拒绝，station metadata 中 unsafe `basin_version_id` / `forcing_filename`、symlink/no-follow、bounded-read violation、malformed CSV 均用 `STATION_FORCING_FILE_MALFORMED` 拒绝

## Forward Compatibility Invariant（direct_grid 模式自动切换条件）

direct_grid 模式（IFS/GFS 0.25° 原生格点当 SHUD 输入站，跳过 IDW 0.1° 插值；rollout 见 `openspec/changes/direct-grid-forcing/`）切换时，本 change 的 reader **0 改、自动生效** 的前提是 forcing_producer 输出保持以下 7 项 SHUD-format CSV invariant：

1. **路径模板**：`${OBJECT_STORE_ROOT}/forcing/{src}/{cycle}/{bv}/{model}/shud/{forcing_filename}`（保留 `shud/` 子目录段）
2. **文件命名**：`X<lon>Y<lat>.csv`（lon/lat 数值由 0.1° 代站坐标改成 0.25° 源网格坐标即可；模板不变）
3. **CSV header**：`nrow ncol start_date end_date` 4 token，制表符或空白分隔
4. **数据列**：固定 6 列、名字不变 `Time_Day Precip Temp RH Wind RN`
5. **时间编码**：`Time_Day` 列为 decimal day-from-cycle（0 = cycle 起点，0.125 = +3h，6.5 = +6d12h）
6. **单位映射**：Precip mm/day, Temp degC, RH 0-1, Wind m/s, RN W/m²（reader 据此输出 `unit` 字段）
7. **station_id → forcing_filename 查询**：仍走 `met.met_station.properties_json.forcing_filename` 单表 lookup（reader 通过 `StationLookup` Protocol 注入，不感知 station 表行数/粒度）

满足以上 7 项 → direct_grid 切换工作仅限：
- forcing_producer 切换输入源（0.1° IDW 中间结果 → 0.25° 原生格点直接当 SHUD 输入站）
- `met.met_station` 表新增 ~250 行 direct_grid station（新 `station_id` / lon / lat / `forcing_filename`），可与旧 1709/386 行通过 station_id 命名约定 + 新 `model_id` 隔离共存
- reader / `packages/common/object_store_forcing.py` / 19 个 unit tests / spec.md 全部 **0 改**

任一打破（换列名、加列、换时间编码、跳 `shud/` 子目录、走非 SHUD producer pipeline），reader 必须 fork 一个 parser dispatch（按 `model_id` 选 SHUD-CSV vs 新格式），**另起 spec + PR**。

**direct-grid-forcing change 启动前的硬门**：先核对上 7 项 invariant，确认 forcing_producer 切换计划全部沿用 SHUD producer pipeline 输出契约；任一不一致则直接升级为"reader 改造 + parser dispatch" scope，不能按本 spec 的"0 改自动切换"承诺执行。
