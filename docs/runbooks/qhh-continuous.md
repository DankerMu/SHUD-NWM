# QHH GFS/IFS 持续多周期自动运行

最后更新：2026-05-26

## 生产调度边界

`scripts/run_qhh_cycle.sh`、`scripts/run_qhh_cycle.sbatch` 和
`scripts/run_qhh_continuous.py` 是 qhh 专用的诊断、回归复现和证据采集入口。
它们用于证明 `data/Basins/qhh` 标准链路可以完成 GFS/IFS 下载、canonical
转换、forcing、native SHUD、parse 和 display product 发布，但不是 backend
production scheduler 的依赖。诊断入口、直接 helper 依赖、out-of-chain helper
和静态 guard 统一记录在 [`../../scripts/diagnostic/qhh/README.md`](../../scripts/diagnostic/qhh/README.md)。

生产多流域连续调度入口在 backend orchestrator：

```bash
export DATABASE_URL=postgresql://nhms:nhms_dev@localhost:5432/nhms
uv run nhms-pipeline plan-production \
  --dry-run \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root .nhms-workspace
```

`nhms-pipeline plan-production --dry-run` 只做候选发现、跳过/阻塞判断和证据写入。
输出包含 `pass_id`、`artifact_path`、`counts`、`source_cycles`、
`candidates`、`blocked_candidates`、`skipped_candidates`、`operator_filters` 和
`no_mutation_proof`。dry-run 明确保证：

- no download：`adapter_download_called=false`，不调用 GFS/IFS adapter 下载。
- no Slurm submit：`slurm_submit_called=false`，不提交 Slurm job。
- no Slurm status sync/cancel：`slurm_status_sync_called=false` 且
  `slurm_cancellation_called=false`，不执行 Slurm 状态同步或取消。
- no SHUD run：`shud_runtime_called=false`，不运行 native SHUD。
- no hydro/met result mutation：`hydro_result_table_writes=false` 且
  `met_result_table_writes=false`，不写 `hydro.*` 或 `met.*` 结果表。
- no pipeline state mutation：`pipeline_status_writes=false` 且
  `pipeline_event_writes=false`，不写 pipeline status/event。

`--plan` 只是 `--dry-run` 的规划别名，只保留给 dry-run/no-mutation smoke 或
业务验证证据；真实生产提交必须使用 `--submit`。

生产提交路径使用同一个 backend scheduler，在 Slurm/database/storage preflight
通过后用 `--submit` 关闭 dry-run。一次性生产提交示例：

```bash
export DATABASE_URL=postgresql://nhms:<strong-password>@pg.cluster.example:5432/nhms
export NHMS_PRODUCTION_SLURM_ENABLED=1
export WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-production
export OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-production/object-store
export SLURM_SHARED_LOG_ROOT=/scratch/frd_muziyao/nhms-production/slurm-logs
export NHMS_RUNTIME_ROOT=/scratch/frd_muziyao/nhms-production/runtime

uv run nhms-pipeline plan-production \
  --submit \
  --source gfs \
  --source IFS \
  --lookback-hours 24 \
  --cycle-lag-hours 6 \
  --max-cycles-per-source 1 \
  --workspace-root "$WORKSPACE_ROOT"
```

该生产路径负责所有 active runnable 注册模型的候选生成、锁、幂等跳过、
Slurm preflight、提交、状态/重试/取消证据和 readiness 输入。qhh 脚本产生的
证据可作为诊断或复现材料，不应写成生产 scheduler dependency，也不应作为最终
production readiness proof。

连续生产守护进程同样必须显式带 `--submit`；不带 `--submit` 的
`--continuous` 仍是 dry-run/no-mutation 轮询：

```bash
uv run nhms-pipeline plan-production \
  --continuous \
  --submit \
  --source gfs \
  --source IFS \
  --interval-seconds "$NHMS_SCHEDULER_INTERVAL_SECONDS" \
  --max-passes "$NHMS_SCHEDULER_MAX_PASSES" \
  --workspace-root "$WORKSPACE_ROOT"
```

### Timing evidence & stdout volume

生产 scheduler 每 pass 的 evidence JSON 顶层新增 `timing:` 块，包含 `pass`（总 wall / CPU / python-time / slurm-wait / status，恒定写入）、`stages`（每 stage 的 `dispatch_ms` 与直接测量的 `slurm_wait_ms`）、`candidates`（每 basin 的 sub-phase 分解，仅在 `NHMS_SCHEDULER_TIMING_LEVEL=candidate` 时出现）与 `restart_reconcile`（`sacct` 子进程 wall/CPU 拆分，`stage` 及以上级别恒定出现）。`NHMS_SCHEDULER_TIMING_LEVEL` 按 `pass ≤ stage ≤ candidate` 三级门控 stdout 数据量：`pass` 级只发 pass 边界的单行 JSON，`stage` 级追加每 stage 起止的单行 JSON，`candidate` 级不额外写 stdout —— candidate 记录只落 evidence，永不进入 journald。journald 由 `nhms-compute-scheduler.service` 自动捕获 stdout，用 `journalctl --user -u nhms-compute-scheduler.service -f | jq -c 'select(.phase|test("^pass:|^stage:"))'` 即可跟随实时 phase transition，而不必尾随 NFS 上的 evidence artefact。

## #214 evidence boundary

Issue #214 的 MVP smoke/evidence 索引见 [`qhh-mvp-smoke-evidence.md`](qhh-mvp-smoke-evidence.md)。本文中的 GFS/IFS `2026052100`、`2026052106` 结果属于 qhh diagnostic/reproduction evidence；它们证明记录周期可以完成 qhh 脚本链路，但不能替代 `nhms-pipeline plan-production` 的 formal scheduler evidence，也不能替代 target-env final production readiness。

IFS 06/18 UTC shorter-horizon 行为在 #214 中通过 deterministic `/hydro-met` browser smoke 标注 144h actual horizon；未在本 PR 执行新的 live IFS 18Z download/SHUD run。任何新 live IFS claim 必须新增 source/cycle/run/artifact receipt，并在 #214 evidence matrix 中独立标注。

## 目标

在本机无 Docker、系统盘空间有限的约束下，以 `data/Basins/qhh` 已校准 SHUD
模型为固定模型资产，复现真实 qhh 后端链路：

1. Basins discovery、package publish、registry import。
2. qhh 原始 386 个 forcing 站点与 SHUD output river identity 幂等 seed。
3. GFS 或 IFS 下载。
4. canonical 转换。
5. qhh forcing 生产，保持 SHUD 标准多站点 forcing 布局。
6. 使用仓库内 `SHUD/shud` 运行模型。
7. output parse、QC、结果摘要、return-period display product 发布。

该诊断入口不是简化 smoke：非 dry-run 时会运行 native SHUD，并把结果写入
API/frontend 可消费的数据表。默认不会 reset DB，也不会删除已完成周期。用于生产
多流域调度时，应优先使用上面的 backend scheduler 路径。

## 入口

以下入口均为 qhh 诊断/复现入口，不是 backend production scheduler 依赖。

单周期真实链路：

```bash
export DATABASE_URL=$(./scripts/local_pg.sh url)
export SHUD_TIMEOUT_SECONDS=1800
./scripts/run_qhh_cycle.sh gfs 2026052100
./scripts/run_qhh_cycle.sh IFS 2026052100
```

持续多周期调度，一次扫描：

```bash
export DATABASE_URL=$(./scripts/local_pg.sh url)
uv run python scripts/run_qhh_continuous.py --once
```

qhh 诊断脚本通过 Slurm 计算节点执行，一次扫描。`DATABASE_URL` 必须指向计算节点可达的生产或集群 PostgreSQL；不要把默认本地开发库暴露到集群网络：

```bash
export DATABASE_URL="postgresql://nhms:<strong-password>@pg.cluster.example:5432/nhms"
export QHH_RUN_ROOT="$PWD/.nhms-runs/qhh-continuous"
export OBJECT_STORE_ROOT="$PWD/.nhms-runs/qhh-continuous"
export OBJECT_STORE_PREFIX="s3://nhms"
export SHUD_EXECUTABLE="$PWD/SHUD/shud"
export SHUD_TIMEOUT_SECONDS=3600
export QHH_CONTINUOUS_SOURCES="gfs,IFS"
export QHH_CONTINUOUS_LOOKBACK_HOURS=24
export QHH_CONTINUOUS_MAX_CYCLES_PER_SOURCE=2
export QHH_CONTINUOUS_CYCLE_LAG_HOURS=6
export QHH_GFS_FORECAST_START_HOUR=3
export QHH_GFS_FORECAST_END_HOUR=168
export QHH_IFS_FORECAST_START_HOUR=3
export QHH_IFS_FORECAST_END_HOUR=144
export QHH_FORCING_MIN_LEAD_HOURS=3
export QHH_MAX_LEAD_HOURS=144
export QHH_CONTINUOUS_EXECUTOR=slurm
export QHH_SLURM_PARTITION=CPU
export QHH_SLURM_CPUS=8
export QHH_SLURM_MEM=128G
export QHH_SLURM_TIME=08:00:00
uv run python scripts/run_qhh_continuous.py --once --executor slurm
```

`--executor slurm` 会通过 `scripts/run_qhh_cycle.sbatch` 为每个 source/cycle
提交一个独立作业，并等待 `sacct` 返回结果。作业执行完整
`scripts/run_qhh_cycle.sh`，因此下载、canonical、forcing、
`nhms-shud-runtime execute`、parse 和 display product 发布都在 compute node
内完成。该模式要求 `DATABASE_URL` 不能指向 `localhost`，否则 compute node
无法写回 `met.*`、`hydro.*` 和展示产品表，runner 会在提交前拒绝执行。
前台 runner 取消时会对已提交作业发起 `scancel`；等待 `sacct` 的阶段有超时边界，默认不会无限挂起。

常驻轮询：

```bash
export DATABASE_URL=$(./scripts/local_pg.sh url)
export QHH_CONTINUOUS_ONCE=0
export QHH_CONTINUOUS_POLL_SECONDS=1800
uv run python scripts/run_qhh_continuous.py
```

查看将要运行的 GFS/IFS 周期，不下载、不运行 SHUD：

```bash
uv run python scripts/run_qhh_continuous.py --dry-run --once
```

qhh 脚本 dry-run 输出每轮 JSON summary，核心字段为 `status`、
`pass_started_at`、`pass_finished_at`、`run_root`、`candidate_count` 和
`results[]`。`results[]` 中每个候选包含 `source_id`、`cycle_time`、`run_id`、
`status=planned` 和 `reason="dry run"`，或已有状态的 skip reason。该 qhh dry-run
不下载、不提交 Slurm、不运行 SHUD，也不写 hydro/met 结果表；它仍会写
`state/qhh-continuous-summary.json` 作为诊断计划摘要。生产 scheduler dry-run 的
正式 no-mutation evidence 见上方 `nhms-pipeline plan-production --dry-run`。

## 默认路径与关键环境变量

- run/object root：`.nhms-runs/qhh-continuous/`
- state root：`.nhms-runs/qhh-continuous/state/`
- lock file：`.nhms-runs/qhh-continuous/state/qhh-continuous.lock`
- local PostgreSQL：`./scripts/local_pg.sh`
- SHUD executable：`SHUD/shud`
- model：`basins_qhh_shud`
- package version：`v0.0.1-qhh-smoke-lake2`
- Slurm sbatch：`scripts/run_qhh_cycle.sbatch`
- Slurm logs：`.nhms-runs/qhh-continuous/slurm-logs/{source}/{cycle}/`
- ecCodes runtime：`.nhms-runs/qhh-continuous/eccodes-runtime/`

常用调度变量：

| 变量 | 默认 | 含义 |
| --- | ---: | --- |
| `QHH_CONTINUOUS_SOURCES` | `gfs,IFS` | 调度的数据源 |
| `QHH_CONTINUOUS_LOOKBACK_HOURS` | `48` | 向前扫描周期窗口 |
| `QHH_CONTINUOUS_MAX_CYCLES_PER_SOURCE` | `2` | 每个 source 每轮最多尝试的周期数 |
| `QHH_CONTINUOUS_CYCLE_LAG_HOURS` | `6` | 跳过过新的周期，给源数据发布时间 |
| `QHH_CONTINUOUS_RETRY_FAILED` | `1` | 下一轮是否重试 failed/unavailable 周期 |
| `QHH_GFS_FORECAST_START_HOUR` | `3` | GFS 起始 lead hour |
| `QHH_GFS_FORECAST_END_HOUR` | `168` | GFS 结束 lead hour |
| `QHH_MAX_LEAD_HOURS` | 空 | forcing 阶段可选截断 lead hour |
| `QHH_IFS_FORECAST_END_HOUR` | 空 | IFS 结束 lead hour 显式覆盖；未设置时使用 IFS 00/12=168、06/18=144 的源策略 |
| `QHH_CONTINUOUS_EXECUTOR` | `local` | 执行器，重计算应使用 `slurm` |
| `QHH_SLURM_PARTITION` | `CPU` | Slurm partition |
| `QHH_SLURM_CPUS` | `8` | 单周期作业 CPU |
| `QHH_SLURM_MEM` | `128G` | 单周期作业内存 |
| `QHH_SLURM_TIME` | `08:00:00` | 单周期作业 walltime |
| `QHH_SLURM_WAIT_TIMEOUT_SECONDS` | `43200` | Slurm 等待总上限 |
| `QHH_SLURM_ACCOUNTING_TIMEOUT_SECONDS` | `300` | 作业离开 `squeue` 后等待 `sacct` 出账的上限 |

多源下载源变量（适配器级，PR #308；两 lane 通用，控制下载镜像链与限流退避）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `GFS_SOURCE_BACKENDS` | `s3,gcs,azure,ftpprd,nomads` | GFS NODD 多镜像顺序；前四者共享 `.idx`+HTTP-Range+cdo-clip，NOMADS grib-filter 为末位回退 |
| `IFS_OPEN_DATA_FALLBACK_SOURCES` | `aws,azure,google,ecmwf` | IFS 云镜像优先顺序；ECMWF 直连有 500 连接上限，强制末位回退 |
| `IFS_SOURCE_COOLDOWN_SECONDS` | `1800` | 镜像被限流（503/429/SlowDown）后跳过该源的冷却时长 |

退避语义：NOMADS 403=动态封禁 → 持久断路器写 `OBJECT_STORE_ROOT/state/source_circuit/gfs_<source>.json`，cooldown 内停重试（`discover_cycles` 403 `retryable=False`）；云镜像 503/429/SlowDown 归类 `RateLimitedError` → 切下一源 + per-source cooldown；f000 缺累积/平均场（APCP/DSWRF）镜像返回 404 → 回落 NOMADS（不静默丢变量）。

本地 PostgreSQL helper 默认只允许 loopback 监听，适合单机调试：

```bash
./scripts/local_pg.sh restart
export DATABASE_URL=$(./scripts/local_pg.sh url)
```

如确需把 helper 暂时暴露给受控测试集群，必须显式确认并使用非默认密码：

```bash
export QHH_LOCAL_PG_ALLOW_REMOTE=1
export APP_PASSWORD="<non-default-strong-password>"
export PGLISTEN=10.0.2.100
export PGHOSTCIDR=10.0.2.0/24
./scripts/local_pg.sh restart
```

`scripts/local_pg.sh start/restart` 会刷新 `postgresql.conf` 和 `pg_hba.conf`，并把 URL 写入权限为 `0600` 的 `.pgdata/qhh-smoke.database-url`；普通日志只打印脱敏 URL。需要完整连接串时显式运行 `./scripts/local_pg.sh url`。应用角色是非 superuser；生产 Slurm 仍应优先使用正式 PostgreSQL endpoint。

compute node 若缺系统 ecCodes，使用项目内 runtime：

```text
.nhms-runs/qhh-continuous/eccodes-runtime/lib/
.nhms-runs/qhh-continuous/eccodes-runtime/share/eccodes/definitions/
```

`scripts/run_qhh_cycle.sbatch` 会自动设置 `LD_LIBRARY_PATH`、`ECCODES_DIR`、`ECCODES_DEFINITION_PATH`，并优先加载项目内 `libstdc++.so.6`，使 `cfgrib` 在 compute node 上可用。

## 状态与跳过规则

每个 source/cycle 写一个状态文件：

```text
.nhms-runs/qhh-continuous/state/cycles/gfs/2026052100.json
.nhms-runs/qhh-continuous/state/cycles/ifs/2026052100.json
```

状态语义：

- `running`：当前周期正在执行。
- `submitted`：Slurm 作业已提交，或 `squeue` / `sacct` 只能确认非终态、未知 accounting、等待超时等 skip-safe 状态；不会写 `finished_at`，后续扫描不会因为控制器或 accounting 暂不可见而重复提交。
- `unavailable`：数据源暂不可用，IFS CLI 会在 cycle 尚未发布时返回该状态。
- `probe_failed`：IFS 源周期探测因 compute node DNS、网络或 timeout 失败，CLI payload 会保留
  `reason=source_cycle_probe_failed`、`classifier=network_error`、`retryable=true` 和已脱敏的
  `attempted_sources`。attempted-source 证据会限制条目数和长字符串，并保留 total/omitted count，
  所以排障时先看 `attempted_source_count`、`omitted_attempt_count` 和已输出条目的
  `source`/`uri`/`error_class`/`error_message`。这不代表 AWS/Azure/Google/ECMWF 均未发布该 cycle，
  不应按源数据延迟处理。
- `rate_limited`：IFS 所有可用镜像当前被 429/503/SlowDown 等限流；保留
  `reason=source_cycle_rate_limited`、`classifier=rate_limited`、`retryable=true`，等待 cooldown
  或下一轮重试，不应改写成 source unavailable。
- `already_done`：数据库中同名 run 已是 `frequency_done` 或 `published`。
- `frequency_done`：完成 SHUD、parse 和 display product 发布。
- `failed`：单周期脚本非 0 退出。

run id 使用 orchestrator 同款规则：

```text
fcst_{source_lower}_{YYYYMMDDHH}_basins_qhh_shud
```

示例：

```text
fcst_gfs_2026052100_basins_qhh_shud
fcst_ifs_2026052100_basins_qhh_shud
```

forcing version id 使用 producer 规则：

```text
forc_{source_lower}_{YYYYMMDDHH}_basins_qhh_shud
```

## GFS 与 IFS 差异

- GFS 下载入口：`nhms-gfs download --source-id gfs --cycle-time ...`
- IFS 下载入口：`nhms-ifs download --cycle-time ...`
- IFS 若数据尚未发布，单周期脚本记录 `unavailable` 并停止下游，不伪造 raw/canonical/forcing。
- IFS 若在 node-22 等 compute node 上返回 `status=probe_failed`，先在同一节点检查 DNS 和出站网络：
  `getent hosts data.ecmwf.int`、`python - <<'PY'\nimport socket; print(socket.getaddrinfo('data.ecmwf.int', 443)[0])\nPY`、
  以及集群代理/防火墙设置；网络恢复后重新运行同一 `nhms-ifs download --cycle-time ...` 或等待下一轮 scheduler retry。
  不要把该状态改写成 `source_cycle_unavailable`，也不要手工写 `met.forecast_cycle` 为 unavailable。
- `scripts/run_qhh_cycle.sh` 会在 IFS CLI 返回 typed `probe_failed` 或 `rate_limited` JSON 后写入同名
  state file 并跳过下游；`scripts/run_qhh_continuous.py` 会保留该 typed state，不会把它覆盖成 generic
  `failed`。CLI 对外仍以非 0 表示下载被阻塞。
- IFS canonical 同时保留 `net_radiation` 与 `shortwave_down`；qhh forcing 的 `Rn` 使用 `shortwave_down`，避免把可能为负的净辐射写入 SHUD forcing。
- manifest scenario：
  - GFS：`forecast_gfs_deterministic`
  - IFS：`forecast_ifs_deterministic`

## 手动重试 wrong-root 恢复

旧版 shared source-cycle 手动重试可能缺失 `OBJECT_STORE_ROOT`，导致
`download_source_cycle` Slurm 作业把 raw bundle 写到 `WORKSPACE_ROOT` 下。排障时先识别
runtime-root 证据，不要直接改 `ops.pipeline_job` / `ops.pipeline_event` 或手工移动 DB 状态：

```sql
SELECT job_id, run_id, cycle_id, status, error_code, error_message
FROM ops.pipeline_job
WHERE job_type = 'download_source_cycle'
  AND manual_retry_marker IS TRUE
ORDER BY updated_at DESC
LIMIT 20;
```

如果同一 retry 的 submission event 中 `runtime_root_resolution.resolved.object_store_root.same_as_workspace=true`，
或 raw manifest/bundle 出现在 `$WORKSPACE_ROOT/raw/<SOURCE>/<YYYYMMDDHH>/` 而不是
`$OBJECT_STORE_ROOT/raw/<SOURCE>/<YYYYMMDDHH>/`，按 legacy wrong-root 处理。保留以下证据：

- retry job id、原始 failed job id、Slurm job id、`run_id`、`cycle_id`。
- `ops.pipeline_event.details.runtime_root_resolution` 的脱敏 JSON。
- workspace 下错误 raw bundle 的路径清单和 mtime；不要删除，直到 corrected retry 验证通过。

安全恢复步骤：

1. 在 node-22/operator shell 显式设置 split roots：

   ```bash
   export WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-production
   export OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-production/object-store
   export OBJECT_STORE_PREFIX=s3://nhms
   export NHMS_PUBLISHED_ARTIFACT_ROOT=/scratch/frd_muziyao/nhms-production/published
   export NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://
   ```

2. 确认 `$OBJECT_STORE_ROOT` 可写，且不是 `$WORKSPACE_ROOT`。如果当前 API 返回
   `RETRY_RUNTIME_ROOTS_UNRESOLVED` 或 `RETRY_RUNTIME_ROOTS_SECRET_BEARING`，先修正环境变量或原始
   submission evidence；不要通过 DB update 绕过 fail-closed guard。
3. 通过既有 retry API 对同一 `run_id` 重新发起手动重试。新的 retry submission event 应包含
   `runtime_root_resolution.resolved.object_store_root.value=$OBJECT_STORE_ROOT` 且
   `same_as_workspace=false`。
4. 只在 corrected retry 完成并确认 `$OBJECT_STORE_ROOT/raw/<SOURCE>/<YYYYMMDDHH>/manifest.json`
   存在后，再将旧 workspace-root raw bundle 作为证据归档或按保留策略清理。不要把旧 bundle
   复制到 object store 来制造成功状态。

## stale source-cycle stage evidence

如果 `download_source_cycle` 原始 job 仍是 `permanently_failed`，但后续手动 retry 已成功，且
`met.forecast_cycle.status=raw_complete`、`manifest_uri=raw/<SOURCE>/<YYYYMMDDHH>/manifest.json`，调度器应把旧失败
视作历史修复证据，而不是 active blocker。排障时先确认三类绑定：

```sql
SELECT job_id, run_id, cycle_id, job_type, stage, status, retry_count,
       manual_retry_marker, error_code, updated_at
FROM ops.pipeline_job
WHERE cycle_id = '<source>_<YYYYMMDDHH>'
  AND job_type = 'download_source_cycle'
ORDER BY updated_at DESC;

SELECT entity_id, event_type, details->>'previous_job_id' AS previous_job_id,
       details->>'manual_retry_marker' AS manual_retry_marker, created_at
FROM ops.pipeline_event
WHERE entity_type = 'pipeline_job'
  AND entity_id IN ('<retry_job_id>', '<failed_job_id>')
ORDER BY created_at DESC;

SELECT cycle_id, source_id, cycle_time, status, manifest_uri, error_code, error_message
FROM met.forecast_cycle
WHERE cycle_id = '<source>_<YYYYMMDDHH>';
```

只有“retry event 指向原始 failed job、retry job 自身 `status=succeeded`、forecast cycle 的 manifest URI
匹配同一 source/cycle”同时成立，旧失败才会在 candidate evidence 中出现
`repaired_stage_evidence.status=repaired`，且旧 job 行附带 `repair_status=repaired` /
`superseded_by_job_id=<retry_job_id>`。如果 API 仍显示 active blocker，优先检查：

- retry event 的 `previous_job_id` 是否指向原始 failed job，而不是无关 sibling job。
- retry job 是否仍是 `pending`、`failed`、`submission_failed` 或 `permanently_failed`。
- `met.forecast_cycle.manifest_uri` 是否缺失、指向错误 source/cycle，或 cycle status 未回到 raw-ready 状态。
- candidate evidence 是否被 row limit 截断；必要时提高只读诊断查询 limit，不要直接修改历史 job/event 行。

修复方式是通过既有 retry API 重新提交同一 `run_id`，让系统写入新的 retry job/event 和 manifest 证据；
不要把旧 failed job 改成 `succeeded`，也不要删除 `permanently_failed` 行。旧失败需要保留用于审计，成功 retry
负责 supersede 它。

## 2026-05-21 诊断实测结果

已通过 qhh 诊断/复现入口按标准链路完成 GFS 与 IFS 两个起报周期：

| Source | Cycle UTC | Run ID | 状态 | 执行位置 |
| --- | ---: | --- | --- | --- |
| GFS | 2026-05-21 00Z | `fcst_gfs_2026052100_basins_qhh_shud` | `frequency_done` | 本项目运行根，已作为 terminal 状态复用 |
| IFS | 2026-05-21 00Z | `fcst_ifs_2026052100_basins_qhh_shud` | `frequency_done` | 本项目运行根，已作为 terminal 状态复用 |
| GFS | 2026-05-21 06Z | `fcst_gfs_2026052106_basins_qhh_shud` | `frequency_done` | Slurm job `5743` |
| IFS | 2026-05-21 06Z | `fcst_ifs_2026052106_basins_qhh_shud` | `frequency_done` | Slurm job `5744` |

Slurm accounting：

```text
5743|qhh_gfs_2026052106|COMPLETED|0:0|00:29:51|
5743.batch|batch|COMPLETED|0:0|00:29:51|78712272K
5744|qhh_ifs_2026052106|COMPLETED|0:0|00:45:02|
5744.batch|batch|COMPLETED|0:0|00:45:02|74473176K
```

DB 验证：

- 4 个 `hydro.hydro_run` 均为 `frequency_done`。
- 4 个 `met.forcing_version` 均已写入，站点数均为 386：
  - `forc_gfs_2026052100_basins_qhh_shud`
  - `forc_gfs_2026052106_basins_qhh_shud`
  - `forc_ifs_2026052100_basins_qhh_shud`
  - `forc_ifs_2026052106_basins_qhh_shud`
- 连续 runner 汇总：`.nhms-runs/qhh-continuous/state/qhh-continuous-summary.json`，`candidate_count=4`，总状态 `completed`。

## 当前边界

- qhh continuous runner 仍按最近 UTC `00/06/12/18` 候选周期扫描，并依赖 GFS/IFS adapter 自身判断可用性；该行为只用于 qhh 诊断/复现。
- 重计算应默认使用 Slurm；本地入口只适合调试短链路、定位 adapter/DB 合同问题或复用已完成状态。
- 本轮优化前 forcing/GRIB 处理是当前资源瓶颈，单周期峰值约 75-79GB RSS。已完成第一阶段 streaming 优化：forcing producer 先读取每个 source/grid 的代表网格定义生成 IDW 权重，再按 valid_time 逐时次读取 canonical field、只保留 IDW 权重需要的 grid cell 值、插值后释放字段缓存，避免把全周期所有变量/lead hour 同时挂在内存里。下一步仍需在新周期 Slurm 实测优化后 MaxRSS，并继续评估更细的 lead-hour array 化。
- backend production scheduler 已承担正式多流域连续自动化路径：默认发现 active runnable 注册模型，按 GFS/IFS 周期生成确定性 candidate/run/forcing identity，记录 operator filters、skip/block reasons、Slurm preflight、array/task/accounting、retry/cancel 和 readiness 证据。qhh continuous runner 不应被服务端生产调度调用。
- qhh 仍没有 flood frequency curve，发布结果继续使用 `no_frequency_curve` 质量标记，不生成虚假重现期或预警等级。
