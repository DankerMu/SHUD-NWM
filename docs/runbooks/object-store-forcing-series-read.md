# Object-store Forcing Series Read Runbook

本文记录 display API 直读 object-store SHUD 站点 forcing CSV 的生产配置、排障口径和上下游协作约定。
PR-A #627 已实现 chunked/bounded object-store CSV reader；PR-B #628 已把 station-series route 切到 direct disk read，
新增 `OBJECT_STORE_ROOT` runtime config，并在 node-27 通过 live receipt。

## Operator Contract

公开接口仍是：

```text
GET /api/v1/met/stations/{station_id}/series?model_id=...&source_id=...&cycle_time=...
```

读路径的当前契约：

- `met.met_station` 仍是站点元数据来源；API 用它查 `basin_version_id`、坐标、高程、角色、active flag 和 `properties_json.forcing_filename`。
- 序列值不再读取 `met.forcing_station_timeseries`，也不再经过 `met.forcing_version` finalize gate。
- `forcing_version_id` 参数保留兼容形状；新路径需要 `model_id + source_id + cycle_time`，单独只传 `forcing_version_id` 会返回 `MISSING_REQUIRED_FILTER`。
- disk 缺文件即返回 `STATION_FORCING_FILE_NOT_FOUND`；不会 fallback 到 DB。

## Required Runtime Config

node-27 display API 必须在 runtime env 中配置：

```bash
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
```

| 项 | 值 |
|---|---|
| 节点 | node-27 (`nwm@210.77.77.27:32099`) |
| 期望值 | `/home/ghdc/nwm/object-store` |
| 配置位置 | node-27 的 display runtime env，例如 `infra/env/display.env` |
| 权限要求 | display API 进程用户可读、可遍历；不要求可写 |
| 模板 | `infra/env/display.example` 已包含同值示例 |

检查命令：

```bash
grep '^OBJECT_STORE_ROOT=' infra/env/display.env
test -d /home/ghdc/nwm/object-store
test -r /home/ghdc/nwm/object-store
test -x /home/ghdc/nwm/object-store
```

改 env 后按当前 display API 启动脚本重启：

```bash
bash scripts/ops/start-display-api.sh
```

## Startup Troubleshooting

| 症状 | 触发条件 | 处置 |
|---|---|---|
| `RuntimeModeError` / `OBJECT_STORE_ROOT_REQUIRED` | `NHMS_SERVICE_ROLE=display_readonly` 启动时未设置 `OBJECT_STORE_ROOT` | 在 node-27 display env 写入 `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`，再重启 display API |
| `RuntimeModeError` / `OBJECT_STORE_ROOT_UNREADABLE` | env 指向不存在、不可读或不可遍历的目录 | 核对挂载是否存在，目录和所有父目录是否有 execute bit；display 用户只需读和遍历，不应为排障放宽成可写 |
| `DISPLAY_BOUNDARY_CONFIG_UNSAFE` 提到 `OBJECT_STORE_ROOT` | 运行代码仍是 PR-B 前的 role-boundary 规则，或部署未同步 `scripts/validate_two_node_docker_runtime.py` / runtime 常量 | 先同步并部署 PR-B 后代码；不要通过删除 `OBJECT_STORE_ROOT` 绕过，因为新 series 路径启动必需该 env |
| 默认本地 import 不要求 `OBJECT_STORE_ROOT` | `NHMS_SERVICE_ROLE` 未设置时是 `dev_monolith` 兼容路径 | 这是预期；生产 display_readonly 仍必须配置 |

## Disk Layout And Producer Coordination

station-series reader 只读 forcing producer 已发布到共享 object-store mirror 的 SHUD CSV：

```text
/home/ghdc/nwm/object-store/forcing/{source}/{YYYYMMDDHH}/{basin_version_id}/{model_id}/shud/X<lon>Y<lat>.csv
```

示例：

```text
/home/ghdc/nwm/object-store/forcing/ifs/2026062012/basins_heihe_vbasins/basins_heihe_shud/shud/X100.75Y37.65.csv
```

上下游分工：

- `forcing_producer` 负责按 source cycle、basin version、model 生成 SHUD forcing package，并保持 `shud/` 子目录和 `X<lon>Y<lat>.csv` 文件名。
- copyback/publish 流程负责把 compute 侧 object-store staging 同步到共享 mirror；node-27 读取的是 `/home/ghdc/nwm/object-store`。
- API 读侧按 `source_id` lowercase、UTC `cycle_time -> YYYYMMDDHH`、`model_id` 和站点元数据里的 `basin_version_id + forcing_filename` 组装路径。
- CSV 必须保持 SHUD 契约：首行 `nrow ncol start_date end_date`，列头 `Time_Day Precip Temp RH Wind RN`，单位为 `mm/day, degC, 0-1, m/s, W/m^2`。
- reader 是 bounded read：单文件按 chunk 读取，并限制行数、文件大小和单行长度；不得让 API 读取任意大文件。

## Station-series Errors

| HTTP | code | 触发条件 |
|---:|---|---|
| 422 | `MISSING_REQUIRED_FILTER` | 请求未同时提供 `model_id`、`source_id`、`cycle_time`。只传旧 `forcing_version_id` 也会触发该错误 |
| 404 | `STATION_NOT_FOUND` | `met.met_station` 查不到 `station_id` |
| 500 | `STATION_FORCING_FILENAME_MISSING` | 站点存在，但 `properties_json.forcing_filename` 缺失或为空 |
| 404 | `STATION_FORCING_FILE_NOT_FOUND` | 按模板解析出的 disk CSV 不存在，包括 cycle 目录已被 retention 清理的情况 |
| 500 | `STATION_FORCING_FILE_MALFORMED` | 文件存在但不可安全读取或 CSV 不满足契约，例如 unsafe path segment、symlink/no-follow 拒绝、header/列数/数值非法、超过 bounded-read 限制 |

旧 DB-backed 路径上的 `FORCING_VERSION_NOT_FOUND` / `FORCING_VERSION_NOT_FINALIZED` 不应再从该 station-series route 产生。新路径不查 `met.forcing_version` readiness，所以不要用这些 code 排查 disk 读问题。

## Disk Retention Window

当前 API 是 disk-first 且 disk-only：可查询窗口等于 node-27 上 `/home/ghdc/nwm/object-store/forcing/{source}/` 下仍保留的 cycle 目录集合。数据库里曾经 finalized 的老 cycle 不代表 disk CSV 仍在。

查看当前保留窗口：

```bash
find /home/ghdc/nwm/object-store/forcing/ifs /home/ghdc/nwm/object-store/forcing/gfs \
  -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

老 cycle 超出 disk retention 后，station-series 应返回 `STATION_FORCING_FILE_NOT_FOUND`。这是当前契约，不是降级路径失败；本 PR 不做 DB fallback。长期历史回看是否走 DB 另见 Follow-ups。

## Role Boundary

`display_readonly` 现在可以合法读取 `OBJECT_STORE_ROOT`，因为该路径承载对外展示所需的 disk-resident forcing CSV。边界变化只放开“只读读取共享 object-store mirror”，不放开 compute mutation：

- display 仍不应运行 Slurm、producer、orchestrator 或任何写 object-store 的任务。
- `OBJECT_STORE_ROOT` 从 display forbidden compute-path env 中移除，并纳入 display required/audited runtime env。
- 安全边界由只读 DB role、目录权限、reader 无副作用测试和 no-follow/bounded-read 共同保证。

## Operational Checks

成功路径应满足：

```bash
curl -sS 'https://test.nwm.ac.cn/api/v1/met/stations/heihe_forc_001/series?model_id=basins_heihe_shud&source_id=ifs&cycle_time=2026-06-20T12:00:00Z&variables=PRCP,TEMP' \
  | jq '.data.series[].variable'
```

老 cycle 或已清理 cycle 应满足：

```bash
curl -sS 'https://test.nwm.ac.cn/api/v1/met/stations/heihe_forc_001/series?model_id=basins_heihe_shud&source_id=ifs&cycle_time=2020-01-01T00:00:00Z' \
  | jq '.error.code'
```

期望错误码是 `STATION_FORCING_FILE_NOT_FOUND`。

## Follow-ups

- #629 Frontend: cycle picker adapt to disk retention window
- #630 PsycopgForecastStore.station_series cleanup or deprecation
- #631 Evaluate long-term forcing series API via DB read

## Related References

- `docs/runbooks/current-production-ops.md` §5.4：shared object-store copyback 和 node-27 mirror 路径。
- `docs/runbooks/display-readonly-live-mvt.md`：display_readonly runtime env 和重启脚本口径。
- `docs/runbooks/production-service-config.md`：生产配置模板中的 object-store env 分类。
