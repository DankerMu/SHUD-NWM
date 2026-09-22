**分册：服务拉起：下载 / 调度器 / ingest**

本页是当前生产值守手册的 §3 开篇、§3.1 开篇、§3.1.1 两节、§3.1.3 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

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
- **legacy run 覆写已由 guard 兜底（#1446）**：#1341 后 river coverage 扫描按代理
  键选行，legacy（pre-#1340、NULL 键）run 扫不到任何行。曾经裸 `--all` 或
  `--run-id <legacy run>` 会把已物化的 `run_display_coverage` 覆写成 0 / NULL
  边界，且**旧值无法就地恢复**（归零后的行本身不是永久损失，但**不会自愈**：归零的
  upsert 会把 `refreshed_at` 刷成 `now()`，该行随即是 fresh 的，cron 的
  `--all --skip-fresh` 永远不会再回头扫它。#1408 身份 backfill 落地后，恢复需要显式
  `--run-id <run>` 刷新——或一次省掉 `--skip-fresh` 的 `--all`——才会重新算出真实
  计数）；现在 upsert 的条件
  `DO UPDATE ... WHERE` 直接拒绝该写入——`--run-id` 退出码 **3** 并在 stderr 打一行
  `DISPLAY_COVERAGE_REFRESH_REFUSED run_id=… existing_segment_count=… advice=…`，
  `--all` 把它计入 JSON 报告的 `refused` 且仍退出 0（不打断批次）。
  代价：被拒的 run 保留旧 `refreshed_at`——**只在它本来就 stale
  （`refreshed_at < hydro_run.updated_at`）时**，cron 的 `--all --skip-fresh` 每个
  tick 才会重扫它一次（空扫描走代理键索引，很快）；被拒但 `refreshed_at` 仍 fresh
  的 run 不会被重扫。两条了结途径：等 #1408 身份 backfill 补上键后下一次刷新自然
  成功并自愈；或运维确认后显式 `--run-id <run> --force` 把该 run 归零（单个 run，
  推荐的人工方式）。`--force` 也可与 `--all` 组合，一条命令把批次里**每个**被拒的
  run 都归零——属运维显式 opt-in，cron 永不使用。`--skip-fresh` 不再是防覆写的必
  需项，但仍应保留——省掉它会让每个 tick 重扫所有已 fresh 的 run。详见
  `scripts/node27_refresh_coverage.py` 模块 docstring 的 "Overwrite guard" 段。

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
misconfiguration. `NHMS_SCHEDULER_JOURNAL_ROOT` 必须写成 realpath——每一段路径
组件都是真实目录、没有任何一段是 symlink（`readlink -f` 的结果与配置值逐字
相同）；否则调度器启动即以 `FILE_JOURNAL_INVALID_ROOT` 拒绝，详见 8.10。 Slurm submission is through the node-22 Slurm Gateway and
`sbatch`; Slurm then runs compute work on allocated compute nodes such as
`cnXX`.

node-22 compute-only chain must stop at
`NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc` with
`NHMS_REQUIRE_FORECAST_WARM_START=true`: this runs SHUD forecast and DB-free
`state_save_qc`, then skips parse/publish. Do not use `forecast` as the
production terminal stage, because it writes forecast outputs but stops before
publishing canonical warm-start checkpoints into the file state index. Node-27
remains the owner of parse/QC/ingest/display.

#### 3.1.1 Pipeline-job provenance sidecar and recovery (#2420)

Live acceptance on 2026-09-20 passed on reviewed source `87236ca54`:
GFS/IFS current-job reads, the complete readonly denial matrix, and formal
public C4 freeze/browser/bind/verify. See the
[deployment receipt](../receipts/2026-09-20-issue2420-job-provenance.json).
Strict success identities use canonical UTC `Z`, matching latest-product.
This receipt does not approve the separate #2121-A / #2346 joint deployment.

The DB-free node-22 terminal copyback hook publishes diagnostic provenance
*before* its existing `copyback_run_trees` call.  It reads only the
source-owned file journal at `NHMS_SCHEDULER_JOURNAL_ROOT`, validates each
selected run's immutable manifest, and writes
`runs/<run_id>/input/pipeline_jobs.json` into the object-store run tree.
It has no `DATABASE_URL` path and never starts, retries, cancels, or changes
Slurm work.  A failed provenance publication is recorded per run but does not
block the scientific run-tree copyback.

Only an exact, complete parent-array task entry may add a display log:
`identity_complete=true`, the requested task id, and the task entry's
run/model must all agree with the journal row.  The hook then writes the
actual returned bytes to the canonical
`published://logs/<source>/<cycle>/<run_id>/<job_id>.out` (and `.err` when
present) path before advertising that URI.  A missing, incomplete, ambiguous,
or mismatched task entry remains unadvertised; no parent-envelope or local
log is substituted.

`scripts/node27_autopipeline.py` performs the matching node-27 import phase
after the already-ingested skip.  It imports the selected sidecars into the
derived `ops.pipeline_job` read model using the node-27 ingest writer
credential, including runs that are already parsed or published.  It does not
rerun register/forcing/parse, update hydro readiness, or refresh coverage.
The autopipe JSON summary reports this as `job_provenance`; a missing sidecar
is an `unavailable` provenance result, not a scientific-ingest failure.

Configuration ownership is strict:

- node-22's `infra/env/compute.scheduler-dbfree.env` supplies
  `NHMS_SCHEDULER_JOURNAL_ROOT`, `OBJECT_STORE_ROOT`,
  `OBJECT_STORE_PREFIX`, `NHMS_OBJECT_STORE_COPYBACK_ROOT`, and, when log
  publication is expected, `NHMS_PUBLISHED_ARTIFACT_ROOT` plus the local
  `SLURM_GATEWAY_URL`;
- node-27's `infra/env/node27-ingest.env` supplies `DATABASE_URL`,
  `OBJECT_STORE_ROOT`, and `OBJECT_STORE_PREFIX`.  It must not receive the
  node-22 journal root, Slurm gateway, or any compute-control credential.

For a selected-run recovery, use the same code path in two separate commands;
the CLI intentionally refuses a mixed publish/import invocation.

```bash
# node-22: publish source-authoritative sidecar and any verified task logs.
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-dbfree.env
set +a
/scratch/frd_muziyao/NWM/.venv/bin/python scripts/backfill_pipeline_job_provenance.py \
  --publish-only \
  --run-id '<run_id>' \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --object-store-root "$OBJECT_STORE_ROOT" \
  --object-store-prefix "${OBJECT_STORE_PREFIX:-}" \
  --published-artifact-root "$NHMS_PUBLISHED_ARTIFACT_ROOT"

# After publication succeeds, copy only the sidecar to the shared object root.
# The publish-only CLI does not perform this transport itself.
/scratch/frd_muziyao/NWM/.venv/bin/python -c '
import os, sys
from services.orchestrator.run_tree_copyback import copyback_run_trees
print(copyback_run_trees(
    object_store_root=os.environ["OBJECT_STORE_ROOT"],
    copyback_root=os.environ["NHMS_OBJECT_STORE_COPYBACK_ROOT"],
    run_ids=[],
    extra_object_keys=[f"runs/{sys.argv[1]}/input/pipeline_jobs.json"],
    object_store_prefix=os.environ.get("OBJECT_STORE_PREFIX", "s3://nhms"),
))
' '<run_id>'

# node-27: project the already-published sidecar only.
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
uv run --no-sync python /home/nwm/NWM/scripts/backfill_pipeline_job_provenance.py \
  --import-only \
  --run-id '<run_id>' \
  --object-store-root "$OBJECT_STORE_ROOT" \
  --object-store-prefix "${OBJECT_STORE_PREFIX:-}"
```

Normal activation is the checked-in hook plus the existing node-22 scheduler
and node-27 autopipe timers; do not add a second provenance daemon.  Before a
code rollback, fence new passes by stopping those two timers and let any
already-started pass finish.  Restore the paired node-22/node-27 release and
their prior role-specific env files, then resume the existing timers.  Never
delete or rewrite the source journal, scientific run tree, sidecar, or
published logs as rollback.  `ops.pipeline_job` is a derived node-27 read
model: if a database rollback is needed, restore only the explicitly captured
rows with the ingest writer role and leave `hydro.hydro_run` readiness and
scientific products untouched.

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
- 历史坑（已修，保留以免误诊）：缺 `PYTHONPATH` 曾在 `_build_manual_cutover_gate` 处
  `ModuleNotFoundError: No module named 'scripts'`——门是默认开的，不是没跑。#1100 把该
  脚本拆成 facade 后补了按 `__file__` 定位仓库根的 `sys.path` bootstrap，直调与 `-m`
  两种形态都不再需要 `PYTHONPATH`。现在再看到同一条报错，**不要**当成缺 `PYTHONPATH`：
  那说明 bootstrap 被改坏或脚本被拷出仓库执行。
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

**依 #1846 裁决：回补是模型换代的必经步骤，调度器不会自动重入 forcing。** 上面的
`ARTIFACT_NOT_FOUND` 是 #1843 之前的现场。#1843 与 #1844 之后，在它们接入见证闸的发射点上，restart 在 `forecast` 的候选决策
发出前会先查该候选自己 `(source, cycle, basin_version_id, model_id)` 的 forcing 见证；查不到就停在
具名 `blocked`（reason `missing_forcing_package_uri` / `forcing_version_row_absent`），不提交作业。
无人值守车道不会为它重进 forcing 阶段，所以每次换代都得人工跑下面的回补；漏跑的代价是这批候选
一直停在看得见的 `blocked`，而不是静默烧掉 forecast。
这份代价还会**占住回补执行槽**（读码结论，未实跑）：被见证闸挡下的 cycle 在完成度评分里仍算 `gap`；
它没有已盖戳的 quarantine rerun，所以 quarantine breaker 永远不会把它放行出槽；于是它一直占着该 source
唯一的回补执行槽，其后的 gap 全部停在 `backfill_deferred_waiting_for_prior_cycle`
（`services/orchestrator/scheduler_discovery.py` 的回补选槽段），直到回补脚本把它清掉。
这与此前一个失败 rerun 占槽的行为相同，不是回归。这是已接受的成本，裁决与对 #1843 三条否决理由的
逐条回应见 [design D3](../../../openspec/changes/archive/2026-09-15-model-swap-forcing-witness-completion/design.md)。

回补脚本排空的 `blocked` 有两个来源，排空通道**按车道分**：

| 来源 | 车道 | 排空通道 |
|---|---|---|
| #1843 strict warm-start 见证 | strict warm-start 车道（候选带 strict warm-start 证据） | 下面的回补脚本**（仅当改名集非空，见下）**；或 8.5 的运维授权单 cycle 修复（`--repair-missing-forcing`）——**除非该候选带 operator 重入确认物**，那种候选会被修复策略拒绝，见下面的块注；改名集为空时两条都不是通道，按块注里的升级路径带外修输入 |
| #1844 journal 前驱身份 quarantine 见证（blocker 带 `state_evidence.journal_predecessor_identity`，且 `artifact_guard.planned_retry_reason = journal_predecessor_identity_mismatch`） | 非 strict 车道 | 下面的回补脚本**（仅当改名集非空）**，否则走块注里的升级路径。8.5 的单 cycle 修复在这条车道上**不会改判这类候选**——但原因不是"策略没被调用"：策略有**两个**调用点，`scheduler_candidates.py:674`（在 `:657` 的 `if strict_warm_start is not None:` 之内）和 `:1860`（在 `_candidate_warm_admission_decision`，def 在 `:1838`），后者**无条件调用、且早于 `:1867-1871` 的 `strict_warm_start is None` 判断**，所以非 strict 车道上策略确实被求值。挡住它的是策略自己的第二个 early return（`:1903-1908`）：这条车道递进去的 decision 是 `skip` / `terminal_hydro_success`，不在 `_MISSING_FORCING_BLOCKER_REASONS` 里，于是原样返回、不写任何证据。净结果对 operator 不变：`--plan` 里这类候选**不会出现** `missing_forcing_repair` / `missing_forcing_repair_status` 证据（由 `tests/test_production_scheduler.py::test_breaker_reentry_on_the_non_strict_lane_stays_blocked_with_the_repair_flag_on` 钉住，该测试用 spy 实测了"策略被调用、拿到的是 skip、没写证据"） |

> **`--repair-missing-forcing` 与 operator 重入确认物互斥（r2-01）**：这个候选如果是靠
> `confirm-operator-reentry` 的确认物才走到 missing-forcing `blocked` 的（strict warm-start 预算
> 或 §8.7 断路器两类 fail-stop），单 cycle 修复**拒绝**它而不是改判它，**不提交、不消费**，
> 那张人工签字保持待用。证据按车道分，结论一样、键不一样：
> - **strict warm-start 车道**（预算臂，以及碰巧落在该车道的断路器重入）：修复策略真的被调用并
>   拒绝，`--plan` 里看到
>   `state_evidence.missing_forcing_repair.status = rejected` +
>   `reason = operator_reentry_confirmation_present`（并回显 `confirmation.decision` /
>   `confirmation.request_id`），候选留在 missing-forcing `blocked` 上。
> - **非 strict 车道**（上面表格 `#1844` 那行）：修复策略**会被调用**（`:1860` 那个调用点无条件
>   执行、早于 `:1867-1871` 的 lane 判断），但它拿到的 decision 是 `skip` /
>   `terminal_hydro_success`，不是那两个 missing-forcing blocker 之一，于是在 `:1903-1908`
>   原样返回、**不写任何证据**。结果一样：这类候选**没有** `missing_forcing_repair` 键，只有
>   见证闸的 missing-forcing `blocked`。别去找那个键——但别把这理解成"策略没跑"。
> 理由：改判后的 `retry_repair_missing_forcing` 从 `forcing` 阶段重启，而重入 provenance 只在
> forecast cohort 的 reservation 处写——forcing 成功才顺带戳到，forcing 失败就是「真提交了、
> 计数没动、确认物还在」，并且失败的 forcing 在新 run-id 前缀下把 stage 域 attempt 打回 0/2、
> 让预算判定失效，随后候选还会自动重入一次 forecast（不带任何签字、不戳 provenance）。
> **正确处置：先把该模型自己的 forcing 补回来，再让确认过的重入自己跑**——它从
> `forecast` 重启、在 reservation 处被戳、计数 +1，恰好消费一次，之后的 pass 回到 blocked。
>
> **"补回来"分两种情况，别默认就是下面那个回补脚本。**
> `scripts/node22_backfill_forcing_for_model_ids.py` 是**纯改名工具**：它要求同时给出改名前后
> 两份 registry manifest（`scripts/node22_backfill_forcing_for_model_ids.py:605-606`），待办
> **完全**由改名集推导（`discover_work` 在 `:409`，`for rename in renames` 在 `:420-421`；
> `main` 的 `resolve_renames → probe_coverage → discover_work` 流水线在 `:648-651`）。
> - **改名集非空**（该模型的 forcing 确实以另一个 model id 存在）：用下面的回补脚本。
> - **改名集为空**（不是改名造成的缺失——例如 retention 在 fail-stop 生效之后删掉了
>   `forcing/<source>/<cycle>/...`，见 `services/orchestrator/retention.py:75` 与 `:684-687`）：
>   回补脚本**不是通道**，它会返回 `work_item_count: 0`（receipt 字段在 `:705` / `:715`），
>   而这个形状与 `--forcing-root` 指错 / NFS 没挂在条目数上**分不开**（本文 §3.1.1 就是为此要求
>   先读 `coverage`）。**升级路径：带外**重新产出该模型的 forcing 包并落进 object store
>   （或修好触发改写的那份 canonical readiness / raw manifest 身份），不要用
>   `--repair-missing-forcing` 绕。修好之前确认物保持待用、候选保持 blocked；修好后的下一趟
>   自然 pass 让确认过的重入从 `forecast` 重启、被戳、计数 +1，恰好消费一次。
>   （retention 的删除前沿是否钉住 blocked / 带确认物的候选**尚未实测**，两个方向都不要断言；
>   设计缺口记在 #2412。）
>
> 判据与撤销口径见
> [`node22-control-plane-manual-recovery.md`](../node22-control-plane-manual-recovery.md) 的「已知限制」。

strict 车道上的 quarantine retry 也可能带 `journal_predecessor_identity`，所以这个键只是提示，不是车道判据。
分不清时以 8.5 的 `--plan` 预览为准：`state_evidence.missing_forcing_repair.status = authorized` 的候选可以走
单 cycle 修复；其余候选不走这条通道，**仅当改名集非空时**改用回补脚本排空。改名集为空时回补脚本同样不是通道
（它返回 `work_item_count: 0`，与指错 `--forcing-root` / NFS 没挂分不开），此时走上面那段引文里的
**带外**升级路径：重新产出该模型的 forcing 包并落进 object store。

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
而 receipt 只在显式传了 `--output` 时才落盘（`scripts/publish_registry/publisher.py` 里
`if output_path is not None` 那支；#1100 把它从 `publish_scheduler_file_registry.py` 搬出来了），
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
只按“数据源 × 时次 × restart-compatible stage × 网关资源 profile 键”构造 cohort、
提交和轮询，不执行 direct-grid forcing。每个 cohort 的全部流域进入 `produce_forcing_array.sbatch`，Gateway
生成 `--array=0-(N-1)%min(cohort预算,resource profile上限,N)`；同一 pass 同时运行的
cohort 预算总和不超过 32。GFS 与 IFS cohort 可同时在 Slurm 中推进，scheduler
只在 pass 收尾时汇总全部 cohort。`NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` 仅限制少量
cohort 提交/轮询控制线程，不能作为流域计算并发口径，也不能替代 Slurm `%N`。
cohort run id 包含排序后 candidate membership 的稳定摘要；同一成员集合重启时复用，
定向过滤或新增/移除流域时生成新 idempotency key，禁止把子集数组误认成全量数组。

**大流域加核（#2543）**：Gateway 只按数组 task 0 的 model_id 解析
`config/resource_profiles.yaml`（`default` + `overrides[model_id]`），因此 scheduler 在
restart-compatible cohort 内再按 profile 键拆分：`overrides` 里有条目的 model_id 各自单独
成一个数组（自己就是 task 0），其余模型仍同在一个 `default` 数组。拆分只在 scheduler 环境
`SLURM_GATEWAY_BACKEND=slurm` 时生效（与 gateway 选后端的判据相同；mock 后端不读该文件，
cohort 不拆）。scheduler 与 gateway 读
同一份 yaml（`SLURM_GATEWAY_RESOURCE_PROFILES_PATH`，默认相对 `WorkingDirectory` 的
`config/resource_profiles.yaml`；node-22 两个 unit 的 WorkingDirectory 都是
`/scratch/frd_muziyao/NWM`）。文件缺失、不是合法 YAML 或缺 `default` 段时，scheduler 在
构造 cohort 阶段抛 `ConfigurationError`、整趟 pass 失败退出，零提交（fail closed）。要给某个大流域更多
核，需同时做两件事：

1. 在 `resource_profiles.overrides` 下为该流域**每个** direct-grid model_id（GFS/IFS 各一个）
   加条目，写 `cpus_per_task`、`memory_gb`、`shud_threads`（必要时加 `walltime`）；
2. 把 registry 中这些行的 `resource_profile.shud_threads` 设成同一个值。SHUD 线程数取自
   registry 行（`services/orchestrator/chain_manifests.py` 的 `threads`：`shud_threads`
   优先，其次 `cpus_per_task`），不读 yaml；只改 yaml 会分到更多核，但 SHUD 仍按旧线程数跑。

没有任何 override 成员时 cohort 拆分结果、membership 与 cohort run id 与旧行为逐字节一致；
新增 override 只会让该模型离开 default 数组，default 数组因成员变化得到新的 cohort run id。

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
