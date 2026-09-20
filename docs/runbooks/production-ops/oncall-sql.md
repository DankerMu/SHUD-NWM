**分册：值守 SQL 片段**

本页是当前生产值守手册的 §9 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 9. 值守 SQL 片段

Run these on node-27 after sourcing the ingest writer env
(`infra/env/node27-ingest.env` on the host, or an equivalent secret-safe
operator env). Do not source `infra/env/display.env` for writer/ingest SQL:
that file belongs to the display_readonly runtime.

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
```

Latest runs:

```sql
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), left(coalesce(error_message,''),120), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;
```

Latest q_down coverage:

```sql
select run_id, variable, count(*) as rows,
       count(distinct river_segment_id) as segments,
       min(valid_time), max(valid_time)
from hydro.river_timeseries
where variable='q_down'
group by run_id, variable
order by max(valid_time) desc
limit 20;
```

Heihe river segment layers:

```sql
select coalesce(properties_json->>'shud_output_river','false') as shud_output_river,
       count(*) as n
from core.river_segment
where river_network_version_id='basins_heihe_rivnet_vbasins'
group by 1
order by 1;
```

### 9.1 `core.river_segment` 两类行与计数不变量（#1693）

上面这条按 `shud_output_river` 分组的查询之所以要分组，是因为**同一个
`river_network_version_id` 下 `core.river_segment` 按设计存两类行**（术语定义见
`openspec/glossary.md` 的 `## Domain terms`：`SHUD input reach row` /
`SHUD output river row`）：

| 行类 | id 形状 | 来源 | `properties_json->>'shud_output_river'` | 作用 |
|---|---|---|---|---|
| reach 行 | `<model_id>_reach_<iRiv:06d>` | model package 的 `gis/river.shp` | 无该键或 `'false'` | 水力参数 + flow-ordered 单 part 几何；`core.river_segment_crosswalk` 只指向它 |
| output 行 | `<model_id>_shud_riv_<N:06d>` | model package 的 `.sp.riv` | `'true'` | SHUD 输出序列身份（`hydro.river_timeseries` 按它建键）；几何从对应 reach 行回填 |

`core.river_network_version.segment_count` **只数 reach 行**（post-PR-2 #561
即 `gis/river.shp` 记录数）。导入时会校验 `river.shp` 记录数 == `.sp.riv` reach 数
（`workers/model_registry/basins_geometry.py::_validate_river_shp_single_part_invariant`），两类行因此恒等量，所以：

> 不加过滤的 `count(*)` 等于 `2 × segment_count` 是**预期值，不是重复播种**。

issue #1122 与 #1123 两次都把这个翻倍读成重复 seed 行，#1123 一度已经准备好生产 delete。
体检查询一律先过滤再比：

```sql
-- 逐类计数并与 segment_count 对照；差值应为 0/0，total 应为 2×segment_count
-- 下面的 river_network_version_id 换成要体检的那一个。
select rnv.river_network_version_id,
       rnv.segment_count,
       count(*)                                                          as total_rows,
       count(*) filter (where coalesce(rs.properties_json->>'shud_output_river','false') = 'false') as reach_rows,
       count(*) filter (where coalesce(rs.properties_json->>'shud_output_river','false') = 'true')  as output_rows
from core.river_network_version rnv
join core.river_segment rs using (river_network_version_id)
where rnv.river_network_version_id = 'basins_heihe_rivnet_vbasins'
group by rnv.river_network_version_id, rnv.segment_count;
```

要点：

- `output_segment_count` **不是** `core.river_network_version` 的列（`db/migrations/` 里
  也没有任何表带这个列名）。它是 JSON / receipt 字段：导入 receipt、
  `core.model_instance.resource_profile`（由 `packages/common/forecast_store.py:268`、
  `packages/common/display_coverage.py:86` 读取），以及调度器 file-registry /
  candidate / chain manifest（`services/orchestrator/scheduler_file_providers.py:1061-1104`
  等）。值同样等于 `.sp.riv` reach 数。按列名去查库只会得到 `column does not exist`。
- 已经正确过滤的现成 oracle，可直接抄谓词：
  `workers/model_registry/basins_registry_import.py:610-620`（reach 行幂等守卫）、
  `tests/test_real_database_integration.py:448-453`（reach 行几何断言）、
  `tests/test_basins_registry_import_qhh.py::test_pr2_contract_reach_rows_single_part_and_crosswalk_count`
  （真实库里钉住 `total_rows == 2 × segment_count`）。
- `workers/output_parser/parser.py::load_river_segments`（`:820-838`）是
  **output-class-first**：先按 `shud_output_river='true'` 取，取不到才回落到
  不带过滤的全量查询。不要把它当成无条件过滤的证据。

### 9.2 `pg_stat_activity` 归因与 cancel 纪律（#1714）

2026-08-22 事故：`pg_stat_activity` 里一条 `state=active`、`dur=00:06:59` 的
`SELECT h.run_id, ...` 被判为「自己的 pytest 慢查询」并 `pg_cancel_backend`，
实际打掉的是每 10 分钟一次的生产 ingest tick（autopipe `rc=1`，本轮 ingest 未完成）。
当时全库应用连接的 `application_name` 都是空串，唯一的信号 `usename` 又被
autopipe/parser/retention 共用的 `nhms` 角色抹平。现在每个组件自带默认标识。

| `application_name` | 归属 |
|---|---|
| `nhms-autopipe` | `scripts/node27_autopipeline.py`（`nhms-node27-autopipe.timer`，每 10 分钟）|
| `nhms-ingest-run` | `scripts/node27_ingest_run.py`（autopipe 子进程）|
| `nhms-output-parser` | `workers/output_parser`（autopipe 子进程）|
| `nhms-refresh-coverage` | `scripts/node27_refresh_coverage.py`（autopipe 子进程）|
| `nhms-display-api` | `apps/api/routes/hydro_display.py`（display API 只读连接池）|
| `nhms-api-pipeline` | `apps/api/routes/pipeline.py`（同一 uvicorn 进程里的控制面 retry/cancel 引擎，**会写**）|
| `nhms-api-forecast` | `apps/api/routes/forecast.py` 的 forecast store 连接 |
| `nhms-api-data-sources` | `apps/api/routes/data_sources.py` 的 forecast store + 气象站元数据查询 |
| `nhms-api-best-available` | `apps/api/routes/best_available.py` 的 best-available 仓库 |
| `nhms-api-models` | `apps/api/routes/models.py` 的 model registry store（**会写**）|
| `nhms-api-state-snapshots` | `apps/api/routes/state_snapshots.py` 的 state snapshot 仓库 |
| `nhms-ts-retention` | `scripts/node27_timeseries_retention.py`（retention timer）|
| `nhms-ts-compression` | `scripts/node27_timeseries_compression.py`（compression timer）|
| `nhms-raw-retention` | `scripts/node27_raw_retention.py`（raw-retention timer；只做 watermark 只读查询）|
| `psql` | 人工会话 |
| `TimescaleDB Background Worker Scheduler` | TimescaleDB 后台 worker，不要动 |
| 空串 | 未在册的连接面（`services/*`、qhh 系脚本、`workers/grid_registry` 等）；先查清来源再处置。三条只读监控 lane——`scripts/node27_frontier_stall_alert.py`、`scripts/node27_coverage_freshness_alert.py`、`scripts/node27_resource_governance.py`（都以 `nhms_display_ro` 连库）——代码里也没设名字，DSN 上不带 `?application_name=` 时同样是空串。**`nhms-display-api.service` 自 #1728 起七个连接面全部具名**，所以空串一定不是 display API |

在册组件**委托给共享 helper 打开的连接**同样带自己的名字：
`packages/common/display_watermark.py` 的 watermark 只读查询（retention /
compression / raw-retention 每个 tick 的第一条连接）与
`packages/common/display_coverage.py` 的 per-run coverage worker 连接
（`--all` 下最多 8 条并发，正是最容易被误 cancel 的长连接）。也就是说
**看到空串就一定不是在册生产 tick**，可以按上表照直处置。

`nhms-display-api.service`（uvicorn `apps.api.main:app`，两个 worker）在同一进程里托管七个
连接面，上表把它们拆成七个名字：`nhms-display-api` 是只读展示池，`nhms-api-pipeline` 与
`nhms-api-models` 是**控制面写入**。名字由 route 层注入（`packages/common/*` 的 store 不
硬编码任何名字），DSN 上的 `?application_name=` 依旧优先。静态闭包由
`tests/test_node27_connection_attribution.py` 守着：从 `apps/api/route_registry.py` 出发
遍历 import 图，新增的 router 或 store 连接面必须具名或写明 `unreachable` 理由。

处置纪律：

- **执行 `pg_cancel_backend` / `pg_terminate_backend` 之前，先用
  `application_name` 归因。** 归因不出来就别取消。

  ```sql
  select pid, usename, application_name, client_addr, state,
         now() - query_start as dur, left(query, 80)
  from pg_stat_activity
  where datname = 'nhms'
  order by query_start;
  ```

- **生产 tick（`nhms-autopipe` / `nhms-ingest-run` / `nhms-output-parser` /
  `nhms-refresh-coverage` / `nhms-ts-retention` / `nhms-ts-compression` /
  `nhms-raw-retention`）不得随手取消。**
  它们本来就有分钟级的正常时长。要停就停对应的 systemd unit（`systemctl --user stop
  nhms-node27-autopipe.timer` 等）并在日志里留痕；慢查询本身属于容量/计划问题，
  走 issue，不走 cancel。
- **integration 测试一律经 `NHMS_INTEGRATION_DATABASE_URL`**（`tests/conftest.py`
  无该 opt-in 时无条件 skip 全部 integration，并且每次建 `nhms_it_<uuid>` throwaway 库
  再 drop）。不要用裸生产 `DATABASE_URL` 跑 pytest —— 那正是把测试会话和生产 tick
  混在一张 `pg_stat_activity` 里的起点。
- **display API 的写入面（`nhms-api-pipeline` / `nhms-api-models`）取消前先看日志。**
  自 #1704 起每个**经过 `error_response()` 的**错误响应都会在 `/tmp/display-api.log` 留一行
  `api_error request_id=… code=… status=… path=… details=…`（5xx 记 ERROR，4xx 记
  WARNING），用客户端拿到的 `X-Request-ID` 直接 grep 即可把一条 backend 对上一次请求：

  ```bash
  grep -F "<X-Request-ID>" /tmp/display-api.log
  ```

  该行里的 `details` 已按审计口径脱敏（绝对路径/URI/校验和/敏感 key 以及 `rejected_value` /
  `rejected_values` 一律 `[redacted]`），所以它能定位问题但不能替代复现客户端请求；
  **但它不是全量脱敏**——其它 key 下的客户端标识（`station_id`、`run_id` 等）保持明文，
  详见 `docs/runbooks/object-store-forcing-series-read.md` 的「脱敏边界」。`details=` 段有固定
  字节预算，超出以 `…[truncated N bytes]` 截断（响应体不截断）；入站 `X-Request-ID` 仅在匹配
  `[A-Za-z0-9._-]{1,64}` 时沿用，否则服务端另发 UUID；`path=` 段按 `quote(path, safe="/")`
  percent-encoding 后再写，客户端可控的路径参数里塞不进空格、`=` 或控制字节（只含 unreserved
  字符与 `/` 的路径逐字节不变；`:` `@` `+` 等 sub-delims 会被编成 `%XX`）。
  `details=` 段在截断前先把换行类字符转义（`\n` → `\\n` 等），所以一次错误响应永远只有一行；
  但段内的值是逐字原样的，出现 `code=…` 这种 token 仿冒串属正常——按 `details=` 之前的位置解析
  字段，别全行扫 token。
  另外，合规形状的 `X-Request-ID` 由客户端自选，grep 命中只证明同一行日志里有这个 id，不证明来源。

  已知盲区（grep 不到 ≠ 没发生）：`/api/v1/slurm*` 的**全部**错误响应（校验错误走
  `services/slurm_gateway/validation_errors.py` 的独立 handler；网关错误由
  `services/slurm_gateway/routes.py:149` 与 `_gateway_error_response`（:212-217）直接构造
  `JSONResponse`）；Starlette 自己应答的 `HTTPException`——未匹配路由的 404（含
  `apps/api/startup_wiring.py:87` 的 SPA catch-all）与 405；以及被
  `ServerErrorMiddleware` 接住的未捕获异常（写出的是 uvicorn traceback，不是 `api_error` 行）。
- 运维需要临时覆写标识时，在 DSN 上写 `?application_name=<name>`：代码给的是
  libpq `fallback_application_name`，显式值永远优先。
