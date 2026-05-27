# 两节点 Docker 化只读展示部署与重构方案

最后更新：2026-05-27  
适用范围：QHH/有限流域水文气象展示 + 运维监控 MVP  
关联方案：

- `docs/runbooks/two-node-production-e2e-plan.md`
- `docs/plans/2026-05-27-two-node-readonly-display-refactor-plan.md`

执行目标：在两台机器上使用 Docker/Compose 部署，但不形成“两节点各跑一套完整系统”。系统必须按角色拆分：

- **22 = compute_control**：生产、调度、Slurm、产物发布、人工恢复。
- **27 = display_readonly**：展示 API、前端静态资源、只读 DB、只读 published artifacts。

## 1. 结论

两节点用 Docker 是可行的，但前提是不要把单机开发环境原样复制到 22 和 27。正确方式是：

```text
一套应用镜像，多角色启动；
22 具备计算控制能力；
27 物理上拿不到计算控制能力；
DB 和 published artifacts 是唯一跨节点共享面；
异常处理由 27 展示，运维人员去 22 处理。
```

本方案采用 **Docker Compose + systemd**，暂不引入 Kubernetes。原因：

- 当前只有两台机器，服务数量有限。
- Slurm/Munge/共享路径和宿主机绑定较强，过早 K8s 会增加复杂度。
- Compose 更容易显式表达 22/27 的能力差异和挂载差异。
- systemd 可负责开机启动、重启策略和日志归档。

## 2. 目标拓扑

```text
22 compute/control node
  ├─ nhms-compute-api 或 compute-control container
  ├─ nhms-scheduler container
  ├─ slurm-gateway host service 或 container
  ├─ published artifacts writer
  ├─ Slurm CLI / munge / shared paths
  └─ SHUD runtime / sbatch templates / Basins assets

shared layer
  ├─ PostgreSQL/PostGIS/TimescaleDB
  │   ├─ 22: rw
  │   └─ 27: ro
  ├─ published artifacts
  │   ├─ 22: rw
  │   └─ 27: ro
  └─ object store or NFS publish root

27 display node
  ├─ nhms-display-api container
  ├─ frontend dist, served by FastAPI or nginx/caddy
  ├─ reverse proxy / TLS
  ├─ readonly DB credentials
  └─ readonly published artifacts credentials/mount
```

## 3. 和前两份 plan 的关系

### 3.1 继承 two-node E2E plan

`two-node-production-e2e-plan.md` 已经规定：

- 22 是计算控制面。
- 27 是展示服务面。
- E2E 分 compute_control_plane、display_service_plane、cross_plane_e2e 三段验收。
- 跨面 E2E 必须用同一个 `run_id/source/cycle/model_id` 贯穿 22 生产和 27 展示。

本方案补充“如何用 Docker/Compose 部署这些角色”。

### 3.2 继承 readonly display refactor plan

`two-node-readonly-display-refactor-plan` 已经规定：

- 27 不直接控制 Slurm。
- 27 不运行 scheduler。
- 27 不读取 22 私有工作目录。
- 27 不用 mock gateway 冒充生产动作。
- 27 不写 hydro/met/pipeline 终态。
- `/ops` 只展示异常、复制诊断信息和人工处理建议。

本方案把这些边界落到 Docker 权限、挂载、环境变量、Compose 文件和 systemd 启动策略上。

## 4. 总体部署原则

### 4.1 一套镜像，多角色启动

建议维护一套主应用镜像：

```text
nhms-app:<git-sha>
```

不同角色通过环境变量区分：

```text
NHMS_SERVICE_ROLE=compute_control
NHMS_SERVICE_ROLE=display_readonly
NHMS_SERVICE_ROLE=slurm_gateway
NHMS_SERVICE_ROLE=dev_monolith
```

不要维护两套几乎一样的镜像，例如：

```text
nhms-compute-image
nhms-display-image
```

否则后续排查会遇到：

- 22 和 27 commit 不一致。
- OpenAPI/type 生成版本不一致。
- 依赖版本漂移。
- 同一 bug 只在某一个镜像复现。

### 4.2 27 必须物理上拿不到控制能力

27 的容器不能只是“逻辑上不调用 Slurm”，而是要在物理部署上也拿不到控制能力：

- 不安装 Slurm CLI。
- 不挂载 `/etc/slurm`。
- 不挂载 munge socket/key。
- 不配置 `SLURM_GATEWAY_URL`。
- 不挂载 22 的 `WORKSPACE_ROOT`。
- 不挂载 22 的 `.nhms-runs`。
- 不挂载 Docker socket。
- DB 使用只读账号。
- published artifacts 使用只读挂载或只读对象存储凭据。

目标是：即使 27 上代码有 bug，也无法提交 Slurm job、取消 Slurm job、修改业务终态或读取 22 私有 workspace。

### 4.3 Slurm Gateway 容器化分阶段

Slurm Gateway 是最大风险点。真实容器化需要：

- 容器内有 Slurm client：`sbatch`、`squeue`、`sacct`、`scancel`。
- 能访问 `/etc/slurm` 或等价配置。
- 能访问 munge socket/key。
- 容器网络能访问 `slurmctld`。
- 容器 UID/GID 与集群账号一致或符合站点策略。
- shared log/workspace 路径与 compute node 一致。

推荐阶段策略：

| 阶段 | Gateway 形态 | 推荐程度 | 说明 |
| --- | --- | --- | --- |
| 阶段 1 | 22 host 上 systemd 服务 | 最稳 | 避免 Slurm/Munge 容器化问题 |
| 阶段 2 | 22 上 gateway 容器 | 可选 | 只在 22，绑定必要 Slurm/Munge 文件 |
| 阶段 3 | 独立 gateway 节点 | 后续 | 需要更严格网络和权限隔离 |

当前 MVP 推荐先用阶段 1：**Slurm Gateway 先作为 22 host 服务跑，其他服务 Docker 化。**

### 4.4 shared DB / artifact 只能有一份

22 和 27 必须消费同一份生产状态。禁止：

- 27 用本地旧 DB 跑通 E2E。
- 27 用本地旧 artifact 跑通页面。
- 27 拷贝 22 workspace 后展示旧数据。
- latest-product 用历史旧周期冒充本轮 22 产物。

跨面 E2E 必须锁定：

```text
run_id
source_id
cycle_time
model_id
basin_id
forcing_version_id
```

## 5. 目标文件结构

建议新增：

```text
infra/
  docker/
    Dockerfile.app
    Dockerfile.frontend-runtime        # 可选，如使用 nginx/caddy 单独服务静态资源
    entrypoint.sh
  compose.compute.yml
  compose.display.yml
  env/
    compute.example
    display.example
    shared.example
  systemd/
    nhms-compute-compose.service
    nhms-display-compose.service
    nhms-slurm-gateway.service         # 阶段 1 推荐
  README.two-node-docker.md
```

已有 `infra/docker-compose.dev.yml` 保留为开发用，不要改造成生产两节点 Compose。当前 dev compose 只包含开发 DB、MinIO 和一个 `m1-worker`，并使用开发密码、开发端口和仓库目录挂载；它不应被当成生产部署文件。

## 6. 镜像设计

### 6.1 Dockerfile.app

目标：同一镜像可运行 API、scheduler、compute-control 工具和 display API。

建议能力：

- Python 3.11+。
- `uv`。
- 项目源码。
- 后端依赖。
- 前端 dist 可选内置。
- 不默认包含 Slurm client。
- 不默认包含 munge。

建议分层：

```dockerfile
# builder
FROM python:3.11-slim AS builder
# install uv, deps, build frontend if needed

# runtime
FROM python:3.11-slim AS runtime
# copy .venv / app / frontend dist
# create non-root user
# entrypoint checks NHMS_SERVICE_ROLE
```

### 6.2 Slurm client 不放入默认 app 镜像

理由：

- 27 不应该物理具备 Slurm 控制能力。
- 默认镜像如果含 Slurm CLI，误部署风险高。
- Slurm client 通常与集群版本、munge、站点配置相关。

可选做法：

```text
Dockerfile.slurm-gateway
```

只用于 22，构建 tag：

```text
nhms-slurm-gateway:<git-sha>
```

但第一阶段建议不用 Docker 化 gateway，先用 host systemd。

### 6.3 前端静态资源服务方式

两种可选：

| 方式 | 优点 | 缺点 | 推荐 |
| --- | --- | --- | --- |
| FastAPI 服务 `apps/frontend/dist` | 简单，一容器 | 静态资源性能一般 | MVP 推荐 |
| nginx/caddy 服务 dist | 性能好，TLS/proxy 清晰 | 多一个容器 | 生产推荐 |

MVP 初期可先用 FastAPI 服务前端，后续再改 nginx/caddy。

## 7. Compose 设计

## 7.1 `infra/compose.compute.yml`

运行位置：22 节点。

推荐服务：

```yaml
services:
  compute-api:
    image: nhms-app:${NHMS_IMAGE_TAG}
    env_file:
      - ./env/compute.env
    environment:
      NHMS_SERVICE_ROLE: compute_control
    volumes:
      - ${NHMS_BASINS_ROOT}:${NHMS_BASINS_ROOT}:ro
      - ${WORKSPACE_ROOT}:${WORKSPACE_ROOT}:rw
      - ${PUBLISHED_ARTIFACT_ROOT}:${PUBLISHED_ARTIFACT_ROOT}:rw
    network_mode: bridge
    restart: unless-stopped

  scheduler:
    image: nhms-app:${NHMS_IMAGE_TAG}
    env_file:
      - ./env/compute.env
    environment:
      NHMS_SERVICE_ROLE: compute_control
    volumes:
      - ${NHMS_BASINS_ROOT}:${NHMS_BASINS_ROOT}:ro
      - ${WORKSPACE_ROOT}:${WORKSPACE_ROOT}:rw
      - ${PUBLISHED_ARTIFACT_ROOT}:${PUBLISHED_ARTIFACT_ROOT}:rw
    command: ["uv", "run", "nhms-pipeline", "scheduler-loop"]
    restart: unless-stopped
```

注意：

- 如果当前还没有 `scheduler-loop`，先用 systemd timer 或 cron 手工执行 `plan-production`。
- scheduler 容器必须能访问 DB、对象存储、published root。
- 计算任务是否在容器中执行由 Slurm sbatch 模板决定；不要让 compute node 依赖 27。

### 可选：22 上 Slurm Gateway 容器

仅在确认 Slurm/Munge 容器化可行后启用：

```yaml
  slurm-gateway:
    image: nhms-slurm-gateway:${NHMS_IMAGE_TAG}
    env_file:
      - ./env/compute.env
    environment:
      NHMS_SERVICE_ROLE: slurm_gateway
      SLURM_GATEWAY_BACKEND: slurm
    volumes:
      - /etc/slurm:/etc/slurm:ro
      - /run/munge:/run/munge:ro
      - ${WORKSPACE_ROOT}:${WORKSPACE_ROOT}:rw
      - ${PUBLISHED_ARTIFACT_ROOT}:${PUBLISHED_ARTIFACT_ROOT}:rw
    ports:
      - "127.0.0.1:8081:8000"
    restart: unless-stopped
```

安全要求：

- 只绑定 22 localhost 或内网控制网段。
- 不暴露给 27。
- 不使用 `privileged: true`，除非站点明确批准。
- 不挂 Docker socket。

## 7.2 `infra/compose.display.yml`

运行位置：27 节点。

推荐服务：

```yaml
services:
  display-api:
    image: nhms-app:${NHMS_IMAGE_TAG}
    env_file:
      - ./env/display.env
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
    volumes:
      - ${PUBLISHED_ARTIFACT_ROOT}:${PUBLISHED_ARTIFACT_ROOT}:ro
    ports:
      - "127.0.0.1:8000:8000"
    restart: unless-stopped

  reverse-proxy:
    image: caddy:2
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - display-api
    restart: unless-stopped

volumes:
  caddy-data:
  caddy-config:
```

27 禁止挂载：

```text
/etc/slurm
/run/munge
WORKSPACE_ROOT
NHMS_BASINS_ROOT
/var/run/docker.sock
22 .nhms-runs
22 private /scratch
```

## 8. 环境变量设计

## 8.1 `infra/env/compute.example`

```bash
NHMS_IMAGE_TAG=<git-sha>
NHMS_SERVICE_ROLE=compute_control

DATABASE_URL=postgresql://nhms_control_rw:<password>@<db-host>:5432/nhms

WORKSPACE_ROOT=/scratch/<user>/nhms-production/workspace
OBJECT_STORE_ROOT=/scratch/<user>/nhms-production/object-store
OBJECT_STORE_PREFIX=s3://nhms-prod
PUBLISHED_ARTIFACT_ROOT=/scratch/<user>/nhms-production/published
PUBLISHED_ARTIFACT_URI_PREFIX=published://

NHMS_BASINS_ROOT=/volume/data/nwm/Basins
SHUD_EXECUTABLE=/path/to/SHUD/shud

SLURM_GATEWAY_BACKEND=slurm
SLURM_GATEWAY_URL=http://127.0.0.1:8081
SLURM_GATEWAY_TEMPLATE_DIR=infra/sbatch
SLURM_GATEWAY_WORKSPACE_DIR=/scratch/<user>/nhms-production/workspace

GFS_NOMADS_BASE_URL=...
IFS_OPEN_DATA_SOURCE=ecmwf
```

## 8.2 `infra/env/display.example`

```bash
NHMS_IMAGE_TAG=<git-sha>
NHMS_SERVICE_ROLE=display_readonly
NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false

DATABASE_URL=postgresql://nhms_display_ro:<password>@<db-host>:5432/nhms

PUBLISHED_ARTIFACT_ROOT=/mnt/nhms-published
PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_ARTIFACT_BACKEND=local
NHMS_LOG_TAIL_MAX_BYTES=1048576

OBJECT_STORE_PREFIX=s3://nhms-prod
S3_ENDPOINT_URL=<optional>
S3_BUCKET_NAME=<optional>
AWS_ACCESS_KEY_ID=<readonly-key-if-needed>
AWS_SECRET_ACCESS_KEY=<readonly-secret-if-needed>

AUTH_BACKEND=...
CORS_ALLOWED_ORIGINS=https://<display-domain>
```

27 不允许配置：

```bash
SLURM_GATEWAY_URL=
SLURM_GATEWAY_BACKEND=slurm
NHMS_BASINS_ROOT=
WORKSPACE_ROOT=
SHUD_EXECUTABLE=
```

如果这些变量在 display 容器中出现，建议启动时 fail fast 或输出高优先级 blocker。

## 9. 关键代码改造要求

本 Docker 方案依赖前一份 readonly plan 中的代码改造。落地顺序如下。

### 9.1 Service role

必须先实现：

```text
NHMS_SERVICE_ROLE=display_readonly
NHMS_SERVICE_ROLE=compute_control
NHMS_SERVICE_ROLE=slurm_gateway
NHMS_SERVICE_ROLE=dev_monolith
```

要求：

- display_readonly 不挂载 Slurm router。
- display_readonly 不执行 retry/cancel。
- display_readonly 不调用 Gateway。
- display_readonly 可用只读 DB 完成展示。

### 9.2 Slurm router 条件挂载

`apps/api/main.py` 当前不能继续无条件 include Slurm router。必须改成：

```python
if runtime_mode.include_slurm_router:
    app.include_router(slurm_router)
```

### 9.3 retry/cancel fail-closed

display_readonly 中：

```text
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/cancel
```

必须返回人工处理提示，不能执行控制动作。

### 9.4 ArtifactReader

`/jobs/{job_id}/logs` 必须从 published artifacts 读取。

支持：

```text
published://...
s3://...
file://<allowed-publish-root>/...
```

禁止：

```text
22 WORKSPACE_ROOT
.nhms-runs
22 private /scratch
/tmp
路径穿越
带密钥的 URL
```

### 9.5 latest-product E2E identity filters

业务 latest 保留，但 E2E 必须支持：

```text
source + cycle_time + run_id + model_id
```

避免 27 用历史旧数据通过跨面 E2E。

## 10. systemd 设计

### 10.1 22 compute compose service

```ini
[Unit]
Description=NHMS compute compose
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/SHUD-NWM/infra
ExecStart=/usr/bin/docker compose -f compose.compute.yml up -d
ExecStop=/usr/bin/docker compose -f compose.compute.yml down
RemainAfterExit=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

### 10.2 27 display compose service

```ini
[Unit]
Description=NHMS display compose
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/SHUD-NWM/infra
ExecStart=/usr/bin/docker compose -f compose.display.yml up -d
ExecStop=/usr/bin/docker compose -f compose.display.yml down
RemainAfterExit=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

### 10.3 22 host Slurm Gateway service, 推荐阶段 1

```ini
[Unit]
Description=NHMS Slurm Gateway
After=network-online.target munge.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/SHUD-NWM
EnvironmentFile=/opt/SHUD-NWM/infra/env/compute.env
Environment=NHMS_SERVICE_ROLE=slurm_gateway
Environment=SLURM_GATEWAY_BACKEND=slurm
ExecStart=/opt/SHUD-NWM/.venv/bin/uvicorn services.slurm_gateway.routes:router --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

注意：上面的 `uvicorn services.slurm_gateway.routes:router` 是否可直接运行，需要按实际 ASGI app 入口调整。若当前没有独立 gateway app，应先新增 `services/slurm_gateway/app.py`。

## 11. 安全边界检查

### 11.1 27 容器检查

在 27 执行：

```bash
docker compose -f infra/compose.display.yml exec display-api sh -lc '
  which sbatch || true
  which scancel || true
  test ! -e /etc/slurm/slurm.conf
  test ! -S /run/munge/munge.socket.2
  env | sort
'
```

通过条件：

- `sbatch` 不存在。
- `scancel` 不存在。
- `/etc/slurm/slurm.conf` 不存在。
- munge socket 不存在。
- 不存在 `SLURM_GATEWAY_URL`。
- 不存在 `WORKSPACE_ROOT` 指向 22 私有路径。
- DB 用户是 readonly。

### 11.2 27 API 检查

```bash
curl -i http://127.0.0.1:8000/api/v1/slurm/health
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/retry
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/cancel
```

通过条件：

- `/api/v1/slurm/health` 返回 404 或明确禁用。
- retry/cancel 返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` 或等价错误。
- 后端日志显示没有调用 Gateway。
- DB 无新增 pipeline_job 或终态写入。

## 12. Docker 化 E2E 测试

### 12.1 22 compute E2E

在 22：

```bash
docker compose -f infra/compose.compute.yml ps
docker compose -f infra/compose.compute.yml exec scheduler uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --max-cycles-per-source 1 \
  --model-id basins_qhh_shud \
  --basin-id basins_qhh \
  --workspace-root "$WORKSPACE_ROOT"
```

生产执行：

```bash
docker compose -f infra/compose.compute.yml exec scheduler uv run nhms-pipeline plan-production \
  --plan \
  --source gfs \
  --source IFS \
  --max-cycles-per-source 1 \
  --model-id basins_qhh_shud \
  --basin-id basins_qhh \
  --workspace-root "$WORKSPACE_ROOT"
```

检查：

- 22 写 DB。
- 22 写 published artifacts。
- 22 写 pipeline jobs。
- Slurm/Gateway receipt 存在。

### 12.2 27 display E2E

在 27：

```bash
docker compose -f infra/compose.display.yml ps
curl -i http://127.0.0.1:8000/health
curl -i 'http://127.0.0.1:8000/api/v1/mvp/qhh/latest-product?source=GFS&run_id=<22-run-id>&cycle_time=<22-cycle>'
```

检查：

- latest-product 是 22 本轮 run。
- station-series 可读。
- forecast-series 可读。
- `/ops` 可读 stages/jobs/logs。
- 日志 URI 来自 published artifacts。
- retry/cancel 禁用。

### 12.3 跨面验收

必须记录：

```text
compute_control_plane: PASS | PARTIAL | FAIL | BLOCKED
display_service_plane: PASS | PARTIAL | FAIL | BLOCKED
cross_plane_e2e: PASS | PARTIAL | FAIL | BLOCKED
manual_ops_boundary: PASS | FAIL
final_production_readiness_claimed: false
```

## 13. 分阶段 PR 计划

### PR A：Docker 文档和 env skeleton

新增：

```text
infra/compose.compute.yml
infra/compose.display.yml
infra/env/compute.example
infra/env/display.example
infra/README.two-node-docker.md
```

不改应用逻辑，只给出 skeleton 和安全注释。

### PR B：Dockerfile.app 和 entrypoint

新增：

```text
infra/docker/Dockerfile.app
infra/docker/entrypoint.sh
```

目标：一套镜像支持不同 role。

### PR C：service role 代码落地

对应 readonly refactor plan 的 PR 1。

### PR D：display 模式禁用控制面 mutation

对应 readonly refactor plan 的 PR 2。

### PR E：ArtifactReader 和 published logs

对应 readonly refactor plan 的 PR 3。

### PR F：latest-product identity filters

对应 readonly refactor plan 的 PR 4。

### PR G：27 `/ops` 只读监控 UI

对应 readonly refactor plan 的 PR 5。

### PR H：systemd + two-node Docker E2E 文档

新增 systemd unit examples 和 E2E 证据模板。

## 14. 发布顺序

1. 本地保持 `dev_monolith` 通过所有现有测试。
2. 22 构建并运行 compute compose。
3. 27 构建并运行 display compose。
4. 验证 27 物理上没有 Slurm 能力。
5. 22 dry-run scheduler。
6. 22 production-like plan。
7. 27 以 run_id/cycle_time 读取本轮产物。
8. 浏览器访问 `/hydro-met` 和 `/ops`。
9. 记录 blockers。
10. 明确 `final_production_readiness_claimed=false`。

## 15. 回滚策略

- Docker skeleton 可直接停用，回到手工启动。
- display_readonly 若阻塞展示，可临时回到 `dev_monolith`，但不得暴露公网。
- Slurm Gateway 容器化若失败，回退到 22 host systemd gateway。
- ArtifactReader 若失败，允许临时只显示 `log_uri` 和人工查看提示，不要挂载 22 私有 workspace 给 27。
- latest-product identity filter 若失败，E2E 标记 BLOCKED，不允许用 historical latest 代替。

## 16. Codex 执行注意事项

每个实现 PR 都必须检查：

```text
1. 这个改动是否让 27 获得 Slurm 或 scheduler 控制能力？如果是，停止。
2. 这个改动是否让 27 读取 22 私有 workspace？如果是，停止。
3. 这个改动是否让 mock gateway 在生产 display mode 可用？如果是，停止。
4. 这个改动是否让 27 写 hydro/met/pipeline 终态？如果是，停止。
5. 这个改动是否允许历史 latest 冒充本轮 two-node E2E？如果是，停止。
```

每个 PR 描述必须填写：

```text
Service role affected:
  - dev_monolith:
  - compute_control:
  - display_readonly:
  - slurm_gateway:

Docker surface:
  - image changes:
  - compose changes:
  - mount changes:
  - exposed ports:

Control capability on 27:
  - Slurm CLI present: yes/no
  - slurm_router registered: yes/no
  - retry/cancel executable: yes/no
  - DB role: readonly/rw

Artifact surface:
  - published URI types:
  - log reader:
  - redaction:
  - max bytes:

E2E claim boundary:
  - deterministic:
  - mocked:
  - production-like:
  - live:
```

## 17. 最终验收清单

### 17.1 22 compute_control

- [ ] 使用 `NHMS_SERVICE_ROLE=compute_control`。
- [ ] 能访问 DB rw 账号。
- [ ] 能访问 GFS/IFS 或资料缓存。
- [ ] 能访问 Basins/model assets。
- [ ] 能运行 scheduler。
- [ ] 能访问 Slurm/Gateway。
- [ ] 能写 published artifacts。
- [ ] 能写 pipeline/job/run 状态。

### 17.2 27 display_readonly

- [ ] 使用 `NHMS_SERVICE_ROLE=display_readonly`。
- [ ] 不挂载 Slurm 配置。
- [ ] 不挂载 munge。
- [ ] 不安装 Slurm CLI。
- [ ] 不配置 `SLURM_GATEWAY_URL`。
- [ ] 不挂 22 workspace。
- [ ] 不挂 Docker socket。
- [ ] DB 使用只读账号。
- [ ] published artifacts 只读。
- [ ] `/api/v1/slurm/*` 不可用。
- [ ] retry/cancel 返回人工处理提示。
- [ ] `/hydro-met` 可展示本轮产物。
- [ ] `/ops` 可展示状态、日志和异常。

### 17.3 cross-plane

- [ ] 22 和 27 使用同一个镜像 tag / git sha。
- [ ] 22 生产的 run_id 被 27 latest-product identity filter 命中。
- [ ] station-series 来自本轮 forcing_version。
- [ ] forecast-series 来自本轮 run_id 或同 cycle/source/model identity。
- [ ] logs 来自 published artifacts。
- [ ] 27 没有使用 mock API 或 mock gateway 通过 E2E。
- [ ] E2E summary 标注 `final_production_readiness_claimed=false`。

## 18. 后续自动化运维预留

本方案不实现 `operation_request`。后续如果需要前端一键重启，可新增：

```text
ops.operation_request
ops.operation_result
compute-control request consumer
```

但必须保持：

- 27 只写请求，不执行动作。
- 22 是唯一执行者。
- 请求和结果有 audit。
- 执行结果绑定 Slurm/Gateway receipt。
- 不改变 display_readonly 的物理边界。

该能力应作为独立 Epic，不与本次 Docker 化和只读展示重构混在一起。
