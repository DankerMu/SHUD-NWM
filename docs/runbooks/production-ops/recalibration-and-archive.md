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
> declaration）。更糟的是它不必等过期：renewal 的 `removed` 恒为空，retire entry
> 按 rule 1 直接判无效，**下一次** refresh 就以 `registry_cutover_declaration_invalid`
> 拒跑（此后每一次都拒，直到删掉 env 行），把每日管线拖停；replace entry 也匹配不到
> 任何 `package_changed`，一旦过期或 generation 不符同样拒跑。§3.1.2 的
> 「retire declaration 恢复顺序」只适用于非 direct-grid 拓扑（见该节的拓扑围栏），
> 不适用于这里。

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
