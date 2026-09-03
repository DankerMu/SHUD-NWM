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
- `forcing_version_id` 参数保留兼容形状；只有与完整 `model_id + source_id + cycle_time` tuple 同传时才会被接受并忽略。单独只传 `forcing_version_id` 会返回 `MISSING_REQUIRED_FILTER`。
- `variables` 省略、空字符串或纯空白时等同于默认变量集，返回 `PRCP`、`TEMP`、`RH`、`wind`、`Rn`；非空但不支持的变量名（例如 `Press` 或未知变量）会被静默丢弃。
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

### `parse_reason` 的 `concurrent-replace` 前缀

producer 用 `os.replace` 原子换入新 inode 发布 `shud/*.csv`，读侧的 no-follow open 在
pre-open stat 与 post-open fstat 之间比对 inode 身份，命中替换窗口就拒绝。读侧对这类拒绝做**有界重试**
（上限 3 次尝试 = 首次 open 加 2 次重试；只重试 open，不重试 parse；不 sleep）。若重试次数用尽仍每次都撞上替换窗口，接口仍按原样返回
HTTP 500 `STATION_FORCING_FILE_MALFORMED`（状态码与错误码都不变），但
`details.parse_reason` 会以固定 token `concurrent-replace:` 加一个空格开头。

排查动作：

- **看到该前缀** = 该次请求读到了 producer 的原子替换窗口且重试耗尽，**不是文件损坏**，不要按坏 CSV 去查
  header/列数/数值。先看该 cycle 的 producer 是否正在写同一批 `shud/` 文件；偶发一两次属预期，重试即可自愈。
- 若持续复现（同一 station 在无 producer 活动的时段仍带该前缀），才升级排查：确认是否有计划外进程在反复重写该路径。
- 该前缀是**单向**判据：有前缀说明命中的是替换窗口；**不能**反过来把「没有该前缀」读成「文件已损坏」——
  没有前缀只表示这次失败不是耗尽的 inode 竞态，其余成因（权限、I/O、CSV 契约违例、bounded-read 越界）仍需按上表逐项排查。

### 事后定位：用 `X-Request-ID` grep display 日志

`concurrent-replace` 这类 500 往往在用户报障时早已过去，重放请求也复现不出来。自 #1704 起
`error_response()` 会为**每一个经过它的**错误响应（`ApiError` 与 slurm 前缀以外的请求校验错误；
不经过它的三类见下文「已知盲区」）在 display unit 的 stderr（systemd
`StandardError=append:/tmp/display-api.log`）写一行，用响应头 `X-Request-ID` 就能捞回来：

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'grep -F "<X-Request-ID>" /tmp/display-api.log'
```

**这个 grep 证明的是「同一行日志里出现了这个 id」，不是「这条日志来自报障的那个客户端」**：
合规形状的入站 `X-Request-ID` 会被原样沿用（见下文），客户端可以自选、也可以复用别人用过的 id，
甚至多个请求共用一个。把 grep 命中当作定位线索用，不要当作来源归属的证据。

这行落地到 stderr 的方式是 `apps/api/main.py::_install_api_log_handler()` 给 `apps.api` logger
树显式装的 handler，而**不是**复用 uvicorn 的 `uvicorn.error`：生产 unit 跑
`python -m uvicorn apps.api.main:app` 且**不传 `--log-config`**，所以 root logger 未被配置，
不自带 handler 的话这行会被直接丢掉。改动 unit 的启动参数或日志配置时，先确认这条前提还成立。

一行的形状（5xx 记 ERROR，4xx 记 WARNING）：

```text
2026-09-02 08:30:09,835 ERROR apps.api.errors api_error request_id=<id> code=STATION_FORCING_FILE_MALFORMED status=500 path=/api/v1/met/stations/<id>/series details={'station_id': '…', 'expected_path': '[redacted]', 'parse_reason': 'concurrent-replace: …'}
```

**脱敏取舍（有意为之）**：`details` 先按 key 抹掉 `rejected_value` / `rejected_values`，
再过审计用的 `redact_audit_payload`。代价是 `expected_path` 这类绝对路径整串变成
`[redacted]`——日志里拿不到具体文件名，要定位文件请按 `station_id` + cycle 目录自己推。
`parse_reason` 明文保留，因为它是这行存在的理由；不要往 `parse_reason` 里塞路径或用户输入。

**脱敏边界（不是「全量脱敏」，别按全量脱敏对外导出）**：无条件按 key 抹掉的**只有**
`rejected_value` 与 `rejected_values` 这两个「原样回显客户端输入」的键。挂在**其它** key 下的
客户端可控值（`station_id`、`layer_id`、`run_id`、`cycle_time` 等）**保持明文**，除非它本身是
路径/URI/校验和形状，或落在 `redact_audit_payload` 的敏感 key 名单里。上面那行样例里的
`details={'station_id': '…', …}` 就是明文的客户端输入——那是**有意**的（这行要能回答「哪个站
挂了」），但也意味着：把 `/tmp/display-api.log` 交给第三方或搬出生产环境前，必须按「含客户端标识
明文」处理，不能当成已完全脱敏的日志。

**行长上限**：单行的 `details=` 段有固定字节预算（`apps/api/errors.py::_DETAILS_RENDER_BUDGET_BYTES`，
当前 8192 B），超出部分截断并以 `…[truncated N bytes]` 结尾。这是为了防止一次校验失败的大 body
（每个非法元素一条 `rejected_value` 记录）把几 MB 写进未做 rotate 的 unit 日志。**响应体不受影响**，
客户端拿到的仍是完整 `details`；也就是说日志里看到截断标记时，完整清单要去复现请求或看响应体。
`path=` 段共用同一个字节预算：路径长度只受服务器请求行上限约束，而 percent-encoding 还会放大它
（一条 40 KiB 全 `%FF` 的路径解码成 U+FFFD 后编码成 `%EF%BF%BD`，实测单行 123 KB），所以超预算时
`path=` 也会截断，标记是同一个 marker 的 percent 形式 `%E2%80%A6%5Btruncated%20N%20bytes%5D`
（不含空格，`path=` 仍是单个 token）。因此单行长度上限约为 2 × 8192 B 加上两个 marker 与前缀。

**请求 ID 只在合规形状下回显**：入站 `X-Request-ID` 仅当整体匹配 `[A-Za-z0-9._-]{1,64}` 时才被沿用，
否则服务端另发 UUID（响应头、审计记录、这行日志三者始终一致）。所以行里 `request_id=` 后面不可能被
客户端塞进空格分隔的假 `code=` / `path=` 字段。

**`path=` 段做 percent-encoding**：`request.url.path` 是**解码后**的路径，带路径参数的路由上
那一段是客户端可控的，所以 `path=` 统一按 `quote(path, safe="/")` 渲染后再写行。效果是：
`path=` 永远是一个不含空格、不含 `=`、不含控制字节的 token（空格 → `%20`，`=` → `%3D`，
`NUL` → `%00`，`ESC` → `%1B`），不会把这行拆成两行。只由 unreserved 字符（`A-Za-z0-9._~-`）
和 `/` 组成的路径逐字节不变，上面的样例行形状不受影响；路径里若出现 `:` `@` `+` `,` `;` `!` `$`
`(` `)` 等 sub-delims，也会被编成 `%XX`，grep 时按编码后的形式写。
因此 `grep -F request_id=<id>` 不会被伪造字段带偏，也不会因为路径里塞了 `%00`
而让 `grep` 把整个未 rotate 的日志报成 “Binary file matches”。
注意 TAB/CR/LF（`%09`/`%0D`/`%0A`）在 `urlsplit` 阶段就被剥掉了，日志里既看不到原字符也看不到
它们的 percent 形式。

**`details=` 一定不断行，但不做 token 净化**：渲染 `details=` 时会把所有换行类字符
（`\n` `\r` `\x0b` `\x0c` `\x85` U+2028 U+2029）转义成 `\\n` 这类可见形式**再**做字节预算截断，
所以无论 `details` 是 mapping、list 还是裸字符串，一次错误响应永远只写一行。但**内容是逐字原样**的：
mapping 的值另有 `repr` 的引号，裸字符串则连引号都没有。也就是说
`details={'station_id': 'STA code=OK status=200'}` 这种「看起来像字段」的串会出现在行里——它属于
`details=` 段，不构成第二组真字段。按位置解析（取 `details=` 之前的部分）是可靠的，
按 `code=` 之类 token 全行扫描则会被 `details=` 里的仿冒串误导。

**已知盲区**（这三类错误响应**不**产生 `api_error` 行，别把「grep 不到」读成「没发生」）：

1. `/api/v1/slurm*` 前缀的**全部**错误响应。请求校验错误由
   `services/slurm_gateway/validation_errors.py` 的独立 handler 应答；网关自身的错误由
   `services/slurm_gateway/routes.py:149` 与 `_gateway_error_response`（:212-217）直接构造
   `JSONResponse`。两条路都不经过 `error_response()`。排查 slurm 网关请按该网关自己的口径。
2. Starlette 自己应答的 `HTTPException`：未匹配路由的 404（含 `apps/api/startup_wiring.py:87`
   SPA catch-all 对 `api/` 前缀抛的 404）与 405 method-not-allowed。
3. 未被捕获的异常：由 `ServerErrorMiddleware` 接住，写出的是 uvicorn 的 traceback 而不是
   `api_error` 行——按 traceback 的时间戳对齐，不要指望 `X-Request-ID`。

`PsycopgForecastStore.station_series()` 仍保留在 `packages/common/forecast_store.py`，
但它现在是 legacy/internal DB helper：只用于保留历史 DB 合同测试和 ADR 0001 所述的
长期历史 API 设计，不是当前 display station-series route 的实现。生产 route/service
代码不得把 disk miss 静默降级到该 helper；如果 retention 外历史回看成为产品需求，应新增独立端点
或显式 mode，而不是复活旧 fallback。

## Variable Filter Semantics

`variables` 的空值语义和不支持变量语义不同：

- 未传 `variables`、`variables=` 或只包含空白/逗号空段时，按未过滤处理，返回当前 SHUD CSV 支持的默认五个变量。
- 传入非空变量名时，只返回支持的变量；`Press` 和未知变量不会报错，但会被从结果集中省略。
- 例如 `variables=Press` 返回 200 且 `data.series=[]`，`variables=PRCP,Press` 只返回 `PRCP`。

## Disk Retention Window

当前 API 是 disk-first 且 disk-only：可查询窗口等于 node-27 上 `/home/ghdc/nwm/object-store/forcing/{source}/` 下仍保留的 cycle 目录集合。数据库里曾经 finalized 的老 cycle 不代表 disk CSV 仍在。

查看当前保留窗口：

```bash
find /home/ghdc/nwm/object-store/forcing/ifs /home/ghdc/nwm/object-store/forcing/gfs \
  -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

老 cycle 超出 disk retention 后，station-series 应返回 `STATION_FORCING_FILE_NOT_FOUND`。这是当前契约，不是降级路径失败；本 PR 不做 DB fallback。长期历史回看边界见
`docs/adr/0001-station-forcing-history-api-boundary.md`：如需 DB/archive 历史回看，必须是独立 archive/history API
或显式 opt-in mode，不能作为当前 route 的静默 fallback。

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

## Related References

- `docs/runbooks/current-production-ops.md` §5.4：仅作为 shared object-store
  copyback 和 node-27 mirror 路径上下文；不要把该 runbook 当作当前 DB/服务拓扑权威。
- `docs/runbooks/display-readonly-live-mvt.md`：live MVT 历史 receipt、开关和重启脚本口径；
  当前 display DB/env 以 `infra/env/display.example`、`docs/governance/ROLE_BOUNDARY.md`、
  `docs/runbooks/two-node-deployment-overview.md` 和
  `docs/runbooks/two-node-production-e2e-plan.md` 为准。
- `docs/runbooks/production-service-config.md`：生产配置模板中的 object-store env 分类。
