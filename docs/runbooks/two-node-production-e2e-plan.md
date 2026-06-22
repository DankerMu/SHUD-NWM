# Two-Node Production-Like E2E Plan

最后更新：2026-05-27
适用范围：22 节点计算控制面 + 27 节点展示服务面
推荐证据目录：`artifacts/two-node-e2e/<run_id>/` 或显式配置的 `/scratch/frd_muziyao/<project-specific-dir>/`

> **2026-06-22 status: historical / superseded M22 evidence plan.**
> 本文保留 M22 设计时代的两节点 E2E 证据边界，不是当前生产拓扑操作手册。
> 当前生产事实是 node-22 只作为 compute/Slurm/SHUD/artifact producer，
> 不连当前活 DB；node-22 本地 PostgreSQL `:55433` 为 historical、
> do-not-connect、pending removal。node-27 承担 active PostgreSQL `:55432`、
> data-plane ingest、display API 和前端。当前值守与 oracle 路由以
> [`current-production-ops.md`](current-production-ops.md) 和
> [`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md) 为准。

Docker/systemd 操作细节见 `infra/README.two-node-docker.md`。所有直接 Docker Compose 和 systemd
install/start/restart lane 都必须先运行 `scripts/validate_two_node_docker_source_trust.py`，把 source-trust
JSON/text 报告写入 `docker-security/` evidence；compute/display 必须使用同一个 `--evidence-run-id`
分别生成 role-scoped source-trust 报告，避免互相覆盖。该脚本失败时对应 lane 只能记为 `BLOCKED`。本文只定义 E2E
证据边界；Docker 验证不能把 compose 启动、DB、API、浏览器、Slurm、日志和只读安全检查合并成一个模糊 PASS。

如果需要先理解系统怎么运转、22/27 各自负责什么、每个节点会产生哪些产物，先读 [`two-node-deployment-overview.md`](two-node-deployment-overview.md)；本文只定义验收证据边界。

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
- MVP 阶段 27 节点按 `display_readonly` 角色运行：不触发 retry/cancel，不调用 Slurm Gateway，不写 hydro/met/pipeline 终态，只提供只读监控、诊断信息复制和人工运维建议。
- retry/cancel 的执行证据只来自 22 计算控制面或授权运维入口；27 只验证这些动作产生的状态、日志和新 job receipt 是否可展示。
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
NHMS_SERVICE_ROLE=compute_control
WORKSPACE_ROOT=...
OBJECT_STORE_ROOT=...
OBJECT_STORE_PREFIX=...
NHMS_PUBLISHED_ARTIFACT_ROOT=...
NHMS_SCHEDULER_LOCK_ROOT=$WORKSPACE_ROOT/scheduler/locks
NHMS_SCHEDULER_EVIDENCE_ROOT=$WORKSPACE_ROOT/scheduler/evidence
NHMS_SCHEDULER_RUNTIME_ROOT=...
NHMS_SCHEDULER_TEMP_ROOT=...
NHMS_SCHEDULER_ALLOWED_ROOTS=$WORKSPACE_ROOT:$OBJECT_STORE_ROOT:$NHMS_SCHEDULER_RUNTIME_ROOT:$NHMS_SCHEDULER_TEMP_ROOT:$NHMS_PUBLISHED_ARTIFACT_ROOT
NHMS_SCHEDULER_SOURCES=gfs,IFS
NHMS_SCHEDULER_MODEL_IDS=basins_qhh_shud
NHMS_SCHEDULER_BASIN_IDS=basins_qhh
NHMS_SCHEDULER_INTERVAL_SECONDS=300
NHMS_SCHEDULER_MAX_PASSES=1
NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE=1
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

如果现场 Slurm 计算节点没有挂载 22 侧 `/ghdc`，这不是 27 展示面问题，也不能通过把 27 私有目录挂进
display 容器解决。应把 Slurm runtime workspace 放在计算节点可见路径，完成后由 22 侧 publish/copyback
步骤写入 `/ghdc/data/nwm/published`，再由 27 从 `/home/ghdc/nwm/published` 只读消费。

### 2.3 27 节点：展示服务面

职责：

- 运行 FastAPI。
- 服务前端静态产物或作为前端 API 后端。
- 提供 `/` 单页地图展示、`/ops` 运维展示，以及 forecast、pipeline、jobs、logs、models 等查询 API。
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
NHMS_SERVICE_ROLE=display_readonly
NHMS_REQUIRE_SERVICE_ROLE=true
NHMS_AUTH_MODE=production
NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/home/ghdc/nwm/published
NHMS_PUBLISHED_ARTIFACT_ROOT=/var/lib/nhms/published
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_PUBLISHED_ARTIFACT_S3_BUCKET=nhms-prod
NHMS_PUBLISHED_ARTIFACT_S3_PREFIX=published
OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store
NHMS_LOG_TAIL_MAX_BYTES=1048576
NHMS_ARTIFACT_BACKEND=local
OBJECT_STORE_PREFIX=s3://nhms-prod
S3_ENDPOINT_URL=https://object-store.internal.example
S3_BUCKET_NAME=nhms-prod
AWS_ACCESS_KEY_ID=readonly-key-placeholder
AWS_SECRET_ACCESS_KEY=readonly-secret-placeholder
CORS_ALLOWED_ORIGINS=https://display.internal.example
```

如果 `/ops` 需要读取文件日志，27 必须能访问日志发布位置。优先使用对象存储或受控共享发布区，而不是挂载 22 的完整工作目录。

当前部署约定使用 27 导出的 NFS 发布面：22 写 `/ghdc/data/nwm/published`，27 从本机
`/home/ghdc/nwm/published` 只读挂载到容器内 `/var/lib/nhms/published`。该目录只承载展示产品、
manifest、日志和诊断证据；不要把 22 私有 workspace 或 `.nhms-runs` 暴露给 27。

## 3. 共享依赖边界

| 依赖 | 22 节点 | 计算节点 | 27 节点 | 说明 |
| --- | --- | --- | --- | --- |
| PostgreSQL/PostGIS/TimescaleDB | 读写 | 读写或间接写 | 只读 | pipeline、met、hydro、model、ops 状态源 |
| Basins/model assets | 读 | 读 | 通常不需要 | 计算侧 source of truth |
| WORKSPACE_ROOT | 读写 | 读写 | 通常不需要 | 计算中间态，不建议作为 27 强依赖 |
| NHMS_PUBLISHED_ARTIFACT_* / readonly object-store read envs | 读写 | 读写 | 读 | 展示产品、manifest、日志发布面 |
| Slurm Gateway | 提交/查询 | 不适用 | 不调用 | 控制链路归 22 |
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
manual_ops_boundary: PASS | PARTIAL | FAIL | BLOCKED
```

## 5. 证据目录

建议每次使用统一 `run_id`：

```bash
export RUN_ID="two-node-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_ROOT="artifacts/two-node-e2e/$RUN_ID"
mkdir -p "$EVIDENCE_ROOT"/{22-compute,27-display,cross-plane,manual-ops,db,api,browser,slurm,logs,docker-preflight,docker-security,final-e2e-evidence}
```

项目创建的临时文件、Docker smoke 输出、review 输出和 E2E evidence 只能写到仓库 `artifacts/` 或 `/scratch/frd_muziyao`。如果 Docker daemon cache 无法由项目控制，必须在 `docker-preflight/` 记录当前 `evidence_run_id`、DockerRootDir、`docker system df` 和相关 `df -h`；最终聚合不接受复制到当前目录但缺少当前 run 绑定的 Docker preflight PASS。空间不足时 Docker lane 记为 `BLOCKED`。

最低交付物：

- `environment.md`：22、27、DB、对象存储、Gateway、计算节点版本和路径。
- `command_index.md`：所有命令、执行节点、退出码和日志路径。
- `22-compute/summary.md`：计算控制面结论。
- `27-display/summary.md`：展示服务面结论。
- `cross-plane/summary.md`：跨面联调结论。
- `manual-ops/summary.json`：必须使用 `nhms.two_node_e2e.manual_ops.v1`，包含当前
  `evidence_run_id`、脱敏 production operator auth 元数据、27 retry/cancel response evidence、
  27 no-side-effect proof，以及每个 declared source 的 22 receipt provenance；旧式
  `production_operator_auth_evidence: true` 或布尔断言不能作为 PASS。每个实际 22 receipt 的 provenance
  必须记录 `producer_node=22`、`producer_role=compute_control`、`receipt_id` 或 `command_id`、匹配的
  `source`/`source_id`、当前 `evidence_run_id` 和 `redacted=true`；如记录 artifact `path`/`artifact_path`
  与 `sha256`，最终聚合会校验路径在批准 evidence root 下、文件存在且 hash 匹配。
- `db/summary.md`：readonly DB role、权限矩阵、脱敏 DSN 和 blocker。
- `api/summary.md`：health、runtime config、models、stations、latest-product、pipeline/jobs/logs。
- `browser/summary.md`：无 mock 的 `/` 单页地图和 `/ops` 浏览器证据；如执行
  `/hydro-met -> /`，只能列为旧别名重定向 smoke。
- `slurm/summary.md`：22 Gateway health、minimal submit probe、Slurm receipt。
- `logs/summary.md`：published log URI、读取结果和缺失原因。
- `docker-preflight/summary.md`：当前 `evidence_run_id`、DockerRootDir、cache/space、TMPDIR 和 evidence root。
- `docker-security/summary.json`：必须由 `security-summary` helper 生成
  `nhms.two_node_docker.security_summary.v1`，并带 `source_trust`、`static`、`smoke`
  child artifact 路径与 sha256；其中 source-trust child 必须包含 compute/display 两份 role env
  ownership/mode/type 证明，static child 必须由 `static --evidence-run-id "$EVIDENCE_RUN_ID" --report "$EVIDENCE_ROOT/docker-security/static-compose-env-check.json"`
  产出最终可校验的 HostConfig/mount/env/readonly proof 字段，smoke child 必须由
  `smoke --evidence-run-id "$EVIDENCE_RUN_ID" --evidence-root "$EVIDENCE_ROOT/docker-security"` 产出。
  `security-summary` 必须在 smoke producer 之后执行，并通过重复 `--source-trust-report`
  消费 `two-node-docker-source-trust-compute.json` 和 `two-node-docker-source-trust-display.json`；手写、
  单 role source-trust、缺 source artifact 或缺当前 run 绑定的 summary 不能作为 PASS。
- `final-e2e-evidence/summary.json`：#239 最终 JSON 汇总，聚合 Docker、DB、API、browser、cross-plane、manual ops、Slurm、logs 和 compute/display lane。
- `summary.md`：最终 PASS/PARTIAL/FAIL/BLOCKED 汇总。
- `bugs.md` 或链接到 `docs/bugs.md`：失败项、根因、复测条件。

`command_index.md` 和复制到 review / incident handoff 的 evidence 只能记录脱敏命令文本；
不得包含原始 DSN、token、signature 或完整 auth header。

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
  --plan \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --model-id basins_qhh_shud \
  --basin-id basins_qhh
```

通过条件：

- 只选中 QHH 模型和 QHH basin。
- 输出包含候选 cycle、source、model_id、basin_id。
- 如 discovery 会下载探针文件，证据中明确标注。
- evidence 的 `resolved_runtime_roots` 记录 `WORKSPACE_ROOT`、`OBJECT_STORE_ROOT`、
  `NHMS_PUBLISHED_ARTIFACT_ROOT`、`NHMS_SCHEDULER_LOCK_ROOT`、`NHMS_SCHEDULER_EVIDENCE_ROOT`、
  runtime/temp root 和 `NHMS_SERVICE_ROLE=compute_control`。
- 不创建当前工作目录下的 `.nhms-workspace`。显式 `--workspace-root`、`--lock-path`、`--evidence-dir`
  只用于诊断兼容，不是业务验收命令。

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
- 如果计算节点不能访问 `/ghdc`，证据中明确记录该 filesystem boundary，并证明 publish/copyback 仍由 22
  写入 shared published artifacts，而不是让 27 读取 Slurm runtime workspace。

### 6.4 正式生产链路执行

在 22 节点执行正式入口：

```bash
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml run --rm scheduler-once
```

`scheduler-once` 的默认命令是 no-flag root 业务验证：`nhms-pipeline plan-production --plan`，只允许从
`compute.env` 解析 roots、locks、evidence、service role 和 source/model filters。它是 #253 的
PASS/BLOCKED smoke，不等同于 Task 7 live E2E。需要真实提交时必须另行使用明确授权的 submit/timer lane，
并保留同一套 root preflight 证据。

`NHMS_SCHEDULER_ALLOWED_ROOTS` 必须是非空、独立审批的 `:` 分隔 allowlist。`WORKSPACE_ROOT`、
`OBJECT_STORE_ROOT`、`NHMS_PUBLISHED_ARTIFACT_ROOT`、`NHMS_SCHEDULER_RUNTIME_ROOT` 和
`NHMS_SCHEDULER_TEMP_ROOT` 必须落在该 allowlist 内；`NHMS_SCHEDULER_LOCK_ROOT` 和
`NHMS_SCHEDULER_EVIDENCE_ROOT` 必须在 `WORKSPACE_ROOT` 内。

有界连续/定时 lane 示例：

```bash
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml run --rm scheduler-once \
  uv run nhms-pipeline plan-production \
    --continuous \
    --interval-seconds "${NHMS_SCHEDULER_INTERVAL_SECONDS:-300}" \
    --max-passes "${NHMS_SCHEDULER_MAX_PASSES:-1}" \
    --max-cycles-per-source "${NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE:-1}" \
    --source gfs \
    --model-id basins_qhh_shud \
    --basin-id basins_qhh \
    --plan
```

禁用定时 lane 时，停用对应 timer/service，并保留 `scheduler-once` 手工 proof path：

```bash
sudo systemctl disable --now nhms-compute-scheduler.timer
sudo systemctl reset-failed nhms-compute-scheduler.service
```

#### 6.4.1 scheduler-once 两种调用模式

`plan-production` 有两种调用形态，必须严格区分：no-flag root 是**业务验证**路径，`--workspace-root`
覆盖只用于**诊断/排查**。两者默认都是 `--dry-run`（`--plan` 是其同义别名），不提交 Slurm、非变更；
只有显式 `--submit` 才会真正进入生产编排提交。

| 维度 | 业务验证（no-flag root） | 诊断（`--workspace-root`） |
| --- | --- | --- |
| 触发方式 | `scheduler-once` 默认命令 / `nhms-pipeline plan-production --plan` | `nhms-pipeline plan-production --plan --workspace-root /path` |
| root 来源 | 从 `WORKSPACE_ROOT` 环境变量解析 | 由 `--workspace-root` 显式覆盖 |
| 用途 | 两节点 E2E 业务验收的 proof path | 本地/排查兼容性诊断，不作为业务证据 |
| 是否变更 | 否（dry-run 默认，不提交 Slurm） | 否（dry-run 默认，不提交 Slurm） |
| 输出 JSON | `scheduler.run_once()` 计划结果（候选 cycle、source、model_id、basin_id、resolved roots） | 同结构 JSON，但 root 反映显式覆盖值 |
| evidence 位置 | `NHMS_SCHEDULER_EVIDENCE_ROOT` / `--evidence-dir` 默认解析 | 可随 `--evidence-dir` 显式落到诊断目录 |

实现依据（`services/orchestrator/cli.py` 的 `_plan_production`）：

- 省略 `--workspace-root` 时，root 从 `WORKSPACE_ROOT` 读取；缺失则报错。这是 no-flag root 业务路径，
  并强制 `require_runtime_roots`（runtime/temp/lock/evidence root 必须就位）。
- 传 `--workspace-root /path` 时显式覆盖 root；dry-run 下放宽 runtime root 要求，仅供诊断兼容，
  不应作为 22 business proof path。

可复制命令示例：

```bash
# 业务验证：root 从 WORKSPACE_ROOT 读取，无任何 root flag
uv run nhms-pipeline plan-production --plan

# 诊断/排查：显式覆盖 root，仅供兼容性诊断
uv run nhms-pipeline plan-production --plan \
  --workspace-root /scratch/frd_muziyao/nwm-diagnostic-workspace
```

#### 6.4.2 逻辑概念 / DB 表列 / API payload 字段映射

下表把同一逻辑概念在三处的命名拉直：「DB 列名」与「API payload 字段名」是两套独立命名，避免混淆。
字段以仓库真实定义为准（`db/migrations/000006_hydro.sql`、`db/migrations/000009_ops.sql`、
Python modules `apps.api.routes.pipeline` 和 `apps.api.routes.forecast`）。

| 逻辑概念 | DB 表 / 列 | API payload 字段名 |
| --- | --- | --- |
| pipeline job | `ops.pipeline_job.job_id`（PK），`run_id`、`cycle_id`、`job_type`、`slurm_job_id`、`status`、`stage`、`exit_code`、`retry_count`、`error_code`、`error_message`、`log_uri` | `/jobs` 与 `/jobs/{job_id}/logs`：`job_id`、`run_id`、`cycle_id`、`job_type`、`slurm_job_id`、`model_id`、`status`、`stage`、`exit_code`、`retry_count`、`error_code`、`error_message`、`log_uri`、`duration_seconds` |
| pipeline event | `ops.pipeline_event`：`event_id`（PK）、`entity_type`、`entity_id`、`event_type`、`status_from`、`status_to`、`message`、`details` | 经由 pipeline status/stages 聚合暴露状态迁移；事件级字段以实现为准 |
| hydro run | `hydro.hydro_run.run_id`（PK），`source_id`、`cycle_time`、`model_id`、`status`、`slurm_job_id`、`log_uri` | `run_id`、`source`/`source_id`、`cycle_time`、`model_id`（latest-product / forecast 响应；request 端用 `source` 查询参数） |

说明：

- DB 用 `source_id`（`hydro.hydro_run.source_id`、外键到 `met.data_source`），API 请求端用 `source`
  查询参数，latest-product 响应同时回 `run_id/source_id/cycle_time/model_id`；命名不要互相套用。
- `ops.pipeline_event` 的事件粒度字段（`status_from`/`status_to` 等）属于 DB 列；API 侧主要以
  pipeline status/stages 聚合形态展示，逐字段 API 命名「以实现为准」。

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
- producer/DB forcing 覆盖 `PRCP/TEMP/RH/wind/Rn/Press`；这只证明生产侧或历史 DB 覆盖，不代表当前 display station-series route 会返回 `Press`。
- `hydro.river_timeseries` 覆盖 `q_down`。
- `ops.pipeline_job` / `ops.pipeline_event` 有完整 job/stage 状态。
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
- 27 使用 `NHMS_SERVICE_ROLE=display_readonly` 启动。
- 27 配置只读 `OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`，且 display API
  进程用户可读、可遍历该 object-store mirror。
- 27 使用只读 DB 账号，或 evidence 中明确标注只读账号尚未补齐。
- 27 的日志读取指向 published artifacts，不依赖 22 私有 workspace。

### 7.2 API 服务启动检查

启动 27 API。wrapper 会自行读取 `infra/env/display.env`；先确认该文件中的
`NHMS_SERVICE_ROLE`、`OBJECT_STORE_ROOT`、DB URL 和 published artifact root
就是本次 evidence 要验证的值，不要依赖调用前临时 export 覆盖：

```bash
DISPLAY_API_PORT="$(
  set -a
  . infra/env/display.env
  set +a
  printf '%s' "${NHMS_DISPLAY_API_PORT-8080}"
)"
export DISPLAY_API_BASE_URL="http://127.0.0.1:${DISPLAY_API_PORT}"
scripts/ops/start-display-api.sh
```

检查：

```bash
curl -i "${DISPLAY_API_BASE_URL}/health"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/models?active=true&limit=20"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/met/stations?model_id=basins_qhh_shud&limit=5"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/slurm/health"
```

通过条件：

- API 服务可启动。
- DB 连接正常。
- QHH active model 和 stations 可查。
- `/api/v1/slurm/*` 在 27 display mode 下不可用或未注册；如果返回 200，本段 FAIL。

### 7.2A Readonly DB 边界验证

在 27 节点用真实只读账号执行 readonly DB 验证。缺少真实只读 DB URL 时，本 lane 必须输出 `BLOCKED`，不能记为 `PASS`。

```bash
: "${RUN_ID:?export shared E2E RUN_ID first}"
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"
EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"
export NHMS_SERVICE_ROLE=display_readonly
export NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
export NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false

# 任选一种 secret-safe 方式加载真实 readonly DSN；env 文件必须未跟踪且权限为 0600。
# 首次创建本地 secret-source 文件；已有文件不要重新 install，否则会截断。
READONLY_SECRET_SOURCE=infra/env/display-readonly-secrets.env
if [ ! -e "$READONLY_SECRET_SOURCE" ]; then
  install -m 0600 /dev/null "$READONLY_SECRET_SOURCE"
elif [ ! -f "$READONLY_SECRET_SOURCE" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be a regular 0600 file before sourcing" >&2
  exit 1
fi
$EDITOR "$READONLY_SECRET_SOURCE"
readonly_secret_mode="$(stat -c '%a' "$READONLY_SECRET_SOURCE")" || {
  echo "BLOCKED: cannot stat $READONLY_SECRET_SOURCE before sourcing" >&2
  exit 1
}
if [ "$readonly_secret_mode" != "600" ]; then
  echo "BLOCKED: $READONLY_SECRET_SOURCE must be mode 0600 before sourcing" >&2
  exit 1
fi
set -a
. "$READONLY_SECRET_SOURCE"
set +a
# 或从站点 secret manager 读取：
# export NHMS_DISPLAY_READONLY_DATABASE_URL="$(secret-manager read nhms/display/readonly-db-url)"

for SOURCE in GFS IFS; do
  export NHMS_READONLY_DB_VALIDATION_SOURCE="$SOURCE"
  export NHMS_READONLY_DB_VALIDATION_CYCLE_TIME='<cycle_time-for-source>'
  export NHMS_READONLY_DB_VALIDATION_RUN_ID='<business-run-id-for-source>'
  export NHMS_READONLY_DB_VALIDATION_MODEL_ID=basins_qhh_shud
  export NHMS_READONLY_DB_VALIDATION_JOB_ID='<job-id-with-published-log-for-source>'

  uv run python scripts/validate_readonly_db_boundary.py \
    --evidence-root "$EVIDENCE_PARENT" \
    --run-id "$EVIDENCE_RUN_ID-db-$SOURCE" \
    --force
done

uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --merge-declared-source GFS \
  --merge-declared-source IFS \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-GFS/db/readonly-db-boundary" \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-IFS/db/readonly-db-boundary" \
  --force
```

其中 `EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"` 和 `EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"`
会把默认 `artifacts/two-node-e2e/$RUN_ID` 以及 `/scratch/frd_muziyao/.../$RUN_ID` 都写回当前 active
per-run lane：`$EVIDENCE_ROOT/db/readonly-db-boundary/`。`NHMS_READONLY_DB_VALIDATION_RUN_ID` 仍是被验证的业务
`hydro.hydro_run.run_id`。如需只通过环境变量指定 evidence bundle ID，使用
`NHMS_READONLY_DB_VALIDATION_EVIDENCE_RUN_ID="$EVIDENCE_RUN_ID"`。完整 GFS/IFS scope 必须先产生两个 per-source
lane，再用 `--merge-source-dir` 和显式 `--merge-declared-source GFS/IFS` 写入最终 `db/readonly-db-boundary/`；
单 source DB evidence 在默认 full-scope merge 中会保持 `BLOCKED`，只有同时声明 `--merge-declared-source <source>`
和 `--reduced-scope` 的单 source merge 才能作为最终 `PARTIAL` 的 DB 输入，不能手写补成 GFS/IFS。每个 per-source lane 必须保留 authoritative sibling：
`summary.json`、`role.json`、`route_smoke.json`、`permission_probes.json`；merge 会拒绝缺 sibling、
sibling 与 summary 不一致、source dir 越界或 symlink、重复/缺失 source、非 live schema、非 `PASS`、
`validation_provenance.mode != "live"`、`live_readonly_proof != true`，以及与当前 final bundle 无关的 stale
source run。合法 per-source run ID 是 `$EVIDENCE_RUN_ID-db-GFS`/`$EVIDENCE_RUN_ID-db-IFS`
或 `$EVIDENCE_RUN_ID-gfs`/`$EVIDENCE_RUN_ID-ifs`；其他命名必须在 source summary 或
`validation_provenance` 里显式记录 parent/current evidence bundle 等于 `$EVIDENCE_RUN_ID`，并记录
`parent_evidence_root`/`final_evidence_root` 指向当前 `$EVIDENCE_PARENT` 或 `$EVIDENCE_ROOT`。prefix-style
source lane 也必须实际位于当前 `$EVIDENCE_PARENT` 下；不能从另一个 approved root 复用同名 run ID。merged summary
会记录每个 source artifact 的路径、sha256、run ID 和 source provenance。`command_index.md` 只记录脱敏形式，例如：

```text
NHMS_DISPLAY_READONLY_DATABASE_URL=<redacted> uv run python scripts/validate_readonly_db_boundary.py --evidence-root "$EVIDENCE_PARENT" --run-id "$EVIDENCE_RUN_ID" --force
```

如果使用 `infra/env/display-readonly-secrets.env`，`command_index.md` 和 evidence 只记录已使用
owner-only `0600` secret source，不记录原始 DSN、文件内容或 `source` 后的环境展开值。`stat` 检查失败时，
本 lane 记为 `BLOCKED`，不得继续 sourcing 后再声明 readonly DB evidence 有效。

验证内容：

- 记录脱敏 DB URL、`current_user`、role type、route smoke、permission probe 和 retry/cancel 结果。
- `/health`、runtime config、models、stations、latest-product、pipeline status/stages、jobs、job logs
  只在真实只读 DB 请求成功时记为 `PASS`；fixture 缺失必须记为 `BLOCKED`。latest-product、pipeline
  status/stages、jobs 和 job logs 必须绑定同一组 `source`、`cycle_time`、`run_id`、`model_id`，
  job logs 还必须绑定 `job_id`；缺少强身份字段时对应 route 记为 `BLOCKED`，不能用 latest、宽泛 jobs
  或未限定 job logs 代替。source/cycle-only evidence 只能记为 `BLOCKED` 或 `PARTIAL`，不能作为 #239
  cross-plane PASS。
- `hydro.hydro_run`、`hydro.river_timeseries`、`met.forecast_cycle`、`met.forcing_station_timeseries`、`ops.pipeline_job`、`ops.pipeline_event` 和 `hydro`、`met`、`ops` schema DDL probe 的 `INSERT`、`UPDATE`、`DELETE`、DDL 必须在提交前被拒绝。
- 在执行任何 DML/DDL probe 前，必须先完成全矩阵 catalog 盘点：表级 `INSERT`、`UPDATE`、`DELETE`、
  `TRUNCATE`、`REFERENCES`、`TRIGGER`，PostgreSQL 支持时的 `MAINTAIN`，列级 `INSERT`/`UPDATE`，
  `hydro`、`met`、`ops` 中所有 sequence 的 `USAGE`/`UPDATE`，每个目标 schema 的 `CREATE`，
  当前 database 的 `CREATE`，以及当前登录可继承或可 `SET ROLE` 到的 reachable role 的危险 role
  attribute 和上述对象权限。
- 任何 catalog 可变权限、reachable writer role、DML/DDL 成功执行、current database `CREATE`，或 audited schema 内任一 sequence 可变权限都记为 `FAIL`。一旦 catalog 盘点发现任一可变能力，整个矩阵不得执行任何 DML/DDL probe，只能写入未执行的 catalog evidence；发现 sequence 可变权限时也不得执行 `nextval`、`setval`、DML 或 DDL。
- retry/cancel 的拒绝证据必须先分出无授权 / 非运维授权的 auth rejection lane；只有拿到真实 production auth
  token/header 时，授权 manual-action lane 才能继续验证 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`。如果拿不到
  真实 auth 路径，本 lane 记为 `BLOCKED`，且仍然不能构造 DB write 或 Gateway 依赖。
- #239 final manual-ops lane 的 PASS 证据必须是 `nhms.two_node_e2e.manual_ops.v1` JSON，记录
  `production_operator_auth` redacted metadata、27 retry/cancel `response_evidence`、27
  `no_side_effect_proof` 和 22 `control_receipts[].provenance`；22 receipt provenance 必须绑定 producer、
  source、当前 evidence bundle、redaction 状态，并在 artifact path/sha256 存在时可由文件 hash 复验。只写
  `production_operator_auth_evidence: true`、`write_executed: false` 这类布尔断言会被最终聚合器判为
  `BLOCKED`。
- `scripts/validate_readonly_db_boundary.py` 内部的 retry/cancel manual-action check 使用 in-process/dev-header
  方式验证后端 no-write/readonly fail-closed 行为；这只属于 readonly DB safety evidence，不能计入 #239
  production-auth manual-action `PASS`，也不能替代真实 operator token/header lane。真实 production auth
  证据必须在独立 manual-ops/API lane 记录；没有真实 auth 路径时，该 production-auth lane 保持 `BLOCKED`。
- evidence 只允许写入仓库 `artifacts/` 或 `/scratch/frd_muziyao` 下，输出中不得包含明文 DSN 密码、token 或 signature。

### 7.3 展示产品 API 检查

针对 22 本轮生产出来的 `run_id/source/cycle/model_id`，优先使用强身份约束查询：

```bash
curl -i "${DISPLAY_API_BASE_URL}/api/v1/mvp/qhh/latest-product?source=GFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/mvp/qhh/latest-product?source=IFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud"
```

`?source=GFS/IFS` 的 latest-only 查询可以作为业务回归检查，但不能单独证明跨面联调通过。

通过条件：

- 至少一个 source 的 latest-product 返回 200。
- 响应中包含 `run_id`、`source_id`、`cycle_time`、`model_id`、version/readiness 元数据。
- 返回的 `run_id/source_id/cycle_time/model_id` 必须和 22 本轮 evidence 一致。
- 如果返回 404，必须记录具体 blocker，例如 `SEGMENT_COUNT_MISMATCH` 或 `Q_DOWN_VALID_TIME_MISSING`。

### 7.4 Station Series 和 Forecast Series 检查

使用 latest-product 返回的 station、forcing_version、segment、river_network_version 检查：

```bash
curl -i '<station-series-url?variables=PRCP,TEMP,RH,wind,Rn>'
curl -i '<station-series-url?variables=Press>'
curl -i '<forecast-series-q-down-url>'
```

通过条件：

- station series 对当前 disk-backed route 支持的 `PRCP`、`TEMP`、`RH`、`wind`、`Rn` 返回非空或可解释的可用性状态，且每个返回变量的 `valid_time`、`source_id`/`cycle_time` 和 `unit` 语义正确。
- `Press` 不作为当前 display station-series route 的 PASS 必需变量；当前 object-store CSV-backed path 不 emit `Press`，请求或期待 `Press` 时应记录为 omitted/unavailable，而不是把五变量响应判为失败。
- forecast series 返回 `q_down`。
- valid time、issue time、source/scenario 和单位语义正确。

### 7.5 Ops API 检查

针对同一个本轮生产 `source/cycle_time/run_id/model_id`：

```bash
curl -i "${DISPLAY_API_BASE_URL}/api/v1/pipeline/status?source=GFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/pipeline/stages?source=GFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/jobs?source=GFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud&limit=20"
curl -i "${DISPLAY_API_BASE_URL}/api/v1/jobs/<job_id>/logs?source=GFS&cycle_time=<cycle_time>&run_id=<run_id>&model_id=basins_qhh_shud"
```

通过条件：

- status/stages/jobs 对同一个 strict identity 返回一致结果。
- jobs 表中能看到 22/Gateway 产生的真实 job id 或 Gateway receipt。
- `/logs` 能读取日志，或者明确返回日志缺失原因；job logs 还必须绑定同一个 `job_id`。
- 只带 source/cycle 的兼容性查询可以保留为回归证据，但不能单独计入 #239 cross-plane PASS。

### 7.6 浏览器 E2E

要求新增或使用无 API mock 的浏览器测试。现有含 `page.route('**/api/v1/**')` 的测试只能算 mocked regression。
生产级 display_readonly 浏览器证据使用 `cd apps/frontend && PLAYWRIGHT_LIVE_BASE_URL=<27 frontend> PLAYWRIGHT_LIVE_API_BASE_URL=<27 api> corepack pnpm run test:e2e:live-display`。
缺少 `PLAYWRIGHT_LIVE_BASE_URL` 或 `PLAYWRIGHT_LIVE_API_BASE_URL` 时该 lane 记为 `BLOCKED`，不能用默认 mocked regression lane 补为 live receipt；两个 URL 都不得通过 username/password userinfo 携带凭据。
live receipt 还必须证明浏览器页面实际从配置的 API binding 读取 `service_role` 严格等于 `display_readonly` 的有界 runtime config 和监控只读 API。监控只读 API 证据只记录 URL/status，不解析响应体。RBAC `权限不足`、runtime config 不可用、任何 `/api/v1/slurm/*` 浏览器请求、retry/cancel mutation 都不能算 PASS。

当前 route authority：`/` 是 active single-map display proof，`/ops` 是 active operational display proof。
`/hydro-met -> /` 可作为单独的旧别名重定向 smoke；`/forecast`、`/meteorology`、
`/flood-alerts`、`/basins/:id`、`/segments/:id` 只属于 legacy redirect /
compatibility context，不能替代当前 live browser proof。

必须覆盖：

- `/` 单页地图使用真实 API bootstrap latest-product，并带完整 strict identity。
- GFS/IFS source 切换。
- 站点 forcing 曲线显示。
- 河段 `q_down` 曲线显示。
- `/ops` 能选择本轮 source/cycle/run/model。
- `/ops` 能展示 stages、jobs、失败原因和日志；browser summary 必须分别记录 identity-bound `ops_jobs`
  和 `ops_job_logs` 检查，jobs 列表和日志面板都必须绑定所选 `job_id`。
- `/ops` 在 27 display mode 下不显示真实 retry/cancel 执行入口，只显示诊断信息复制和 22 人工处理建议。
- viewer/operator/sys_admin 权限状态符合只读展示预期，不得通过前端触发控制动作。

通过条件：

- 截图、DOM snapshot、console/network 错误留证，并保留 `/ops` jobs/logs 的 strict identity 与 `job_id` payload。
- 页面不允许靠 mock API 通过。
- 如果 latest-product 或 strict identity / job_id 绑定不可用，浏览器 E2E 只能记为 BLOCKED/PARTIAL。
- 如果浏览器或网络记录显示 27 调用 `/api/v1/runs/*/retry`、`/api/v1/runs/*/cancel` 或 `/api/v1/slurm/*` 执行控制动作，本段 FAIL。

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
  -> DB ops.pipeline_job / ops.pipeline_event
  -> met forcing station series
  -> hydro q_down river_timeseries
  -> latest-product API
  -> / single-map browser
  -> /ops browser logs (same strict identity + job_id)
```

通过条件：

- 22 和 27 的证据指向同一个 `run_id/cycle/source/model_id`，且 `/ops` logs 绑定同一个 `job_id`。
- 27 不使用本地旧数据冒充 22 本轮产物。
- 27 不直接提交 Slurm job。
- 27 不触发 retry/cancel，不通过 mock gateway 生成控制面 receipt。
- 27 只读取 DB 和 published artifacts；不得读取 22 私有 workspace 或 compute node local path。
- source/cycle-only evidence 只能作为兼容性检查，不能作为 cross-plane PASS。
- 失败项可以追溯到计算控制面、数据产品面、API 合同面或前端展示面。

## 9. Retry / Cancel 展示边界测试

Retry/cancel 属于计算控制面能力。MVP 阶段 27 不触发 retry/cancel，只展示 22 处理前后的状态、日志和诊断信息。

计算控制面测试方式：

1. 22 制造一个受控失败 job。
2. 22 或授权运维入口触发 retry/cancel；如果这里只能看到无授权 / 非运维授权请求，它们应先作为 auth rejection evidence 单独记录。
3. 计算侧产生真实 Gateway/Slurm receipt。
4. 22 写入 pipeline/job/stage 状态和 published log。

展示服务面测试方式：

1. 27 `/ops` 展示失败、日志、error code 和人工处理建议。
2. 27 能复制诊断信息，诊断信息包含 `run_id/source/cycle_time/job_id/slurm_job_id/log_uri/error_code`。
3. 27 的 retry/cancel API 在无 auth 或非运维 auth 时先返回 auth rejection；只有带真实 production operator auth header/token 时，才验证 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`。如果拿不到真实 auth 路径，这个 lane 记为 `BLOCKED`。
4. 22 完成 retry/cancel 后，27 通过刷新只读查询展示新 job、最终状态和日志。

通过条件：

- 27 不暴露真实 retry/cancel UI 操作；operator/sys_admin 也只能看到诊断和处理建议。
- 27 上 `POST /api/v1/runs/{run_id}/retry` 和 `POST /api/v1/runs/{run_id}/cancel` 在无授权时返回 401/403 或 `AUTH_REQUIRED` / `NOT_AUTHORIZED` 等稳定拒绝错误；授权 manual-action lane 返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` 或等价稳定错误。
- 27 直接 API 调用不会创建新 job，不更新 run，不调用 Gateway。
- 22 侧 retry 产生新 job id 或 Gateway receipt，并由 27 只读展示。
- 22 侧 cancel 不得对不存在或已终态 job 误报成功，并由 27 只读展示最终状态。

## 10. 验收闸门

### 10.0.0 当前 bundle metadata

最终聚合前必须先写入当前 bundle 的 source-of-truth metadata，文件路径为 `$EVIDENCE_ROOT/run.json`。模板如下，字段值必须来自本轮
22 生产链路和对应 job/log evidence，不能使用 latest fallback：

```json
{
  "schema": "nhms.two_node_e2e.run.v1",
  "evidence_run_id": "<EVIDENCE_RUN_ID>",
  "declared_sources": ["GFS", "IFS"],
  "reduced_scope": false,
  "strict_identities": {
    "GFS": {
      "source": "GFS",
      "cycle_time": "<gfs-cycle-time>",
      "run_id": "<gfs-business-run-id>",
      "model_id": "basins_qhh_shud",
      "job_id": "<gfs-job-id-with-published-log>"
    },
    "IFS": {
      "source": "IFS",
      "cycle_time": "<ifs-cycle-time>",
      "run_id": "<ifs-business-run-id>",
      "model_id": "basins_qhh_shud",
      "job_id": "<ifs-job-id-with-published-log>"
    }
  }
}
```

创建步骤：

```bash
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
: "${EVIDENCE_RUN_ID:=$(basename "$EVIDENCE_ROOT")}"
$EDITOR "$EVIDENCE_ROOT/run.json"
python -m json.tool "$EVIDENCE_ROOT/run.json" >/dev/null
```

单 source 演练必须把 `declared_sources` 缩为实际 source，并设置 `"reduced_scope": true`；否则 final gate 会按 full scope 检查。

### 10.0 最终 evidence 聚合

完成各 lane 后，从 checkout root 执行最终聚合器。`--evidence-root` 指向包含 `<RUN_ID>/` 的父目录，`--run-id` 是本次 evidence bundle 目录名：

```bash
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
EVIDENCE_PARENT="$(dirname "$EVIDENCE_ROOT")"
EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"

uv run python scripts/validate_two_node_e2e_evidence.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --source GFS \
  --source IFS \
  --force
```

单 source 演练必须显式保留 reduced-scope 语义，不能写成完整 GFS/IFS PASS：

```bash
uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --merge-declared-source GFS \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-GFS/db/readonly-db-boundary" \
  --reduced-scope \
  --force

uv run python scripts/validate_two_node_e2e_evidence.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --source GFS \
  --reduced-scope \
  --force
```

输出文件为 `$EVIDENCE_ROOT/final-e2e-evidence/summary.json`。该汇总只有在每个必需 lane 都有当前 evidence bundle 的 live evidence、
27 Docker/display runtime 无控制能力、readonly DB 是真实 live PASS、API/browser/logs 都匹配 22 产出的完整 strict identity，
且所有 declared source 都通过时才允许 `PASS`。缺 Docker/DB/browser/Slurm/生产 auth 或 stale evidence 时是 `BLOCKED`；
单 source 或缺 source scope 是 `PARTIAL` 或 `BLOCKED`；任何 27 Slurm/Munge/Docker/workspace 能力、writer DB/mutating grant、
mock/historical latest、wrong run/model/source/cycle/job identity 或 27 产生 retry/cancel receipt 都是 `FAIL`。

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
- `NHMS_SERVICE_ROLE=display_readonly` 下 `/api/v1/slurm/*` 不可用。
- retry/cancel 控制动作在 27 返回人工处理提示，不写 DB，不调用 Gateway。
- latest-product 至少一个 source 返回 200。
- latest-product 可用强身份约束锁定 22 本轮 `run_id/source/cycle/model_id`，`/ops` 的 status/stages/jobs/logs 也必须用同一 strict identity，`ops_jobs` 和 `ops_job_logs` 还要绑定 `job_id`。
- `/` 单页地图和 `/ops` 无 mock 浏览器 E2E 通过；如果只拿到 source/cycle-only 证据，这些 lane 只能记为 `BLOCKED` 或 `PARTIAL`。
- `/ops` 显示诊断信息和 22 人工处理建议，不显示真实控制按钮。

### 10.3 跨面 PASS

必须满足：

- 22 产出的同一 run 能被 27 API 和前端完整消费。
- `/ops` 能显示本轮真实 jobs/stages/logs，且 jobs/logs 证据绑定同一 strict identity 和 `job_id`。
- 证据中没有把 deterministic、mocked 或历史旧 cycle 升级成 live receipt。
- 证据中没有把 27 display mode 误当成控制面，也没有把 27 mock retry/cancel 当成 live Slurm receipt。

## 11. 当前已知准备缺口

截至 2026-05-27，当前状态分两类：

- #236/#237 负责的 role / compose / entrypoint / readonly-display prerequisites 已交付，并且本文已经以这些 runtime 边界作为当前约束来源。
- 最终仍由 #239 负责并且必须用真实 evidence 证明的部分是 live Docker security、readonly DB、browser、Slurm、manual ops 和 strict cross-plane evidence；在这些证据出现前，对应 lane 只能记为 `BLOCKED` / `PARTIAL`，不能声明 `PASS`。
