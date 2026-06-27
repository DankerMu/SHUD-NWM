# Node-27 Download And Node-22 NFS Scheduler Handoff Plan

最后更新：2026-06-27

适用范围：GFS/IFS 下载、node-27 active DB/ingest/display、node-22
scheduler/Slurm/SHUD、共享 NFS object-store/published 数据面。

## 1. 结论

当前目标不是把 scheduler 搬到 node-27。目标拓扑是：

```text
node-27
  -> source discovery/download for GFS/IFS
  -> raw manifest and raw bundles under shared NFS object-store
  -> active PostgreSQL :55432 source-cycle evidence
  -> ingest/import/parse/publish/display readiness

node-22
  -> production scheduler/control point remains here
  -> checks shared NFS raw manifest produced by node-27
  -> stages that cycle's raw files into compute-visible /scratch object-store
  -> starts downstream cycle from convert when raw is ready
  -> does not fall back to production download when NFS raw is required
```

这样处理能解决当前最急的拆分点：下载和数据面归 node-27，调度和 Slurm/SHUD
仍归 node-22，中间用共享 NFS 上的 raw manifest 交接。由于 Slurm compute
nodes 不能假定可读 `/ghdc/data/nwm`，node-22 scheduler/control node 在提交
`convert` 前必须把该 cycle raw 物化到 compute-visible `OBJECT_STORE_ROOT`
（当前 `/scratch/frd_muziyao/nhms-prod/object-store`）。node-22 本地 PostgreSQL
`:55433` 仍是历史负担，但退役它需要先替换 scheduler lock/job state，不并入
这次下载迁移切片。

## 2. 当前事实

- node-27 是当前 active PostgreSQL、ingest、display API 和 public frontend
  host；display API 使用 `127.0.0.1:55432/nhms`。
- node-27 已能把 GFS/IFS `2026-06-26T12:00:00Z` raw 下载到共享 NFS
  object-store，并写入 node-27 `met.forecast_cycle`。
- node-22 登录/控制节点能看到同一份 NFS raw manifest；但 Slurm compute node
  快检显示 `/ghdc/data/nwm/object-store/...` 为 `NFS_READ_FAIL`，
  `/scratch/frd_muziyao/nhms-prod/object-store/...` 为 `SCRATCH_READ_OK`。
- node-22 仍运行 production scheduler/Slurm Gateway/SHUD runtime；它应该继续
  负责启动 cycle。
- node-22 本地 `:55433` 仍服务 scheduler lock/job state。2026-06-27 live
  evidence 显示 `DATABASE_URL` 指向 `10.0.2.100:55433/nhms` 且
  `NHMS_SCHEDULER_LOCK_BACKEND=postgres`，scheduler pass 会打开 PostgreSQL
  连接。它不能再作为 GFS/IFS 生产下载真相源，但当前不能直接停掉。

## 3. 目标

- GFS/IFS source discovery 和 download 在 node-27 上运行，并只写 node-27
  active DB 与共享 NFS object-store。
- node-27 每个成功下载的 cycle 都留下可被 node-22 验证的
  `raw/<source>/<cycle>/manifest.json`。
- node-22 scheduler 在候选 cycle 上检查 NFS raw manifest；manifest 缺失、
  非法、source/cycle 不匹配或引用文件缺失时阻断该 cycle。
- node-22 scheduler 在提交 Slurm 前把 NFS raw manifest 及其引用文件 staged
  到 compute-visible `OBJECT_STORE_ROOT`，manifest 最后落盘。
- raw 已 ready 且 canonical 产品缺失时，node-22 scheduler 从 `convert` 开始
  后续链路，禁止提交 `download_source_cycle`。
- node-22 active forecast Slurm job mapping 不再暴露 `download_source_cycle`。
- node-27 对共享 NFS raw source bundles 运行独立 production retention，
  保留 JSON evidence 和安全路径约束，不再暴露 dry-run 开关。
- runbook、env template 和 OpenSpec 明确：27 下载，22 调度，NFS 交接。

## 4. 非目标

- 不在本迁移内扩大 frontend/display 功能。
- 不把 display API 变成 writer；node-27 data-plane ingest/download 使用独立
  writer role，display 继续 readonly。
- 不把 scheduler/control plane 迁到 node-27。
- 不在没有 scheduler-state 替代方案的情况下停止 node-22 `:55433`。
- 不把 return-period degraded quality 当成本迁移阻塞项；它是独立数据质量问题。

## 5. 阶段计划

### Phase 0: 现场冻结与证据快照

捕获 node-22 scheduler、Slurm Gateway、compute API、历史 PG `:55433`、当前
Slurm 队列，以及 node-27 active DB、ingest/display env、public latest-product
状态。完成标准：有不含 secret 的 receipt 说明迁移前谁在跑、public 当前展示
哪个 cycle、NFS/object-store 根在哪里。

### Phase 1: node-27 下载预检与 bounded runner

新增 node-27 download env template 和 bounded wrapper，启动前检查 writer
`DATABASE_URL`、`OBJECT_STORE_ROOT`、`WORKSPACE_ROOT`、GRIB toolchain、bbox、
cycle-hours、锁和日志根。完成标准：node-27 对 GFS/IFS 安全 cycle 成功写
raw manifest、raw files 和 node-27 DB source-cycle evidence。

### Phase 2: node-22 NFS raw manifest bridge

在 scheduler 候选状态中读取共享 NFS `raw/<source>/<cycle>/manifest.json`，
验证 source/cycle/URI/entry/file 完整性。完成标准：无 node-22 本地
`met.forecast_cycle` 行时，scheduler 仍能从 NFS manifest 构造 raw-ready
候选状态。

### Phase 3: 禁止 node-22 下载兜底

启用 `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` 后，缺失或非法 manifest
直接 block candidate；ready manifest + canonical zero rows 则先 staged raw 到
compute-visible object-store，再从 `convert` 启动。完成标准：focused tests
证明 scheduler 不提交 `download_source_cycle`，node-22 runtime env 指向共享
NFS object-store 和 `/scratch` staging root。

### Phase 3b: 物理移除 node-22 生产下载入口

active forecast stage catalog 从 `convert` 开始，Slurm gateway 默认 job type
mapping 移除 `download_source_cycle`，`infra/sbatch/download_source_cycle.sbatch`
删除。完成标准：fresh zero-canonical 但缺 node-27 raw manifest 的候选被 block，
而不是提交 full-chain/download。

### Phase 4: 生产观察窗口

node-27 cron/autopipeline 负责选择允许的 UTC `00,12` cycle 并下载；node-22
scheduler 消费 NFS raw manifest 启动后续链路。完成标准：GFS 和 IFS 至少各
一次 live cycle 从 node-27 raw download 推进到 public latest-product，中间
没有 node-22 production download。

### Phase 4b: node-27 raw NFS retention

node-27 对 `/home/ghdc/nwm/object-store/raw/<source>/<cycle>` 运行独立定时
retention。只清理 GFS/IFS raw source bundles，不清理 canonical、forcing、
runs、published 或 static assets。完成标准：systemd timer enabled，live
summary JSON 记录 production execute、planned/deleted/skipped/failed。

### Phase 5: 后续 node-22 DB 治理

另起 change 设计 scheduler lock/job state 替代方案，再决定是否退役
node-22 `:55433`。完成标准：归档/dump、checksum、rollback、两类 live cycle
证据和 static guard 齐全后再停服务。

## 6. 验收证据

- node-27 live：GFS/IFS raw manifest 和 raw files 位于共享 NFS object-store。
- node-27 DB：`met.forecast_cycle` 使用 active PostgreSQL `:55432`。
- node-22 scheduler：候选 evidence 含 `nfs_raw_manifest`，ready 时
  `restart_stage=convert`，提交前 `model_run_evidence` 含
  `nfs_raw_manifest_staging`，missing required raw 时 block。
- node-22 Slurm gateway：默认 job type mapping 不含 `download_source_cycle`。
- node-27 retention：raw NFS cleanup summary 证明只扫描 raw source cycle
  目录，生产执行并保留 evidence。
- public display：latest-product 最终推进到 node-27 下载的 source/cycle。
- env/secret：22 runtime 配置只暴露脱敏 evidence，不泄漏 credential。
- regression：focused pytest、ruff、OpenSpec validate 全部通过。

## 7. 风险与缓解

- node-27 缺少 `cdo/eccodes` 或网络出口不同。用 preflight 和单 cycle live
  proof 先验，不直接切生产。
- 下载 IO/CPU 影响 display/API。runner 必须 bounded、带锁、限并发，日志写
  node-27 evidence root。
- NFS gate 早于 27 cron 稳定启用会让 22 block。这个行为是故意的，优先
  阻止 22 静默兜底下载。
- compute node 不能读 `/ghdc`。scheduler/control node 必须先 staging 到
  `/scratch`，并且 manifest 最后复制，避免 convert 看到半成品 raw。
- 22 scheduler 当前可能仍有在跑 job。启用前先看 Slurm 队列和 scheduler
  状态，不杀正在运行的 compute job。

## 8. 回滚

- Phase 1/2 失败：停用 node-27 download wrapper/cron；恢复 22 下载必须走
  单独变更和显式回滚审批，不再作为默认 fallback。
- Phase 3/4 失败：显式关闭 `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST`，记录
  失败原因和恢复窗口；不得把 22 下载重新写成目标架构。
- Phase 5 失败：只把 node-22 PG 作为临时 emergency path 恢复，同时记录
  退役阻塞原因。
