# Current Production Operations Runbook

最后更新：2026-07-18

适用范围：node-27 active DB + ingest + display，node-22 Slurm/SHUD compute，
以及两者共享的 NFS object-store/published 数据面。

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
  入库并刷新 display coverage。
- node-27 display API 由 `scripts/ops/start-display-api.sh` 管理，
  当前监听 `127.0.0.1:8080`；公网入口是 `https://test.nwm.ac.cn`。
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
| node-27 display API | node-27 `127.0.0.1:8080` | display_readonly FastAPI, `/health`, `/api/v1/*`, frontend backend | `scripts/ops/start-display-api.sh` |
| node-27 public entry | `https://test.nwm.ac.cn` | nginx reverse proxy to local display API | `/etc/nginx/conf.d/test.nwm.ac.cn.conf` |
| node-22 compute | node-22 `/scratch/frd_muziyao/NWM` | Slurm Gateway、diagnostic API、DB-free scheduler、Slurm/SHUD compute wrapper | `nhms-compute-scheduler.timer`, `python -m services.slurm_gateway`, Slurm jobs |
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
tail -n 160 /home/nwm/autopipe.log
```

正常现象：

- 日志中每 10 分钟出现 `autopipe: start` 与 `autopipe: done rc=0`。
- JSON summary 包含 `object_store_root=/home/ghdc/nwm/object-store`、
  discovered/ingested/already_ingested runs、seeded/already_seeded basins。
- `coverage backstop (--all --skip-fresh)` 可刷新或跳过 display coverage；
  该步骤非 fatal，不应掩盖 autopipe 主返回码。

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
镜像到 node-22 私有 object store 后，先用 `FileSchedulerModelRegistry(...,
require_direct_grid=True)` 校验，再依次原子发布 Slurm worker mirror 与 shared canonical，
并重建 canonical readiness。任何一步失败都保留上一份 canonical，不允许退回 IDW。

**当前 authority（2026-07-18 node-22 现场）**：共有以下 18 个业务流域，口径为
12 个旧流域加 6 个新流域；每个流域有 GFS、IFS 两个 source-scoped direct-grid
model variant，所以 scheduler registry 是 36 行，不再是下面 baseline ID 的 18 行：

```text
basins_dth_ls_shud
basins_dth_zj_shud
basins_hhe_shud
basins_huai_main_shud
basins_lh_gl_shud
basins_heihe_shud
basins_hetianhe_shud
basins_jialingjiang_shud
basins_kashigeer_shud
basins_keliya_shud
basins_qhh_shud
basins_qinyijiang_shud
basins_weiganhe_shud
basins_xinanjiang_upstream_shud
basins_zhaochen_bst_shud
basins_zhaochen_mc_shud
basins_zhaochen_wem_shud
basins_tailanhe_shud
```

因此 GFS/IFS 各有 18 个 source-model candidate，共 36 个候选执行单元。
调度器在 candidate 构造前按 direct-grid contract 的 `applicable_source_ids` 投影模型；
不得把 36 个 variant 与两个 source 做 72 行笛卡尔积，也不得把预期的异源不适配记成
pass-blocking failure。合同缺失或损坏仍须 fail closed。
`NHMS_SCHEDULER_MODEL_IDS` 和 `NHMS_SCHEDULER_BASIN_IDS` 正常保持为空，由
file registry 决定全量自动计算；只在定向 rollback/drill 时临时收窄。
生产目标 `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND=32` 是全局流域/数据源执行
worker 上限。每个“流域 × 数据源 × 时次”独立推进，同一流域的 GFS/IFS forcing
允许同时计算；scheduler 只在该 pass 收尾时等待全部 execution unit 并汇总证据。
该值不是总提交数限制，也不保证同时出现 32 个 `RUNNING` job；Slurm 仍负责
资源仲裁，资源不足的任务会排队。

`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` 是生产硬门禁：publisher 不能用 legacy/IDW
行覆盖 canonical，consumer 读到任一非 direct-grid 行也会整体阻断。每日
`nhms-scheduler-file-provider-refresh.timer` 在该模式下重验并重发当前 direct-grid
authority、readiness 与 state index，不从 Basins 自动生成 IDW replacement；新增流域
必须先走上一段 direct-grid provisioning 流程。

2026-06-30 的 13 模型、2026-07-01 的 submit bound 13 仅是当时的历史现场
快照，不再代表当前 registry 或并发配置。
若只读 Basins 源中某个模型仅缺 `*.tsd.rl`，脚本会在私有 scratch copy
里复制同覆盖期 radiation 模板，原始 NFS Basins 源保持不变。

#### 3.1.1 DB-free file-provider 稳态刷新

Registry、canonical readiness 和 state index 的 consumer freshness 上限均为
168 小时；不得延长上限或只修改 `generated_at`。node-22 用独立 user-systemd
timer 每日从权威内容完整重验并重发三个 provider，scheduler consumer 仍然只读、
fail closed。direct-grid 生产模式下 timer 重发当前已验证 registry；
`publish_scheduler_file_registry.py` 只负责 baseline staging，不能直写 canonical。
它与 timer、model lifecycle、readiness/state writer 使用同一个
destination-derived lock 和 expected-preimage 检查，并发者不会覆盖较新的权威内容。
refresh user unit 不启用 `PrivateTmp`：node-22 的 user-systemd mount namespace 会在该模式下
拒绝进程打开 `/`，与 provider 的绝对路径逐级 no-follow 校验冲突；私有边界继续由 mode-0600
env、mode-0700 workspace/receipt/emergency/lock 目录、`UMask=0077` 和 DB selector 清除保证。
现场是 split-root：`OBJECT_STORE_ROOT` 必须保持
`/scratch/frd_muziyao/nhms-prod/object-store`，用于发布 registry package，并校验 scheduler
实际消费的 catalog/checkpoint 引用；`NHMS_SCHEDULER_PROVIDER_STORE_ROOT` 必须指向
`/ghdc/data/nwm/object-store`，且只承载 registry、canonical-readiness、state-index 三个
shared-NFS canonical provider。registry JSON 位于 shared root，但其中
`s3://nhms/models/...` 始终由 private `OBJECT_STORE_ROOT` 解析；不得依赖历史双份 package、
合并两根或关闭 object verification。

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
2026-07-18 当前 authority 为 18 个模型，因此每个 source 必须恰有 18 条、
总计 36 条。2026-07-15 在移除被 `HHe` 完整覆盖的重复目录
`HHe-MAIN-02` 后得到的 19 模型、每源 19 条、共 38 条，只是当日历史证据。
最新 catalog
invalid 时禁止回退旧 cycle；consumer identity mismatch 必须重读同一绑定 catalog 后重算。
State index 才允许仅绕过年龄并重验 checkpoint object。任何 missing/invalid 引用或
registry/readiness model-set mismatch 都在 canonical replace 前失败，绝不续签 legacy
readiness、复制巨大 products、生成空 index、DB fallback 或 timestamp-only 文件：

```bash
scripts/scheduler_file_provider_refresh_once.sh --dry-run
jq '{outcome,reason,database_free,providers,orphans}' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json

scripts/scheduler_file_provider_refresh_once.sh
jq '{outcome,reason,database_free,providers,orphans}' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json
```

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
（previous 有、prospective 无）、`refused`、`declared_cutovers`。三个 refusal 原因均在
canonical replace 前退出、非零：

- `registry_cutover_undeclared`：某个已存在 `model_id` 的 `package_checksum` 变了但没有
  匹配的 cutover declaration。先看 `registry_classification.refused` 找到具体 model 与
  old/new checksum；确认漂移是有意后按下述格式提交 declaration，再重跑。
- `registry_cutover_removal_refused`：previous canonical 里的某个 `model_id` 在
  prospective 里消失。#1080 不允许 removal；需要下线一个流域走单独的 declared workflow，
  否则不要动 `NHMS_BASINS_ROOT` 里的对应目录。
- `registry_cutover_declaration_invalid`：declaration 文件本身或某条 entry 无效。常见
  原因：schema 不匹配、`generation` 与 prospective 不一致、`old_checksum`/`new_checksum`
  与实际不符、`effective_cycle_utc` 未对齐 00:00 或 12:00 UTC、超出 24h 过期 / 168h
  未来窗口、entry 里有 duplicate `model_id`、declaration 文件是 symlink/非常规文件、
  超过 256 KiB。

Cutover declaration 是 `nhms.scheduler.registry_package_cutover.v1`（schema：
`schemas/scheduler_registry_package_cutover.schema.json`；参考 example：
`schemas/examples/scheduler_registry_package_cutover.example.json`）。文件路径通过
新增的 optional env `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` 传入 refresh 进程；
env 未设置或空值等同于"无 declaration"（只有当没有 `package_changed`/`removed`
时才允许）。示例：

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
model set 真正变了，generation 才会变（这时也必须重新出 declaration）。

操作流程：先看被拒 receipt -> 拷 generation / old/new checksum 到 declaration -> 提交
declaration 到 mode-0600 路径 -> `export NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<path>` ->
重跑 refresh。`effective_cycle_utc` 必须精确对齐 00:00 或 12:00 UTC，且落在
`[now-24h, now+168h]` 区间；`transition_mode` 目前仅支持 `replace`。

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
常规运维必须走 declaration + 重跑，绝不 default 到 bypass。

**`cutover_gate` audit（R2-A1，v2 summary）**：CLI 每次退出（成功 summary 到 stdout、
失败 error payload 到 stderr）都会写入一个 `cutover_gate` audit 块，schema 是
`nhms.scheduler.basins_file_registry_publish.v2`。三个字段：`mode ∈ {enforced,
bypassed_allow_uncovered_cutover, not_wired}`、`declaration_env`（enforced 时是
`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`，否则 null）、`declaration_present`
（bool，declaration file 是否可读的 regular file；符号链接和权限拒绝均计为 false）。
同一个 audit 块也会 mirror 到 manifest publication receipt 上（`publish_scheduler_
registry_manifest` 返回的 dict 里的 `cutover_gate` 字段），所以 downstream 直接读
`manifest-last.json` 的 companion receipt 也能看到同一份 audit。

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

启用 refresh timer 前必须 `jq '.registry_classification'
/scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json`
核对：`previous_registry_sha256` 等于 shared canonical 的实际 SHA-256、`new_registry_sha256`
等于本次刚 commit 的 canonical SHA-256、`refused.total == 0`、`declared_cutovers`
里的 entry 与 `entries` 数量与 declaration 完全一致。任何 `refused` 都禁止把 timer
enable；那说明当前 declaration 与 prospective 不匹配、需要重新提交。

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

前端全国总览的静态边界/河网也必须从同一个 Basins 真相源刷新；这是新增流域后
让公网地图立刻显示边界和基础河网的运维入口。脚本会在 `--basins-root` 下自动发现
所有 `**/input/*/gis/domain.shp` / `river.shp`，包括 `zhaochen/` 下的子流域，
不要再手工维护 qhh/heihe 列表：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
/home/nwm/.local/bin/uv run python scripts/geo/build_national_domain_geo.py \
  --basins-root /home/ghdc/nwm/Basins
/home/nwm/.local/bin/uv run python scripts/geo/build_national_river_geo.py \
  --basins-root /home/ghdc/nwm/Basins

jq -r '.features | length' apps/frontend/public/geo/national-basin-domain.geojson
jq -r '.features[].properties.basin_id' apps/frontend/public/geo/national-basin-river.geojson \
  | sort | uniq -c
```

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

3. 在 node-22 刷新 DB-free scheduler file registry，让新增流域进入自动计算：

   ```bash
   ssh -p 32099 frd_muziyao@210.77.77.22
   cd /scratch/frd_muziyao/NWM
   set -a
   . infra/env/compute.scheduler-dbfree.env
   set +a
   .venv/bin/python scripts/publish_scheduler_file_registry.py \
     --basins-root "$NHMS_BASINS_ROOT" \
     --registry-manifest "$NHMS_SCHEDULER_REGISTRY_MANIFEST" \
     --object-store-root "$OBJECT_STORE_ROOT" \
     --object-store-prefix "$OBJECT_STORE_PREFIX" \
     --work-dir "$WORKSPACE_ROOT/scheduler/basins-file-registry-publish" \
     --output "$WORKSPACE_ROOT/scheduler/basins-file-registry-publish/receipt.json"
   ```

   `NHMS_SCHEDULER_MODEL_IDS` 和 `NHMS_SCHEDULER_BASIN_IDS` 正常保持为空；
   不要为了新增流域在生产长期写死单个 basin。刷新后，`nhms-compute-scheduler.timer`
   的后续 tick 会按 00/12 UTC 业务 cycle 走 Slurm 计算。

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
2. Restore the missing forcing package from preserved workspace products only
   when the preserved files match the affected source/cycle/model identity.
   Copy both the staging object-store path and shared NFS copyback root, then
   verify file count and `forcing_package.json` checksum from node-22 and
   node-27 views.
3. Clear only stale scheduler locks whose PID is dead or whose live pass was
   intentionally stopped; preserve the stale-lock evidence JSON.
4. Restart scheduler from the latest merged code, not by hand-editing journal
   rows as a normal operating path.

Business-readiness receipt after fix:

- `nhms-compute-scheduler.service` and timer run with
  `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, no `DATABASE_URL`, and
  `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND=32`. The receipt treats 32 as the
  global basin/source execution-worker ceiling; GFS and IFS forcing share this
  pool and synchronize only at pass finalization. It does not require or imply
  32 simultaneous `RUNNING` jobs because Slurm remains the resource arbiter.
- The emergency one-at-a-time override is removed or disabled.
- The receipt includes at least two eligible candidates or array tasks; a
  no-work pass proves safe daemon behavior but does not prove business
  operation.
- Slurm evidence binds terminal status to submitted manifest/task/stdout or
  file-journal identity. Generic job names such as `nhms_forecast` alone are not
  sufficient to mark success.
- Scheduler evidence shows duplicate-free file-journal progress and lock release
  after the pass.

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
