# Two-Node Docker Runbook

最后更新：2026-05-29
适用范围：M22 两节点 Docker skeleton，22 `compute_control` + 27 `display_readonly`

## 1. 结论

生产两节点部署只使用 `infra/compose.compute.yml` 和 `infra/compose.display.yml`。`infra/docker-compose.dev.yml` 只用于本地开发依赖栈，不是生产两节点部署文件，不能拿它声明 22/27 Docker 验收通过。

面向部署阅读的角色、流程、产物和节点职责总览见 [`docs/runbooks/two-node-deployment-overview.md`](../docs/runbooks/two-node-deployment-overview.md)。本文只保留更偏操作执行的命令、预检和回滚细节。

本 runbook 给出可执行的启动、停止、状态、日志、预检和回滚命令，但不声明最终 #239 E2E、只读 DB、浏览器或 live 部署已经 `PASS`。这些结果必须由实际证据单独记录。

## 2. 拓扑

| 节点 | 角色 | 能力 | 禁止事项 |
| --- | --- | --- | --- |
| 22 | `compute_control` | writer DB、writable workspace、writable published artifacts、scheduler-once、Slurm/Gateway 访问 | 不暴露公网控制入口 |
| 27 | `display_readonly` | readonly DB、readonly published artifacts、FastAPI/frontend display、`/ops` 只读诊断 | 不挂 Slurm/Munge、workspace、Basins、Docker socket，不配置 Gateway URL，不写业务终态 |

共享面只允许是 PostgreSQL 和 published artifacts。27 不能通过挂载 22 私有 workspace、`.nhms-runs`、private `/scratch` 或 mock Gateway 来完成生产验收。

当前两节点发布目录约定：

| 视角 | Host path | 权限语义 |
| --- | --- | --- |
| 22 `compute_control` | `/ghdc/data/nwm/published` | 22 写入展示产品、manifest、日志和诊断证据 |
| 27 `display_readonly` | `/home/ghdc/nwm/published` | 27 只读读取同一目录 |
| 容器内 | `/var/lib/nhms/published` | 两个角色统一使用的应用内路径 |

`/ghdc/data` 是 22 挂载的 27 NFS 发布面；不要把业务发布产物放到 22 私有 `/scratch` 或仓库
`artifacts/` 里作为 27 展示依赖。

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
infra/env/display-readonly-secrets.env
```

`compute.env`、`display.env` 和 readonly DB 验证用的 `display-readonly-secrets.env`
都必须以 `0600` 权限编辑，不能提交。项目创建的临时 secret material 必须放在非 evidence 目录，例如
`/scratch/frd_muziyao/nwm-secret-tmp/`；Docker smoke 证据、review 输出和 E2E evidence 必须写入仓库
`artifacts/` 或 `/scratch/frd_muziyao`，不要写到系统盘任意目录。

## 4. Canonical Env

发布产物变量必须使用 `NHMS_` 前缀：

```bash
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/ghdc/data/nwm/published   # 22 compute
# NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/home/ghdc/nwm/published # 27 display
NHMS_PUBLISHED_ARTIFACT_ROOT=/var/lib/nhms/published
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_PUBLISHED_ARTIFACT_S3_BUCKET=nhms-prod
NHMS_PUBLISHED_ARTIFACT_S3_PREFIX=published
```

`NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` 是 compose host bind source；容器内运行时读取 `NHMS_PUBLISHED_ARTIFACT_ROOT`。不要使用无前缀的 `PUBLISHED_ARTIFACT_ROOT` 作为应用运行时变量。

22 必须显式设置：

```bash
NHMS_SERVICE_ROLE=compute_control
NHMS_REQUIRE_SERVICE_ROLE=true
DATABASE_URL=postgresql://<writer-user>:<secret>@<db-host>:5432/<db-name>
WORKSPACE_ROOT=<node-22-writable-workspace>
OBJECT_STORE_ROOT=<node-22-compute-visible-object-store>
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/ghdc/data/nwm/published
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
NHMS_PUBLISHED_ARTIFACT_HOST_ROOT=/home/ghdc/nwm/published
NHMS_PUBLISHED_ARTIFACT_ROOT=/var/lib/nhms/published
NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
NHMS_PUBLISHED_ARTIFACT_S3_BUCKET=nhms-prod
NHMS_PUBLISHED_ARTIFACT_S3_PREFIX=published
NHMS_LOG_TAIL_MAX_BYTES=1048576
NHMS_ARTIFACT_BACKEND=local
OBJECT_STORE_PREFIX=s3://nhms-prod
S3_ENDPOINT_URL=https://object-store.internal.example
S3_BUCKET_NAME=nhms-prod
AWS_ACCESS_KEY_ID=<readonly-key-placeholder>
AWS_SECRET_ACCESS_KEY=<readonly-secret-placeholder>
CORS_ALLOWED_ORIGINS=https://display.internal.example
```

27 禁止设置或挂载，且要与 `infra/docker/entrypoint.sh` 和
`scripts/validate_two_node_docker_runtime.py` 的 display forbidden set 保持一致：

```text
SLURM_GATEWAY_URL
SLURM_GATEWAY_BACKEND
WORKSPACE_ROOT
RUN_WORKSPACE_ROOT
SHARED_LOG_ROOT
OBJECT_STORE_ROOT
NHMS_BASINS_ROOT
NHMS_MODEL_ASSET_ROOT
SLURM_GATEWAY_TEMPLATE_DIR
SLURM_GATEWAY_WORKSPACE_DIR
MUNGE_SOCKET
MUNGE_KEY
SHUD_EXECUTABLE
DOCKER_HOST
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
5. 两边都先执行 Docker disk preflight、source-trust preflight 和 compose config。
6. 先启动 22 compute compose；如果 Slurm Gateway 走 host service，先在 22 启动并验证 Gateway health/probe。
7. 再启动 27 display compose。
8. 分别记录 compute、display、cross-plane、manual ops、DB、API、browser、Slurm、logs、Docker security 证据。

## 6. Docker Disk Preflight

任何 build、smoke 或长时间 compose 验证前先执行：

```bash
export RUN_ID="two-node-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_ROOT="artifacts/two-node-e2e/$RUN_ID"
mkdir -p "$EVIDENCE_ROOT/docker-preflight"
export TMPDIR="$PWD/artifacts/tmp"
mkdir -p "$TMPDIR"
uv run python scripts/validate_two_node_docker_runtime.py preflight \
  --evidence-run-id "$RUN_ID" \
  --evidence-root "$EVIDENCE_ROOT/docker-preflight"
```

该命令记录当前 `evidence_run_id`、Docker version、compose version、DockerRootDir、`docker system df`、
`df -h`、`TMPDIR` 和 evidence root。最终 E2E 聚合要求 Docker preflight `PASS` payload 显式绑定当前 run；
复制旧的无 ID preflight JSON 不能作为当前 run 的 PASS。Docker 不可用或空间不足时，本 lane 记为
`BLOCKED`，不能继续并声明 `PASS`。Docker daemon 自身 cache 位置由 DockerRootDir 决定，必须在
evidence 中单独记录。

## 7. Env Files

下面命令假设从实际 checkout root 执行，并把 source-trust evidence 写入本次 `docker-security/` lane。
`--trusted-owner` 必须列出本站点允许写 checkout/env 且可访问 Docker 的 root-equivalent 用户；示例值需要按站点调整。
`--trust-root` 是从哪个路径组件开始检查到 `$CHECKOUT_ROOT/infra`，例如 `/opt` 或部署 checkout 的父目录。

在 22：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
if [ ! -e "$CHECKOUT_ROOT/infra/env/compute.env" ]; then
  install -m 0600 "$CHECKOUT_ROOT/infra/env/compute.example" "$CHECKOUT_ROOT/infra/env/compute.env"
elif [ ! -f "$CHECKOUT_ROOT/infra/env/compute.env" ]; then
  echo "BLOCKED: $CHECKOUT_ROOT/infra/env/compute.env must be a regular 0600 file" >&2
  exit 1
fi
$EDITOR "$CHECKOUT_ROOT/infra/env/compute.env"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
```

在 27：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
if [ ! -e "$CHECKOUT_ROOT/infra/env/display.env" ]; then
  install -m 0600 "$CHECKOUT_ROOT/infra/env/display.example" "$CHECKOUT_ROOT/infra/env/display.env"
elif [ ! -f "$CHECKOUT_ROOT/infra/env/display.env" ]; then
  echo "BLOCKED: $CHECKOUT_ROOT/infra/env/display.env must be a regular 0600 file" >&2
  exit 1
fi
$EDITOR "$CHECKOUT_ROOT/infra/env/display.env"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
```

只读 DB 验证如果使用本地 secret-source 文件：

```bash
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
```

必须替换示例中的密码、host、路径、image tag 和域名。`compute.env`、`display.env` 和
`display-readonly-secrets.env` 都包含生产 secret-bearing 值，必须保持 owner-only `0600`；任何 source
前都要用上面的 `BLOCKED` 守卫失败即退出。`compute.env` / `display.env` 还必须通过
`scripts/validate_two_node_docker_source_trust.py --role ...`，该脚本会检查 env mode 精确为 `0600`、
owner allowlist、symlink 和 group/world-writable source。默认 umask 不可信时，先执行 `umask 077` 或重新用
`install -m 0600` 生成。示例里的 `change-me`、
`*.internal.example`、`m22-placeholder` 只能用于 render/config 检查，不能作为 live 部署证据。

## 8. Compose Commands

22 compute：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" config --quiet
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" up -d
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" ps
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" logs --tail=200 compute-api
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" down
```

Linux Docker 上如果 `DATABASE_URL` 指向 22 host 上的 DB/Gateway 服务，可以使用
`host.docker.internal`；`infra/compose.compute.yml` 已把它映射到 `host-gateway`。如果本地 E2E 的
PostgreSQL 本身也是 Docker 容器，优先把该 DB 容器加入 `nhms-compute_default` 网络，并在未提交的
`infra/env/compute.env` 中使用 DB 容器名和容器端口，例如
`postgresql://<user>:<redacted>@nhms-22-e2e-db:5432/nhms`。不要为了本机测试把明文 DSN 写入
checked-in example，也不要把测试 DB 产物放进仓库。

22 scheduler-once 手工执行：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" run --rm scheduler-once
```

27 display：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" config --quiet
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" up -d
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" ps
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" logs --tail=200 display-api
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" down
```

每次直接执行上面的 `docker compose ... config/up/run/ps/logs/down` 证据 lane 前，都必须重新执行同一条
source-trust preflight；失败时本 lane 记为 `BLOCKED`，不得让 compose 读取未审计 source。如果需要完整渲染结果，
只能临时查看 `docker compose ... config`，因为它会展开包含 `DATABASE_URL` 和 AWS 凭据的 secret-bearing 值；
原始输出不得直接存成 evidence，必须先脱敏。

静态验证：

```bash
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
uv run python scripts/validate_two_node_docker_runtime.py static \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --report "$EVIDENCE_ROOT/docker-security/static-compose-env-check.json"
```

## 9. Systemd Install

示例 units 使用 `/opt/SHUD-NWM/infra` 作为 `WorkingDirectory`，且未设置 `User=`，因此默认以 systemd
system service 的 root 权限执行 Docker Compose。Docker 访问本身是 root-equivalent；只有 root 或站点指定的可信
Docker 部署用户可以写 checkout、compose、env 和 unit 源文件。安装前必须把 unit 文件里的 `/opt/SHUD-NWM`
替换为实际 checkout 绝对路径，并确认 docker binary 路径是 `/usr/bin/docker`。systemd 启动、reload、restart
都会重新读取可变的 compose/env 内容；在 `systemctl enable/start/restart` 前必须先记录可信 checkout preflight。

如果站点要增加 `User=nhms-deploy` / `Group=docker`，该用户和 Docker group 必须按 root-equivalent 管理，且
checkout/env 只能由该可信用户或 root 写入。不要从 untrusted user 可写、group/world-writable 的 checkout path
运行 systemd-managed Docker Compose。

source-trust preflight 是 authoritative gate：`scripts/validate_two_node_docker_source_trust.py` 在 22/27 各自节点执行，
并把 JSON/text 报告保存到本次 `docker-security/` evidence。脚本会 fail closed 检查：

- `--trust-root` 到 `$CHECKOUT_ROOT/infra` 的每个路径组件不能是 symlink，owner 必须在 allowlist 内，且不能
  group/world-writable。
- `$CHECKOUT_ROOT`、`infra/`、两份 compose 文件、`infra/env`、`infra/systemd`、两份 unit source 文件必须存在、
  不能是 symlink，owner 必须在 allowlist 内，且不能 group/world-writable。
- 请求的 role env file 必须通过同样 source 检查，且 mode 精确为 `0600`。

如果该脚本输出 `BLOCKED:` 或非零退出，停止直接 Docker Compose、systemd 安装、enable/start/reload/restart 和
rollback/restart；本 lane 只能记为 `BLOCKED`。不得让 group/world 读取生产 DB URL、object-store credential 或 auth
配置，也不得让 untrusted user 在 validation 之后修改 compose/env 等待 root service 重启执行。

22：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT=/opt/SHUD-NWM
TRUST_ROOT="${TRUST_ROOT:-/opt}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service" /etc/systemd/system/nhms-compute-compose.service
sudo systemctl daemon-reload
sudo systemctl enable nhms-compute-compose.service
sudo systemctl start nhms-compute-compose.service
sudo systemctl status nhms-compute-compose.service
sudo journalctl -u nhms-compute-compose.service -n 200 --no-pager
sudo systemctl stop nhms-compute-compose.service
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
sudo systemctl restart nhms-compute-compose.service
```

27：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT=/opt/SHUD-NWM
TRUST_ROOT="${TRUST_ROOT:-/opt}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service" /etc/systemd/system/nhms-display-compose.service
sudo systemctl daemon-reload
sudo systemctl enable nhms-display-compose.service
sudo systemctl start nhms-display-compose.service
sudo systemctl status nhms-display-compose.service
sudo journalctl -u nhms-display-compose.service -n 200 --no-pager
sudo systemctl stop nhms-display-compose.service
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
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

当前 22 live probe 暴露的站点边界是：Slurm 计算节点可能没有 `/ghdc` 挂载。此时 `/ghdc/data/nwm/published`
仍然是 22 与 27 的展示发布共享面，但不应作为 sbatch runtime workspace。sbatch 应使用计算节点可见的
workspace/object-store 路径；完成后由 22 publish/copyback 把完整 `runs/<run_id>/...` 同步到
`/ghdc/data/nwm/object-store`，把展示瓦片、manifest 和日志写到 `/ghdc/data/nwm/published`。27 分别从
`/home/ghdc/nwm/object-store` 和 `/home/ghdc/nwm/published` 只读读取。

## 11. Security Probes

27 容器安全检查必须以 `scripts/validate_two_node_docker_runtime.py static` 和 Docker smoke/image absence evidence 作为 `docker-security/` 的权威边界。下面的容器内探针只是补充性快速检查，但覆盖同一组代表性 Slurm/Munge/Docker socket binary/path：

在最终 E2E 聚合前，先用 checked-in helpers 生成本 run 的 source-trust、static 和 smoke producer evidence，
再把它们规范化成 `$EVIDENCE_ROOT/docker-security/summary.json`；不要手写该 JSON。compute/display
source-trust 必须分别写入 role-scoped 报告，`security-summary` 通过重复 `--source-trust-report` 同时消费两份报告：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
EVIDENCE_RUN_ID="$(basename "$EVIDENCE_ROOT")"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$EVIDENCE_RUN_ID" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$EVIDENCE_RUN_ID" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
uv run python scripts/validate_two_node_docker_runtime.py static \
  --evidence-run-id "$EVIDENCE_RUN_ID" \
  --report "$EVIDENCE_ROOT/docker-security/static-compose-env-check.json"
uv run python scripts/validate_two_node_docker_runtime.py smoke \
  --evidence-run-id "$EVIDENCE_RUN_ID" \
  --evidence-root "$EVIDENCE_ROOT/docker-security"
uv run python scripts/validate_two_node_docker_runtime.py security-summary \
  --evidence-run-id "$EVIDENCE_RUN_ID" \
  --source-trust-report "$EVIDENCE_ROOT/docker-security/two-node-docker-source-trust-compute.json" \
  --source-trust-report "$EVIDENCE_ROOT/docker-security/two-node-docker-source-trust-display.json" \
  --static-report "$EVIDENCE_ROOT/docker-security/static-compose-env-check.json" \
  --smoke-report "$EVIDENCE_ROOT/docker-security/docker-smoke.json" \
  --output "$EVIDENCE_ROOT/docker-security/summary.json"
```

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" exec display-api sh -lc '
  set -eu
  forbidden_found=0
  for bin in sbatch scancel squeue srun sacct sinfo scontrol munge unmunge
  do
    if command -v "$bin" >/dev/null 2>&1; then
      printf "forbidden binary present: %s\n" "$bin"
      forbidden_found=1
    fi
  done
  for path in /etc/slurm /run/munge /etc/munge /var/run/munge /run/docker.sock /var/run/docker.sock
  do
    if [ -e "$path" ] || [ -L "$path" ]; then
      printf "forbidden path present: %s\n" "$path"
      forbidden_found=1
    fi
  done
  for key in \
    NHMS_SERVICE_ROLE \
    NHMS_REQUIRE_SERVICE_ROLE \
    NHMS_AUTH_MODE \
    NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS \
    NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS \
    NHMS_PUBLISHED_ARTIFACT_ROOT \
    NHMS_PUBLISHED_ARTIFACT_URI_PREFIX \
    NHMS_PUBLISHED_ARTIFACT_S3_BUCKET \
    NHMS_PUBLISHED_ARTIFACT_S3_PREFIX \
    NHMS_LOG_TAIL_MAX_BYTES \
    NHMS_ARTIFACT_BACKEND \
    OBJECT_STORE_PREFIX \
    S3_ENDPOINT_URL \
    S3_BUCKET_NAME \
    CORS_ALLOWED_ORIGINS
  do
    value="$(printenv "$key" 2>/dev/null || true)"
    printf "%s=%s\n" "$key" "${value:-<unset>}"
  done
  for key in DATABASE_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  do
    value="$(printenv "$key" 2>/dev/null || true)"
    if [ -n "$value" ]; then
      printf "%s=<redacted>\n" "$key"
    else
      printf "%s=<unset>\n" "$key"
    fi
  done
  for key in \
    SLURM_GATEWAY_URL \
    SLURM_GATEWAY_BACKEND \
    WORKSPACE_ROOT \
    RUN_WORKSPACE_ROOT \
    SHARED_LOG_ROOT \
    OBJECT_STORE_ROOT \
    NHMS_BASINS_ROOT \
    NHMS_MODEL_ASSET_ROOT \
    SLURM_GATEWAY_TEMPLATE_DIR \
    SLURM_GATEWAY_WORKSPACE_DIR \
    MUNGE_SOCKET \
    MUNGE_KEY \
    SHUD_EXECUTABLE \
    DOCKER_HOST
  do
    value="$(printenv "$key" 2>/dev/null || true)"
    if [ -n "$value" ]; then
      printf "%s=<present>\n" "$key"
      forbidden_found=1
    else
      printf "%s=<absent>\n" "$key"
    fi
  done
  test "$forbidden_found" -eq 0
'
```

API 边界检查：

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/api/v1/runtime/config
curl -i http://127.0.0.1:8000/api/v1/slurm/health
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/retry
curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/cancel

# 优先从未跟踪 0600 env 文件读取；不存在时交互式静默输入。
block_operator_auth_source() {
  echo "BLOCKED: $*" >&2
  exit 1
}

if [ -f infra/env/operator-auth.env ]; then
  operator_auth_mode="$(stat -c '%a' infra/env/operator-auth.env)" || \
    block_operator_auth_source "cannot stat infra/env/operator-auth.env before sourcing"
  if [ "$operator_auth_mode" != "600" ]; then
    block_operator_auth_source "infra/env/operator-auth.env must be mode 0600 before sourcing"
  fi
  . infra/env/operator-auth.env
else
  read -r -s -p "Operator auth token: " OPERATOR_AUTH_TOKEN
  printf '\n'
fi
: "${OPERATOR_AUTH_TOKEN:?operator auth token required}"

OPERATOR_SECRET_DIR="${OPERATOR_SECRET_DIR:-/scratch/frd_muziyao/nwm-secret-tmp}"
mkdir -p "$OPERATOR_SECRET_DIR"
chmod 700 "$OPERATOR_SECRET_DIR"
OPERATOR_CURL_HEADER="$(mktemp "$OPERATOR_SECRET_DIR/operator-auth-header.XXXXXX")"
chmod 600 "$OPERATOR_CURL_HEADER"
trap 'rm -f "$OPERATOR_CURL_HEADER"' EXIT

{
  printf '%s' 'Authorization: '
  printf '%s' 'Bearer '
  printf '%s\n' "$OPERATOR_AUTH_TOKEN"
} >"$OPERATOR_CURL_HEADER"
unset OPERATOR_AUTH_TOKEN

operator_auth_curl() {
  curl --header "@$OPERATOR_CURL_HEADER" "$@"
}

operator_auth_curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/retry
operator_auth_curl -i -X POST http://127.0.0.1:8000/api/v1/runs/<run_id>/cancel
```

通过条件：

- runtime config 报告 `display_readonly`。
- `/api/v1/slurm/*` 不可用。
- 无授权请求应返回 401/403 或 `AUTH_REQUIRED` / `NOT_AUTHORIZED` 等稳定拒绝错误。
- 如果部署还提供 viewer-only 或非运维 token/header，要把它作为单独的 unauthorized lane 记录，和无授权请求分开。
- 只有真实生产运维 auth token/header 可走授权 manual-action lane；如果部署拿不到这条授权路径，本 lane 记为 `BLOCKED`，不能 `PASS`。
- 授权请求应返回 `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` 或等价稳定错误，且不构造 DB write 或 Gateway 依赖。
- evidence 显示 27 没有 Gateway 调用、没有业务终态写入、published artifact mount 是 readonly。
- `command_index.md` 和复制到 review / incident handoff 的 evidence 只能记录未展开变量或 redacted/helper
  调用；operator auth setup 可记录为 `prepare 0600 curl header under /scratch/frd_muziyao/nwm-secret-tmp/<redacted>`，
  授权请求只记录 `operator_auth_curl ...`。不得包含原始 DSN、token、signature 或完整 auth header。

## 12. Evidence Paths

默认 evidence root：

```bash
export RUN_ID="two-node-e2e-$(date -u +%Y%m%dT%H%M%SZ)"
export EVIDENCE_ROOT="artifacts/two-node-e2e/$RUN_ID"
mkdir -p "$EVIDENCE_ROOT"/{22-compute,27-display,cross-plane,manual-ops,db,api,browser,slurm,logs,docker-security,docker-preflight,final-e2e-evidence}
```

允许的 project-created 输出路径只有：

```text
artifacts/
/scratch/frd_muziyao/<project-specific-dir>/
```

每个 evidence bundle 必须记录 status：`PASS`、`PARTIAL`、`FAIL` 或 `BLOCKED`。缺真实 readonly DB、缺 live browser、缺 Slurm probe 或缺 cross-plane strict identity 时，只能记 `BLOCKED` 或 `PARTIAL`，不能补写 `PASS`。

最终聚合前必须写 `$EVIDENCE_ROOT/run.json`，作为当前 bundle 的 metadata 和 strict identity 真相源：

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

创建后先执行 `python -m json.tool "$EVIDENCE_ROOT/run.json" >/dev/null`。单 source 演练必须把 `declared_sources`
缩成实际 source，并设置 `"reduced_scope": true`。

## 13. Docker Validation Matrix

| Evidence | 记录内容 | PASS 边界 |
| --- | --- | --- |
| `docker-preflight/` | DockerRootDir、cache/space、TMPDIR、evidence root | 只证明 Docker 环境可继续，不证明 E2E |
| `22-compute/` | compute compose config/up/ps/logs、scheduler-once、writer DB、published artifact write | 只证明 22 compute lane |
| `27-display/` | display compose config/up/ps/logs、runtime config、readonly mount | 只证明 27 display lane |
| `db/` | readonly DB role、`current_user`、permission probes、redacted DSN | 真实 readonly DB 缺失时 BLOCKED |
| `api/` | health、runtime config、models、stations、latest-product、ops/jobs/logs | strict identity 缺失时不得 PASS |
| `browser/` | `/hydro-met`、`/ops` screenshots、DOM/network/console、identity-bound `ops_jobs` 和 `ops_job_logs`（含 `job_id`） | mock API 或缺 `/ops` jobs/logs payload 不能算 production-like PASS |
| `slurm/` | 22 Gateway health、minimal submit probe、Slurm receipt | 27 不需要也不应具备 Slurm CLI |
| `logs/` | published log URI、read result、缺失原因 | 不能读取 22 private workspace |
| `manual-ops/` | `nhms.two_node_e2e.manual_ops.v1`，含当前 `evidence_run_id`、脱敏 operator auth metadata、27 response evidence、27 no-side-effect proof、22 receipt provenance；每个实际 22 receipt provenance 必须绑定 producer、source、当前 bundle、redaction 和可选 artifact hash | 布尔断言、空 provenance 或 27 receipt 不能 PASS |
| `docker-security/` | `security-summary` 生成的 `nhms.two_node_docker.security_summary.v1`，含 source-trust/static/smoke artifact 路径与 sha256、no Slurm/Munge/Docker socket、HostConfig/mount/env 检查 | 手写或缺 source artifact 不能 PASS；任一 27 控制能力为 FAIL |
| `cross-plane/` | 同一 `run_id/source/cycle_time/model_id` 从 22 到 27 | historical latest/mock 数据不能 PASS |
| `final-e2e-evidence/` | `scripts/validate_two_node_e2e_evidence.py` 聚合后的 lane/source/blocker/finding 汇总 | 只有全 lane、全 declared source、live readonly/display/strict identity 证据都通过才 PASS |

完整 GFS/IFS readonly DB evidence 不能只跑一次单 source。先分别运行 `scripts/validate_readonly_db_boundary.py`
写入 per-source lane，再用同一脚本的 `--merge-source-dir` 合并到 `$EVIDENCE_ROOT/db/readonly-db-boundary/`：
每个 source dir 必须包含匹配的 `summary.json`、`role.json`、`route_smoke.json`、`permission_probes.json`；
merge 会拒绝缺 sibling、sibling 与 summary 不一致、source dir 越界或 symlink、非 live/PASS source、
`validation_provenance.mode != "live"`、`live_readonly_proof != true`、重复/缺失 source，以及与当前 final
bundle 无关的 stale source dir。source run ID 使用 `$EVIDENCE_RUN_ID-db-GFS`/`$EVIDENCE_RUN_ID-db-IFS`
或 `$EVIDENCE_RUN_ID-gfs`/`$EVIDENCE_RUN_ID-ifs`；其他命名必须在 source summary 或
`validation_provenance` 中显式绑定当前 final bundle，并记录 `parent_evidence_root`/`final_evidence_root`
指向当前 `$EVIDENCE_PARENT` 或 `$EVIDENCE_ROOT`。prefix-style source lane 必须实际位于当前
`$EVIDENCE_PARENT` 下；不能从另一个 approved root 复用同名 run ID。merged summary 会记录 source artifact path/sha256/run ID
和 source provenance。

```bash
uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID-db-GFS" \
  --source GFS \
  --cycle-time '<gfs-cycle-time>' \
  --strict-run-id '<gfs-business-run-id>' \
  --model-id basins_qhh_shud \
  --job-id '<gfs-job-id-with-published-log>' \
  --force

uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID-db-IFS" \
  --source IFS \
  --cycle-time '<ifs-cycle-time>' \
  --strict-run-id '<ifs-business-run-id>' \
  --model-id basins_qhh_shud \
  --job-id '<ifs-job-id-with-published-log>' \
  --force

uv run python scripts/validate_readonly_db_boundary.py \
  --evidence-root "$EVIDENCE_PARENT" \
  --run-id "$EVIDENCE_RUN_ID" \
  --merge-declared-source GFS \
  --merge-declared-source IFS \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-GFS/db/readonly-db-boundary" \
  --merge-source-dir "$EVIDENCE_PARENT/$EVIDENCE_RUN_ID-db-IFS/db/readonly-db-boundary" \
  --force
```

最终聚合命令：

```bash
set -euo pipefail
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

单 source 或明确缩减范围的演练必须在 DB merge 和 final 聚合两处使用 `--reduced-scope`，并用
`--merge-declared-source <source>` 声明实际 source scope；最终只能是 `PARTIAL` 或更低状态，不能作为完整跨面 PASS。
默认 full-scope merge 仍要求 GFS 和 IFS 两个 source dir。
聚合器会拒绝非 `artifacts/` 或 `/scratch/frd_muziyao/...` evidence root，并把 stale bundle ID、缺 live Docker/container、
缺真实 readonly DB、缺 browser/API/log strict identity、缺生产 operator auth、mock/historical latest、writer DB 和 27 控制面 receipt
纳入最终 blocker/finding。

## 14. Rollback

停止 27 display：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role display
sudo systemctl stop nhms-display-compose.service
docker compose --env-file "$CHECKOUT_ROOT/infra/env/display.env" -f "$CHECKOUT_ROOT/infra/compose.display.yml" down
```

停止 22 compute：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute
sudo systemctl stop nhms-compute-compose.service
docker compose --env-file "$CHECKOUT_ROOT/infra/env/compute.env" -f "$CHECKOUT_ROOT/infra/compose.compute.yml" down
```

回滚镜像：

```bash
set -euo pipefail
: "${EVIDENCE_ROOT:?export shared E2E EVIDENCE_ROOT first}"
CHECKOUT_ROOT="${CHECKOUT_ROOT:-$PWD}"
TRUST_ROOT="${TRUST_ROOT:-$(dirname "$CHECKOUT_ROOT")}"
cd "$CHECKOUT_ROOT"
$EDITOR "$CHECKOUT_ROOT/infra/env/compute.env"
$EDITOR "$CHECKOUT_ROOT/infra/env/display.env"
uv run python scripts/validate_two_node_docker_source_trust.py \
  --checkout-root "$CHECKOUT_ROOT" \
  --trust-root "$TRUST_ROOT" \
  --evidence-root "$EVIDENCE_ROOT/docker-security" \
  --evidence-run-id "$(basename "$EVIDENCE_ROOT")" \
  --trusted-owner root --trusted-owner nhms-deploy \
  --role compute --role display
sudo systemctl restart nhms-compute-compose.service
sudo systemctl restart nhms-display-compose.service
```

回滚原则：

- 27 display 出问题时，不要把公网 27 切回 `dev_monolith` 或 writer DB。
- Slurm Gateway 容器化失败时，回退到 22 host Gateway 服务。
- Artifact/log 读取失败时，允许显示 `log_uri` 和人工提示，不要把 22 私有 workspace 挂给 27。
- latest-product strict identity 缺失时，cross-plane 记 `BLOCKED`，不能用 historical latest 代替。
- readonly DB 权限缺失时，修正 SELECT grants；不要用 writer credential 冒充生产只读验证。
- systemd restart/reload 会重新执行 checkout 中的 compose/env；每次回滚或重启前都要重新运行
  `scripts/validate_two_node_docker_source_trust.py`，记录 path component、unit/compose/env ownership/mode 证据。

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
