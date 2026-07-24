# QHH node-22 业务化运行流程与方法（首跑联调梳理）

> 本文记录在 node-22 上把 QHH 全链路从"E2E 测试架"推向"真实 Slurm 生产运行"过程中**实测验证出的正确执行模型、配置与方法**，作为后续正式业务化的依据。
>
> 状态标注：✅ 已实测验证 / ⏳ 待完成 / ⚠️ 已知待修。
>
> 最后更新：2026-06-04（GFS+IFS 双源跑通至 `frequency_done`；本文保留 node-22 首跑诊断记录，正式业务化已迁移至 m24 通用 daemon——见下方横幅）。
>
> ⚠️ **架构转向（m24）**：本 runbook 记录的是 `run_qhh_continuous.py → run_qhh_cycle.sh` **诊断/bring-up 路径**，m23 `design.md` 已**否决其作为生产自动化**。
> 正式"全持续守护"迁移到**通用编排器**（`services/orchestrator` scheduler/chain + Slurm HTTP gateway），由 **m24** 落地：多流域并发 + 跨周期暖启动承接 + 退役诊断脚本。
> OpenSpec：`openspec/changes/m24-multibasin-continuous-daemon-live/`；跟踪 epic **#285**（子任务 #286–#293）。本文继续作为诊断/排障与 m24 bring-up 期回退手册。
>
> ✅ **生产路径 = 通用守护进程（M24，权威）**：正式业务运行是通用 daemon
> `nhms-pipeline plan-production --continuous --submit`（→ `services/orchestrator/scheduler.py` `run_continuous`），
> 经**独立 Slurm gateway** 提交；多流域并发 + 两阶段预留 + 跨周期暖启动均在此路径。
> 不带 `--submit` 的 `plan-production` 默认 dry-run/no-mutation，即使同时带 `--continuous` 也不会作为生产提交 daemon。
> 本 runbook 的 `run_qhh_continuous.py → run_qhh_cycle.sh` 诊断 lane **仅作 bring-up 回退/排障**，
> **不**声称 production；M24 §5/#293 已加护栏测试
> （`tests/test_qhh_scripts_static.py::test_production_scheduler_does_not_invoke_qhh_diagnostic_scripts`）
> 静态断言生产 scheduler/chain 不引用这三个诊断脚本。
> #292 daemon live receipt：`artifacts/m24/m24-daemon-5880d09/`
> （`lease_nfs.json` 两进程心跳锁、`grib_preflight.json` GRIB 预检、`daemon_pass.json` 守护进程通过证据）；
> worklog `openspec/changes/m24-multibasin-continuous-daemon-live/issue-292-worklog.md`。
>
> ⚠️ **Post-#837 status (2026-06-29)**：本文中所有 node-22
> `10.0.2.100:55433` / `nhms-22-e2e-db` / `DATABASE_URL` 配置都是
> pre-#837 historical diagnostic evidence。node-22 `:55433` 现已
> archived/stopped，仅可在显式 rollback drill 中临时重启；当前生产与值守入口
> 见 `docs/runbooks/current-production-ops.md` 和 DB-free scheduler env。

---

## 0. 核心结论（TL;DR）

1. **执行面必须在宿主机（node-22 登录节点 `xnode`）跑，不能在 compute-api 容器里跑** —— 容器内没有 Slurm CLI
   （`sbatch`/`sinfo` not found），无法提交真作业；且 SHUD 生产 preflight 会 `os.stat`+`ldd`+跑 `--version`，
   需要二进制与 SUNDIALS 在执行处可见，宿主机才看得到 `/scratch` 与 `$HOME/sundials`。
2. **所有运行根必须在 `/scratch`** —— 计算节点（cn01-24）只挂 `/scratch`（NFS 10.0.2.99）、`/volume/data/nwm/Basins`、
   `/users/frd_muziyao/sundials`，**看不到 `/ghdc`**（那是 node-27 的 NFS）。把 workspace/object-store/run-root 放
   `/ghdc` 会导致作业 1 秒即死（连 sbatch 日志都写不出）。
3. **Pre-#837 historical DB note，不用于当前生产**：当时诊断 DB 必须用集群 IP，不能用容器名或
   `127.0.0.1`。历史值是 `10.0.2.100:55433`（`nhms-22-e2e-db` 容器发布端口），但该
   listener 现已 archived/stopped；当前流程不得把它当业务 DB。
4. **`OBJECT_STORE_ROOT` 必须等于 `QHH_RUN_ROOT`** —— QHH 包经 s3 前缀发布到 object-store，seed 步骤在 run-root 找；二者不一致会"package path unsafe / 找不到"。
5. **生产/业务运行器是 `nhms-pipeline plan-production --continuous --submit`**；它通过通用 orchestrator + standalone Slurm gateway 提交所有 active runnable 注册模型。`scripts/run_qhh_continuous.py --executor slurm` 只保留为 QHH 诊断/bring-up fallback；`--once` 仅做一次诊断扫描，去掉 `--once` 也不是生产 daemon。
6. **对象存储用生产前缀 `s3://nhms`**（`OBJECT_STORE_PREFIX`，文件系统对象存储的 URI 标签）——e2e 标签 `s3://nhms-22-e2e` 已废弃；`object_store.py` 会校验 URI 桶名==前缀,切前缀必须先清干净旧标签数据(否则旧 URI 解析报错)。
7. **SHUD 输出间隔 = 5min**（`QHH_MODEL_OUTPUT_INTERVAL`，代码默认已改 5）——洪频分析假设 hourly，3h 太粗；5min 对任意整数小时窗口可整除（7天=2004步）。窗口必须被输出间隔整除，否则 `INVALID_TIME_WINDOW`。
8. **⚠️ 运行纪律：作业运行中禁止在 node-22 `git pull`**——git 换 inode 会让正在逐行 exec `run_qhh_cycle.sh` 的 bash 句柄失效（`Stale file handle`），秒杀作业。要 pull 必须先等作业结束。代码更新与作业运行错开。

---

## 1. 执行拓扑

| 角色 | 位置 | 职责 | 可见文件系统 |
|------|------|------|------|
| 控制/调度面 | node-22 登录节点 `xnode`（10.0.2.100 / 10.0.1.100 / 210.77.77.22） | 生产：跑 `nhms-pipeline plan-production --continuous --submit` 并通过 standalone Slurm gateway 提交；诊断 fallback：手动跑 `run_qhh_continuous.py --executor slurm` | `/scratch`、`/ghdc`、`/volume`、`/users`、容器 |
| 计算执行面 | 计算节点 cn01-24（CPU 分区） | sbatch 内跑全链路（下载→canonical→forcing→SHUD→parse→publish） | `/scratch`、`/volume/data/nwm/Basins`、`/users/frd_muziyao/sundials` ❌ **无 `/ghdc`** |
| 历史诊断数据库（pre-#837，不用于当前生产） | 容器 `nhms-22-e2e-db`（timescaledb-ha pg15） | historical hydro/met/ops/flood schema（26 迁移） | archived/stopped rollback-only；历史端口为 55433 / 10.0.2.100:55433 |

> ⚠️ 首跑使用的是现有 **e2e DB 容器**（`nhms-22-e2e-db`）作为生产库的临时承载；正式业务化前需评估是否切换为独立生产 PostgreSQL。

---

## 2. 前置依赖（一次性准备）

### 2.1 SHUD 二进制 ✅

- 路径（QHH 约定）：`/scratch/frd_muziyao/NWM/SHUD/shud`
- 在**计算节点**重编以匹配运行环境（cn：Ubuntu 24.04 / g++ 13.3 / glibc 2.39）：

  ```bash
  cd /scratch/frd_muziyao  # Slurm 拦截器要求从 /scratch 提交
  srun -p CPU -n1 -c4 -t10 bash -lc \
    'cd /scratch/frd_muziyao/NWM/SHUD && make clean && SUNDIALS_DIR=$HOME/sundials make shud'
  ```

- 依赖 SUNDIALS/CVODE 6（已装 `$HOME/sundials`，即 `/users/frd_muziyao/sundials`，计算节点可见）；Makefile 经 `-Wl,-rpath,$HOME/sundials/lib` 把库路径写进二进制。
- 验证：`file shud`（应为 ELF x86-64）、`ldd ./shud | grep sundials`（应解析到 `$HOME/sundials/lib`）、`./shud`（应输出 `Success.`）。
- ⚠️ 注意：仓库内存在一套**未提交的 WIP**（solar/netcdf/timecontext，引用了 `Control_Data` 未添加的成员）会让 `make` 失败。首跑已将其隔离到 `SHUD/.wip-quarantine-20260604/`（13 文件，可恢复），用**干净 master** 编译。

### 2.2 GRIB 工具链 ✅

- conda env：`/scratch/frd_muziyao/nhms-grib`（cdo 2.6.1 + libeccodes + `share/eccodes/definitions` + libstdc++）。
- 用途：canonical 读 GRIB（cfgrib/eccodes）、IFS 下载裁剪（cdo）。在 QHH sbatch 里通过 `QHH_ECCODES_RUNTIME` 注入 `LD_LIBRARY_PATH`/`ECCODES_DEFINITION_PATH`；`bin/cdo` 加进 `PATH`。

### 2.3 数据库 schema ✅

```bash
set -a; . infra/env/compute.host.env; set +a   # 提供 DATABASE_URL
uv run python -m packages.common.migrate        # 全量迁移；已应用则全部 skip
```

> **部署顺序义务（m24 §3A / #290）**：迁移 `000029_pipeline_reservation`（`ops.pipeline_job` 预留列
> `idempotency_key`/`candidate_id` + 部分唯一索引 `pipeline_job_idempotency_key_uidx`）**必须在并发预留代码上线前、
> 且在 #292 连续守护进程 go-live 前 apply**。psycopg `reserve_pipeline_job` 引用这两列；列缺失则 reserve 抛
> `UndefinedColumn`（被 submit 路径吞为 `submission_failed`，可恢复但退化）。迁移纯 additive + 幂等
> （`ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`），可安全先于代码 apply。node-22 prod DB **尚未 apply 000029**。

### 2.4 Basins 数据 ✅ — 业务化唯一 source-of-truth

- 路径：`/volume/data/nwm/Basins/<流域>/input/<流域>/`（计算节点可见；仓库侧镜像 `data/Basins/<流域>/`）。**后续新增流域一律落此目录**。
- **硬约束**：业务化注册与运行的**所有参数必须从这套真实 SHUD 模型派生，严禁手配/即兴覆盖**。本轮 bring-up 的即兴参数（smoke 注册段数、`GFS_FORECAST_START_HOUR` 临时改值、partition 覆盖）属反面教材，不得带入生产。
- **两个 river 图层务必分清（产品键在 `.sp.riv`，不是 seg.shp）**：

  | 文件 | 含义 | qhh | heihe | 用途 |
  |---|---|---|---|---|
  | `<b>.sp.riv` | SHUD 河道路由 reach = **输出/产品层** | **1633** | **2352** | river discharge 输出、return_period、发布产品**按此键** |
  | `<b>.sp.rivseg` / `gis/seg.shp` | GIS 细分 river 段（几何） | 3738 | 4759 | river_network 几何/展示 |
  | `<b>.sp.att` | SHUD 计算单元 | 4773 | 6335 | 模型单元 |

  SHUD forecast 算/输出的是 `.sp.riv` reach 层（rivqdown.csv 列数 = `.sp.riv` count + 1）。**产品/输出校验必须对
  `.sp.riv` 段数（经 `qhh_production_bootstrap.read_qhh_output_segment_count` 读多块 `.sp.riv`），不能对 river_network
  几何段数（seg.shp 3738）**。诊断流正确分离了二者（注册 river_segment=3738 几何 + 单独 seed「SHUD 输出河段」1633，
  发布 1633 段，见 §6/§本节末）；通用编排器（`basins_registry_import`）原先只 seed seg.shp 几何（3738）、漏 `.sp.riv`
  输出层，forecast `verify_output` 拿输出（1634 列）对几何（3739）→ 误拒正确输出 → publish 阻断（#291 §3B live 暴露）。
  **已修（8cf7130）**：`basins_geometry` 暴露 `output_segment_count`、`basins_registry_import` seed
  `shud_output_river=true` 输出层（id `{model_id}_shud_riv_NNNNNN`）+ 记 `resource_profile.output_segment_count`
  （chain.py 透传 forecast manifest，`verify_output` 遂按 1633/2352 校验），使 **注册≡输出≡产品**；任何落入
  `data/Basins` 的新流域自动获得正确产品身份。

### 2.4.1 业务化运行 receipt（通用编排器，2026-06-05，#291）

- **双流域 published**：通用编排器（非诊断脚本）在 node-22 真实 Slurm 把 qhh+heihe 跑到 published。cycle `gfs_2026060500` → `met.forecast_cycle.status=complete`，publish job 6043 succeeded。
- **复用 download/forcing（跳过下载）**：重跑设 `restart_stage=forecast`，6029/6030/6031（download/convert/forcing）原样复用未重跑；qhh forecast `6040_0 COMPLETED`（列数不再失配，按 1633 段产出）。
- **publish 降级口径**：`flood.flood_frequency_curve` 历史基线全库 0 行（两流域皆无 hindcast 校准），故无法发洪水
  return-period tiles；publish 降级（0601cea）走 `_publish_qdown_from_database` 发**流量 q_down display 产品**，
  manifest `status=published` `degraded_to_display=true` `published_basins=2`，return-period 记诚实
  `RETURN_PERIOD_RESULT_UNAVAILABLE` residual_blocker。产品：qhh `q_down_timeseries` segment_count=1633 / 274344 行、
  heihe 2352 / 395136 行；`hydro.river_timeseries` q_down 两流域齐、`map.tile_layer` 各 `published_flag=true`。
- **真正洪水 tiles 前置（后续）**：需为每流域 onboard `flood_frequency_curve` 基线（`workers/flood_frequency` hindcast 校准），属新流域入网工作，留 #292/#293 或独立任务。

---

## 3. 运行环境配置

### 3.1 host 运行 env：`infra/env/compute.host.env`

由 `infra/env/compute.env` 派生（**不提交**，密钥留服务器），关键改写：

| 键 | 值 | 说明 |
|----|----|------|
| `DATABASE_URL` | pre-#837 historical value: `postgresql://nhms:***@10.0.2.100:55433/nhms` | archived/stopped rollback-only；当前生产不得使用 |
| `SHUD_EXECUTABLE` | `/scratch/frd_muziyao/NWM/SHUD/shud` | 真二进制 |
| `WORKSPACE_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace` | /scratch |
| `OBJECT_STORE_ROOT` | `/scratch/frd_muziyao/nhms-prod/object-store`（运行时被覆盖为 = RUN_ROOT，见 3.2） | /scratch |
| `NHMS_SCHEDULER_*_ROOT` | `/scratch/frd_muziyao/nhms-prod/...`（locks/evidence/runtime/tmp） | /scratch；运行前需 `mkdir -p` |
| `NHMS_PUBLISHED_ARTIFACT_ROOT` | `/scratch/frd_muziyao/nhms-prod/published-staging` | ⏳ 验证期 staging；业务化改 `/ghdc/data/nwm/published`（见 §7） |
| `NHMS_GRIB_ENV_ROOT` | `/scratch/frd_muziyao/nhms-grib` | — |
| `NHMS_DOWNLOAD_BBOX_*` | S8/N64/W63/E145 | 中国+10° |
| 首跑期 | `NHMS_SCHEDULER_BACKFILL_ENABLED=false`、`NHMS_RETENTION_ENABLED=false` | 受控验证；业务化再开（见 §7） |

### 3.2 诊断 fallback 启动脚本：`run_qhh_business_slurm.sh`

（位于仓库根，**不提交**；source 上面 env 后追加 QHH_* 覆盖）历史首跑/诊断参数：

```bash
export QHH_RUN_ROOT=/scratch/frd_muziyao/nhms-prod/qhh-continuous
export OBJECT_STORE_ROOT=$QHH_RUN_ROOT          # 必须 = RUN_ROOT
export QHH_ECCODES_RUNTIME=/scratch/frd_muziyao/nhms-grib
export PATH=/scratch/frd_muziyao/nhms-grib/bin:$PATH                 # cdo 二进制
# cfgrib(Python) 需经 libeccodes.so 读 GRIB2；手动跑 canonical 必须显式注入 lib，
# 否则报 "unrecognized engine cfgrib"。sbatch 路径由 chain.py:7571-7579 自动注入，
# 手动/登录节点路径无此 hook，故此处补全（缺它会被误诊为"包坏了"）。
export LD_LIBRARY_PATH=/scratch/frd_muziyao/nhms-grib/lib:${LD_LIBRARY_PATH:-}
export SHUD_TIMEOUT_SECONDS=21600               # 6h，留给 5min×7天
export QHH_CONTINUOUS_SOURCES=gfs               # GFS 先跑通，再加 IFS
export QHH_CONTINUOUS_LOOKBACK_HOURS=48
export QHH_CONTINUOUS_MAX_CYCLES_PER_SOURCE=1
export QHH_CONTINUOUS_CYCLE_LAG_HOURS=6         # 对准最新已完整出 f168 的 cycle
export QHH_GFS_FORECAST_START_HOUR=1
export QHH_GFS_FORECAST_END_HOUR=168            # 7 天
export QHH_GFS_FORECAST_RESOLUTION_SEGMENTS="120:1;384:3"  # 原生变步长:hourly≤120h,3h≤168h
export QHH_FORCING_MIN_LEAD_HOURS=1
export QHH_MAX_LEAD_HOURS=168
export QHH_CONTINUOUS_EXECUTOR=slurm
export QHH_SLURM_PARTITION=CPU
export QHH_SLURM_CPUS=8
export QHH_SLURM_MEM=64G
export QHH_SLURM_TIME=08:00:00
# 输出间隔走代码默认 5min；段分隔符用 ';'（slurm env 透传禁逗号）
uv run python scripts/run_qhh_continuous.py --once --executor slurm
```

> 并行多 cycle:需 `QHH_SLURM_WAIT=0`(提交即放锁)+ `MAX_CYCLES_PER_SOURCE≥2`;否则 `--once` 默认 `slurm-wait=True` 抱单例锁阻塞等作业,提交串行(执行仍是独立 slurm 作业并行)。

---

## 4. 诊断 fallback 运行流程

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
nohup ./run_qhh_once_slurm.sh > /tmp/qhh-once-slurm.log 2>&1 &
```

诊断 runner `run_qhh_continuous.py --once --executor slurm` 会：
1. 发现窗口内 cycle（lookback/cycle_lag），选 1 个；
2. 渲染 `scripts/run_qhh_cycle.sbatch` → `sbatch` 提交到 CPU 分区（一个作业包整 cycle）；
3. `--slurm-wait` 等作业结束并回流状态。
sbatch 内 `run_qhh_cycle.sh` 顺序执行：迁移 → **QHH 模型注册**（core.model_instance + river_network，river_segment 3738）→ seed forcing 站点（386）→ seed SHUD 输出河段 → 下载 → canonical → forcing → SHUD → parse → 汇总/发布。

---

## 5. 监控与排障

```bash
squeue -u frd_muziyao -o "%.10i %.20j %.8T %.10M %R"     # 队列/运行态
sacct -j <jobid> --format=JobID,JobName%28,State,ExitCode,Elapsed
# sbatch 日志（注意在 /scratch）：
tail -f /scratch/frd_muziyao/nhms-prod/qhh-continuous/slurm-logs/gfs/<cycle>/<jobid>.out
# DB 运行态：
psql "$DATABASE_URL" -c "select run_id,status from hydro.hydro_run order by 1 desc limit 5;"
```

作业里每个 stage 以结构化 JSON（含 `error_code`/`status`）落到 `.out`，便于定位失败 stage。

DB-free scheduler journal 的 `pipeline-jobs/` 只保留 cohort master 与 pre-#1112 legacy
flat rows，供 bounded restart discovery 使用。#1112 之后的 terminal candidate direct
projection 写入
`pipeline-jobs/by-cycle/<source>/<YYYYMMDDHH>/<job_id>.json`；按 job-id 直接读取和按
source/cycle 读取会命中该分区，但全局 restart/hard-limit scan 不递归枚举历史 candidate
分区。`journal/<source>/<cycle>.jsonl` 仍是 append-only audit truth，`latest/` 仍是
model-scoped materialized view；不得通过删除 journal 来控制 direct-file 数量，也不得把
`by-cycle/` 搬回 flat namespace。marker-free legacy flat rows 保持只读兼容，不自动升级为
accepted-submit reconcile authority。

版本化 accepted-submit master 的 reserve/commit/reject/accounting-bind 只按确定的
`pipeline_job_id` 读取 flat direct 与对应 `journal/<source>/<cycle>.jsonl`，不会为了匹配
idempotency key 枚举无关历史 `latest/`、journal 或 direct 文件。exact-comment `sacct`
仍固定查询七天、按 12 小时分页；零结果只有在查询覆盖从当前
`submission_attempt_started_at` 一直到冻结的 query end 时才是权威 absence。attempt 早于
七天 floor 时记录 `accounting_unavailable` + `coverage_incomplete`，保留 reservation，禁止
retry；窗口内找到精确记录仍可绑定。页输出超过 row/byte 上限分别记录
`bounded_output_rows_saturated` / `bounded_output_bytes_saturated`，这代表 accounting
不可用，不代表已经证明多个 exact matches，因此同样禁止 bind、cancel 和 retry。公开
evidence 只输出上述受限类别，不输出 raw comment、accounting row 或运行路径。

版本化 master 的 `submission_attempt_started_at` 是当前 attempt 的必需、不可变 evidence：
首次 reserve 必须提供合法的带时区时间；同一 attempt 的普通 upsert 不得改写；reclaim 只由
持有 cycle lock 的成功方写入新的 UTC anchor，忽略锁外请求携带的时间。direct、journal 与
latest replay 遇到缺失、无时区或畸形 anchor 均 fail closed。adapter 返回的
`coverage_complete=true` 仅是声明，reconcile consumer 还会使用 durable anchor 重新验证
`coverage_start <= anchor <= coverage_end`；缺 bounds、倒序、越界、畸形 bounds 或缺 durable
anchor 一律降为 `accounting_unavailable` + `coverage_incomplete`，不得 retry。上述 coverage
限制只约束零匹配 authority；身份已独立证明的 exact match 仍可绑定。

**实时生产监控快照**（`nhms-monitor`，通用编排器，49883ea）——一次性扫 DB + Slurm 生成结构化健康快照，适合 cron/守护轮询：

```bash
set -a; . infra/env/compute.host.env; set +a     # 提供 DATABASE_URL
uv run nhms-monitor                               # 打印 JSON 到 stdout，并写快照文件
# 输出：$NHMS_MONITORING_OUTPUT_DIR（默认 $WORKSPACE_ROOT/monitoring）/
#   monitoring_status.json  (schema nhms.live_monitoring.v1：cycle/slurm/scheduler 三段 + status=ok|warning)
#   monitoring_alerts.json  (触发的告警列表)
```

阈值（env 可调）：`NHMS_MONITORING_MAX_STALE_MINUTES=20`（cycle 停滞）、`NHMS_MONITORING_MAX_FAILED_CYCLES=0`、`NHMS_MONITORING_MAX_ACTIVE_SLURM_JOBS=32`、`NHMS_MONITORING_LOOKBACK_HOURS=168`。任一告警 → `status=warning`；Slurm CLI 不可用单列 `slurm_monitor_unavailable`。

### 5.1 Evidence retention timer (24h)

**用途**：instrument-node22-scheduler-pass-timing 之后 per-pass evidence 体积上升，需要有界回收 `NHMS_SCHEDULER_EVIDENCE_ROOT`，避免 evidence 目录无限增长。
retention 走 systemd user timer，每 24h 触发一次 age-then-size 清理，写 receipt 到 `<root>/retention/retention-<utc-iso>.json`。

**部署步骤**（在 node-22 `frd_muziyao` 账户下执行）：

```bash
cd /scratch/frd_muziyao/NWM
mkdir -p ~/.config/systemd/user
cp infra/systemd/nhms-scheduler-evidence-retention.service ~/.config/systemd/user/
cp infra/systemd/nhms-scheduler-evidence-retention.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nhms-scheduler-evidence-retention.timer
```

**验证**：

- `systemctl --user is-active nhms-scheduler-evidence-retention.timer` → `active`。
- `systemctl --user list-timers | grep nhms-scheduler-evidence-retention` → 显示下一次触发时间在 24h 内。
- 首次触发后，`ls $NHMS_SCHEDULER_EVIDENCE_ROOT/retention/retention-*.json` 应能看到 receipt 文件（首跑时脚本自建 `retention/` 子目录）。

**rollback 独立性**：`systemctl --user disable --now nhms-scheduler-evidence-retention.timer` 只停 retention，**不影响** `nhms-compute-scheduler.timer`。
两者是独立 unit 文件、无共享依赖，scheduler 继续照常提交 pass，只是 evidence 目录不再有上限。

**On-disk 路径约定**：unit 源文件位于 `infra/systemd/nhms-scheduler-evidence-retention.{service,timer}`，与 `nhms-node27-raw-retention.{service,timer}` 保持同一命名约定（两个都是 user-scope 单元，但源文件都放在 `infra/systemd/`）。

---

## 6. 首跑已处置的问题清单（按出现顺序）

| # | 现象 | 根因 | 处置 | 状态 |
|---|------|------|------|------|
| 1 | `SHUD_EXECUTABLE=/bin/true` | e2e 桩 / 旧 macOS 档 | 计算节点重编真实 Linux 二进制 | ✅ |
| 2 | 容器 `sbatch`/`sinfo` not found | 容器非 Slurm 提交点 | 执行面改到宿主机 | ✅ |
| 3 | 作业 1 秒 FAILED、无日志 | 计算节点看不到 `/ghdc` | 运行根全迁 `/scratch/nhms-prod` | ✅ |
| 4 | slurm 要求 DB 可达校验失败 | pre-#837 诊断 DB 用 127.0.0.1 / 容器名 | historical fix was `DATABASE_URL` to 10.0.2.100:55433; post-#837 this DB is archived/stopped rollback-only | ✅ |
| 5 | `QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE`（找不到 package） | `OBJECT_STORE_ROOT ≠ QHH_RUN_ROOT` | 令二者相等 | ✅ |
| 6 | canonical 需 cdo/eccodes | sbatch 用自有 runtime 约定 | `QHH_ECCODES_RUNTIME=nhms-grib` + cdo 入 PATH | ✅ |
| 7 | `QHH_BOOTSTRAP_SP_RIV_MALFORMED` | `qhh.sp.riv` 多块 SHUD 格式解析器不支持 | 多块感知解析(跳列名行/只读首块 count 行)，17f5229 | ✅ |
| 8 | `canonical_incomplete` 枚举缺失 | met.cycle_status 无该值 | migration 000027 补值 | ✅ |
| 9 | `missing_canonical_leads`（precip 9/15/21 告警） | GFS APCP 每 6h 桶重置被当单调累积 | 桶感知去累积，142dff0 | ✅ |
| 10 | forcing `manifest_bytes 4.3MB exceeds 2MB` | 7天952产品的 canonical_input_signature 内嵌格点签名膨胀（合法 lineage） | 默认 2MB→32MB，a58c25a | ✅ |
| 11 | `INVALID_TIME_WINDOW: not divisible by output interval` | 167h 窗口 ÷ 3h 输出间隔除不尽 | 输出间隔默认改 5min，81e50ff | ✅ |
| 12 | 重提作业秒死 `Stale file handle` | 运行中 `git pull` 换了正在 exec 的脚本 inode（NFS ESTALE） | 运行纪律:作业运行中不 pull（§0.8） | ✅ |
| 13 | runner state 文件被 cycle 脚本覆盖丢 `slurm_job_id` | STATE_FILE 同路径整体覆写 | json_status 合并保留 runner 字段，1baee15 | ✅ |
| 14 | 重提反复重下 125MB | download 被打断后状态停 `downloading`、trusted-identity 未持久化 | 干净跑完一次即稳；download-skip 门已落地（d29d370） | ✅ |
| 15 | forcing `Missing canonical shortwave_down/prcp_rate_or_amount`（夜间/长程时步） | GFS/IFS 累积量（ssr/apcp）16-bit GRIB 量化噪声在平段去累积出微负 delta，被标 `warn` → forcing 只收 `ok` 故剔除致缺产品 | 对齐 SHUD（`rn<0→0`、`prcp<1e-4→0`、`rh∈[0,1]`）/rSHUD：亚阈值微负记 anomaly 但保 `ok`，超阈值仍 `warn`；8cced52 + 8a8ba3d | ✅ |
| 16 | 改 converter 逻辑后重跑，旧 canonical 不重转、forcing 仍按旧 `warn` 剔除 | `QHH_FORCE_UPSTREAM=1` 未透传进 sbatch（runner `--export` 仅带 `DATABASE_URL`）；`canonical_ready` 见任意 `met.canonical_met_product` 行即跳过重转 | 兜底：FK 守卫下删该 cycle `canonical_met_product` 行触发全量重转（§9）；透传修复记后续 issue | ⏳ |

**首个真实端到端 cycle 已跑通 publish**（24h 冒烟，job 5980）：迁移→模型注册（river_segment 3738）→forcing 站点（386）→SHUD→parse（qc_passed，11431 行）→published_for_display（1633 段，return_period 诚实 `no_frequency_curve`）。

**GFS 7天/5min 全流程已跑通至 `frequency_done`**（cycle 2026060400, s3://nhms）；**IFS 7天/10min 全流程已跑通至 `frequency_done`**（job 6004, river_timeseries 1,381,518 行，canonical 384 全 ok）。GFS+IFS 双源业务化端到端验证通过。

---

## 7. 通往连续正式业务化的后续步骤

1. ✅ **修 sp.riv 解析 + 跑到 publish**：冒烟 24h cycle 已 published（job 5980）。
2. ✅ **GFS 7天/5min 全流程**：cycle 2026060400 跑至 `frequency_done`（洪频已算、display-products 已发布）。
3. ✅ **IFS 全流程**：job 6004 跑通至 `frequency_done`（cycle 2026060400；删旧 warn canonical 触发重转 → canonical 384 全 ok → forcing→SHUD→parse→frequency；river_timeseries 1,381,518 行）。
4. ✅ **GFS 尾段刷新（方案A）**：用修正后的 return_period（peak `curve_duration` 167h→1h）对 GFS 既有 SHUD 输出重跑 frequency/publish，不重算 SHUD；跨标签 re-run 残留的旧 167h 孤儿 peak 行已手动清理。
5. ⏳ **发布到用户面**：`NHMS_PUBLISHED_ARTIFACT_ROOT` 由 `published-staging` 切 `/ghdc/data/nwm/published`（node-27 NFS）；计算节点无 `/ghdc`，需控制节点 copyback。
6. ⏳ **7天 gap 审计 + 保留清理**：`NHMS_SCHEDULER_BACKFILL_ENABLED=true`、`LOOKBACK_HOURS=168`、`MAX_CYCLES_PER_SOURCE=8`；retention 先 `DRY_RUN=true`。不变量 `RETENTION_DAYS*24 > LOOKBACK_HOURS`。**注意数据量**：5min×7天≈327万行/cycle，retention 必须配套。
7. 🔀 **转连续 = m24**（不再沿用本诊断脚本转连续）：正式守护走通用 scheduler/chain daemon，由 **m24** 落地。三道硬坎(均为 m24 任务)：
   - ① Slurm HTTP gateway 在 node-22 部署(#288，通用 chain 只走 gateway、从未 live)；
   - ② 跨周期暖启动 path(b) 短 analysis 段(#289，现状每周期从固定打包标定态起跑、无水文记忆)；
   - ③ 并发 submit-and-return + durable reservation(#290)，**有序生产调度器重试已 operationalize**（b9b8446：可复用 auto-retry job + `AUTO_RETRY_JOB_CONFLICT` 守卫；新 env `NHMS_SCHEDULER_LOCK_BACKEND=postgres`、`NHMS_SCHEDULER_CYCLE_LAG_HOURS=6`）。**代码已完成并合并**：锁内 sbatch 前写持久预留行
     (`pipeline_job.status='reserved'` + `idempotency_key`)、提交后原子 bind `slurm_job_id`(`WHERE slurm_job_id IS NULL`)、
     reclaim 原子接管死预留(`status IN ('submission_failed','reservation_lost')`)；双提交防护跨重叠 pass
     (partial unique index) + 提交崩溃窗口(crash-after-sbatch-before-bind)经 reconcile-by-comment 恢复
     (每 pass 开头 `_run_restart_reconcile`，`sacct --comment=nhms_idem:<key>` 匹配，array master 行 `<id>_<task>`
     归一化到裸 `<id>`)；grace-gate 锚 `updated_at`(reserve/reclaim/bind 三路径刷新)防 slurmdbd 滞后误把 in-flight 预留降级
     reservation_lost；reconcile 会话 commit 失败后 rollback 避免毒化后续 pass。**部署前置见 §2.3**（000029 必须先 apply）。
     **尚未 live**——overlapping-submit 实况 receipt 待 daemon live = #292，依赖 #287(验 m23 #255 鲜活摄取)。

     Accepted-submit exact-comment reconcile 的全局 0/1/多重匹配证明会同时执行 `scontrol show config`
     （controller）和 `sacctmgr show config`（SlurmDBD）；两侧 `PrivateData` 都不含 `jobs`/`all` 时才允许
     `sacct --allusers`。任一命令不可用、字段缺失或配置受限时，本轮记为 accounting unavailable，不释放
     retry，也没有可绕过的 env acknowledgement。两个配置探针的 stdout/stderr 都受 byte、row 和 wall-time
     上限约束，超限或超时同样 fail closed。`sacct` 按 12 小时时间页扫描 7 天窗口，并在一个
     reconcile pass 内缓存页面、跨页按 master job id 去重；每页独立执行 20,000 行/2 MiB 限制，整轮只共享
     wall-time deadline，避免将合法的 7 天 task/`.batch` 总量误判为超限。
     文件 journal 的重启发现只读取 `reconcile-inventory/`：升级后的首次 pass 会在 flock 保护下对 current 与
     marker-free legacy active rows 做可重入 backfill，完成后写
     `reconcile-inventory-migration-v1.json`。若首次迁移中断，marker 不落盘，重启会继续；稳态 pass 不再扫描
     全量 `pipeline-jobs/`、cycle journal 或 `pipeline-jobs/by-cycle/` candidate 历史。

     **禁止直接回退到 pre-inventory writer。** 支持的回滚必须先停 timer/service，并用当前版本争用生产
     scheduler file lease、写 preparation receipt 和 rollback fence；命令失败时不得切换代码：

     ```bash
     uv run nhms-pipeline prepare-file-journal-rollback \
       --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
       --workspace-root "$WORKSPACE_ROOT" \
       --scheduler-lock-backend file \
       --scheduler-state stopped \
       --active-scheduler-processes 0 \
       --checked-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
       --checked-by "$USER" \
       --target-writer-generation '<rollback-git-sha>'
     ```

     `--target-writer-generation` 必须是计划运行 checkout 的完整 `git rev-parse HEAD`，不得使用短 SHA；
     格式校验发生在 file lease、marker、fence 或 receipt 的任何变更之前。
     preparation receipt 的 `receipt_id` 必须随回滚记录保存；旧 writer 不得直接启动，也不得由操作者
     自报 actual generation。必须从仍在当前版本的控制 checkout 调用
     `launch-file-journal-rollback-writer`，让它从将要执行的 clean checkout 内部解析并复核 generation，
     验证该 checkout 的 `.venv/bin/python` 存在且可执行，通过 gate 后才以该 runtime 启动 writer：

     ```bash
     uv run nhms-pipeline launch-file-journal-rollback-writer \
       --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
       --workspace-root "$WORKSPACE_ROOT" \
       --receipt-id '<preparation-receipt-id>' \
       --writer-repository-root '<clean-rollback-checkout>' \
       -- plan-production --submit --continuous --max-passes 1
     ```

     launcher 只接受 `plan-production` 的一次真实 `--submit`；缺少 `--submit`、`--plan`、`--dry-run`、
     `--help`/`--version`、root/lock override 或其他命令都必须在 writer 零启动时 fail closed。通过
     receipt 后，controller 将完整目标 SHA 物化为
     `WORKSPACE_ROOT/.nhms-rollback-execution-v1/<receipt>-<generation>/source`，并把已打开且
     复核过的目标解释器复制到同一私有只读 generation root 的 `runtime`；两者均不位于原
     checkout/venv，active binding 发布后删除整个原 checkout 也不得影响后续 worker。child 的
     journal/workspace/file lock 由 receipt gate
     强制绑定，ambient environment 不能改写。checkout dirty、含 untracked 文件、无法解析、切换中、
     runtime 不可用或与 receipt target 不同也均必须零启动。rollback fence 存在期间，当前版本
     scheduler 必须返回 `scheduler_rollback_fence_prepared`，不得自动 backfill 或提交业务任务。重新升级后，
     在 timer/service 仍停止时执行显式 roll-forward；成功 receipt 落盘且 fence 被消费后才可恢复 timer：

     ```bash
     uv run nhms-pipeline complete-file-journal-rollforward \
       --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
       --workspace-root "$WORKSPACE_ROOT" \
       --scheduler-lock-backend file \
       --preparation-receipt-id '<preparation-receipt-id>'
     ```

     preparation 和 roll-forward 命令都必须取得与生产 scheduler 相同的 file lease；live lease、
     `preparing` 崩溃恢复失败、receipt 篡改/过期/跨 root 重放、writer checkout generation 不匹配或
     backfill 期间 authority 变化均 fail closed。node-22 回滚演练 receipt 必须同时保存 preparation、旧 writer
     gate、roll-forward 和恢复后 inventory-only 证据。

     `WORKSPACE_ROOT` 下的 generation retention root 必须位于 gateway/compute 共同可见路径；目标
     checkout 和 `.venv` 只需在 active 发布前对 launcher 可见。该 runtime 经 manifest 和 HTTP
     gateway 显式传给 forcing、forecast、state-save 三阶段；普通生产提交仍用原 console
     entrypoint。runtime bundle 的 retention 为
     `retained_fail_closed_until_operator_cleanup`：old writer 非零退出或 controller 崩溃也不得删除。
     active binding 会 bounded/no-follow 遍历完整 runtime tree；nested file/dir 可写、symlink、
     special entry 或非约定 executable mode 都必须在零 sbatch 时 fail closed。
     active 脚本会 unset `PYTHONHOME`/`VIRTUAL_ENV`，固定 `PYTHONPATH` 为 bound source，并用 bound
     runtime bin 加最小系统路径替换 ambient `PATH`；forecast 两段 heredoc 也使用 exact runtime。
     launcher 的独立 execution flock 会阻止 old writer 存活期间的 roll-forward；在所有引用该 runtime
     的 Slurm task 终态前，也不得人工前滚、恢复 timer 或删除 generation retention root。严格前滚查询
     会固定 reconcile-inventory/journal/latest/pipeline-jobs/active-reconcile 五个 root 身份，任何消失、
     替换或变化都统一 fail closed；recursive journal/latest walker 还会在每层目录 list 前后及 child
     recursion 后复核该层签名，nested entry 不得静默消失、替换或新增。只有全程不存在的 root
     可视为空。最终 receipt 必须保存 exact job
     identity、三个 sbatch 中的同一 runtime/source 路径，以及 generation retention 清理结果。
     out-of-scope LOW 收尾 → #300。
7b. ✅ **多源下载韧性**（PR #308，b4a2e85/eeb4d5c）：GFS 换 NODD 多镜像链（`GFS_SOURCE_BACKENDS=s3,gcs,azure,ftpprd,nomads`，共享 `.idx`+HTTP-Range+本地 cdo-clip，NOMADS grib-filter 末位回退）；
IFS 云镜像优先（`IFS_OPEN_DATA_FALLBACK_SOURCES` 默认 `aws,azure,google,ecmwf`，ECMWF 直连 500 连接上限末位）。NOMADS 403=动态封禁 → 持久断路器（`OBJECT_STORE_ROOT/state/source_circuit/`，cooldown 内停重试）；
云镜像 503/429/SlowDown 归类 `RateLimitedError` → 切源 + per-source cooldown（`IFS_SOURCE_COOLDOWN_SECONDS=1800`）；f000 缺累积场镜像 404 回落 NOMADS、重复 APCP idx fail-loud。**从根上消解 §3B 缺陷③ 单源静默丢 cycle。**
8. ⏳ **生产硬化**（并入 m24/后续）：独立生产 PostgreSQL（替换 e2e 容器）、固化 `compute.host.env`、`QHH_FORCE_UPSTREAM` 透传 sbatch（§6 #16，亦由 m24 改走 chain 自动重转消解）、source-trust/docker 预检、监控告警（`nhms-monitor` 已落地，见 §5；接 cron/守护）、诊断脚本退役护栏(#293)。
9. ⏳ **洪频曲线对齐**：将来建 ERA5 hindcast 洪频曲线（hourly）后，5min 预报侧 return-period 窗口需按 12 行/小时折算（`flood_frequency/frequency.py` ROWS 窗口假设 hourly）。

> 注:本节 1–4 是诊断路径下已实测的科学链路结果(GFS+IFS 至 frequency_done),证明 worker 链正确;5–9 的"正式业务化"在 m24 通用 daemon 上重做并 live 验证,不再以本脚本声称 production。

---

## 8. 已修复：`qhh.sp.riv` 多块格式解析 ✅

- 位置：`workers/model_registry/qhh_production_bootstrap.py::read_qhh_output_segment_count`（commit 17f5229）。
- 原因：旧逻辑 `rows = lines[1:]` 把首行后**所有行**当数据，既不跳列名行也不在首块 `count` 行后停。真实 SHUD `.sp.riv` 是多块（拓扑/河道类型/坐标），每块"计数行+列名行+数据行"。
- 修法：跳过可选列名行（首 token 非段号时）→ 只读首块 `count` 行 → 忽略尾块；兼容旧格式；已补多块 fixture 单测。本例首块 1633 段。

---

## 9. 操作：改 converter 逻辑后强制 canonical 重转

改了 `canonical_converter` 的质量/单位处理（如 §6 #15 的量化容差）后，已下载并转过的 cycle 不会自动重转——
`run_qhh_cycle.sh::canonical_ready` 见任意 `met.canonical_met_product` 行即跳过，且 `QHH_FORCE_UPSTREAM=1` 当前不透传进
sbatch（§6 #16）。兜底是删该 cycle 的 canonical 行使 `canonical_ready=0`，再重跑 launcher 即在完整 sbatch env 下重转：

```bash
# 1) 删前先确认无 forcing_version_component 引用（FK 守卫），再删
set -a; source infra/env/compute.host.env; set +a   # 提供 DATABASE_URL（仅 DB，不含 S3 凭证）
uv run python - <<'PY'
import os, psycopg2
from datetime import datetime, timezone
SRC, CT = "IFS", datetime(2026,6,4,0,0,tzinfo=timezone.utc)   # 改成目标 source/cycle
con = psycopg2.connect(os.environ["DATABASE_URL"]); con.autocommit=False; cur=con.cursor()
cur.execute("""SELECT count(*) FROM met.forcing_version_component fvc
  JOIN met.canonical_met_product c ON c.canonical_product_id=fvc.canonical_product_id
  WHERE c.source_id=%s AND c.cycle_time=%s""",(SRC,CT))
if cur.fetchone()[0]==0:
    cur.execute("DELETE FROM met.canonical_met_product WHERE source_id=%s AND cycle_time=%s",(SRC,CT))
    print("deleted", cur.rowcount); con.commit()
else:
    con.rollback(); print("ABORT: forcing_version_component 仍引用，先处理 forcing_version")
con.close()
PY
# 2) 重跑 launcher：canonical_ready=0 → 全量重转（应用新代码）→ forcing→SHUD→parse→publish
```

> ⚠️ 不要手动 `nhms-canonical convert`：手动 shell 只 source 了 `compute.host.env`（无对象存储 endpoint/凭证），读
> `s3://nhms/raw/.../manifest.json` 会回退成本地相对路径 `raw` 而报 `No such file or directory`。必须走 sbatch
> （计算节点有完整 S3 runtime）。
