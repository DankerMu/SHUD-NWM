# 两节点部署总览

最后更新：2026-06-22
适用范围：M22 两节点 role contract / design-intent background；不是当前 host
分配操作手册。当前值守入口见
[`current-production-ops.md`](current-production-ops.md)。

> **⚠️ 2026-06-22 当前部署事实 vs 本文档设计意图的差异**：
>
> 本文档描述的是 M22 **设计意图**（22 = writer / DB mutation，27 = readonly）。
> 当前 (2026-06-22) **物理部署已偏离**：
> - **node-22 是纯计算 / Slurm / SHUD / artifact producer**，不连任何活 DB
>   （本机 PG `:55433` 是 historical、do-not-connect、pending removal）
> - **node-27 一台机器同时跑** active primary PG (`:55432`) + data-plane ingest
>   + display API (`:8080`) + 前端
> - 公网入口 `https://test.nwm.ac.cn` 由 27 反代对外
> - node-27 是 live DB/display/frontend oracle；node-22 只在改 sbatch、Slurm
>   gateway、SHUD runtime 或调度行为时作为 Slurm scheduling oracle；本地 lint/unit/
>   OpenSpec 检查不能替代 node-27 live receipt。
>
> 设计文档保留作为 role contract reference（代码层 `ServiceRole` 仍按角色契约强制）；
> 物理 host 分配以 `CLAUDE.md` 服务器拓扑段 + `docs/governance/ROLE_BOUNDARY.md` 顶部
> "Current physical deployment" 段为准。下文 §3.4 权限矩阵、§4 共享表、§5.2 22 compute
> 启动段都是设计意图描述，**不反映当前生产 host 分配**。

## 1. M22 设计结论（历史）

以下是 M22 设计意图，不是 2026-06-22 的当前物理部署事实。当前事实见上方
banner 和 [`current-production-ops.md`](current-production-ops.md)。

M22 设计将系统从“单机既计算又展示”的形态，收敛为两个清晰角色：

- 22 节点是计算控制面，负责生产数据、提交计算、发布结果和执行运维控制动作。
- 27 节点是只读展示面，负责面向用户提供 `/` 单页地图、`/ops` 和查询 API，只读取数据库、已发布展示产物和 shared object-store mirror。
- 两个节点之间不通过 27 调 22 控制接口完成生产操作；共享边界只有 PostgreSQL、published artifacts 和只读 shared object-store mirror。
- Docker 部署使用同一个 app 镜像，但用不同 Compose/env/systemd 文件启动成不同角色。
- 27 节点的设计目标是“能看、能诊断、能复制交接信息”，不是“能直接重跑或停止计算”。

这份文档用于说明系统怎么运转、每个节点负责什么、怎么部署、会产生哪些产物。当前值守命令、生产路径、产物位置和已知卡点见
[`current-production-ops.md`](current-production-ops.md)。具体命令见 [`infra/README.two-node-docker.md`](../../infra/README.two-node-docker.md)，端到端证据要求见 [`docs/runbooks/two-node-production-e2e-plan.md`](two-node-production-e2e-plan.md)。

## 2. 整体运转流程

一次 QHH 预报或展示链路可以按下面顺序理解：

```text
资料源 GFS/IFS
  -> 22 发现周期并下载资料
  -> 22 调度 canonical / forcing / SHUD / parse / publish 任务
  -> Slurm 计算节点执行具体计算
  -> 22 写入 pipeline、met、hydro、ops 状态到 PostgreSQL
  -> 22 把完整 runs/<run_id>/... copyback 到 shared object-store mirror
  -> 22 把展示产品、manifest、日志和诊断证据发布到 published artifacts
  -> 27 只读查询 PostgreSQL、published artifacts 和 shared object-store mirror
  -> 用户在 / 和 /ops 查看结果、日志、异常和人工处理建议
```

关键边界是：22 生产并发布，27 只读消费并展示。27 不使用 22 的私有 workspace 来拼出结果，也不调用 Slurm Gateway 来处理失败任务。

当前展示 route authority：`/` 是 active single-map display entrypoint，`/ops` 是 active
operational display path。`/hydro-met`、`/forecast`、`/meteorology`、`/flood-alerts`、
`/basins/:id`、`/segments/:id` 只作为 legacy redirect / compatibility aliases 保留，不是
独立 active 页面，也不能作为当前 live display proof。

当前发布目录约定为：

```text
22 host path: /ghdc/data/nwm/published
27 host path: /home/ghdc/nwm/published
container path: /var/lib/nhms/published
URI prefix: published://
```

22 看到的 `/ghdc/data` 是 27 上 `/home/ghdc` 的 NFS 导出；因此 22 写入
`/ghdc/data/nwm/published` 后，27 可以从本机 `/home/ghdc/nwm/published` 读取同一份发布产物。

## 3. 角色和职责

### 3.1 22 节点：计算控制面

22 节点运行角色为 `compute_control`。

它负责：

- 发现 GFS/IFS 周期，下载或读取资料缓存。
- 运行正式生产调度入口 `nhms-pipeline plan-production`。
- 访问 Slurm Gateway 或本机 Slurm 能力，提交计算任务。
- 管理计算侧 workspace、Basins/model assets、对象存储或发布目录。
- 写入 PostgreSQL 中的 run、stage、job、met、hydro、ops 等状态。
- 将完整 `runs/<run_id>/...` copyback 到 shared object-store mirror，并把展示需要的结果、manifest、日志、诊断证据写入 published artifacts。
- 执行 retry/cancel 等控制面动作，并把执行后的状态和日志继续发布出来。

它不应该：

- 把控制面 API 暴露成公网入口。
- 让 27 通过私有目录挂载来绕过发布面读取中间态。
- 用 mock Gateway 或 historical latest 代替真实生产链路证据。

### 3.2 Slurm 计算节点：执行环境

Slurm 计算节点不是第三个业务部署节点，但它是 22 计算控制面的执行资源。

它负责：

- 执行下载、转换、forcing、SHUD、解析、发布等批处理任务。
- 读取 Basins/model assets、workspace 输入和对象存储输入。
- 写回运行日志、阶段产物、manifest 或状态文件。

它必须能访问 22 调度任务所需的共享路径、数据库或对象存储。计算节点的可达性属于 22 侧 live proof，不属于 27 展示节点能力。

当前 22 节点 live probe 已确认一个重要边界：Slurm 计算节点可运行作业，但计算节点未必直接挂载
`/ghdc`。因此 `/ghdc/data/nwm/published` 和 `/ghdc/data/nwm/object-store` 是 22 与 27 之间的共享读面，
不应被默认当成 Slurm 作业运行目录。Slurm 作业应使用计算节点可见的 workspace/object-store 路径运行；
任务完成后，由 22 侧 publish/copyback 步骤把完整 `runs/<run_id>/...` 同步到
`/ghdc/data/nwm/object-store`，把展示产品、manifest、日志和诊断证据写入 `/ghdc/data/nwm/published`。

### 3.3 27 节点：只读展示面

27 节点运行角色为 `display_readonly`。

它负责：

- 运行 FastAPI 和前端静态资源入口。
- 提供 `/` 单页地图水文气象展示。
- 提供 `/ops` 运维展示，包括阶段、作业、日志、异常、队列不可用提示和诊断信息复制。
- 只读查询 PostgreSQL 中的模型、气象、水文、pipeline 和 ops 状态。
- 只读读取 published artifacts 中的展示产品、manifest 和日志；只读读取 shared object-store mirror 中的完整运行产物。

它不负责：

- 不运行正式生产调度。
- 不提交 Slurm job。
- 不调用 Slurm Gateway。
- 不执行 retry/cancel。
- 不写 hydro/met/pipeline 终态。
- 不挂载 22 私有 workspace、`.nhms-runs`、private `/scratch`、Munge、Slurm 配置或 Docker socket。

用户在 27 看到失败任务时，正确动作是复制诊断信息，交给 22 的 operator 在计算控制面处理。22 处理完成后，27 再通过只读状态刷新看到结果。

### 3.4 操作权限矩阵

22 节点拥有全部自动化能力（计算 + 发布的读写）；27 节点只读消费，即只读 DB 副本、
只读 published 产物（`/ghdc/data/nwm/published`，27 本机为 `/home/ghdc/nwm/published`）和
只读 shared object-store mirror（`/ghdc/data/nwm/object-store`，27 本机为 `/home/ghdc/nwm/object-store`），
不调用任何写入或调度命令。

| 操作 | node-22（compute_control） | node-27（display_readonly） |
| --- | --- | --- |
| `nhms-pipeline plan-production`（调度计划/提交） | 拥有（dry-run 计划 + `--submit` 提交） | 禁止（不运行正式调度入口） |
| `nhms-pipeline publish-qdown`（发布 q_down 产物） | 拥有（写 published） | 禁止 |
| `nhms-pipeline publish-tiles`（发布瓦片产物） | 拥有（写 published） | 禁止 |
| retry / cancel（控制面动作） | 拥有（写 DB + Gateway） | 禁止（只展示诊断和处理建议，无真实控制入口） |
| DB `hydro` schema 写 | 读写 | 只读 |
| DB `met` schema 写 | 读写 | 只读 |
| DB `ops` schema 写（`ops.pipeline_job`/`ops.pipeline_event`） | 读写 | 只读 |
| `/ghdc/data/nwm/published` 读 | 读（写后自读） | 只读（本机 `/home/ghdc/nwm/published`） |
| `/ghdc/data/nwm/published` 写 | 写 | 禁止（只读挂载） |
| `/ghdc/data/nwm/object-store` 读 | 读（copyback 后自读） | 只读（本机 `/home/ghdc/nwm/object-store`） |
| `/ghdc/data/nwm/object-store` 写 | 写（publish/copyback only） | 禁止（只读挂载） |
| Slurm Gateway / job 提交 | 拥有 | 禁止（不调用、不安装 CLI） |

27 出现任何写/调度能力（可写 DB、可写 published、可提交 Slurm、可触发 retry/cancel），都视为
角色边界破坏，按第 7.3 节处理，不能临时把 27 改成单机控制面。

## 4. 节点之间共享什么

| 共享面 | 22 节点 | 27 节点 | 用途 |
| --- | --- | --- | --- |
| PostgreSQL/PostGIS/TimescaleDB | 读写账号 | 只读账号 | pipeline、met、hydro、model、ops 状态源 |
| published artifacts | 写 `/ghdc/data/nwm/published` | 只读读 `/home/ghdc/nwm/published` | `/` 单页地图、`/ops`、日志弹窗和诊断 |
| shared object-store mirror | publish/copyback 写 `/ghdc/data/nwm/object-store` | 只读读 `/home/ghdc/nwm/object-store` | 完整 `runs/<run_id>/...` 运行产物交付 |
| Docker 镜像 | 同一 app 镜像 | 同一 app 镜像 | 通过环境变量和 Compose 分化角色 |

不共享的内容：

- 22 私有 workspace。
- 22 `.nhms-runs`。
- 22 私有 `/scratch`。
- Slurm/Munge/Docker socket。
- 22 的 writer DB credential 和控制面 credential。

## 5. 部署方式

### 5.1 镜像

两节点使用同一个应用镜像。镜像里包含：

- Python 后端和服务代码。
- 前端构建产物 `apps/frontend/dist`。
- OpenAPI、schema、配置、sbatch 模板和 Docker entrypoint。

镜像启动时通过 `NHMS_SERVICE_ROLE` 决定角色。生产或类生产启动时必须显式设置角色，不能依赖默认单机开发模式。

### 5.2 22 节点部署入口

22 使用：

- Compose 文件：[`infra/compose.compute.yml`](../../infra/compose.compute.yml)
- 环境样例：[`infra/env/compute.example`](../../infra/env/compute.example)
- 本地真实环境文件：`infra/env/compute.env`，不提交
- systemd 样例：[`infra/systemd/nhms-compute-compose.service`](../../infra/systemd/nhms-compute-compose.service)

Compose 中包含两个服务形态：

- `compute-api`：计算控制面 API，默认不发布公网端口。
- `scheduler-once`：手工 profile，用于一次性运行 no-flag 生产计划入口
  `nhms-pipeline plan-production --plan`。

22 的环境文件需要配置 writer DB、workspace、object-store、published artifact 写入路径、scheduler
lock/evidence/runtime/temp roots、非空 `NHMS_SCHEDULER_ALLOWED_ROOTS`、service role、
source/model/basin filters、interval/max-pass bounds、Basins/model assets、Slurm Gateway 和资料源参数。

当前 22 published artifact host root 使用 `/ghdc/data/nwm/published`，即写入 27 导出的 NFS 发布面。
`scheduler-once` 不传 `--workspace-root`、`--lock-path` 或 `--evidence-dir`；这些路径必须分别由
`WORKSPACE_ROOT`、`NHMS_SCHEDULER_LOCK_ROOT` 和 `NHMS_SCHEDULER_EVIDENCE_ROOT` 解析。显式 root flags
只保留给本地诊断兼容，不作为 22 business proof path。
连续或 timer 模式也使用同一套 env roots，并显式限定 `NHMS_SCHEDULER_INTERVAL_SECONDS`、
`NHMS_SCHEDULER_MAX_PASSES`、`NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE` 和 source/model/basin filters；不得改回
应用目录内 `.nhms-workspace`。
`NHMS_SCHEDULER_ALLOWED_ROOTS` 是独立批准的 root 边界，不能在运行时由这些候选 root 自动推导；缺失或为空时
no-flag scheduler 必须在 registry/adapters/orchestrator 前阻塞。

### 5.3 27 节点部署入口

27 使用：

- Compose 文件：[`infra/compose.display.yml`](../../infra/compose.display.yml)
- 环境样例：[`infra/env/display.example`](../../infra/env/display.example)
- 本地真实环境文件：`infra/env/display.env`，不提交
- systemd 样例：[`infra/systemd/nhms-display-compose.service`](../../infra/systemd/nhms-display-compose.service)

27 的容器是只读展示容器：

- 容器根文件系统为只读。
- 只允许挂载 published artifacts，且挂载为只读。
- 丢弃 Linux capabilities，并启用 `no-new-privileges`。
- 对外只绑定本机 `127.0.0.1:${NHMS_DISPLAY_API_PORT:-8080}`，通常由反向代理或内网入口转发。

27 的环境文件需要配置 readonly DB、readonly published artifact root、只读对象存储凭证或只读 shared object-store mirror 挂载、CORS 和端口。不能配置 Slurm Gateway、workspace、Basins/model assets、Docker socket、`NHMS_OBJECT_STORE_COPYBACK_ROOT` 或其他计算控制能力。

当前 27 published artifact host root 使用 `/home/ghdc/nwm/published`，并在 display compose 中以只读方式挂载到容器内 `/var/lib/nhms/published`。

### 5.4 systemd 的作用

systemd 单元只是把对应 Compose 服务交给系统服务管理：

- 开机后拉起对应节点的 Compose。
- 支持 stop/reload/restart。
- 固定 WorkingDirectory 和 env 文件位置。

systemd 不是新的应用运行模式，也不会改变 22/27 的职责边界。安装或重启 systemd 前，仍要先做 source-trust 和 Docker 预检，避免让 Docker 读取权限不安全的 env、compose 或 unit 文件。

## 6. 每个节点的产物

### 6.1 22 节点产物

22 会产生或更新：

- PostgreSQL 中的 run、stage、job、event、met、hydro、ops 状态。
- workspace 中的计算中间态和任务工作目录。
- object-store 或发布目录中的 raw/canonical/forcing/output/manifest 等产物。
- published artifacts 中供 27 读取的展示产品、manifest、日志和诊断证据。
- Slurm job id、array task 状态、stdout/stderr、retry/cancel receipt。
- 22 侧部署和 E2E 证据，通常放在 `artifacts/` 或 `/scratch/frd_muziyao/` 下。

22 的关键交付不是“本机跑过”，而是“把可展示、可追踪、可诊断的发布产物写到了 27 可读的共享面”。

### 6.2 27 节点产物

27 自身不生产水文气象结果。它产生的是展示和诊断相关产物：

- 面向用户的 `/` 单页地图页面。
- 面向运维查看的 `/ops` 页面。
- API 响应中的 runtime config、latest-product、pipeline、jobs、logs 等只读查询结果。
- 失败任务的诊断复制内容，供 operator 带到 22 处理。
- 27 侧 Docker/security/readonly DB/browser evidence。
- 27 服务日志和反向代理日志。

27 的关键交付是“只读展示链路可用，并且物理上没有控制计算的能力”。

### 6.3 共享产物

共享产物用于把 22 生产结果交给 27 展示：

- 严格绑定同一个 `run_id/source/cycle_time/model_id/basin_id` 的 DB 状态。
- `published://`、allowlisted `file://` 或 allowlisted `s3://` 日志 URI。
- latest-product 所需的产品身份和时间窗口。
- `/ops` 中 stages/jobs/logs 能对应同一轮生产身份。

跨面联调必须证明这些身份一致，不能用历史 latest 或 mock API 代替。

## 7. 典型操作路径

### 7.1 正常生产和展示

1. 22 operator 确认资料源、DB、Slurm、workspace、published artifacts 可用。
2. 22 运行调度，提交计算任务。
3. 计算节点完成 SHUD 和后处理。
4. 22 写 DB 状态并发布展示产品、manifest 和日志。
5. 27 读取 readonly DB、published artifacts 和只读 shared object-store mirror。
6. 用户访问 `/` 查看水文气象结果，访问 `/ops` 查看运行状态和日志。

### 7.2 失败诊断和人工处理

1. 用户或 operator 在 27 `/ops` 看到失败 stage/job。
2. 27 页面只提供诊断复制、日志查看和 22 处理建议。
3. 22 operator 根据诊断信息在计算控制面执行 retry/cancel 或其他处理。
4. 22 更新 DB 状态并发布新的日志或 receipt。
5. 27 刷新后展示处理后的只读状态。

### 7.3 只读边界异常

如果 27 出现下面任一情况，不能继续声明部署通过：

- `/api/v1/slurm/*` 可访问。
- 27 上出现真实 retry/cancel 控制按钮或控制 POST。
- 27 需要 writer DB credential 才能展示。
- 27 需要挂载 22 私有 workspace 才能看日志。
- 27 容器挂载 Docker socket、Slurm/Munge 或额外可写业务目录。

这些都是角色边界破坏，应该先修部署配置或回滚展示入口，而不是临时把 27 改成单机控制面。

## 8. 部署前检查清单

### 8.1 两边都要确认

- 两台机器 checkout 同一个 git commit。
- 两台机器使用同一个 app image digest。
- 本机依赖按 Linux 环境重建，不复用其他机器的 `.venv` 或 `node_modules`。
- Docker 可用，DockerRootDir、磁盘空间、`TMPDIR` 和 evidence root 已记录。
- 本地 env 文件权限为 `0600`，且没有被提交。
- 项目生成的临时产物写入仓库 ignored 的 `artifacts/` 或 `/scratch/frd_muziyao/`。

### 8.2 22 重点确认

- `NHMS_SERVICE_ROLE=compute_control`。
- `DATABASE_URL` 是 writer-capable 账号。
- `WORKSPACE_ROOT`、`OBJECT_STORE_ROOT`、`NHMS_PUBLISHED_ARTIFACT_ROOT` 可写。
- `NHMS_SCHEDULER_LOCK_ROOT` 和 `NHMS_SCHEDULER_EVIDENCE_ROOT` 位于 `WORKSPACE_ROOT` 内且可写。
- `NHMS_SCHEDULER_ALLOWED_ROOTS` 非空，并覆盖 `WORKSPACE_ROOT`、`OBJECT_STORE_ROOT`、
  `NHMS_PUBLISHED_ARTIFACT_ROOT`、`NHMS_SCHEDULER_RUNTIME_ROOT` 和 `NHMS_SCHEDULER_TEMP_ROOT`。
- `NHMS_SCHEDULER_RUNTIME_ROOT`、`NHMS_SCHEDULER_TEMP_ROOT` 和实际挂载一致。
- `NHMS_SCHEDULER_SOURCES`、`NHMS_SCHEDULER_MODEL_IDS`、`NHMS_SCHEDULER_BASIN_IDS`、
  `NHMS_SCHEDULER_INTERVAL_SECONDS`、`NHMS_SCHEDULER_MAX_PASSES`、`NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE`
  已按本次业务验证范围设置。
- Basins/model assets 可读。
- Slurm Gateway 或 Slurm CLI 能提交、查询和取消任务。
- 资料源或资料缓存可访问。
- 计算节点的 GRIB 工具链（`cdo` + `libeccodes`）：计算节点（cn01-24）系统通常不带 `cdo`
  和 `libeccodes`，IFS 下载裁剪（cdo）与 canonical 的 cfgrib 读 GRIB 都依赖它们。通过
  共享盘上的 conda env 提供（`conda create -p <root> -c conda-forge cdo eccodes`，root 在
  全计算节点可达的共享盘），并设 `NHMS_GRIB_ENV_ROOT=<root>`；调度渲染 sbatch 时会把
  `<root>/bin` 注入 PATH、`<root>/lib` 注入 LD_LIBRARY_PATH。缺失 `cdo` 会导致 IFS 下载
  fail-loud（error_code=`CDO_MISSING`）。
- 预报数据保留清理：`NHMS_RETENTION_ENABLED=true` 时每轮调度按 `cycle_time` 清理早于
  `NHMS_RETENTION_DAYS`（默认 14）的 raw 与计算中间态；published 产物与静态网格始终保留。
  首次上线建议 `NHMS_RETENTION_DRY_RUN=true` 先看清理计划，确认后再设 false 真删。
- 空间裁剪与网格基线：canonical/forcing 的网格是从 GRIB / `grid.json` 动态读取的，自动适应
  裁剪后的区域网格，无需改代码。但 `canonical/{source}/grid/{grid_id}/grid.json` 与
  `met.forcing_interp_weights`（按 grid_id 缓存）是按 grid_id 固定的网格基线：**全新部署**
  会用裁剪场首次建立区域基线、天然一致；**仅当从已建立全球网格基线的环境切换到裁剪**时，
  需一次性删除旧 `grid.json` 并清空对应 interp_weights，让其用区域场重建（否则 canonical
  会因 grid signature 不匹配 fail-loud）。

### 8.3 27 重点确认

- `NHMS_SERVICE_ROLE=display_readonly`。
- `DATABASE_URL` 是 readonly 账号，并有 denied write probe 证据。
- published artifact host root 只读挂载。
- 未配置 Slurm Gateway、workspace、Basins/model assets、Docker socket、Munge 或 Slurm 路径。
- `/api/v1/runtime/config` 返回 display readonly 能力。
- `/ops` 不请求 Slurm 队列深度，不显示真实控制按钮。

## 9. 验收边界

可以声明的：

- M22 deterministic 和 Docker 安全边界已在代码、测试和 CI 中覆盖。
- 22/27 的 Docker 部署形态、env 示例、systemd 示例和只读边界已经落地。
- 27 的产品定位是只读展示和诊断交接。

不能直接声明的：

- 目标生产环境已 final production ready。
- 任何历史 latest 可以代表本轮两节点 E2E。
- 27 可以执行 retry/cancel。
- 27 可以用 writer DB 或挂载 22 workspace 作为临时生产形态。

正式上线前还需要目标环境 live proof：真实 DB、真实 published artifacts、真实 Slurm、真实 GFS/IFS 下载、真实 QHH SHUD runtime、无 mock 浏览器 E2E、跨面身份一致性和 `docs/bugs.md` 中问题的复测归因。

## 10. 相关文档

- Docker 命令手册：[`infra/README.two-node-docker.md`](../../infra/README.two-node-docker.md)
- 两节点 E2E 证据计划：[`docs/runbooks/two-node-production-e2e-plan.md`](two-node-production-e2e-plan.md)
- 两节点 Docker 部署方案：[`docs/plans/2026-05-27-two-node-docker-readonly-display-deployment-plan.md`](../plans/2026-05-27-two-node-docker-readonly-display-deployment-plan.md)
- 只读展示重构方案：[`docs/plans/2026-05-27-two-node-readonly-display-refactor-plan.md`](../plans/2026-05-27-two-node-readonly-display-refactor-plan.md)
- M22 OpenSpec：[`openspec/changes/m22-two-node-docker-readonly-display/`](../../openspec/changes/m22-two-node-docker-readonly-display/)
- 当前项目进度索引：[`progress.md`](../../progress.md)
