# Two-Node Docker Runbook

最后更新：2026-05-29  
适用范围：M22 两节点 Docker skeleton，22 `compute_control` + 27 `display_readonly`

## 1. 结论

生产两节点部署只使用 `infra/compose.compute.yml` 和 `infra/compose.display.yml`。`infra/docker-compose.dev.yml` 只用于本地开发依赖栈，不是生产两节点部署文件，不能拿它声明 22/27 Docker 验收通过。

本 runbook 给出可执行的启动、停止、状态、日志、预检和回滚命令，但不声明最终 #239 E2E、只读 DB、浏览器或 live 部署已经 `PASS`。这些结果必须由实际证据单独记录。

## 2. 拓扑

| 节点 | 角色 | 能力 | 禁止事项 |
| --- | --- | --- | --- |
| 22 | `compute_control` | writer DB、writable workspace、writable published artifacts、scheduler-once、Slurm/Gateway 访问 | 不暴露公网控制入口 |
| 27 | `display_readonly` | readonly DB、readonly published artifacts、FastAPI/frontend display、`/ops` 只读诊断 | 不挂 Slurm/Munge、workspace、Basins、Docker socket，不配置 Gateway URL，不写业务终态 |

共享面只允许是 PostgreSQL 和 published artifacts。27 不能通过挂载 22 私有 workspace、`.nhms-runs`、private `/scratch` 或 mock Gateway 来完成生产验收。

## 3. 目录和文件

生产两节点 Docker 文件：

```text
infra/compose.compute.yml
infra/compose.display.yml
infra/env/compute.example
infra/env/display.example
infra/env/README.md
infra/systemd/nhms-compute-compose.service
infra/systemd/nhms-display-compose.service
```

操作员本地文件：

```text
infra/env/compute.env
infra/env/display.env
```

`compute.env` 和 `display.env` 应从 `*.example` 复制后编辑，不能提交。项目创建的临时文件、Docker smoke 证据、review 输出和 E2E evidence 必须写入仓库 `artifacts/` 或 `/scratch/frd_muziyao`，不要写到系统盘任意目录。

## 4. Canonical Env

发布产物变量必须使用 `NHMS_` 前缀：

```bash
NHMS_PUBLISHED_ARTIFACT_ROOT=/var/lib/nhms/published
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_PUBLISHED_ARTIFACT_S3_BUCKET=nhms-prod
NHMS_PUBLISHED_ARTIFACT_S3_PREFIX=published
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/mnt/nhms-published
```

`NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` 是 compose host bind source；容器内运行时读取 `NHMS_PUBLISHED_ARTIFACT_ROOT`。不要使用无前缀的 `PUBLISHED_ARTIFACT_ROOT` 作为应用运行时变量。

22 必须显式设置：

```bash
NHMS_SERVICE_ROLE=compute_control
NHMS_REQUIRE_SERVICE_ROLE=true
DATABASE_URL=postgresql://<writer-user>:<secret>@<db-host>:5432/<db-name>
WORKSPACE_ROOT=<node-22-writable-workspace>
NHMS_BASINS_ROOT=<node-22-basins-root>
NHMS_MODEL_ASSET_ROOT=<node-22-model-assets-root>
```

27 必须显式设置：

```bash
NHMS_SERVICE_ROLE=display_readonly
NHMS_REQUIRE_SERVICE_ROLE=true
NHMS_AUTH_MODE=production
NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true
NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false
DATABASE_URL=postgresql://<readonly-user>:<secret>@<db-host>:5432/<db-name>
```

27 禁止设置或挂载：

```text
SLURM_GATEWAY_URL
SLURM_GATEWAY_BACKEND
WORKSPACE_ROOT
OBJECT_STORE_ROOT
NHMS_BASINS_ROOT
NHMS_MODEL_ASSET_ROOT
SHUD_EXECUTABLE
/etc/slurm
/run/munge
/var/run/munge
/etc/munge
/var/run/docker.sock
.nhms-runs
22 private /scratch
```

## 5. 部署顺序

1. 在 22 和 27 分别 checkout 同一 commit，并重建本机依赖。Linux 迁移时不要复用 macOS `.venv` 或 `node_modules`。
2. 构建或拉取同一个 `nhms-app:<git-sha>` 镜像，记录 image digest 和 git sha。
3. 在 22 准备 `infra/env/compute.env`，确认 writer DB、workspace、Basins/model assets、published artifact host root 都可访问。
4. 在 27 准备 `infra/env/display.env`，确认 DB 是 readonly 账号，published artifact mount/credentials 是 readonly。
5. 两边都先执行 Docker disk preflight 和 compose config。
6. 先启动 22 compute compose；如果 Slurm Gateway 走 host service，先在 22 启动并验证 Gateway health/probe。
7. 再启动 27 display compose。
8. 分别记录 compute、display、cross-plane、manual ops、DB、API、browser、Slurm、logs、Docker security 证据。

## 6. Docker Disk Preflight

任何 build、smoke 或长时间 compose 验证前先执行：

```bash
export TMPDIR="$PWD/artifacts/tmp"
mkdir -p "$TMPDIR"
uv run python scripts/validate_two_node_docker_runtime.py preflight
```

该命令记录 Docker version、compose version、DockerRootDir、`docker system df`、`df -h`、`TMPDIR` 和 evidence root。Docker 不可用或空间不足时，本 lane 记为 `BLOCKED`，不能继续并声明 `PASS`。Docker daemon 自身 cache 位置由 DockerRootDir 决定，必须在 evidence 中单独记录。

## 7. Env Files

在 22：

```bash
cp infra/env/compute.example infra/env/compute.env
$EDITOR infra/env/compute.env
```

在 27：

```bash
cp infra/env/display.example infra/env/display.env
$EDITOR infra/env/display.env
```

必须替换示例中的密码、host、路径、image tag 和域名。示例里的 `change-me`、`*.internal.example`、`m22-placeholder` 只能用于 render/config 检查，不能作为 live 部署证据。

## 8. Compose Commands

22 compute：

```bash
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml config
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml up -d
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml ps
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml logs --tail=200 compute-api
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml down
```

22 scheduler-once 手工执行：

```bash
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml run --rm scheduler-once
```

27 display：

```bash
docker compose --env-file infra/env/display.env -f infra/compose.display.yml config
docker compose --env-file infra/env/display.env -f infra/compose.display.yml up -d
docker compose --env-file infra/env/display.env -f infra/compose.display.yml ps
docker compose --env-file infra/env/display.env -f infra/compose.display.yml logs --tail=200 display-api
docker compose --env-file infra/env/display.env -f infra/compose.display.yml down
```

静态验证：

```bash
uv run python scripts/validate_two_node_docker_runtime.py static
```

## 9. Systemd Install

示例 units 使用 `/opt/SHUD-NWM/infra` 作为 `WorkingDirectory`。安装前必须把 unit 文件里的 `/opt/SHUD-NWM` 替换为实际 checkout 绝对路径，并确认 docker binary 路径是 `/usr/bin/docker`。

22：

```bash
sudo install -m 0644 infra/systemd/nhms-compute-compose.service /etc/systemd/system/nhms-compute-compose.service
sudo systemctl daemon-reload
sudo systemctl enable nhms-compute-compose.service
sudo systemctl start nhms-compute-compose.service
sudo systemctl status nhms-compute-compose.service
sudo journalctl -u nhms-compute-compose.service -n 200 --no-pager
sudo systemctl stop nhms-compute-compose.service
sudo systemctl restart nhms-compute-compose.service
```

27：

```bash
sudo install -m 0644 infra/systemd/nhms-display-compose.service /etc/systemd/system/nhms-display-compose.service
sudo systemctl daemon-reload
sudo systemctl enable nhms-display-compose.service
sudo systemctl start nhms-display-compose.service
sudo systemctl status nhms-display-compose.service
sudo journalctl -u nhms-display-compose.service -n 200 --no-pager
sudo systemctl stop nhms-display-compose.service
sudo systemctl restart nhms-display-compose.service
```

`ExecReload` 会重新执行 `docker compose up -d`，用于 env/image/compose 更新后的受控刷新。systemd 管 compose 生命周期，不替代 Docker/应用级 E2E 证据。

## 10. Slurm Gateway MVP Limitation

MVP 推荐第一阶段把 Slurm Gateway 保持为 22 host service，由 22 的系统 Python/venv、Slurm client、Munge 和站点配置直接访问真实 Slurm。当前仓库有 `services/slurm_gateway/routes.py` 的 APIRouter 和 `apps/api` 中的业务 API 装配，但没有已证明的独立 Gateway ASGI app 或生产 Gateway 容器入口。

因此本文不提供可直接 install 的 `nhms-slurm-gateway.service`，也不声称 `uvicorn services.slurm_gateway.routes:router` 或 `NHMS_SERVICE_ROLE=slurm_gateway` 能作为完整 Gateway 服务运行。`slurm_gateway` role 在当前 entrypoint 中是 reserved/fail-fast，不能启动 full business API。

22 host Gateway 的真实 systemd unit 应由后续 dedicated Gateway app 或站点现有 Gateway 服务提供；记录时至少要保留：

```text
unit name
WorkingDirectory
EnvironmentFile
ExecStart
listen address
health URL
Slurm backend mode
Munge/Slurm config source
minimal submit probe evidence
```

如果后续容器化 Gateway，只能在 22 启用，且必须单独证明 Slurm/Munge/container 边界；不能把 Gateway 容器或 Slurm/Munge 挂载加入 27 display compose。

## 11. Security Probes

27 容器安全检查：

```bash
docker compose --env-file infra/env/display.env -f infra/compose.display.yml exec display-api sh -lc '
  set -eu
  ! command -v sbatch
  ! command -v scancel
  test ! -e /etc/slurm/slurm.conf
  test ! -S /run/munge/munge.socket.2
  test ! -S /var/run/docker.sock
  env | sort
'
```

API 边界检查：

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/api/v1/runtime/config
curl -i http://127.0.0.1:8000/api/v1/slurm/health
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/retry
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/cancel
```

通过条件：

- runtime config 报告 `display_readonly`。
- `/api/v1/slurm/*` 不可用。
- retry/cancel 返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` 或等价稳定错误。
- evidence 显示 27 没有 Gateway 调用、没有业务终态写入、published artifact mount 是 readonly。

## 12. Evidence Paths

默认 evidence root：

```bash
export RUN_ID="two-node-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_ROOT="artifacts/two-node-e2e/$RUN_ID"
mkdir -p "$EVIDENCE_ROOT"/{22-compute,27-display,cross-plane,manual-ops,db,api,browser,slurm,logs,docker-security,docker-preflight}
```

允许的 project-created 输出路径只有：

```text
artifacts/
/scratch/frd_muziyao/<project-specific-dir>/
```

每个 evidence bundle 必须记录 status：`PASS`、`PARTIAL`、`FAIL` 或 `BLOCKED`。缺真实 readonly DB、缺 live browser、缺 Slurm probe 或缺 cross-plane strict identity 时，只能记 `BLOCKED` 或 `PARTIAL`，不能补写 `PASS`。

## 13. Docker Validation Matrix

| Evidence | 记录内容 | PASS 边界 |
| --- | --- | --- |
| `docker-preflight/` | DockerRootDir、cache/space、TMPDIR、evidence root | 只证明 Docker 环境可继续，不证明 E2E |
| `22-compute/` | compute compose config/up/ps/logs、scheduler-once、writer DB、published artifact write | 只证明 22 compute lane |
| `27-display/` | display compose config/up/ps/logs、runtime config、readonly mount | 只证明 27 display lane |
| `db/` | readonly DB role、`current_user`、permission probes、redacted DSN | 真实 readonly DB 缺失时 BLOCKED |
| `api/` | health、runtime config、models、stations、latest-product、ops/jobs/logs | strict identity 缺失时不得 PASS |
| `browser/` | `/hydro-met`、`/ops` screenshots、DOM/network/console | mock API 不能算 production-like PASS |
| `slurm/` | 22 Gateway health、minimal submit probe、Slurm receipt | 27 不需要也不应具备 Slurm CLI |
| `logs/` | published log URI、read result、缺失原因 | 不能读取 22 private workspace |
| `manual-ops/` | 27 fail-closed retry/cancel、22 实际处理 receipt、27 只读展示结果 | 27 不能产生控制面 receipt |
| `docker-security/` | no Slurm/Munge/Docker socket、HostConfig/mount/env 检查 | 任一 27 控制能力为 FAIL |
| `cross-plane/` | 同一 `run_id/source/cycle_time/model_id` 从 22 到 27 | historical latest/mock 数据不能 PASS |

## 14. Rollback

停止 27 display：

```bash
sudo systemctl stop nhms-display-compose.service
docker compose --env-file infra/env/display.env -f infra/compose.display.yml down
```

停止 22 compute：

```bash
sudo systemctl stop nhms-compute-compose.service
docker compose --env-file infra/env/compute.env -f infra/compose.compute.yml down
```

回滚镜像：

```bash
$EDITOR infra/env/compute.env
$EDITOR infra/env/display.env
sudo systemctl restart nhms-compute-compose.service
sudo systemctl restart nhms-display-compose.service
```

回滚原则：

- 27 display 出问题时，不要把公网 27 切回 `dev_monolith` 或 writer DB。
- Slurm Gateway 容器化失败时，回退到 22 host Gateway 服务。
- Artifact/log 读取失败时，允许显示 `log_uri` 和人工提示，不要把 22 私有 workspace 挂给 27。
- latest-product strict identity 缺失时，cross-plane 记 `BLOCKED`，不能用 historical latest 代替。
- readonly DB 权限缺失时，修正 SELECT grants；不要用 writer credential 冒充生产只读验证。

## 15. Operator Checklist

- 22/27 同一 git sha 和 image digest 已记录。
- 22 使用 `compute_control`，27 使用 `display_readonly`。
- 22 compose 用 writer DB 和 writable published artifacts。
- 27 compose 用 readonly DB 和 readonly published artifacts。
- 27 没有 Slurm/Munge/workspace/Basins/Docker socket。
- Docker preflight 和 static validation 已记录。
- systemd status/journal、compose ps/logs 已记录。
- DB、API、browser、Slurm、logs、manual ops、Docker security 证据分开保存。
- `infra/docker-compose.dev.yml` 未用于生产两节点部署。
- 最终 #239 E2E/live/readiness PASS 未在没有证据时声明。
