**分册：率定 warm carry-over 与 file-journal 冷归档**

本页是当前生产值守手册的 §5.7、§5.8 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

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

背景与被否决的替代方案见 [`docs/adr/0005-recalibration-state-carryover.md`](../../adr/0005-recalibration-state-carryover.md)。

#### 5.7.1 整条 rollout 的顺序与 manifest 发布（2026-08-22 实战 receipt）

5.7 只讲克隆工具本身。一次完整的率定切换还要 provision 与 manifest 发布，**顺序是硬约束**：

```text
provision M1′（node-27，写 core.model_instance；工具 scripts/provision_direct_grid_scheduler_registry.py）
  -> 写克隆行（node-22，两份 state-index）
  -> 最后才发布合并 manifest
```

direct-grid 生产上 model set 变更的通道就是这三步：`M1′` 行由
`scripts/provision_direct_grid_scheduler_registry.py` 产出，再由下面的直接发布落到
manifest——**不是** §3.1.2 的 cutover declaration。

**provision 这一步先 dry-run 再 `--apply`**（#2737）。不带 `--apply` 时脚本什么都不写
（不写库、不在 object store 上建包或 chmod、不写 `--output-registry`），只预测每个变体的
`model_id` 并落一份回执；三步共用同一个 `--succession-id`。`--output-registry` 必须写在**两节点都读得到**
的地方——发布那一步在 node-22 上读它：放共享 NFS（node-27 写 `/home/ghdc/nwm/...`，node-22 看到的是
同一挂载的 `/ghdc/data/nwm/...`），且父目录须属执行账号本人、不能组/他人可写（见 [`service-bringup.md`](service-bringup.md) hop 3 的坑）。
这一条 dry-run 与 `--apply` 都在最开头检查（#2753）：不合格就拒绝，不连库、不落回执，报错里点名目录、属主 uid 与 mode。
自己的目录 `chmod 755` 后原命令重跑；别人的目录（例如组可写的共享候选目录）改不了，就把 `--output-registry` 换到自建目录
（`mkdir -m 755`）；`--apply` 必须与它的 dry-run 写同一路径，所以只有当本 id 已有一份写着旧路径的 dry-run 回执时，才需要换一个新的
`--succession-id` 重做 dry-run（被拒绝的 dry-run 没落回执，原 id 换个路径照用）。父目录尚不存在时只要求最近的已存在祖先对本人可写，
apply 会自己按 0755 建出来；已存在的父目录还须对本人可写。`s3://` / `published://` 形式的 URI 不做这项目录检查，
其他 scheme（含首段带冒号的相对路径）发布器不支持，同样在开头就拒绝，改写成普通路径即可。

provision 的 dry-run 与 `--apply` 命令如下：

```bash
# node-27，/home/nwm/NWM；DATABASE_URL / OBJECT_STORE_ROOT / OBJECT_STORE_PREFIX 已在环境里
mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
SUCCESSION_ID=<succession-id>          # [A-Za-z0-9._-]{1,80}，一次率定切换一个
PROVISION_ARGS=(
  --baseline-registry "<含新率定 baseline 行的 registry>"
  # 写到两节点都读得到的共享 NFS 上（node-27 的 /home/ghdc/nwm/... 即 node-22 的 /ghdc/data/nwm/...），绝不指向生产 canonical manifest
  --output-registry "<共享 NFS 上的本次 workspace>/direct-grid-registry.json"
  --operator-id "<operator>"
  --model-id "<baseline_model_id>"
  --succession-id "$SUCCESSION_ID"
)
PYTHONPATH=/home/nwm/NWM uv run python scripts/provision_direct_grid_scheduler_registry.py "${PROVISION_ARGS[@]}"
cat "$OBJECT_STORE_ROOT/scheduler/succession/$SUCCESSION_ID/provision-dry-run.json"
PYTHONPATH=/home/nwm/NWM uv run python scripts/provision_direct_grid_scheduler_registry.py "${PROVISION_ARGS[@]}" --apply
```

读回执时核对 `models[]`：新率定的 `M1′` 应是 `inserted=true` 且 `model_id` 与旧 `M1` 不同
（包校验和变了，身份随之变）；`inserted=false` 说明这个身份已经登记过，但 apply 仍会更新该行的
`model_package_uri` / `resource_profile` 并逐站 upsert `met.met_station`。`--apply` 只认同 id 的
dry-run 回执，输入或预测集合有任何出入就拒绝并回滚；回执 `provision-dry-run.json` /
`provision-apply.json` 落在 `<OBJECT_STORE_ROOT>/scheduler/succession/<succession-id>/`
（两节点共享的 NFS，从不覆盖）。回执只记录 baseline -> 变体，不记录前驱 -> 后继；
克隆与发布所需的前驱 `M1` 仍按 `(basin_version_id, source_id)` 从 canonical manifest 取。
各选项的细节与回执根的一次性放权见
[`service-bringup.md`](service-bringup.md) 3.1.1 的 hop 3。

**包内容没变、但要一个新 generation 时**（例如只为换一代 `model_id` 而重新发布同一棵流域树）：baseline 包的版本号由内容派生，
原样再跑 hop 1 的结果是 `already_done`，`package_checksum` 不变，provision 预测出的变体 `model_id` 也就与旧的相同——
没有后继可言，这条通道无事可做。要得到新的一代，hop 1 的 baseline 发布须带 `--package-version-template` 并在默认模板
`vbasins-{slug_id}-{content_hash}-{source_hash}` 之后加一个本次专属后缀（例如 `...-{source_hash}-g2`），同时把
`--registry-manifest` 指向本次专属文件（理由见 [`service-bringup.md`](service-bringup.md) hop 1 的坑）；版本号变了，
`package_checksum` 与变体 `model_id` 随之变，再把这份 registry 交给上面的 `--baseline-registry` 走 dry-run 与 `--apply`。

**node-22 这一侧用一条命令（#2739）。** provision `--apply` 之后，回拷、克隆、发布、refresh 以及调度器 timer 的停与启，
由 `scripts/node22_model_succession.py` 按固定顺序做完，不连库、不跨机 ssh：

```text
copyback（新包从共享根拷到 scratch 根） -> preflight（克隆工具与发布工具各自的 dry-run）
  -> begin（记下 timer 原状态、停 timer、等在跑的 pass 自己结束） -> clone -> publish -> refresh
  -> finish（两份 manifest 逐字节相同且已换成新 id，timer 原来是 active 才启动）
```

前两步在调度器照常运行时做，timer 只为 clone / publish / refresh 停。每步完成写一份
`<回执根>/<succession-id>/step-<步名>.json`；已有回执的步骤直接跳过，所以**同一条命令重跑就是续跑**；
上一步回执缺失时本步拒绝并点名缺的路径（先发布、后克隆因此做不出来）。克隆与发布两个工具各自的回执
（`clone-dry-run.json` / `clone-apply.json` / `publish-dry-run.json` / `publish-apply.json`）原样落在同一目录。

```bash
# node-22，frd_muziyao
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env   # 两个根、prefix、两份 manifest、state index、refresh 锁与回执根；不含 DB 变量
set +a
SUCCESSION_ID=<succession-id>                        # 与 provision 那一步相同
SUCCESSION_ARGS=(
  --succession-id "$SUCCESSION_ID"
  --kind recalibration
  --pair "<旧 M1 的 model_id>:<M1′ 的 model_id>"      # 每个 (流域, source) 一对，重复写
  --cutover-time <YYYYMMDDHH>                        # 克隆行的 valid_time，选法见下文「t* 怎么选」
  --new-rows-registry "<node-22 视角的路径>"            # provision 的 --output-registry；回执记了 object_store_key 时可省
  --operator-id "<operator>"
)
LOG=/scratch/frd_muziyao/nhms-prod/workspace/succession-$SUCCESSION_ID.log

# 1) dry-run：不改任何文件、不写回执、只发 is-active 查询；读它打印的 JSON
cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_model_succession "${SUCCESSION_ARGS[@]}"

# 2) 报告无误后 detached 执行（等一趟在跑的 pass 可能要三个多小时）；看 $LOG 与回执目录
cd /scratch/frd_muziyao/NWM && { setsid nohup .venv/bin/python -m scripts.node22_model_succession "${SUCCESSION_ARGS[@]}" --apply > "$LOG" 2>&1 < /dev/null & }
```

- **读 dry-run 报告看什么**：`would_be_refused` 为空；`steps.copyback.packages[]` 每个包是 `would_copy` 还是
  `already_present`（`differs` 即 scratch 上已有一份不同的包，apply 会拒绝且不覆盖）；包已在 scratch 上时
  `steps.preflight` 给出 `kind_check`（每一对在八个 state-compatibility 面上是否相等；有结构变更的一对就拒绝，并在
  `would_be_refused` 里多一条，出路见 5.7.2）与两个工具 dry-run 的结论，否则是 `needs copyback`
  （apply 会在 preflight 里跑，仍在停 timer 之前）；
  `unit_states_now` 是 timer 与 service 此刻的状态。refresh 与 finish 不做预测。
- **timer 由工具自己停、自己启。** `begin` 把 timer 当时是否 active 记进 `timer-before-stop.json`（只写一次，续跑时
  读它、不重新推断），发一次普通的 `stop`，然后轮询等 `nhms-compute-scheduler.service` 自己结束
  （`--pass-wait-seconds`，默认 14400；超时算失败，同一条命令续等）。它**从不** stop / kill service，不 enable /
  disable / mask 任何 unit，不装 drop-in 围栏；取而代之的是 clone / publish / refresh / finish 每步动手前都复查
  timer 与 service 均未在跑，发现有人中途启动了调度器就拒绝、该步什么都不写。
  这是第一个会停、启调度器 timer 的工具；`scripts/install_node22_scheduler_file_provider_refresh.sh` 仍把该 timer
  当受保护对象（那个安装器只断言自己没改动它）。succession 持有 timer 期间 stall probe 报 `timer_stopped` 属预期。
  `finish` 对 timer 做了什么写在 `step-finish.json` 与 apply 报告的 `timer_action` 里：`started`，或
  `left_stopped_was_inactive_at_begin`（`begin` 时 timer 本来就没在跑，工具不启动它；此时报告的 `timer_note` 与
  stderr 都会明说 timer 仍是停的，调度器该跑就手工启动）。
- **一次只能有一个 succession 持有 timer。** 某个 succession 停了 timer（它的 `timer-before-stop.json` 里
  `timer_was_active=true`）却既没 finish 也没 abort 时，别的 `--succession-id` 的 apply 一律拒绝并点名它，dry-run 把
  同一条写进 `would_be_refused`——否则后来者会把 timer 记成「本来就没在跑」，finish 时让调度器一直停着。先续跑它，
  或先放弃它（见下文「放弃」）。回执根下别人的 `timer-before-stop.json` 读不出来同样拒绝并点名路径，不会跳过。
  这项检查在任何步骤之前做一次，`begin` 记 timer 状态之前再做一次（copyback / preflight 期间可能有别的 succession
  先停了 timer；此时 `begin` 失败，不碰 timer、不写自己的 `timer-before-stop.json`）。
  检查只扫**同一个回执根**：换一个 `--receipt-root` 就绕过了它，生产只用默认回执根，不要给这个选项。
  写 `timer-before-stop.json` 时被 kill 或磁盘写满会留下半截文件（那一刻 timer 还没被碰过）：它会挡住本次和
  其他所有 succession，直到操作员先核对 timer 的实际状态、再把这个文件移走。
- **任何一步失败，工具都不启动 timer。** 非零退出，并写 `succession-failed-<UTC 时间戳>.json`：失败的步骤与原因、
  已有哪些回执、**当时实测**的 timer / service 状态、timer 是否由本工具停的，以及出路。copyback / preflight 失败发生在
  碰 timer 之前，回执里写明。出路两条：排除原因后**原样重跑同一条命令**；或放弃（见下条）。被 kill 的运行不留失败
  回执，同样重跑即可。命令行与首次 apply 写下的 `plan.json`（pairs 及其顺序、cutover time、provision id、provision
  回执与 registry 的 sha256）不一致时拒绝；计划变了就换一个 `--succession-id`——但本次若已经停过 timer，
  **先用首次 apply 的那条命令行加 `--abort --confirm-timer-start` 放弃它**：只有它能把 timer 启回来，它还持有 timer 时
  新 id 会被拒绝。已有的 `clone-dry-run.json` / `publish-dry-run.json` 与计划不符时同样在 preflight（停 timer 之前）拒绝，
  出路相同。
- **hard stop（不要重跑，转下面的手工步骤）**：`clone-apply.json` 是 `aborted`（已有克隆行写入，工具不重试克隆，
  按 5.7 的 receipt 判读处理）；存在 `outcome=inconsistent` 的 `publish-apply-failed-*.json`；发布成功但回执没写出
  （发布**已经生效**：不要从备份恢复、不要再跑 `--apply`，接着手工做 provider refresh；timer 则用首次 apply 的那条
  命令行加 `--abort --confirm-timer-start` 启动——它在 timer 原来是 active 时启动 timer，并把这个 id 关掉，否则它一直
  算作持有 timer，之后的 succession 都会被拒绝）。
  发布工具的 `refused` / `rolled_back` 是普通失败，重跑会重试。
- **放弃**：把上面命令的 `--apply` 换成 `--abort --confirm-timer-start`。timer 原来是 active 就启动它，写
  `abort-<UTC 时间戳>.json`，此后该 `--succession-id` 的任何运行都拒绝；只给 `--abort` 则只打印报告、什么都不做。
  publish 之前放弃：调度器继续跑旧 model；**已写入的克隆行留在两份 state index 里**，而调度器取某 model 最早的克隆行
  作为它的 cutover time——以后对同一个新 id 用更晚的 cutover time 再做一次 succession，生效时刻仍是这一次的。
  publish 之后放弃：新 model 已经生效，余下的 refresh 与核对必须按下面的手工步骤做完。报告与回执的 `publish_state`
  是 `not_published` / `published` / `published_without_receipt`（本次已过 clone、两份 manifest 已是新 id 但
  `publish-apply.json` 没写出，同样按「已生效」处理）/ `manifests_differ`；`timer_was_active_at_begin=false` 时
  timer 在 succession 开始前就是停的，放弃不会启动它。
- **`manifests_differ`（两份 manifest 不一致或读不出来）**：最先判定，典型来源是发布在两次写入之间被 `kill -9`，
  不留回执。已过 clone 的 succession 此时 dry-run / `--apply` 在任何步骤之前拒绝；`--abort --confirm-timer-start`
  照常写放弃回执并关掉这个 id，但**不启动 timer**（worker 在两份不一致时拒绝 submit，启动调度器只会产出失败），
  报告与回执里写明。按下文发布工具的做法比对两份的 sha256、从本次的 `.bak-<succession-id>-<stamp>` 备份恢复两份，
  恢复后重跑同一条命令，或放弃后手工启动 timer。

**下面是逐步的手工做法**——succession 命令被 hard stop 之后的回退路径，也是每一步在做什么的说明。

倒过来做的后果**比这段原文写的更重**（原文早于 #1164）：manifest 先落地时，`M1′`
在任何 generation 都没有 state 行，走的是 first-cycle 分支
（`services/orchestrator/scheduler_generation.py` 的 `evaluate_transition_decision`
里 `not history.exists_any_generation` 那支）；而生产 registry 行带
`manifest_uri`，会产出一个**合格**的 packaged-IC 信号，于是该 run 被
**放行**为 `PACKAGED_IC_BOOTSTRAP`（同一分支内 `packaged_initial_condition.qualified`
为真的返回），
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
> declaration）。更糟的是它不必等过期：renewal 的 `removed` 恒为空，retire entry
> 按 rule 1 直接判无效，**下一次** refresh 就以 `registry_cutover_declaration_invalid`
> 拒跑（此后每一次都拒，直到删掉 env 行），把每日管线拖停；replace entry 也匹配不到
> 任何 `package_changed`，一旦过期或 generation 不符同样拒跑。§3.1.2 的
> 「retire declaration 恢复顺序」只适用于非 direct-grid 拓扑（见该节的拓扑围栏），
> 不适用于这里。

手工发布跑 `scripts/node22_publish_merged_scheduler_registry.py`（#2738）。它在 node-22 上、不连库，
以两份 manifest 的属主 `frd_muziyao` 身份运行：把「当前 canonical 全量 − 旧 M1 行 + provision 输出的
M1′ 行」合并（没被点名的行原样、原序保留，被替换的行原位换成 provision registry 里的那一行），
先各备份一份，再发 canonical（对读到的那份字节做 CAS），然后用**同一个 `generated_at`** 发 scratch
mirror（两份字节相同是结构性的，不依赖事后比对），最后回读两份并要求 sha 相同。
不带 `--apply` 就是 dry-run：全部校验照做、预测合并后 manifest 的大小，但不写任何一份 manifest、
不备份、不取锁，只落一份回执。与 provision 共用同一个 `--succession-id`：

```bash
# node-22，frd_muziyao
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env   # 两份 manifest 路径、两个根、prefix、refresh 锁；不含 DB 变量
set +a
SUCCESSION_ID=<succession-id>                        # 与 provision 那一步相同
PUBLISH_ARGS=(
  --replace "<旧 M1 的 model_id>:<M1′ 的 model_id>"   # 每个 (流域, source) 一对，重复写
  # provision 的 --output-registry 在 node-22 上的路径；它在 object store 之外时回执不记 object_store_key，必须给
  --new-rows-registry "<node-22 视角的路径>"            # /home/ghdc/nwm/... 在这里写成 /ghdc/data/nwm/...
  --operator-id "<operator>"
  --succession-id "$SUCCESSION_ID"
)

# 1) dry-run
cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry "${PUBLISH_ARGS[@]}"

# 2) 读回执，逐项核对后再继续
cat "$NHMS_SCHEDULER_PROVIDER_STORE_ROOT/scheduler/succession/$SUCCESSION_ID/publish-dry-run.json"

# 3) 调度器 timer 先停（systemctl --user stop nhms-compute-scheduler.timer），确认 service 不在跑，再 apply；
#    参数与第 1 步逐字相同，只多一个 --apply。发布并跑完 refresh 之后再把 timer 恢复原状态
systemctl --user is-active nhms-compute-scheduler.timer nhms-compute-scheduler.service || true
cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry "${PUBLISH_ARGS[@]}" --apply
```

- **`--replace` 的两个 id 从哪来**：M1′ 取 `provision-apply.json` 的 `models[].model_id`；旧 M1 按
  `(basin_version_id, source_id)` 从 canonical manifest 取。新行只能来自 provision 的输出 registry：
  工具要求同 id 的 `provision-apply.json`、核对该 registry 文件的 sha256 与回执一致，并拒绝
  「回执里有、但既没被本次操作引入也不在 canonical 里」的 `model_id`。provision 的输出 registry 在
  object store 之外时用 `--new-rows-registry <路径>` 指明；发布计划变了就换一个新的 `--succession-id`，
  用 `--provision-succession-id` 指回没变的那次 provision。
- **读 dry-run 回执看什么**：`introduced_model_ids` / `removed_model_ids` 与预期一致、
  `row_count_before == row_count_after`、`replaced[]` 每对的 `basin_id` / `source_id` 以及新旧
  `basin_version_id`、`canonical.sha256_before == mirror.sha256_before`、
  `manifest_bytes_remaining` 与 `manifest_json_nodes_remaining` 为正。2026-10-05 生产实测
  132 行、13,353,454 B、329,431 个 JSON 值节点，上限 32 MiB / 800,000 节点（节点先撞，口径见
  [`service-bringup.md`](service-bringup.md) 的 hop 4）。
- **发布前工具自己拒绝的情形**（都在任何写入之前，报错里点名 `model_id`）：两份 manifest 字节不同
  （先跑一趟 refresh 让两份对齐）、id 不在 / 已在 canonical、同一个 id 出现在两个操作里、行缺
  `resource_profile.direct_grid_source_id` 或它不在该行 contract 的 `applicable_source_ids` 里、
  replace 换了流域或 source、合并后某流域不是「每个 source 恰好一行」、新包在 `OBJECT_STORE_ROOT`
  （scratch，回拷没做就是这里拒）或 `NHMS_SCHEDULER_PROVIDER_STORE_ROOT` 下缺失或校验和不符、
  合并结果过不了 publisher 自己的校验 / 字节上限 / 节点上限。
- **apply 期间调度器 timer 应处于停止状态**（整段 rollout 已按上文 disabled 的话保持即可；工具自己不停、
  不启任何 timer 或 service，也不跑 refresh）。两份 manifest 不一致时 **worker 会拒绝 submit**，所以工具
  保证不留下这种状态：apply 全程不阻塞地持有 provider refresh 锁（refresh 正在跑就直接拒绝），任何一步失败
  都把**本次提交过的**那份按提交时的 preimage 恢复成读到的字节。
- **apply 的三种失败结局**（都非零退出，各留一份 `publish-apply-failed-<UTC 时间戳>.json`，与本次备份
  同一个时间戳）：`refused`＝两份都没被本次改动；`rolled_back`＝改过的已恢复，两份回到原字节；
  这两种直接用同一个 `--succession-id` 重跑 apply。`inconsistent`＝恢复失败，或某份被别的写者改了（工具
  不会覆盖别人的字节）：报错给出两份当前 sha256、两个备份路径和两条出路——用备份恢复两份，或跑一趟
  refresh 把 canonical 的行重发到两份。成功只写 `publish-apply.json`（`outcome=published`），它存在后同一个
  id 不能再 apply。若两份都已发布、回读一致但回执写不出，工具**不回滚**，非零退出并打印 sha256、两个备份
  路径与 `manifest_generated_at`，按「已发布、未留回执」手工记录后继续。
- 备份是 `<manifest>.bak-<succession-id>-<UTC yyyymmddThhmmssZ>`，每次 apply 尝试各一对，从不覆盖。
- **`--apply` 没有任何输出就死了**（被 `kill -9`、节点掉电等，来不及恢复也来不及写回执）：比对两份 manifest 的
  sha256，不同就把两份都从本次的 `.bak-<succession-id>-<stamp>` 备份恢复。

**发布后再手动跑一趟 refresh**：renewal 重建 canonical readiness 并留下
`outcome=published` / `refused=[]` 的 receipt（触发方式与判据见下文「触发手动 refresh 的坑」）。

2026-08-22 实测（Huai-MAIN + jialingjiang，各 gfs/IFS 两行，`t*`=2026-08-22T00:00:00Z）：
34 行进、34 行出；旧四行消失、新四行到位；两份 sha 相同；随后 renewal receipt
`added:0 removed:0 package_changed:0 refused:0`——`removed:0` 正是上面那段的实证
（确实删了四个 model_id，闸却看不到）。

> **M1′ 已被 #1816 接替。** 上面这次写进 manifest 的 Huai-MAIN M1′ id（`dg_281ff8c7…` gfs /
> `dg_03b3cd97…` ifs）在 2026-08-24 #1816 重发布时被换掉；8 个流域 16 行的 old→new 映射与首趟暖启证据见
> [`../receipts/2026-08-24-issue-1816-republish-identity.md`](../receipts/2026-08-24-issue-1816-republish-identity.md)。

**触发手动 refresh 的坑**：refresh 不与 scheduler pass 并行。wrapper
（`scripts/scheduler_file_provider_refresh_once.sh`，#2749）启动后先读
`systemctl --user is-active nhms-compute-scheduler.service` 的输出：`inactive` / `failed`
直接刷新；`active` / `activating` / `deactivating` / `reloading`（oneshot 的 pass 在跑时是
`activating`）就等这一趟 pass 结束，每 15 秒查一次、每分钟在 stderr（journal）记一行；
unit 不再带 `ExecCondition`，不存在「静默 skip」这条路。所以 `systemctl --user start`
会**一直阻塞**到在跑的 pass 结束、refresh 也跑完才返回；等待期间 unit 的 `Before=` 把下一趟
pass 排在 refresh 后面，refresh 最多等一趟。等满 5400 秒 pass 仍未结束，或状态读不出来
（`systemctl` 缺失、无输出、未知状态），wrapper 以 exit 3 拒绝、不做任何刷新，`start` 返回
非零、unit 进 `failed`（再次触发或跑 installer 前先 `reset-failed`）。因为会阻塞，`start`
同样要 detached 跑。判据仍然只有一个：`latest.json` 的 `started_at` 变新（`start` 返回 0
而 `started_at` 没变，说明当时已有一趟 refresh 在跑，这次 `start` 只是跟着它返回）。**不要**用
「receipt 文件数增加」判成功——`latest.json` 是原地覆写的，计数不变，照此写循环会无限重试、
反复触发 refresh。

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

#### 5.7.2 结构变更的 cold-start succession（#2740）

5.7.1 的 `--kind recalibration` 靠克隆行让新 model 接过旧 model 的 state。**结构变更**做不到这一点：新旧两个包在
八个 state-compatibility 面（`workers/mapping_builder/rewrite.py` 的 `STATE_COMPATIBILITY_SURFACES`，即十个
水文核心面去掉 `calibration` 与 `solver_config`）上只要有一面不等——mesh、河网、lake、soil / geol / land、
`.sp.att` 的非 `FORC` 列，或 `cfg.ic` 的字节——旧 state 就不可用，克隆门以 `state_compatibility_unequal` 拒绝。
这时用 `--kind cold_start`：不克隆、不读写任何 state index，新 model 从包内率定好的初始条件起步。

**哪种变更算结构变更，以门的比较结果为准，不靠人读 diff。** 只改 `cfg.calib` 或只改 `cfg.para` 是 state-compatible，
属于 5.7.1 的 recalibration——把它当 cold start 做会白白丢掉可用的 state。两种 kind 的 `preflight` 都先对每一对
`--pair` 做这同一项八面比较（见下文「preflight 拒绝什么」），kind 给错了在停 timer 之前就被点名。

调度器这一侧**不需要任何批准产物**：一个在所有 generation 都没有 state 历史的 `model_id` 走
`services/orchestrator/scheduler_generation.py` 的 first-cycle 分支——包内 IC 合格时放行为
`packaged_ic_bootstrap`，不合格或读不出时 block 为 `first_cycle_initial_state_undecided`。cold-start succession
做的是在发布之前**证明**包内 IC 合格，并在回执里写明过程线不连续。

步骤比 recalibration 少一个 `clone`，其余（timer 的停与启、续跑、失败回执、`--abort`）与 5.7.1 相同：

```text
copyback -> preflight（kind 检查、包内 IC 审计、发布工具的 dry-run）
  -> begin（记下 timer 原状态、停 timer、等在跑的 pass 自己结束） -> publish -> refresh -> finish
```

每步仍然只认它在本 kind 步骤表里前一步的回执：`publish` 要 `step-begin.json`。provision 那一步与 5.7.1 相同
（node-27 上先 dry-run 再 `--apply`，同一个 `--succession-id`）。

命令就是 5.7.1 那个代码块：环境、`LOG`、dry-run 与 detached `--apply` 两行**逐字相同**，只有 `SUCCESSION_ARGS`
换成下面这样（差别是 `--kind`，以及 `--cutover-time` 的含义）：

```bash
SUCCESSION_ARGS=(
  --succession-id "$SUCCESSION_ID"
  --kind cold_start
  --pair "<旧 model_id>:<新 model_id>"                # 每个 (流域, source) 一对，重复写
  --cutover-time <YYYYMMDDHH>                        # 操作员声明的切换时刻：只记录，工具不强制
  --new-rows-registry "<node-22 视角的路径>"            # provision 的 --output-registry；回执记了 object_store_key 时可省
  --operator-id "<operator>"
)
```

dry-run 同样不改任何文件、不写回执（包括 `ic-audit.json`）、只发 `is-active` 查询。

- **读 dry-run 报告看什么**：`steps` 里没有 `clone`；新包已在 scratch 上时 `steps.preflight` 给出 `kind_check`、
  `ic_audit`（`outcome` 以及每个新 model 的 `ic_status`）与 `publish_dry_run`，否则是 `needs copyback`；
  `would_be_refused` 为空；`continuity` 就是下文那份不连续声明。
- **preflight 拒绝什么（都在停 timer 之前，什么都没发布）**：
  - **kind 与包不符。** `cold_start` 的某一对八面相等：点名这一对，要求改用 `--kind recalibration`；
    `recalibration` 的某一对八面不等（包括某个面的文件只在一侧存在）：点名这一对，要求改用 `--kind cold_start`。
    `plan.json` 记了 kind，所以改 kind 要换一个 `--succession-id`，并用 `--provision-succession-id` 指回原来的
    provision。一次 succession 里两种对都有时同样拒绝：拆成两次 succession，各用各的 kind。
    `recalibration` 的这项检查排在克隆 dry-run 之前——克隆门先看源 state，结构变更的一对若在 cutover time 没有
    合格的源 state，原本只会因为那个原因被拒，看不出是 kind 错了。
  - **比较做不成。** model 行找不到（旧行取自 canonical manifest，新行取自 provision 的 registry）、包目录不在
    scratch 根上、`*.sp.att` / `*.cfg.ic` / `*.cfg.para` 不是恰好一个、文件解析不了：这是步骤失败，**不算**
    「不等」，工具不会据此建议换 kind。修好包或行之后原样重跑。
  - **包内 IC 不合格**（仅 `cold_start`）。工具进程内调用 `scripts/audit_first_cycle_initial_state.py`，对象是
    provision 的 registry（发布要用的那些行）、object store 根取 scratch 根（run 实际读的包）。计划里每个新
    model 至少要有一条审计行，且每条的 `ic_status` 都是 `qualified`；`unqualified`（没有规范位置的
    `<shud_input_name>.cfg.ic`、文件为空、首行不是 3 或 4 个数值 token）、`unreadable`（包 manifest 读不出，或规范位置的 IC 对象探测不了）、
    `absent`（registry 行没有 `manifest_uri`）或审计被 block 都拒绝，消息逐个列出 model 与它的 `ic_status`。
    该 registry 里不属于本计划的行会出现在审计回执里，但不参与判定。
- **`ic-audit.json`**：审计通过时写一次（审计工具自己的回执格式，写在 succession 目录，从不覆盖），
  `step-preflight.json` 记它的路径与 sha256；审计不通过不留文件，修好之后原样重跑即可。续跑时已有的
  `ic-audit.json` 只读不重写，并按同一条件再判一次；同时只读地重审一遍，计划里每个新 model 的 `ic_status` 与
  `ic_sha256` 必须与回执里记的相同——scratch 上的包在审计之后被改过就是步骤失败，回执不会重写，出路是换一个
  `--succession-id`（用 `--provision-succession-id` 指回原来的 provision）。`publish` 调发布工具之前再读它一遍：文件缺失、sha256 与
  `step-preflight.json` 记的不一致、或内容不再满足条件，都是步骤失败、什么都不发布——此时 timer 已停，
  把文件恢复原样后重跑，或按 5.7.1 的「放弃」处理。
- **不连续声明（`continuity`）**：`plan.json`、`step-publish.json`、`step-finish.json`、dry-run 报告与 apply
  报告都带 `continuity`：`mode=cold_start`、`state_carried=false`、`declared_cutover_time`（即
  `--cutover-time`，按操作员声明记录，工具不强制）以及一段 `notice`——旧 model 的 state 不承接；每个新 model 在
  timer 启动后调度器规划的第一个 cycle 从包内率定 IC 起步；这些流域的水文过程线在该处**断开**。对外通告时引用它。
- **失败、hard stop 与放弃**：规则与 5.7.1 相同，失败消息里的 runbook 指向本节。`published_without_receipt` 与
  `manifests_differ` 在 cold start 里以 `step-begin.json` 存在为「本次已到发布」的判据（recalibration 是
  `step-clone.json`）。hard stop 之后的手工做法（比对两份 manifest、从备份恢复、provider refresh、启动 timer）
  就是 5.7.1 里发布与 refresh 那两段，克隆那段不适用。放弃时没有克隆行需要交代。

#### 5.7.3 新流域上线的 add-basin succession（#2756）

5.7.1 与 5.7.2 都是**换** model（`--pair 旧:新`）。把一个调度器里还没有的**新流域**接进来没有旧 model 可言：
它的 dg 变体只需要回拷到 scratch 根、证明包内 IC 合格、加进合并 manifest、再被一趟 provider refresh 接住，发布前后
调度器停着。这就是 [`service-bringup.md`](service-bringup.md) 3.1.1「新流域上线四跳」的 hop 3b 到 hop 4，
用 `--kind add_basin` 一条命令做完，顺序陷阱（先发布后回拷、timer 没停就发布、发布后忘了 refresh）与另外两种
kind 一样由工具关掉。

**这条命令之前的几跳仍然手工做，命令不变**（都在 `service-bringup.md` 3.1.1）：hop 1 baseline 发布（node-22）、
hop 1b 把 staged 树拷进 NFS Basins、hop 2 node-27 登记 baseline（seed）、hop 3 node-27 provision dg 变体
（先 dry-run 再 `--apply`，`--succession-id` 与本节相同）。工具**从不碰任何 Basins 目录**，也不做 seed 与 provision；
它的输入只有 hop 3 留下的 `provision-apply.json` 与 `--output-registry`。

步骤与 cold start 相同，只是 `preflight` 没有 kind 检查——没有成对的新旧包可比：

```text
copyback（新包从共享根拷到 scratch 根） -> preflight（包内 IC 审计、发布工具的 dry-run）
  -> begin（记下 timer 原状态、停 timer、等在跑的 pass 自己结束） -> publish -> refresh -> finish
```

timer 的停与启、每步的回执与续跑、失败回执、`--abort`、hard stop 都与 5.7.1 相同；`publish` 要 `step-begin.json`，
不读写任何 state index。

命令就是 5.7.1 那个代码块：环境、`LOG`、dry-run 与 detached `--apply` 两行**逐字相同**，只有 `SUCCESSION_ARGS`
换成下面这样（没有 `--pair`，也没有 `--cutover-time`——新流域没有历史，谈不上切换时刻）：

```bash
SUCCESSION_ARGS=(
  --succession-id "$SUCCESSION_ID"                   # 与 hop 3 的 provision 相同
  --kind add_basin
  --add "<新流域的 gfs model_id>" --add "<新流域的 IFS model_id>"   # 每个新流域每个 source 一个，重复写
  --new-rows-registry "<node-22 视角的路径>"            # provision 的 --output-registry；回执记了 object_store_key 时可省
  --operator-id "<operator>"
)
```

- **参数按 kind 检查，写任何文件之前就拒绝**：`add_basin` 至少要一个 `--add`，给了 `--pair` 或 `--cutover-time`
  都拒绝；`recalibration` / `cold_start` 给了 `--add` 拒绝，并且仍然必须有 `--pair` 与 `--cutover-time`；同一个
  `model_id` 写了两次也拒绝。一次 succession 只做一种事：既要换 model 又要加流域，就拆成两次，各用各的 provision 与 `--succession-id`。
- **任何步骤之前就拒绝的计划**：`--add` 的 id 已经在 canonical manifest 里（已经在调度，无事可加）；`--add` 的 id
  不在 `provision-apply.json` 的 `models[]` 里；`provision-apply.json` 列了一个既没有用 `--add` 写出、也不在
  canonical manifest 里的 id（provision 出来的行不能被无意漏掉——hop 3 的每个 `models[].model_id` 各写一个 `--add`）。
- **读 dry-run 报告看什么**：`steps` 是上面六步，没有 `clone`；新包已在 scratch 上时 `steps.preflight` 给出
  `ic_audit`（`outcome` 以及每个新 model 的 `ic_status`）与 `publish_dry_run`（`introduced_model_ids` 就是本次
  全部新行，`row_count_after = row_count_before + --add 的个数`），其中**没有** `kind_check`；否则是
  `needs copyback`；`would_be_refused` 为空；`continuity` 就是下文那份声明。
- **preflight 拒绝什么（都在停 timer 之前，什么都没发布）**：
  - **包内 IC 不合格。** 与 5.7.2 是同一项审计、同一个判据（每个新 model 至少一条审计行，且每条 `ic_status` 都是
    `qualified`），`ic-audit.json` 的写入、续跑时的只读重审、`publish` 之前的再读一遍也都相同。它取代了 hop 3b
    后面那段手工跑 `scripts/audit_first_cycle_initial_state.py` 的硬闸。审计按计划里出现的每个 source 各审一遍，
    所以两个 source 的新流域每个 model 有两条审计行。
  - **发布工具的 dry-run 拒绝。** 发布工具自己的检查原样生效，工具不重复实现：某个流域不是 canonical manifest 的
    每个 source 恰好一行（只加了 gfs、没加 IFS 就落在这一条——同一流域的各 source 必须一起加）、新包在 scratch 根
    或 NFS 根下缺失或校验和不符、非 `direct_grid` 行、manifest 的字节或 JSON 节点上限。失败消息以
    `The publish dry-run refused` 开头，后面是发布工具的原话。
- **`continuity`**：`plan.json`、`step-publish.json`、`step-finish.json`、dry-run 报告与 apply 报告都带
  `continuity`：`mode=new_basin`、`state_carried=false` 与一段 `notice`，没有 `declared_cutover_time`。
- **发布之后：不需要 forcing 回补，新流域自己逐 cycle 追赶。** 纯新增流域没有改过 `model_id`，不触发
  `service-bringup.md` 3.1.1 的 hop 5（forcing 回补是换代场景专有的）。timer 启动后，调度器为每个新 model 规划的
  第一个 cycle **不是当前 cycle，而是 lookback 窗口里最早的那个**：model 在那里从包内率定 IC 起步，然后一个
  cycle 一个 cycle 追到当前。追赶期间 `service-bringup.md` 3.1.1 末尾的两条 lookback 约束照样适用：
  回填深度（`NHMS_SCHEDULER_LOOKBACK_HOURS + NHMS_SCHEDULER_CYCLE_LAG_HOURS`）必须落在 node-27 压缩截止之内，
  否则旧 cycle 的河段时序落进已压缩 chunk 被拒写；**追赶没完成之前不得收窄 lookback**，否则下一个未完成 cycle
  落到窗外，model 永久阻塞并占住该 source 的回填槽。全国径流图层在新河网追平之前的表现也见那一段。
- **失败、hard stop 与放弃**：规则与 5.7.1 相同，失败消息里的 runbook 指向本节。「本次已到发布」的判据与 cold start
  一样是 `step-begin.json`。hard stop 之后的手工做法就是 5.7.1 里发布与 refresh 那两段（发布工具的参数是每个新
  model 一个 `--add`，见 `service-bringup.md` 3.1.1 的 hop 4），克隆那段不适用。发布之前放弃时，调度器照常运行，
  只是没有新流域的 model；已经回拷到 scratch 根的包留在原处。

#### 5.7.4 流域退役的 remove-basin succession（#2757）

5.7.1 到 5.7.3 都会让调度器多出或换上 model。**退役一个流域**正相反：把它的 `dg_*` 行从两份 manifest 里拿掉，
调度器从此不再为它规划 run（[`operating-scope.md`](operating-scope.md) 7.2 五步里的第 1 步）。这一步用
`--kind remove_basin` 一条命令做完：timer 的停与启、回执、发布、refresh、续跑与 `--abort` 都由工具管，
不再手工停 timer、手工跑发布工具。

**这只是退役的 node-22 一半。** 工具**从不碰任何 Basins 目录**，不动 node-27 上的 run、`AUTOPIPE_EXCLUDE_BASINS`
与 `core.model_instance`；那些是 `operating-scope.md` 7.2 的第 2 步起，由 node-27 的退役工具
`scripts/node27_retire_basin.py` 接手（命令、回执与手工复活见 [`operating-scope.md`](operating-scope.md) 7.6），
它用**同一个 `--succession-id`**，每个 basin version 跑一次，并且在本节的 succession 跑完（`step-finish.json`）之前拒绝运行。

没有新包、没有 provision，所以没有 `copyback`，也没有 kind 检查、IC 审计与克隆，五步：

```text
preflight（发布工具对这次移除的 dry-run）
  -> begin（记下 timer 原状态、停 timer、等在跑的 pass 自己结束） -> publish -> refresh -> finish
```

timer 的停与启、每步的回执与续跑、失败回执、`--abort`、hard stop 都与 5.7.1 相同；`publish` 要 `step-begin.json`，
不读写任何 state index，回执根下也不需要任何 `provision-apply.json`。

命令就是 5.7.1 那个代码块：环境、`LOG`、dry-run 与 detached `--apply` 两行**逐字相同**，只有 `SUCCESSION_ARGS`
换成下面这样：

```bash
SUCCESSION_ARGS=(
  --succession-id "$SUCCESSION_ID"                   # 一次退役一个；node-27 的退役工具之后用同一个
  --kind remove_basin
  --remove "<该流域的 gfs model_id>" --remove "<该流域的 IFS model_id>"   # 每个要退的流域每个 source 一个，重复写
  --operator-id "<operator>"
)
```

要退的 `model_id` 按 `basin_id` 从 canonical manifest 取（`dg_*` 是哈希、不含流域名）。

- **参数按 kind 检查，写任何文件之前就拒绝**：`remove_basin` 至少要一个 `--remove`，给了 `--pair`、`--add`、
  `--cutover-time`、`--provision-succession-id`、`--new-rows-registry` 任何一个都拒绝；另外三种 kind 给了
  `--remove` 拒绝；同一个 `model_id` 写了两次也拒绝。既要退流域又要换或加 model，就拆成几次，各用各的 `--succession-id`。
- **任何步骤之前就拒绝的计划**：`--remove` 的 id 不在 canonical manifest 里（消息是
  `old model_id is not in the canonical manifest`）；回执根目录不存在（没有 provision 回执可读，最先撞上的是
  「别的 succession 是否占着 timer」那项检查，消息以 `cannot list the receipt root` 开头——生产回执根一直存在，
  自己指定 `--receipt-root` 做演练时先建好目录）。
- **读 dry-run 报告看什么**：`steps` 是上面五步，没有 `copyback` 与 `clone`；`steps.preflight` 只有
  `publish_dry_run`，其中 `removed_model_ids` 就是本次要退的全部行、`row_count_after = row_count_before − --remove 的个数`；
  `would_be_refused` 为空；报告里没有 `provision_apply_receipt` 与 `new_rows_registry`（`plan.json` 里同样没有这些键）。
- **preflight 拒绝什么（在停 timer 之前，什么都没发布）**：发布工具的 dry-run 拒绝，原话照搬，工具不重复实现——
  最常见的是只退了一个流域的部分 source（只写了 gfs、没写 IFS：合并后的 manifest 里每个流域必须是每个 source
  恰好一行，同一流域的各 source 必须一起退）。失败消息以 `The publish dry-run refused` 开头。
- **不查批处理队列**：各流域的 Slurm job name 相同，`squeue` 分不出哪些 job 属于要退的 model。`begin` 会等在跑的
  那趟 pass 自己结束；此前已经提交的 run 之后仍可能跑完并入库，这由 node-27 那一半处理（先加排除名单，再翻
  `superseded`）。
- **`continuity`**：`plan.json`、`step-publish.json`、`step-finish.json`、dry-run 报告与 apply 报告都带
  `continuity`：`mode=basin_removed`、`state_carried=false` 与一段 `notice`——发布之后调度器不再规划该流域、
  已提交的 run 仍可能跑完入库、下一步是 node-27 的退役工具并用同一个 `--succession-id`。
- **失败、hard stop 与放弃**：规则与 5.7.1 相同，失败消息里的 runbook 指向本节。「本次已到发布」的判据是
  `step-begin.json`：有了它而两份 manifest 里已经没有任何要退的 id、又没有 `publish-apply.json`，就是「发布已生效
  但没有回执」的 hard stop——不要撤销、不要重跑，手工做完 provider refresh 再启动 timer（5.7.1 里 refresh 那一段）。
  `begin` 之前同样的情形只是上面那条「不在 canonical manifest 里」的普通拒绝。发布之前用
  `--abort --confirm-timer-start` 放弃时 timer 恢复，该流域照常继续被调度。工具之外的手工回退办法仍是
  `operating-scope.md` 7.2 末尾那段直接跑发布工具的命令。

### 5.8 node-22 file-journal cycle cold archive（已启用，#2119）

自 2026-09-08 起，生产 `nhms-scheduler-journal-retention.timer` 已启用；激活审计证据见
[2026-09-08 node-22 journal-retention live activation](../receipts/2026-09-08-node22-journal-retention-live-activation.md)
（[#2119](https://github.com/DankerMu/SHUD-NWM/issues/2119)）。长期 provider-timer 漂移治理由
[#2146](https://github.com/DankerMu/SHUD-NWM/issues/2146) 跟踪。

此 user-systemd oneshot 仅会归档 `NHMS_SCHEDULER_JOURNAL_ROOT` 下 `latest/`、`journal/` 和
`pipeline-events/` 中完整的 `(source_id, cycle)` 切片。它**不会**扫描、改写或删除
`pipeline-jobs/`、`reconcile-inventory/`、`.locks/`、state-index 或 quarantine；quarantine
绝不由本服务删除。它不为 tar archive 增加透明读取层，也不会自动恢复。node-22 保持 DB-free：
retention 命令与 service 不连接任何数据库、也不提交 Slurm 作业；不得运行 `uv sync`、裸 `uv run`
或任何重建环境的命令。scheduler 自身的业务提交不在本服务范围内。

当前生效的四个 retention key 为：

|Key|最终值|
|---|---|
|`NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED`|`true`|
|`NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN`|`false`|
|`NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS`|`90`|
|`NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT`|既有 0600 envfile 中已批准的绝对 journal-archive root；其非公开路径不记录于此|

`DAYS=90` 是本次批准的生产窗口，不是代码硬最小值；未经授权不得缩短。现场的
`/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env` 是既有 0600 envfile；保留全部
现场 key，只可原子地修改上述四个 key，且不得以模板覆盖它。service 必须使用固定解释器和 envfile：

```text
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/NWM/scripts/node22_scheduler_journal_retention.py
```

已安装的 user unit 为 `~/.config/systemd/user/nhms-scheduler-journal-retention.service` 与
`~/.config/systemd/user/nhms-scheduler-journal-retention.timer`。timer 按 `*-*-* 04:45:00 UTC`
调度，`RandomizedDelaySec=15m`、`Persistent=true`；没有 scheduler-wide idle 条件。每个 cycle
仍由既有非阻塞 flock 串行，锁忙即跳过。最近 scheduler passes 均 `planning_only` 且 Slurm queue
为空，但 scheduler service 仍须运行以发布稳定 snapshots，不能把 queue 空误判为可以绕过 frontier
或停止服务。

初始交付状态曾为 `ENABLED=false`、`DRY_RUN=true` 且不安装/enable unit；该默认关闭背景是合同的一部分，
现已由 #2119 完成启用，不能再作为当前生产状态引用。

#### 查看 timer 与 receipt

每次调用都会在 `<NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT>/retention/` 写入有界 JSON receipt。先确认
unit 与 timer 状态，再找最新 receipt；不要以 receipt 文件数判断一次运行是否发生。

```bash
systemctl --user status nhms-scheduler-journal-retention.service \
  nhms-scheduler-journal-retention.timer
systemctl --user list-timers nhms-scheduler-journal-retention.timer
journalctl --user -u nhms-scheduler-journal-retention.service --since '24 hours ago'
# 只读出已批准的 archive root；不要 source 整个 envfile。
NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=$(
  awk -F= '/^NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=/{print substr($0, index($0,$2)); exit}' \
    /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
)
find "${NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT:?set approved archive root}/retention" \
  -maxdepth 1 -type f -name '*.json' -print
```

先读 `preflight_blockers`（必须为空）、`frontier`（必须 fresh 且 `status=ok`）和 `discovery`
（必须完整、非 `blocked`），再看每个 cycle 的 `status` / `reason`：

- `planned`：dry-run 已识别完整成员与字节数，未创建 archive、未删热文件；
- `archived`：仅在 archive/manifest 验证后，才删除 manifest 绑定的热成员；
- `live_row`、`pipeline_frontier_exempt`、`in_flight`：分别是可恢复 scheduler 行、当前
  frontier 窗口和 writer 锁争用，均为保留而非故障；
- `blocked`：前置窗口、frontier、发现、成员完整性、archive 冲突或工具验证未能证明安全；
  修复根因后重跑，绝不绕过。已发布 archive 后的部分删除可安全重跑；冲突 archive 必须人工比较
  manifest/digest，绝不覆盖。

#### 回滚与恢复

**配置回滚（已记录、未在最终激活后执行）**：先 `disable --now` timer 并 stop service；再用固定
`.venv/bin/python` 对既有 0600 envfile 做 fail-closed 原子两键替换（不 source 该 envfile）；grep
核对四键后才 `start` service 做一次 dry-run 并核验 receipt。下列命令都是可执行步骤。本段只记录回滚程序；
最终激活后并未执行它。

```bash
set -eu
cd /scratch/frd_muziyao/NWM
systemctl --user disable --now nhms-scheduler-journal-retention.timer
systemctl --user stop nhms-scheduler-journal-retention.service
/scratch/frd_muziyao/NWM/.venv/bin/python - <<'PY'
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

REPO = Path("/scratch/frd_muziyao/NWM")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from packages.common.safe_fs import (
    atomic_write_bytes_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)

ENV = REPO / "infra" / "env" / "compute.scheduler-dbfree.env"
MAX_BYTES = 65536
ENABLED = "NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED"
DRY_RUN = "NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN"
DAYS = "NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS"
ARCHIVE = "NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT"
KEYS = (ENABLED, DRY_RUN, DAYS, ARCHIVE)


def fail(message: str) -> None:
    raise SystemExit(message)


def line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


info = stat_no_follow(ENV, containment_root=REPO)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
    fail("envfile must be a regular non-symlink file")
if (info.st_mode & 0o777) != 0o600:
    fail("envfile mode must be 0600")
if info.st_uid != os.getuid():
    fail("envfile owner must be the current uid")

raw = read_bytes_limited_no_follow(ENV, max_bytes=MAX_BYTES, containment_root=REPO)
if len(raw) > MAX_BYTES:
    fail("envfile exceeds 65536-byte ceiling")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as error:
    fail(f"envfile is not strict utf-8: {error}")

found = {key: 0 for key in KEYS}
out: list[str] = []
for line in text.splitlines(keepends=True):
    ending = line_ending(line)
    payload = line[: len(line) - len(ending)]
    matched = False
    for key in KEYS:
        prefix = f"{key}="
        if not payload.startswith(prefix):
            continue
        found[key] += 1
        value = payload[len(prefix) :]
        if key == ENABLED:
            if value not in {"true", "false"}:
                fail(f"{key} must be true or false")
            out.append(f"{prefix}false{ending}")
        elif key == DRY_RUN:
            if value not in {"true", "false"}:
                fail(f"{key} must be true or false")
            out.append(f"{prefix}true{ending}")
        elif key == DAYS:
            if value != "90":
                fail(f"{key} must remain 90")
            out.append(line)
        else:
            if not value:
                fail(f"{key} must be non-empty")
            out.append(line)
        matched = True
        break
    if not matched:
        out.append(line)

for key, count in found.items():
    if count != 1:
        fail(f"{key} must occur exactly once, found {count}")

atomic_write_bytes_no_follow(
    ENV,
    "".join(out).encode("utf-8"),
    containment_root=REPO,
    mode=0o600,
    require_durable_replace=True,
)
after = stat_no_follow(ENV, containment_root=REPO)
if not stat.S_ISREG(after.st_mode) or (after.st_mode & 0o777) != 0o600:
    fail("replaced envfile must remain a regular 0600 file")
if after.st_uid != os.getuid():
    fail("replaced envfile owner must remain the current uid")
print("retention env rolled back: ENABLED=false DRY_RUN=true DAYS=90")
PY
envfile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED=false$' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN=true$' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS=90$' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED=' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN=' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS=' "$envfile")" = 1
test "$(grep -c '^NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=' "$envfile")" = 1
NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=$(
  awk -F= '/^NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=/{print substr($0, index($0,$2)); exit}' \
    "$envfile"
)
systemctl --user start nhms-scheduler-journal-retention.service
systemctl --user status nhms-scheduler-journal-retention.service
find "${NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT:?set approved archive root}/retention" \
  -maxdepth 1 -type f -name '*.json' -print
```

`atomic_write_bytes_no_follow(..., mode=0o600, require_durable_replace=True)` 在同目录以
`O_CREAT|O_EXCL|O_NOFOLLOW` 写 temp、`fchmod 0600`（创建者为当前 uid，故 owner 与当前进程相同）、
fsync 文件与父目录后 `os.replace`；失败则删除 temp。不要删除任何 archive 或 quarantine。激活期间的失败
路径曾多次证明其回滚动作；这不等同于声称最终激活后的 rollback dry-run 已执行。

恢复始终是离线、单 cycle 操作：先停止或排除目标 cycle 的 writer，保存 archive 前的
`query_pipeline_jobs_by_cycle` 输出，然后验证、stage 并 no-clobber restore，最后比较查询：

```bash
# 写入隔离 staging 根；目标热文件已存在、路径逃逸、symlink 或 digest 不同都会拒绝。
/scratch/frd_muziyao/NWM/.venv/bin/python \
  /scratch/frd_muziyao/NWM/scripts/node22_scheduler_journal_retention.py verify-restore \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --archive-root "$NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT" \
  --source-id gfs --cycle 2026050100 \
  --stage-root /scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal-restore-stage

/scratch/frd_muziyao/NWM/.venv/bin/python - <<'PY'
import json, os
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from workers.data_adapters.base import parse_cycle_time
root = os.environ["NHMS_SCHEDULER_JOURNAL_ROOT"]
repo = FileOrchestrationJournalRepository(root)
print(json.dumps(repo.query_pipeline_jobs_by_cycle("gfs_2026050100"), sort_keys=True))
PY
```

将最后的 JSON 与 archive 前捕获的 cycle query 作逐字节或结构化等价比对，并确认每个 restored member
的 manifest SHA-256。确认 `pipeline-jobs/` 与 state-index 的前后 checksum 不变后，才重新允许
writer。恢复不编辑 direct records、inventory 或 state-index。
