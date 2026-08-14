# Current Production Operations Runbook

最后更新：2026-08-07

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

#### 3.1.1 DB-free scheduler 的受支持回滚/前滚

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
uv run nhms-pipeline prepare-file-journal-rollback \
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
uv run nhms-pipeline launch-file-journal-rollback-writer \
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
uv run nhms-pipeline complete-file-journal-rollforward \
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
**shared root 上历史 state entry 的对象存在性**：node-27 product-archive mover 按 14 天策略
归档 shared root 的 state 对象，而没有任何组件剪枝 shared state index，因此 copyback merge
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
（previous 有、prospective 无）、`refused`、`declared_cutovers`。三个 refusal 原因均在
canonical replace 前退出、非零：

- `registry_cutover_undeclared`：某个已存在 `model_id` 的 `package_checksum` 变了但没有
  匹配的 cutover declaration。先看 `registry_classification.refused` 找到具体 model 与
  old/new checksum；确认漂移是有意后按下述格式提交 declaration，再重跑。
- `registry_cutover_removal_refused`：previous canonical 里的某个 `model_id` 在
  prospective 里消失。#1080 不允许 removal；需要下线一个流域走单独的 declared workflow，
  否则不要动 `NHMS_BASINS_ROOT` 里的对应目录。
- `registry_cutover_declaration_invalid`：declaration 文件本身或某条 entry 无效。常见
  原因：`NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` 指向的文件不存在 / 不可读（已被删除或
  轮转走）、schema 不匹配、`generation` 与 prospective 不一致、`old_checksum`/`new_checksum`
  与实际不符、`effective_cycle_utc` 未对齐 00:00 或 12:00 UTC、超出 24h 过期 / 168h
  未来窗口、entry 里有 duplicate `model_id`、declaration 文件是 symlink/非常规文件、
  超过 256 KiB。

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
model set 真正变了，generation 才会变（这时也必须重新出 declaration）。

操作流程（手动 CLI 路径）：先看被拒 receipt -> 拷 generation / old/new checksum 到
declaration -> 提交 declaration 到 mode-0600 路径 ->
`export NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<path>` -> 重跑 refresh。
`effective_cycle_utc` 必须精确对齐 00:00 或 12:00 UTC，且落在
`[now-24h, now+168h]` 区间；`transition_mode` 目前仅支持 `replace`。

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
常规运维必须走 declaration + 重跑，绝不 default 到 bypass。

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
运维含义：DB 增长会挤压归档层的 refuse 阈值，可能重现 mover ↔ retention 死锁。

容量核查必须**两块盘都看**：`df -h /home /data/GHDC`。治理 receipt 的口径是
partial 且**两个方向都失真**：`archive_root` 块**确实**报 `/dev/md0` 的
free/total 并带 warn/refuse 告警（需 `NHMS_ARCHIVE_FREE_SPACE_{WARN,REFUSE}_BYTES`
两个都设——两个都不设则 `band=unconfigured` 静默不告警；只设一个是
`ValueError`，整个治理 audit fail-closed、连 receipt 都不产出）；但 `pgdata_root` 只 `du`
`/home/nwm/nhms-pgdata`，DB 体量**少报**迁走的字节；而 `archive_root.used_bytes`
是整个归档根的 `du`，表空间就在根下面，归档体量**多报**了约 502 GB。单独量归档用
`du -s --exclude=nhms-tablespace /data/GHDC/nwm-archive`。issue #1290。

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

### 7.1 2026-08-07：hhe 退出业务化，当前业务集为 17 流域

`basins_hhe`（全国级网格，43799 river segments）SHUD 参数待进一步校正
（单次 forecast 积分远超常规流域，见 #1295；gfs_2026072112 修复线在
forecast 运行超 2 小时后由操作员决定取消），暂时退出业务化。当前生产
业务集为 **17 流域 × gfs/ifs 双源**。

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
移除 `AUTOPIPE_EXCLUDE_BASINS` 中的 `hhe`。

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
   state_evidence.forcing_provenance.probe_key   = <manifest object key that was
                                                    actually probed>
   state_evidence.forcing_provenance.artifact_exists = true | false
   state_evidence.artifact_guard.unsafe_reason   = <why the probe refused the
                                                    reference, or null>
   ```

   - `tier_status` names which provenance tier failed, and routes the repair
     decision through the table in step 1.
   - `probe_key` is the manifest object key the existence probe was actually
     given (derived from this candidate's own identity). Compare it against
     `manifest_uri`, which is only what the record *claimed*: a mismatch means
     the record points somewhere other than this candidate's package.
   - `artifact_exists` says whether that probed manifest object was found;
     `false` on an `object_store_sidecar` source is a genuinely absent package,
     not a read failure.
   - `artifact_guard.unsafe_reason` says why the probe refused or could not use
     the reference (unsafe path, invalid object key); `null` means the reference
     was probeable and simply not found.

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
`error_code` 之一——`OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`（pre-commit fail-closed，
另含尚未分流的 `replace_uncertain` 族，见下面的判读口径）或
`OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`（#1193，index 可能已提交）——
`details.details.error` 是具体的 reason（provider_atomic 或 state-manager 的都可能；
`..._COMMIT_UNCERTAIN` 另有 `details.details.error_reason`）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
JOURNAL=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/journal
grep -rlE 'OBJECT_STORE_COPYBACK_STATE_INDEX_(FAILED|COMMIT_UNCERTAIN)' "$JOURNAL" | tail -20
# 两份 index 的 entry 数（shared 是调度器实际读的那份）
uv run python -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["entries"]))' \
  /scratch/frd_muziyao/nhms-prod/object-store/scheduler/state-index/index-last.json
uv run python -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["entries"]))' \
  /ghdc/data/nwm/object-store/scheduler/state-index/index-last.json
```

判读口径：

- `OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`（事件里嵌套的
  `details.details.error_reason` 一般是 `provider_lock_release_failed`）—— **shared index
  可能已经提交**：CAS 之后 provider 锁释放才失败，锁范围内的写已经做完。**不得**按
  "未提交、直接重跑"处置：先核对 shared index 的 `entry_count` 是否已包含本批 entry、
  以及是否出现 lost 方向的收缩（对照 private index），确认没有丢失后再幂等重跑 stage
  或走下面的 replay 补账；出现收缩就按下面 exit 3 的
  `destination_entries_lost_after_merge` 分支停手。
- `OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED` 覆盖 pre-commit fail-closed，**以及尚未分流的
  `replace_uncertain` 族**（`provider_replace_uncertain`/`provider_postread_failed`：分流只认
  phase 为 `release_uncertain` 的 ProviderAtomicError，这两个被包成 StateManagerError 后仍落
  `..._FAILED`，此时 index 可能已提交；replay 侧同样按 commit-uncertain 处理）。看到
  `..._FAILED` 且 `details.details.error` 是这两个 reason 之一时，同样先核 shared index 的
  `entry_count` 再处置；其余 `..._FAILED` 才按"未提交"幂等重跑。
- `state_snapshot_index_object_missing` / `..._object_checksum_mismatch`，且缺失对象在
  **private** `OBJECT_STORE_ROOT` 下 —— 真故障，source 侧全量校验按设计 fail-closed，先查
  `/scratch` 上的 state 对象是否被误删/截断，不得放宽校验。
- shared root（`/ghdc/data/nwm/object-store`）下历史 state 对象缺失 —— **不再**是 copyback
  失败原因（#1189 已收窄；node-27 mover 按 14 天归档 shared 对象，调度器与 refresh 都以
  private root 解析对象）。若仍看到该失败，说明运行的是修复前的代码，先确认部署 SHA。
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
uv run python -m scripts.scheduler_state_index_copyback_replay \
  --cycle gfs_2026072000 --cycle ifs_2026072000

# 2) 逐项核对 dry-run 输出后再执行：
#    - resolved_run_ids / preview_new_entry_count 符合预期
#    - destination_entry_count_before 与共享 index 现有条数一致（当前 ~1645），而不是 0
#      （0 = 根写错 / NFS 没挂，别 enforce）
#    - destination_index_existed 为 true
uv run python -m scripts.scheduler_state_index_copyback_replay \
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
| 2 | `refused` | 只用于**可证明未提交**的拒绝（`destination_index_missing`、`cycles_absent_from_source_index`、`run_ids_empty`、`roots_identical`/`roots_overlap`、`receipt_root_*`、`index_*`，以及 `merge_failed`）。`merge_failed` **仅当** merge 抛出的 `error_reason` 属工具内的 pre-commit allowlist（`scripts/scheduler_state_index_copyback_replay.py` 的 `MERGE_PRE_COMMIT_REFUSAL_REASONS`：`provider_preimage_changed`/锁**获取**类（`provider_lock_unavailable`/`provider_lock_changed` 等，**不含**释放期的 `provider_lock_release_failed`——那是 exit 3）/校验类/`state_snapshot_index_copyback_conflict` 等，raise 点均在 destination CAS 之前）——此时 shared index **未改**，但胜出 entry 的对象可能已拷到 shared root，幂等重跑安全。allowlist 之外（含未来新增 reason）工具已归为 commit-uncertain，走 exit 3 `merge_commit_uncertain`，**不会**出现在 exit 2 里 | 按 reason 修根/修输入/修 receipt 目录后重跑 |
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

### 10.6 信号口径（认领的盲区，不是 bug）

本 lane 观测的是 **post-ingest 前沿**（含 `succeeded`），**不是**严格的
display-published 前沿。即：ingest 仍在落库、但 parse/publish 段静默冻结的几何
**不会**触发本告警。这是刻意取舍——该故障类会让 autopipeline 非零退出、unit
failed，属于 systemd 已经能看见的面，与本 issue 针对的"挂死但从不 failed"不同类。
要观测 display 已发布前沿，另立 issue，别把判据混进本 lane。

同样不在本 lane 范围：`RuntimeMaxUSec` 之类 unit 超时兜底、钉钉/企业微信通道、
per-source 独立告警、以及任何自动恢复动作（本 lane 只通知不处置）。

### 10.7 安装

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
cp infra/env/node27-frontier-alert.example infra/env/node27-frontier-alert.env
chmod 600 infra/env/node27-frontier-alert.env
# 填入只读角色 DSN（nhms_display_ro）与收件人。wrapper 在**任何**路径（systemd 或
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
stalled；投递验证必须三重——sendmail exit 0 + mail log 远端 250 + 收件箱人工确认，
不得只看 exit 0。unit 已注册进 `scripts/node27_resource_governance.py`
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
