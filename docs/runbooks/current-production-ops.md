# Current Production Operations Runbook

最后更新：2026-06-22

适用范围：node-27 active DB + ingest + display，node-22 Slurm/SHUD compute，
以及两者共享的 NFS object-store/published 数据面。

本文是当前生产值守手册。物理部署事实以
[`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md) 的 "Current physical deployment"
段为准；[`two-node-deployment-overview.md`](two-node-deployment-overview.md)
保留为两节点 role contract 和设计意图背景，不作为当前 host 分配的操作手册。

历史 bring-up 记录见 [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)。

## 1. 当前结论

- node-27 是当前 active production service host：本机 PostgreSQL `:55432`、
  cron-driven ingest、display API 和前端公网入口都在 27。
- node-27 每 10 分钟通过 cron 调用
  `/home/nwm/NWM/scripts/node27_autopipe_cron.sh`，再运行
  `scripts/node27_autopipeline.py` 扫描 NFS object-store、注册/解析 run、
  入库并刷新 display coverage。
- node-27 display API 由 `scripts/ops/start-display-api.sh` 管理，
  当前监听 `127.0.0.1:8080`；公网入口是 `https://test.nwm.ac.cn`。
- node-22 是计算与 Slurm host：运行 Slurm Gateway、诊断 API、Slurm/SHUD
  wrapper，并向 NFS 写 object-store/published 产物；node-22 不作为当前 NHMS
  业务数据库 writer。
- 完整 forcing 包和 SHUD run 输出的共享真相源是
  `object-store/forcing/...` 与 `object-store/runs/...`；`published/`
  只放 display products、tiles、logs、manifests。
- node-22 看到共享数据面为 `/ghdc/data/nwm/...`；node-27 看到同一份 NFS
  数据为 `/home/ghdc/nwm/...`。

## 2. 节点和服务

| 面 | 位置 | 当前职责 | 关键入口 |
| --- | --- | --- | --- |
| node-27 DB | node-27 `127.0.0.1:55432/nhms` | active PostgreSQL/PostGIS/TimescaleDB | writer `DATABASE_URL` from node-27 ingest env; display uses readonly `display.env` only |
| node-27 ingest | node-27 `/home/nwm/NWM` | 扫描 object-store runs、seed registry、register、parse、publish、refresh coverage | `infra/env/node27-ingest.env` -> `scripts/node27_autopipe_cron.sh` -> `scripts/node27_autopipeline.py` |
| node-27 display API | node-27 `127.0.0.1:8080` | display_readonly FastAPI, `/health`, `/api/v1/*`, frontend backend | `scripts/ops/start-display-api.sh` |
| node-27 public entry | `https://test.nwm.ac.cn` | nginx reverse proxy to local display API | `/etc/nginx/conf.d/test.nwm.ac.cn.conf` |
| node-22 compute | node-22 `/scratch/frd_muziyao/NWM` | Slurm Gateway、diagnostic API、Slurm/SHUD compute wrapper | `python -m services.slurm_gateway`, Slurm jobs |
| Shared NFS data | 22 `/ghdc/data/nwm`, 27 `/home/ghdc/nwm` | object-store mirror, published artifacts, Basins source data | NFS mount, no rsync step |

node-22 may still expose old local database processes for historical reasons.
Do not use node-22 local PostgreSQL as current NHMS production state. Current
database checks and ingest/write checks belong on node-27 against `:55432`.

## 3. 如何拉起和确认服务

### 3.1 调度器 / ingest

当前生产 ingest 不是常驻 `plan-production` 进程。node-27 使用 cron 周期性启动
bounded autopipe pass：

```bash
ssh -p 32099 nwm@210.77.77.27
crontab -l | grep -F 'scripts/node27_autopipe_cron.sh'
```

期望存在类似条目：

```text
*/10 * * * * /home/nwm/NWM/scripts/node27_autopipe_cron.sh >> /home/nwm/autopipe.log 2>&1
```

查看 wrapper 和最近运行结果：

```bash
cd /home/nwm/NWM
sed -n '1,180p' scripts/node27_autopipe_cron.sh \
  | sed -E 's#^(export DATABASE_URL=).*#\1<redacted>#'
tail -n 160 /home/nwm/autopipe.log
```

正常现象：

- 日志中每 10 分钟出现 `autopipe: start` 与 `autopipe: done rc=0`。
- JSON summary 包含 `object_store_root=/home/ghdc/nwm/object-store`、
  discovered/ingested/already_ingested runs、seeded/already_seeded basins。
- `coverage backstop (--all --skip-fresh)` 可刷新或跳过 display coverage；
  该步骤非 fatal，不应掩盖 autopipe 主返回码。

确认 node-27 只按 bounded cron 模式运行，并且 node-22 没有残留的 active
production scheduler/writer：

```bash
pgrep -af 'node27_[a]utopipeline|node27_[a]utopipe' || true

ssh -p 32099 frd_muziyao@210.77.77.22 \
  'pgrep -af "services[.]orchestrator|[p]lan-production" || true'
```

The node-22 scheduler check is expected to print nothing.

如果没有长驻 `node27_autopipeline.py` 进程但 cron 日志持续刷新，这是正常的
bounded cron 模式，不代表 ingest 停摆。

### 3.2 Slurm Gateway

Slurm Gateway 当前仍在 node-22。它负责把调度/诊断请求转成 Slurm 行为；
node-27 display 不调用 Slurm Gateway。

确认 node-22 Gateway 与诊断 API：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
pgrep -af '[s]ervices.slurm_gateway|uvicorn apps[.]api[.]main'
ss -ltnp 2>/dev/null | grep -E ':(8000|8001)\b' || true
curl -fsS --max-time 2 http://127.0.0.1:8001/health
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"
```

2026-06-22 现场验证：

- `python -m services.slurm_gateway` 在 node-22 运行。
- node-22 diagnostic API `/health` 在 `:8001` 返回 `{"status":"ok",...}`。
- node-22 `/ghdc/data/nwm/object-store` 与 `/ghdc/data/nwm/published`
  可见，是 node-27 `/home/ghdc/nwm/...` 的同一份 NFS 数据面。

### 3.3 API / 展示服务

node-27 display API 通过仓库 wrapper 管理：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
bash scripts/ops/start-display-api.sh
```

wrapper 会：

- source `infra/env/display.env`；
- 校验 `DATABASE_URL`、`NHMS_ENABLE_LIVE_POSTGIS_MVT`、`OBJECT_STORE_ROOT`；
- 创建并校验 `NHMS_MVT_FILE_CACHE_DIR`，未设置时默认 `$HOME/.cache/nhms/mvt`；
- 停掉旧的 `apps.api.main:app` uvicorn；
- 在 `127.0.0.1:${NHMS_DISPLAY_API_PORT:-8080}` 重新启动；
- 跑 `/health` 与 `/api/v1/models?limit=1` basin_id smoke check。

确认当前 live 状态：

```bash
cd /home/nwm/NWM
grep -E '^NHMS_DISPLAY_API_PORT=|^NHMS_SERVICE_ROLE=|^OBJECT_STORE_ROOT=' \
  infra/env/display.env

if grep -q '^DATABASE_URL=' infra/env/display.env; then
  printf 'DATABASE_URL=<set redacted>\n'
else
  printf 'DATABASE_URL=<missing>\n'
fi

pgrep -af 'uvicorn apps[.]api[.]main'
ss -ltnp 2>/dev/null | grep -E ':(55432|8080)\b'
curl -fsS --max-time 5 http://127.0.0.1:8080/health
curl -fksS --max-time 5 https://test.nwm.ac.cn/health
```

2026-06-22 现场修正过一次 display port drift：`display.env` 曾设置
`NHMS_DISPLAY_API_PORT=8000`，而 nginx 与仓库模板期望 `8080`。已备份原文件并
改回 `8080`，随后 `scripts/ops/start-display-api.sh` smoke check 和 public
`https://test.nwm.ac.cn/health` 均返回 `ok`。后续若公网 502，先同时检查本地
`127.0.0.1:8080/health`、nginx `proxy_pass` 和 `NHMS_DISPLAY_API_PORT`。

### 3.4 监控快照

node-27 ingest 侧优先看 autopipe 日志和 DB/run coverage：

```bash
ssh -p 32099 nwm@210.77.77.27
tail -n 200 /home/nwm/autopipe.log

cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;"
```

If the host-provisioned `infra/env/node27-ingest.env` is absent, treat ingest
writer checks as blocked and fix the ingest env. Do not fall back to
`infra/env/display.env`; that file is display_readonly runtime config only.

node-22 compute 侧优先看 Slurm queue、Gateway、shared NFS 输出：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"
pgrep -af '[s]ervices.slurm_gateway'
find /ghdc/data/nwm/object-store/runs -maxdepth 1 -type d \
  -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort | tail -20
```

## 4. 业务流程

当前物理流程按数据面理解：

```text
node-22 / Slurm
  -> produces forcing and SHUD run artifacts
  -> writes shared NFS object-store/published roots
node-27 cron autopipe
  -> scans /home/ghdc/nwm/object-store/runs
  -> seeds basin registry when needed
  -> registers/mirrors/parses runs
  -> writes node-27 PostgreSQL :55432
  -> refreshes display coverage and publish status
node-27 display
  -> reads PostgreSQL :55432 and NFS object-store/published
  -> serves /, /ops, /api/v1/* through https://test.nwm.ac.cn
```

`scripts/node27_autopipeline.py` is idempotent. Already-seeded basins and
already-ingested runs are skipped, so cron re-runs are expected and cheap.
One run failure should appear in the JSON summary without aborting unrelated
run discovery.

## 5. 产物位置

### 5.1 数据库

当前 active NHMS DB 在 node-27 本机 `127.0.0.1:55432/nhms`。display API uses a
readonly role from `infra/env/display.env`; cron ingest uses writer credentials
from the node-27 ingest env, normally `infra/env/node27-ingest.env`.

Secret-safe DB checks:

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a

psql "$DATABASE_URL" -P pager=off -Atc "
select current_database(), current_user, inet_server_addr(), inet_server_port();"

psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;"
```

Common tables:

| Schema / table | 用途 |
| --- | --- |
| `hydro.hydro_run` | 每个 source/model/basin 的水文 run 状态 |
| `hydro.river_timeseries` | q_down 等河段时序 |
| `hydro.run_display_coverage` | latest display fast path coverage |
| `met.forecast_cycle` | source cycle 状态 |
| `met.forcing_version` | forcing 包索引 |
| `ops.pipeline_job` | 阶段 job 状态 |
| `core.basin_version` / `core.river_segment` | 流域、河段、几何和输出段 |
| `map.tile_layer` | 发布图层登记 |

### 5.2 Workspace 和运行日志

node-27 ingest wrapper/log:

```text
/home/nwm/NWM/scripts/node27_autopipe_cron.sh
/home/nwm/NWM/scripts/node27_autopipeline.py
/home/nwm/autopipe.log
/home/nwm/autopipe-work/
```

node-22 compute workspace/log roots remain compute-side operational paths:

```text
/scratch/frd_muziyao/NWM
/scratch/frd_muziyao/nhms-prod/workspace/
/scratch/frd_muziyao/nhms-prod/object-store/
/scratch/frd_muziyao/nhms-prod/runtime/
```

Use node-22 paths for Slurm/job runtime troubleshooting. Use node-27 paths for
DB/display/ingest troubleshooting.

### 5.3 Object-store mirror

Complete forcing packages and run outputs live under shared object-store:

```text
node-22 view: /ghdc/data/nwm/object-store
node-27 view: /home/ghdc/nwm/object-store

forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/
runs/<run_id>/
```

Check current visibility from both hosts:

```bash
# node-22
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'stat -c "%n %A %U:%G" /ghdc/data/nwm/object-store &&
   find /ghdc/data/nwm/object-store/runs -maxdepth 1 -type d \
     -printf "%TY-%Tm-%Td %TH:%TM %p\n" | sort | tail -20'

# node-27
ssh -p 32099 nwm@210.77.77.27 \
  'stat -c "%n %A %U:%G" /home/ghdc/nwm/object-store &&
   find /home/ghdc/nwm/object-store/runs -maxdepth 1 -type d \
     -printf "%TY-%Tm-%Td %TH:%TM %p\n" | sort | tail -20'
```

### 5.4 Published artifacts

Display products, tiles, manifests, and logs live under `published/`:

```text
node-22 view: /ghdc/data/nwm/published
node-27 view: /home/ghdc/nwm/published

published/logs/<source>/<YYYYMMDDHH>/...
published/tiles/hydro/<source>_<YYYYMMDDHH>/...
published/manifests/...
```

Do not look under `published/` for complete SHUD `runs/<run_id>/output`.
Those belong under `object-store/runs/<run_id>/`.

Checks:

```bash
# node-22
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'test -d /ghdc/data/nwm/published &&
   stat -c "%n %A %U:%G" /ghdc/data/nwm/published &&
   find /ghdc/data/nwm/published/logs /ghdc/data/nwm/published/tiles \
     -maxdepth 4 -type f -printf "%TY-%Tm-%Td %TH:%TM %p\n" 2>/dev/null |
   sort | tail -40'

# node-27
ssh -p 32099 nwm@210.77.77.27 \
  'test -d /home/ghdc/nwm/published &&
   stat -c "%n %A %U:%G" /home/ghdc/nwm/published &&
   find /home/ghdc/nwm/published/logs /home/ghdc/nwm/published/tiles \
     -maxdepth 4 -type f -printf "%TY-%Tm-%Td %TH:%TM %p\n" 2>/dev/null |
   sort | tail -40'

ssh -p 32099 nwm@210.77.77.27 \
  'find /home/ghdc/nwm/published -path "*/runs/*" -o -path "*/forcing/*"'
```

The second command should normally print nothing. If full `runs/` or `forcing/`
payloads appear under `published/`, the publication boundary is wrong.

### 5.5 Basins source data

node-27 autopipe seeds/refreshes basin registry from:

```text
/home/ghdc/nwm/Basins
```

Check:

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'stat -c "%n %A %U:%G" /home/ghdc/nwm/Basins &&
   find /home/ghdc/nwm/Basins -maxdepth 2 -type d | sort | head -40'
```

## 6. 如何判断是否卡住

先分清三种状态：

- 正常运行：node-22 Slurm 有 active job，或 node-27 autopipe 正在本轮 ingest；
  `/home/nwm/autopipe.log` 周期性刷新。
- 等下一 cron tick：Slurm queue 空，autopipe 最近一轮 `rc=0`，DB 中没有新的
  un-ingested runs。
- 真实卡住：autopipe 多轮非 0、同一 run 反复 failed，public `/health` 失败，
  或 node-22 Slurm terminal 后 shared object-store/published 不更新。

推荐检查顺序：

```bash
date '+%F %T %Z'

# node-27 ingest/display
ssh -p 32099 nwm@210.77.77.27 \
  'tail -n 120 /home/nwm/autopipe.log &&
   curl -fsS --max-time 5 http://127.0.0.1:8080/health &&
   curl -fksS --max-time 5 https://test.nwm.ac.cn/health'

# node-22 compute
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R" &&
   pgrep -af "[s]ervices.slurm_gateway"'
```

If public health fails but local `127.0.0.1:8080/health` succeeds, inspect nginx
proxy target and certificates. If local health fails, restart with
`bash scripts/ops/start-display-api.sh` from `/home/nwm/NWM` and read
`/tmp/display-api.log`.

## 7. 当前运行口径

This section is a live snapshot, not a permanent fact. Refresh it during handoff.

2026-06-22 verification found:

- node-27 `node27_autopipe` cron active every 10 minutes.
- Recent `/home/nwm/autopipe.log` runs discovered 300 runs, ingested 4 new runs,
  published 4, and refreshed 4 display coverage rows.
- node-27 display API listens on `127.0.0.1:8080`; local and public `/health`
  both returned `ok` after port alignment.
- node-22 Slurm Gateway process is active; node-22 diagnostic API `/health` on
  `:8001` returned `ok`.

## 8. 当前已知卡点

### 8.1 Display port drift

Symptom:

- `http://127.0.0.1:8080/health` fails or public `https://test.nwm.ac.cn/health`
  returns 502.

Check:

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
grep -E '^NHMS_DISPLAY_API_PORT=' infra/env/display.env
ss -ltnp 2>/dev/null | grep -E ':(8080|8000)\b'
curl -fsS --max-time 5 http://127.0.0.1:8080/health
curl -fksS --max-time 5 https://test.nwm.ac.cn/health
```

Fix:

```bash
cd /home/nwm/NWM
bash scripts/ops/start-display-api.sh
```

If `display.env` disagrees with nginx, back up the env file first, align the
port, restart through the wrapper, and verify both local and public `/health`.

### 8.2 Autopipe ingest failures

Symptoms:

- `/home/nwm/autopipe.log` shows repeated non-zero rc.
- JSON summary has non-empty `failed_runs`.
- New `object-store/runs/fcst_*` directories exist but DB `hydro.hydro_run`
  does not advance.

Checks:

```bash
ssh -p 32099 nwm@210.77.77.27
tail -n 240 /home/nwm/autopipe.log
cd /home/nwm/NWM
bash scripts/node27_autopipe_cron.sh
```

The wrapper uses the same env defaults, log path, and non-overlap lock as cron.
It is idempotent; rerun manually only after reading the previous failure and
confirming no cron run is active.

### 8.3 `flood.run_product_quality` 缺表

Historical symptom:

```text
RETURN_PERIOD_FAILED
relation "flood.run_product_quality" does not exist
```

Impact:

- basin-level `frequency` 子任务失败；
- `hydro.hydro_run.status` may remain `parsed`;
- q_down ingestion and display can still be usable.

Boundary:

- Do not manually set run status to `frequency_done` to hide the issue.
- Judge display readiness with `hydro.river_timeseries`, published tiles/logs,
  and display coverage, not only frequency status.

### 8.4 `/ghdc` 与计算节点边界

Facts:

- node-22 can access `/ghdc/data/nwm/...`.
- Slurm compute nodes should not assume `/ghdc` is their runtime workspace.
- Compute intermediates belong under `/scratch/frd_muziyao/nhms-prod/...`;
  completed shared artifacts appear under `/ghdc/data/nwm/...` and then
  `/home/ghdc/nwm/...` on node-27.

If a Slurm job fails because `/ghdc` is missing, runtime roots are wrong. Fix
the compute-side workspace/object-store config rather than moving display paths
into sbatch runtime.

### 8.5 Heihe 底图和 DB 范围混用

Current DB registered Heihe data uses `/home/ghdc/nwm/Basins/...` on node-27.
Older static basemap scripts may have used repository-local fixtures with a
smaller extent. For live display and ingest, use the node-27 Basins source of
truth.

### 8.6 Heihe 河段两层模型

Heihe DB river network has GIS display segments and SHUD output segments.
`hydro.river_timeseries.q_down` attaches directly to SHUD output segments.
GIS segments map through `properties_json->>'iRiv'`. If an API/frontend query
uses GIS segment ids directly, some segments can appear to have no flow.

## 9. 值守 SQL 片段

Run these on node-27 after sourcing the ingest writer env
(`infra/env/node27-ingest.env` on the host, or an equivalent secret-safe
operator env). Do not source `infra/env/display.env` for writer/ingest SQL:
that file belongs to the display_readonly runtime.

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
```

Latest runs:

```sql
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), left(coalesce(error_message,''),120), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;
```

Latest q_down coverage:

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

Heihe river segment layers:

```sql
select coalesce(properties_json->>'shud_output_river','false') as shud_output_river,
       count(*) as n
from core.river_segment
where river_network_version_id='basins_heihe_rivnet_vbasins'
group by 1
order by 1;
```

## 10. 相关文档

- [`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md)：current physical
  deployment source of truth.
- [`two-node-deployment-overview.md`](two-node-deployment-overview.md)：role
  contract and design-intent background; read its top banner before using it.
- [`node-27-bringup-checklist.md`](node-27-bringup-checklist.md)：node-27
  display bring-up and live checks.
- [`display-readonly-live-mvt.md`](display-readonly-live-mvt.md)：display API
  restart and live MVT evidence.
- [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)：historical bring-up
  and early incident notes; not current topology.
