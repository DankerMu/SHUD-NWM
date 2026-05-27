# Two-Node Production-Like E2E Plan

最后更新：2026-05-27  
适用范围：22 节点计算控制面 + 27 节点展示服务面  
推荐证据目录：`artifacts/two-node-e2e/<run_id>/`

## 1. 文档目的

本文用于明确当前项目从单机 production-like 验证调整为两块部署后的职责边界和端到端测试范围。

两块部署目标：

- 22 节点负责资料下载、调度控制、Slurm Gateway、计算任务提交和计算侧证据。
- 27 节点负责 API、前端、查询展示、运维页面和展示侧证据。

原则：

- 27 节点不直接控制 Slurm 计算，不要求安装 Slurm CLI。
- 27 节点不直接挂载 22 节点本地工作目录作为强依赖。
- 22 节点和计算节点负责完成数据生产闭环。
- 27 节点通过数据库和对象存储/发布产物消费结果。
- E2E 证据必须区分计算控制面通过、展示服务面通过和跨面联调通过。

## 2. 部署块职责

### 2.1 22 节点：计算控制面

职责：

- 运行 GFS/IFS 下载和周期发现。
- 运行 `nhms-pipeline plan-production`。
- 提供或访问 Slurm Gateway。
- 提交 canonical、forcing、SHUD runtime、output parse、frequency、publish 等计算任务。
- 维护计算节点可见的 Basins、workspace、object-store、logs 路径。
- 把 pipeline/run/job/stage 状态写入 PostgreSQL。
- 把可展示产品、manifest、日志和诊断证据写入对象存储或共享发布区。

22 节点需要的能力：

- `uv` + Python 3.11+ 环境。
- 可访问 PostgreSQL。
- 可访问外部资料源或资料缓存。
- 可访问 Slurm controller，或本机运行 Slurm Gateway 且 Gateway 后端可提交真实 job。
- 可访问计算节点共享路径。
- 可运行 SHUD 相关 sbatch 模板所需的环境。

关键配置：

```bash
DATABASE_URL=...
SLURM_GATEWAY_BACKEND=slurm
WORKSPACE_ROOT=...
OBJECT_STORE_ROOT=...
OBJECT_STORE_PREFIX=...
NHMS_BASINS_ROOT=...
SHUD_EXECUTABLE=...
```

如果 Slurm Gateway 独立部署，还需要：

```bash
SLURM_GATEWAY_URL=http://<gateway-host>:<port>
```

### 2.2 计算节点

职责：

- 执行 Slurm 分配的下载、转换、forcing、SHUD、解析和发布任务。
- 读取 Basins/model assets、workspace 输入和对象存储输入。
- 写回运行日志、产物、manifest 和状态文件。
- 通过网络访问 PostgreSQL 或由任务脚本间接写入必要状态。

计算节点必须证明：

- 能访问 `NHMS_BASINS_ROOT`。
- 能读写 `WORKSPACE_ROOT`。
- 能读写 `OBJECT_STORE_ROOT` 或访问对象存储。
- 能访问 `DATABASE_URL` 对应数据库。
- 能运行 `SHUD_EXECUTABLE`。

### 2.3 27 节点：展示服务面

职责：

- 运行 FastAPI。
- 服务前端静态产物或作为前端 API 后端。
- 提供 `/hydro-met`、`/ops`、forecast、pipeline、jobs、logs、models 等查询 API。
- 读取 PostgreSQL 中的模型、气象、hydro、pipeline 和 ops 状态。
- 读取对象存储/发布区中可展示产物和日志。
- 执行前端单测、构建、API contract 和浏览器展示 E2E。

27 节点不负责：

- 不运行 `nhms-pipeline plan-production` 作为正式计算调度入口。
- 不直接提交 Slurm job。
- 不依赖本地复制的 22 工作目录来完成计算。

关键配置：

```bash
DATABASE_URL=...
OBJECT_STORE_ROOT=...
OBJECT_STORE_PREFIX=...
S3_ENDPOINT_URL=...
S3_BUCKET_NAME=...
AUTH_BACKEND=...
CORS_ALLOWED_ORIGINS=...
```

如果 `/ops` 需要读取文件日志，27 必须能访问日志发布位置。优先使用对象存储或受控共享发布区，而不是挂载 22 的完整工作目录。

## 3. 共享依赖边界

| 依赖 | 22 节点 | 计算节点 | 27 节点 | 说明 |
| --- | --- | --- | --- | --- |
| PostgreSQL/PostGIS/TimescaleDB | 读写 | 读写或间接写 | 读 | pipeline、met、hydro、model、ops 状态源 |
| Basins/model assets | 读 | 读 | 通常不需要 | 计算侧 source of truth |
| WORKSPACE_ROOT | 读写 | 读写 | 通常不需要 | 计算中间态，不建议作为 27 强依赖 |
| OBJECT_STORE_ROOT/S3 | 读写 | 读写 | 读，必要时有限写 | 展示产品、manifest、日志发布面 |
| Slurm Gateway | 提交/查询 | 不适用 | 通常不调用 | 控制链路归 22 |
| API/frontend | 不适用 | 不适用 | 提供服务 | 展示和运维入口 |

## 4. E2E 总体策略

E2E 拆成三段：

1. 22 计算控制面 E2E：证明资料下载到计算产物发布的生产链路闭环。
2. 27 展示服务面 E2E：证明 API/前端能消费已发布产物并展示运维状态。
3. 跨面联调 E2E：用同一个 `run_id/source/cycle/model_id` 贯穿 22 生产和 27 展示。

验收时必须记录每段状态：

```text
compute_control_plane: PASS | PARTIAL | FAIL | BLOCKED
display_service_plane: PASS | PARTIAL | FAIL | BLOCKED
cross_plane_e2e: PASS | PARTIAL | FAIL | BLOCKED
```

## 5. 证据目录

建议每次使用统一 `run_id`：

```bash
export RUN_ID="two-node-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_ROOT="artifacts/two-node-e2e/$RUN_ID"
mkdir -p "$EVIDENCE_ROOT"/{22-compute,27-display,cross-plane,db,api,browser,slurm,logs}
```

最低交付物：

- `environment.md`：22、27、DB、对象存储、Gateway、计算节点版本和路径。
- `command_index.md`：所有命令、执行节点、退出码和日志路径。
- `22-compute/summary.md`：计算控制面结论。
- `27-display/summary.md`：展示服务面结论。
- `cross-plane/summary.md`：跨面联调结论。
- `summary.md`：最终 PASS/PARTIAL/FAIL/BLOCKED 汇总。
- `bugs.md` 或链接到 `docs/bugs.md`：失败项、根因、复测条件。

## 6. 22 计算控制面 E2E

### 6.1 环境预检

在 22 节点执行：

```bash
hostname
git rev-parse HEAD
uv run python --version
uv run ruff check .
uv run pytest -q tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_shud_runtime.py tests/test_output_parser.py
```

记录：

- 当前 commit。
- Python/uv 版本。
- `.venv` 是否为 22 本机重建。
- `DATABASE_URL` 脱敏摘要。
- `WORKSPACE_ROOT`、`OBJECT_STORE_ROOT`、`NHMS_BASINS_ROOT` 是否存在。

### 6.2 数据源预检

目标：

- GFS 至少一个最新或指定 cycle 可发现。
- IFS 至少一个最新或指定 cycle 可发现。
- dry-run 的 side effect 必须被记录，不能误称无副作用。

建议命令：

```bash
uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --model-id basins_qhh_shud \
  --basin-id basins_qhh \
  --workspace-root "$WORKSPACE_ROOT"
```

通过条件：

- 只选中 QHH 模型和 QHH basin。
- 输出包含候选 cycle、source、model_id、basin_id。
- 如 discovery 会下载探针文件，证据中明确标注。

### 6.3 Slurm Gateway 和计算节点预检

如果 22 本机运行 Gateway：

```bash
curl -i http://127.0.0.1:<port>/api/v1/slurm/health
```

如果 Gateway 独立部署：

```bash
curl -i "$SLURM_GATEWAY_URL/api/v1/slurm/health"
```

必须提交一个最小计算节点 probe，证明计算节点可见核心路径：

```bash
hostname
date -u
ls "$NHMS_BASINS_ROOT/qhh"
touch "$WORKSPACE_ROOT/probe-from-compute-$(hostname)"
touch "$OBJECT_STORE_ROOT/probe-from-compute-$(hostname)"
```

通过条件：

- Gateway health 通过。
- probe job 成功完成。
- 计算节点 hostname、exit code、stdout/stderr、accounting 信息留证。
- 计算节点能读 Basins，能写 workspace/object-store。

### 6.4 正式生产链路执行

在 22 节点执行正式入口：

```bash
uv run nhms-pipeline plan-production \
  --plan \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --model-id basins_qhh_shud \
  --basin-id basins_qhh \
  --workspace-root "$WORKSPACE_ROOT" \
  --evidence-dir "$EVIDENCE_ROOT/22-compute/scheduler"
```

必须覆盖阶段：

- source download
- canonical convert
- forcing produce
- SHUD runtime
- output parse
- flood/frequency 或 MVP 所需 result product
- publish display products
- pipeline persistence

通过条件：

- 每个 source 至少一个 cycle 有完整 stage 记录。
- Slurm job id 或 Gateway receipt 已写入 pipeline job。
- 每个 job 有可追踪 `log_uri`。
- 失败任务必须有 error code、error message 和重试判断。
- 计算结果写入 DB 和对象存储/发布区。

### 6.5 计算结果数据库检查

对同一个 `source/cycle/model_id/basin_id` 检查：

- `met` canonical/forcing 元数据存在。
- `met.forcing_station_timeseries` 覆盖 `PRCP/TEMP/RH/wind/Rn/Press`。
- `hydro.river_timeseries` 覆盖 `q_down`。
- `ops.pipeline_jobs` 有完整 stage/job 状态。
- `log_uri` 指向 27 可读取的发布日志位置。

通过条件：

- 不允许只用历史旧 cycle 冒充本轮生产结果。
- 如 q_down 只覆盖子集，必须标注 coverage 和 readiness 合同，不得声明全量 latest-product ready。

## 7. 27 展示服务面 E2E

### 7.1 环境预检

在 27 节点执行：

```bash
git rev-parse HEAD
uv run python --version
uv run ruff check .
uv run pytest -q tests/test_api.py tests/test_gateway.py tests/test_forecast_api.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py
cd apps/frontend
corepack pnpm test
corepack pnpm exec tsc --noEmit
corepack pnpm run check:api-types
corepack pnpm build
corepack pnpm check:bundle
```

通过条件：

- 后端 focused tests 通过。
- 前端 test/type/build/bundle 通过。
- FastAPI `/health` 通过。
- 27 配置不包含 Slurm CLI 作为必需项。

### 7.2 API 服务启动检查

启动 27 API：

```bash
uv run python -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
```

检查：

```bash
curl -i http://127.0.0.1:8000/health
curl -i 'http://127.0.0.1:8000/api/v1/models?active=true&limit=20'
curl -i 'http://127.0.0.1:8000/api/v1/met/stations?model_id=basins_qhh_shud&limit=5'
```

通过条件：

- API 服务可启动。
- DB 连接正常。
- QHH active model 和 stations 可查。

### 7.3 展示产品 API 检查

针对 22 本轮生产出来的 `source/cycle`：

```bash
curl -i 'http://127.0.0.1:8000/api/v1/mvp/qhh/latest-product?source=GFS'
curl -i 'http://127.0.0.1:8000/api/v1/mvp/qhh/latest-product?source=IFS'
```

通过条件：

- 至少一个 source 的 latest-product 返回 200。
- 响应中包含 run/version/cycle/readiness 元数据。
- 如果返回 404，必须记录具体 blocker，例如 `SEGMENT_COUNT_MISMATCH` 或 `Q_DOWN_VALID_TIME_MISSING`。

### 7.4 Station Series 和 Forecast Series 检查

使用 latest-product 返回的 station、forcing_version、segment、river_network_version 检查：

```bash
curl -i '<station-series-url>'
curl -i '<forecast-series-q-down-url>'
```

通过条件：

- station series 返回六个 MVP 变量。
- forecast series 返回 `q_down`。
- valid time、issue time、source/scenario 和单位语义正确。

### 7.5 Ops API 检查

针对同一个本轮生产 cycle：

```bash
curl -i 'http://127.0.0.1:8000/api/v1/pipeline/status?source=GFS&cycle_time=<cycle>'
curl -i 'http://127.0.0.1:8000/api/v1/pipeline/stages?source=GFS&cycle_time=<cycle>'
curl -i 'http://127.0.0.1:8000/api/v1/jobs?source=GFS&cycle_time=<cycle>&limit=20'
curl -i 'http://127.0.0.1:8000/api/v1/jobs/<job_id>/logs'
```

通过条件：

- status/stages/jobs 对同一个 cycle 返回一致结果。
- jobs 表中能看到 22/Gateway 产生的真实 job id 或 Gateway receipt。
- `/logs` 能读取日志，或者明确返回日志缺失原因。

### 7.6 浏览器 E2E

要求新增或使用无 API mock 的浏览器测试。现有含 `page.route('**/api/v1/**')` 的测试只能算 mocked regression。

必须覆盖：

- `/hydro-met` 使用真实 API bootstrap latest-product。
- GFS/IFS source 切换。
- 站点 forcing 曲线显示。
- 河段 `q_down` 曲线显示。
- `/ops` 能选择本轮 source/cycle。
- `/ops` 能展示 stages、jobs、失败原因和日志。
- viewer/operator/sys_admin 权限状态符合预期。

通过条件：

- 截图、DOM snapshot、console/network 错误留证。
- 页面不允许靠 mock API 通过。
- 如果 latest-product 不可用，浏览器 E2E 只能记为 BLOCKED/PARTIAL。

## 8. 跨面联调 E2E

跨面联调只允许使用同一个生产链路 run：

```text
source: GFS 或 IFS
cycle_time: <22 本轮生产 cycle>
model_id: basins_qhh_shud
basin_id: basins_qhh
run_id: <22 pipeline run_id>
```

检查链路：

```text
22 plan-production receipt
  -> Slurm/Gateway job receipt
  -> compute node stdout/stderr
  -> DB pipeline_jobs/stages
  -> met forcing station series
  -> hydro q_down river_timeseries
  -> latest-product API
  -> /hydro-met browser
  -> /ops browser logs
```

通过条件：

- 22 和 27 的证据指向同一个 `run_id/cycle/source/model_id`。
- 27 不使用本地旧数据冒充 22 本轮产物。
- 27 不直接提交 Slurm job。
- 失败项可以追溯到计算控制面、数据产品面、API 合同面或前端展示面。

## 9. Retry / Cancel 测试

Retry/cancel 属于计算控制面能力，建议由 22 触发并由 27 展示。

测试方式：

1. 22 制造一个受控失败 job。
2. 22 或授权运维入口触发 retry/cancel。
3. 计算侧产生真实 Gateway/Slurm receipt。
4. 27 `/ops` 展示失败、日志、retry 后新 job、最终状态。

通过条件：

- viewer 不能 retry/cancel。
- operator/sys_admin 权限按策略允许。
- retry 产生新 job id 或 Gateway receipt。
- cancel 不得对不存在或已终态 job 误报成功。
- `/ops` 中能看到前后状态变化。

## 10. 验收闸门

### 10.1 计算控制面 PASS

必须满足：

- 22 依赖重建并通过后端基础检查。
- Gateway/Slurm 最小 probe 通过。
- 计算节点能读写共享计算路径。
- QHH GFS/IFS 至少一个 source 完成正式 production chain。
- DB、对象存储、日志均有本轮 run 证据。

### 10.2 展示服务面 PASS

必须满足：

- 27 依赖重建并通过后端/前端基础检查。
- API `/health`、models、stations、station-series、forecast-series 可用。
- latest-product 至少一个 source 返回 200。
- `/hydro-met` 和 `/ops` 无 mock 浏览器 E2E 通过。

### 10.3 跨面 PASS

必须满足：

- 22 产出的同一 run 能被 27 API 和前端完整消费。
- `/ops` 能显示本轮真实 jobs/stages/logs。
- 证据中没有把 deterministic、mocked 或历史旧 cycle 升级成 live receipt。

## 11. 当前已知准备缺口

截至 2026-05-27，已知需要补齐：

- 22 节点正式计算控制面环境、Gateway URL、Slurm receipt 和计算节点路径 receipt。
- 27 节点已完成本地依赖重建和基础检查，但缺 live `DATABASE_URL`、对象存储/发布区配置。
- OpenSpec CLI 在 27 当前 PATH 中缺失，需要在正式 evidence lane 中恢复或记录替代验证方式。
- 既有 `docs/bugs.md` 中的 latest-product、segment universe、ops cycle identity、logs、retry receipt 等问题仍需在两段式 E2E 中逐项复测归因。

