# Current Production Operations Runbook

最后更新：2026-08-07

适用范围：node-27 active DB + ingest + display，node-22 Slurm/SHUD compute，
以及两者共享的 NFS object-store/published 数据面。

> **node-22 维护窗口前执行说明**：node-22 活动 checkout 的共享 `.venv` 在
> 运维批准的维护窗口前保持 Python 3.12.7，**禁止**在其中运行裸 `uv run` /
> `uv sync`（会在 3.11 pin 下重建环境，已实测会打断成半拆状态）。本文中所有
> node-22 活动操作一律使用精确活动解释器
> `/scratch/frd_muziyao/NWM/.venv/bin/python`（控制台入口用 `-m services.orchestrator.cli`）；
> node-27 命令与孤立 rollback checkout 的 `uv sync` 不受此限。

本文是当前生产值守手册。物理部署事实以
[`ROLE_BOUNDARY.md`](../governance/ROLE_BOUNDARY.md) 的 "Current physical deployment"
段为准；[`two-node-deployment-overview.md`](two-node-deployment-overview.md)
保留为两节点 role contract 和设计意图背景，不作为当前 host 分配的操作手册。

历史 bring-up 记录见 [`qhh-22-business-bringup.md`](qhh-22-business-bringup.md)。

## 1. 当前结论

- node-27 是当前 active production service host：本机 PostgreSQL `:55432`、
  source download、systemd-driven ingest、display API 和前端公网入口都在 27。
- node-27 source download 由用户级 systemd timer
  `nhms-node27-download.timer` 驱动，调用
  `scripts/node27_download_once.sh`，自动选择 00/12 UTC 业务 cycle 并把 raw
  manifest 写入共享 NFS object-store。
- node-27 每 10 分钟通过用户级 systemd timer
  `nhms-node27-autopipe.timer` 调用
  `/home/nwm/NWM/scripts/node27_autopipe_cron.sh`，再运行
  `scripts/node27_autopipeline.py` 扫描 NFS object-store、注册/解析 run、
  入库并刷新 display coverage。生产配置用两个独立 run worker 并行处理，
  每个 worker 使用独立数据库事务；所有 run 收敛后只执行一次最终 display publish。
- node-27 display API 由 user systemd `nhms-display-api.service` 托管，
  `scripts/ops/start-display-api.sh` 负责安装权威 unit、平滑接管和 smoke check；
  当前监听 `127.0.0.1:8080`，默认 2 workers；公网入口是 `https://test.nwm.ac.cn`。
- node-22 是计算与 Slurm host：运行 Slurm Gateway、诊断 API、DB-free
  production scheduler timer、Slurm/SHUD wrapper，并向 NFS 写
  object-store/published 产物；node-22 不作为当前 NHMS 业务数据库 writer。
- 完整 forcing 包和 SHUD run 输出的共享真相源是
  `object-store/forcing/...` 与 `object-store/runs/...`；`published/`
  只放 display products、tiles、logs、manifests。
- node-22 看到共享数据面为 `/ghdc/data/nwm/...`；node-27 看到同一份 NFS
  数据为 `/home/ghdc/nwm/...`。

## 2. 节点和服务

| 面 | 位置 | 当前职责 | 关键入口 |
| --- | --- | --- | --- |
| node-27 DB | node-27 `127.0.0.1:55432/nhms` | active PostgreSQL/PostGIS/TimescaleDB | writer `DATABASE_URL` from node-27 ingest env; display uses readonly `display.env` only |
| node-27 download | node-27 `/home/nwm/NWM` | 自动下载 GFS/IFS 00/12 UTC raw source cycles 到共享 object-store | `infra/env/node27-download.env` -> `nhms-node27-download.timer` -> `scripts/node27_download_once.sh` |
| node-27 ingest | node-27 `/home/nwm/NWM` | 扫描 object-store runs、seed registry、register、parse、publish、refresh coverage | `infra/env/node27-ingest.env` -> `nhms-node27-autopipe.timer` -> `scripts/node27_autopipe_cron.sh` -> `scripts/node27_autopipeline.py` |
| node-27 display API | node-27 `127.0.0.1:8080` | display_readonly FastAPI, `/health`, `/api/v1/*`, frontend backend | `infra/systemd/nhms-display-api.service` -> `scripts/ops/start-display-api.sh` |
| node-27 public entry | `https://test.nwm.ac.cn` | nginx reverse proxy to local display API | `/etc/nginx/conf.d/test.nwm.ac.cn.conf` |
| node-22 compute | node-22 `/scratch/frd_muziyao/NWM` | Slurm Gateway、diagnostic API、DB-free scheduler、Slurm/SHUD compute wrapper | `nhms-compute-scheduler.timer`, `/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway`, Slurm jobs |
| Shared NFS data | 22 `/ghdc/data/nwm`, 27 `/home/ghdc/nwm` | object-store mirror, published artifacts, Basins source data | NFS mount, no rsync step |

Node-22 historical PostgreSQL `:55433` was archived and stopped on 2026-06-29
and is retained only as an explicit rollback archive. Do not use node-22 local
PostgreSQL as current NHMS production state. Current database checks and
ingest/write checks belong on node-27 against `:55432`.

## 3. 如何拉起和确认服务

### 3.1 下载 / 调度器 / ingest

node-27 source download 使用用户级 systemd timer。它不使用 display env，不连
node-22 DB；未显式设置 `NODE27_DOWNLOAD_CYCLE_TIME` 时自动选择最近的 00/12 UTC
业务 cycle：

```bash
ssh -p 32099 nwm@210.77.77.27
systemctl --user status nhms-node27-download.timer nhms-node27-download.service --no-pager
tail -n 160 /home/nwm/node27-download-logs/download.log
```

期望：

- `nhms-node27-download.timer` 为 `active (waiting)`。
- `infra/env/node27-download.env` mode 为 `0600`。
- 下载 summary 的 `cycle_time_selection` 为 `automatic`，cycle hour 在 `0,12`
  之内。

node-27 ingest 使用用户级 systemd timer 周期性启动 bounded autopipe pass：

```bash
ssh -p 32099 nwm@210.77.77.27
systemctl --user status nhms-node27-autopipe.timer nhms-node27-autopipe.service --no-pager
```

期望：

- `nhms-node27-autopipe.timer` 为 `active (waiting)`。
- `infra/env/node27-ingest.env` mode 为 `0600`。

`N22_DSN`、`NHMS_NODE22_DSN_SOURCE` 和
`NHMS_ALLOW_ARCHIVED_NODE22_DB_ROLLBACK_MIRROR` 不属于当前生产 ingest
配置。wrapper 和 `scripts/node27_autopipeline.py` 都会把这些旧 node-22 DB
变量作为 `NODE22_DB_RUNTIME_ENV_FORBIDDEN` 显式阻断；forcing 元数据只通过
object-store forcing-domain handoff 进入 node-27 DB。

查看 wrapper 和最近运行结果：

```bash
cd /home/nwm/NWM
sed -n '1,180p' scripts/node27_autopipe_cron.sh \
  | sed -E 's#^(export DATABASE_URL=).*#\1<redacted>#'
tail -n 160 /home/nwm/autopipe-logs/autopipe.log
```

正常现象：

- 日志中每 10 分钟出现 `autopipe: start` 与 `autopipe: done rc=0`。
- JSON summary 包含 `object_store_root=/home/ghdc/nwm/object-store`、
  discovered/ingested/already_ingested runs、seeded/already_seeded basins。
- wrapper 固定传入 `--direct-grid-only`，并从 ingest env 读取
  `AUTOPIPE_RUN_WORKERS`、`AUTOPIPE_COVERAGE_WORKERS` 和
  `AUTOPIPE_EXCLUDE_BASINS`。当前生产值分别为 `2`、`2`、
  `zhaochen_hhy`；worker 上限为 8，未经下一轮真实 cycle 压测不得继续上调。
- `OUTPUT_PARSER_BATCH_SIZE=10000` 将河段时序批量写入页从默认 1,000 行提升为
  10,000 行，减少数据库往返；这不改变事务边界或最终 publish 语义。
- `coverage backstop (--all --skip-fresh)` 可刷新或跳过 display coverage；
  当前使用两个独立连接并行刷新。该步骤非 fatal，不应掩盖 autopipe 主返回码。
- **`--skip-fresh` 不可省**：#1341 后 river coverage 扫描按代理键选行，legacy
  （pre-#1340、NULL 键）run 扫不到任何行，裸 `--all` 或 `--run-id <legacy run>`
  会把已物化的 `run_display_coverage` 覆写成 0 / NULL 边界，**无法撤销**，该 run
  随即掉出 latest-product readiness 与 national tile。详见
  `scripts/node27_refresh_coverage.py` 模块 docstring 的 warning 块。

确认 node-27 ingest 按 bounded systemd 模式运行，并且 node-22 的 production
scheduler 是 DB-free systemd timer：

```bash
pgrep -af 'node27_[a]utopipeline|node27_[a]utopipe' || true

ssh -p 32099 frd_muziyao@210.77.77.22 '
systemctl --user status nhms-compute-scheduler.timer nhms-compute-scheduler.service --no-pager
pid=$(systemctl --user show -p MainPID --value nhms-compute-scheduler.service)
if [ "${pid:-0}" != "0" ]; then
  tr "\0" "\n" < /proc/$pid/environ | grep -E "^(DATABASE_URL|PGHOST|PGPORT|PGDATABASE)=" || true
fi'
```

The scheduler service is oneshot; it is normal for it to be inactive between
timer ticks. Any `DATABASE_URL`/libpq env in the scheduler process is a
misconfiguration. Slurm submission is through the node-22 Slurm Gateway and
`sbatch`; Slurm then runs compute work on allocated compute nodes such as
`cnXX`.

node-22 compute-only chain must stop at
`NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc` with
`NHMS_REQUIRE_FORECAST_WARM_START=true`: this runs SHUD forecast and DB-free
`state_save_qc`, then skips parse/publish. Do not use `forecast` as the
production terminal stage, because it writes forecast outputs but stops before
publishing canonical warm-start checkpoints into the file state index. Node-27
remains the owner of parse/QC/ingest/display.

node-22 scheduler 的模型清单来自 DB-free file registry。当前 canonical registry
是 direct-grid authority；新增或移动 Basins 后，禁止把 Basins publisher 生成的
legacy/IDW 行直接写到 canonical 路径。先发布 baseline staging，再在 node-27 生成
GFS/IFS 两个 source-scoped variant，最后把 direct-only candidate 发布到生产。
2026-06-30 现场 22 节点的 Basins 根为 `/volume/nwm/Basins`（Linux 路径区分大小写；
`/volume/NWM/Basins` 当前不是有效挂载点）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-dbfree.env
set +a
test -d "$NHMS_BASINS_ROOT"
NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=false \
.venv/bin/python scripts/publish_scheduler_file_registry.py \
  --basins-root "$NHMS_BASINS_ROOT" \
  --registry-manifest /ghdc/data/nwm/object-store/scheduler/baseline-registry/manifest-last.json \
  --object-store-root "$OBJECT_STORE_ROOT" \
  --object-store-prefix "$OBJECT_STORE_PREFIX" \
  --work-dir "$WORKSPACE_ROOT/scheduler/basins-file-registry-publish" \
  --output "$WORKSPACE_ROOT/scheduler/basins-file-registry-publish/receipt.json"
```

运行 baseline publisher 时必须对该单次 staging 命令显式设置
`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=false`；canonical/consumer 环境仍保持 `true`。
随后在 node-27 先把新增 baseline 登记到 registry DB，再运行
`scripts/provision_direct_grid_scheduler_registry.py`，输入上述 baseline staging、输出
direct-only candidate。候选必须满足：18 个现有流域加新增流域，每个流域恰有 GFS/IFS
两行，`resource_profile.forcing_mapping_mode` 唯一值为 `direct_grid`。把新 variant package
复制到 node-22 私有 object store 后，先用 `FileSchedulerModelRegistry(...,
require_direct_grid=True)` 校验，再依次原子发布 Slurm worker mirror 与 shared canonical，
并重建 canonical readiness。任何一步失败都保留上一份 canonical，不允许退回 IDW。

#### 3.1.1 新流域上线四跳（2026-08-22 #1699 实战 receipt，7 个流域）

上面那段是骨架，下面是走通一次之后每一跳的实际口径与坑。四跳顺序不可换。

**三个根必须分清**（同名不同物，弄混必错）：

| 角色 | 路径 | 说明 |
|---|---|---|
| node-22 计算侧 Basins 源 | `/volume/nwm/Basins`（本地 175T `/dev/sda`） | 流域原始目录，用**原名** |
| node-27 `BASINS_ROOT` = NFS Basins | 22 侧 `/ghdc/data/nwm/Basins` = 27 侧 `/home/ghdc/nwm/Basins` | seed 的 inventory 源，用**已定名** |
| NFS object store | 22 侧 `/ghdc/data/nwm/object-store` = 27 侧 `/home/ghdc/nwm/object-store` | node-27 `OBJECT_STORE_ROOT`；baseline 包与 dg 变体落这里 |
| node-22 调度器私有 object store | `/scratch/frd_muziyao/nhms-prod/object-store` | **调度器只认这个根**，dg 变体必须回拷 |

**hop 1 — baseline 发布（node-22）。**

```bash
PYTHONPATH=/scratch/frd_muziyao/NWM NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=false \
/scratch/frd_muziyao/NWM/.venv/bin/python scripts/publish_scheduler_file_registry.py \
  --basins-root /ghdc/data/nwm/Basins \
  --basin-slug <每个新流域重复> \
  --object-store-root /ghdc/data/nwm/object-store --object-store-prefix s3://nhms \
  --registry-manifest /ghdc/data/nwm/object-store/scheduler/baseline-registry/manifest-last.json \
  --work-dir <workspace>/work --output <workspace>/receipts/baseline-publish.json
```

- **必须显式传 `--registry-manifest`**：默认值取 `NHMS_SCHEDULER_REGISTRY_MANIFEST`，
  也就是**生产 canonical**。忘了就是直接往生产写 baseline/IDW 行。
- `baseline-registry/` 目录 2026-08-22 前不存在，首次需 `mkdir -p`。
- 缺 `PYTHONPATH` 会在 `_build_manual_cutover_gate` 处
  `ModuleNotFoundError: No module named 'scripts'`——门是默认开的，不是没跑。
- **第二次及以后的上线，`--registry-manifest` 必须换成本次专属路径**，例如
  `baseline-registry/<rollout>-<date>.json`。2026-08-22 #1699 是**首次**使用该目录，
  属 bootstrap（无 previous manifest）所以不触发闸门；2026-08-25 黄河子流域复用
  `manifest-last.json` 时当场被拒：publisher 把传入的 models **整体写出、不做合并**，
  于是本次 7 行会**移除** #1699 留下的 7 行，#1080 cutover 闸门报
  `registry_cutover_removal_refused` / `SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED`
  （包已发布，registry 未动，重跑幂等）。不要用 `--allow-uncovered-cutover` 绕——
  那会真的把上一批 baseline 行删掉。hop 3 的 `--baseline-registry` 只是个路径参数，
  指向本次专属文件即可；该文件须落在 NFS 上，因为 hop 3 在 node-27 跑。
- **`--basins-root` 要指向持久路径，不要指向临时 staging 目录。** 包版本号是纯内容派生、
  与路径无关（实测同一批树从 scratch staging 和从 NFS Basins 发布，7 个版本号逐字相同，
  第二次全部 `already_done`），但 registry 行会把 `source_path` 钉死成发布时的路径。
  #1698 就留下了 `source_path=/scratch/.../recalibration-1698/basins-staging/jialingjiang`
  这样指向临时目录的生产行。
  该字段**不参与运行期解析**（实证：`basins_dth_ls_shud` 的
  `source_path=/volume/nwm/Basins/DTH_LS` 根本不存在——真实目录叫 `CJ-DTH-LS`——
  而 DTH_LS 每日照常出 96 个 run），但它是唯一的 provenance，指向会被清理的目录等于自毁溯源。

**hop 1b — 把 staged 树拷进 NFS Basins（零 run 流域的 seed 前提）。**
拷**staging 副本**（含 IC 修复、含有意剔除的目录），不是源；两边必须 `diff -rq` 为空，
因为 publisher 与 seed 各自对同一棵树跑 `discover_basins_inventory`，身份一致是靠内容相同保证的。
拷完 `chmod -R a+rX`（22 侧 uid 1103 建的目录，27 侧 uid 1005 要读）。

**hop 2 — node-27 登记 baseline。** 就是 autopipeline 的 seed：

```bash
PYTHONPATH=/home/nwm/NWM uv run python scripts/node27_autopipeline.py --seed-only --only-basin <slug>
```

零 run 流域**只能**从 `BASINS_ROOT` inventory seed（`node27_autopipeline.py` 的 phase-1
先取 `_discover_seed_basin_identities(basins_root)`，run manifest 存在时才 override）。
`basin_id` 由**根目录名**推：`basins_{_slug_id(basin_slug)}`——改名就是在定 `basin_id`。
seed 写出的 `model_package_uri` 版本段是占位的
`vbasins-{slug_id}-production`（模板不含内容哈希），与 hop 1 发布的内容寻址版本**天然不同**，
这是既有形状（`basins_tailanhe_shud` 至今如此）、**不是错误**：
provision 只读该行的 `river_network_version_id / mesh_version_id /
calibration_version_id / shud_code_version`，不读 `model_package_uri`。

**hop 3 — provision dg 变体（node-27）。** `--source-grid` 用默认
`GFS=gfs_0p25 / IFS=ifs_0p25` 即可：`normalize_source_id` 走 `_STORAGE_SOURCE_IDS[upper()]`，
产出生产在用的 `gfs`（小写）与 `IFS`（大写）——这个不对称是规范化结果，别去"修正"。
两个坑：

- `direct_grid_variants/<baseline_model_id>/` 的父目录属 `frd_muziyao:huser` 且带 sticky，
  node-27 的 `nwm` 建不了子目录 → `PermissionError`。**先在 node-22 侧建好并放权**：
  `mkdir -p <dir> && chgrp nwmuser <dir> && chmod 2775 <dir>`（两个账号共有组 `nwmuser`/1107）。
- `--output-registry` 的**父目录不能组可写**，否则 `provider_lock_parent_unsafe`
  （`provider_atomic.py` 要求 `st_uid == geteuid()` 且 `mode & 0o022 == 0`）。`chmod 755` 即可。
- **`--output-registry` 绝不能指向生产 canonical manifest。** 与 hop 1 的坑不同形：
  这里 `--output-registry` 是 `required=True`（`scripts/provision_direct_grid_scheduler_registry.py:581`），
  没有默认值、忘不了；危险的是**主动指过去**。该脚本在 `:558` 调
  `publish_scheduler_registry_manifest(output_models, output_registry, ...)`，而后者把传入的
  `output_models` 当作**完整 `models` 列表**整体写出（`scheduler_file_providers.py:586-594`），
  **不与目标已有内容做任何合并**。指向生产 canonical 的后果是：生产 manifest 的 models 被本次
  产出的 dg 变体行**整体替换**，其余所有流域的行当场消失。且此处未传 `expected_preimage`，
  **没有 CAS 保护**兜底。正确姿势始终是：输出到本次 workspace 下的独立路径，再在 hop 4 合并发布。

**hop 3b — 变体包回拷 node-22 scratch。** 用 `cp -r`，**不要 `cp -a`**：
flash `/scratch` 不支持保留权限位，`cp -a` 每个文件都报
`preserving permissions ... Operation not supported` 并以非零码退出，配 `set -e` 会中途断掉
（数据其实已拷完，容易误判）。拷完 `diff -rq` 对齐。

**发布前的硬闸：packaged-IC 探针必须对每一行 qualified。** dg 变体 manifest 只有
`direct_grid_forcing` 键、没有 `included_files`，走 tier (b) 对象探针，URI 由
`{model_package_uri}{shud_input_name}.cfg.ic` 推出并在 **scratch 根**上解析。用现成工具，别手搓：

```bash
PYTHONPATH=/scratch/frd_muziyao/NWM /scratch/frd_muziyao/NWM/.venv/bin/python scripts/audit_first_cycle_initial_state.py \
  --registry-manifest <candidate>.json \
  --object-store-root /scratch/frd_muziyao/nhms-prod/object-store \
  --object-store-prefix s3://nhms --workspace-root /scratch/frd_muziyao/nhms-prod/workspace \
  --receipt-path <receipts>/ic-audit.json
```

判据是每行 `ic_status=qualified`；`verdict=undetermined` 在还没有 run 时是正常的。
`shud_input_name` 来自**内层 `input/<Name>/` 目录名**，only-root 改名后与 `basin_slug` 不同
（实战：`SHJ-2SHJ` 的 `shud_input_name` 是 `2SHJ`，`DTH_XJ` 的是 `CJ-DTH-XJ`）——
任何假设 `name == slug` 的推导都会在这类行上断。

> `/ghdc/data/nwm/object-store/scheduler/direct-grid-candidates/`（world-writable + sticky）
> 是 provision 候选产物的**共享落点**，实测长期为空——#1698 与 #1699 都把 `--output-registry`
> 写在各自 workspace 里，因为共享目录的组可写属性会触发 `provider_lock_parent_unsafe`。
> 它不是遗留垃圾、也不参与任何自动流程，保留即可；候选放哪儿由 `--output-registry` 决定。

**hop 4 — 合并发布。** 机制与 5.7.1 相同（直接调 `publish_scheduler_registry_manifest`、
两份共用同一 `generated_at`、CAS）。新流域上线是 **add-only**，发布前断言：
行数 = 旧 + 2×新流域数、无重复 `model_id`、新 slug 与既有 slug 不相交、全部 `direct_grid`、
每流域恰好一条 gfs 一条 IFS、每行都带 `shud_input_name` 与 `model_package_uri`。
备份 stamp 每次换新，否则脚本会因备份已存在而拒跑。

**registry manifest 有字节上限，行数增长会撞。** `MAX_REGISTRY_MANIFEST_BYTES`
（`services/orchestrator/scheduler_file_providers.py`）同时管**写入后回读**和
**所有消费者的读取**。2026-08-25 合并到 62 行时实测 4,250,534 B，超过当时的 4 MiB
上限 56,230 B：原子写入器写完、回读时 `capture_provider_preimage` 报
`provider_destination_size_limit_exceeded`，于是**回滚并抛 `provider_restored_previous`**
（canonical 完好无损，这是闸门在正常工作，不是数据损坏）。已提到 16 MiB（约 160 行）。
再撞时的处置顺序是死的：**改常量 → CI → merge → 22 与 27 都 `git pull --ff-only` → 才能发布**。
先发布后升级 = 旧代码的调度器读不动新 manifest，等于全量停摆。
覆盖坪估算：大流域一行约 100 KB（`direct_grid_forcing.station_bindings` 内联就占 ~60 KB）。

**发布后必须手动跑一趟 refresh 重建 readiness。** readiness 索引条目与 registry identity
是**逐一相等**关系（`validate_readiness_registry_model_set`），48 行 registry 配 34 行 readiness
会被拒。好在 refresh 是从当前 `registry_models` **现推**再自校验
（`derive_catalog_bound_readiness_entries`），不加载旧条目，所以不存在死锁——但
`nhms-scheduler-file-provider-refresh.timer` 实测是**天级**周期（下次触发可能在 26 小时后），
**不能等它**。触发方式与判据见 5.7.1（`latest.json` 的 `started_at` 变新；
refresh 的 `ExecCondition` 要求 `nhms-compute-scheduler.service` 非 active，
而一趟 pass 可以跑一小时以上）。

**hop 5 — 改了 `model_id` 的流域必须回补 forcing（重发场景专有；纯新增流域不触发）。**
forcing 是**按 model 分目录**存的：`<object-store>/forcing/<source>/<cycle>/<basin_version_id>/<model_id>/`。
包内容一变，`dg_*` 身份就变，于是所有**已产过 forcing 的 cycle** 只有旧 id 的产物，新 id 一份没有。
而调度器判 forcing 完成度是**按 cycle** 的，它不会为这种 cycle 重进 forcing 阶段——
forecast 照submit，1~2 秒死在 `ARTIFACT_NOT_FOUND`（#1816 重发 8 流域时实测，16 个 model 全中）。

正确做法是**重放生产**，不是 `cp`。重发若没有移动测站（标定-only / 元数据-only 的常见情形），
`station_bindings` 逐行物理相同、只差 `dg-<src>-<hex>::` 身份前缀，所以在新 id 下重跑 producer
必然得到数值等价、且 id 与嵌套 checksum 自洽的包。拷贝旧目录则会把旧
`model_input_package_id` / `binding_uri` / station id 焊进**每一个**成员文件，
而 `met.met_station` 是按**新** binding 身份注册的。

```bash
.venv/bin/python scripts/node22_backfill_forcing_for_model_ids.py \
  --previous-manifest <registry>/manifest-last.json.pre-<stamp> \
  --current-manifest  <registry>/manifest-last.json \
  --forcing-root /scratch/frd_muziyao/nhms-prod/object-store/forcing \
  --cycle <只补调度器下一趟要跑的那个 cycle> --execute --output <receipts>/forcing-backfill.json
```

工具自带验收：`shud/*.csv`（SHUD 真正读的输入，不含任何身份串）必须**逐字节相同**，
`forcing.tsd.forc` / `forcing_debug.csv` / `payloads/*.json` 在把身份串归一化后必须相同。
三个 JSON manifest **不参与**比对——它们带成员 checksum，成员字节一变它们本就该变。
测站真移动了（`station_bindings` 归一化后仍不等）时工具**拒绝回补**并把该行记进
`rebound_models_skipped`：那是重新绑定，得走正常 provisioning，不是回补。
默认 dry-run；不传 `--cycle` 会扫出**所有**历史 cycle，而历史预报不追溯——按需只补下一趟要跑的。
`--jobs N` 并发跑（每个 item 写各自的 model 目录，互不争用；实战 `--jobs 5`，单个 model
约 20 分钟，node-22 48 核，注意别顶满）。注意 forcing 路径的 source 段是
`normalize_source_id(x).lower()`——canonical `IFS` 落在 `forcing/ifs/` 下，
按 canonical id 去扫会一条都找不到、静默漏掉一半的活。

**receipt 里必须先看 `coverage`，再看 `work_item_count`。** `renamed_model_count: N,
work_item_count: 0` 既是"全部已回补"的稳态，也是 `--forcing-root` 指错 / NFS 没挂 / 环境不对的
样子——这两者用条目数分不开。所以 receipt 带一段 `coverage`：`source_dirs_probed`（探了哪些
`<root>/<source>/` 路径）、`source_dirs_found`（其中真实存在的）、`previous_model_dirs_found`
（扫到多少个旧 id 目录）。**一条 source 目录都不存在时工具直接拒绝**
（`BACKFILL_FORCING_ROOT_UNCOVERED`，`--forcing-root` 本身不是目录则是
`BACKFILL_FORCING_ROOT_ABSENT`），错误里就带上探过的路径，不会以 exit 0 冒充"没活可干"。
部分漏覆盖不拒绝（`--cycle` 本来就会收窄扫描面），但在 `coverage` 里看得见：
`source_dirs_found` 少于 `source_dirs_probed` 就该问为什么。

**status 一览（除 `verified` / `dry_run` 之外都让命令 exit 非 0）**：

| status | 含义 | 操作 |
|---|---|---|
| `verified` | 重放产物通过等价验收 | 无 |
| `dry_run` | 缺省预览，没跑 producer | 核对后加 `--execute` |
| `existing_target_unverified` | 新 id 目录**已存在但验收不过**——producer 是**按文件**原子写、不是按目录，中途被杀就会留下只有部分成员的目录 | 见下 |
| `produce_failed` | producer 非 0 退出，残留目录**已成功隔离**（`detail.quarantine_path`；无残留时该键为 `null`） | 看 `detail.stderr_tail` |
| `verification_failed` | 跑完了但与旧包不等价，产物**已成功隔离**（`detail.quarantine_path`） | 看 `detail.verification` |
| `quarantine_failed` | **隔离动作本身失败**：`detail.unverified_artifact_live: true`，未通过验收的产物**仍然活在** `detail.live_target_dir` 上 | **最高优先级**，见下 |
| `errored` | 该 item 处理时抛异常（成员不可读等），`detail.error` 里是异常 | 修掉底层故障后重跑该 cycle |
| `pending` | 该 item 根本没被处理到——只会在 receipt 顶层出现 `loop_error` 时成片出现 | 看 `loop_error`，修掉后整条命令重跑 |

`errored` 是**逐 item** 的：一个 item 出事不会吞掉整份 receipt，其余 item 的 status 照常落盘
（`--output` 也照写），命令 exit 1。顶层 `loop_error` 则是 item 循环**外面**炸了（只在异常情况下
出现）：receipt 照写、照 exit 1，但循环剩下的 item 停在 `pending`，它们的活一件没干。

**`existing_target_unverified` 不会被跳过，但缺省也不会被动。** 老实现按
`target_dir.is_dir()` 判"已完成"，于是半截目录在 receipt 里与"已正确回补"长得一模一样，且以后
每一趟都继续跳过、永远修不到。现在这种目录会被**发现并报告**、命令 exit 非 0，产物**原样保留**
——它不是本次跑出来的，删不删是运维的决定。确认要重做时加
`--replace-unverified-target`（help 里写明是破坏性的）：它把该目录移到同级
`_backfill_quarantine/` 下再重跑 producer。验收通过的已存在目录仍然照旧跳过。
**只加 `--replace-unverified-target`、不加 `--execute` 的预览仍然报 `existing_target_unverified`、
仍然 exit 非 0**（多一个 `detail.would_replace_target: true` 表明加 `--execute` 会替换它）——
预览不该比它所预览的状态更绿，拿 dry-run 的 exit code 当放行闸的脚本要的就是这条。

**验收不过 / producer 失败的产物一定不留在真实 model 路径上。** forecast 阶段是直接读
`<basin_version_id>/<model_id>/` 的，留一份没通过验收的包在那儿就等于让 SHUD 静默吃下去。
工具把它移到 `<basin_version_id>/_backfill_quarantine/quarantined-<model_id>.<status>.<UTC 时间戳>.pid<pid>/`，
路径记在 receipt 的 `detail.quarantine_path` 里。
目录名带前导下划线、条目名带 `quarantined-` 前缀，都不可能被当成合法的 `dg_*` model 目录。

**下一步**：照 `detail.verification` / `detail.stderr_tail` 定位原因（原始输入被 retention 清了？
盘满？成员不可读？），修掉之后重跑同一条命令——此时该 model 路径已经空了，会被当成正常的
待回补项重新产出。隔离目录确认无用后手工删除，工具不自动清。

**隔离也会失败，而失败时产物还站在活路径上——那是另一个 status，不是同一个。**
`/scratch` 是 NFS 上的（ESTALE、权限漂移、`_backfill_quarantine` 父目录建不出来：配额 / ENOSPC /
同名文件挡路），rename 就是会失败。此时 status 是 **`quarantine_failed`**，`detail.unverified_artifact_live: true`，
`detail.live_target_dir` 是那条**仍然可被 forecast 读到**的路径，每次尝试的失败原因逐条记在
`detail.quarantine_errors`（列表，两次尝试不会互相覆盖），`detail.quarantine_failed_after` 说明是哪一步
（`produce_failed` / `verification_failed` / `replaced_unverified`）触发的隔离。**处置**：先手工把
`detail.live_target_dir` 移走或删掉（**在下一趟 pass 跑到这个 cycle 之前**——留着它 SHUD 就会吃下去），
再修存储故障，然后重跑同一条命令。带 `--replace-unverified-target` 时若隔离失败，工具**不会**去跑 producer：
写是按文件原子的、不会先清目录，往还在的半截包里写等于把两份包搅在一起。

**回补完不会自愈——已经跑失败的 run 必须单独放行。** `ARTIFACT_NOT_FOUND` 被分类器判为
**永久失败**（`classify_failure` 给 `retryable=False, permanent=True`），与重试预算无关
（实战 `submission_attempt=1`，limit 是 6）。所以产物补上之后，那些 run 仍然是
`blocked` / `permanent_failure_guard`，下一趟 pass 照样不会重跑它们。

正规通道是 `pipeline.retry_run` 的 manual-retry marker（`record_manual_repair`：
policy 门 + cycle 写锁 + 冲突/缺失拒绝 + 证据留痕），`classify_failure(..., manual=True)`
只对被标记的那个 run 把 `permanent` 翻成 `False`。**不要改 journal 行**——8.5 禁的是手改行，
用这个带门的类型化 API 正是它指向的替代路径。

```bash
.venv/bin/python scripts/node22_manual_retry_failed_runs.py \
  --journal-root /scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal \
  --run-id fcst_<source>_<cycle>_<model_id> \
  --reason "<为什么重启>" --requested-by "<操作者>" --execute
```

**先看 preview（缺省就是 preview，不写）**：forecast 阶段除了逐 run 行，还有一条覆盖该
cycle 全部 model 的 **cohort master** 行，标错它会把整个 cohort 重跑。preview 会把要动的
行 id 打出来——实战命中的是 `job_fcst_..._forecast_reconciled_34817_6`（逐 run），不是
`job_cycle_..._forecast_cohort_...`。逐 run 逐个标，不做批量扫。

标完跑一趟 bounded pass（`systemctl --user start nhms-compute-scheduler.service`，
timer 全程保持关闭），验收判据不是候选变成 `selected`，而是 **forecast 真跑成、
`state_save_qc` 在新 id 下写出下一个 `valid_time` 的 state**——那才是接回 warm chain 的东西。
顺带核对没被标记的 run 仍是 `blocked`，以证明 marker 是逐 run 生效的。全部通过后再
`systemctl --user enable --now nhms-compute-scheduler.timer`。

**一条 provenance 备注**：publisher 曾对"越界"标定参数（`SOIL_ALPHA` 上界 20.0、
`GEOL_DMAC` 上界 4.0）在隔离副本上静默改写后再打包（`basins.calibration_repair.v1`）。
该 repair 已在 **#1816 中整体删除**——它静默改写的是外部用户跑 SHUD 收敛得到的标定值。

**两个上界的出处不同，且都不是仓库里 grep 得到的**（`SHUD/` 被 gitignore，见 `.gitignore:81`）：

- `SOIL_ALPHA <= 20`：SHUD 里确有声明（`ModelConfigure.cpp:90`），但 `checkValue()` 调用
  `checkRange()` 后**丢弃返回值**，而 `checkRange` 只 `fprintf` 一行——是**软告警，不是闸门**。
  越界不会被拒，也不会崩。
- `GEOL_DMAC <= 4`：**SHUD 里根本没有对应物**。源码声明的范围是 `[0, 10]`
  （`ModelConfigure.cpp:109`），而源值 `GEOL_DMAC=5 × Dmac 列上限 1.0 = 5` 落在该范围内、
  零告警、照样 NaN。4 是**实测稳定边界**（2×2 跨 gfs/IFS 两个独立源：4.5 跑通、4.75 NaN、
  源值 5 两边都 NaN），任何源码里都不存在——所以它只能靠**显式声明**承载，见 #1832 的
  `config/calibration_overrides.yaml`。

publisher 对**未被声明**的标定文件是**纯拷贝**：包内的 `*.cfg.calib` 与 Basins 树里的
源文件逐字节相同，`cmp` 应当返回 0。被声明覆盖的流域参数记在
`manifest["calibration"]["overrides"]` 里——声明是唯一入口，没被点名的一律不动。
2026-08-22 之前发布的包中有 8 个流域（含 `SHJ-2SHJ`、`hetianhe`）带着被改写的值，
它们在 #1816 之后单独重发；重发前的历史预报不追溯、不重签。
**仍在运行的 repair 只有缺失辐射模板那一条**（`basins.missing_tsd_rl_template_repair.v1`，
staging 目录 `repaired-basins`）：它补的是缺失文件，不改任何标定值。
（另有 staging 目录 `overridden-basins`，那是**声明式标定覆盖**的落点，不是 repair：
它只对 `config/calibration_overrides.yaml` 点名的流域参数生效，且记进 manifest。）
**它记在发布 receipt 的 `summary["repairs"]` 里，不在 package manifest 里**——
`publish_basins_package` 不收 repair 参数，manifest 对任何 repair 都没有字段。
而 receipt 只在显式传了 `--output` 时才落盘（`publish_scheduler_file_registry.py:396-397`），
否则只打到 stdout。查 repair 溯源要找 receipt，不要翻 manifest。

**当前 authority（2026-08-22 node-22 实测 canonical manifest）**：共 24 个业务流域，
口径为 17 个既有流域加 #1699 上线的 7 个；每个流域有 GFS、IFS 两个 source-scoped
direct-grid model variant，所以 scheduler registry 是 **48 行**，不是下面 baseline ID 的 24 行。
（此前文档写的「18 流域 / 36 行」在 2026-08-22 前就已 stale：`basins_hhe_shud`
早已不在 registry 中，实际是 17 流域 34 行。数量一律以
`jq '.models|length' manifest-last.json` 实测为准。）

```text
basins_dth_ls_shud
basins_dth_xj_shud
basins_dth_yj_shud
basins_dth_zj_shud
basins_heihe_shud
basins_hetianhe_shud
basins_huai_main_shud
basins_huaiyss_shud
basins_jialingjiang_shud
basins_kashigeer_shud
basins_keliya_shud
basins_lh_gl_shud
basins_lh_ldbd_shud
basins_lh_lxyh_shud
basins_lh_ylj_shud
basins_qhh_shud
basins_qinyijiang_shud
basins_shj_2shj_shud
basins_tailanhe_shud
basins_weiganhe_shud
basins_xinanjiang_upstream_shud
basins_zhaochen_bst_shud
basins_zhaochen_mc_shud
basins_zhaochen_wem_shud
```

因此 GFS/IFS 各有 24 个 source-model candidate，共 48 个候选执行单元。
调度器在 candidate 构造前按 direct-grid contract 的 `applicable_source_ids` 投影模型；
不得把 36 个 variant 与两个 source 做 72 行笛卡尔积，也不得把预期的异源不适配记成
pass-blocking failure。合同缺失或损坏仍须 fail closed。
`NHMS_SCHEDULER_MODEL_IDS` 和 `NHMS_SCHEDULER_BASIN_IDS` 正常保持为空，由
file registry 决定全量自动计算；只在定向 rollback/drill 时临时收窄。
生产并发由 scheduler 的全局 Slurm 数组预算
`NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND=32` 和 resource profile 的
`max_concurrent=32` 共同约束。scheduler 登录节点
只按“数据源 × 时次 × restart-compatible stage”构造 cohort、提交和轮询，不执行
direct-grid forcing。每个 cohort 的全部流域进入 `produce_forcing_array.sbatch`，Gateway
生成 `--array=0-(N-1)%min(cohort预算,resource profile上限,N)`；同一 pass 同时运行的
cohort 预算总和不超过 32。GFS 与 IFS cohort 可同时在 Slurm 中推进，scheduler
只在 pass 收尾时汇总全部 cohort。`NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` 仅限制少量
cohort 提交/轮询控制线程，不能作为流域计算并发口径，也不能替代 Slurm `%N`。
cohort run id 包含排序后 candidate membership 的稳定摘要；同一成员集合重启时复用，
定向过滤或新增/移除流域时生成新 idempotency key，禁止把子集数组误认成全量数组。

`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 是生产硬门禁：publisher 不能用 legacy/IDW
行覆盖 canonical，consumer 读到任一非 direct-grid 行也会整体阻断。每日
`nhms-scheduler-file-provider-refresh.timer` 在该模式下重验并重发当前 direct-grid
authority、readiness 与 state index，不从 Basins 自动生成 IDW replacement；新增流域
必须先走上一段 direct-grid provisioning 流程。

2026-06-30 的 13 模型、2026-07-01 的 submit bound 13 仅是当时的历史现场
快照，不再代表当前 registry 或并发配置。
若只读 Basins 源中某个模型仅缺 `*.tsd.rl`，脚本会在私有 scratch copy
里复制同覆盖期 radiation 模板，原始 NFS Basins 源保持不变。

#### 3.1.3 DB-free scheduler 的受支持回滚/前滚

> 编号非单调是**故意**的：本节曾是 3.1.1，#1699 插入新的 3.1.1 后本应顺推为 3.1.2，
> 但 `openspec/specs/scheduler-registry-refresh/spec.md:376`（live spec，非 archive）明文
> 把 **§3.1.2** 绑定到下面的「DB-free file-provider 稳态刷新」那节。所以那节保持 3.1.2，
> 本节让到 3.1.3。**不要顺手把它改回 3.1.2** —— 那会打断 spec 引用；真要改得走独立的
> openspec change 同步改 spec。

禁止直接把 `/scratch/frd_muziyao/NWM` checkout 到 pre-inventory writer 后启动。
受支持流程必须保留当前版本作为 rollback controller，并为目标 SHA 创建一个临时、
clean、detached checkout；目标 generation 一律使用完整 SHA：

```bash
ROLLBACK_SHA=$(git rev-parse '<rollback-ref>^{commit}')
ROLLBACK_CHECKOUT="/scratch/frd_muziyao/nhms-rollback-${ROLLBACK_SHA}"
git worktree add --detach "$ROLLBACK_CHECKOUT" "$ROLLBACK_SHA"
(cd "$ROLLBACK_CHECKOUT" && uv sync --all-extras --dev)
test -x "$ROLLBACK_CHECKOUT/.venv/bin/python"
test -z "$(git -C "$ROLLBACK_CHECKOUT" status --porcelain=v1 --untracked-files=all)"

systemctl --user stop nhms-compute-scheduler.timer nhms-compute-scheduler.service
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli prepare-file-journal-rollback \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --workspace-root "$WORKSPACE_ROOT" \
  --scheduler-lock-backend file \
  --scheduler-state stopped \
  --active-scheduler-processes 0 \
  --checked-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --checked-by "$USER" \
  --target-writer-generation "$ROLLBACK_SHA"
```

`ROLLBACK_CHECKOUT` 及其 `.venv` 只需在 launcher 完成 active binding 发布前可由当前
controller 读取；不要在 gate 运行中执行 `uv sync`、切换 checkout 或改写解释器。active
发布后，计算节点依赖的是 `WORKSPACE_ROOT/.nhms-rollback-execution-v1/` 下由
`<receipt_id>-<target_generation>` 唯一确定的共享保留目录，不再依赖原 checkout 或其 venv。

保存 preparation `receipt_id`。旧 writer 只能由仍在当前版本的 controller 通过下面
的 gate 启动；该命令不接受操作者自报的 actual generation，而是从即将运行的 checkout
内部执行 `git rev-parse HEAD`、检查 tracked/untracked dirty 状态，并要求目标 checkout 的
`.venv/bin/python` 存在且可执行。gate 只接受 `plan-production` 的一次真实 `--submit`；
不带 `--submit`、`--plan`、`--dry-run`、`--help`/`--version`，以及操作者传入的
`--workspace-root`/`--lock-path` 覆盖，都会在 writer 零启动时拒绝：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli launch-file-journal-rollback-writer \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --workspace-root "$WORKSPACE_ROOT" \
  --receipt-id '<preparation-receipt-id>' \
  --writer-repository-root "$ROLLBACK_CHECKOUT" \
  -- plan-production --submit --continuous --max-passes 1
```

通过 receipt 后，controller 会把目标完整 SHA 物化到上述 workspace-scoped、私有且只读的
generation retention root，并从已经打开和复核过的目标解释器复制一个内容固定的 runtime；
runtime 自带复制且锁紧的库和配置，不保留指回原 venv 的软链。active binding 发布后，即使
删除整个原 checkout，binding 校验及 forcing、forecast、state-save 执行也必须继续成功。
prepare 成功前，controller 先写入 workspace-scoped `prepared` execution binding，
把 preparation receipt、journal/workspace/file-lock 与目标 generation 绑定为 no-launch
authority；launcher 在 child 启动前将其替换为包含 source/runtime 的 `active` binding。
ambient environment 不能改写这些值；即使旧版本 writer 不认识新 manifest 字段，当前
HTTP Slurm gateway 也只会按 exact workspace 注入 active binding。forcing、
forecast、state-save 三阶段都会切换到该 source 并使用该 runtime；无 active binding 的
普通生产提交仍使用原 console entrypoint。
每个 Gateway single/array/direct-render 请求只捕获并验证一次 binding，array task 复用同一
request-local 结果；active 期间拒绝调用方覆盖 `PATH`、`PYTHONPATH`、`PYTHONHOME`、
`VIRTUAL_ENV`；生成脚本会 unset `PYTHONHOME`/`VIRTUAL_ENV`，把 `PYTHONPATH` 固定为 bound
source，并把 `PATH` 替换为 bound runtime bin 加固定最小系统路径，不继承 gateway 的
ambient `PATH`。worker 命令及 forecast 两段 inline Python 都必须使用 exact bound runtime。
launcher 首次启动只接受 exact `prepared`，重放只接受 exact `active`；binding 缺失或为
`completed` 都是零启动，completed generation 只能由下一次 prepare 归档并替换。

source 与 runtime bundle 都以
`retained_fail_closed_until_operator_cleanup` 保留，launch JSON 中的
`target_python_source_root`、`target_python_runtime` 和
`rollback_execution_binding_id` 是审计路径/身份。每次 active binding 捕获都会用 bounded
no-follow walk 复核完整 runtime tree；任何 nested file/dir 可写、symlink、special entry 或
非约定 executable mode 都会在零 sbatch 时拒绝。不要单独删除任一 bundle；只有所有引用
它们的 Slurm task 均已终态且前滚完成后，才能清理该 workspace generation retention root；
原 rollback checkout 是独立对象，active 发布后可删除，不能把它当作 bundle retention owner。

`preparing` receipt 无论遗留在 marker 删除前还是删除后，都只能在重新取得同一 production
file lease 后自动续成一个 `prepared` fence；不得人工删除 marker/receipt。fence 存在期间，
当前 scheduler 必须以 `scheduler_rollback_fence_prepared` 拒绝业务提交。

旧 writer 停止后，从当前版本执行前滚，成功消费 fence 后才能恢复 timer：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli complete-file-journal-rollforward \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --workspace-root "$WORKSPACE_ROOT" \
  --scheduler-lock-backend file \
  --preparation-receipt-id '<preparation-receipt-id>'
systemctl --user start nhms-compute-scheduler.timer
```

launcher 持有独立的 rollback execution flock，并把 fd 传给 child；即使 controller
崩溃，只要 old writer 仍存活，roll-forward 也会以
`file_journal_rollback_execution_active` fail closed。前滚命令还会在首次状态迁移前只按
bounded reconcile inventory 读取 exact current journal/latest/direct/legacy authority，不扫描
年度历史：只有显式 terminal allowlist 可通过；local/no-ID、空/未知状态、partial cohort，
以及 enumerate/stat/read 期间 authority 消失或查询不可用，都会拒绝前滚且不改变
fence/binding；该 quiescence proof 本身也不会创建或更新 journal/lock authority。查询开始时
会固定 `reconcile-inventory/`、`journal/`、`latest/`、`pipeline-jobs/`、
`active-reconcile/` 五个 root 的签名；任一原本存在的 root 消失、被替换或在最终复核前变化，
统一报 `file_journal_quiescence_authority_changed`。journal/latest 等 recursive walker 还会在
每层目录 list 前、list 后和 child recursion 后复核该层签名，nested entry 不能在首次 list 前
被静默删除、替换或新增。只有全程不存在的 root 才可视为空。确认
source/runtime 仍存在且任务全部收敛后，binding 按
`active -> rolling_forward -> completed` 迁移；中途崩溃可从
`rolling_forward` 续跑。若 prepare 后决定不启动 old writer，也只能由 exact `prepared`
authority 在 unsettled job 为空时执行 `prepared -> rolling_forward -> completed`；binding
缺失或被篡改时禁止手工删除 fence。只有 completed receipt 才允许恢复 timer 或清理
receipt/generation retention root；原 worktree 可在 active 发布后独立删除。

node-22 live drill 必须保存 preparation、old-writer launch、roll-forward 三段 receipt，
并证明 A receipt 只能运行 clean A commit 快照；B/dirty/unresolved checkout、不可用目标
runtime、root/lock override 和非 submit/eager-exit 命令均为零启动；还必须保存三个
worker stage 的 sbatch，证明它们引用 launch receipt 中同一
`target_python_runtime` 和 `target_python_source_root`。

#### 3.1.2 DB-free file-provider 稳态刷新

Registry、canonical readiness 和 state index 的 consumer freshness 上限均为
168 小时；不得延长上限或只修改 `generated_at`。node-22 用独立 user-systemd
timer 每日从权威内容完整重验并重发三个 provider，scheduler consumer 仍然只读、
fail closed。direct-grid 生产模式下 timer 重发当前已验证 registry；
`publish_scheduler_file_registry.py` 只负责 baseline staging，不能直写 canonical。
它与 timer、model lifecycle、readiness/state writer 共用同一个 destination-derived
lock（CLI 在 commit 时短暂持有），但 **CLI 不传 `expected_preimage`**：`main()` 从不
populate 该参数，expected-preimage 检查只由 refresh runner 自身的
registry/worker-mirror/readiness/state lane（含回滚路径）与 `state_manager` 的
state-index copyback 使用。因此 CLI 对 refresh timer 的并发保护是 **operator-gated 而非代码强制**——
若 refresh 在 CLI 的 snapshot→commit 窗口内提交，CLI 会静默覆写它且不会报
`provider_preimage_changed`。运行前必须按下面"手动 publisher CLI"条目确认 timer 与
oneshot service 均非活跃。其余 writer 之间的 lock + preimage 语义不变。
refresh user unit 不启用 `PrivateTmp`：node-22 的 user-systemd mount namespace 会在该模式下
拒绝进程打开 `/`，与 provider 的绝对路径逐级 no-follow 校验冲突；私有边界继续由 mode-0600
env、mode-0700 workspace/receipt/emergency/lock 目录、`UMask=0077` 和 DB selector 清除保证。
现场是 split-root：`OBJECT_STORE_ROOT` 必须保持
`/scratch/frd_muziyao/nhms-prod/object-store`，用于发布 registry package，并校验 scheduler
实际消费的 catalog/checkpoint 引用；`NHMS_SCHEDULER_PROVIDER_STORE_ROOT` 必须指向
`/ghdc/data/nwm/object-store`，且只承载 registry、canonical-readiness、state-index 三个
shared-NFS canonical provider。registry JSON 位于 shared root，但其中
`s3://nhms/models/...` 始终由 private `OBJECT_STORE_ROOT` 解析；不得依赖历史双份 package、
合并两根，也不得关闭 private root 上的 object verification——registry package 解析、refresh
续期的 checkpoint 校验、state-index copyback 的 source 侧全量校验一律照旧。唯一例外是
**shared root 上历史 state entry 的对象存在性**：已退役的 node-27 product-archive mover
（#1370）曾按 14 天策略归档 shared root 的 state 对象，而没有任何组件剪枝 shared state
index——被它搬走的对象不会回来，index 里的历史 entry 仍指向空位，因此 copyback merge
只校验并搬运本次胜出的 source entry，不再要求 shared index 里历史 entry 的对象仍在
shared root（#1189，见 8.8）。不要照旧文档把 destination 侧全量 object verification
"恢复"回去——那会原地重装同一个链停摆雷。

Registry package version 必须由 publisher 同一套源计划生成：required、optional SHUD
runtime、`CALIB/` 与 forcing CSV 的相对路径、大小和内容 checksum 都参与；机器绝对路径、
repair run workspace 路径和 object URI 不参与。因此同内容跨 run/root 必须复用同 version，
任一上述内容变化必须生成新 version。若现场出现
`BASINS_PACKAGE_CHECKSUM_CONFLICT`，先核对运行代码是否仍使用旧的“required/checksums +
绝对 source path”版本算法；不得删除或覆盖已有 immutable package。新实现还会在发布前
重算 identity，期间源内容变化会以 `BASINS_PACKAGE_SOURCE_IDENTITY_CHANGED` 在 canonical
replace 前失败。

首次安装必须先记录 scheduler 与 refresh unit 状态，并保持 scheduler timer 原状态：

```bash
cd /scratch/frd_muziyao/NWM
systemctl --user is-enabled nhms-compute-scheduler.timer || true
systemctl --user is-active nhms-compute-scheduler.timer || true
systemctl --user is-active nhms-compute-scheduler.service || true
squeue -h -u "$USER"

install -m 0600 infra/env/compute.scheduler-provider-refresh.env.example \
  infra/env/compute.scheduler-provider-refresh.env
# 按现场真值核对每个绝对路径；installer/wrapper 会拒绝完整 libpq selector 集。
grep -En '^(DATABASE_URL|PIPELINE_DATABASE_URL|PG[A-Z0-9_]+)=' \
  infra/env/compute.scheduler-provider-refresh.env && exit 1 || true
install -d -m 0700 /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/runs \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/emergency

scripts/install_node22_scheduler_file_provider_refresh.sh --install
```

部署窗口先 dry-run；它必须重新发现完整 Basins inventory。Readiness 不续签旧 index：
在任何 canonical replace 前，用同次 prospective registry model identities 分别扫描 private
`OBJECT_STORE_ROOT` 中最新的 GFS/IFS cycle catalog，执行 bounded/no-follow、schema、
source/cycle、统一 lineage identity、forecast hours、catalog row、canonical object checksum
全验证，并按 direct-grid `applicable_source_ids` 为每个适用的 source/model 生成一条只含
`catalog_uri + catalog_sha256 + catalog_row_count` 绑定的 entry；不得生成异源不适配的
readiness 行。
条数不写死：readiness 条目与 registry identity 是**逐一相等**关系
（`validate_readiness_registry_model_set`），所以每个 source 的条数恒等于该 source 适用的
registry 模型数，总条数恒等于 registry 行数——一律以
`jq '.models|length' manifest-last.json` 与 readiness index 实测为准，不引用文档里的历史数字。
（历史证据留档：2026-07-15 为 19 模型 / 每源 19 条 / 共 38 条，2026-07-18 为 18 / 18 / 36，
2026-08-22 #1699 上线后为 24 / 24 / 48。这三个数只说明它会变，不构成断言。）
最新 catalog
invalid 时禁止回退旧 cycle；consumer identity mismatch 必须重读同一绑定 catalog 后重算。
State index 才允许仅绕过年龄并重验 checkpoint object。任何 missing/invalid 引用或
registry/readiness model-set mismatch 都在 canonical replace 前失败，绝不续签 legacy
readiness、复制巨大 products、生成空 index、DB fallback 或 timestamp-only 文件：

```bash
scripts/scheduler_file_provider_refresh_once.sh --dry-run
jq '{outcome,reason,database_free,cutover_gate,providers,orphans}' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json

scripts/scheduler_file_provider_refresh_once.sh
jq '{outcome,reason,database_free,cutover_gate,providers,orphans}' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json
```

（这两条 projection 里的 `"cutover_gate": null` 只是 `jq` 对象构造对缺失 key 的补位产物，
表示 receipt 里根本没有该字段，**不是** 持久化的 `null` 占位；要区分请直接
`jq 'has("cutover_gate")'`。）

`published` receipt 必须绑定三个 shared canonical 文件以及
`NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` 指向的 private compute-visible registry mirror。
shared registry 与 worker mirror 必须具有完全相同的物理 SHA-256 和 model count；
registry 的现场模型数应为当前完整 inventory。历史演进为 2026-06-30 的 13、
2026-07-14 的 20、移除重复 `HHe-MAIN-02` 后 2026-07-15 的 19；当前
2026-07-18 authority 为 18。readiness 必须与同次
registry model set 逐 source 完全一致并记录 catalog URI/SHA/row count；state entry 不能因
刷新减少。
Installer 在任何 systemd mutation 前都会用同一 strict v1 runtime validator 读取 bounded/no-follow
latest receipt，并逐一比对三个 shared provider、worker registry mirror 的当前 SHA-256 及
shared/mirror model count；minimal、extra、symlink、oversize、
stale、missing 或非 `published` receipt 均拒绝启用。
Wrapper 会把 mode-0600 env 当作固定 key/value 数据解析并 export，不执行其中的 shell；
systemd `UnsetEnvironment=` 与 wrapper 最终 `unset` 会同时清除 user-manager/调用 shell
继承的 `DATABASE_URL`、`PIPELINE_DATABASE_URL` 和全部受支持 libpq selector。
Receipt schema 为 `nhms.scheduler.file_provider_refresh_receipt.v1`，outcome 只允许
`dry_run`、`published`、`already_running`、`failed`、`replace_uncertain`、
`restored_previous`、`published_receipt_failed`。latest 原子替换，history 只留最新 32；
单次 workspace 上限 64 GiB/250,000 entry/depth 32。canonical commit 前产生的 immutable
content-addressed package 不自动删除；receipt 只记录安全相对标识，最多前 256 条、总数
及 truncated，候选总数超过 4,096 时阻断。不要凭目录名批量删除 package 或不确定 temp
residue。

refresh unit 仅在 `nhms-compute-scheduler.service` inactive 时运行，并声明在 scheduler
service 之前排序。registry 提交顺序固定为 worker mirror 先、shared canonical 后；两者使用
同一 prospective model rows 与 `generated_at`，所以成功字节必须完全一致。shared CAS 失败时，
worker mirror 按其 committed preimage 恢复旧 bytes；任何恢复不确定都报
`replace_uncertain`。短暂的 mirror-new/shared-old 窗口不会被当作可执行 generation：每个
Slurm stage manifest 建立前会逐字核对两份 registry，不一致以
`SCHEDULER_REGISTRY_MIRROR_MISMATCH` fail closed，不提交 job。禁止用 `cp` 手工追平 mirror。
registry/mirror 成功后若 readiness 或 state 发布失败，runner 会按
state → readiness → shared registry → worker mirror 的逆提交顺序，用每条 lane 的
committed preimage CAS 恢复旧 bytes。全部恢复才允许 `restored_previous`，并清空 committed
provider evidence；任一 lane 被并发替换、无法读取或无法恢复都保持
`replace_uncertain`，primary receipt 失败时也不得改写成 `published_receipt_failed`。

Canonical replace 前失败时旧文件完整 stat/digest tuple 不变；preimage race 返回
`provider_preimage_changed`。读者在原子 replace 时只能看见完整 old/new。确定的 post-read
失败会恢复经验证的旧 bytes 并报 `restored_previous`；replace/fsync 不确定时返回
`replace_uncertain`，不要宣称回滚。provider 已 commit 但 primary receipt 发布失败时，
预留的本地 mode-0600 emergency record 为唯一 acceptance evidence；用下列命令只重建
receipt，绝不重发 provider：

```bash
scripts/scheduler_file_provider_refresh_once.sh \
  --recover-emergency /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/emergency/<receipt>.json
```

恢复会先比对三个当前 canonical SHA-256 与 worker registry mirror。primary 与 emergency
均失败就是 `replace_uncertain`，必须直接重验四个绑定；journal/stderr 只作诊断。

**Registry cutover gate (#1080) refusal semantics**：refresh 在 canonical registry
replace 前对 prospective vs 上一份 canonical `manifest-last.json` 做逐行分类，并把
`registry_classification` 写进 v1 receipt（`dry_run` / `published` / cutover refusal
outcome 都必须带）。分类桶：`added`（prospective 有、previous 无）、`unchanged`
（同 `model_id` 且 `model_package_uri` / `manifest_uri` / `package_checksum` 逐字节
相等）、`package_changed`（同 `model_id`，`package_checksum` 不同）、`removed`
（previous 有、prospective 无）、`refused`、`declared_cutovers`、
`declared_retirements`（#1433：被 retire 声明放行的 removal，是 `removed` 的子集，
不进 `refused`；老 receipt 没有这个桶，按 0 读）。三个 refusal 原因均在
canonical replace 前退出、非零：

- `registry_cutover_undeclared`：某个已存在 `model_id` 的 `package_checksum` 变了但没有
  匹配的 cutover declaration。先看 `registry_classification.refused` 找到具体 model 与
  old/new checksum；确认漂移是有意后按下述格式提交 declaration，再重跑。
- `registry_cutover_removal_refused`：previous canonical 里的某个 `model_id` 在
  prospective 里消失。触发面**不只是**「动了 `NHMS_BASINS_ROOT` 里的目录」——已注册
  model 的包变 invalid（`*.cfg.ic` 头部畸形、缺 `*.tsd.rl` 且无模板可修等）会被 bulk
  publish 合法 skip，prospective 因此少一行，同样判 removal；而 `--dry-run` 预览
  **看不到**这条拒绝（dry_run 不评估 removal）。两形靠 refusal entry 区分（#1433）：
  带 `status` / `missing_required_files` / `invalid_required_files` 三键 = 包变 invalid
  被 skip（键值就是 publisher 的 not-publishable 判据）；无这三键 = model 目录真没了。
  合法下线走下面的 **retire declaration 恢复顺序**；不打算下线就修包后重跑。
- `registry_cutover_declaration_invalid`：declaration 文件本身或某条 entry 无效。常见
  原因：`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` 指向的文件不存在 / 不可读（已被删除或
  轮转走）、schema 不匹配、`generation` 与 prospective 不一致、`old_checksum`/`new_checksum`
  与实际不符、`effective_cycle_utc` 未对齐 00:00 或 12:00 UTC、超出 24h 过期 / 168h
  未来窗口、entry 里有 duplicate `model_id`、declaration 文件是 symlink/非常规文件、
  超过 256 KiB。

第四个非 cutover 的 refusal 原因（#1832）：

- `calibration_override_invalid`：`config/calibration_overrides.yaml`（或
  `$NHMS_CALIBRATION_OVERRIDES_PATH` 指向的文件）里某条声明加载不了或应用不上。
  receipt 的 `calibration_overrides.error` 带 `error_code` + `message` +
  `entries`（`basin_slug`/`parameter`），直接点名是哪条：
  `CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY`（slug 打错或改名，discovery 里根本
  没这个 basin）、`CALIBRATION_OVERRIDE_UNKNOWN_PARAMETER`（basin 的 `*.cfg.calib`
  里没有这个参数）、`CALIBRATION_OVERRIDE_VALUE_UNPARSEABLE`、
  `CALIBRATION_OVERRIDE_DECLARATION_UNREADABLE` / `_INVALID`。这条拒绝在 canonical
  replace 之前退出，registry 保持上一代；timer 每 tick 都会复现，直到声明改对。
  另外 `calibration_overrides.not_applied` 记录「声明了但这趟没发布」的 basin
  （`reason_not_applied="basin_not_selected_for_this_run"`）——不是错误，但说明这条
  override 本趟没生效。

**分类 `mode`（#1140）**：`registry_classification` 还带一个 `mode` 字段，记录这次 refresh
实际跑的分类分支。`id_only` 只来自 `dry_run`——prospective 行只有 `model_id`/`basin_id`、
没有 checksum，所以观察不到漂移，也不评估 removal；`full` 来自真实 publish 路径的分类
（`dry_run=false`）。cutover refusal 与 gate 之后失败的 receipt，其 `mode` 取决于当时跑的是不是
dry_run——dry_run 下一律是 `id_only`，包括 declaration 失效的 refusal receipt。receipt 校验按 `mode`
而不是 `outcome` 选对账规则，所以一次 dry_run 若在 gate 之后失败（例如 readiness 派生报错），
receipt 会带 `outcome: "failed"` + 真实 reason + `mode: "id_only"` 正常落盘，不再被
`primary_receipt_failed` 顶掉、连 receipt 都不留。`outcome` 与 `mode` 的交叉伪造
（`dry_run` 配 `full`、`published` 配 `id_only`）一律拒。

**升级 pre-#1140 receipt**：#1140 部署之前写下的 receipt 的 `registry_classification` 里
没有 `mode` 字段，这属正常、不是篡改信号；校验对这种老 receipt 回退到按
`outcome == "dry_run"` 选分支，即 #1140 之前的行为。判定版次：比对 `.started_at`
与 #1140 部署时间，或者跑一次 manual refresh 拿新 receipt——新 receipt 一定带 `mode`。

Cutover declaration 是 `nhms.scheduler.registry_package_cutover.v1`（schema：
`schemas/scheduler_registry_package_cutover.schema.json`；参考 example：
`schemas/examples/scheduler_registry_package_cutover.example.json`）。文件路径通过
新增的 optional env `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` 传入 refresh 进程。
**手动 CLI 路径**（自己 `export` 后直接跑 runner）：env 未设置或空值等同于"无
declaration"（只有当没有 `package_changed`/`removed` 时才允许）。**systemd 路径**
不同：空值会在 wrapper 解析阶段就 abort（见下"systemd 路径"）。示例：

```json
{
  "schema_version": "nhms.scheduler.registry_package_cutover.v1",
  "generated_at": "2026-07-15T11:45:00Z",
  "generation": "manifest-b44ab3b785f4",
  "entries": [
    {
      "model_id": "basins_kashigeer_shud",
      "old_checksum": "<previous canonical package_checksum>",
      "new_checksum": "<prospective package_checksum>",
      "effective_cycle_utc": "2026-07-16T00:00:00Z",
      "transition_mode": "replace"
    }
  ]
}
```

`generation` 必须等于本次 prospective 的 registry generation；这个值是
`manifest-<12hex>`（12hex 是 sorted-by-model_id prospective model list 的 SHA-256
前 12 位，**不含**任何 wall-clock 分量）。相同 model set 的重跑 refresh 得到 byte-
identical 的 generation string，所以"先看被拒 receipt -> 拷 generation 到 declaration ->
重跑 refresh"这个循环里，第二次 refresh 一定能匹配 declaration；只有 prospective
model set 真正变了，generation 才会变（这时也必须重新出 declaration）。被拒 receipt
直接带这个值：`registry_classification.generation`（#1433 起）。dry_run receipt 该键
为 `null`——id-only 分类的 prospective 行没有 checksum，其 generation 不是真实
publish 绑定的那个值，**不要从 dry-run 拷**。

操作流程（手动 CLI 路径）：先看被拒 receipt -> 拷 generation / old/new checksum 到
declaration -> 提交 declaration 到 mode-0600 路径 ->
`export NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<path>` -> 重跑 refresh。
`effective_cycle_utc` 必须精确对齐 00:00 或 12:00 UTC，且落在
`[now-24h, now+168h]` 区间。`transition_mode` 有两个值：`replace`（换包，
`new_checksum` 必须是 64 位 hex）和 `retire`（下线一行，`new_checksum` 必须显式写
`null`——缺键与 `null` 语义不同，schema 两条 if/then 钉死这个配对）。

**退役一个 model（retire declaration 恢复顺序，#1433，首选路径）**：这是
`registry_cutover_removal_refused` 的正规出口，无论 removal 来自「删了目录」还是
「包变 invalid 被 skip」。全程适用 #1104 并发禁令。

1. 停 timer：`systemctl --user stop nhms-scheduler-file-provider-refresh.timer`，
   再按上面的成对 status 判据确认 oneshot service 也已退出。
2. 跑一趟**真实 refresh**（不加 `--dry-run`）。它会以
   `registry_cutover_removal_refused` 拒——canonical 字节不变、零发布，这正是
   本次要解决的那条拒绝——然后从这张被拒 receipt 的
   `registry_classification.generation` 拷出本趟 prospective 的 generation。
   **不要用 `--dry-run` 取这个值**：dry_run 走 id-only 分类（prospective 行只有
   id、没有 checksum），既不评估 removal，其 `generation` 也不是真实 publish 会
   绑定的那个值——receipt 里该键为 `null`。

   ```bash
   jq -r '.registry_classification.generation' <receipt>
   ```

3. 写 declaration，entry 形：`model_id` = 要退役的行、`old_checksum` = **previous
   canonical 那一行**的 `package_checksum`（与上一步 generation 同源——都从这张
   被拒 receipt 拷，`old_checksum` 取 `registry_classification.refused` 里那条
   `registry_cutover_removal_refused` 行）、`new_checksum: null`、
   `transition_mode: "retire"`、
   `effective_cycle_utc` 对齐 00:00/12:00 UTC 且在窗口内。generation 绑定、过期
   窗口、cycle 对齐、256 KiB 上限对 retire 逐条同样适用，没有任何 retire 专用豁免。
4. 跑一趟 refresh（timer 路径同样受 gate 审计）。
5. 核对 receipt：`registry_classification.declared_retirements` 里有这一行、
   `refused` 里没有它、`outcome: "published"`。注意声明的是**整份文件**：任何一条
   entry 无效（generation 不符、checksum 不符、retire 了一个还在发布的 model）都会
   让本趟**一条 retirement 也不入桶**，全部按 `registry_cutover_declaration_invalid`
   拒——退役是破坏性动作，不做「部分放行」。
6. 删 declaration（systemd 路径是删掉 EnvironmentFile 里那整行，见下），恢复 timer。

留在原地的 retire declaration 与 replace declaration 一样会过期并拖停每日管线，
清理纪律完全相同。

**遗留路径降级（`--allow-uncovered-cutover`）**：手动 CLI 的 bypass 仍然可用，但
**仅当 declaration 通道不可用时**才用（例如连 dry-run 都跑不起来）。它依旧是审计
红旗：要记 bypass 理由 + 双端 SHA-256 + 事后 declaration 复位。常规退役一律走上面
的 retire declaration。

**共享消费者提示（#1433）**：同一份 declaration 文件也被 scheduler 侧
（`services/orchestrator/scheduler_generation.py`）读。retire entry 对它无意义，
被容忍-跳过：并存的 replace entry 照常匹配，retire entry 永不匹配任何候选。恢复
顺序不需要为 scheduler 侧加步骤，但要知道这个文件是两边共享的。

**systemd 路径（timer/service，#1095 起可用）**：wrapper
`scripts/scheduler_file_provider_refresh_once.sh` 把 EnvironmentFile 当数据解析并只接受固定
key allowlist；`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` **自 #1095 起在 allowlist 内**
（optional，不在 required 集合里）。声明期间按下列顺序操作：

1. 提交 declaration 到 mode-0600 路径（同上，generation / old/new checksum 从被拒
   receipt 拷贝）。
2. 在 node-22 编辑 EnvironmentFile
   `/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-provider-refresh.env`（保持
   mode 0600、非 symlink），**新增一行**
   `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<declaration 绝对路径>`。
3. 等下一次 timer 触发，或手动触发一次：先
   `systemctl --user is-active nhms-compute-scheduler.service` 确认它**不是** active，
   再 `systemctl --user start nhms-scheduler-file-provider-refresh.service`。
   unit 的 `ExecCondition`（`infra/systemd/nhms-scheduler-file-provider-refresh.service`）
   在 scheduler 活跃时会在 ExecStart **之前**短路：`start` 仍然返回 0、unit 被 skip、
   **不产生任何 receipt**（journal 里是 condition failed）。**不要把 `start` 返回 0
   当成"已执行"**，一律以新 receipt 为准。wrapper 自己的 exit 3 只保护直接手工调用
   `scripts/scheduler_file_provider_refresh_once.sh` 的场景。
4. 核对 receipt：`registry_classification.declared_cutovers` 覆盖本次
   `package_changed`，且 outcome 为 `published`（timer 路径的 ExecStart 不带
   `--dry-run`，见上述 unit 文件），reason 不是 `registry_cutover_undeclared` /
   `registry_cutover_removal_refused` / `registry_cutover_declaration_invalid`
   —— 这三个是 outcome=`failed` 时的 refusal reason，不是 outcome 取值
   （receipt outcome 枚举只有 `dry_run` / `published` / `already_running` /
   `failed` / `replace_uncertain` / `restored_previous` / `published_receipt_failed`）。
5. Cutover 落地后**删除整行**。这不是可选的清理，而是主要失效模式：把非空行留着，
   一旦 declaration 过期（`effective_cycle_utc` 超出
   `CUTOVER_PAST_TOLERANCE=24h`，见 `scripts/scheduler_file_provider_refresh.py:107`、
   `:2327`）、文件被删除或轮转掉（`:2283-2289` 的 `OSError` 直接判 invalid）、
   或 declaration 的 `generation` 相对新的 prospective 过期（`:2556+`），
   **每一次**后续 refresh 都会以 outcome=`failed`、reason=
   `registry_cutover_declaration_invalid` 拒跑——每日 timer 管线从此停摆，直到有人删掉这一行。
   前两类（过期 / 文件不可读）是 declaration **加载**失败，在 `:2704-2732` 无条件生效，
   连 zero-drift 和 `--dry-run` 预览都不豁免；generation 过期则在每一次真实 publish
   （timer 路径）上拒跑。检测信号：看最新 receipt 的
   `jq -r '.outcome, .reason'`——`failed` + `registry_cutover_declaration_invalid`
   且没人在做 cutover，基本就是这条 leftover 行。
   同样不要留 `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=` 空值：wrapper 的
   `-n "$value"` 解析检查会对空值直接 fail-fast（bare exit 1，无 stdout），
   service 会启动失败。空值 **不等同于** key 不存在；只有删掉整行才回到"无 declaration"
   的安全默认（此后再有未声明的 package 漂移会照常被 refuse）。

**Consumer-side note (Issue #1081 §8)**：`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`
同时被 scheduler consumer (`services/orchestrator/scheduler_generation.load_
cutover_declaration`) 读取，用于生成 §8 transition decision（warm_continue /
cold_new_model / cold_declared_cutover / 5 个 block_* reasons）。scheduler 在
每次 pass 开始时读一次（D8.1: read-once-per-pass, cached per ProductionScheduler
lifetime），中途修改 declaration 文件不会被生效，直到下一次 scheduler 重启或
下一次 pass 时才重新加载。node-22 systemd EnvironmentFile
`compute.scheduler-dbfree.env` 里必须显式设置这个 env 才能 §8 gating 生效；
未设置 = declaration 缺席 -> 每个 declared-cutover 候选会 block 为
`registry_cutover_declaration_missing`。

**手动 publisher CLI**（`scripts/publish_scheduler_file_registry.py`）：为兼容 #1080 gate，
manual publisher 默认也会跑 cutover gate，语义与 refresh runner 一致；未通过 gate 就
不会替换 canonical。仅在 bootstrap（没有 previous canonical `manifest-last.json`）或
显式一次性 recovery 时使用 `--allow-uncovered-cutover` 跳过（会在 stderr 打印 WARNING）。
常规运维必须走 declaration + 重跑，绝不 default 到 bypass；退役一行走 retire
declaration（见上），bypass 仅在 declaration 通道不可用时才动。

**并发禁令（#1104，operator-gated）**：`nhms-scheduler-file-provider-refresh.timer`
或其 oneshot service 处于活跃状态时，**严禁**运行 manual publisher CLI。CLI 路径
**没有** CAS 防护——`main()` 不传 `expected_preimage`，若 refresh 在 CLI 的
snapshot→commit 窗口内提交，CLI 会静默覆写 refresh 刚发布的 canonical bytes，且
两边都不会出现 `provider_preimage_changed` 证据。这条边界只靠运维纪律保证，代码
不会拦你；CLI 每次启动都会在 stderr 打印一行 WARNING 提醒本条。运行前必须成对确认：

```bash
systemctl --user status nhms-scheduler-file-provider-refresh.timer \
  nhms-scheduler-file-provider-refresh.service --no-pager
```

判据（两条**同时**满足才可运行 CLI）：

- timer 为 `inactive` / `disabled`（`Active: inactive (dead)`）；
- service **不是** `activating` 或 `active`——oneshot service 可能在 timer 停掉后
  仍在执行本次 tick，只看 timer 会漏判。

标准做法：先 `systemctl --user stop nhms-scheduler-file-provider-refresh.timer`，
再重跑上面的成对 status 确认 service 也已退出，然后运行 CLI；跑完
`systemctl --user start nhms-scheduler-file-provider-refresh.timer` 恢复 timer，并用
`systemctl --user list-timers nhms-scheduler-file-provider-refresh.timer --no-pager`
核对下次 tick 已排上。若 status 显示 service 正在跑，等它自然结束，不要 kill——
中途打断会留下未完成的 canonical replace 状态。

**`cutover_gate` audit（R2-A1，v2 summary）**：CLI 每次退出（成功 summary 到 stdout、
失败 error payload 到 stderr）都会写入一个 `cutover_gate` audit 块，schema 是
`nhms.scheduler.basins_file_registry_publish.v2`。三个字段：`mode ∈ {enforced,
bypassed_allow_uncovered_cutover, not_wired}`、`declaration_env`（enforced 时是
`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`，否则 null）、`declaration_present`
（bool，declaration file 是否可读的 regular file；符号链接和权限拒绝均计为 false）。
同一个 audit 块也会 mirror 到 manifest publication receipt 上（`publish_scheduler_
registry_manifest` 返回的 dict 里的 `cutover_gate` 字段），所以 downstream 直接读
`manifest-last.json` 的 companion receipt 也能看到同一份 audit。

第三条通道是 **runner refresh receipt**（#1132）：自动 timer 路径的
`refresh_scheduler_file_providers` 把同一个 audit 块写进
`.../provider-refresh/receipts/latest.json` 的 `.cutover_gate`，`published`、
`dry_run`、cutover refusal、rollback（`restored_previous` / `replace_uncertain`）
和 catch-all failure receipt 都带；只有在 gate 装上之前就失败的 run（lock contention、
provider preimage 冲突）才**整个字段缺席**——不写 `null` 占位，缺席本身就表示"gate 没跑"
（或系 pre-#1132 版本写下的 receipt，见下"升级 pre-#1132 receipt"）。

```bash
# runner receipt（自动 timer 路径的 audit 通道）
jq '.cutover_gate' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json
# 期望：{"mode": "enforced", "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
#       "declaration_present": false}   # 无 cutover 在途时 false 是正常值
```

Runner 路径没有 `--allow-uncovered-cutover`，所以这里的 `mode` 恒为 `enforced`；出现
其它值说明这份 receipt 不是本 runner 写的。`declaration_present` 则让事后 forensics
能区分两种 refusal：`false` = 运维根本没 staged declaration，`true` = staged 了但没覆盖
这次漂移（对照同一 receipt 的 `registry_classification.refused` 定位具体 model）。

任何一次 `--allow-uncovered-cutover` 之后，运维必须 `jq '.cutover_gate'` 核对：

```bash
# 手动 publisher summary（成功走 stdout；失败/refusal 走 stderr 最后一行）
scripts/publish_scheduler_file_registry.py ... | jq '.cutover_gate'
# 期望常规运维：{"mode": "enforced", "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
#              "declaration_present": true}
# 一次性 recovery：{"mode": "bypassed_allow_uncovered_cutover", "declaration_env": null,
#              "declaration_present": false}
```

`mode == "bypassed_allow_uncovered_cutover"` 是 **审计红旗**：必须在 issue/worklog
里留下 bypass 理由、bypass 时刻的 previous canonical SHA-256 以及本次 commit 的
canonical SHA-256，并跟一次 declaration + 正常 refresh 复位。

**升级 pre-#1080 receipt**：如果 `.../provider-refresh/receipts/latest.json` 是升级前
（无 `registry_classification` 字段）的 published receipt，第一次 post-#1080 refresh
仍然会正常 publish 并把新 receipt 写入 `latest.json`；不需要人工清 stale receipt。
`_publish_primary_receipt` 用 lenient reader 只读 `(started_at, run_id)` 做 history/
latest.json 的 monotonic-order 排序，legacy shape 不会触发 `receipt_classification_required`。
写入的新 receipt 通过 `_validate_receipt` 严格校验，之后 `install_node22_scheduler_
file_provider_refresh.sh --enable`（内部走 `validate_current_receipt`）会看到完整
post-#1080 shape。

**receipt 契约升级/回滚兼容性（升级 pre-#1132 receipt）**：#1132 部署之前写下的 receipt
（包括正常的 `published`）同样没有 `.cutover_gate`。#1144 起这**不再是需要 operator 判断的
软信号**：`outcome` 为 `published`/`dry_run`、或 `reason` 为三个 registry-cutover refusal
之一的 receipt 必须带该字段，schema 与 `_validate_receipt` 同批拒绝缺席语料（运行时 reason
为 `receipt_cutover_gate_required`）。后果落在
`install_node22_scheduler_file_provider_refresh.sh --enable` 上：
它内部走 `validate_current_receipt`，读到 pre-#1132 的 published
`latest.json` 会直接抛 `emergency_record_invalid`（`phase="receipt"`），与"receipt 被篡改"
同码——升级后第一次 `--enable` 失败时先 `jq 'has("cutover_gate")'` 看是不是这个原因。
处置与 pre-#1080 段（见上）一致：跑一次**成功**（`outcome == "published"`）的 manual
refresh 把 `latest.json` 重写掉，新 receipt 一定带 `.cutover_gate`，`--enable` 随即通过。
旧 receipt 不会阻塞 refresh 的写路径——`_publish_primary_receipt` 同样用 lenient reader 只读
`(started_at, run_id)` 排序。注意 refused/failed 的 refresh 也会重写 `latest.json`，但
`outcome != "published"` 仍被 `validate_current_receipt` 拒绝，必须拿到一次真正 published
的 receipt 才算复位。gate 装上之前就失败的 run（lock contention、provider preimage 冲突）
其 outcome 既非 `published`/`dry_run` 也不带 refusal reason，缺席依旧合法，无需处置。

**回滚方向（#1143：post-#1132 receipt + pre-#1132 代码）**：反向 skew 的症状串是
`--enable` → `validate_current_receipt` → `emergency_record_invalid`——与升级方向同码，
但根因相反：旧校验器的顶层键校验是**精确 allowed-set**（`RECEIPT_OPTIONAL_KEYS` 不含
`cutover_gate`），带该键的新 receipt 被判 `receipt_shape_invalid` 后就地转码。这是
**版本 skew，不是 provider 漂移**——`emergency_record_invalid` 在别处意味着"落盘证据与
实际 provider 不一致"，回滚（热修 / bisect / 紧急回退）的高压场景里最容易被误读成数据
事故。判别：`jq 'has("cutover_gate")' .../provider-refresh/receipts/latest.json` 为
`true`，而当前 checkout 的 `RECEIPT_OPTIONAL_KEYS`（`scripts/scheduler_file_provider_refresh.py`）
不含它，即为本条。同期落在 emergency slot 的 receipt 同样带该键，
`reconstruct_primary_receipt` 在旧 checkout 上也会以 `emergency_record_invalid` 失败——
受影响的不止 `--enable`。处置：用**旧代码**跑一次**成功**（`outcome == "published"`）的
manual refresh，让 `latest.json` 回到旧 shape 后再 `--enable`（refused/failed 的 refresh
也会重写 `latest.json`，shape 问题消失但 `outcome != "published"` 仍被拒，同升级方向）；
注意 refresh 自愈**只覆盖 `latest.json`**——emergency slot 是独立落盘文件，普通 refresh
不读它，`--recover-emergency` 需要在**新代码** checkout 下执行（只有新校验器认这份
shape）。行为全程 fail-closed（不写坏 canonical provider、不静默降级），
且写路径不被旧 receipt 阻塞（`_publish_primary_receipt` 的 lenient reader 只读
`(started_at, run_id)` 排序、不跑 `_validate_receipt`），所以下一次旧代码 refresh 即自愈。
**不要手工删 `latest.json`**：它同时是 monotonic-order 的排序锚点，删掉只会把可判别的
版本 skew 变成无锚点的空白现场，而正确处置（跑一次 refresh）本来就会覆盖它。

上述两个方向是"**receipt 新增顶层可选键**"的通用后果，不是 `cutover_gate` 一次性的坑：
精确 allowed-set 天然单向兼容——新代码在 **shape 层**认旧 receipt（少可选键，放行；
条件必填规则另算，如 #1144 的 presence 条件仍会拒掉旧 published receipt），旧代码不认新
receipt（多未知键，`receipt_shape_invalid`）。嵌套键同理（#1140 给
`registry_classification` 加的 `mode` 在 pre-#1140 校验器上以
`receipt_classification_invalid` 拒绝，传导路径与处置完全相同）。速查：

| 方向 | 症状 | 阻塞 `--enable`? | 处置 |
| --- | --- | --- | --- |
| 升级：pre-#1080/#1132 receipt + 新代码 | `validate_current_receipt` 报 `emergency_record_invalid`（缺 `registry_classification` / `.cutover_gate`） | 是（refresh 写路径不阻塞） | 新代码跑一次**成功** manual refresh |
| 回滚：post-#1132/#1140 receipt + 旧代码 | 同码 `emergency_record_invalid`（多未知键 → `receipt_shape_invalid` / `receipt_classification_invalid`）；emergency slot reconstruct 同样失败 | 是（refresh 写路径不阻塞） | 旧代码跑一次**成功** manual refresh；emergency slot 用新代码 `--recover-emergency`；勿删 `latest.json` |

下一次给 receipt 加顶层（或嵌套 exact-set 内）可选键时，在本小节的表里加一行即可，
不要再散落一次性备注。

启用 refresh timer 前必须 `jq '.registry_classification'
/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json`
核对：`previous_registry_sha256` 等于 shared canonical 的实际 SHA-256、`new_registry_sha256`
等于本次刚 commit 的 canonical SHA-256、`refused.total == 0`、`declared_cutovers`
里的 entry 与 `entries` 数量与 declaration 完全一致。任何 `refused` 都禁止把 timer
enable；那说明当前 declaration 与 prospective 不匹配、需要重新提交。
同一次核对里还要 `jq '.cutover_gate'` 确认是 `{"mode": "enforced", "declaration_env":
"NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", "declaration_present": <bool>}`。#1144 起这项
核对由 `--enable` 自己硬性执行：published receipt 缺 `.cutover_gate`，
`validate_current_receipt` 直接以 `emergency_record_invalid` 失败，没有"人工确认一下再
enable"的余地。字段缺席只有两种来源——gate 装上之前就失败的 run，或 pre-#1132 版次
（见上"receipt 契约升级/回滚兼容性"）；前者 outcome 本来就不是 `published`，同样过不了
`--enable`。两种情况一律先跑一次**成功**的 manual refresh 拿到带 `.cutover_gate` 的新
receipt。

成功 manual refresh 后才建立稳态：

```bash
scripts/install_node22_scheduler_file_provider_refresh.sh --enable
systemctl --user status nhms-scheduler-file-provider-refresh.timer \
  nhms-scheduler-file-provider-refresh.service --no-pager
systemctl --user list-timers nhms-scheduler-file-provider-refresh.timer --no-pager
```

timer cadence 为每日 02:15 UTC 加最多 30 分钟 jitter，严格小于 168 小时；oneshot
service 在 tick 间应为 inactive。refresh unit 的安装、失败和回滚不得 enable/disable、
start/stop 或替换 `nhms-compute-scheduler.*`。若安装、manual refresh 或 live scheduler
proof 任一步失败，执行：

```bash
scripts/install_node22_scheduler_file_provider_refresh.sh --rollback
# 脚本按 install 前记录恢复 refresh 初态，并断言 scheduler units 完全未变。
```

Live acceptance 还必须把 receipt -> 三 provider digest -> scheduler pass/candidate/run ->
实际 Slurm stage job/terminal -> 同一 run 的全新 forcing/runs/states leaf 串起来，并从
node-27 同一 NFS 视图核对 owner/group/mode/default ACL 与 `nwm` 访问。旧 forcing 复用、
synthetic ACL probe、未绑定/非 terminal job 都不算通过。只有这一链通过后保留 refresh
timer enabled/active；所有退出路径恢复 scheduler 初始状态并确认无 issue-owned job。

前端全国总览的静态边界仍从 Basins 真相源刷新；基础河网不再生成全国 GeoJSON，
而是由 active `core.model_instance` 对应的 `core.river_segment` 通过 national
river-network MVT 自动纳入。新增流域只需完成 registry seed/active model 和
`stream_type` 派生，不会扩大首屏静态包。`zhaochen_hhy` 已由 `hhe` 覆盖，domain
生成仍显式排除 HHY：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
/home/nwm/.local/bin/uv run python scripts/geo/build_national_domain_geo.py \
  --basins-root /home/ghdc/nwm/Basins \
  --model-packages-root /home/ghdc/nwm/object-store/models/direct_grid_variants \
  --exclude-basins zhaochen_hhy
jq -r '.features | length' apps/frontend/public/geo/national-basin-domain.geojson
curl -fsS http://127.0.0.1:8080/api/v1/layers \
  | jq '.data[] | select(.layer_id == "river-network") | .metadata.source_generation'
```

2026-07-19 当前 domain authority 是 18 个业务流域，包含 6 个新增流域
`dth_ls`、`dth_zj`、`hhe`、`huai_main`、`jialingjiang`、`lh_gl`，不包含 HHY。
历史静态 river GeoJSON（59,702 features，约 45 MB 解码）已退出运行关键路径；
基础 river-network MVT 只负责全国底图常显，不参与点击。HHE 与其他业务流域的点击、流量上色
统一来自 `hydro-national/q_down` live MVT；HHE model package 的 `river.shp.Type`
必须回填到对应 output segment，低 zoom 优先按该真实河级筛选，历史缺失 `Type` 的
segment 才回退到流量分位筛选。MVT feature 必须同时携带 `river_segment_id`、
`basin_version_id` 和 `river_network_version_id`，否则前端不得打开河段时序弹窗。
全国总览的 basin API 请求固定带 `has_display_product=true`；因此把 HHY 的
`core.basin_version.valid_to` 置为退役时间后，历史 run 仍保留但不再进入展示列表。
不要用 `active_flag` 做这项退役：当前 Basins importer 创建的版本默认都是 false，
误用它会把 18 个现行流域一起隐藏。

以下是 **2026-07-01 历史展示快照**，不是当前 registry 或 display inventory
authority：当时 domain 输出 13 个 basin；river 输出 20,100 条 feature，
覆盖 `basins_heihe`、`basins_hetianhe`、`basins_kashigeer`、`basins_keliya`、
`basins_qhh`、`basins_qinyijiang`、`basins_tailanhe`、`basins_weiganhe`、
`basins_xinanjiang_upstream`、`basins_zhaochen_bst`、`basins_zhaochen_hhy`、
`basins_zhaochen_mc`、`basins_zhaochen_wem`。目前没有对应 2026-07-18 inventory
的 river feature 总数现场真值；不得把 20,100 外推或改写成新的 river 数量。
刷新后重新部署前端，公网
`https://test.nwm.ac.cn` 才会看到新增流域边界和一致的缩放河网底图。

显式补跑某个 00/12 UTC 周期时，使用 node-22 的 DB-free 入口脚本，不要手工
拼 `lookback/cycle-lag`，也不要改 scheduler systemd env：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM

# 先 plan，确认 source_cycles/candidates/blocked_candidates。
scripts/ops/node22-run-cycle-once.sh \
  --cycle-time 2026-06-27T00:00:00Z \
  --plan

# 确认后提交。省略 --basin-id 会使用 file registry 中的全部 active basin。
scripts/ops/node22-run-cycle-once.sh \
  --cycle-time 2026-06-27T00:00:00Z \
  --submit
```

该脚本 source `infra/env/compute.scheduler-dbfree.env`，调用
`plan-production --cycle-time ... --disable-backfill`。`--cycle-time` 固定单一
source cycle，避免恢复运行被更早的历史 backfill 缺口劫持；`--disable-backfill`
只影响本次显式补跑，不改变 timer 的常规 backfill 策略。需要定向少数流域时可重复
传 `--basin-id basins_xxx`；需要只补某个 source 时传 `--source gfs` 或
`--source IFS`，不传则按 scheduler env 跑全部生产 source。

如果没有长驻 `node27_autopipeline.py` 进程但 cron 日志持续刷新，这是正常的
bounded cron 模式，不代表 ingest 停摆。

#### 3.1.4 流域投递规范（发给建模者；平台侧照此验收）

新流域或更新版本交到 `/volume/nwm/Basins/` 之前，建模者必须满足下面七条。

> **先分清两个 Basins 根，别投错地方**（2026-08-22 实测）：
>
> | 路径 | 载体 | 谁读它 |
> |---|---|---|
> | node-22 `/volume/nwm/Basins` | 本地 175 T xfs (`/dev/sda`) | **投递落点与权威**：`NHMS_BASINS_ROOT`，scheduler / baseline publish / provision 全走它 |
> | node-22 `/ghdc/data/nwm/Basins` ≡ node-27 `/home/ghdc/nwm/Basins` | NFS `ghdc:/home/ghdc`（同 inode，确为一份） | node-27 ingest 的 `BASINS_ROOT`；#1699 的 staged 树也在这儿 |
>
> 两棵树**内容本就不同**且是有意的：权威根放原始投递名（`CJ-DTH-XJ`），NFS 树放
> only-root staging 后的名字（`DTH_XJ`）。所以「两端不一致」不等于漂移——比对前先确认
> 比的是同一棵树。建模者只投 `/volume/nwm/Basins`，NFS 侧由平台按需 stage。
不满足的交付会在上线四跳的第 1 跳（baseline publish）或第 2 跳（provision）失败，
或者更坏——静默上线成一个错的永久身份。

**1. 目录名就是永久 `basin_id`，投递后改不了。**
顶层目录名经 `_slug_id()`（`[^0-9a-zA-Z]+` → `_`，转小写）变成 `basin_id`：
`Huai-MAIN` → `basins_huai_main`，`SHJ-2SHJ` → `basins_shj_2shj`。
所以取短名、**不带分区/单位/人名前缀**。`basin_id` 一旦进注册表就嵌进
`basin_version_id` 和 dg `model_id` 的 hash 输入，改名等于换身份，
必须走一整套「新 id 注册 + 状态克隆 + 旧 id 退役」（见 #1698 / #1701），不是 rename。

**2. 二级容器目录名会成为 `basin_id` 前缀。**
`HYS/BST/input/BST/` → `basins_hys_bst`。要么别用二级容器，要么容器名本身也当作
永久标识来取。**深过两级的布局不会被发现**——`a/b/c/input/c/gis/` 在
`basins_discovery._find_model_dirs` 和两份 geo builder 里都被跳过，不报错、直接不存在。

**3. 结构固定为 `<顶层名>/input/<模型名>/`。**
`gis/`（含 `domain.shp` / `river.shp` 及其 `.shx`/`.dbf`/`.prj`）在
`input/<模型名>/gis/` 下。注意 `<模型名>`（`shud_input_name`）**可以**与顶层目录名不同，
这是 only-root staging 的正常结果（`SHJ-2SHJ/input/2SHJ/`）；但它决定
packaged IC 的规范路径 `<package>/<模型名>.cfg.ic`，写错就是 IC 探测失败。

**4. `cfg.ic` 首行必须是 3 列。**
少一列的 header 会让 SHUD 读 IC 失败。平台侧上线时会在 **staging 副本**上补
`\t0.000000`（#1699 补过 4 个流域），**不改源**——但这属于救火，交付时就应该是对的。

**5. `umask 002`。**
`/volume/nwm/Basins` 的默认 ACL 给 `nwmuser` 组写权限，但投递者的显式权限位会**覆盖**默认 ACL。
2026-08 出现过整棵树 20304 个文件对组只读、平台无法 staging 的情况。
交付后自查：`find <你的目录> -not -writable | wc -l` 应为 0（以 `nwmuser` 组成员身份）。

2026-08-25 复测：全树 **220** 个（不是历史峰值 20304），全部属 `st_zhanghx`、模式
`-rw-r-----` / `drwxr-s---`，集中在 7 个黄河流域（`longmen_zhi_sanmenxia` 32、
`lanzhou_zhi_hekouzhen` 32、`hekouzhen_zhi_longmen` 32、`sanmenxia_zhi_huayuankou` 31、
`neiliuqu` 31、`longyangxia_zhi_lanzhou` 31、`longyangxia_yishang` 31）。
**它复发在最新一批投递上**，即本条约束尚未被投递方执行；与 `forcing/` 零交集
（`find ! -writable -path "*/forcing/*"` = 0），不阻塞 forcing 清理。

本条即 #1702 长期方案的 (b) 支：**由投递者保证 `umask 002`**。另一支 (a)——把
`Basins/` 的 owner 改成平台账号 `nwm`——尚未采纳，属 owner 决策；在它落地之前，
(b) 是唯一在册的约束，一次性的 root `setfacl` 修复只是补救、不是机制。

**6. 率定更新原地覆盖，但必须通知平台。**
只改参数、不改 mesh/river/IC 的率定更新可以原地覆盖同一目录。
但**平台需要在覆盖前后各做一次动作**：包重新发布 + 状态延续克隆（`state_compatibility` 门，见 §5.7）。
不通知就覆盖 = 新包与旧状态之间没有克隆行，`REQUIRE_FORECAST_WARM_START=true` 下
下一个 cycle 直接 `state_clone_cold_start_approval_required` 停摆。

平台侧执行这一步时**顺序是硬的，且要留证**：先写克隆行、再发布 manifest。
反过来会在 `REQUIRE_FORECAST_WARM_START=true` 下 stall 一个 cycle。判定顺序一律看两份
receipt 的 `generated_at`，**不要看 `manifest-last.json.bak-*` 的 mtime**——备份是保留源
时间戳的拷贝，mtime 反映的是上一次发布，不是本次。#1698 的实测形状可照抄：

```text
receipts/<basin>-<cutover>-apply.json   generated_at 2026-08-22T06:42:07Z
                                        cloned_pair_count 2 / dry_run false /
                                        invocation_outcome complete
receipts/manifest-publish-<N>.json      generated_at 2026-08-22T07:02:41Z
                                        introduced_model_ids == 预期新身份集合
```

即克隆先于发布约 20 分钟。**不要把 provider refresh 的时序当兜底**——它只是碰巧
掩盖过顺序错误，不是保护机制。

**7. 改 mesh / river / IC 视为新版本，不是更新。**
这三者进 8 面指纹；变了就不是同一个模型，状态不可延续，必须冷启动或走新 id。
交付时明确写清属于第 6 条还是第 7 条。

**平台侧责任（不要建模者自己做）**：

- 旧目录**不要自删**。退役由平台 `mv` 到 `/volume/nwm/Basins-retired/issue-<N>-<slug>/`
  （同 xfs，rename 不拷贝），保留 90 天后由 owner 决定删除。自删会让还在引用该路径的
  注册行失去溯源根。
- `forcing/` 子目录（IDW 代站 CSV）**不要再带**。direct-grid 已不读：62 行注册表的
  `source_policy.forcing_source` 全部是 `node27_raw_handoff`，运行时 forcing 走
  object store（`manifest["forcing"]["forcing_uri"]`），从不读 Basins 树里的 CSV。
  2026-08-25 清理已执行（#1702 第 3 项）：**15 个目录全清**，共 **10040 个条目 / 62 G**
  移到 `/volume/nwm/Basins-retired/forcing-cleanup-20260825/`，全树 **66 G → 544 M**。
  清理**按 §5.5.1 的纪律**——清空目录、保留目录、不改名（含 `tailanhe/focing`
  这个拼写错误）。

  > `heihe/forcing`（12 G / 1711 文件）一度被错划进第 2 项「整目录退役」而缓做。
  > **划错了**：`heihe` 是活的生产流域（注册表 2 行 `active`、`basins_heihe_shud`
  > 的 `active_flag = t`、350 条 published run、在展示的流域集里），它 12 G 里的
  > 模型本体只有 `input/heihe/` 9.3 M，其余全是上一版率定留下的旧代站 CSV。
  > **流域是活的、forcing 是旧的**——属第 3 项，不属第 2 项。判一个目录该不该
  > 整体退役，看注册表和 `core.model_instance`，不要看它的体积。

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
- 安装仓库内 `infra/systemd/nhms-display-api.service`，停掉旧的手工 uvicorn；
- 由 user systemd 在 `127.0.0.1:${NHMS_DISPLAY_API_PORT:-8080}` 启动
  `${NHMS_DISPLAY_WORKERS:-2}` 个 worker，失败自动恢复；
- 跑 `/health` 与 `/api/v1/models?limit=1` basin_id smoke check。

确认当前 live 状态：

```bash
cd /home/nwm/NWM
systemctl --user is-enabled nhms-display-api.service
systemctl --user is-active nhms-display-api.service
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
tail -n 200 /home/nwm/autopipe-logs/autopipe.log

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
systemctl --user list-timers 'nhms-compute-scheduler.timer' --no-pager
find /ghdc/data/nwm/object-store/runs -maxdepth 1 -type d \
  -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort | tail -20
```

## 4. 业务流程

当前物理流程按数据面理解：

```text
node-27 download timer
  -> downloads GFS/IFS raw cycles to shared NFS object-store
node-22 DB-free scheduler timer / Slurm
  -> consumes node-27 raw manifests from shared NFS
  -> submits per-basin GFS/IFS convert/forcing/forecast/state-save-QC work
     concurrently through Slurm Gateway/sbatch
  -> Slurm runs compute jobs on allocated compute nodes
  -> produces forcing and SHUD run artifacts
  -> writes shared NFS object-store/published roots
node-27 cron autopipe
  -> scans /home/ghdc/nwm/object-store/runs
  -> seeds basin registry when needed
  -> applies object-store forcing-domain handoff, registers/parses runs
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

数据库文件自 2026-08-06 起分布在**两块设备**上。容器 `nhms-db` 由裸
`docker run` 创建（无 compose、无 systemd unit），三个 bind mount 缺一不可：

| 宿主机路径 | 容器路径 | 设备 | 内容 |
|---|---|---|---|
| `/home/nwm/nhms-pgdata` | `/home/postgres/pgdata/data` | `/dev/mapper/ubuntu--vg-home`（1.7 TB，与 object store 共卷） | 主 `pg_default` 表空间 |
| `/data/GHDC/nwm-archive/nhms-tablespace` | `/home/postgres/pgdata/tablespaces/ghdc` | `/dev/md0`（15 TB，**同时承载归档根 `/data/GHDC/nwm-archive`**） | 表空间 `ghdc`：`river_timeseries` 的 `_hyper_3_10`/`_hyper_3_14`、`forcing_station_timeseries` 的 `_hyper_1_12`/`_hyper_1_13` 及其全部索引，约 502 GB |
| `/home/nwm/nhms-evidence` | `/var/lib/postgresql/evidence` | 同 1.7 TB 卷 | evidence 输出 |

注意第二行：DB 数据与归档层现在**共用文件系统**，这是对 2026-07-26
"归档 FS 不得承载 pgdata" 边界的一次**有记录的例外**（成因、代价与承受条件见
`docs/adr/0002-node27-timeseries-hot-cold-tiering.md` "Amendment (2026-08-06)"）。
运维含义：mover ↔ retention 死锁已随归档车道退役消失（#1370），但它的余量
告警也一并消失——DB 在 `ghdc` 上的增长现在**无人观测**，只能靠下面的手工核查。

容量核查必须**两块盘都看**：`df -h /home /data/GHDC`，而且必须**手工**看：
归档车道已随 #1370 永久退役（ADR 0002 Revision 2026-08-11），治理 receipt
不再有 `archive_root` 块，也不再读 `NHMS_ARCHIVE_FREE_SPACE_{WARN,REFUSE}_BYTES`
——`/dev/md0` 现在**完全没有**自动余量观测。receipt 仍在的 `pgdata_root` 只 `du`
`/home/nwm/nhms-pgdata`，DB 体量**少报**迁走的字节。归档层体量单独量：
`du -s --exclude=nhms-tablespace /data/GHDC/nwm-archive`（表空间在归档根下面，
不排除会多报约 502 GB）。历史口径偏差记在 issue #1290。

重建 `nhms-db` 容器的流程见
`docs/runbooks/tier-node27-timeseries-storage.md` §4.3.3；**不要**拿
`infra/docker-compose.dev.yml` 当模板，那是本地 dev 栈。

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
/home/nwm/autopipe-logs/autopipe.log
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

#### 5.5.1 清理已注册流域的 `forcing/`：清空目录，不要删目录（#1813 / #1702 第 3 项）

`forcing/` 下的 IDW 代站 CSV direct-grid 已不读，可以清理。但清理方式决定它是不是
**真 no-op**，因为 basins 包身份对 `forcing/` 的依赖不是一刀切的（裁定见
[ADR 0006](../adr/0006-forcing-csv-out-of-basins-package-identity.md)）：

| 对已注册流域的操作 | 包身份 | 下次 baseline publish |
|---|---|---|
| 删除/修改 `forcing/*.csv`，**保留** `forcing/` 目录 | 不变 | 无 cutover |
| 整个 `forcing/` 目录 `mv` 走或删除 | **变** | 需要逐流域 declared cutover |
| 把 legacy `focing/` 改名成 `forcing/` | **变** | 需要 declared cutover |

原因：CSV 载荷证据（数量、字节、聚合校验和）自 `basins.package.v2` 起已不进
`content_sha256` / `package_checksum`，discovery 也不再把 `forcing_csv_count` 写进
inventory；但 `forcing_dir` / `forcing_dir_original_name` 仍在 inventory 里（打包要靠
它们定位源目录），它们随目录存在与否变化，进而改变
`source_inventory_checksum`——而 cutover 门把该字段算作 model identity
（`scripts/scheduler_file_provider_refresh.py:164`）。目录是结构事实，载荷不是。

所以清理动作是：

```bash
# 已注册流域：搬走 CSV，留下空目录
ssh -p 32099 nwm@210.77.77.27 \
  'set -e
   d=/home/ghdc/nwm/Basins/<basin>/forcing
   dest=/home/ghdc/nwm/Basins-retired/forcing-csv-$(date +%Y%m%d)/<basin>
   mkdir -p "$dest"
   find "$d" -maxdepth 1 -type f -name "*.csv" -exec mv -t "$dest" {} +
   ls -A "$d" | wc -l   # 期望 0；目录本身必须还在'
```

留一个空目录的代价是零，换来的是清理当天和往后每次 publish 都不触发 cutover。

**新流域投递**则相反：新流域根本不带 `forcing/` 是对的——它没有历史身份要延续，
首次 publish 不存在 `package_changed`。投递规范禁止再带 `forcing/`，只约束新投递，
不要拿它去反推已注册流域可以直接删目录。

### 5.6 新增或恢复流域的运维入口

后续增加新的 `Basins/` 流域时，当前生产入口固定为：

| 目标 | 节点 | 入口 |
| --- | --- | --- |
| seed/register/ingest/display coverage | node-27 | `scripts/node27_autopipe_cron.sh` -> `scripts/node27_autopipeline.py` |
| 刷新可计算模型清单 | node-22 | `scripts/publish_scheduler_file_registry.py` |
| 重启展示 API | node-27 | `scripts/ops/start-display-api.sh` |

不要把新增流域做成 qhh/heihe/kashigeer 的一次性手工流程。标准流程：

1. 把流域源数据放到共享 Basins 根：

   ```text
   node-22 view: /ghdc/data/nwm/Basins/<basin>...
   node-27 view: /home/ghdc/nwm/Basins/<basin>...
   ```

   目录必须允许 node-27 的 `nwm` 用户读取和进入。跨用户从 node-22 复制
   Basins 源时，不要保留源端私有权限；复制后至少确认：

   ```bash
   ssh -p 32099 nwm@210.77.77.27 \
     'find /home/ghdc/nwm/Basins/<basin> -maxdepth 3 -type d | sort | head -40'
   ```

2. 在 node-27 走 autopipe wrapper，而不是直接绕过 wrapper 调 Python：

   ```bash
   ssh -p 32099 nwm@210.77.77.27
   cd /home/nwm/NWM
   bash scripts/node27_autopipe_cron.sh
   tail -n 240 /home/nwm/autopipe-logs/autopipe.log
   ```

   wrapper 会从 `infra/env/node27-ingest.env` 加载 writer DB、NFS object-store
   和 `BASINS_ROOT`，并阻断 display env、ambient libpq env、node-22 historical
   DB env。`scripts/node27_autopipeline.py` 是实现入口：发现 `Basins/` inventory
   与 `object-store/runs/`，seed 缺失 basin registry，应用 forcing-domain
   handoff，解析 run，并刷新 display coverage。它是幂等的，后续新增流域也走
   同一入口。

3. 在 node-22 生成 baseline staging，然后在 node-27 provision GFS/IFS 两个
   source-scoped direct-grid variant；禁止把 baseline/IDW registry 直接发布为
   canonical。完整命令和原子发布顺序见本手册第 3.3 节。完成后必须证明：

   - 每个流域恰有 GFS/IFS 两行；
   - `resource_profile.forcing_mapping_mode` 只有 `direct_grid`；
   - station binding 的 `grid_id`、`grid_cell_id` 和经纬度来自对应
     GFS/IFS 0.25° 原始格点；
   - canonical consumer 保持 `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`。

   `NHMS_SCHEDULER_MODEL_IDS` 和 `NHMS_SCHEDULER_BASIN_IDS` 正常保持为空；
   不要为了新增流域在生产长期写死单个 basin。direct-only canonical 原子发布后，
   `nhms-compute-scheduler.timer` 的后续 tick 才会按 00/12 UTC 业务 cycle 走
   Slurm 计算。

4. 展示 API 不负责 seed 新流域。只有代码、env、端口或 display runtime
   变更后，才用以下入口重启：

   ```bash
   ssh -p 32099 nwm@210.77.77.27
   cd /home/nwm/NWM
   bash scripts/ops/start-display-api.sh
   ```

5. 新增流域完成后的最低验收：

   ```bash
   # node-27: API 能枚举新 basin；有 published run 后 has_display_product=true 才会出现
   curl -fsS 'http://127.0.0.1:8080/api/v1/basins?limit=500'
   curl -fsS 'http://127.0.0.1:8080/api/v1/basins?has_display_product=true&limit=500'

   # node-22: scheduler registry 包含新增 model
   ssh -p 32099 frd_muziyao@210.77.77.22
   cd /scratch/frd_muziyao/NWM
   .venv/bin/python -c 'import json; from pathlib import Path; p=json.loads(Path("/scratch/frd_muziyao/nhms-prod/object-store/scheduler/registry/manifest-last.json").read_text()); print("\n".join(sorted(item["model_id"] for item in p.get("models", []) if "model_id" in item)))'
   ```

   `has_display_product=true` 只代表已有发布 run 的流域；新流域完成 registry
   但尚未跑出 SHUD run 时，应先出现在普通 `/api/v1/basins` 和 scheduler
   registry 中，等 22 产出 run、27 autopipe ingest 后再进入展示产品列表。

### 5.7 率定参数更新（recalibration）的 warm carry-over

**适用**：某流域只改了率定参数（`cfg.calib` + `CALIB/*` + `cfg.para`），mesh /
river / gis / `cfg.ic` 全不变，需要让新包 `M1'` **接着** 老包 `M1` 的状态算下去，
而不是冷启动。任何其他 hydrologic-core 面（mesh / river / lake / soil / geol /
land / `.sp.att` 非 `FORC` 字段 / `cfg.ic`）有变化，工具会 fail-closed 拒绝——这是
对的，新 `cfg.ic` 意味着建模侧声明了新起点。

**执行节点：node-22（唯一）。** 它是唯一同时能访问 NFS canonical state index 与
node-22 本地 scratch mirror 的机器；node-22 本身 DB-free，recalibration 模式只写
文件索引、不取任何 DB 句柄。不要在 node-27 上跑这个工具。

**必须一次写两份索引。** canonical（`/ghdc/data/nwm/object-store/scheduler/state-index/`）
与 scratch mirror（`/scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/`）
在同一次调用里写入同一个行对象，两份序列化字节完全相同——这正是后续 copyback merge
能走 `current == source_entry` 无冲突分支的前提。只写一份会在 effective cycle 之前
留下两份不一致的索引。

```bash
ssh -p 32099 frd_muziyao@210.77.77.22   # 必须是 provider 属主 frd_muziyao
cd /scratch/frd_muziyao/NWM
export PATH=$HOME/.local/bin:$PATH

CANONICAL=/ghdc/data/nwm/object-store/scheduler/state-index/index-last.json
MIRROR=/scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/index-last.json
REGISTRY=/scratch/frd_muziyao/nhms-prod/object-store/scheduler/registry/manifest-last.json
RECEIPT_DIR=/scratch/frd_muziyao/nhms-prod/workspace/recalibration
RUN_TAG=huai-2026081512   # 流域 + --cutover-time；每次调用换一个，receipt 路径不得重复

# 1) dry-run：跑完全部校验与八面门，但不写任何索引行
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.node22_clone_direct_grid_cutover_states \
  --transfer-mode recalibration \
  --object-store-root /scratch/frd_muziyao/nhms-prod/object-store \
  --state-index "$CANONICAL" \
  --mirror-state-index "$MIRROR" \
  --variant-registry "$REGISTRY" \
  --pairs huai_dg_gfs_v1:huai_dg_gfs_v2,huai_dg_ifs_v1:huai_dg_ifs_v2 \
  --cutover-time 2026081512 \
  --receipt "$RECEIPT_DIR/$RUN_TAG-dry-run.json"

# 2) 逐项核对 dry-run receipt 后再执行（--apply）
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.node22_clone_direct_grid_cutover_states \
  ... 同上 ... --apply \
  --receipt "$RECEIPT_DIR/$RUN_TAG-apply.json"
```

判读口径：

- `--pairs` 是 `<M1_model_id>:<M1prime_model_id>` 列表，按 **model_id** 直接在
  registry payload 里解析。**不要**指望按 baseline 索引的 variant map：重跑
  provision 脚本产出的 `M1'`，其 `resource_profile.baseline_model_id` 仍指向最初的
  baseline，压根表达不了 `M1→M1'` 这一对。
- 每对都在写任何行之前 fail-closed 校验：两侧 model 行存在、两侧包根解析为目录、
  两侧都判定为 direct-grid、两侧 `direct_grid_source_id` 归一化后相等、`M1 != M1'`。
  GFS/IFS 是两个 model_id，写成两对。
- receipt 就是这次 carry-over 的**声明凭证**：含 pairs、`t*`、`transfer_mode`、每对的
  八面指纹、`clone_gate_kind=state_compatibility`、两侧的
  `model_package_version`/`checksum`、两份索引路径与各自写入结果，以及
  `evidence_fingerprint_cross_check=skipped_no_recorded_value`（provision 脚本不记录
  `hydrologic_core_fingerprint`，因此交叉校验显式豁免而非拿刚算出的值自证）。
  receipt 以 `O_EXCL` 写入，路径已存在会直接失败——不要覆盖旧 receipt。
- **每次调用必须用互不相同的 receipt 路径**（按流域 + `t*` 命名，如
  `huai-2026081512-dry-run.json`）：`O_EXCL` 下重复路径会让一次本来干净的调用直接失败
  （post-loop 写 receipt 时 `FileExistsError`），而实际上什么问题都没有。逐流域跑、
  同一流域 dry-run 与 `--apply` 各一次，路径都要各自唯一。
  **中止路径上的 receipt 写失败不会顶掉原始错误**：已写入克隆行后中途失败、且 receipt
  路径又已存在时，原始克隆/镜像异常照常传播（进程仍非零退出），`FileExistsError` 作为
  exception note 附加在原始异常上（Python 3.11+ `add_note`），operator 同时看到两个事实
  ——克隆为什么停 + 它的声明凭证没写成——而旧 receipt 文件保持原样、绝不被覆盖。
- **先看 `invocation_outcome`**：`complete` = 每一对都跑到了记录结果；`aborted` = 中途
  停了，`failed_pair` 指名是哪一对、`failure_kind` 是 `pair_not_completed`（该对被拒
  或报错）还是 `mirror_write_failed`，`error` 带原文。`declared_pair_count` vs
  `cloned_pair_count` 给出写了几行。
  `pairs` 里是每个「跑到记录结果」的对各一条记录：
  - `failure_kind=pair_not_completed`（loop body 内任何异常：registry 行缺失、包根解析
    失败、非 direct-grid、跨 source、门拒绝、rewrite 报错）：失败那对**不在** `pairs`
    里——异常发生在 append 之前。这种 receipt 只有在更早的对已经写入时才会存在。
  - `failure_kind=mirror_write_failed`：失败那对**在** `pairs` 里，是**最后一条**，
    `state_index_outcomes.canonical.outcome=written` + `mirror.outcome=not_written` 带错
    误文本，**并且计入 `cloned_pair_count`**（canonical 行已经落地）。这种失败只可能在
    `--apply` 下出现（mirror 写入本身由 `args.apply` 把关）。
  - 因此：判断单对成败一律看 `failed_pair` / `failure_kind` 与 `state_index_outcomes`，
    **不要**用「在不在 `pairs` 里」推断。
- **spin-up 失真告知义务保留**：warm carry-over 的失真小于冷启动但不为零，receipt
  里的 `spin_up_distortion_announcement` 是这条义务的落点。
- 被拒绝时（`refusal_scope=state_compatibility_unequal`）工具非零退出，**索引状态取决
  于这对是第几对**：
  - 第一对（或唯一一对）被拒：两份索引都没有写入，也不产出 receipt——干净重来即可。
  - 前面已经有对写成功、后面某对被拒：**前面那些对的克隆行已经真实落在两份索引里**，
    工具照样写 receipt（`invocation_outcome=aborted`，`pairs` 列出已写入的对，
    `failed_pair` 记录被拒的对与原因）。这时**不要**当成"什么都没发生"：要么按 receipt
    修好被拒那对后补跑（`--pairs` 只写剩下的对，receipt 换新路径），要么显式决定让已写
    入的对生效。dry-run（不带 `--apply`）中途被拒则两份索引都没动，同样不产出 receipt。
  审计记录里带 `missing_category`/`missing_relative_path`/`missing_side` 时，说明某个
  hydrologic-core 文件只存在于一侧（新增或删除都算面不相等），按该路径定位后再决定是修
  包还是走冷启动 + approval。
- **mirror 写失败**（canonical 已写成功）：工具非零退出，receipt 里该对的
  `state_index_outcomes.canonical.outcome=written` 而
  `...mirror.outcome=not_written` 并带错误文本，顶层同时是
  `invocation_outcome=aborted` + `failed_pair.failure_kind=mirror_write_failed`。
  必须在 `t*` 之前修好 mirror；在
  `NHMS_REQUIRE_FORECAST_WARM_START=true` 下，未修好的后果是本轮停摆（fail-safe），
  不是算错。
- 调度**准入**侧零改动：克隆行带 `M1'` 的 `model_id` + `model_package_version` +
  `model_package_checksum`，`_validate_state_lineage` 按既有口径接收。
- **rollout 不再重开 backfill 窗口**（#1735，2026-08-22 事故的修复）：`M1'` 是新的
  content-derived `model_id`，`t*` 之前它没有任何 pipeline 历史。修复前每个
  completeness 判据都只按 `model_id` 匹配，于是 336h lookback 里的全部 cycle 从
  `complete` 翻成 `gap`，backfill 把自己钉在一个 `M1'` 永远关不掉的 cycle 上，前向
  lane 饿死。现在 scheduler 会读克隆行的 `cloned_from_model_id` / `valid_time`：
  **cycle_time < t\* 的 cycle 不把 `M1'` 计入完成度、也不为它建 candidate**（按
  `(model_id, source_id)` 各自解析，GFS/IFS 可以在不同时刻切换；边界严格，
  `cycle_time == t*` 照常打分、照常从克隆行 warm start）。
  - 值守判读：pass evidence 里出现 `type=lineage_scoped_out_pre_cutover` 的条目，
    带被排除的 `model_id`、`predecessor_model_id` 和 `cutover_valid_time` ——
    这是「因为还不存在而没被打分」，不是「所有模型都真的跑完了」。它只是注记，
    不参与任何判定。
  - 不需要清理旧数据：停摆期间写下的 `M1'` journal 行会因为出了 scope 而自然失效，
    没有迁移动作。
  - 一个**已接受**的取舍：`t*` 之前由已退休的 `M` 遗留的真实缺口，切换后不再显示为
    gap（`M` 已不在 active model set，`M1'` 被 scope 掉）。反正调度器两边都关不掉它，
    而钉死的 cycle 会饿死前向 lane；上面那条 evidence 注记就是它的可见性落点。

背景与被否决的替代方案见 [`docs/adr/0005-recalibration-state-carryover.md`](../adr/0005-recalibration-state-carryover.md)。

#### 5.7.1 整条 rollout 的顺序与 manifest 发布（2026-08-22 实战 receipt）

5.7 只讲克隆工具本身。一次完整的率定切换还要 provision 与 manifest 发布，**顺序是硬约束**：

```text
provision M1′（node-27，写 core.model_instance）
  -> 写克隆行（node-22，两份 state-index）
  -> 最后才发布合并 manifest
```

倒过来做的后果**比这段原文写的更重**（原文早于 #1164）：manifest 先落地时，`M1′`
在任何 generation 都没有 state 行，走的是 first-cycle 分支
（`services/orchestrator/scheduler_generation.py:1057`）；而生产 registry 行带
`manifest_uri`，会产出一个**合格**的 packaged-IC 信号，于是该 run 被
**放行**为 `PACKAGED_IC_BOOTSTRAP`（`scheduler_generation.py:1057-1078`），
**不是 block**。也就是说代价不是"白停一个 cycle"，而是发出一份从包内 IC 起步、
而非承接 warm state 的预报——生产水文过程线断一刀。
只有 packaged IC 不可读或不合格时才落到
`BLOCK_FIRST_CYCLE_INITIAL_STATE_UNDECIDED` 那条 fail-safe 上。
**所以顺序不是"省一个 cycle"的优化，是正确性约束**；稳妥做法是整段 rollout 期间
让 `nhms-compute-scheduler.timer` 处于 disabled，从结构上关掉这个放行窗口。

**`t*` 怎么选**：pass evidence 的 `cycle_window` 实测 `cycle_lag_hours=16`、
`end_time_utc = now − 16h`、cycle 步长 12h。所以 cycle `C` 进入调度窗口的时刻是 `C + 16h`，
这就是发布截止时间。克隆行的 `valid_time` 必须**等于** `C` 本身（不是 `C − lead`）——
判据见 `packages/common/state_manager.py` 里 expected-predecessor key 的注释。

**manifest 只能直接发布，没有闸可走。** 生产两个 env 都设了
`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`，此时 `scheduler_file_provider_refresh.py`
的 `publish_registry()` 走的是 `precommit_provider_generation(workspace, [], previous_models_snapshot)`
再重发 `previous_models_snapshot`——**prospective ≡ previous 的纯 renewal**。
于是 #1080 cutover 闸在这条拓扑上结构性看不到 model set 变更：`added`/`removed`/
`package_changed` 恒为 0，`declared_retirements` 恒为空。

> **不要为这种切换准备 retire declaration。** 它不会被任何东西匹配（refresh 侧闸看不到
> 变更；scheduler 侧 §8 因为克隆行制造了同代历史而走 `warm_continue`，该分支根本不读
> declaration）。而留在 env 里的 declaration 会过期，之后**每一次** refresh 都以
> `registry_cutover_declaration_invalid` 拒跑，把每日管线拖停。5.x 的
> 「retire declaration 恢复顺序」适用于非 direct-grid 拓扑，不适用于这里。

做法是直接调 `publish_scheduler_registry_manifest`，两份目标**共用同一个 `generated_at`**
（这样两份字节相同是结构性的，不依赖事后比对），canonical 侧带 `expected_preimage` 做 CAS：

```text
合并集合 = 当前 canonical 全量 − 旧 M1 行 + provision 输出的 M1′ 行
发布前校验：行数不变、无重复 model_id、全部 direct_grid、每流域行数不变
发布顺序：先备份两份 manifest -> canonical（CAS）-> scratch mirror
发布后再手动跑一趟 refresh：renewal 重建 canonical readiness 并留下
  outcome=published / refused=[] 的 receipt
```

2026-08-22 实测（Huai-MAIN + jialingjiang，各 gfs/IFS 两行，`t*`=2026-08-22T00:00:00Z）：
34 行进、34 行出；旧四行消失、新四行到位；两份 sha 相同；随后 renewal receipt
`added:0 removed:0 package_changed:0 refused:0`——`removed:0` 正是上面那段的实证
（确实删了四个 model_id，闸却看不到）。

**触发手动 refresh 的坑**：refresh 的 unit 带
`ExecCondition=... ! is-active nhms-compute-scheduler.service`，而 scheduler 每 5 分钟
跑一趟 oneshot。`systemctl --user start` 返回 0 **不代表跑了**（condition 不满足会静默
skip）。判据只有一个：`latest.json` 的 `started_at` 变新。**不要**用「receipt 文件数增加」
判成功——`latest.json` 是原地覆写的，计数不变，照此写循环会无限重试、反复触发 refresh。

**回补 forcing 时必须临时改指 registry，而不是提前发布 manifest。** forcing producer
是从 **file model registry** 解析目标模型的（`Model instance '<model_id>' was not found
in file model registry`），所以新 id 得先能被解析——这看着与"manifest 最后发"矛盾，
其实不矛盾：顺序约束针对的是**调度器读的那份活 registry**。做法是只对这一次调用
`export NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST=<workspace>/canonical-merged.json`，
并在这一步前后各记一次活 canonical 的 sha，证明它没被动过。
2026-08-24 hetianhe 实测：回补 `verified: 2`，活 canonical sha 前后逐字相同。

**marker 不是每次切换都需要，先跑 preview 再下结论。** #1816 那次 8 个流域是
`ARTIFACT_NOT_FOUND`（永久判死）且 id **留在** registry 里，所以要放行；hetianhe 是
`SHUD_EXIT_10`（NaN 本身）而它的 id 被本次切换**退休**了。preview 的输出直接点破：
唯一 `would_mark` 的是那个已退休的 gfs id，给它打 marker 等于去重启一个不在 registry
里的模型。新 id 没有任何 journal 历史，本来就不需要放行。

**5.1-5.7 每一步都要 detached 跑**（`{ setsid nohup ... & }`）。实测踩过：前台 producer
熬过了 ssh 会话，父进程死了子进程还在写，随后补起的第二次调用与它并发写同一个目标目录。
两个都按 PID 杀掉、目标目录核对干净才重跑的，但这个窗口是真的。

**旧行标 `superseded` 必须等 M1′ 的首个 run 发布之后。** display 候选 SQL
（`packages/common/forecast_store.py` 的 `_QHH_LATEST_CANDIDATE_RUNS_SQL`）是
`h.status IN ('succeeded','parsed','published')`，`superseded` **不在**白名单；
选择键是 `bv.basin_id`、无 model_id 谓词。提前标就是让该流域前端立刻空窗，
直到新 run 落地。等新行 published 之后再标，连续性由 `cycle_time DESC` 自然接上。

**node-27 侧不需要 staging Basins——但这只对已有 run 的流域成立。**
`node27_autopipeline.py` 的 seed 对**已有 run** 的流域用
`_basin_identity(object_store_root, first_run)` 取身份，run manifest 会 override
inventory 派生的身份，此时 `BASINS_ROOT` 里有没有该流域都无所谓。
实证：node-27 的 `/home/ghdc/nwm/Basins/` 当时只有 10 个原始流域、没有 Huai-MAIN 与
jialingjiang，而这两个流域各已有 73 条 published run。

> **不要把这条推广到新流域上线。** 零 run 流域**没有** run manifest 可用，
> 唯一的身份来源就是 `BASINS_ROOT` inventory（`node27_autopipeline.py` phase-1 的
> `_discover_seed_basin_identities`），所以新流域**必须**把 staged 树放进 NFS Basins。
> 2026-08-22 的 #1699 上线即走此路；见上文「新流域上线四跳」hop 1b/hop 2。
（另注意 node-22 的 `BASINS_ROOT=/volume/nwm/Basins` 在本地 175T 盘上，
与 NFS `/ghdc/data` 不是同一个文件系统。）

## 6. 如何判断是否卡住

先分清三种状态：

- 正常运行：node-22 Slurm 有 active job，或 node-27 autopipe 正在本轮 ingest；
  `/home/nwm/autopipe-logs/autopipe.log` 周期性刷新。
- 等下一 cron tick：Slurm queue 空，autopipe 最近一轮 `rc=0`，DB 中没有新的
  un-ingested runs。
- 真实卡住：autopipe 多轮非 0、同一 run 反复 failed，public `/health` 失败，
  或 node-22 Slurm terminal 后 shared object-store/published 不更新。

推荐检查顺序：

```bash
date '+%F %T %Z'

# node-27 ingest/display
ssh -p 32099 nwm@210.77.77.27 \
  'tail -n 120 /home/nwm/autopipe-logs/autopipe.log &&
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

### 6.1 No-progress circuit（跨 pass 重复同一理由的证据标记，#1118）

调度器每个**完整 pass** 会统计"同一主体连续报同一 no-progress 理由"的次数，
达阈值即在证据里开闸。**纯观测**：不改调度决策、不停重试、不新增终态。

阈值 `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`（默认 3；`<= 0` 完全禁用——
不写状态文件、evidence 无该键、零日志）。跨 pass 计数落在
`<evidence_root>/no-progress-tracker.json`（oneshot 每 tick 新进程，内存计数
活不过一个 tick）；该文件不以 `scheduler_` 开头，retention 归 `unrecognised`
永不删除。

pass evidence 顶层 `no_progress_circuit` 块：

```json
{
  "threshold": 3,
  "tracked": 4,
  "state_reset": "missing",
  "open": [
    {
      "subject_kind": "job",
      "subject_id": "job_cycle_gfs_2026071200_forecast_fixture_forecast",
      "reason": "query_unavailable:comment_accounting_unproven",
      "consecutive_passes": 3,
      "first_pass_id": "scheduler_2026081812_...",
      "last_pass_id": "scheduler_2026081814_..."
    },
    {
      "subject_kind": "candidate",
      "subject_id": "gfs:2026-07-12T00:00:00+00:00",
      "reason": "blocked:state_snapshot_index_prior_checkpoint_missing_after_history",
      "consecutive_passes": 3,
      "first_pass_id": "scheduler_2026081812_...",
      "last_pass_id": "scheduler_2026081814_...",
      "operator_action_required": true
    }
  ],
  "truncated": 0
}
```

- `tracked` = 本 pass 在跟踪的 (主体, 理由) 条目数；`open` 只列到阈值的，按
  次数降序**最多 50 条**，多出的计在 `truncated`。
- `state_reset` 只在状态文件缺失（`"missing"`，首次启用即如此）或损坏
  （`"corrupt"`）时出现，两者都只是从零重算，**不会让 pass 失败**。健康期
  每个完整 pass 都会重写状态文件，所以稳态下这个键不该再出现。
- `operator_action_required` 只在该候选行自己带 #1152 三态判据时随行出现。
- `state_write_failed: true` = **本 pass 的计数没落盘**（tracker 写盘失败），
  下一 pass 会从最后一次成功落盘的值接着数，本 pass 白数一轮。出现即查
  evidence_root 权限/挂载，以及是否有残留的 `no-progress-tracker.json.tmp`
  （非常规残留——符号链接、空目录——会被下一 pass 自动清掉；非空目录或异主
  文件要人工清）。同时会有一条
  `SCHEDULER_NO_PROGRESS_CIRCUIT_STATE_WRITE_FAILED` 日志。
- **超出 evidence 字节预算的 pass 上该块会被整块丢弃**（它是两道字节门里第一个
  被舍的项，以保证尺寸裁决、既有键裁剪与 pass 终态都与本功能不存在时逐字相同）。
  此时 journalctl 的聚合 WARNING 与状态文件里的计数都不受影响——按下面的 grep
  走，别以为"没这个块=没开闸"。

告警（journalctl 是当前唯一被实际消费的通道，每个开闸的完整 pass 一条聚合行）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'journalctl -u nhms-compute-scheduler.service --since "-24h" \
     | grep SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN'
```

**`consecutive_passes` 数的是完整观察 pass，不是 timer tick**：早退、prelock
阻塞、lock 争用、资源中止的 pass 既不计数也不清零（它们的候选列表本就是空的，
在那里观察等于把计数误清零）。所以墙钟跨度可能明显大于同数 tick——不要拿
`first_pass_id`/`last_pass_id` 的时间差除以 tick 间隔来反推。

同样的 gap 还有 **adapter 级**的一层：某个 pass 里适配器的源整个缺席（reconcile
段报错只写 `reserved_unbound_error`、dry-run），该适配器名下的条目**原样保留**
（不计数也不清除），`last_pass_id` 就此冻结。所以读条目时对一下产物自己的
`pass_id`：**`open` 条目的 `last_pass_id` 落后于本 pass 的 `pass_id`，说明这条
是陈旧观测**（当前 pass 根本没看到该主体，只是没被清除），别当成"这一轮又卡了
一次"。WARNING 行里的 `last=` 字段就是给这个对账用的。

`reason` 的三类来源与下游处置：

| reason 形状 | 来源 | 去哪儿处置 |
|---|---|---|
| `blocked:<candidate reason>`，且条目带 `operator_action_required: true` | #1152 predecessor-pending 三态判据 | [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md)（`self_heal_expected` / `backfill_predecessor_state` 一节） |
| `<action>:identity_mismatch_blocked` / 相关 identity 尾迹 | #1173 identity 阶梯（streak ≥ 3 自动放行为 `identity_mismatch_released`） | [`failed-basin-retry.md`](failed-basin-retry.md) § `identity_mismatch_released`；本文 §8.5 是同一条线的配置口径 |
| `query_unavailable:comment_accounting_unproven` | #1116：本集群不存 job comment，reserved 行**设计性永久**扣着 | [`failed-basin-retry.md`](failed-basin-retry.md) §"Reserved rows held by `comment_accounting_unproven`"——**必须人工处置**，无自动出口 |

开闸只说明"这个主体连续 N 个完整 pass 没动过"，不判定谁对谁错；先按上表定位
到下游 runbook，再决定动不动手。

## 7. 当前运行口径

This section is a live snapshot, not a permanent fact. Refresh it during handoff.

2026-06-22 verification found:

- node-27 `node27_autopipe` cron active every 10 minutes.
- Recent `/home/nwm/autopipe-logs/autopipe.log` runs discovered 300 runs, ingested 4 new runs,
  published 4, and refreshed 4 display coverage rows.
- node-27 display API listens on `127.0.0.1:8080`; local and public `/health`
  both returned `ok` after port alignment.
- node-22 Slurm Gateway process is active; node-22 diagnostic API `/health` on
  `:8001` returned `ok`.

### 7.1 2026-08-07：hhe 退出业务化（当时业务集 17 流域）

`basins_hhe`（全国级网格，43799 river segments）SHUD 参数待进一步校正
（单次 forecast 积分远超常规流域，见 #1295；gfs_2026072112 修复线在
forecast 运行超 2 小时后由操作员决定取消），暂时退出业务化。**退役当时**的生产业务集是 17 流域 × gfs/ifs 双源（34 行）——
这是 2026-08-07 的历史快照，不是当前值。当前业务集一律以
`jq '.models|length' manifest-last.json` 实测为准，口径见 §3.1 开头的 authority 段。

退役操作记录（均有备份，可逆）：

- node-22 scheduler registry：两份 `scheduler/registry/manifest-last.json`
  （NFS + `/scratch/frd_muziyao/nhms-prod` 本地）移除 hhe 的 2 条模型
  （`dg_edd58a2fe…`/gfs、`dg_2c13fd98…`/ifs，36→34），checksum 按
  canonical-JSON（排除 `checksum` 键、sort_keys 紧凑序列化的 sha256）
  重算；原件备份 `manifest-last.json.bak-hhe-retire-20260807`。
- node-27 DB：`hydro.hydro_run` 中 23 条 published 的
  `basins_hhe_vbasins` 行翻为 `superseded`（与 window-retirement 语义一致，
  ingest re-scan 会无条件跳过）；行级备份
  `/home/nwm/hhe-published-runs-backup-20260807.csv`。
- node-27 ingest：`infra/env/node27-ingest.env` 的
  `AUTOPIPE_EXCLUDE_BASINS=zhaochen_hhy,hhe`（备份同名 `.bak-hhe-retire-20260807`）。
- 展示验证：display API `/api/v1/basins?has_display_product=true` 返回
  17 个流域，不含 hhe。

复活路径：恢复 manifest 备份（或重新 provision registry）＋ 27 侧
`--force` 注册（其 register 步骤会把 `superseded` 翻回 active）＋
移除 `AUTOPIPE_EXCLUDE_BASINS` 中的 `hhe`＋把 baseline `core.model_instance`
行 `activate` 回来（见 §7.1.1，否则全国底图上没有这个流域的河网）。

#### 7.1.1 2026-08-25 补漏：baseline `core.model_instance` 必须一并 deactivate

上面 2026-08-07 的四步**漏了一步**，导致 hhe 退役 18 天后仍在公网底图上显示全部
43799 条河网：`core.model_instance` 的 baseline 行 `basins_hhe_shud` 一直是
`active_flag = t / lifecycle_state = active`（当时只处理了 registry 里的两条 `dg_*` 行）。
全国 river-network MVT 的成员判据就是这一条
（[`services/tiles/mvt.py:361`](../../services/tiles/mvt.py) 的
`EXISTS (... model_instance mi WHERE mi.river_network_version_id = rnv.river_network_version_id AND mi.active_flag = true)`），
且 `national_river_network_source_version`（同文件 `:1374`）的 digest 也只看 active 集合——
所以这条行不翻，tile 内容和 ETag 都不会变。

**退役流域时必须做的第 5 步**：把该流域的 baseline `basins_<slug>_shud` 行走
lifecycle 通道 deactivate。

- 通道：`POST /api/v1/models/{model_id}/lifecycle`，`operation=deactivate`，
  `override_missing_active=true`（退役后该 basin_version 为零 active，属合法终态，
  数据里已有先例），非空 `reason`，actor 角色需 `sys_admin`
  （[`packages/common/model_registry.py:2925`](../../packages/common/model_registry.py)
  的 `MISSING_ACTIVE_RISK` / `OVERRIDE_REQUIRES_SYS_ADMIN` 前置判据）。
- node-27 的 `:8080` 是 display-readonly 部署（`nhms_display_ro` +
  `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`），**不要**为了这一次操作放开写权限。
  用仓库自身代码在 27 上以 owner DSN 进程内调用同一个 store 方法即可——
  route 只是 `store.model_lifecycle_operation(...)` 的薄封装：

  ```python
  decision = trusted_internal_policy_decision(
      "models.deactivate", target_type="model_instance",
      target_id=MODEL_ID, actor_id="ops:<who>-<date>", roles=("sys_admin",))
  store.preflight_model_operation(MODEL_ID, operation="deactivate",
      policy_decision=decision, override_missing_active=True, reason=REASON)
  # blockers 非空一律中止，不要改用 trusted_internal=True 硬闯
  store.model_lifecycle_operation(...)   # 同参数
  ```

  跑之前 `env -u NHMS_AUTH_MODE -u AUTH_BACKEND`，只 source
  `infra/env/node27-ingest.env`（`display.env` 里的 `NHMS_AUTH_MODE=production`
  会把 CLI 证据路径判成 `release_blocked`）。deactivate 不做任何继任者提升，
  manifest / state-index post-commit publisher 生产上未挂载（默认 no-op），无调度侧副作用。
- 不要动 `core.basin_version`（其 `active_flag` 由 importer 恒置 false，无意义），
  也不要动已经 inactive 的 `dg_*` 行。一行一操作。

**验收 receipt（必须前后对照，只翻旗不算修好）**：

| 项 | 2026-08-25 实测 |
|---|---|
| `/api/v1/layers` river-network `source_generation` | `…:34c95f183d39f2504658:25` → `…:5cd1a080b67a0da5d6ca:24` |
| active `model_instance` / active river networks | 25 → 24，diff 只少 `basins_hhe_shud` 一行 |
| tile `river-network-national/5/25/12.pbf` | 65023 B → 10443 B |
| tile `…/6/50/24.pbf` | 14023 B → 467 B |
| tile `…/4/12/6.pbf` | 67700 B → 31865 B |
| 公网 `https://test.nwm.ac.cn` 同一 tile | 10443 B（与内网一致，nginx 无陈旧缓存） |
| `ops.audit_log` | `log_id=14`，`models.deactivate` / `sys_admin` / `basins_hhe_shud` |

tile 前后字节数不变 = 缓存问题，回去查 `source_version` 与 nginx，不要直接宣布完成。

**静态 geojson 的残留（2026-08-25 已清，#1701 一并处理）**：

两份 `apps/frontend/public/geo/*.geojson` 曾长期留着已退役流域的几何。它们确实
**不会被渲染**（`withStaticBasinBoundaries()` 只对**服务端已返回的** basin 按
basinId 查表回填，而 basinId 来自 `has_display_product=true`；river 那份前端
**刻意不 fetch**，见
[`useNationalBasinGeo.ts:37`](../../apps/frontend/src/pages/m11/useNationalBasinGeo.ts)
并有测试钉住），但把退役流域的几何继续投递给浏览器没有道理，已清：

| 文件 | before | after |
|---|---|---|
| `national-basin-river.geojson` | 45.0 MB / 59702 features（43799 为 hhe，5294 为 zhaochen） | **8.85 MB / 10609** |
| `national-basin-domain.geojson` | 0.50 MB / 18 features | **0.34 MB / 14** |

清理只过滤 `properties.basin_id`，不重建；退役新流域时照做一次即可。

**`core.basin` 的退役行不要删**：`core.basin_version` 以 `NO ACTION` 外键引用
`core.basin.basin_id`，每个退役流域各有 1 行 `basin_version`，硬删要连
`basin_version` → `model_instance` → `hydro_run` 一起删，会毁掉血缘和上面的复活路径。
裸 `/api/v1/basins`（不带 `has_display_product`）就是 `core.basin` 原始目录，
含已退役流域**属预期**；前端走的是 `has_display_product=true`
（[`stores/overviewData.ts:537`](../../apps/frontend/src/stores/overviewData.ts)）。

### 7.2 2026-08-25：zhaochen 系列退出业务化（#1701，owner 裁定不建后继）

`basins_zhaochen_{bst,mc,wem}` 三个流域整体退出生产。**owner 裁定「彻底退出，不建
basins_hys_* 后继」**，所以 #1701 原计划的「换 id + 状态延续」整条路作废——没有目标
包，就没有克隆对，`#1697` 的 `--transfer-mode recalibration` 不参与本次操作。

地理位置与名字无关，别按名字找：`bst` 在新疆天山（83.0–88.3°E / 41.5–43.3°N，
**9572 河段**），`mc` 在四川（103.8–104.0°E / 28.8–29.0°N，708 段），
`wem` 在 102.0°E / 34.1°N（308 段）。验收挑 tile 时按这三个 bbox 挑，不要按名字猜。

**本次五步全做了**（§7.1 的 hhe 退役漏了第 5 步，见 §7.1.1）：

1. **注册表两份**（NFS + `/scratch/frd_muziyao/nhms-prod` 本地）各移除 6 条
   `dg_*`（3 流域 × gfs/ifs），62 → 56。checksum **复用仓库自己的
   `scheduler_file_provider_refresh._prospective_registry_content`** 重算，
   不要手写 canonical 序列化；写入后立刻用 `_load_previous_canonical_registry`
   原地回读校验（sha 相符 + 56 行）才算成功。备份
   `manifest-last.json.bak-zhaochen-retire-20260825`。
2. **node-27 `hydro.hydro_run`**：552 条 published（184 × 3）翻 `superseded`，
   行级备份 `/home/nwm/zhaochen-published-runs-backup-20260825.csv`。
3. **`infra/env/node27-ingest.env`**：`AUTOPIPE_EXCLUDE_BASINS` 追加
   `zhaochen_bst,zhaochen_mc,zhaochen_wem`（保留原有 `zhaochen_hhy,hhe`），
   备份同名 `.bak-zhaochen-retire-20260825`。
4. **目录**：`zhaochen/` 两棵树各自 `mv` 到
   `<root>/Basins-retired/issue-1701-20260825/`（各 4.2 G，同盘 rename），不删。
5. **baseline `core.model_instance` deactivate ×3**：`basins_zhaochen_{bst,mc,wem}_shud`
   走 §7.1.1 的进程内 lifecycle 通道，`override_missing_active=true`，一行一操作，
   preflight `blockers` 非空一律中止（本次三个都是 `[]`，唯一 warning
   `COPIED_ROOT_EVIDENCE_MISSING` 与 deactivate 无关）。

**两个把这次退役做返工的坑（§7.1 / §7.1.1 的模板里没有，务必照做）**：

1. **按 `basin_version_id` 查 `model_instance`，绝不要按 `model_id`。**
   direct-grid 变体行的 `model_id` 是哈希（`dg_e8ced3a5…`），**不含流域名**，
   `where model_id like '%<slug>%'` 会把它们全部漏掉，只查到 3 行 baseline
   `_shud`。本次实际有 **9 行**（3 个 `_shud` + 6 个 `dg_*`，其中 3 个 `dg_*`
   是 active）。只关 `_shud` 会出现一个骗人的中间态：`source_generation` 确实
   从 `:31` 掉到 `:28`、tile 确实归零——因为那一刻 `dg_*` 恰好也没在 active 集里——
   然后下一轮 autopipe 一跑，河网全回来了。正确查法：

   ```sql
   select model_id, basin_version_id, active_flag, lifecycle_state
     from core.model_instance
    where basin_version_id like '%<slug>%'
    order by active_flag desc, model_id;
   ```

   §7.1.1 那句「不要动已经 inactive 的 `dg_*` 行」只在 hhe 的情形下成立
   （hhe 的 dg 行本就全 inactive）；**active 的 `dg_*` 行必须一起 deactivate**。

2. **先加 `AUTOPIPE_EXCLUDE_BASINS`，再 deactivate——顺序反了会被翻回来。**
   `nhms-node27-autopipe.timer` 每 10 分钟一轮，
   [`node27_autopipe_cron.sh:19/109`](../../scripts/node27_autopipe_cron.sh)
   每轮重新 source `infra/env/node27-ingest.env`，其 register 步骤会把
   `superseded` / `inactive` 翻回 active。本次 19:46:02 deactivate、19:48:47
   autopipe 就把 3 个 `dg_*` 重新激活了，而排除项 19:48:50 才落盘——**差 3 秒**。
   验收必须**跨至少一轮完整 autopipe** 再读数（本次 19:58:40 那轮跑完后
   zhaochen active = 0、active 总数 28，才算数）。

**不需要做的**（都核实过，别顺手做）：

- **不需要 retirement declaration**。`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 让刷新走
  [`scheduler_file_provider_refresh.py:896`](../../scripts/scheduler_file_provider_refresh.py)
  的 replay 分支，`previous_models_snapshot` 由
  `_load_previous_canonical_registry(registry_uri)` 直接读 manifest——手工删行之后
  previous 本身就是 56，分类器看到的是 `56/56 unchanged`，**根本不产生 `removed`**，
  `enforced` 门不会 refuse。`declared_retirements` 机制是给非 replay 路径用的。
- **不需要 geo 重建**。前端 geojson 刻意不 fetch（§7.1.1 末尾），成员判据在 DB 侧，
  正是第 5 步翻的那一行。
- **不需要维护窗口**。删行只停未来调度；已发布的包仍在 object store。动手前确认
  `squeue` 里没有这 6 个 `dg_*` 在飞即可（本次在跑的是 `dg_3264c89a`/gfs 与
  `dg_30a94855`/ifs，无交集）。

**验收 receipt（2026-08-25 实测）**：

| 项 | before | after |
|---|---|---|
| 注册表 `entry_count`（4 个 provider 全部） | 62 | **56** |
| 刷新分类 `refresh_20260825T114421Z_16c2c2f7df62` | — | `56/56 unchanged`，`removed 0`，`refused 0`，`package_changed 0` |
| `generation` | `manifest-c76de906099f` | `manifest-c5d926ff02b8` |
| active `core.model_instance` / active river networks | 31 / 31 | **28 / 28** |
| `/api/v1/layers` river-network `source_generation` | `…:a4559c13156eb0ea8b29:31` | `…:2f80d8c240118084e6fa:28` |
| `/api/v1/basins?has_display_product=true` | 24（含 3 个 zhaochen） | **21**（无 zhaochen） |
| tile `6/47/23`、`7/94/47`、`8/188/94`（bst） | 有内容 | **0 B** |
| tile `7/100/53`、`8/201/106`（mc） | 有内容 | **0 B** |
| 仍有内容的 tile 里 `zhaochen` 字符串出现次数 | — | **0**（`5/23/11`、`8/200/102`、`9/401/204`、`4/12/6`） |
| 公网 `test.nwm.ac.cn` 同 tile 字节 | — | 与内网一致（nginx 无陈旧缓存） |
| `ops.audit_log` | — | `log_id` 15/16/17，`models.deactivate` / `sys_admin` |

> **读 `source_generation` 有个坑**：deactivate 之后立刻读可能仍是旧值（显示 API 的
> 连接池会话快照）。不要据此判定「没生效」——先用 SQL 直接数 digest 行数
> （`JOIN core.model_instance ... WHERE mi.active_flag = true`），DB 是真值。
>
> **tile 字节不变不一定是缓存**。`cache_key` 含 `tile.source_version`
> （[`services/tiles/mvt.py:139`](../../services/tiles/mvt.py)），source_version 一变缓存键必变，
> 取到的就是新生成的 tile。字节不变要先确认挑的 tile **真的覆盖**该流域——
> 本次第一轮就是按名字猜到 98°E/34°N，五个 tile 全部不覆盖，白测一遍。

**复活路径**：恢复两份 manifest 备份（或重新 provision）＋ 目录从
`Basins-retired/issue-1701-20260825/` 移回＋ 27 侧 `--force` 注册（会把
`superseded` 翻回 active）＋ `AUTOPIPE_EXCLUDE_BASINS` 去掉三项＋ baseline
`core.model_instance` 三行 `activate` 回来（少这一步 = 底图上没有河网）。

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

- `/home/nwm/autopipe-logs/autopipe.log` shows repeated non-zero rc.
- JSON summary has non-empty `failed_runs`.
- New `object-store/runs/fcst_*` directories exist but DB `hydro.hydro_run`
  does not advance.

Checks:

```bash
ssh -p 32099 nwm@210.77.77.27
tail -n 240 /home/nwm/autopipe-logs/autopipe.log
cd /home/nwm/NWM
bash scripts/node27_autopipe_cron.sh
```

The wrapper uses the same env defaults, log path, and non-overlap lock as cron.
It is idempotent; rerun manually only after reading the previous failure and
confirming no cron run is active.

### 8.3 Forcing handoff parse failures

Symptoms:

```text
FORCING_DOMAIN_HANDOFF_UNAVAILABLE
checksum mismatch
mixed native_resolution labels for one valid_time
```

Impact:

- node-22 has completed run/output trees under the object store.
- node-27 autopipe skips or fails the affected run before DB ingest.
- `/api/v1/runs` is missing the basin/cycle even though SHUD output exists.

Boundary:

- Do not manually edit DB status to hide the issue.
- Repair the handoff payload/checksums or regenerate the forcing package, then
  rerun node-27 autopipe.
- Judge display readiness with parsed hydro output, layer publication logs, and
  node-27 API coverage.

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

### 8.5 Node-22 scheduler stuck after missing forcing artifact

Accepted-submit restart reconciliation is configured by
`NHMS_SCHEDULER_RECONCILE_ABSENCE_SECONDS` (production example: 300 seconds).
Values outside 30–3600 seconds fail closed at scheduler configuration time.
`NHMS_SCHEDULER_RECONCILE_SLURM_USER` and
`NHMS_SCHEDULER_RECONCILE_SLURM_ACCOUNT` must match the `sacct` owner of jobs
submitted by node-22; an owner, comment, master, task-prefix, stage, or cohort
identity mismatch remains reconciling and cannot project candidate state.
`NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` (default 3, `<= 0` disables)
bounds how many consecutive `identity_mismatch_blocked` passes such a
reserved-unbound row may accumulate before it is released to
`reservation_lost` / `identity_mismatch_released`. When disabled (`<= 0`) the
`identity_blocked_streak` counter freezes at its current value instead of
counting (`0` only for rows that never counted; a row that reached `2` under an
enabled exit keeps reporting `2`), so read no-progress off the repeated
`identity_mismatch_blocked` outcome rows for the same `job_id`, not off the
counter; see
[`failed-basin-retry.md`](failed-basin-retry.md) for the disposition of released
rows and of `blocked_strict_warm_start_init_state_mismatch` candidates.

Symptoms:

- `nhms-compute-scheduler.service` consumes CPU with no new Slurm job and no
  advancing file-journal evidence.
- Reconcile records `SLURM_RECONCILE_UNVERIFIED` for a Slurm job that `sacct`
  reports terminal.
- A previously completed cycle/basin is selected again because an older
  `hydro_run.status` row still says `created`.
- Forecast retry fails as a generic runtime/node failure while stderr shows a
  missing `forcing_package_uri` object-store tree.

Safe online mitigation:

1. Keep node-22 compute-only. Node-22 local PostgreSQL `:55433` is historical,
   archived, and stopped — do not connect it as a current runtime dependency.
2. The default missing-forcing policy is fail-closed. For a package that can be
   regenerated, use the exact-cycle wrapper described below; do not edit the
   file journal, submit forecast directly, or switch a warm candidate to cold.
   Restoring preserved forcing bytes remains valid only when the preserved
   package, checksum, source/cycle/model identity, staging object-store path,
   and shared NFS copyback root all match.
3. Clear only stale scheduler locks whose PID is dead or whose live pass was
   intentionally stopped; preserve the stale-lock evidence JSON.
4. Restart scheduler from the latest merged code, not by hand-editing journal
   rows as a normal operating path.

Exact-cycle missing-forcing regeneration (node-22 only):

1. Confirm that the affected candidates are blocked only by
   `missing_forcing_package_uri` / `FORCING_PACKAGE_URI_MISSING` (package
   determined absent) or `forcing_version_row_absent` /
   `FORCING_VERSION_ROW_ABSENT` (no provenance tier — journal row, journal
   direct file, or object-store forcing-version sidecar — could witness the
   package); the repair channel accepts both reason/classifier pairs, and the
   repair action is the same idempotent exact-cycle forcing rebuild.

   A `forcing_version_row_absent` blocker is only repairable by a rebuild when
   the tier that failed is a data tier. Route by
   `state_evidence.forcing_provenance.tier_status`:

   | `tier_status` | Fault | Does an exact-cycle forcing rebuild fix it? |
   |---|---|---|
   | `sidecar_absent` | No forcing-version sidecar for this cycle | Yes — rebuild writes the package, sidecar, and manifest |
   | `sidecar_malformed` | Sidecar unparseable or names no package | Yes — rebuild rewrites the sidecar |
   | `sidecar_unreadable` (permission/IO class) | Reading the sidecar *record* was denied or failed | No — fix the store permissions/mount first |
   | `sidecar_oversized` | The sidecar record exceeds the read limit | No — the record itself is anomalous; investigate the producer/lineage that wrote it first |
   | `sidecar_manifest_probe_error` | Object-store read fault on the manifest object (symlinked leaf, stale NFS handle, permissions) | No — a rebuild cannot clear a read fault; fix the object/mount first |
   | `store_unconfigured` | No `object_store_root` for this candidate | No — the rebuild could not even write; fix the config first |
   | `identity_incomplete` | Candidate has no `basin_version_id`/`model_id` | No — fix the registry/candidate identity first |

   For the five config/identity/read-fault statuses the rebuild will NOT clear
   the blocker; repeating it only burns a cycle. Correct the configuration,
   identity, or storage fault, let the next pass re-read the tiers, and only
   then repair if a data tier is still the fault.

   Also confirm that
   `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`; and that the current registry has
   18 source-scoped variants for each enabled source. The raw readiness record
   must say `status=ready`, `required=true`, and
   `source=node27_nfs_raw_manifest`. The scheduler re-reads the manifest and all
   referenced raw files from the trusted
   `NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT`; the redacted public journal value
   `[local-path]` is never used as an operational path. A stale journal `ready`
   value alone cannot authorize repair. Every admitted basin must also have a
   complete warm state identity (`id`, URI, checksum, valid time, and warm
   lineage); missing, partial, cutover/cold-new, or cold state remains blocked.
   Provision these values from the tracked
   `infra/env/compute.scheduler-dbfree.env.example` into the ignored live
   `infra/env/compute.scheduler-dbfree.env` (do not edit or commit the live
   file):

   ```bash
   NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
   NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true
   NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT=/ghdc/data/nwm/object-store
   NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX=s3://nhms
   ```

   Both variables are bindings to the fixed node-22 topology authority; neither
   variable defines that authority. Runtime preflight requires both roots to
   resolve to the canonical directory and to each other, so moving them together
   to an allow-listed staging directory still fails before lock acquisition or
   repair work. Public evidence records only redacted path placeholders and
   boolean identity results.
   `/ghdc/data/nwm/object-store` is node-22's view of node-27
   `/home/ghdc/nwm/object-store`; it must remain in
   `NHMS_SCHEDULER_ALLOWED_ROOTS`. It is not the compute-visible staging root
   under `/scratch`; an allow-listed staging path cannot replace either
   authority value.
2. Preview the exact UTC cycle. Omitting `--source` intentionally previews both
   configured GFS and IFS cohorts; omitting `--basin-id` retains all 18 active
   basins per source:

   ```bash
   cd /scratch/frd_muziyao/NWM
   scripts/ops/node22-run-cycle-once.sh \
     --cycle-time 2026-07-12T00:00:00Z \
     --repair-missing-forcing \
     --plan
   ```

3. Inspect the evidence artifact printed by the wrapper. Every admitted repair
   must contain:

   ```text
   state_evidence.missing_forcing_repair.status = authorized
   state_evidence.missing_forcing_repair.restart_stage = forcing
   state_evidence.missing_forcing_repair.slurm_stage = produce_forcing_array
   state_evidence.missing_forcing_repair.login_node_forcing = false
   state_evidence.cold_fallback_allowed = false
   ```

   Read `state_evidence.forcing_provenance` on the same record to see which
   provenance tier the blocker came from:

   ```text
   state_evidence.forcing_provenance.source = journal | direct |
                                              object_store_sidecar | absent
   state_evidence.forcing_provenance.tier_status = <sidecar tier detail, only
                                                    when source = absent>
   state_evidence.forcing_provenance.probe       = manifest | package_uri
   state_evidence.forcing_provenance.probe_key   = <object key that was
                                                    actually probed>
   state_evidence.forcing_provenance.artifact_exists = true | false
   state_evidence.artifact_guard.unsafe_reason   = <why the probe refused the
                                                    reference, or null>
   ```

   - `tier_status` names which provenance tier failed, and routes the repair
     decision through the table in step 1.
   - `probe` / `probe_key` name the object the existence probe was actually
     given. On the journal/direct tiers (the recorded `forcing_package_uri`):
     `probe = manifest` means the record held a package *prefix* (with or
     without a trailing `/`) and the probe used the derived witness manifest
     file key `<package prefix>/forcing_package.json`; `probe = package_uri`
     means the record was already a valid file key and was probed verbatim. On
     the `object_store_sidecar` tier `probe_key` is always derived from this
     candidate's own identity — compare it against `manifest_uri`, which is only
     what the record *claimed*: a mismatch means the record points somewhere
     other than this candidate's package.
   - `probe_key` is stamped *before* the probe runs. If
     `artifact_guard.unsafe_reason` is `object_store_root_unconfigured` or
     `artifact_probe_error`, that key was **not** probed at all —
     `unsafe_reason` is the authoritative verdict, not `probe_key`.
     Do **not** generalize that to "a non-null `unsafe_reason` means nothing was
     probed": `artifact_target_not_a_file` is the opposite case — the probe ran,
     reached `probe_key`, and *determined* that something other than a regular
     file stands there.
   - `artifact_exists` says whether that probed manifest object was found *as a
     regular file*. `false` on an `object_store_sidecar` source is still never a
     read failure — a read fault leaves that tier as `source = absent` with a
     read-fault `tier_status` instead — but since #1394 it covers two different
     determinations, so read it together with `unsafe_reason`: a genuinely absent
     package (`unsafe_reason` is `null`), or something other than a regular file
     standing on the derived witness key (`artifact_target_not_a_file`). Only the
     first one is a missing package.
   - `artifact_guard.unsafe_reason` says why the probe refused or could not use
     the reference; `null` usually means the reference was probeable and simply
     not found ("probed, determined absent"), but read it together with
     `forcing_provenance.tier_status` — on the `object_store_sidecar` tier a
     read fault surfaces as `forcing_version_row_absent` with a read-fault
     `tier_status` and a **null** `unsafe_reason`, and there the rebuild is
     ineffective. That null-reason shape covers the tier's *read faults* only:
     a non-regular witness target is a determination, so on that tier it surfaces
     as the ordinary `missing_forcing_package_uri` blocker carrying a **non-null**
     `artifact_target_not_a_file`, the same as on the journal and direct tiers.
     Route by this table:

   | `unsafe_reason` | Fault | Does an exact-cycle forcing rebuild fix it? |
   |---|---|---|
   | `null` | The probe ran and the package is genuinely absent — **unless** `forcing_provenance.tier_status` is a read-fault status (see below), or the recorded reference is malformed enough that no probe could resolve it | Usually yes — that is exactly what the rebuild repairs. A malformed unresolvable reference is also repairable (the rebuild re-records it). **But** if `tier_status` is a read-fault status, no: on the `object_store_sidecar` tier `tier_status`, not `unsafe_reason`, is the authoritative verdict |
   | `object_store_root_unconfigured` | Neither the candidate's `object_store_root` nor `OBJECT_STORE_ROOT` is set, so no probe ran | No — the remedy is configuration; fix it and let the next pass re-probe |
   | `artifact_probe_error` | The object store refused the stat (symlinked witness leaf or ancestor, stale NFS handle, permissions) | No — a rebuild cannot clear a filesystem fault; fix the object/mount/permissions first |
   | `artifact_target_not_a_file` | The probe **did** run and found something at `probe_key` that is not a regular file — almost always a directory squatting on a file key (a leftover placeholder, an interrupted writer, a rsync that created the path as a directory); a FIFO, socket or device node is judged the same way. Applies to the object leg and the local leg alike — but the two name their target differently, so inspect per leg | No — the rebuild would have to write a file where a directory stands (`IsADirectoryError`). Inspect it first. **Object leg**: `probe_key` is the *recorded reference* with the manifest filename appended — it is store-relative only when the recording was. The `object_store_sidecar` tier derives a bare key, so `ls -ld "$OBJECT_STORE_ROOT/$PROBE_KEY"` works there directly; the journal/direct tier stamps the producer's recorded uri verbatim, which under every tracked config carries an `s3://<bucket>[/<prefix path>]` head (`OBJECT_STORE_PREFIX`). Strip that head before joining — `ls -ld "$OBJECT_STORE_ROOT/${PROBE_KEY#"$OBJECT_STORE_PREFIX"/}"` — exactly as `normalize_object_key` does, and percent-decode as well if the recorded value carries `%XX`. **Local leg**: `probe_key` is already an absolute local path (or a `file://` uri) — `ls -ld` it directly with any `file://` stripped, and do **not** prefix the store root; also note this leg stamps `forcing_provenance` only for a journal/direct `forcing_version` that names the same uri, so it can be `null` entirely and then there is no `probe_key` at all. **Copyback leg**: never stamps a `probe_key` of its own — so check `artifact_guard.artifact_type` FIRST: when it is `copyback_source`, use `artifact_guard.artifact_uri` even if a `probe_key` is present, because that `probe_key` was stamped by the forcing leg earlier in the same pass and names the forcing manifest, not the squatted copyback path. Whenever `probe_key` is missing, the reference is `artifact_guard.artifact_uri`. Remove the placeholder once you are sure it holds no wanted data, then let the next pass re-probe or re-run the rebuild |
   | `invalid_local_artifact_path` / `local_artifact_path_outside_allowed_roots` / `local_artifact_path_unresolvable` | A local-path reference is unresolvable or outside the allowed roots | No — fix the path, or the roots this probe actually consults: resource-profile keys `object_store_root` / `object_store_copyback_root` / `copyback_root` / `published_artifact_root` plus env `OBJECT_STORE_ROOT` / `NHMS_OBJECT_STORE_COPYBACK_ROOT` / `NHMS_PUBLISHED_ARTIFACT_ROOT` (**not** `NHMS_SCHEDULER_ALLOWED_ROOTS`, which feeds a different mechanism and is never read here) |
   | `local_artifact_root_unresolvable` | **A configured artifact ROOT itself could not be canonicalized**, and no remaining resolvable root contains the artifact. Investigate the root, **not** the artifact's placement: a symlink loop in the root chain, a directory the scheduler user cannot traverse (EACCES), a stale NFS handle (ESTALE) or an unmounted/half-mounted share, a non-directory component (ENOTDIR). Not only symlink loops — **every** errno other than `ENOENT` lands on this reason; `ENOENT` alone (a root that simply does not exist yet — including forms like `<missing>/../<loop>` where a missing component is hit before the loop) stays admitted and never produces this reason. Same root list as the row above | No — a rebuild cannot clear a filesystem or mount fault. Fix or unmount/remount the offending root (`readlink -f "$ROOT"` and `ls -ld` on each component reproduce the kernel's verdict), then let the next pass re-probe |

   A blocker with a non-null `unsafe_reason` is rejected by the authorized
   repair channel as `forcing_artifact_reference_unsafe` (see the rejected
   reasons below). That is deliberate: a rebuild cannot cure a configuration or
   filesystem fault.

   `source = absent` with a config/identity/read-fault `tier_status`
   (`store_unconfigured`, `identity_incomplete`, a permission-class
   `sidecar_unreadable`, `sidecar_oversized`, or `sidecar_manifest_probe_error`)
   means the rebuild cannot clear the blocker — see the routing table in step 1.

   A rejected preview retains the original missing-forcing blocker and records
   a stable reason such as `raw_manifest_not_ready`,
   `raw_manifest_identity_mismatch`, `candidate_not_direct_grid`, or
   `exact_cycle_identity_mismatch`. Fix the stated precondition; do not bypass
   it.
4. Submit the same exact cycle only after the preview admits the intended set:

   ```bash
   cd /scratch/frd_muziyao/NWM
   scripts/ops/node22-run-cycle-once.sh \
     --cycle-time 2026-07-12T00:00:00Z \
     --repair-missing-forcing \
     --submit
   ```

   The process-scoped flag is cleared by the wrapper when omitted on later
   invocations, even if a stale env file contains it. The scheduler rejects the
   repair mode for continuous, backfill, multi-cycle, missing, or malformed
   exact-cycle use.
5. Acceptance requires one `produce_forcing_array` cohort per source with 18
   members (for the current registry) and Slurm array throttles whose concurrent
   total is at most 32. The subsequent forecast stage must retain each basin's
   selected `init_state_*` and lineage. Login-node `ForcingProducer` calls,
   forecast-only submission, cold fallback, a different cycle, or a raw
   identity mismatch are failures, not degraded success.

Business-readiness receipt after fix:

- `nhms-compute-scheduler.service` and timer run with
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, no `DATABASE_URL`, and
  `NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND=32` plus a Slurm resource
  profile whose `max_concurrent=32`. The receipt must show multi-task
  `produce_forcing_array` submissions whose simultaneous array throttles sum
  to at most 32. `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` only bounds
  source/cycle cohort control threads; it is not accepted as forcing-concurrency
  proof. GFS and IFS cohorts may overlap and synchronize at pass finalization.
- The cohort run id carries a stable digest of its candidate membership. A
  filtered drill and the full registry pass must not share the same array
  idempotency key; adding a basin must produce a new cohort identity.
- The emergency one-at-a-time override is removed or disabled.
- The receipt includes at least two eligible candidates or array tasks; a
  no-work pass proves safe daemon behavior but does not prove business
  operation.
- Slurm evidence binds terminal status to submitted manifest/task/stdout or
  file-journal identity. Generic job names such as `nhms_forecast` alone are not
  sufficient to mark success.
- Scheduler evidence shows duplicate-free file-journal progress and lock release
  after the pass.

#### 8.5.1 Withheld copyback source (`COPYBACK_SOURCE_WITHHELD`)

A candidate blocked with reason `copyback_source_withheld` / error code
`COPYBACK_SOURCE_WITHHELD` is **not** a missing-forcing blocker and none of the
triage tables above apply to it. The blocker means the copyback source reference
the scheduler resolved was a redaction placeholder (`[object-uri]`, `[uri]`,
`[local-path]`, `[redacted]`, `sha256:[redacted]`): the public-read redaction
boundary withheld the value, so existence could not be determined. Read it as
"cannot determine", not "source determined absent" — that is what distinguishes
it from `missing_copyback_source` / `COPYBACK_SOURCE_MISSING`, which does mean a
probe ran and found nothing.

- `artifact_guard.unsafe_reason` is `null` here because **no probe ran at all**.
  The §8.5 `unsafe_reason` table above is keyed on probe verdicts and does not
  cover this blocker; do not read its `null` row as "probed, determined absent".
- `artifact_guard.artifact_uri` is the placeholder itself, i.e. the evidence that
  the reference was withheld rather than absent.
- The exact-cycle forcing rebuild does **not** apply: it repairs forcing
  packages, and this blocker names a copyback reference. Running it burns a cycle
  and clears nothing. The blocker is also refused by the missing-forcing repair
  authorization channel by design.
- Whether a manual retry request helps depends on which arm the candidate rides,
  so check for a failure signal (failed pipeline status, failed hydro run, or a
  failed job row) before choosing:
  - **Failure-state candidate** (the common case, and the one this blocker's
    regression tests pin): a manual retry request **does** pre-empt this blocker
    and re-submits the candidate, exactly as it does for the missing-forcing
    blockers. Note it **bypasses, not clears**: the withheld reference is
    untouched, so if the resubmitted run fails again the blocker reappears on the
    next pass.
  - **Completed-stage resume candidate** (no failure signal at all; the state
    carries `completed_stage_evidence` with a `copyback` restart stage): that arm
    is evaluated *before* the manual-retry branch, so a manual retry request has
    no effect and the candidate stays blocked. There is no operator clearing path
    for this arm today — the DB-free public read re-redacts the reference on every
    pass. Defining one depends on a copyback write side that does not exist yet;
    it is tracked in issue #1464. Report the occurrence — the geometry is latent
    in production and a live instance is itself the signal.

### 8.6 Heihe 底图和 DB 范围混用

Current DB registered Heihe data uses `/home/ghdc/nwm/Basins/...` on node-27.
Older static basemap scripts may have used repository-local fixtures with a
smaller extent. For live display and ingest, use the node-27 Basins source of
truth.

### 8.7 Heihe 河段两层模型

Heihe DB river network has GIS display segments and SHUD output segments.
`hydro.river_timeseries.q_down` attaches directly to SHUD output segments.
GIS segments map through `properties_json->>'iRiv'`. If an API/frontend query
uses GIS segment ids directly, some segments can appear to have no flow.

### 8.8 state-index copyback fail-closed 与 replay 补账

判读入口（node-22）：`state_save_qc` 终态后的 copyback merge 是把新 checkpoint entry 写进
调度器读取的 shared canonical state index 的**唯一写者**。它失败时 journal 事件里带两个
`error_code` 之一——`OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`（纯 pre-commit fail-closed：
index 未被改动，按"未提交"幂等重跑）或
`OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`（#1193/#1364，index 可能已提交）——
两个 code 的 `details.details` 均含 `error_reason`（具体的 reason，provider_atomic 或
state-manager 的都可能），`details.details.error` 是异常文本：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
JOURNAL=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/journal
grep -rlE 'OBJECT_STORE_COPYBACK_STATE_INDEX_(FAILED|COMMIT_UNCERTAIN)' "$JOURNAL" | tail -20
# 两份 index 的 entry 数（shared 是调度器实际读的那份）
/scratch/frd_muziyao/NWM/.venv/bin/python -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["entries"]))' \
  /scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/index-last.json
/scratch/frd_muziyao/NWM/.venv/bin/python -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["entries"]))' \
  /ghdc/data/nwm/object-store/scheduler/state-index/index-last.json
```

判读口径：

- `OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`（事件里嵌套的
  `details.details.error_reason` 是 destination CAS 之后三族——释放不确定 / 替换不确定 /
  postcommit——的 reason 之一：
  `provider_lock_release_failed`（CAS 之后锁释放失败）/`provider_replace_uncertain`
  （替换已执行、持久化或身份确认失败）/`provider_postread_failed`（CAS 后回读失败且未
  回滚）/`provider_restored_previous`（回读失败但回滚已校验成功））—— **shared index
  可能已经提交**：这几族的 raise point 全部在 destination CAS 之后，锁范围内的写已经
  做完。**不得**按"未提交、直接重跑"处置：先核对 shared index 的 `entry_count` 是否已
  包含本批 entry、以及是否出现 lost 方向的收缩（对照 private index），确认没有丢失后再
  幂等重跑 stage 或走下面的 replay 补账；出现收缩就按下面 exit 3 的
  `destination_entries_lost_after_merge` 分支停手。`provider_restored_previous` 是其中
  唯一 destination 已被 provider 回滚为旧字节的形，`entry_count` 预期**不含**本批
  entry——核对确认后按幂等重跑处置，仍不得跳过核对（新 entry 曾短暂可见）。
- `state_snapshot_index_object_missing` / `..._object_checksum_mismatch`，且缺失对象在
  **private** `OBJECT_STORE_ROOT` 下 —— 真故障，source 侧全量校验按设计 fail-closed，先查
  `/scratch` 上的 state 对象是否被误删/截断，不得放宽校验。
- shared root（`/ghdc/data/nwm/object-store`）下历史 state 对象缺失 —— **不再**是 copyback
  失败原因（#1189 已收窄；已退役的 node-27 mover 曾按 14 天归档 shared 对象且不会归还，
  调度器与 refresh 都以 private root 解析对象）。若仍看到该失败，说明运行的是修复前的
  代码，先确认部署 SHA。
- provider-refresh 天天"成功"不能证明 copyback 正常：refresh 只续期、只用 private root
  解析对象。判"链是否在写入" 必须看 shared index 的 entry_count 是否随 cycle 增长。

失败后的补账（stage 终态已落账，copyback 不会自然重试；无 replay 则那批 entry 永不进 index）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22   # 必须是 provider 属主 frd_muziyao：
                                        # provider lock 要求锁父目录 st_uid == geteuid()，
                                        # CAS 替换要求 preimage uid 匹配；换身份会不透明 fail-closed
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env   # 提供 OBJECT_STORE_ROOT / OBJECT_STORE_PREFIX
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/copyback-replay/receipts
set +a
install -d -m 0700 "$NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT"

# 1) 只读预览（默认 dry-run：不调用 merge、不改 index、不拷对象）
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_copyback_replay \
  --cycle gfs_2026072000 --cycle ifs_2026072000

# 2) 逐项核对 dry-run 输出后再执行：
#    - resolved_run_ids / preview_new_entry_count 符合预期
#    - destination_entry_count_before 与共享 index 现有条数一致（当前 ~1645），而不是 0
#      （0 = 根写错 / NFS 没挂，别 enforce）
#    - destination_index_existed 为 true
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_copyback_replay \
  --cycle gfs_2026072000 --cycle ifs_2026072000 --enforce
```

约束与判读：

- cycle 用生产小写形式 `<source>_<YYYYMMDDHH>`（工具会对输入小写归一）；也可用
  `--run-ids a,b`（逗号分隔）。二者互斥且必选。
- 解析为空（cycle/run-id 在 private index 中无对应 entry）→ **非零退出 + 结构化 reason，
  不调用 merge、不写 index**。不要为了"跑通"改口径。
- `--enforce` 走的是生产同一个 merge 代码路径（同样的锁、CAS、checksum 与冲突语义），
  幂等：重复 enforce 零拷贝（copied/reused/replaced 全 0）、entry 数不变——与共享 index
  既有 entry 字节相同的胜出 entry 不重拷对象，避免把已归档对象复活回共享根。
- `--enforce` 的前置守卫：派生出的 destination index 文件不存在时 **非零退出**（reason
  `destination_index_missing`），在调用 merge 之前就拒绝，不写任何 index/对象。这挡的是
  "根写错 / NFS 未挂的桩 mountpoint" —— 否则 merge 会走 bootstrap 分支新建一份只含本次
  entry 的假 canonical index 并出绿 receipt。只有确认是真正的首次 copyback 才加
  `--allow-bootstrap` 放行 0-entry 开局。dry-run 不受该守卫限制（照常预览，before=0）。
- `--enforce` 的后置守卫：merge 提交后重读 destination index，断言其 entry 身份集
  **涵盖**前置守卫读到的全部 destination entry 身份。merge 只增不减，所以该断言零误报；
  比"after ≥ before"计数比较更严（等计数收缩也会被抓）。违反 → exit 3、reason
  `destination_entries_lost_after_merge`（见下面的退出码表）。
- receipt 落在 `NHMS_SCHEDULER_COPYBACK_REPLAY_RECEIPT_ROOT`（0700 目录、0600 文件，
  含 `latest.json`），字段含 mode、resolved run ids、前后 entry 数、copied/reused/replaced、
  `destination_index_existed`/`allow_bootstrap`、`merge_commit_state`（`committed` /
  `uncertain` / `dry_run`）与 `merge_error_reason`；enforce 模式未设该 env 直接拒绝执行。
- 工具只碰 state-index：不写 journal、不动 registry / canonical-readiness、不改 pipeline 行。

退出码判读（exit 2 与 exit 3 的区别是"index 有没有被改/可能被改"，别混着看）：

| exit | stderr `status` | 含义 | 处置 |
|---|---|---|---|
| 0 | —（stdout 是 receipt） | 成功 | 存档 receipt |
| 2 | `refused` | 只用于**可证明未提交**的拒绝（`destination_index_missing`、`cycles_absent_from_source_index`、`run_ids_empty`、`roots_identical`/`roots_overlap`、`receipt_root_*`、`index_*`，以及 `merge_failed`）。`merge_failed` **仅当** merge 抛出的 `error_reason` 属工具内的 pre-commit allowlist（`scripts/scheduler_state_index_copyback_replay.py` 的 `MERGE_PRE_COMMIT_REFUSAL_REASONS`：`provider_preimage_changed`/锁**获取**类（`provider_lock_unavailable`/`provider_lock_changed` 等，**不含**释放期的 `provider_lock_release_failed`——那是 exit 3）/校验类/`state_snapshot_index_copyback_conflict`/取锁**之前**的 lockfile 身份守卫两条（`state_snapshot_index_copyback_lock_identical`、`state_snapshot_index_copyback_lock_identity_unavailable`，#1609）等，raise 点均在 destination CAS 之前）——此时 shared index **未改**，但胜出 entry 的对象可能已拷到 shared root，幂等重跑安全。allowlist 之外的 reason 工具已归为 commit-uncertain，走 exit 3 `merge_commit_uncertain`；给 merge 加新 reason 时**必须同步**这份 allowlist，否则一个什么都没碰的前置拒绝会被报成「可能已提交」 | 按 reason 修根/修输入/修 receipt 目录后重跑 |
| 3 | `merge_committed_incomplete` | merge **已提交或不能证明未提交**，尾段没跑干净。stdout 一定带已知部分的 merge 摘要 | 先留存 stdout 摘要作 4.1 证据，再按下表分支 |

exit 3 的五个 reason（逐字与代码一致）。一次运行可能同时命中多个，stderr 的 `reason`
按严重度取最重的那个（`destination_entries_lost_after_merge` > `post_merge_readback_failed`
\> `merge_commit_uncertain` > `receipt_write_failed_after_merge`），其余折进 details 并在
`failure_reasons` 列全：

- `post_merge_readback_failed`：merge 已提交，但提交后重读 destination index 失败
  （NFS EIO/ESTALE、并发 preimage 变化、字节损坏）。receipt **照样写**，只是
  `destination_entry_count_after` 为 `null` 且 `post_merge_readback_reason` 记下原因。
  处置：手工数一遍 shared index 的 entry 数核对 stdout 摘要的
  `merge.published_entry_count`，再重跑 enforce（幂等）拿一份完整 receipt。
- `destination_entries_lost_after_merge`：提交后读回的 entry 身份集**没有涵盖**
  enforce 前置守卫读到的全部 destination entry —— 前置守卫在锁外读、merge 在锁内重读，
  这个窗口里 index 被带外删除 / NFS 掉挂，会让 merge 走 bootstrap 分支把 canonical index
  削成只含本次 entry（#1189 的 1645→36 灾难态）。**这是数据丢失告警，不是重跑就好**：
  立刻停手，别再 enforce，先确认 `/ghdc/data/nwm` 挂载状态，再从 private index
  （`OBJECT_STORE_ROOT` 下那份，永不剪枝）重建 shared index。receipt 里
  `destination_entries_lost_count` 是丢失的身份数，stderr 的 `lost_entry_identities`
  给前 20 个身份元组。
- `merge_commit_uncertain`：merge 自己抛错，但 `error_reason` **不在** pre-commit
  allowlist 上（如 `provider_replace_uncertain`：`os.replace` 已成功、父目录 fsync 失败；
  `provider_postread_failed`：CAS 写完后校验读失败；或任何未来新增的未知 reason）。
  **按"已提交"对待**：工具照样跑完提交后证据链（读回 + 超集守卫 + receipt），只是
  merge 返回值不存在，所以 receipt 的 `merge` 与 `checkpoint_*_count` 为 `null`，
  `merge_commit_state` 为 `uncertain`、`merge_error_reason` 记原始 reason。
  `provider_lock_release_failed`（CAS 之后 provider 锁释放失败：`flock(LOCK_UN)` 或
  `os.close` 在 NFS 上 EIO/ESTALE；锁范围内的写已经做完，receipt/stdout 的
  `merge_error_reason` 就记这个 reason）也是本 reason 的具名例子之一。
  merge 抛出的是**未分类异常**（不带 reason 的裸异常）时同样归到本 reason，此时
  `merge_error_reason` 是合成标识 `merge_unexpected_exception:<异常类型>`
  （如 `merge_unexpected_exception:OSError`），
  异常原文在 stderr 的 `error` 字段（receipt 只记 `merge_error_reason`，无 `error` 键）。处置：
  先看 stdout 摘要/receipt 的 `destination_entry_count_after` 与
  `destination_entries_lost_count` —— **`destination_entries_lost_count` 非 0 就转下面的
  lost 分支停手**；为 0 且 `destination_entry_count_after` 符合预期则幂等重跑 enforce
  拿一份干净 receipt。
- `receipt_write_failed_after_merge`：index 变更已提交但 receipt 写不下去，
  `receipt_failure_reason` 是底层原因。处置：**重跑前先看 stdout 摘要的
  `destination_entries_lost_count`——非 0 就按上面的 lost 分支停手，绝不重跑**（按严重度
  排序，lost 会直接顶掉本 reason，但 receipt 没写下来时 stdout 摘要是唯一现场证据）；
  为 0 才留存 stdout 摘要作证据、修好 receipt 目录后重跑 enforce（幂等）补上 receipt。
- `post_merge_unexpected_error`：兜底——merge 已提交、尾段抛了上面三类之外的异常
  （`error_type`/`error` 记下原文）。当 index 已变更处理：先核对 shared index entry 数，
  再重跑 enforce。

**exit 3 一律不是 refused**：看到 `status` 是 `merge_committed_incomplete` 就说明 shared
index 可能已被改，不能按"什么都没发生"处理。

本案处置记录（#1189，2026-07-20 00Z 链停摆）：node-27 product-archive mover 以
`cutoff=2026-07-06T00:00:00Z`、`minimum_age_days=14` 归档了 shared root 上 574 个旧 state
对象，而 shared index 无人剪枝；copyback merge 在写入前对 destination 全量 1645 条历史 entry
做对象存在性校验，于 2026-07-25T18:40:48Z 之后每次 `state_save_qc` 均 fail-closed，导致
2026072000 产出的 36 条 f012 后继 checkpoint（gfs 18 + IFS 18，`valid_time=2026-07-20T12:00Z`）
只存在于 private index，调度器读的 shared index 判 072000 为 gap、072012 永不规划。修复把
destination 侧收窄为"只校验并搬运本次胜出、且 shared index 尚未逐字节在册的 source entry"，积压用上面的 replay
（`--cycle gfs_2026072000 --cycle ifs_2026072000 --enforce`）补进 shared index。

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

### 9.1 `pg_stat_activity` 归因与 cancel 纪律（#1714）

2026-08-22 事故：`pg_stat_activity` 里一条 `state=active`、`dur=00:06:59` 的
`SELECT h.run_id, ...` 被判为「自己的 pytest 慢查询」并 `pg_cancel_backend`，
实际打掉的是每 10 分钟一次的生产 ingest tick（autopipe `rc=1`，本轮 ingest 未完成）。
当时全库应用连接的 `application_name` 都是空串，唯一的信号 `usename` 又被
autopipe/parser/retention 共用的 `nhms` 角色抹平。现在每个组件自带默认标识。

| `application_name` | 归属 |
|---|---|
| `nhms-autopipe` | `scripts/node27_autopipeline.py`（`nhms-node27-autopipe.timer`，每 10 分钟）|
| `nhms-ingest-run` | `scripts/node27_ingest_run.py`（autopipe 子进程）|
| `nhms-output-parser` | `workers/output_parser`（autopipe 子进程）|
| `nhms-refresh-coverage` | `scripts/node27_refresh_coverage.py`（autopipe 子进程）|
| `nhms-display-api` | `apps/api/routes/hydro_display.py`（display API 只读连接池）|
| `nhms-ts-retention` | `scripts/node27_timeseries_retention.py`（retention timer）|
| `nhms-ts-compression` | `scripts/node27_timeseries_compression.py`（compression timer）|
| `nhms-raw-retention` | `scripts/node27_raw_retention.py`（raw-retention timer；只做 watermark 只读查询）|
| `psql` | 人工会话 |
| `TimescaleDB Background Worker Scheduler` | TimescaleDB 后台 worker，不要动 |
| 空串 | 未在册的连接面（`packages/common/*` 中在册组件够不到的模块、`services/*`、qhh 系脚本等）；先查清来源再处置 |

在册组件**委托给共享 helper 打开的连接**同样带自己的名字：
`packages/common/display_watermark.py` 的 watermark 只读查询（retention /
compression / raw-retention 每个 tick 的第一条连接）与
`packages/common/display_coverage.py` 的 per-run coverage worker 连接
（`--all` 下最多 8 条并发，正是最容易被误 cancel 的长连接）。也就是说
**看到空串就一定不是在册生产 tick**，可以按上表照直处置。

处置纪律：

- **执行 `pg_cancel_backend` / `pg_terminate_backend` 之前，先用
  `application_name` 归因。** 归因不出来就别取消。

  ```sql
  select pid, usename, application_name, client_addr, state,
         now() - query_start as dur, left(query, 80)
  from pg_stat_activity
  where datname = 'nhms'
  order by query_start;
  ```

- **生产 tick（`nhms-autopipe` / `nhms-ingest-run` / `nhms-output-parser` /
  `nhms-refresh-coverage` / `nhms-ts-retention` / `nhms-ts-compression` /
  `nhms-raw-retention`）不得随手取消。**
  它们本来就有分钟级的正常时长。要停就停对应的 systemd unit（`systemctl --user stop
  nhms-node27-autopipe.timer` 等）并在日志里留痕；慢查询本身属于容量/计划问题，
  走 issue，不走 cancel。
- **integration 测试一律经 `NHMS_INTEGRATION_DATABASE_URL`**（`tests/conftest.py`
  无该 opt-in 时无条件 skip 全部 integration，并且每次建 `nhms_it_<uuid>` throwaway 库
  再 drop）。不要用裸生产 `DATABASE_URL` 跑 pytest —— 那正是把测试会话和生产 tick
  混在一张 `pg_stat_activity` 里的起点。
- 运维需要临时覆写标识时，在 DSN 上写 `?application_name=<name>`：代码给的是
  libpq `fallback_application_name`，显式值永远优先。

## 10. 前沿停摆告警（frontier stall alert）

2026-08-12 事故：scheduler pass 在 forcing 阶段被 NFS 锁挂死，unit 恒
`activating` 从未 failed，业务化静默停摆 ~11h 零告警。补的不是"某个 unit 的超时"，
而是"没有任何机制发现业务化不再产出"。`nhms-node27-frontier-alert.timer`
（每 30 分钟，`scripts/node27_frontier_stall_alert.py`）就是这个机制：它不看
Slurm、不看 unit 状态，只看**最终落库产物**——node-22 任何故障类都坍缩成同一个
可观测量：前沿不再推进。

### 10.1 判据（progress-based，不看墙上时钟）

每 tick 对 `hydro.hydro_run` 做一次只读聚合，限 `cycle_time IS NOT NULL` 且
`status IN ('succeeded','parsed','published')`，按 `COALESCE(source_id,'__null_source__')`
取三个 marker：

| marker | 含义 |
|---|---|
| `max(cycle_time)` | 前沿 |
| `count(DISTINCT cycle_time)` | 回填计数 |
| `max(created_at)` | 到达高水位 |

**进度 = 出现新 source，或某 source 的任一 marker 相对持久化基线严格增大**。
关键性质，看告警时别搞错：

- 判据**永不**比对墙上时钟。追欠账时前沿本就落后数天，wall-clock 判据恒误报。
- **减少不算进度**：行转出集合（`failed`/`cancelled`/`superseded`/`pending`）、
  人工 DBA 删除、source 整体消失，都不重置 stall 计时。
- **基线只升不降**（per-source 高水位）：marker 掉下去再回到原值不算"严格增"，
  否则 `succeeded → failed → parsed` 的出入集合会在真实停摆期伪造一次进度。
- 集合内跃迁（`succeeded → parsed → published`，同一行同一 cycle）**不动快照**，
  不是进度也不是异常。

### 10.2 三类告警邮件怎么读

| 邮件 | 触发 | 第一步做什么 |
|---|---|---|
| `frontier-stalled` | 连续 ≥ `NHMS_FRONTIER_STALL_HOURS`（默认 4h）无进度。首触发一封，持续未恢复每 `NHMS_FRONTIER_RESEND_HOURS`（默认 6h）重发一封 | 按 §6"如何判断是否卡住"走：先看 node-22 `squeue` + scheduler unit 是否恒 `activating`（本次事故几何），再看 node-27 autopipe 日志。邮件正文自带 per-source 快照与 `last_change_at`，可直接判断是"全线停"还是"某 source 早就没数据" |
| `frontier-recovered` | 告警期内出现方向性进度，闭环一封 | 只是闭环通知，不需要处置。正文列出触发恢复的 marker 变化，可用于确认真的是新产出而不是人为改库 |
| `monitoring-degraded` | 告警器**自身**降级，两个细类：`state-corrupt`（状态文件损坏/schema 不符/非 UTF-8 字节/深嵌套 JSON，已按当前观测重建基线）、`observability-unavailable`（观测查询连续失败 ≥ `NHMS_FRONTIER_QUERY_FAIL_TICKS`，默认 2 tick = 1h） | 这类邮件说明"你现在看不见前沿了"，与业务是否正常无关。`observability-unavailable` 先查 DB 可达性与只读角色口令；`state-corrupt` 检查 `NHMS_FRONTIER_STATE_PATH` 所在目录是否被别的东西写坏/磁盘满 |

degraded 邮件正文若以 `(retry: ...)` 开头，说明**上一 tick 就已经降级、只是邮件没发出去**
（sendmail 非零退出）：告警被落盘进 state 的 `degraded_pending`，本封是补发。
判读时间线**必须查 `frontier-alert-events.jsonl`**（追加式，每封告警一行）——
receipt 是 latest-only、每 tick 被原子覆写，出事那一 tick 的 receipt 早就没了。
以 JSONL 里的事件序列 + 邮件正文的 `Reason` 为准，别把补发当成新故障。
补发同样受 6h 去重钟约束；只要没发成功就一直挂在 `degraded_pending` 里，不会被丢。

fail-safe 方向是**宁可多报不可漏报**：告警器的任何内部故障只会**提高**告警倾向。
具体地——查询失败**不重置** stall 计时（瞎着也照样按点发 stalled）；状态损坏立即
发降级邮件而不是静默重置计时；sendmail 非零退出**不记**"已告警"，下一 tick 必然
重试；缺 `DATABASE_URL` 或 `NHMS_ALERT_EMAIL_TO` 直接结构化 config error 退出非零
（unit failed 可见），绝不带默认收件人静默跑。状态文件**缺失**是唯一的静默分支：
那是首次安装/换路径的 bootstrap，只记 `baseline_established_at`，代价是人为删档会
伪装成 bootstrap、盲窗 ≤ 一个 stall 窗口。

**持久性损坏**这类误配置有**两种截然不同的表现**，别把它们当成一回事：

| 现场 | 表现 | 怎么发现 |
|---|---|---|
| `NHMS_FRONTIER_STATE_PATH` 指到一个目录（锁能开、state 写不下去） | 去重钟随状态一起丢 → **每 tick 一封** `state-corrupt`（上限 48 封/日），同时每 tick exit 1、unit failed | 邮箱被刷屏；这是刻意选的过报方向，不是 bug |
| **锁文件本身打不开**（卷只读、root 用 sudo 跑过一次留下 root 属主的 `<state>.lock`） | 结构化 config error、**rc=2、零邮件** —— 告警器根本没跑到观测那步 | **只有 `systemctl --user status nhms-node27-frontier-alert.service` 的 failed 状态和 `frontier-alert.log` 能看见**，邮箱一片安静。定期看治理 receipt 里的 unit 状态就是为了这个 |

两种都先修路径/属主本身，别去调告警器参数。第二种只是"故障但零邮件"这一**族**的
**一个**成员：同族还有 wrapper 期的 env 文件是符号链接 / 权限宽于 0600 / 源失败，
以及缺 `DATABASE_URL`、`NHMS_ALERT_EMAIL_TO` 的 config error —— 这些都发生在任何
邮件通道之前，现场证据行是 bootstrap 日志 `/home/nwm/node27-frontier-alert.log` 里的
`BLOCKED rc=2 reason=<REASON>`（如 `ENV_FILE_SYMLINK_FORBIDDEN` /
`ENV_FILE_MODE_UNSAFE`），runner 期成员则写 `<log root>/frontier-alert.log`。

族里还有一个**通道之内**的成员：收件人/发件人取值让邮件正文编不出来（env 里混进
非 UTF-8 字节，进程内表现为代理对）。它每 tick 都走完判定、把发送记成
`returncode=70`（`SEND_INTERNAL_FAILURE_RC`）的失败并 exit 1、unit failed，但
**不会**有 `BLOCKED rc=2` 行；证据在 `frontier-alert-receipt.json` 的
`send_failures` / `emails[].error` 与 `frontier-alert-events.jsonl` 的 `sent:false`
（告警本身不算已送达，下一 tick 继续重试）。全族排查路径相同：unit failed ->
上述日志/receipt -> 治理 receipt 里的 unit 状态；几何不对称的进一步收口另立 issue。

### 10.3 阈值口径（改之前先读这段）

默认 4h 的来源：全趟实测 2h16m–3h01m，加落库滞后 15–30min，健康周期的现实上界
约 3h30m，4h 是留余量后的最小安全值。**别为了"少收几封"随手上调**——阈值就是
"业务停多久才被发现"的上限。真要调，先在 node-27 记录几天的真实 pass 时长再定，
并同步改 `infra/env/node27-frontier-alert.env` 与本节。resend 6h 同理：调大等于
延长"已知停摆但没人再被提醒"的窗口。

**阈值必须大于生产 cycle 节奏**，否则稳态本身就是"无进度"、天天误报：4h 只适配
回填期（一天多个 cycle）；稳态**每天只跑 1 个 cycle** 时，阈值要 ≥ 节奏的 ~2 倍
（漏掉两个日 cycle 才告警）。**生产现值：48h**（operator 裁定 2026-08-14，
`node27-frontier-alert.env` 已改；回填结束进入单日 cycle 稳态的口径）。resend 保持
6h——那是"真停摆持续期间的提醒频率"，与触发阈值是两个旋钮。

### 10.4 误报处置

先确认是不是**真**误报——"前沿 4h 没动"本身几乎总是事实，问题只在于是不是可接受：

1. 计划内停机 / 人工暂停业务化：告警属预期。停机前 `systemctl --user stop
   nhms-node27-frontier-alert.timer`，恢复后再 start；别改阈值。
2. 上游断供导致某个 source 长期不来：判据是**观测级**的（任一 source 推进=业务
   活着），单 source 断供不会触发本告警；若全部 source 同时断供，那不是误报。
3. 怀疑判据本身出问题：`--dry-run` 完整跑一遍判定并打印本应发生的动作，**零副作用**
   （不发邮件、不写 state/receipt/JSONL）：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a; . infra/env/node27-frontier-alert.env; set +a
.venv/bin/python scripts/node27_frontier_stall_alert.py --once --dry-run | python3 -m json.tool
```

4. 确认业务其实活着而告警仍在：读**当前** receipt 的 `baseline` 与 `observation`
   两块，对比哪个 marker 应该增而没增；要看**历史**（哪一 tick 开始不动、发过几封）
   一律读 `frontier-alert-events.jsonl`——receipt 只有最新一 tick，每 tick 覆写。
   `hydro.hydro_run` 侧用 §9 的值守 SQL 交叉验证。

### 10.5 状态、产物与恢复闭环语义

- 状态：`NHMS_FRONTIER_STATE_PATH`（原子 tmp+rename，含 `schema_version`、
  per-source 高水位基线、`last_change_at`、`alert_active`、`last_alert_at`、
  `baseline_pending`+`baseline_pending_kind`、`degraded_pending`、
  `last_degraded_alert_by_kind`）。单实例互斥是**脚本内** `fcntl.flock`
  （锁文件 `<state>.lock`），第二实例结构化 no-op 退出 0，不会重复观测或重复发信。
  锁目录不可建/不可写（EACCES/EROFS/ENOTDIR）走结构化 config error 退出 2，不是
  裸 traceback。
- **`baseline_pending`（基线待建）**：基线只能由**真实观测**建立。若 bootstrap 或
  损坏重建那一 tick 观测同时失败，脚本**不落空基线、不记** `baseline_established_at`
  / `baseline_reset_at`，只置 `baseline_pending=true`。下一次成功观测会**静默**填充
  基线——**不算进度、不动 `last_change_at`、不清 `alert_active`**。这条很关键：若
  当时把空基线当真，下一次成功观测会把所有 source 判成"新 source"=进度，把 stall
  时钟整整推后一个 DB 中断时长（漏报）。在 receipt 里看到 `baseline_pending: true`
  就意味着"现在没有可比对的基线"，优先修 DB 可达性。填充时按 `baseline_pending_kind`
  记到正确的戳位：损坏起源记 `baseline_reset_at`（如实说"这是一次降级重建"），
  bootstrap 起源记 `baseline_established_at`——损坏重建绝不冒充全新安装。
- 产物：`NHMS_FRONTIER_RECEIPT_PATH`（每 tick 原子覆写，latest 语义）+ 同目录
  `frontier-alert-events.jsonl`（追加式告警事件流）。两者都不是正式 schema
  产物，无跨工具消费方。
- 恢复闭环：`alerting` 状态下一旦出现方向性进度，发**恰一封** `frontier-recovered`
  并清 `alert_active`；此后重新计满一个完整 stall 窗口才可能再次触发，不会因为
  "刚恢复又慢了半小时"连环发信。恢复邮件发送失败不会把告警重新挂起（不存在的
  停摆不该被重新武装），失败事实记在 receipt 与 JSONL 里。
- **发送失败的重试语义**：stalled 邮件发失败 → 不记 `last_alert_at`，下 tick 必重试，
  且补发那封**仍标 `initial`**（它才是本轮真正投出去的第一封，operator 的时间线
  按投递事实重建）。degraded 邮件发失败 → 进 `degraded_pending`，下 tick 在 6h
  去重钟允许时补发，成功即清；补发正文带 `(retry: ...)` 前缀。recovered 邮件发失败
  是唯一不重试的一类（见上）。
- **degraded 去重钟是 per-kind 的**（`last_degraded_alert_by_kind`）：`state-corrupt`
  与 `observability-unavailable` 各有自己的 6h 窗。一封 `state-corrupt` **不会**
  压掉紧随其后的首封 `observability-unavailable`——那等于整类事件从未被告知
  （漏报方向）。同类 6h 内的重复仍然去重。

### 10.6 投递通道（认证 SMTP shim，不要用本机 sendmail）

告警器只会 `<NHMS_FRONTIER_SENDMAIL> -t -i`（正文走 stdin，exit 0 == 已发），
它自己不认识 SMTP。**node-27 上这个路径必须指向
`scripts/node27_frontier_smtp_sendmail.py`**（stdlib-only shim，直接以
`smtplib.SMTP_SSL` + 认证向 `smtp.163.com:465` 投递），凭据只从
`NHMS_SMTP_HOST/PORT/USER/PASS` 读，绝不走 argv，`NHMS_SMTP_PASS` 只喂
`login()`、不进任何输出。

**不要用默认的 `/usr/sbin/sendmail`**：node-27 的本机 postfix 是刻意断路的
（`default_transport = error`，只听 loopback）。它照单全收、exit 0，然后**异步**
把所有外发信退掉——2026-08-13 实测 `dsn=5.0.0 status=bounced`。即"exit 0 但从未
投出"，告警器会把它记成一封成功的 stalled 邮件。

shim 的退出码即投递事实：0 = 目的地提交服务器**同步**回了 250（stderr 留一行
`SMTP-ACCEPTED host=... code=250 recipients=<n>`）；69 = 投递失败
（`SMTP-FAILED stage=connect|ehlo|login|send ...`，其中会话预算到期那条带
`reason=session-budget elapsed=<s> budget=<s>`，或部分收件人被拒的
`SMTP-PARTIAL-REFUSAL ...`）；64 = 配置/用法错（缺 `NHMS_SMTP_USER` /
`NHMS_SMTP_PASS`、端口非法、`NHMS_SMTP_SESSION_BUDGET_SEC` 非正数或 ≥60s、正文无
收件人、From 与认证账号不一致）；70 = 内部故障
兜底（整类 contained，永不吐 traceback）。全部非零都会被告警器当作发送失败记进
receipt 与 JSONL，下一 tick 按 §10.5 的重试语义重来。TLS 是**验证过的**
`ssl.create_default_context()`——`SMTP_SSL` 的缺省 context 是 `CERT_NONE` + 不校验
主机名，那样授权码会递给任何应答方、250 也可被路径上伪造。

**三层时限，各管各的**（不是"嵌套超时"，三个值的量纲不同）：

1. **单次操作 30s**（`SMTP_TIMEOUT_SEC`，socket 级）：每次阻塞读写各自计时，**会被
   重置**。一次会话有 ≥8 次往返（connect/TLS/greeting/ehlo/login/MAIL/RCPT/DATA/
   收尾点），所以它**不是**会话上限；对方每 29s 挤一个字节就能把它无限续期。
2. **会话预算 45s**（`SESSION_BUDGET_SEC`，`setitimer(ITIMER_REAL)` 一次性闹钟）：
   从连接前起算的墙钟，到期就在**当前 stage 上**中断正在阻塞的那次调用，打
   `SMTP-FAILED stage=<stage> ... reason=session-budget` 并退 69，随后用
   `close()` 而不是 `quit()` 拆链路（不再多一次往返）。要改用
   `NHMS_SMTP_SESSION_BUDGET_SEC`（正数秒）——这是与下面那道 60s 墙**唯一的对齐
   点**。该 env **必须严格小于 60s**：`SESSION_BUDGET_CEILING_SEC = 60.0` 把
   ≥60 的值直接判成配置错（rc=64，连都不连），否则 SIGKILL 先到、stage 又丢，
   等于把改动前的几何悄悄装回来。默认值与这道天花板都有跨模块测试盯着
   （shim 不 import 告警器：它是被绝对路径 exec 的，依赖方向是 lane → shim，
   常数是镜像的，测试就是校准点）。
3. **告警器 60s 墙**（`SENDMAIL_TIMEOUT_SEC`，`subprocess` SIGKILL）：只是兜底，
   正常情况轮不到它——第 2 层先退。

**"没收到 250" ≠ "没投出去"**。RFC 5321 的收尾点一旦被服务器接收，投递责任就已
转移；我们只是没等到那个 250。所以下面**三种**记录都要按"**信可能已经投出去**"读：

- `rc=124` 且 receipt 里**没有** `SMTP-FAILED stage=` 行 —— **shim 自己挂死了**
  （不是投递失败，投递失败会带 stage），连自己那行都没来得及打；
- `rc=69` + `SMTP-FAILED stage=send ... reason=session-budget` —— 会话预算在
  DATA 中途开了闸；
- `rc=69` + `SMTP-FAILED stage=send ... error=TimeoutError`（或别的 socket 超时）
  —— 同一个窗口，只是这次是单次操作先超时。

其余 stage（`connect`/`ehlo`/`login`）的失败**确实**证明没投出去。三种"可能已投"
的情况下，下一 tick 都会按设计重发（§10.5 的重试语义），operator 可能因此收到一封
重复告警——这是**刻意选的**过报方向，不要按"重复发信"去查 bug。receipt 的 `error`
里若带 `| sendmail stderr: ...` 尾巴，那是 shim 被 kill 前来得及打印的内容，优先按
它定位。

163 会拒绝 From 头与认证账号不一致的信，而 shim **不改** From 头——所以
`NHMS_ALERT_EMAIL_FROM` 的**地址部分必须等于** `NHMS_SMTP_USER`（display-name
形式 `NHMS Frontier Alert <账号地址>` 允许且推荐，比对只看 `<>` 里的地址；见
`.example`）。不一致时 shim 在**连接之前**就退 64 并把两个地址都打印出来，不会每
tick 去换一个 550。

**成功也留证**：shim 那行 `SMTP-ACCEPTED ...` 会被告警器捕获进 receipt 与 JSONL 的
`emails[].evidence`（DSN 打码后）。receipt 里 `sent=true` 而 `evidence` 为 `null`，
说明发信通道不是本 shim（经典 sendmail 成功时不打印任何东西）——那正是 §10.7 那条
"exit 0 什么都不证明"的几何。

**正文含中文**（runbook 指引），所以 shim 会先 `EHLO`：服务器宣告 `8BITMIME` 就带
`BODY=8BITMIME` 发原始 UTF-8，否则把正文重编码成 quoted-printable，**绝不**裸推
8-bit（严格服务器会拒、宽松服务器会砍高位，把唯一一封告警变成乱码）。信头里的非
ASCII（例如 From 的中文 display name）统一按 `=?utf-8?...?=` 出线，不会出现无人能
渲染的 `=?unknown-8bit?...?=`。

**不要在 `NHMS_ALERT_EMAIL_TO` 里配多个收件人**：一封信一个信封，只要有一个地址被
拒，整次投递就算失败（exit 69）——已经收到的那位会在停摆期间每 30 min 收到一封重复
告警。要分发就在邮箱侧做别名/转发，别在这里堆地址。

### 10.7 信号口径（认领的盲区，不是 bug）

本 lane 观测的是 **post-ingest 前沿**（含 `succeeded`），**不是**严格的
display-published 前沿。即：ingest 仍在落库、但 parse/publish 段静默冻结的几何
**不会**触发本告警。这是刻意取舍——该故障类会让 autopipeline 非零退出、unit
failed，属于 systemd 已经能看见的面，与本 issue 针对的"挂死但从不 failed"不同类。
要观测 display 已发布前沿，另立 issue，别把判据混进本 lane。

同样不在本 lane 范围：`RuntimeMaxUSec` 之类 unit 超时兜底、钉钉/企业微信通道、
per-source 独立告警、以及任何自动恢复动作（本 lane 只通知不处置）。

**投递侧那条盲区已经不是理论问题，也已经收口**（2026-08-13 live receipt）：
告警器只认 sendmail 的退出码，而本机 postfix 的 exit 0 只代表"本机收下了"。实测
它随后异步 bounce（`dsn=5.0.0 status=bounced relay=none`，`default_transport = error`）
——**投递失败而告警器记成成功**，是本 lane 唯一漏报方向的几何。改用 §10.6 的认证
SMTP shim 后该盲区闭合：250 由目的方提交服务器**同步**返回才退 0，并留下
`SMTP-ACCEPTED` 证据行。**若把 `NHMS_FRONTIER_SENDMAIL` 改回 `/usr/sbin/sendmail`，
盲区原样复现。**

### 10.8 安装

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
cp infra/env/node27-frontier-alert.example infra/env/node27-frontier-alert.env
chmod 600 infra/env/node27-frontier-alert.env
# 填入只读角色 DSN（nhms_display_ro）、收件人（**只填一个**，多收件人的部分拒收会
# 让已收方每 30 min 重复收信，见 §10.6），以及 §10.6 的 NHMS_SMTP_USER /
# NHMS_SMTP_PASS（163 授权码，手工存入、绝不入库）；NHMS_ALERT_EMAIL_FROM 的地址
# 部分必须等于 NHMS_SMTP_USER（不一致 shim 退 64，零投递）。
# wrapper 在**任何**路径（systemd 或
# 手工）都拒绝符号链接 / 非 0600 的 env 文件——权限契约与"谁来 source"无关。
bash -n infra/env/node27-frontier-alert.env   # 填完先过一遍语法（见下 env 语法警示）
install -m 644 infra/systemd/nhms-node27-frontier-alert.service ~/.config/systemd/user/
install -m 644 infra/systemd/nhms-node27-frontier-alert.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nhms-node27-frontier-alert.timer
systemctl --user list-timers 'nhms-node27-frontier-alert.timer' --no-pager
tail -n 50 /home/nwm/node27-frontier-alert-logs/frontier-alert.log
```

首 tick 是 bootstrap：**预期零邮件**，receipt 记 `baseline_established_at`。要验证
真实投递，把 state 里的 `last_change_at` 人为回拨 5h，下一 tick 会真发一封
stalled；投递验证必须三重——shim exit 0 + receipt/JSONL 里 `emails[].evidence` 的
`SMTP-ACCEPTED ... code=250`（250 来自 smtp.163.com 提交服务器，同步；`evidence`
为 `null` 说明根本没走这个 shim）+ 收件箱人工确认，**不得只看 exit 0**
（用本机 sendmail 时 exit 0 什么都不证明，见 §10.7）。unit 已注册进 `scripts/node27_resource_governance.py`
`DEFAULT_SERVICES`，治理审计 receipt 里能看到它的 systemd 状态（timer 被人 disable
掉时，靠治理面发现，而不是靠"怎么没收到邮件"）。

**env 文件单读者 + 双语法警示**：systemd 路径下 env 由 service 的
`EnvironmentFile=` 读**一次**，同时 service 注入 lane 专属哨兵
`Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`；wrapper 只在该哨兵**缺席**
（手工 shell 调试）时才 `source`，不会二次解析。哨兵刻意不用 `DATABASE_URL`——那是
全仓最常被别的 lane 导出的变量名，用它当哨兵会让调试 shell 里带着他 lane DSN 的人
静默跳过 source、跑错库。注意：**符号链接 / 0600 校验不受哨兵约束，恒执行**。
但同一份文件仍可能被两种语法读到，所以：含空格
的值**必须加引号**（systemd 与 bash 都会剥掉外层双引号），密码里出现
`` $ ` " \ `` 时两个解析器**释义不同**（bash 在双引号内会展开/吞掉，systemd 不会）
——这种口令要么换成 `[A-Za-z0-9._~-]` 字符集，要么两条路径都实测一遍。改完
`.env` 先跑 `bash -n`，再 `systemctl --user restart`。

**tick 有执行期限**：service 用 `TimeoutStartSec=900`（不是同族的 `0`）。oneshot
的整个执行就是 start 阶段，所以这是唯一生效的期限；正常 tick 是秒级（connect 10s +
statement 30s + sendmail 60s 上限），900s 只兜挂死。**挂死的监控器必须变成 unit
failed**——否则就是 2026-08-12 那套"恒 activating、零告警"的几何在监控层重演。

## 11. 相关文档

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
