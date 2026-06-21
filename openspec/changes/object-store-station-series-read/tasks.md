## 0. Pre-implementation introspection (HARD GATE — must commit findings to `design.md §Introspection Results` or a new `introspection-findings.md` before §1 opens)

- [ ] 0.1 OQ1: SSH node-27, scan IFS + gfs × heihe + qhh × available cycles × 抽 5 shud/CSV，确认 header `nrow ncol start_date end_date` 格式跨 cycle 一致；记 row count 范围（§0 实测 IFS=53、GFS=56）
- [ ] 0.2 OQ2: 列举 `/home/ghdc/nwm/object-store/forcing/{ifs,gfs}/` 下所有目录名，确认全是 `YYYYMMDDHH` 10 位格式
- [ ] 0.3 OQ3: 确认 `met_station.basin_version_id` 取值集合（heihe + qhh）与 disk 目录名 100% 对应
- [ ] 0.4 OQ4: confirm OBJECT_STORE_ROOT 只读权限即可（reader 不写）；不要求 write
- [ ] 0.5 OQ5: Read `apps/api/main.py` + grep `app.state`/`Depends` 现有注入模式；决定 reader 拿 `object_store_root` 和 `station_lookup` connection 用 `app.state.X` 还是 `Depends(get_X)`；commit 决定到 design.md
- [ ] 0.6 测试 layout 决定：扁平 `tests/test_object_store_forcing*.py`（不开 `tests/packages/common/` 子目录，保持与现有 test 风格一致）
- [ ] 0.7 §0 introspection 全部结论写进**仓内已 commit 文件**——`design.md` 新增 `## Introspection Results` 段，或新建 `openspec/changes/object-store-station-series-read/introspection-findings.md`；**不**写 PR body（PR body 不进 repo history）
- [ ] 0.8 OQ6: grep `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` 确认实际位置（`tests/test_role_boundary_static.py:89` import 的源文件）；commit 三处 boundary fix 清单到 design.md AD-11
- [ ] 0.9 OQ7: 读 `tests/test_forecast_store_product_quality_sql.py` 看现有 psycopg.Connection mock 模式；决定 reader unit test 注入策略选 (a) `Protocol`-typed `StationLookup` (AD-13 推荐) 或 (b) `psycopg.Connection` mock；commit 决定到 design.md
- [ ] 0.10 200 response baseline capture：在 node-27 curl 老 cycle (2026-06-01T00:00:00Z) heihe + IFS 200，把响应 JSON 保存为 `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json`（脱敏 request_id 等）作为新 reader 输出 byte-shape 比对 oracle

## 1. Reader module — `packages/common/object_store_forcing.py`

- [ ] 1.1 typed errors:
  - `ObjectStoreForcingError(Exception)` 基类
  - `StationForcingFilenameMissingError(code='STATION_FORCING_FILENAME_MISSING', status_code=500)` (new)
  - `StationForcingFileNotFoundError(code='STATION_FORCING_FILE_NOT_FOUND', status_code=404)` (new)
  - `StationForcingFileMalformedError(code='STATION_FORCING_FILE_MALFORMED', status_code=500)` (new)
  - 不重定义 `STATION_NOT_FOUND` 和 `MISSING_REQUIRED_FILTER`：reader 内 lookup 失败时 raise 与 `forecast_store.py:2099-2104, 909, 2132-2146` 同 code + 同 details shape 的 typed error；建议复用 `ForecastStoreError` 子类或导出常量保持单一来源
- [ ] 1.2 `_normalize_source_id(source_id: str) -> str` lowercase + 验证非空（None 由 422 在 route 层处理）
- [ ] 1.3 `_compute_cycle_compact(cycle_time: datetime) -> str` 强制 UTC 转换 + `YYYYMMDDHH` 格式；naive datetime 当 UTC 处理
- [ ] 1.4 `StationLookup` Protocol + `PsycopgStationLookup` 实现：
  - Protocol: `def lookup(self, station_id: str) -> StationMetadata`
  - psycopg 实现：单 PG 查询 `SELECT properties_json, basin_version_id, station_name, ST_X(geom) AS lon, ST_Y(geom) AS lat, elevation_m, station_role, active_flag FROM met.met_station WHERE station_id = $1`
  - station 不存在 raise 与 `STATION_NOT_FOUND` 同 code + `{station_id}` details（参见 §1.1）
  - properties_json 不含 `forcing_filename` raise `StationForcingFilenameMissingError`
- [ ] 1.5 `_resolve_disk_path(object_store_root: Path, source_normalized, cycle_compact, basin_version_id, model_id, forcing_filename) -> Path`
- [ ] 1.6 `_parse_csv_header(line1: str) -> CMFDHeader-like` 提取 nrow / start_date / end_date
- [ ] 1.7 `_parse_csv_data(rows: Iterable[str], cycle_time: datetime) -> Iterator[(variable, valid_time, value)]`，按列名映射 (`Precip→PRCP, Temp→TEMP, RH→RH, Wind→wind, RN→Rn`)，按 `int(round(Time_Day*86400))` 算 valid_time（注意 round 不是 int 截断）
- [ ] 1.8 `_apply_filters(tuples, variables, from_time, to_time, limit) -> list[tuple]`：variables filter 静默丢弃未知（Press / UnknownVariable 均 drop，不 raise）；from/to inclusive；limit 截断总 tuple count（不是 per-variable）；保持排序 `[PRCP, TEMP, RH, wind, Rn]` 然后 valid_time ascending
- [ ] 1.9 `read_station_forcing_csv(*, station_lookup, object_store_root, station_id, source_id, cycle_time, model_id, variables=None, from_time=None, to_time=None, limit=None) -> StationSeriesResponse` 主入口（Protocol 注入）
- [ ] 1.10 输出符合 `StationSeriesResponse` schema (`openapi/nhms.v1.yaml:2873`)：`data.station` (来自 station_lookup) + `data.series[].variable+unit+points[].valid_time+value` (来自 CSV) + `data.metadata.{returned_points, truncated, returned_from, returned_to}`
- [ ] 1.11 每个 series 项的 `unit` 字段按 AD-5 输出：`PRCP="mm/day", TEMP="degC", RH="0-1", wind="m/s", Rn="W/m^2"`
- [ ] 1.12 文件 OPEN 异常（PermissionError / OSError / FileNotFoundError）转 typed error：
  - FileNotFoundError → `StationForcingFileNotFoundError`
  - PermissionError / OSError → `StationForcingFileMalformedError`
- [ ] 1.13a unit test: path resolution（heihe + IFS happy path）
- [ ] 1.13b unit test: cycle UTC 归一化 3 种输入（naive / `+00:00` / `+08:00` 都得到 `2026062012`）
- [ ] 1.13c unit test: station not found → 404 + `{station_id}`
- [ ] 1.13d unit test: forcing_filename missing → 500 `STATION_FORCING_FILENAME_MISSING`
- [ ] 1.13e unit test: file not found → 404 `STATION_FORCING_FILE_NOT_FOUND` + details 含 expected_path + basin_version_id + source_id + cycle_time + model_id
- [ ] 1.13f unit test: malformed CSV 6 变体（缺 header / nrow 非数字 / data 行 column 数错 / 数值非数字 / 空 file / declared nrow 与实际 data 行数不一致）→ 500 `STATION_FORCING_FILE_MALFORMED`
- [ ] 1.13g unit test: 变量名映射全表 + unit 字段全表（5 个变量）
- [ ] 1.13h unit test: valid_time 边界 — 第一行 Time_Day=0 → cycle；最后一行 Time_Day=6.5 → cycle + 6d12h
- [ ] 1.13i unit test: rounding — Time_Day=0.041666 → cycle + 3600s（不是 3599s）
- [ ] 1.13j unit test: variables filter 单变量（PRCP）
- [ ] 1.13k unit test: variables=Press → 200 + `data.series=[]`
- [ ] 1.13l unit test: variables=PRCP,Press → 200 + 只含 PRCP（Press 静默 drop）
- [ ] 1.13m unit test: variables=UnknownVariable → 200 + `data.series=[]`
- [ ] 1.13n unit test: from/to filter inclusive 两端
- [ ] 1.13o unit test: from > to → 200 + `data.series=[]`
- [ ] 1.13p unit test: limit=10 截断总 tuple count + `metadata.truncated=true` + 排序保持
- [ ] 1.13q unit test: 默认 tuples = 5×N，参数化覆盖 N=1、N=53 (IFS shape)、N=56 (GFS shape)、N=100
- [ ] 1.13r unit test: response shape 对比 §0.10 baseline fixture — 字段名/字段类型/排序与 baseline 一致（除 `request_id` 和 series points 内的真实数值外）
- [ ] 1.13s unit test: SQL 查询统计 — 用 spy connection/cursor 驱动 `PsycopgStationLookup`，调一次完整 `read_station_forcing_csv` 后断言 cursor.execute 恰好 1 次命中 `met.met_station`，对 `met.forcing_version` / `met.forcing_station_timeseries` 的 SELECT 次数 = 0（覆盖 spec.md "verify met.forcing_version SELECT count = 0 during series request" 场景）
- [ ] 1.13t unit test: side-effect-free reads — 用 tmp_path CSV 连续调用 reader 3 次，断言 response shape 稳定、CSV mtime 不变，并通过 monkeypatch/spy 证明 reader 不调用 `mkdir` / 写模式 `open(..., "w")`
- [ ] 1.14 `ruff check packages/common/object_store_forcing.py tests/test_object_store_forcing.py` PASS
- [ ] 1.15 PR-A-scoped `openspec validate object-store-station-series-read --strict --no-interactive` PASS — §0 introspection commits 与 design.md/introspection-findings.md 编辑可能破坏 spec 结构，PR-A merge 前必须本地通过 validate（§8.1 是 PR-B 完整重跑；本条是 PR-A 独立 guard）

## 2. Series route 切换 — `apps/api/routes/data_sources.py`

- [ ] 2.1 import `read_station_forcing_csv` + 4 个 typed errors (new) + 复用 existing `ForecastStoreError` for STATION_NOT_FOUND / MISSING_REQUIRED_FILTER
- [ ] 2.2 `get_met_station_series` 函数体替换：不再调 `store.station_series()`，改调 `read_station_forcing_csv(station_lookup=..., object_store_root=app.state.object_store_root, ...)`
- [ ] 2.3 reader 异常 → API HTTP error mapping：`StationForcingFileNotFoundError → 404`、`StationForcingFilenameMissingError → 500`、`StationForcingFileMalformedError → 500`；已有 `STATION_NOT_FOUND` / `MISSING_REQUIRED_FILTER` 维持原 `ForecastStoreError` 路径
- [ ] 2.4 保留 query params 解析（不动 signature）
- [ ] 2.5 `forcing_version_id` query param 在新路径下：与 cycle_time 同时传时静默忽略；单独传（无 cycle_time/model_id/source_id）触发 `MISSING_REQUIRED_FILTER` 422
- [ ] 2.6 旧 `ForecastStoreError(FORCING_VERSION_NOT_FINALIZED)` / `FORCING_VERSION_NOT_FOUND` 的 try-except 块在该路由上不再出现（reader 不会 raise 这两个）
- [ ] 2.7 API-level mocked test 在 `tests/test_forecast_api.py` 或新文件 `tests/test_forecast_api_met_station_series.py`：通过 `FastAPI TestClient` + `Depends` override 注入 `FakeStationLookup` + tmp_path fixture 文件，验证 4 个 typed error 的 HTTP 映射 + 验证 route 不再 import 或调用 `_ensure_forcing_version_finalized` / `station_series` + 显式 case `forcing_version_id=X` 单独传（无 `cycle_time`/`model_id`/`source_id`）→ 422 `MISSING_REQUIRED_FILTER` + `forcing_version_id=X` 与 `cycle_time` 同时传 → 200（静默忽略）

## 3. Startup env check + boundary fix — `apps/api/runtime_mode.py` + `apps/api/main.py`

- [ ] 3.1 在 `RuntimeConfig` (`apps/api/runtime_mode.py:66`) 加字段 `object_store_root: Path | None = None`
- [ ] 3.2 在 `load_runtime_config(env)` (`apps/api/runtime_mode.py:101`) 内：
  ```python
  raw = env.get("OBJECT_STORE_ROOT", "").strip()
  if not raw:
      raise RuntimeModeError("OBJECT_STORE_ROOT env var is required")
  path = Path(raw).expanduser().resolve()
  if not path.is_dir() or not os.access(path, os.R_OK):
      raise RuntimeModeError(f"OBJECT_STORE_ROOT={path} is not a readable directory")
  ```
- [ ] 3.3 `apps/api/main.py:create_app()` (line 304+) 把 `runtime_config.object_store_root` 设到 `app.state.object_store_root`（或 `Depends(get_object_store_root)` provider，按 §0.5 决定）
- [ ] 3.4 **AD-11 boundary fix**：从下面三处同步移除 `OBJECT_STORE_ROOT`：
  - `apps/api/runtime_mode.py:27-32 _DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS` 元组
  - `tests/test_role_boundary_static.py:19-27 DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS`
  - `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS`（位置由 §0.8 introspection 确认）
- [ ] 3.5 unit test (`tests/test_runtime_mode.py` 现有 + 新加)：`load_runtime_config` 缺 env → raise `RuntimeModeError`；env 指向不存在路径 → raise；env 指向真实目录 → 通过 + `RuntimeConfig.object_store_root == Path(...)`
- [ ] 3.6 `tests/test_role_boundary_static.py` 跑通——同步后 forbidden 集合不再包含 OBJECT_STORE_ROOT，互锁断言 (line 89) 仍 pass

## 4. OpenAPI schema — `openapi/nhms.v1.yaml` + `apps/api/main.py:_patch_station_series_openapi`

- [ ] 4.1 `_patch_station_series_openapi` (line 564) 把 4 个 error code 加入 4xx/5xx 响应的 examples 列表：`STATION_FORCING_FILENAME_MISSING (500)`, `STATION_FORCING_FILE_NOT_FOUND (404)`, `STATION_FORCING_FILE_MALFORMED (500)`, `MISSING_REQUIRED_FILTER (422)`；保留现有 `STATION_NOT_FOUND` 404 example 不变
- [ ] 4.2 移除（或不再列出）`FORCING_VERSION_NOT_FOUND` / `FORCING_VERSION_NOT_FINALIZED` 在 `getMetStationSeries` operation examples 中的出现
- [ ] 4.3 `getMetStationSeries` operation 的 `forcing_version_id` query parameter 加 `deprecated: true` + description 说明新路径下该参数被忽略
- [ ] 4.4 `pnpm run check:api-types`（如存在）PASS — 验证 OpenAPI 改动对前端 type-gen 是 additive（不破坏现有类型）

## 5. Env files — `infra/env/`

- [ ] 5.1 `infra/env/display.example`: 新增 `OBJECT_STORE_ROOT=` （注释默认值 `/home/ghdc/nwm/object-store`）
- [ ] 5.2 `infra/env/display.env` (gitignored 不入 repo) 在 node-27 host 上 ops 配 `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`；本仓库不 commit 该文件
- [ ] 5.3 在 runbook `docs/runbooks/object-store-forcing-series-read.md` 文档化 "node-27 上 OBJECT_STORE_ROOT 期望值是 `/home/ghdc/nwm/object-store`，由 ops 配置到 display.env"

## 6. Real-disk integration tests — node-27 oracle

- [ ] 6.1 `tests/test_object_store_forcing_real_disk.py` (marked `e2e` or `real_disk`，CI 跳过，node-27 跑)
- [ ] 6.2 fixture: 真实 station_id (`heihe_forc_001` / `qhh_forc_001`) + 已存在 cycle (2026-06-20T12:00:00Z)
- [ ] 6.3 sub-test 1: heihe + IFS + 现存 cycle → 200 + 5×N tuples + 校验 valid_time 范围 + 验证 `unit` 字段全表
- [ ] 6.4 sub-test 2: qhh + gfs + 现存 cycle → 200
- [ ] 6.5 sub-test 3: heihe + 不存在 cycle (`2020-01-01T00:00:00Z`) → 404 `STATION_FORCING_FILE_NOT_FOUND` + details 含 expected_path
- [ ] 6.6 sub-test 4: 不存在 station_id (`bogus_forc_999`) → 404 `STATION_NOT_FOUND`
- [ ] 6.7 sub-test 5: variables filter (`PRCP,TEMP`) → 只回 2 变量 + 各自 N 个 points
- [ ] 6.8 sub-test 6: from/to filter → 中间区段 + inclusive 两端
- [ ] 6.9 sub-test 7: cycle_time `+08:00` 输入 → 与 `Z` 输入产生相同响应
- [ ] 6.10 sub-test 8: variables=Press → 200 + `data.series=[]`
- [ ] 6.11 sub-test 9: variables=PRCP,Press → 200 + 只含 PRCP series
- [ ] 6.12 sub-test 10: 注：SQL spy 计数断言由 §1.13s unit test 覆盖（`PsycopgStationLookup` + spy connection/cursor 更合适在单元层），real-disk e2e 不重复 — 此条仅占位记录归属，无需在 §6 文件内实现
- [ ] 6.13 sub-test 11: 4 个 currently-409 cases (heihe×IFS, heihe×gfs, qhh×IFS, qhh×gfs at 2026-06-20T12:00:00Z) 全部 200
- [ ] 6.14 sub-test 12: 响应 byte-shape 与 §0.10 baseline fixture 对比（除 request_id 和真实数值字段外，结构 + 字段类型 + 排序一致）
- [ ] 6.15 sub-test 13: read-only side-effect 验证：连续 3 个相同请求后 `OBJECT_STORE_ROOT/forcing/...` mtime 不变（采样几个文件）

## 7. Documentation

- [ ] 7.1 NEW `docs/runbooks/object-store-forcing-series-read.md`：operator 视角，含
  - OBJECT_STORE_ROOT 配置示例 + node-27 期望值
  - 启动失败排错（缺 env / 不可读 / `RuntimeModeError`）
  - 5 种 4xx/5xx 错误码与触发条件
  - 与 forcing_producer 协作约定（disk 路径 layout）
  - disk retention window 与 404 的关系（老 cycle 不可调）
  - role boundary 变化说明（display 现合法读 OBJECT_STORE_ROOT）
- [ ] 7.2 `docs/forcing数据处理流程与rSHUD一致性说明.md` 末尾追加 §：API 直读 disk 段
- [ ] 7.3 `CLAUDE.md` 技术栈速查表加一行 "气象代站时间序列 | 直读 object-store /home/ghdc/nwm/object-store/forcing/.../shud/X<lon>Y<lat>.csv"
- [ ] 7.4 创建 3 个 follow-up GitHub issue，issue 标题 + 编号 commit 至 `docs/runbooks/object-store-forcing-series-read.md` §Follow-ups 段：
  - (a) "Frontend: cycle picker adapt to disk retention window" (前端 cycle 选 disk 已 rotate 的会 404)
  - (b) "PsycopgForecastStore.station_series cleanup or deprecation" (read 路径已不再使用)
  - (c) "Evaluate long-term forcing series API via DB read" (老 cycle 历史回看需求评估)

## 8. Validation

- [ ] 8.1 `openspec validate object-store-station-series-read --strict --no-interactive` PASS
- [ ] 8.2 本地 `uv run pytest tests/test_object_store_forcing.py tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_forecast_api_met_station_series.py -q` PASS
- [ ] 8.3 本地 `uv run ruff check packages/common/object_store_forcing.py tests/test_object_store_forcing.py apps/api/routes/data_sources.py apps/api/main.py apps/api/runtime_mode.py tests/test_role_boundary_static.py` PASS
- [ ] 8.4 node-27 整链路 live：apply env 改动 → 重启 display API (`scripts/ops/start-display-api.sh`) → 跑 §6 real-disk integration → 收 receipt
- [ ] 8.5 node-27 curl 验证 4 种组合（heihe×IFS / heihe×gfs / qhh×IFS / qhh×gfs）最新 cycle 全部 200 + 真数据（不再 409）
- [ ] 8.6 node-27 curl 验证老 cycle (2026-05-31) 4 种组合全部 404 `STATION_FORCING_FILE_NOT_FOUND`（确认不 fallback DB）

## 9. PR 边界 (3 PR) — descriptive only, NOT a task; do not check

> 本节是 PR 拆分的文档说明，不是可执行 task；标 `[-]` 而非 `[ ]` 是有意区分。
> 各 PR 真正的 task 在 §0/§1/§2/§3/§4/§5/§6/§7/§8/§10 中分配，已映射到 sub-issue #622/#623/#624。

- [-] 9.1 **PR 1** (~700 LOC): §0 introspection 结论提交至 design.md/introspection-findings.md + §1 reader module + §1.13a–§1.13t unit tests + §1.14 ruff
- [-] 9.2 **PR 2** (~700 LOC): §2 series route 切换 + §3 startup env check + §3.4 boundary fix + §4 OpenAPI + §5 env files + §6 real-disk integration tests + §8.4–§8.6 node-27 live receipt（依赖 PR 1 merged）
- [-] 9.3 **PR 3** (~350 LOC): §7 docs + 3 个 follow-up issue 创建 + §10 closing actions（依赖 PR 2 merged + node-27 sync）

## 10. Closing actions

- [ ] 10.0 重跑 `openspec validate object-store-station-series-read --strict --no-interactive` PASS（archive guard：防止 PR-C 文档/follow-up 编辑后破坏 spec 结构）
- [ ] 10.1 `openspec archive object-store-station-series-read`
- [ ] 10.2 3 条 `docs/review-loop-log.jsonl` append（每 PR 一行）
- [ ] 10.3 关闭 Epic + 3 子 issue
- [ ] 10.4 node-27 `/health` 200 check（验证 PR-B 部署仍在运行；如 PR-B receipt 在最近 24h 内已记录 uvicorn pid 变化，PR-C 不必再次重启服务，只做 health probe）
