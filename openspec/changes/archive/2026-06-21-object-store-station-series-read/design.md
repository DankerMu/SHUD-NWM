## Context

Series read 端点 `/api/v1/met/stations/{station_id}/series` 当前完全走 DB（`PsycopgForecastStore.station_series()` → `_ensure_forcing_version_finalized()`），最新 cycle 因 forcing_producer 未写 `met.forcing_version.checksum` 而被 hard-gate 拦成 409。物理 forcing package 已经在 disk (`/home/ghdc/nwm/object-store/forcing/{src}/{cycle}/{bv}/{model}/shud/X{lon}Y{lat}.csv`)，且 `met_station.properties_json.forcing_filename` 提供了 station→file 100% 映射。

本 change 在读侧绕开 DB finalize gate（不查 `met.forcing_version` 和 `met.forcing_station_timeseries`），直读 disk 物理文件；station_id → forcing_filename 的 `met.met_station` 单表 lookup 仍走 DB（这是 disk-only 概念的合理边界——station 元数据来源仍是真理之源）。

## Architecture Decisions

### AD-1: Reader 模块独立放 `packages/common/object_store_forcing.py`

不放 `forecast_store.py`（避免污染既有 DB 读写抽象），不放 `apps/api/routes/`（保持路由薄）。reader 与 DB store 完全解耦，便于后续抽换 backend（local FS → MinIO S3 client）。

**Alternatives considered:**
- 放在 `forecast_store.py` 内复用 connection/repo——拒绝：会让 store 同时承担 DB + disk 两种 backend 职责，违背单一职责
- 放在 `workers/forcing_producer/store.py`——拒绝：producer 是写侧，reader 是读侧，反向依赖不合理

### AD-2: `station_id → forcing_filename` 走 `met_station.properties_json.forcing_filename`，单表 DB 查询

API 输入 station_id（例 `heihe_forc_001`），reader 一次 PG 查询 `SELECT properties_json, basin_version_id, station_name, ST_X(geom) AS lon, ST_Y(geom) AS lat, elevation_m FROM met.met_station WHERE station_id = $1`，拿到 `properties_json.forcing_filename` (`X100.75Y37.65.csv`) + `basin_version_id` (`basins_heihe_vbasins`) 拼路径，并把 station 元数据（lon/lat/elevation/name/basin_version_id/properties_json）填进 200 response 的 `station` 字段。

**这是本 spec 范围内唯一允许的 DB 查询**——station_id 是 API 入参的标识符层，必须从 DB 解析；它不属于"finalize gate 兜底"。

**事实支撑：** heihe 1709/1709 + qhh 386/386 100% 覆盖 `properties_json.forcing_filename`，已实测。

**Alternatives considered:**
- 读 `forcing_package.json` manifest 反查——拒绝：每次 series 调用要额外读 ~3 MB manifest（heihe），IO 倍数放大
- 从 `met_station.geom` `ST_X` / `ST_Y` 反推 lon/lat 拼文件名——拒绝：浮点表示 `37.650000555388` 与文件名 `Y37.65` 精度不一致，需要 ROUND，引入隐式 truncation 风险
- 在 reader 内 cache mapping——拒绝：mapping 跟 station 表绑定，station 表本身就是缓存

### AD-3: Disk 路径模板硬编码

`${OBJECT_STORE_ROOT}/forcing/{source_id_normalized}/{cycle_compact}/{basin_version_id}/{model_id}/shud/{forcing_filename}`

其中：
- `source_id_normalized`：API 接收的 source_id（如 `IFS` / `gfs`）小写化映射到 disk 实际目录名（disk 上是 `ifs` / `gfs` 都是小写）；该 lowercase 归一化与 forecast_store 内现有 `LOWER(source_id)` 查询语义对齐，本 spec 仅在 disk-path 层做这一步
- `cycle_compact`：`YYYYMMDDHH` 10 位无分隔（如 `2026062012`），按 disk 命名实测 (§0.2 introspection 全量验证)
- `basin_version_id`：直接来自 `met_station.basin_version_id`（与 disk 子目录名一致，实测 `basins_heihe_vbasins`）
- `model_id`：来自 API query param

不解析 `forcing_package.json` manifest——manifest 用于完整性校验场景，本读路径只需要单 station 文件。

### AD-4: CSV 解析模型 + 明确舍入策略

shud/X<lon>Y<lat>.csv 实测结构（IFS 代表样例；§0 introspection 发现 GFS 当前为 56 行）：
```
53\t6\t20260620\t20260627       <- row 1: nrow ncol start_date(YYYYMMDD) end_date(YYYYMMDD)
Time_Day\tPrecip\tTemp\tRH\tWind\tRN   <- row 2: column names
0\t0\t5.522532453\t0.7263231972\t4.000543943\t0
0.125\t1.14809095\t...
...
```

`valid_time` 计算：`valid_time = cycle_time + timedelta(seconds=int(round(Time_Day * 86400)))`

- cycle_time 必须以 UTC `datetime` 参与（API 接 ISO 8601 带 `Z`）
- 使用 `int(round(...))` 而不是 `int(...)`，避免 float 截断（如 `int(0.041666 * 86400) = 3599` vs `int(round(0.041666 * 86400)) = 3600`，差 1 秒）
- Time_Day 间隔 0.125 = 3 hours（实测）
- IFS 当前样例为 53 数据点，GFS 当前样例为 56 数据点；row count 不在 spec 里 hardcode，按 CSV 的 nrow header 字段读，spec scenarios 用 "5 × N 元组" 关系表达
- IFS 53 点样例末点 Time_Day=6.5 → cycle_time + 6 天 12 小时；GFS 56 点样例同样由 CSV 自身 `Time_Day` 决定窗口末端

### AD-5: 变量名映射 + `unit` 字段输出契约 + Press 静默丢弃语义

按 `docs/forcing数据处理流程与rSHUD一致性说明.md:303` 单位契约：

| shud CSV 列名 | API canonical `variable` | API `unit` 字段输出 |
|---|---|---|
| Precip | `PRCP` | `mm/day` |
| Temp   | `TEMP` | `degC` |
| RH     | `RH`   | `0-1` |
| Wind   | `wind` | `m/s` |
| RN     | `Rn`   | `W/m^2` |
| (无)   | `Press` | (不输出) |

`Press` 不在 shud/CSV 内：

- 请求 `variables=Press` 单独传 → response 不含任何 series（200 OK，`data.series=[]`）
- 请求 `variables=PRCP,Press` 混合 → Press 从过滤集合中**静默丢弃**，response 仅含 PRCP series（不带 Press 的 empty list，不带 warning）
- 请求 `variables=Press,UnknownVariable` → Press 静默丢弃，UnknownVariable 静默丢弃，response `data.series=[]` (200 OK)

不区分"已知但不可生产 (Press)"和"未知变量 (UnknownVariable)"——两类都静默 drop。理由：维持 schema simple，与现 OpenAPI `StationSeries[].variable` 字段无 enum 约束的现状一致。

### AD-6: 错误码契约 — 复用 + 新增

| 失败场景 | 错误码 | status | details | 来源 |
|---|---|---|---|---|
| 未知 station_id (DB 查不到) | `STATION_NOT_FOUND` | 404 | `{station_id}` | **复用** `forecast_store.py:2099-2104`（shape 不变） |
| cycle_time / model_id / source_id 三必传少一个 | `MISSING_REQUIRED_FILTER` | 422 | `{required_alternatives: [["forcing_version_id"], ["model_id", "source_id", "cycle_time"]]}` | **复用** `forecast_store.py:2132-2146`（exact existing site for station_series；shape 不变） |
| station 查到但 `properties_json.forcing_filename` 缺失 | `STATION_FORCING_FILENAME_MISSING` | 500 | `{station_id}` | **新增** |
| disk 路径模板组装的文件不存在 | `STATION_FORCING_FILE_NOT_FOUND` | 404 | `{station_id, expected_path, basin_version_id, source_id, cycle_time, model_id}` | **新增** |
| 文件存在但 CSV 解析失败 | `STATION_FORCING_FILE_MALFORMED` | 500 | `{station_id, expected_path, parse_reason}` | **新增** |
| OBJECT_STORE_ROOT env 缺失/不可读 | (启动期 fail) | N/A | (raise `RuntimeModeError` from `load_runtime_config`) | **新增**（路径见 AD-7） |

不复用 `FORCING_VERSION_NOT_FOUND` / `FORCING_VERSION_NOT_FINALIZED`：旧码语义绑定 `met.forcing_version` 表，新路径不查该表，复用会让 error code 跨语义。

### AD-7: 启动期 env check 走 `runtime_mode.load_runtime_config`

`apps/api/main.py:create_app()` 在应用组装早期调用 `apps/api/runtime_mode.py:load_runtime_config(env)` → `RuntimeConfig`。这是仓内唯一被复用的"启动期 env 一致性校验"层，已有 `RuntimeModeError` 异常类。

**实现：**

1. 在 `RuntimeConfig` 加字段 `object_store_root: Path | None`
2. 在 `load_runtime_config` 内：
   ```python
   raw = env.get("OBJECT_STORE_ROOT", "").strip()
   if role == ServiceRole.DISPLAY_READONLY and not raw:
       raise RuntimeModeError("OBJECT_STORE_ROOT env var is required")
   path = Path(raw).expanduser().resolve() if raw else None
   if path is not None and (not path.is_dir() or not os.access(path, os.R_OK)):
       raise RuntimeModeError(f"OBJECT_STORE_ROOT={path} is not a readable and traversable directory")
   runtime_config.object_store_root = path
   ```
3. `apps/api/main.py` 把 `runtime_config.object_store_root` 通过 `app.state.object_store_root = ...` 或 FastAPI `Depends(get_object_store_root)` 注入到 reader

fail-fast：display_readonly env 缺失或任何 role 的 env 指向不可读目录 → `RuntimeModeError` 让 process 启动期就 exit，不到第一个 series 请求才报。

兼容性：仓内存在 `apps/api/main.py` 模块级 `app = create_app()`，以及大量测试 `from apps.api.main import app`。因此默认 `dev_monolith` import path 不要求 `OBJECT_STORE_ROOT`；非 display role 未配置时 `RuntimeConfig.object_store_root` 为 `None`，但 station-series route 若实际被调用仍必须通过 `get_object_store_root` 拿到有效 root（测试可 override provider）。

**关键：** AD-7 必须配合 AD-11 boundary 调整，否则 display API 启动失败（详见 AD-11）。

### AD-8: 完全断 `met.forcing_version` 和 `met.forcing_station_timeseries` 查询，不 fallback

`get_met_station_series` 内：

1. 通过 AD-2 单表 `met.met_station` 查询拿 station 元数据 + forcing_filename
2. 直接调 `object_store_forcing.read_station_forcing_csv(...)` 读 disk
3. **不调** `PsycopgForecastStore.station_series()`、`_ensure_forcing_version_finalized()`、不 SELECT `met.forcing_version`、不 SELECT `met.forcing_station_timeseries`
4. **不留** try-except 回到 DB 的逻辑；disk miss → 404

`PsycopgForecastStore.station_series` 实现保留（其他模块可能调用；deprecation/cleanup 留 follow-up issue），但 series 路由不再调用。

**"完全断 DB" 措辞精确化：** 不是字面上零 DB 查询（station 元数据 lookup 仍走 DB），而是不再依赖 `met.forcing_version` 表的 finalize 状态和 `met.forcing_station_timeseries` 表的 timeseries 内容。

### AD-9: 不缓存

单次请求读单个小型 CSV（当前 IFS 53 行、GFS 56 行，约数 KB）。无缓存即可满足 P95 < 50ms（disk read）。reader 是**纯读、无副作用**：不写文件、不修改 DB、不变动任何 OBJECT_STORE_ROOT 下文件 mtime。未来如有性能问题加 LRU + mtime invalidation。

### AD-10: 变量过滤 + from/to/limit 在 reader 层，明确 limit 与 series 排序语义

reader 内：
1. 通过 no-follow descriptor-bound open 读 CSV，并用 bytes / line / row 硬上限约束输入规模（N 仍由 CSV header nrow 字段决定）
2. 按 `variables` filter 列（已知不可生产/未知变量静默 drop，参见 AD-5）
3. 按 `from_time` / `to_time` filter 行（inclusive 两端 UTC 比较）
4. 按 `limit` 截断**总元组数**（不是 per-variable）

读侧使用 chunked line reader；CSV 当前很小，但仍保留 hard cap，避免异常对象文件拖垮 display hot path。

**输出排序契约：**

- `data.series[]` 按固定变量顺序排列：`[PRCP, TEMP, RH, wind, Rn]`（仅过滤后存在的变量）
- 每个 `series.points[]` 按 `valid_time` ascending
- `limit` 截断时，从展开成单一 (variable, valid_time, value) 元组流之后按"先变量名顺序、变量内时间 ascending"截取 N 个元组

### AD-11: Display role boundary 调整（必须先做，否则 AD-7 启动检查导致 display API 拒启）

**问题：** `apps/api/runtime_mode.py` 的 `_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS` 曾列入 `OBJECT_STORE_ROOT`；display.env 一旦含该 key 即在 `display_boundary_blockers()` 被 raise `DISPLAY_BOUNDARY_CONFIG_UNSAFE`。`tests/test_role_boundary_static.py` 的 `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS` 与 `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` 断言互锁，也曾同步列入。

**决策：** display 现在合法需要读 forcing CSV（disk-only 路径属于 display 输出业务面），从下面四处同步移除/重分类 `OBJECT_STORE_ROOT`：

1. `apps/api/runtime_mode.py` `_DISPLAY_FORBIDDEN_COMPUTE_PATH_ENVS` 元组
2. `tests/test_role_boundary_static.py` `DISPLAY_RUNTIME_FORBIDDEN_ENV_KEYS`
3. `scripts/validate_two_node_docker_runtime.py` `DISPLAY_FORBIDDEN_ENV_KEYS`
4. `scripts/validate_two_node_docker_runtime.py` `COMPUTE_ONLY_PATH_ENV_KEYS`

同时把 `OBJECT_STORE_ROOT` 纳入 display required/audited runtime env surfaces（`DISPLAY_REQUIRED_ENV` / `DISPLAY_REQUIRED_RUNTIME_ENV` / `DISPLAY_AUDITED_RUNTIME_ENV` 相关互锁），并更新 `tests/test_role_boundary_static.py` 的 allowed display required set。否则 `COMPUTE_ONLY_PATH_ENV_KEYS <= DISPLAY_FORBIDDEN_ENV_KEYS` 和 `DISPLAY_REQUIRED_CONFIG_KEYS.isdisjoint(COMPUTE_ONLY_PATH_ENV_KEYS)` 两个静态不变量会把它重新拉回 forbidden。

**理由记录在 AD-11**：display 业务面合法读取 disk-resident forcing CSV，与本 spec 业务目标一致；boundary 收紧到"只读不可写"由 OBJECT_STORE_ROOT 目录权限 + AD-9 read-only 契约保证。

### AD-12: Reader connection 注入策略 — FastAPI Depends

`read_station_forcing_csv` 签名：

```python
def read_station_forcing_csv(
    *,
    station_lookup: StationLookup,  # Protocol-typed
    object_store_root: Path,
    station_id: str,
    source_id: str,
    cycle_time: datetime,
    model_id: str,
    variables: Sequence[str] | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    limit: int | None = None,
) -> StationSeriesResponse
```

`StationLookup` 是 `Protocol`：

```python
class StationLookup(Protocol):
    def lookup(self, station_id: str) -> StationMetadata: ...
```

route 用 `Depends(get_station_lookup)` 提供基于 `PsycopgForecastStore.connection` 的具体实现；测试用 `FakeStationLookup`（dict-backed）注入。

### AD-13: Reader unit test 注入策略 — Protocol-typed fakes

单元测试**不用真 DB**，注入 `FakeStationLookup`（dict-backed）和参数化文件系统 fixture（`tmp_path` + 写入测试 CSV）。`_lookup_station` 的 DB error 行为通过 fake raise 模拟。

real-DB integration test 在 §6（node-27 oracle）做。

## Risks

### R-1: Boundary blocker（display API 启动失败）

如果 AD-11 没先做就改 display.env 加 `OBJECT_STORE_ROOT`，display API 直接 raise `DISPLAY_BOUNDARY_CONFIG_UNSAFE`，整个服务下线。

**Mitigation:** PR 2 任务序：先改 `runtime_mode.py` + `test_role_boundary_static.py`（AD-11 三处），再改 `display.example`，最后 node-27 配 `OBJECT_STORE_ROOT` + 重启。

### R-2: `OBJECT_STORE_ROOT` 配错导致 API 启动失败

**Mitigation:** AD-7 fail-fast；`infra/env/display.example` 模板写入正确默认；runbook 在 `docs/runbooks/object-store-forcing-series-read.md` 文档化排错。

### R-3: disk 上 `cycle_compact` 格式跨 source 不一致

实测 disk 上 IFS + gfs 都是 `YYYYMMDDHH` 10 位。但若未来新 source 用别的格式（如 `YYYY-MM-DD_HH`），reader 模板会 miss。

**Mitigation:** §0.2 introspection 对 IFS + gfs 各 10 cycle 全量目录名扫描确认；新增 source 时由 `direct-grid-forcing` 或新 issue 同步更新此 reader。

### R-4: `properties_json.forcing_filename` 缺失场景

heihe + qhh 实测 100% 覆盖。但其他未来接入 basin 可能没填——返回 500 `STATION_FORCING_FILENAME_MISSING`（AD-6），不静默退化。

### R-5: CSV 行数随 forecast 长度变化

§0 introspection 实测 IFS 为 53 数据点、GFS 为 56 数据点。如果 forecast 长度变（如改成 10 天），CSV 行数也会变。

**Mitigation:** reader 不假设固定行数，按 nrow header 字段读，并设置 hard row cap；spec 用"5 × N tuples"表达；test cases 覆盖 1 / 53 / 56 / 100 多种长度、declared nrow mismatch、超出 cap 的 nrow。

### R-6: CSV header 跨版本漂移

如果 forcing_producer 升级改 header 格式（如改 `Precip` → `PRCP`），reader 会 parse fail。

**Mitigation:** reader 按列名 lookup 不按列序号；新增/重命名列写测试单独覆盖。

### R-7: cycle_time 时区一致性

API 接 ISO 8601 `2026-06-20T12:00:00Z`（UTC 带 `Z`），disk 路径 `2026062012` 无时区元数据。如果 API 接收的是 local time（如 `2026-06-20T20:00:00+08:00`），cycle_compact 算成 `2026062012` 才对——所以必须先 UTC 转换。

**Mitigation:** reader 内 cycle_time 强制 UTC 转换；测试 case 覆盖 naive datetime / `+00:00` / `+08:00` 三种输入归一化到 UTC compact。

### R-8: AD-2 单表 DB 查询使 reader 不是"纯 disk"

`met.met_station` 单表 lookup 是 reader 的唯一 DB 依赖。如果 DB 不可用 / 连接池耗尽 → reader fail。这是有意接受的折衷（参见 AD-8 "完全断 DB" 措辞精确化）。

**Mitigation:** 错误透传 (DB 不可用 → 500 generic)；未来 cache met_station 行可减少 DB 依赖。

### R-9: Press 静默丢弃可能让前端误以为获得了 Press 数据

AD-5 决策为静默丢弃；前端如果显示 `data.series[]` empty 时不区分"请求了无效变量"vs"时间窗内无数据"，UX 体验差。

**Mitigation:** 写进 runbook + follow-up frontend issue: cycle picker + variable 选择器适配新语义；本 change 不做前端改动（提案中已 Out of Scope）。

## Open Questions

- **OQ1**: 不同 cycle / source / basin_version 的 shud/CSV 行数是否一致？(§0.1 introspection 全量扫 20 个 cycle × 4 basin/source × 抽样 5 文件)
- **OQ2**: disk 路径 cycle 段是否在所有 source 都是 `YYYYMMDDHH`？(§0.2 全量列举)
- **OQ3**: `met_station.basin_version_id` 是否与 disk 目录名 `{bv}` 一一对应？实测 `basins_heihe_vbasins` 一致，但 §0.3 检查 qhh `basins_qhh_vbasins` 也一致
- **OQ4**: 启动期 OBJECT_STORE_ROOT check 是否需要可写权限？只读已足够（reader 不写，参见 AD-9）
- **OQ5**: `apps/api/runtime_mode.py:load_runtime_config()` 接收 `env: Mapping[str, str] | None` 的注入模式；reader connection 注入要走 `Depends(get_object_store_root)` 还是 `app.state.object_store_root`？（§0.5 grep `app.state` 与 `Depends` 现有模式决定）
- **OQ6**: `docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS` 实际位置在哪？是 `tests/test_role_boundary_static.py:89` import 的哪个模块？AD-11 fix 是否需要触动该模块？(§0.8)
- **OQ7**: reader unit test 注入用 `Protocol`-typed `StationLookup` (AD-13) vs `psycopg.Connection` mock pattern（`tests/test_forecast_store_product_quality_sql.py` 现存模式）哪个更对齐项目惯例？(§0.9)

§0 introspection 必须先做完且 commit 结论，再开始 §1 实现。

## Subagent Workflow Fixture (PR-A #622)

Fixture level: expanded. Repair intensity: high.

Project profile: NHMS (`openspec/project-profile.md`).

Change surface for PR-A:
- New reader module: `packages/common/object_store_forcing.py`.
- New flat unit tests: `tests/test_object_store_forcing.py`.
- Introspection evidence and baseline fixture: `design.md`/`introspection-findings.md` plus `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json`.

Must preserve:
- Existing `StationSeriesResponse` field shape and ordering oracle from `openapi/nhms.v1.yaml`.
- Existing `STATION_NOT_FOUND` and `MISSING_REQUIRED_FILTER` code/details shape from `packages/common/forecast_store.py`.
- Existing DB-backed route behavior until PR-B switches the API entrypoint; PR-A only adds the reader and unit evidence.

Must add/change:
- Disk path resolution from `station_id`, `source_id`, UTC `cycle_time`, `model_id`, and `met_station.properties_json.forcing_filename`.
- SHUD CSV parsing, unit mapping, UTC valid_time computation, variables/from/to/limit filtering, and side-effect-free reads.
- Typed disk/read errors for missing `forcing_filename`, missing file, and malformed CSV.
- SQL-spy evidence that the reader does not query `met.forcing_version` or `met.forcing_station_timeseries`.

Risk packs considered:
- Public API / CLI / script entry: not selected for PR-A - API route switch is PR-B; PR-A only exposes a library entrypoint.
- Config / project setup: not selected for PR-A - `OBJECT_STORE_ROOT` startup/env wiring is PR-B.
- File IO / path safety / overwrite: selected - reader opens object-store paths assembled from DB metadata and API inputs; tests must cover safe single path components, root-inner traversal rejection, no-follow/symlink rejection, file miss, malformed file, and no writes.
- Schema / columns / units / field names: selected - CSV columns map to API variables/units and response shape must match the existing schema.
- Auth / permissions / secrets: not selected - no credentials or authorization path changes in PR-A.
- Concurrency / shared state / ordering: not selected - reader is stateless and has no cache.
- Resource limits / large input / discovery: selected - CSV reads must be bounded by declared header row count plus explicit row/bytes/line hard caps; tests cover multiple N values and cap violations.
- Legacy compatibility / examples: selected - old response shape and existing error-code shapes remain the oracle.
- Error handling / rollback / partial outputs: selected - every expected reader failure maps to a stable typed error and PR-A has no writes to roll back.
- Release / packaging / dependency compatibility: not selected - no dependency or package metadata change expected.
- Documentation / migration notes: not selected for PR-A - docs/runbook work is PR-C.

Domain packs:
- Hydro-met time series / forcing windows: selected - UTC cycle compact, Time_Day conversion, and forecast-window bounds are core behavior.
- Published NHMS artifacts / display identity: selected - disk file identity must bind to the same station and response identity without touching DB finalize state.
- PostGIS / TimescaleDB domain behavior: selected narrowly - allowed DB access is only `met.met_station`; forbidden tables are asserted by SQL spy.
- Geospatial / CRS / basin geometry: not selected - PR-A reads stored station lon/lat/filename metadata and does not alter geometry/CRS.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD execution or numerical solver behavior.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm path.
- External hydro-met providers / snapshot reproducibility: not selected - no provider fetch/conversion.
- Run manifest / QC provenance: not selected - no manifest/QC read or write.

Invariant Matrix:
- Governing invariant: PR-A's reader may resolve and parse one station CSV, but it must not consult forcing-version readiness or mutate object-store/DB state.
- Source-of-truth identity/contract: `met.met_station.station_id` -> `basin_version_id` + `properties_json.forcing_filename`; CSV contract `Time_Day Precip Temp RH Wind RN`; existing `StationSeriesResponse` schema.
- Producers: none - forcing_producer output is consumed as existing disk input, not changed.
- Validators/preflight: `_normalize_source_id`, `_compute_cycle_compact`, `_resolve_disk_path`, CSV header/data parser, typed error constructors.
- Storage/cache/query: `StationLookup` / `PsycopgStationLookup` may query `met.met_station` only; no cache.
- Public routes/entrypoints: none in PR-A - PR-B wires the FastAPI route.
- Frontend/downstream consumers: unchanged response shape verified against baseline fixture.
- Failure paths/rollback/stale state: file missing, missing filename, malformed CSV, unknown station, unsupported variables, reversed time window; no writes or cleanup.
- Evidence/audit/readiness: §0 introspection conclusions, §0.10 baseline fixture, §1.13a-t unit tests, ruff, and `openspec validate`.
- Regression rows:
  - Valid heihe/IFS station + existing fixture CSV -> 200-shape payload with fixed variable order and UTC points.
  - Missing file / malformed CSV / missing forcing_filename -> stable typed error with operator details.
  - Complete reader call under SQL spy -> zero SELECTs against `met.forcing_version` and `met.forcing_station_timeseries`.
  - Consecutive reads -> no object-store writes and stable response shape.

Required evidence:
- `uv run pytest tests/test_object_store_forcing.py -q`.
- `uv run ruff check packages/common/object_store_forcing.py tests/test_object_store_forcing.py`.
- `openspec validate object-store-station-series-read --strict --no-interactive`.
- Node-27 §0 introspection and baseline capture committed before reader implementation is reviewed.

Review focus:
- Verify §0 introspection is committed and stable before accepting §1 implementation.
- Verify the reader's only DB dependency is station lookup, not forcing readiness or station timeseries content.
- Verify time/unit/limit/filter behavior matches AD-4/AD-5/AD-10 and `spec.md`.
- Verify malformed/missing inputs produce stable typed errors without hiding path/identity details needed by operators.

## Subagent Workflow Fixture (PR-B #623)

Fixture level: expanded. Repair intensity: high.

Project profile: NHMS (`openspec/project-profile.md`).

Change surface for PR-B:
- FastAPI route and dependency wiring: `apps/api/routes/data_sources.py`, `apps/api/main.py`.
- Runtime startup and display boundary config: `apps/api/runtime_mode.py`, `scripts/validate_two_node_docker_runtime.py` (`DISPLAY_FORBIDDEN_ENV_KEYS`, `COMPUTE_ONLY_PATH_ENV_KEYS`, display required/audited runtime env sets), `tests/test_role_boundary_static.py`, `infra/env/display.example`.
- Public contract and evidence: `_patch_station_series_openapi`, `openapi/nhms.v1.yaml` if generated/static sync is required, API mocked tests, node-27 real-disk tests and live receipt.

Must preserve:
- `StationSeriesResponse` envelope/schema shape, variable ordering, and existing `STATION_NOT_FOUND` / `MISSING_REQUIRED_FILTER` error shapes.
- Display role remains read-only: `OBJECT_STORE_ROOT` is allowed and required for display reads, but compute mutation envs and write boundaries stay forbidden.
- Existing module-level `app = create_app()` import path and dev/compute `create_app()` callers remain compatible without `OBJECT_STORE_ROOT`; display test helpers either receive a readable tmp root or intentionally check the missing-env failure.
- Existing DB-backed `PsycopgForecastStore.station_series()` implementation remains available for non-route cleanup/deprecation work; PR-B only disconnects the public series route.
- PR-A reader safety invariants: path containment, no-follow descriptor-bound reads, hard row/bytes/line caps, malformed/non-finite handling, no object-store writes.

Must add/change:
- `/api/v1/met/stations/{station_id}/series` calls `read_station_forcing_csv(...)` through `Depends(get_station_lookup)` and `Depends(get_object_store_root)` and does not call `store.station_series()` or `_ensure_forcing_version_finalized()`.
- `RuntimeConfig.object_store_root` is startup-validated for display and for any configured path, stored on `app.state`, and `OBJECT_STORE_ROOT` is removed from display-forbidden/compute-only runtime sets while becoming a display required/audited runtime env.
- OpenAPI marks `forcing_version_id` deprecated and lists disk-path errors (`STATION_FORCING_FILE_NOT_FOUND`, `STATION_FORCING_FILENAME_MISSING`, `STATION_FORCING_FILE_MALFORMED`, `MISSING_REQUIRED_FILTER`) instead of forcing-version finalize errors for this route.
- node-27 receipt proves the route+runtime+env boundary works live before draft PR is marked ready.

Risk packs considered:
- Public API / CLI / script entry: selected - this PR changes the public station-series route and documented query/error behavior.
- Config / project setup: selected - `OBJECT_STORE_ROOT` becomes startup-required runtime config and app state.
- File IO / path safety / overwrite: selected - public route exposes the PR-A disk reader; PR-B must not weaken reader guards or add fallback/write paths.
- Schema / columns / units / field names: selected - OpenAPI examples/deprecation and response-shape compatibility are part of the acceptance.
- Auth / permissions / secrets: selected narrowly - display boundary permits a new read env without exposing secrets or compute mutation knobs; receipts must not leak env values beyond paths already documented.
- Concurrency / shared state / ordering: not selected - object root is immutable startup config and reader remains stateless/no-cache.
- Resource limits / large input / discovery: selected - route must use the bounded PR-A reader and real-disk tests must not scan broad object-store roots.
- Legacy compatibility / examples: selected - `forcing_version_id` is accepted but ignored with `cycle_time`; alone it now returns existing 422 shape.
- Error handling / rollback / partial outputs: selected - typed reader errors map to stable HTTP envelopes; no fallback DB and no partial write/rollback path.
- Release / packaging / dependency compatibility: selected narrowly - no dependency changes, but node-27 env/restart receipt is a release gate.
- Documentation / migration notes: selected narrowly - env example plus runbook env-section placeholder are required; full docs/follow-up issues stay PR-C.

Domain packs:
- Hydro-met time series / forcing windows: selected - live route must serve the latest IFS/GFS cycle from disk and preserve UTC filter semantics.
- Published NHMS artifacts / display identity: selected - display response identity must bind station metadata to the exact disk artifact path and not stale DB readiness.
- PostGIS / TimescaleDB domain behavior: selected narrowly - allowed DB access is only `met.met_station`; forbidden forcing tables stay unused by the route.
- Geospatial / CRS / basin geometry: not selected - station geometry is read-only response metadata; no CRS or station table change.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD execution or numerical conversion change beyond PR-A parser already covered.
- Slurm production lifecycle / mock-vs-real parity: not selected - no scheduler path.
- External hydro-met providers / snapshot reproducibility: not selected - no provider fetch/conversion.
- Run manifest / QC provenance: not selected - no manifest/QC artifact contract change.

Invariant Matrix:
- Governing invariant: The public series route must serve disk-resident SHUD station CSVs through a readonly display object-store root, without consulting forcing-version readiness or mutating DB/object-store state.
- Source-of-truth identity/contract: API tuple `station_id + model_id + source_id + cycle_time`; `met.met_station` -> `basin_version_id + forcing_filename`; startup `OBJECT_STORE_ROOT`; existing `StationSeriesResponse` schema and API error envelope.
- Producers: none - forcing_producer output and DB writers are unchanged.
- Validators/preflight: `load_runtime_config`, `display_boundary_blockers`, docker runtime validator constants including display forbidden/compute-only/required/audited env sets, OpenAPI schema patch, reader validation from PR-A.
- Storage/cache/query: `PsycopgStationLookup` may query `met.met_station` only; no route fallback to `met.forcing_version` or `met.forcing_station_timeseries`; no cache.
- Public routes/entrypoints: `get_met_station_series`, dependency providers, generated/static OpenAPI operation for `getMetStationSeries`.
- Frontend/downstream consumers: response shape and existing `forcing_version_id` query acceptance preserved; module-level app import and non-display `create_app()` tests still start without object root; display tests/routes still start once they provide a readable object root; front-end UX changes remain PR-C/follow-up.
- Failure paths/rollback/stale state: missing object root startup fail, unreadable root startup fail, disk miss 404, missing filename/malformed file 500, only-`forcing_version_id` 422, old finalized DB cycle with rotated disk file 404.
- Evidence/audit/readiness: API mocked tests, runtime/boundary static tests, real-disk node-27 tests, curl receipt, ruff, OpenSpec validation, CI, draft-to-ready gate evidence.
- Regression rows:
  - Latest cycle heihe/qhh x IFS/gfs with disk file present -> HTTP 200 non-empty disk series, no 409 finalize error.
  - `forcing_version_id` alone -> HTTP 422 existing `MISSING_REQUIRED_FILTER`; with `cycle_time/model_id/source_id` -> HTTP 200 identical disk response.
  - Missing/unreadable/untraversable `OBJECT_STORE_ROOT` -> startup `RuntimeModeError`; display env with readable + traversable root -> no `DISPLAY_BOUNDARY_CONFIG_UNSAFE`.
  - Module import `from apps.api.main import app` with default dev env -> succeeds; explicit display startup without root -> `RuntimeModeError`; display startup with readable tmp root -> prior runtime route inventory and non-series API tests still pass.
  - Old cycle present in DB but absent on disk -> HTTP 404 `STATION_FORCING_FILE_NOT_FOUND`, no DB fallback.

Required evidence:
- `openspec validate object-store-station-series-read --strict --no-interactive`.
- `uv run pytest tests/test_object_store_forcing.py tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_forecast_api_met_station_series.py -q`.
- Targeted sibling-startup compatibility: preserve module-level `app = create_app()` import under default dev env, update all touched display `create_app()` test env helpers, and run at least the affected runtime/monitoring/pipeline artifact tests that previously constructed display apps without `OBJECT_STORE_ROOT`.
- `uv run ruff check packages/common/object_store_forcing.py tests/test_object_store_forcing.py apps/api/routes/data_sources.py apps/api/main.py apps/api/runtime_mode.py tests/test_role_boundary_static.py`.
- `cd apps/frontend && pnpm run check:api-types` when available after OpenAPI changes.
- node-27: sync branch, set gitignored display env, restart with `scripts/ops/start-display-api.sh`, run real-disk tests, capture `/health`, uvicorn pid change, four latest-cycle 200 curls, and four old-cycle 404 curls in the PR body before ready-for-review.

Non-goals:
- Do not change forcing_producer, station table data, DB schemas, `PsycopgForecastStore.station_series()` internals, frontend UX, S3/MinIO client behavior, or full PR-C runbook/follow-up issue work.

Review focus:
- Verify AD-11 is fixed consistently in runtime code, docker runtime validator forbidden/compute-only/required/audited constants, and static tests, with `OBJECT_STORE_ROOT` allowed/required but compute mutation envs still forbidden.
- Verify route injection uses the PR-A reader and station lookup dependency, with no DB fallback and no force-version readiness calls.
- Verify global `app = create_app()` import and non-display `create_app()` startup stay compatible, while display startup enforces readable `OBJECT_STORE_ROOT`.
- Verify OpenAPI and tests document `forcing_version_id` deprecation/ignore semantics and remove old finalize error examples for this operation.
- Verify live node-27 receipt is SHA-matched to the PR head before the PR leaves draft.
