# QHH GFS/IFS 持续多周期自动运行

最后更新：2026-05-21

## 目标

在本机无 Docker、系统盘空间有限的约束下，以 `data/Basins/qhh` 已校准 SHUD 模型为固定模型资产，持续执行真实后端链路：

1. Basins discovery、package publish、registry import。
2. qhh 原始 386 个 forcing 站点与 SHUD output river identity 幂等 seed。
3. GFS 或 IFS 下载。
4. canonical 转换。
5. qhh forcing 生产，保持 SHUD 标准多站点 forcing 布局。
6. 使用仓库内 `SHUD/shud` 运行模型。
7. output parse、QC、结果摘要、return-period display product 发布。

该入口不是简化 smoke：它会运行 native SHUD，并把结果写入 API/frontend 可消费的数据表。默认不会 reset DB，也不会删除已完成周期。

## 入口

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

Slurm 计算节点执行，一次扫描。`DATABASE_URL` 必须指向计算节点可达的生产或集群 PostgreSQL；不要把默认本地开发库暴露到集群网络：

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
|---|---:|---|
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

`scripts/local_pg.sh` 会刷新 `postgresql.conf` 和 `pg_hba.conf`，并把 URL 写入 `.pgdata/qhh-smoke.database-url`。应用角色是非 superuser；生产 Slurm 仍应优先使用正式 PostgreSQL endpoint。

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
- `unavailable`：数据源暂不可用，IFS CLI 会在 cycle 尚未发布时返回该状态。
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
- IFS canonical 同时保留 `net_radiation` 与 `shortwave_down`；qhh forcing 的 `Rn` 使用 `shortwave_down`，避免把可能为负的净辐射写入 SHUD forcing。
- manifest scenario：
  - GFS：`forecast_gfs_deterministic`
  - IFS：`forecast_ifs_deterministic`

## 2026-05-21 实测结果

已按标准链路完成 GFS 与 IFS 两个起报周期：

| Source | Cycle UTC | Run ID | 状态 | 执行位置 |
|---|---:|---|---|---|
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

- 连续 runner 目前按最近 UTC `00/06/12/18` 候选周期扫描，并依赖 GFS/IFS adapter 自身判断可用性。
- 重计算应默认使用 Slurm；本地入口只适合调试短链路、定位 adapter/DB 合同问题或复用已完成状态。
- 本轮优化前 forcing/GRIB 处理是当前资源瓶颈，单周期峰值约 75-79GB RSS。已完成第一阶段 streaming 优化：forcing producer 先读取每个 source/grid 的代表网格定义生成 IDW 权重，再按 valid_time 逐时次读取 canonical field、只保留 IDW 权重需要的 grid cell 值、插值后释放字段缓存，避免把全周期所有变量/lead hour 同时挂在内存里。下一步仍需在新周期 Slurm 实测优化后 MaxRSS，并继续评估更细的 lead-hour array 化。
- 当前 continuous runner 已能提交独立 Slurm 作业；正式 orchestrator/integration lane 后续应复用同样的 qhh manifest、forcing station、display publish 规则，并补齐 array 化、失败重试和长日志归档。
- qhh 仍没有 flood frequency curve，发布结果继续使用 `no_frequency_curve` 质量标记，不生成虚假重现期或预警等级。
