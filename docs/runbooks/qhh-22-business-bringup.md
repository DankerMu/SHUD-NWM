# QHH node-22 业务化运行流程与方法（首跑联调梳理）

> 本文记录在 node-22 上把 QHH 全链路从"E2E 测试架"推向"真实 Slurm 生产运行"过程中**实测验证出的正确执行模型、配置与方法**，作为后续正式业务化的依据。
>
> 状态标注：✅ 已实测验证 / ⏳ 待完成 / ⚠️ 已知待修。
>
> 最后更新：2026-06-04（首个真实 cycle 已跑通 publish；e2e→生产前缀切换；干净 GFS 7天/5min 全流程进行中）。

---

## 0. 核心结论（TL;DR）

1. **执行面必须在宿主机（node-22 登录节点 `xnode`）跑，不能在 compute-api 容器里跑** —— 容器内没有 Slurm CLI（`sbatch`/`sinfo` not found），无法提交真作业；且 SHUD 生产 preflight 会 `os.stat`+`ldd`+跑 `--version`，需要二进制与 SUNDIALS 在执行处可见，宿主机才看得到 `/scratch` 与 `$HOME/sundials`。
2. **所有运行根必须在 `/scratch`** —— 计算节点（cn01-24）只挂 `/scratch`（NFS 10.0.2.99）、`/volume/data/nwm/Basins`、`/users/frd_muziyao/sundials`，**看不到 `/ghdc`**（那是 node-27 的 NFS）。把 workspace/object-store/run-root 放 `/ghdc` 会导致作业 1 秒即死（连 sbatch 日志都写不出）。
3. **DB 要用集群 IP**，不能用容器名或 127.0.0.1 —— 计算节点解析不了 docker 容器名 `nhms-22-e2e-db`，也连不上登录节点的 `127.0.0.1`。用 `10.0.2.100:55433`（DB 容器在宿主机发布的端口 + 集群网 IP），登录节点与计算节点都可达。
4. **`OBJECT_STORE_ROOT` 必须等于 `QHH_RUN_ROOT`** —— QHH 包经 s3 前缀发布到 object-store，seed 步骤在 run-root 找；二者不一致会"package path unsafe / 找不到"。
5. **业务运行器是 `scripts/run_qhh_continuous.py --executor slurm`**（runbook 记载的 QHH 业务路径，自带模型注册 + 全链路）；`--once` 一轮、去掉即连续。**不是** `plan-production`（后者假设模型已注册，且容器内无法提交 Slurm）。
6. **对象存储用生产前缀 `s3://nhms`**（`OBJECT_STORE_PREFIX`，文件系统对象存储的 URI 标签）——e2e 标签 `s3://nhms-22-e2e` 已废弃；`object_store.py` 会校验 URI 桶名==前缀,切前缀必须先清干净旧标签数据(否则旧 URI 解析报错)。
7. **SHUD 输出间隔 = 5min**（`QHH_MODEL_OUTPUT_INTERVAL`，代码默认已改 5）——洪频分析假设 hourly，3h 太粗；5min 对任意整数小时窗口可整除（7天=2004步）。窗口必须被输出间隔整除，否则 `INVALID_TIME_WINDOW`。
8. **⚠️ 运行纪律：作业运行中禁止在 node-22 `git pull`**——git 换 inode 会让正在逐行 exec `run_qhh_cycle.sh` 的 bash 句柄失效（`Stale file handle`），秒杀作业。要 pull 必须先等作业结束。代码更新与作业运行错开。

---

## 1. 执行拓扑

| 角色 | 位置 | 职责 | 可见文件系统 |
|------|------|------|------|
| 控制/调度面 | node-22 登录节点 `xnode`（10.0.2.100 / 10.0.1.100 / 210.77.77.22） | 跑 `run_qhh_continuous.py`、提交 sbatch、等待、写 DB | `/scratch`、`/ghdc`、`/volume`、`/users`、容器 |
| 计算执行面 | 计算节点 cn01-24（CPU 分区） | sbatch 内跑全链路（下载→canonical→forcing→SHUD→parse→publish） | `/scratch`、`/volume/data/nwm/Basins`、`/users/frd_muziyao/sundials` ❌ **无 `/ghdc`** |
| 数据库 | 容器 `nhms-22-e2e-db`（timescaledb-ha pg15） | hydro/met/ops/flood schema（26 迁移） | 宿主端口 55433；集群可达 10.0.2.100:55433 |

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
uv run python -m packages.common.migrate        # 26 迁移；已应用则全部 skip
```

### 2.4 Basins 数据 ✅

- `/volume/data/nwm/Basins/qhh`（计算节点可见）。QHH 包由运行器从此发布。

---

## 3. 运行环境配置

### 3.1 host 运行 env：`infra/env/compute.host.env`

由 `infra/env/compute.env` 派生（**不提交**，密钥留服务器），关键改写：

| 键 | 值 | 说明 |
|----|----|------|
| `DATABASE_URL` | `postgresql://nhms:***@10.0.2.100:55433/nhms` | 集群 IP，计算节点可达 |
| `SHUD_EXECUTABLE` | `/scratch/frd_muziyao/NWM/SHUD/shud` | 真二进制 |
| `WORKSPACE_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace` | /scratch |
| `OBJECT_STORE_ROOT` | `/scratch/frd_muziyao/nhms-prod/object-store`（运行时被覆盖为 = RUN_ROOT，见 3.2） | /scratch |
| `NHMS_SCHEDULER_*_ROOT` | `/scratch/frd_muziyao/nhms-prod/...`（locks/evidence/runtime/tmp） | /scratch；运行前需 `mkdir -p` |
| `NHMS_PUBLISHED_ARTIFACT_ROOT` | `/scratch/frd_muziyao/nhms-prod/published-staging` | ⏳ 验证期 staging；业务化改 `/ghdc/data/nwm/published`（见 §7） |
| `NHMS_GRIB_ENV_ROOT` | `/scratch/frd_muziyao/nhms-grib` | — |
| `NHMS_DOWNLOAD_BBOX_*` | S8/N64/W63/E145 | 中国+10° |
| 首跑期 | `NHMS_SCHEDULER_BACKFILL_ENABLED=false`、`NHMS_RETENTION_ENABLED=false` | 受控验证；业务化再开（见 §7） |

### 3.2 启动脚本：`run_qhh_business_slurm.sh`

（位于仓库根，**不提交**；source 上面 env 后追加 QHH_* 覆盖）业务参数：

```bash
export QHH_RUN_ROOT=/scratch/frd_muziyao/nhms-prod/qhh-continuous
export OBJECT_STORE_ROOT=$QHH_RUN_ROOT          # 必须 = RUN_ROOT
export QHH_ECCODES_RUNTIME=/scratch/frd_muziyao/nhms-grib
export PATH=/scratch/frd_muziyao/nhms-grib/bin:$PATH
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

## 4. 运行流程

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
nohup ./run_qhh_once_slurm.sh > /tmp/qhh-once-slurm.log 2>&1 &
```

运行器 `run_qhh_continuous.py --once --executor slurm` 会：
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

---

## 6. 首跑已处置的问题清单（按出现顺序）

| # | 现象 | 根因 | 处置 | 状态 |
|---|------|------|------|------|
| 1 | `SHUD_EXECUTABLE=/bin/true` | e2e 桩 / 旧 macOS 档 | 计算节点重编真实 Linux 二进制 | ✅ |
| 2 | 容器 `sbatch`/`sinfo` not found | 容器非 Slurm 提交点 | 执行面改到宿主机 | ✅ |
| 3 | 作业 1 秒 FAILED、无日志 | 计算节点看不到 `/ghdc` | 运行根全迁 `/scratch/nhms-prod` | ✅ |
| 4 | slurm 要求 DB 可达校验失败 | DB 用 127.0.0.1 / 容器名 | `DATABASE_URL` 改集群 IP 10.0.2.100:55433 | ✅ |
| 5 | `QHH_BOOTSTRAP_PACKAGE_PATH_UNSAFE`（找不到 package） | `OBJECT_STORE_ROOT ≠ QHH_RUN_ROOT` | 令二者相等 | ✅ |
| 6 | canonical 需 cdo/eccodes | sbatch 用自有 runtime 约定 | `QHH_ECCODES_RUNTIME=nhms-grib` + cdo 入 PATH | ✅ |
| 7 | `QHH_BOOTSTRAP_SP_RIV_MALFORMED` | `qhh.sp.riv` 多块 SHUD 格式解析器不支持 | 多块感知解析(跳列名行/只读首块 count 行)，17f5229 | ✅ |
| 8 | `canonical_incomplete` 枚举缺失 | met.cycle_status 无该值 | migration 000027 补值 | ✅ |
| 9 | `missing_canonical_leads`（precip 9/15/21 告警） | GFS APCP 每 6h 桶重置被当单调累积 | 桶感知去累积，142dff0 | ✅ |
| 10 | forcing `manifest_bytes 4.3MB exceeds 2MB` | 7天952产品的 canonical_input_signature 内嵌格点签名膨胀（合法 lineage） | 默认 2MB→32MB，a58c25a | ✅ |
| 11 | `INVALID_TIME_WINDOW: not divisible by output interval` | 167h 窗口 ÷ 3h 输出间隔除不尽 | 输出间隔默认改 5min，81e50ff | ✅ |
| 12 | 重提作业秒死 `Stale file handle` | 运行中 `git pull` 换了正在 exec 的脚本 inode（NFS ESTALE） | 运行纪律:作业运行中不 pull（§0.8） | ✅ |
| 13 | runner state 文件被 cycle 脚本覆盖丢 `slurm_job_id` | STATE_FILE 同路径整体覆写 | json_status 合并保留 runner 字段，1baee15 | ✅ |
| 14 | 重提反复重下 125MB | download 被打断后状态停 `downloading`、trusted-identity 未持久化 | 干净跑完一次即稳；download-skip 门待落地（task #7） | ⏳ |

**首个真实端到端 cycle 已跑通 publish**（24h 冒烟，job 5980）：迁移→模型注册（river_segment 3738）→forcing 站点（386）→SHUD→parse（qc_passed，11431 行）→published_for_display（1633 段，return_period 诚实 `no_frequency_curve`）。当前在跑干净 GFS 7天/5min 全流程（job 5992, s3://nhms）。

---

## 7. 通往连续正式业务化的后续步骤

1. ✅ **修 sp.riv 解析 + 跑到 publish**：冒烟 24h cycle 已 published（job 5980）。
2. ⏳ **校验 7天/5min 全流程产物**：干净 GFS（job 5992, s3://nhms）跑至 `published`，校验 DB `hydro.hydro_run`、river_timeseries（≈2004步×1633段）、return_period_result。
3. ⏳ **发布到用户面**：`NHMS_PUBLISHED_ARTIFACT_ROOT` 由 `published-staging` 切 `/ghdc/data/nwm/published`（node-27 NFS）；计算节点无 `/ghdc`，需控制节点 copyback。
4. ⏳ **7天 gap 审计 + 保留清理**：`NHMS_SCHEDULER_BACKFILL_ENABLED=true`、`LOOKBACK_HOURS=168`、`MAX_CYCLES_PER_SOURCE=8`；retention 先 `DRY_RUN=true`。不变量 `RETENTION_DAYS*24 > LOOKBACK_HOURS`。**注意数据量**：5min×7天≈327万行/cycle，retention 必须配套。
5. ⏳ **加 IFS 源**（GFS 跑通后）：`QHH_CONTINUOUS_SOURCES=gfs,IFS`，`IFS_FORECAST_RESOLUTION_SEGMENTS`（3h≤144h+6h），cdo 本地裁剪（已在 PATH）。
6. ⏳ **转连续**（等指令）：去 `--once` 或 systemd timer；并行需 `QHH_SLURM_WAIT=0`+`MAX_CYCLES≥2`。
7. ⏳ **生产硬化**：独立生产 PostgreSQL（替换 e2e 容器）、固化 `compute.host.env`、download-skip 门、source-trust/docker 预检、监控告警。
8. ⏳ **洪频曲线对齐**：将来建 ERA5 hindcast 洪频曲线（hourly）后，5min 预报侧 return-period 窗口需按 12 行/小时折算（`flood_frequency/frequency.py` ROWS 窗口假设 hourly）。

---

## 8. 已修复：`qhh.sp.riv` 多块格式解析 ✅

- 位置：`workers/model_registry/qhh_production_bootstrap.py::read_qhh_output_segment_count`（commit 17f5229）。
- 原因：旧逻辑 `rows = lines[1:]` 把首行后**所有行**当数据，既不跳列名行也不在首块 `count` 行后停。真实 SHUD `.sp.riv` 是多块（拓扑/河道类型/坐标），每块"计数行+列名行+数据行"。
- 修法：跳过可选列名行（首 token 非段号时）→ 只读首块 `count` 行 → 忽略尾块；兼容旧格式；已补多块 fixture 单测。本例首块 1633 段。
