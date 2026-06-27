# Node-27 Download Migration And Node-22 DB Retirement Plan

最后更新：2026-06-27

适用范围：GFS/IFS 下载、node-27 active DB/ingest/display、node-22
Slurm/SHUD compute、共享 NFS object-store/published 数据面。

## 1. 结论

下载迁到 node-27 后，目标拓扑不应是“22 继续调度，27 补入库”，而应是：

```text
node-27
  -> source discovery/download for GFS/IFS
  -> raw/canonical source-cycle DB state in node-27 PostgreSQL :55432
  -> node27_autopipeline ingest/import/parse/publish/display readiness

node-22
  -> Slurm Gateway + SHUD/forcing compute worker only
  -> no active NHMS DATABASE_URL
  -> artifact/receipt writes to shared NFS object-store
```

这样处理最彻底。否则迁下载后仍会保留两份 source-cycle / job state：
node-22 历史 DB 一份，node-27 active DB 一份，后续每次排障都可能重新遇到
“public display 看到的状态”和“22 调度器看到的状态”不一致。

## 2. 当前事实

- node-27 是当前 active PostgreSQL、ingest、display API 和 public frontend
  host；display API 使用 `127.0.0.1:55432/nhms`。
- node-27 `scripts/node27_autopipeline.py` 已经能扫描
  `/home/ghdc/nwm/object-store/runs`，注册 run、应用 object-store forcing
  handoff、解析输出并刷新 display coverage。
- node-22 当前仍有历史 PostgreSQL `:55433`，且现场
  `infra/env/compute.host.env` 仍让 compute scheduler 使用该 DB。
- `nhms-gfs download` 与 `nhms-ifs download` 是普通 CLI。它们依赖
  `DATABASE_URL`、`OBJECT_STORE_ROOT`、`WORKSPACE_ROOT`、下载网络和
  `cdo/eccodes` 等 GRIB 依赖，不天然依赖 Slurm。
- node-22 的 `SLURM_GATEWAY_EXCLUDE_NODES=cn24` 是 Slurm 下载/计算节点规避；
  下载迁到 node-27 后，该规避只应继续服务 22 上的 compute job。

## 3. 目标

- GFS/IFS source discovery 和 download 在 node-27 上运行，并只写 node-27
  active DB 与 node-27 视角的 object-store。
- node-27 以同一份 source-cycle/run identity 驱动后续 ingest/display。
- node-22 Slurm/SHUD job 只消费显式 artifact/input，产出 object-store
  package 和 receipt，不连接 active 或历史业务 DB。
- node-22 历史 PostgreSQL `:55433` 在新路径通过观察窗口后停止并保留归档。
- runbook、env template、static guard 能阻止重新引入 node-22 active DB writer。

## 4. 非目标

- 不在本迁移内扩大 frontend/display 功能。
- 不把 display API 变成 writer；node-27 data-plane ingest/download 使用独立
  writer role，display 继续 readonly。
- 不在没有观察窗口的情况下删除 node-22 历史 DB 数据目录。
- 不把 return-period degraded quality 当成本迁移阻塞项；它是独立数据质量问题。

## 5. 阶段计划

### Phase 0: 现场冻结与证据快照

先捕获当前状态，避免迁移中误判：

- node-22 scheduler、Slurm Gateway、compute API、历史 PG `:55433` 进程和端口。
- node-22 当前 env 中所有 `DATABASE_URL`、`WORKSPACE_ROOT`、
  `OBJECT_STORE_ROOT` 的脱敏形态。
- node-27 ingest env、display env、cron、active DB、public latest-product 状态。
- 共享 NFS 上最近 GFS/IFS raw、run、published artifact identity。

完成标准：有一份不含 secret 的 receipt，可以证明迁移前 22 DB 仍被谁使用，
27 public API 当前展示哪个 cycle。

### Phase 1: node-27 下载预检与 bounded runner

先让 27 具备“能独立下载”的最小完整能力：

- 新增 node-27 download env template，和 display/ingest env 分离。
- 新增 bounded download wrapper/runner，按 source/cycle 执行
  `nhms-gfs download` / `nhms-ifs download`。
- wrapper 启动前检查 `DATABASE_URL` 必须是 node-27 writer、DB host/port
  必须是 `127.0.0.1:55432` 或明确允许的 node-27 endpoint。
- 检查 `OBJECT_STORE_ROOT`、`WORKSPACE_ROOT`、GRIB toolchain、下载 bbox、
  cycle-hours、日志根和锁。
- 运行结果输出 credential-safe JSON summary，并记录 manifest URI、files、
  bytes、retry_count、status、source/cycle。

完成标准：在 node-27 对一个已存在或安全测试 cycle 运行下载 wrapper，
不触碰 node-22 DB，能写入/验证 node-27 DB 的 `met.forecast_cycle` 与
object-store raw manifest。

### Phase 2: 27 下载成为生产 source-cycle owner

把 download 从“可运行”变成“生产 source-cycle 真相源”：

- 27 定时任务或 autopipeline 前置阶段负责选择允许的 UTC `00,12` cycle。
- 27 download 对 GFS/IFS 使用幂等锁，避免 cron 重入和重复下载。
- source-cycle 状态只写 node-27 DB。
- 22 scheduler 中的 `download_source_cycle` 不再作为生产链第一阶段。
- current-production runbook 改成 node-27 下载、node-27 ingest、node-22 compute。

完成标准：一个新 GFS cycle 和一个新 IFS cycle 的 raw download 均由 node-27
产生，public latest-product 最终推进到同一 cycle，证据中没有 node-22 DB 读写。

### Phase 3: node-22 compute job DB-free 化

把 22 从“调度器带 DB”收缩成“计算执行器”：

- sbatch template 不再渲染或继承业务 `DATABASE_URL`。
- forcing/SHUD/parse/publish 中必须写 DB 的步骤移到 27，或改为写
  object-store package/receipt 后由 27 ingest apply。
- Slurm Gateway 仍可保留在 22，但请求 payload 必须携带显式 artifact
  identity、workspace、object-store URI 和 receipt path。
- `services/orchestrator` 中和 Slurm execution 绑定的 preflight 不再要求
  compute-node reachable `DATABASE_URL`，而是要求 DB-free compute contract。

完成标准：22 上跑一次 compute/SHUD job，进程环境和 sbatch 文本都不含
业务 `DATABASE_URL`，产物由 NFS/object-store 被 node-27 ingest 接收。

### Phase 4: 27 统一编排，22 只提供 Slurm capability

在 27 上统一 source-cycle、job lifecycle 和 display readiness：

- 27 控制面持有 pipeline job state。
- 27 通过 22 Slurm Gateway 提交 compute job，而不是让 22 本地 scheduler
  查询自己的 DB 决策。
- 22 返回 Slurm job id、state、stdout/stderr path、artifact receipt。
- 27 根据 receipt 更新 node-27 DB，并继续 autopipeline parse/publish。

完成标准：停掉 22 scheduler 后，27 仍能推动一个完整 GFS/IFS cycle
从 download 到 public display readiness；22 只表现为 Slurm execution oracle。

### Phase 5: 22 历史 PostgreSQL 退役

最后处理历史 DB，而不是第一步直接删除：

- 对 node-22 `:55433` 做只读归档或 dump，记录路径和校验。
- 保留一个短观察窗口，至少通过两个完整业务 cycle：GFS 和 IFS 各至少一次。
- 从 node-22 runtime env 移除 `DATABASE_URL`。
- 停止 PostgreSQL 容器/服务，确认 `ss -ltnp | grep 55433` 为空。
- 增加 static guard：compute role 或 node-22 active runbook 重新出现
  `10.0.2.100:55433` / active `DATABASE_URL` 时失败。

完成标准：22 `:55433` 不再监听，node-27 latest-product 正常推进，
current docs/env/tests 都不再把 22 DB 当生产依赖。

## 6. 验收证据

- node-27 live：
  `latest-product?source=GFS&identity_only=true` 和
  `latest-product?source=IFS&identity_only=true` 均推进到新下载 cycle。
- node-27 DB：
  `met.forecast_cycle`、`hydro.hydro_run`、display readiness 与 public API
  使用同一 source/cycle/run identity。
- node-22 live：
  `pgrep` 不显示 production scheduler，Slurm Gateway 正常；退役阶段后
  `:55433` 不监听。
- env/secret：
  22 compute env 和 sbatch 文本不含业务 `DATABASE_URL`；27 download/ingest
  receipt 不泄漏 credential。
- object-store：
  27 下载产生的 raw manifest 与后续 run artifact 都位于共享 NFS 的 canonical
  object-store root 下。
- regression：
  focused pytest、ruff、OpenSpec validate、topology static guard 全部通过。

## 7. 风险与缓解

- node-27 缺少 `cdo/eccodes` 或网络出口不同。Phase 1 先做 preflight 和
  单 cycle live proof，不直接切生产。
- 下载 IO/CPU 影响 display/API。runner 必须 bounded、带锁、限并发，日志写
  node-27 本地 evidence root。
- 22 scheduler 当前可能仍有在跑 job。Phase 0/2 只在队列清楚或可回滚时切换。
- 旧 run 仍可能缺 object-store forcing handoff。保持显式 transitional mirror
  的 sunset 语义，但不得隐式连接 22 DB。
- 直接停 22 DB 会让当前 scheduler 立即失去状态。必须先迁控制面，再退役 DB。

## 8. 回滚

- Phase 1/2 失败：停用 node-27 download cron/wrapper，恢复 22 当前下载路径。
- Phase 3/4 失败：恢复 22 scheduler env 备份，保留 22 PG 归档和服务，重新
  启动原 scheduler；不得恢复 display-env writer 或隐式 mirror fallback。
- Phase 5 失败：从归档恢复 22 PG 只作为临时 emergency path，同时记录退役
  阻塞原因；恢复窗口不得改变 node-27 active DB 是生产真相源的目标。

## 9. 建议 issue 切分

1. Node-27 download preflight and bounded runner。
2. Node-27 GFS/IFS production download ownership and cron integration。
3. Disable node-22 production download stage and route source-cycle truth to 27。
4. Make node-22 Slurm/SHUD compute jobs DB-free artifact producers。
5. Node-27 orchestration of 22 Slurm Gateway compute submissions。
6. Retire node-22 historical PostgreSQL with archive, guardrails, and live receipts。

