# Current Production Operations Runbook

最后更新：2026-06-14
适用范围：node-22 计算控制面、Slurm 计算节点、node-27 只读展示面。

本文是当前业务化运行值守手册，记录服务如何拉起、业务流程、每一步产物位置和当前已知卡点。历史 bring-up 记录见
[`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)；两节点职责边界见
[`two-node-deployment-overview.md`](two-node-deployment-overview.md)。

## 1. 当前结论

- 正式业务入口是 node-22 上的通用调度器：
  `python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1`。
- 调度器每个 pass 发现 GFS/IFS cycle，提交 `download -> convert -> forcing -> forecast -> parse -> state_save_qc -> frequency -> publish`。
- 计算任务通过 standalone Slurm Gateway 提交到 CPU 分区，实际执行在 Slurm 计算节点。
- 计算节点不保证能访问 `/ghdc`；运行中间态必须放在 `/scratch/frd_muziyao/nhms-prod`。
- 对 27 展示可见的发布面是 `/ghdc/data/nwm/published`，27 本机对应 `/home/ghdc/nwm/published`。
- 主展示产品当前以 DB/PostGIS live 查询和 published q_down/logs 为主；`frequency` 的 basin-level 质量记录仍有缺表卡点，见第 8 节。

## 2. 节点和服务

| 面 | 位置 | 当前职责 | 关键入口 |
| --- | --- | --- | --- |
| 计算控制面 | node-22 / `10.0.2.100` | 调度、写 DB、提交 Slurm、发布到 `/ghdc` | `plan-production --submit --continuous --max-passes 1` |
| Slurm Gateway | node-22 | 将调度请求转为 Slurm 作业 | `python -m services.slurm_gateway` |
| Slurm 计算节点 | cnXX | 执行 download/convert/forcing/SHUD/parse/frequency 等 sbatch 阶段 | Slurm job / array task |
| 展示服务面 | node-27 | 只读展示 `/` 和 `/ops` | FastAPI + frontend，只读 DB 和 published artifacts |
| DB | `10.0.2.100:55433/nhms` | `met`、`hydro`、`ops`、`core`、`map`、`flood` 状态源 | writer 用于 22，readonly 用于 27 |

不要把 `10.0.2.100:55432` 当成当前业务库；当前业务/展示口径是 `10.0.2.100:55433/nhms`。

## 3. 如何拉起和确认服务

### 3.1 调度器

当前生产 pass 形态：

```bash
cd /scratch/frd_muziyao/NWM
set -a
source infra/env/compute.host.env
set +a

uv run python -m services.orchestrator.cli plan-production \
  --submit \
  --continuous \
  --max-passes 1
```

实际值守时通常由外部 supervisor、timer 或守护脚本周期性拉起 bounded pass。确认方式：

```bash
pgrep -af "plan-production|services.orchestrator.cli|scheduler"
find /scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence \
  -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort | tail -20
tail -200 /scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.log
tail -200 /scratch/frd_muziyao/nhms-prod/workspace/scheduler/logs/nhms-compute-scheduler.err
```

正常现象：scheduler evidence 约每 5-6 分钟刷新；正在提交或探测时会出现一个短生命周期
`plan-production --submit --continuous --max-passes 1` 进程。

### 3.2 Slurm Gateway

确认入口：

```bash
pgrep -af "services.slurm_gateway"
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"
sacct -j <job_id> --format=JobID,JobName%24,State,ExitCode,Elapsed,Start,End -P
```

Gateway systemd 样例在 [`infra/systemd/nhms-slurm-gateway.service`](../../infra/systemd/nhms-slurm-gateway.service)。
它只应暴露 Slurm gateway API，不应指向完整业务 API。

### 3.3 API / 展示服务

当前常见进程形态：

```bash
pgrep -af "uvicorn apps.api.main"
```

可能同时存在：

- Docker 容器内 API：`uvicorn apps.api.main:app --host 0.0.0.0 --port 8000`
- node-22 本机诊断 API：`uvicorn apps.api.main:app --host 0.0.0.0 --port 8001 --log-level info`

运行日志示例：

```bash
tail -200 /scratch/frd_muziyao/nhms-prod/runtime/api/nhms-api-8001.log
```

27 展示面应使用只读 DB 账号和只读 published mount；不应运行正式 `plan-production`。

### 3.4 监控快照

```bash
cd /scratch/frd_muziyao/NWM
set -a
source infra/env/compute.host.env
set +a
uv run nhms-monitor

cat /scratch/frd_muziyao/nhms-prod/workspace/monitoring/monitoring_status.json
cat /scratch/frd_muziyao/nhms-prod/workspace/monitoring/monitoring_alerts.json
tail -200 /scratch/frd_muziyao/nhms-prod/workspace/monitoring/logs/nhms-live-monitor.log
```

`scheduler_stale` 表示最近 evidence 超过阈值未刷新；先结合 scheduler 进程、Slurm 队列和 DB job 状态判断是否真实卡住。

## 4. 业务流程

一次 cycle 的主流程如下：

```text
source discovery
  -> download_source_cycle
  -> convert_canonical
  -> produce_forcing_array
  -> run_shud_forecast_array
  -> parse_output_array
  -> save_state_snapshot_array
  -> compute_frequency_array
  -> publish_tiles
```

对应 DB job 类型和阶段：

| 顺序 | `ops.pipeline_job.job_type` | `stage` | 说明 |
| --- | --- | --- | --- |
| 1 | `download_source_cycle` | `download` | 下载 GFS/IFS 原始资料 |
| 2 | `convert_canonical` | `convert` | 转 canonical met product |
| 3 | `produce_forcing_array` | `forcing` | 按流域和模型生成 SHUD forcing |
| 4 | `run_shud_forecast_array` | `forecast` | 运行 SHUD |
| 5 | `parse_output_array` | `parse` | 解析 SHUD 输出并入库 `hydro.river_timeseries` |
| 6 | `save_state_snapshot_array` | `state_save_qc` | 状态快照和 QC |
| 7 | `compute_frequency_array` | `frequency` | 计算 flood return period / 质量产物 |
| 8 | `publish_tiles` | `publish` | 发布 q_down 展示产物和日志到 published 面 |

`met.forecast_cycle.status=complete` 表示该 source cycle 主链路完成。`hydro.hydro_run.status` 当前可能停在
`parsed`，原因是 basin-level `frequency` 子任务写质量表失败；只看主展示是否可用时，需要同时看
`ops.pipeline_job` 的 `publish_tiles` 和 `hydro.river_timeseries`。

## 5. 产物位置

### 5.1 数据库

| Schema / 表 | 内容 | 值守用途 |
| --- | --- | --- |
| `met.forecast_cycle` | source cycle 状态 | 判断时次是否 discovered/downloading/canonical_ready/complete |
| `met.canonical_met_product` | canonical 气象产品索引 | 查 convert 是否产出完整 |
| `hydro.hydro_run` | 每个 source/model/basin 的水文 run | 查流域 run 状态、错误码 |
| `hydro.river_timeseries` | q_down 等河段时序 | 展示主数据源 |
| `ops.pipeline_job` | 每个阶段 job 状态 | 判断卡在哪个 stage |
| `ops.pipeline_event` | 状态事件 | 追踪状态流转 |
| `core.basin_version` / `core.river_segment` | 流域、河段、几何和输出段 | 判断 Heihe/QHH 范围和河段映射 |
| `map.tile_layer` | 发布图层登记 | 判断 published product 是否被登记 |
| `flood.return_period_result` / `flood.flood_frequency_curve` | 洪频结果和曲线 | frequency 支线 |

常用查询：

```bash
psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select cycle_id, source_id, cycle_time, status, retry_count, coalesce(error_code,'')
from met.forecast_cycle
order by cycle_time desc, source_id
limit 20;"

psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select job_id, cycle_id, job_type, status, slurm_job_id, submitted_at, started_at,
       finished_at, updated_at, coalesce(error_code,''), left(coalesce(error_message,''),120)
from ops.pipeline_job
order by submitted_at desc nulls last
limit 80;"
```

### 5.2 Workspace

计算中间态和 Slurm 日志在：

```text
/scratch/frd_muziyao/nhms-prod/workspace/
  cycle_<source>_<YYYYMMDDHH>/logs/<slurm_job_id>.out|err
  fcst_<source>_<YYYYMMDDHH>_<model_id>/logs/<slurm_job_id>_<array_task>.out|err
  runs/<run_id>/logs/shud_stdout.log
  runs/<run_id>/logs/shud_stderr.log
  scheduler/evidence/*.json
  scheduler/logs/nhms-compute-scheduler.log|err
  monitoring/monitoring_status.json
```

示例：

```bash
tail -100 /scratch/frd_muziyao/nhms-prod/workspace/cycle_gfs_2026061312/logs/<jobid>.out
tail -100 /scratch/frd_muziyao/nhms-prod/workspace/fcst_gfs_2026061312_basins_heihe_shud/logs/<jobid>_0.out
```

### 5.3 Object Store 文件系统根

当前文件系统对象存储根：

```text
/scratch/frd_muziyao/nhms-prod/object-store/
  canonical/<source>/<YYYYMMDDHH>/<variable>/*.nc
  forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/
  runs/<run_id>/
  tiles/...
```

URI 口径通常使用 `s3://nhms/...`，但当前本地落盘根是上面的 `/scratch/.../object-store`。
不要把 `/scratch/frd_muziyao/nhms-prod/object-store` 删除；它是计算节点可见的生产 staging 和对象存储落盘根。

### 5.4 Shared Object Store Copyback

完整水文 run 产物不放在 `published/` 下。`s3://nhms/runs/<run_id>/...` 当前先落在
`/scratch/frd_muziyao/nhms-prod/object-store/runs/<run_id>/...`，再由 22 控制面的 publish 阶段同步到共享对象存储镜像：

```text
22 host: /ghdc/data/nwm/object-store/runs/<run_id>/
27 host: /home/ghdc/nwm/object-store/runs/<run_id>/
URI key: runs/<run_id>/
```

当前生产环境通过 `NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store` 启用 copyback。
如果该同步失败，`publish` 阶段应失败，而不是只发布 tiles 后把完整 run 产物留在 22 私有 staging。
历史已发布 q_down run 如果缺少对应 `forcing/...` 包，按
[`forcing-copyback-backfill.md`](forcing-copyback-backfill.md) 在 node-22 先 dry-run、再显式 `--apply`
补拷；不要通过手动修改 `hydro`/`met` 状态来绕过缺包。

检查最新 run 产物：

```bash
find /ghdc/data/nwm/object-store/runs -maxdepth 2 -type d -name 'fcst_*20260613*' \
  -printf '%TY-%Tm-%Td %TH:%TM %M %u %g %p\n' | sort | tail -30

ls -la /ghdc/data/nwm/object-store/runs/fcst_gfs_2026061312_basins_heihe_shud/output/
```

### 5.5 Published Artifacts

当前 22 写、27 读的发布面：

```text
22 host: /ghdc/data/nwm/published
27 host: /home/ghdc/nwm/published
container: /var/lib/nhms/published
URI prefix: published://
```

目录形态：

```text
/ghdc/data/nwm/published/
  logs/<source>/<YYYYMMDDHH>/cycle_<source>_<YYYYMMDDHH>/job_*.out
  tiles/hydro/<source>_<YYYYMMDDHH>/q-down/...
```

`published/` 只承载展示发布物、瓦片 manifest 和日志；不要在这里查完整 SHUD `runs/<run_id>/output/`。
完整 run 产物见第 5.4 节的 `/ghdc/data/nwm/object-store/runs/`。

检查最新发布：

```bash
find /ghdc/data/nwm/published/logs -maxdepth 4 -type f -name '*publish.out' \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort | tail -30

find /ghdc/data/nwm/published/tiles/hydro -maxdepth 3 -type f \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort | tail -30
```

注意：Slurm 计算节点不应依赖 `/ghdc`。需要先在 `/scratch/.../object-store` 和 workspace 完成计算，再由 22 控制面的
publish/copyback 阶段把完整 run 产物写到 `/ghdc/data/nwm/object-store`，把展示产物、manifest、日志写到
`/ghdc/data/nwm/published`。

## 6. 如何判断是否卡住

先分清三种状态：

- 正常运行：Slurm 有 active job，DB `ops.pipeline_job.status=running`，对应日志持续更新。
- 等下一 pass：Slurm 队列空，scheduler evidence 在 5-6 分钟内刷新，`met.forecast_cycle` 没有新的可提交 gap。
- 真实卡住：Slurm job 已 terminal，但 DB 仍 `running` 很久；或 scheduler evidence 超过阈值不刷新；或同一 stage 持续失败。

推荐检查顺序：

```bash
date '+%F %T %Z'
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"

psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select job_id, cycle_id, job_type, status, slurm_job_id, submitted_at, started_at,
       finished_at, updated_at, coalesce(error_code,''), left(coalesce(error_message,''),140)
from ops.pipeline_job
where submitted_at >= now() - interval '18 hours'
order by submitted_at desc nulls last
limit 80;"

find /scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence \
  -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort | tail -20
```

如果 `squeue` 为空但 DB 仍显示 `running`，查 Slurm accounting：

```bash
sacct -j <slurm_job_id> --format=JobID,JobName%24,State,ExitCode,Elapsed,Start,End -P
```

如果 Slurm 已 `COMPLETED`，等待下一次 scheduler pass 回收；如果超过一个 pass 周期仍未回收，再查 scheduler 日志和 DB 会话：

```bash
psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select pid, state, wait_event_type, wait_event, now()-query_start as query_age,
       now()-xact_start as xact_age, left(query,200)
from pg_stat_activity
where datname='nhms'
order by query_start nulls last;"
```

## 7. 当前运行口径

截至 2026-06-14 中午的现场观察：

- `2026061306` 已完成 GFS/IFS 主链路并发布到 `/ghdc/data/nwm/published`。
- `2026061312` 已进入运行，GFS/IFS 下载和 canonical 转换完成，正在或已进入后续 forcing/forecast 阶段。
- `2026061318` 已发现部分 source，可在上一轮完成后由后续 pass 提交。
- scheduler evidence 持续刷新，不是完全停摆。

这段是值守快照，不是长期事实。交接时必须用第 6 节命令重新刷新。

## 8. 当前已知卡点

### 8.1 `flood.run_product_quality` 缺表

现象：

```text
RETURN_PERIOD_FAILED
relation "flood.run_product_quality" does not exist
```

影响：

- basin-level `frequency` 子任务失败。
- `hydro.hydro_run.status` 可能停在 `parsed`，并记录 `RETURN_PERIOD_FAILED`。
- 主链路的 `compute_frequency_array` job 可能仍为 `succeeded`，随后 `publish_tiles` 可以继续成功。
- `q_down` 入库和 q_down 展示发布不受该缺表直接阻断。

处理边界：

- 不要为了消除告警手动把 run 状态改成 `frequency_done`。
- 正确修复是补齐 schema/migration 或调整 frequency 质量记录写入逻辑。
- 在修复前，判断主展示是否可用以 `publish_tiles=succeeded`、`hydro.river_timeseries` 覆盖和 published logs/tiles 为准。

### 8.2 IFS `SlowDown`

现象：IFS availability probe 或 download 日志中出现 AWS/ECMWF `SlowDown` / HTTP 503。

影响：

- 可能拖慢 source discovery 或 download。
- 通常会 fallback 到其他 open-data source 或下一次 pass 继续。

处理：

- 先看是否已进入 `download_source_cycle` 和 Slurm 是否仍在跑。
- 若只是 probe 慢，不要误判为 scheduler 停。

### 8.3 `/ghdc` 与计算节点边界

事实：

- node-22 能访问 `/ghdc/data/nwm/published`。
- Slurm 计算节点不应假设能访问 `/ghdc`。
- 计算中间态必须在 `/scratch/frd_muziyao/nhms-prod/workspace` 和 `/scratch/frd_muziyao/nhms-prod/object-store`。

处理：

- 如果作业因 `/ghdc` 路径不可见失败，说明运行根配置错了。
- publish/copyback 到 `/ghdc` 应发生在控制面可见路径上，不应把 `/ghdc` 当作 sbatch runtime root。

### 8.4 Heihe 底图和 DB 范围混用

当前 DB 注册的 Heihe 使用 `/volume/data/nwm/Basins/heihe/input/heihe/gis/*`，范围覆盖下游额济纳。
前端静态底图脚本历史上可能读取仓库 `SHUD/input/heihe/gis/*`，范围偏上游祁连。

处理：

- DB/业务模型注册以 `/volume/data/nwm/Basins/...` 为准。
- 静态底图产物需要改成同一 source-of-truth 或明确标注历史/诊断来源。

### 8.5 Heihe 河段两层模型

Heihe DB 河网有两层：

- GIS 展示段：4759 条，`shud_output_river=false`
- SHUD 输出段：2352 条，`shud_output_river=true`

`hydro.river_timeseries.q_down` 只直接挂在 2352 条 SHUD 输出段上。GIS 段通过 `properties_json->>'iRiv'`
映射到输出段。前端/API 若拿 4759 条 GIS 段直接查时序，会表现为“部分河段没有流量”。

## 9. 值守 SQL 片段

最新 cycle：

```sql
select cycle_id, source_id, cycle_time, status, retry_count,
       coalesce(error_code,''), left(coalesce(error_message,''),120)
from met.forecast_cycle
order by cycle_time desc, source_id
limit 20;
```

最新 job：

```sql
select job_id, cycle_id, job_type, status, slurm_job_id,
       submitted_at, started_at, finished_at, updated_at,
       coalesce(error_code,''), left(coalesce(error_message,''),140)
from ops.pipeline_job
order by submitted_at desc nulls last
limit 80;
```

最新可展示 q_down 覆盖：

```sql
select run_id, variable, count(*) as rows,
       count(distinct river_segment_id) as segments,
       min(valid_time), max(valid_time)
from hydro.river_timeseries
where variable='q_down'
group by run_id, variable
order by max(valid_time) desc
limit 20;
```

Heihe 河段层：

```sql
select coalesce(properties_json->>'shud_output_river','false') as shud_output_river,
       count(*) as n
from core.river_segment
where river_network_version_id='basins_heihe_rivnet_vbasins'
group by 1
order by 1;
```

## 10. 相关文档

- [`two-node-deployment-overview.md`](two-node-deployment-overview.md)：22/27 职责、发布面、只读展示边界。
- [`two-node-production-e2e-plan.md`](two-node-production-e2e-plan.md)：E2E 验收证据和两节点检查项。
- [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)：历史 bring-up 和早期踩坑；不要直接当当前状态。
- [`node-27-bringup-checklist.md`](node-27-bringup-checklist.md)：27 只读展示侧启动检查。
- [`production-service-config.md`](production-service-config.md)：生产配置模板。
