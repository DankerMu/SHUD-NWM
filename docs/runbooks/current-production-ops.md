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
- 完整业务 forcing 包和 SHUD run 输出的共享真相源是 object-store `forcing/...`、`runs/...`，不是 `published/`。
- 对 27 展示可见的 `published/` 只放 tiles、logs、display manifests；22 路径是 `/ghdc/data/nwm/published`，27 本机对应 `/home/ghdc/nwm/published`。
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
node-22 OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
Slurm/container OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store

/scratch/frd_muziyao/nhms-prod/object-store/
  canonical/<source>/<YYYYMMDDHH>/<variable>/*.nc
  forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/forcing_package.json
  runs/<run_id>/
  tiles/...
```

URI 口径通常使用 `s3://nhms/...`，但当前本地落盘根是上面的 `/scratch/.../object-store`。
不要把 `/scratch/frd_muziyao/nhms-prod/object-store` 删除；它是计算节点可见的生产 staging 和对象存储落盘根。

### 5.4 Shared Object Store Copyback

完整 forcing 包和水文 run 产物不放在 `published/` 下。`s3://nhms/forcing/...` 和
`s3://nhms/runs/<run_id>/...` 当前先落在 `OBJECT_STORE_ROOT`，再由 22 控制面的 publish/copyback
阶段同步到共享对象存储镜像：

```text
node-22 NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
node-27 shared object-store mirror=/home/ghdc/nwm/object-store

22 host forcing: /ghdc/data/nwm/object-store/forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/
27 host forcing: /home/ghdc/nwm/object-store/forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/
URI key: forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/

22 host runs: /ghdc/data/nwm/object-store/runs/<run_id>/
27 host runs: /home/ghdc/nwm/object-store/runs/<run_id>/
URI key: runs/<run_id>/
```

当前生产环境通过 `NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store` 启用 copyback。
如果该同步失败，`publish` 阶段应失败，而不是只发布 tiles 后把完整 run 产物留在 22 私有 staging。
历史已发布 q_down run 如果缺少对应 `forcing/...` 包，按
[`forcing-copyback-backfill.md`](forcing-copyback-backfill.md) 在 node-22 先 dry-run、再显式 `--apply`
补拷；不要通过手动修改 `hydro`/`met` 状态来绕过缺包。

检查最新 run 产物：

```bash
find /ghdc/data/nwm/object-store/forcing -maxdepth 5 -type f \
  \( -name 'forcing_package.json' -o -name 'manifest.json' \) \
  -printf '%TY-%Tm-%Td %TH:%TM %M %u %g %p\n' | sort | tail -30

find /ghdc/data/nwm/object-store/runs -maxdepth 2 -type d -name 'fcst_*20260613*' \
  -printf '%TY-%Tm-%Td %TH:%TM %M %u %g %p\n' | sort | tail -30

ls -la /ghdc/data/nwm/object-store/runs/fcst_gfs_2026061312_basins_heihe_shud/output/
```

### 5.5 Published Artifacts

当前 22 写、27 读的发布面：

```text
node-22 NHMS_PUBLISHED_ARTIFACT_ROOT=/var/lib/nhms/published
node-22 published host mount: /ghdc/data/nwm/published
node-27 published host mount: /home/ghdc/nwm/published
container: /var/lib/nhms/published
URI prefix: published://
```

目录形态：

```text
/ghdc/data/nwm/published/
  logs/<source>/<YYYYMMDDHH>/cycle_<source>_<YYYYMMDDHH>/job_*.out
  tiles/hydro/<source>_<YYYYMMDDHH>/q-down/...
  manifests/... display manifests only
```

`published/` 只承载展示发布物、瓦片 manifest 和日志；不要在这里查完整 SHUD `runs/<run_id>/output/`。
完整 forcing 包和 run 产物见第 5.4 节的 `/ghdc/data/nwm/object-store/forcing/` 和
`/ghdc/data/nwm/object-store/runs/`。

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

### 5.6 单个业务 run 验收元组

以下检查绑定同一个输入元组 `<source_id, cycle_time, basin_version_id, model_id, run_id>`。示例先设置变量：

```bash
source_id=gfs
cycle_time='2026-06-13 12:00:00+00'
cycle_key=2026061312
basin_version_id=basins_heihe_vbasins
model_id=basins_heihe_shud
run_id=fcst_gfs_2026061312_basins_heihe_shud
previous_cycle_id=gfs_2026061300
object_store_mirror_root=${NHMS_OBJECT_STORE_COPYBACK_ROOT:-/ghdc/data/nwm/object-store}
object_store_uri_prefix=${OBJECT_STORE_PREFIX:-s3://nhms-prod}

normalize_object_key() {
  uri="${1%/}"
  prefix="${object_store_uri_prefix%/}"
  if [ -n "$prefix" ] && [ "${uri#"$prefix"/}" != "$uri" ]; then
    printf '%s\n' "${uri#"$prefix"/}"
    return 0
  fi
  case "$uri" in
    forcing/*|runs/*|states/*) printf '%s\n' "$uri" ;;
    *)
      printf >&2 'unexpected object-store URI/key: %s\n' "$uri"
      return 1
      ;;
  esac
}
```

`met.forcing_version` 没有 `status` 字段；这里用真实谓词判断 forcing 包是否可用：`checksum` 非空，且
`forcing_package_uri` 可规范化为 object-store `forcing/...` key。URI 可以是相对 key（`forcing/...`），也可以是带
`OBJECT_STORE_PREFIX` 的形式（例如 `s3://nhms-prod/forcing/...`）：

```bash
psql "$DATABASE_URL" -P pager=off -F $'\t' -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" \
  -v object_store_uri_prefix="$object_store_uri_prefix" -Atc "
select forcing_version_id, source_id, cycle_time, model_id,
       forcing_package_uri,
       nullif(trim(coalesce(checksum, '')), '') is not null as checksum_present,
       (
         forcing_package_uri like 'forcing/%'
         or (
           :'object_store_uri_prefix' <> ''
           and forcing_package_uri like rtrim(:'object_store_uri_prefix', '/') || '/forcing/%'
         )
       ) as forcing_uri_in_object_store_scope
from met.forcing_version
where source_id = :'source_id'
  and cycle_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
order by created_at desc
limit 5;"
```

期望：至少一行，且同一行 `checksum_present=t`、`forcing_uri_in_object_store_scope=t`。`checksum` 为空表示 producer
尚未 finalization，不能把它当成 ready。对象存储检查先把 URI 规范化为相对 key，再检查 package manifest：

```bash
forcing_package_uri=$(psql "$DATABASE_URL" -P pager=off -At -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" \
  -v object_store_uri_prefix="$object_store_uri_prefix" -c "
select forcing_package_uri
from met.forcing_version
where source_id = :'source_id'
  and cycle_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
  and nullif(trim(coalesce(checksum, '')), '') is not null
  and (
    forcing_package_uri like 'forcing/%'
    or (
      :'object_store_uri_prefix' <> ''
      and forcing_package_uri like rtrim(:'object_store_uri_prefix', '/') || '/forcing/%'
    )
  )
order by created_at desc
limit 1;")
forcing_key=$(normalize_object_key "$forcing_package_uri")

test -f "$object_store_mirror_root/${forcing_key%/}/forcing_package.json" \
  || test -f "$object_store_mirror_root/${forcing_key%/}/manifest.json"
find "$object_store_mirror_root/${forcing_key%/}" -maxdepth 2 -type f -printf '%p\n' | sort | head -40
```

严格 warm-start 要求当前 cycle 的精确 successor checkpoint：`hydro.state_snapshot.valid_time = cycle_time`、
`lead_hours = 12`、同一 `source_id/model_id`、`usable_flag=true`。这行通常由上一 allowed cycle 的 run 在
`state_save_qc` 阶段产出；不要按上一 allowed cycle 的 `valid_time` 查 checkpoint，也不要用当前待启动 `run_id` 去判断
checkpoint producer：

```bash
psql "$DATABASE_URL" -P pager=off -F $'\t' -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" -Atc "
select state_id, source_id, valid_time, run_id as producer_run_id,
       cycle_id as producer_cycle_id, lead_hours, state_uri, usable_flag,
       nullif(trim(coalesce(checksum, '')), '') is not null as checksum_present
from hydro.state_snapshot
where source_id = :'source_id'
  and valid_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
  and lead_hours = 12
  and usable_flag is true
order by created_at desc
limit 5;"

state_uri=$(psql "$DATABASE_URL" -P pager=off -At -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" -c "
select state_uri
from hydro.state_snapshot
where source_id = :'source_id'
  and valid_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
  and lead_hours = 12
  and usable_flag is true
order by created_at desc
limit 1;")
state_key=$(normalize_object_key "$state_uri")
test -f "$object_store_mirror_root/$state_key"
find "$(dirname "$object_store_mirror_root/$state_key")" -maxdepth 1 -type f -printf '%p\n' | sort

state_producer_run_id=$(psql "$DATABASE_URL" -P pager=off -At -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" -c "
select run_id
from hydro.state_snapshot
where source_id = :'source_id'
  and valid_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
  and lead_hours = 12
  and usable_flag is true
order by created_at desc
limit 1;")
state_producer_cycle_id=$(psql "$DATABASE_URL" -P pager=off -At -v source_id="$source_id" \
  -v cycle_time="$cycle_time" -v model_id="$model_id" -c "
select coalesce(cycle_id, '')
from hydro.state_snapshot
where source_id = :'source_id'
  and valid_time = :'cycle_time'::timestamptz
  and model_id = :'model_id'
  and lead_hours = 12
  and usable_flag is true
order by created_at desc
limit 1;")
state_producer_cycle_id=${state_producer_cycle_id:-$previous_cycle_id}

psql "$DATABASE_URL" -P pager=off -F $'\t' \
  -v producer_run_id="$state_producer_run_id" \
  -v producer_cycle_id="$state_producer_cycle_id" -Atc "
select job_id, stage, status, slurm_job_id,
       coalesce(error_code,''), left(coalesce(error_message,''),140)
from ops.pipeline_job
where stage = 'state_save_qc'
  and (
    (:'producer_run_id' <> '' and run_id = :'producer_run_id')
    or (:'producer_cycle_id' <> '' and cycle_id = :'producer_cycle_id')
  )
order by updated_at desc
limit 5;"
```

期望：snapshot 查询至少一行，且 `lead_hours=12`、`usable_flag=t`、`checksum_present=t`，`state_uri` 文件存在。
`state_save_qc` job 应是 `succeeded`、`complete`、`completed` 或其他非 failed terminal 状态；如失败，先查修复
producer run / 上一 allowed cycle 的 state 文件、QC 结果、Slurm 日志，再重跑该 cycle 的 `state_save_qc`/后续链路。

调度、forcing、run 阶段不应有 failed terminal 状态：

```bash
psql "$DATABASE_URL" -P pager=off -F $'\t' -v run_id="$run_id" -v source_id="$source_id" \
  -v cycle_key="$cycle_key" -Atc "
select job_id, stage, job_type, status, slurm_job_id,
       coalesce(error_code,''), left(coalesce(error_message,''),140)
from ops.pipeline_job
where (run_id = :'run_id' or cycle_id = lower(:'source_id') || '_' || :'cycle_key')
  and stage in ('download','convert','forcing','forecast','parse','state_save_qc','frequency','publish')
order by submitted_at nulls last, updated_at;"
```

对象存储 run 输出和 scheduler evidence：

```bash
test -f "$object_store_mirror_root/runs/${run_id}/input/manifest.json"
find "$object_store_mirror_root/runs/${run_id}/output" -maxdepth 2 -type f -printf '%p\n' | sort | head -40

grep -R "\"allowed_cycle_hours_utc\"\\|\"cycle_hour_not_allowed\"\\|\"cycle_time_utc\"" \
  /scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence | tail -80
```

published 面只验展示产物：

```bash
find /ghdc/data/nwm/published/logs /ghdc/data/nwm/published/tiles \
  -type f \( -name '*.out' -o -name '*.err' -o -name '*.json' -o -name '*.pmtiles' -o -name '*.pbf' \) \
  -path "*${source_id}*" -print | tail -80

find /ghdc/data/nwm/published -path '*/runs/*' -o -path '*/forcing/*'
```

最后一个命令期望无输出；如果 `published/forcing` 或 `published/runs` 出现完整业务包，说明发布边界配置错了。

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

### 8.2 `flood.return_period_result` 索引和空间回收

#490 清理 no-curve 空行后，只是减少逻辑行数；PostgreSQL/TimescaleDB 不会因此自动把表文件、chunk 文件或索引文件空间还给文件系统。`flood.return_period_result` 的索引精简、`REINDEX`、`VACUUM FULL`、`pg_repack`、chunk rebuild 或 Timescale 压缩都必须作为单独维护窗口处理，不能放进应用启动、普通 migration、CI 或调度器 pass 自动执行。

先只生成审计证据和手工 SQL：

```bash
cd /scratch/frd_muziyao/NWM
set -a
source infra/env/compute.host.env
set +a

uv run --no-sync python scripts/audit_return_period_indexes.py \
  --connection-mode readonly \
  --report-out /scratch/frd_muziyao/nhms-prod/workspace/db-maintenance/return-period-index-audit.json \
  --manual-sql-out /scratch/frd_muziyao/nhms-prod/workspace/db-maintenance/return-period-index-maintenance.manual.sql
```

该工具必须连接 live DB；未提供 `DATABASE_URL` 时应非 0 退出且不写 report/manual SQL。`--report-out`
和 `--manual-sql-out` 也必须是两个不同路径，即使加 `--overwrite` 也不能复用同一文件。

审计报告必须包含：

- `flood.return_period_result` root relation/table/index/total size。
- root index inventory、`pg_get_indexdef`、`pg_stat_user_indexes` 使用计数。
- live DB 模式下 root relation、index inventory、root index usage 任一失败都必须阻断并返回非 0；Timescale metadata 可降级，但报告必须明确记录 unavailable reason。
- Timescale chunk/chunk-index size、chunk index usage，以及各 chunk section 的 total/observed/limit/truncated 元数据；不能把被截断或 unavailable 的 chunk 证据当作完整证据。
- summary、ranking/segments、timeline、GeoJSON fallback tile、MVT selected identity、valid-time discovery、TilePublisher readiness、latest-ready-run quality behavior 的 `EXPLAIN (ANALYZE, BUFFERS)` 模板。
- `return_period_result_null_return_period_run_idx`、`return_period_result_null_warning_level_run_idx` 等 NULL partial index 只可标为 drop/investigate 候选，不能无证据静默删除。

维护窗口前后都要保存同一组证据；manual SQL 产物会内置这组 before/after 查询，包含 root table/index
和 Timescale chunk/chunk-index/usage 证据：

```sql
select current_database() as database_name,
       pg_size_pretty(pg_database_size(current_database())) as database_size;

select pg_size_pretty(pg_relation_size('flood.return_period_result'::regclass)) as table_size,
       pg_size_pretty(pg_indexes_size('flood.return_period_result'::regclass)) as indexes_size,
       pg_size_pretty(pg_total_relation_size('flood.return_period_result'::regclass)) as total_size;

select idx.indexrelid::regclass::text as index_name,
       pg_size_pretty(pg_relation_size(idx.indexrelid)) as index_size,
       pg_get_indexdef(idx.indexrelid) as indexdef
from pg_index idx
where idx.indrelid = 'flood.return_period_result'::regclass
order by pg_relation_size(idx.indexrelid) desc;
```

执行边界：

- 使用 writer 连接前必须有明确维护窗口和人工审批。
- 设置短 `lock_timeout`，失败时 `ROLLBACK`，先查 `pg_locks`/`pg_stat_activity`，不要在业务高峰反复重试。
- `DROP INDEX CONCURRENTLY` / `REINDEX CONCURRENTLY` 不能放在 transaction block 内；普通 `DROP INDEX` 可进事务但锁更重，必须逐条审查。
- 如果热路径 `EXPLAIN` 退化，停止后续索引变更，用报告中的 `pg_get_indexdef` 或原迁移 SQL 先恢复相关索引，再复测。

### 8.3 IFS `SlowDown`

现象：IFS availability probe 或 download 日志中出现 AWS/ECMWF `SlowDown` / HTTP 503。

影响：

- 可能拖慢 source discovery 或 download。
- 通常会 fallback 到其他 open-data source 或下一次 pass 继续。

处理：

- 先看是否已进入 `download_source_cycle` 和 Slurm 是否仍在跑。
- 若只是 probe 慢，不要误判为 scheduler 停。

### 8.4 `/ghdc` 与计算节点边界

事实：

- node-22 能访问 `/ghdc/data/nwm/published`。
- Slurm 计算节点不应假设能访问 `/ghdc`。
- 计算中间态必须在 `/scratch/frd_muziyao/nhms-prod/workspace` 和 `/scratch/frd_muziyao/nhms-prod/object-store`。

处理：

- 如果作业因 `/ghdc` 路径不可见失败，说明运行根配置错了。
- publish/copyback 到 `/ghdc` 应发生在控制面可见路径上，不应把 `/ghdc` 当作 sbatch runtime root。

### 8.5 Heihe 底图和 DB 范围混用

当前 DB 注册的 Heihe 使用 `/volume/data/nwm/Basins/heihe/input/heihe/gis/*`，范围覆盖下游额济纳。
前端静态底图脚本历史上可能读取仓库 `SHUD/input/heihe/gis/*`，范围偏上游祁连。

处理：

- DB/业务模型注册以 `/volume/data/nwm/Basins/...` 为准。
- 静态底图产物需要改成同一 source-of-truth 或明确标注历史/诊断来源。

### 8.6 Heihe 河段两层模型

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
