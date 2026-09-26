**分册：DB-free file-provider 稳态刷新**

本页是当前生产值守手册的 §3.1.2 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

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
  带 `status` / `missing_required_files` / `invalid_required_files` /
  `unreadable_required_files` 四键 = 包变 invalid 被 skip（键值就是
  publisher 的 not-publishable 判据；`unreadable_required_files` 是「必需文件
  匹配到但读不出」的第三态，#1552/#1553：此时前两个 list 可能都为空，不能据此
  当成「无因的 partial」）；无这四键 = model 目录真没了。
  合法下线：非 direct-grid 拓扑走下面的 **retire declaration 恢复顺序**；
  direct-grid 生产（`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`）不走 declaration，见下面的
  **拓扑围栏**与 §7.2。不打算下线就修包后重跑。
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

> **拓扑围栏（#1720，先读再往下做）**：本节以下的 declaration 操作流程——手动 CLI
> 路径、retire declaration 恢复顺序、systemd 路径——**只适用于
> `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID` 不为 true 的拓扑**。当前生产（direct-grid）
> 两个 env 都是 `true`：`scripts/scheduler_file_provider_refresh.py` 的
> `publish_registry()` 走 direct-grid 分支，
> `precommit_provider_generation(workspace, [], previous_models_snapshot)` 再重发
> `previous_models_snapshot`——prospective ≡ previous，`removed` / `package_changed`
> 恒为空，cutover 闸结构性看不到任何 model set 变更。于是：
>
> - **retire** entry 匹配不到任何 removal，按 rule 1（retire 的 `model_id` 必须在
>   `removed` 集合里）判无效——**下一次** refresh 就以 outcome=`failed`、reason=
>   `registry_cutover_declaration_invalid` 拒跑，不是等过期之后才拒。这条
>   **entry 级**拒绝（retire 不在 `removed` 里）只在真实 publish 上评估——
>   `_classify_registry()` 的 dry_run 分支走 id-only 分类后直接返回、不评估
>   removal——所以 `--dry-run` 预览看不到它，不能用它"先试一下"。反过来，
>   **文件级** declaration 加载失败（文件缺失 / 不可读 / 过期 / schema 不符）
>   在 `--dry-run` 下**照样拒**：`_registry_precommit_gate()` 无条件先加载
>   declaration、加载失败即按 `registry_cutover_declaration_invalid` 拒
>   （`scripts/scheduler_refresh/precommit_gate.py`），而 direct-grid 分支
>   在 dry-run return 之前就调用了 precommit（`scripts/scheduler_refresh/runner.py`）。
> - **replace** entry 没有 `package_changed` 可覆盖，永远不生效；它的 `generation`
>   一旦与 renewal 的 prospective 不符、或 declaration 过期 / 文件不可读，同样拖停
>   每日管线。
>
> direct-grid 生产上的换包 / 退役不走 declaration：率定切换按 §5.7.1（provision
> `M1′` + 直接发布合并 manifest），退役按 §7.2（手工删 manifest 行 + 后续 DB 侧
> deactivate）。direct-grid 生产上 `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH` 必须保持
> 未设置（删掉整行，不留空值），拒跑机制在 refresh 进程，要查的是 refresh 读 env 的
> 两个入口：
>
> - systemd timer 路径：`nhms-scheduler-file-provider-refresh.service` 的
>   EnvironmentFile `infra/env/compute.scheduler-provider-refresh.env`（wrapper
>   `scripts/scheduler_file_provider_refresh_once.sh` 按 allowlist 解析，模板
>   `infra/env/compute.scheduler-provider-refresh.env.example`）里不能有这一行；
> - 手动 CLI 路径：跑 refresh 的 shell 里不能 `export` 它。
>
> scheduler 侧 `infra/env/compute.scheduler-dbfree.env`（见下方 Consumer-side note）
> 在 direct-grid 生产上同样不设：§5.7.1 的克隆行让 §8 走 `warm_continue`，用不到
> declaration；而一旦设了、文件加载失败（缺失 / 过期 / 不合法），
> `services/orchestrator/scheduler_generation.py` 的 `evaluate_transition_decision()` 分支 (b) 在
> `warm_continue` 判定**之前**就把每个候选 block 为 `block_declaration_missing` /
> `block_declaration_stale`。

操作流程（手动 CLI 路径）：先看被拒 receipt -> 拷 generation / old/new checksum 到
declaration -> 提交 declaration 到 mode-0600 路径 ->
`export NHMS_REGISTRY_CUTOVER_DECLARATION_PATH=<path>` -> 重跑 refresh。
`effective_cycle_utc` 必须精确对齐 00:00 或 12:00 UTC，且落在
`[now-24h, now+168h]` 区间。`transition_mode` 有两个值：`replace`（换包，
`new_checksum` 必须是 64 位 hex）和 `retire`（下线一行，`new_checksum` 必须显式写
`null`——缺键与 `null` 语义不同，schema 两条 if/then 钉死这个配对）。

**退役一个 model（retire declaration 恢复顺序，#1433；仅非 direct-grid 拓扑的首选路径，
direct-grid 生产见上面的拓扑围栏与 §7.2）**：在非 direct-grid 拓扑上这是
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
   `CUTOVER_PAST_TOLERANCE=24h`，见 `scripts/scheduler_refresh/constants.py` 与
   `scripts/scheduler_refresh/cutover_declaration.py`）、文件被删除或轮转掉
   （同一模块里的 `OSError` 分支直接判 invalid）、
   或 declaration 的 `generation` 相对新的 prospective 过期
   （`scripts/scheduler_refresh/precommit_gate.py` 的 `_prospective_registry_generation` 比对），
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
下一次 pass 时才重新加载。**仅在非 direct-grid 拓扑**（`NHMS_SCHEDULER_REQUIRE_DIRECT_GRID`
不为 true）上：node-22 systemd EnvironmentFile
`compute.scheduler-dbfree.env` 里必须在 declared cutover 期间显式设置这个 env，
declared-cutover 候选的 §8 gating 才能放行；未设置 = declaration 缺席 -> 每个
declared-cutover 候选会 block 为 `registry_cutover_declaration_missing`。
direct-grid 生产（当前）不用 declaration：换包走 §5.7.1，克隆行让 §8 走
`warm_continue`，该分支不读 declaration；scheduler 与 refresh 两侧 env 都保持未设置，
理由见 §3.1.2 的拓扑围栏（#1720）。

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

标准做法是一对**必须成对完成**的动作，缺后半条就是事故：

1. `systemctl --user stop nhms-scheduler-file-provider-refresh.timer`；
2. 重跑上面的成对 status，确认 service 也已退出；
3. 运行 manual publisher CLI；
4. **`systemctl --user start nhms-scheduler-file-provider-refresh.timer`**——这一步
   不是收尾礼节，是本窗口的第二半；
5. `systemctl --user list-timers nhms-scheduler-file-provider-refresh.timer --no-pager`
   核对下次 tick 已排上（`NEXT` 必须是具体时刻，不是 `-`）。

**漏掉第 4 步的终态有名字**：timer 停在 `UnitFileState=enabled` +
`ActiveState=inactive`、`list-timers` 的 `NEXT` 为 `-`、`status` 显示
`Trigger: n/a`。`enabled` 依旧绿着，但没有任何 tick 会发生——这就是 2026-08-28 到
09-08 那 11 天的形状，见本节后面的"`enabled` + `inactive` 是失败态（#2041 / #2146）"。

若 status 显示 service 正在跑，等它自然结束，不要 kill——
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
# 按 install-state/refresh.before 恢复 refresh 两个 unit 并读回确认；再读回断言 scheduler units 未变。
```

**installer 的失败路径与恢复基线（#2294）**：

- 脚本以 `set -Eeuo pipefail` 运行。`--install` / `--enable` 在第一次 mutation 之后的任何失败
  （函数内部、命令替换内部的失败也算）都只在主 shell 里跑**一次**恢复：每一步都会尝试，
  前一步失败不会截断后一步；最后必跑两次读回，然后非零退出、**不打印**状态行。
- 读回按 unit 类型：refresh timer 比 `UnitFileState` 和 `is-active`，refresh service（timer
  驱动的 oneshot）只比 `UnitFileState`；compute scheduler 同理，timer 两列都比，service 只比
  `UnitFileState`（它每 5 分钟自己翻一次 `is-active`，比它只会误报）。
- 恢复目标：`--rollback` 和失败的 `--install` 都回到 `refresh.before` 记录的基线——失败的重装
  会被回退成 rollback 之后的样子，而不是停在半更新；失败的 `--enable` 回到本次调用开始时的
  状态，不会把一条已经 armed 的 lane 解除。
- `--rollback` 只有在读回确认两件事都成立后才打印 `{"status":"rolled_back",...}`：refresh
  units 等于基线，compute scheduler 未变。`refresh.before` 缺失或格式不对（不是恰好两行、
  每行不是恰好两个 tab 分隔的非空字段）时，`--rollback` 在任何 mutation 之前就失败。
- **入口闸门先于一切（原有行为，未改）**：三个动作（`--install` / `--enable` / `--rollback`）
  在读 compute scheduler、做任何 mutation 之前，都要求 refresh service 的 `is-active` 恰为
  `inactive`；`active`、`activating`、`failed` 等一律非零退出、不打印状态行、不做 mutation
  （stderr 无 `refusing --install` 行）。所以 `failed` 的 refresh service 必须先
  `systemctl --user reset-failed nhms-scheduler-file-provider-refresh.service`，才能跑**任何**
  installer 动作，包括 `--rollback`；在跑的 tick 等它结束再来。
- **armed 时拒绝 `--install`**：过了入口闸门之后，refresh timer 或 service 的 `is-enabled` 不在
  `disabled` / `static` / `not-found` / 空，或 `is-active` 不在 `inactive` / `failed` 时，
  `--install` 在 stderr 打印 `refusing --install: <unit> is <enabled>/<active>; run --rollback first`
  并退出 1，不做任何 mutation。这里的 `failed` 只对 refresh **timer** 有实际意义——service
  的 `failed` 已经被入口闸门拦下，不会被当成解除态。处置：先 `--rollback` 再装；没有基线、
  timer 是手工 arm 的主机，先手工 `systemctl --user disable --now nhms-scheduler-file-provider-refresh.timer` 再装。
  `--install` 的成功同样以读回为准：`disable --now` 之后两个 refresh unit 都读回为解除态才打印
  `{"status":"installed_stopped",...}`，否则走上面的恢复。
- **基线只写一次**：`refresh.before` 不存在时，首次 `--install` 先记录两个 unit 的 `.before`
  文件，最后经 `refresh.before.tmp` + `mv` 写出 `refresh.before`——它的存在就表示基线完整，
  中途被打断的首装不留下 `refresh.before`，下一次 `--install` 会整套重新记录。`refresh.before`
  已存在时，后续 `--install` 和 `--rollback` 都**不改写、不删除**它和两个 `.before` 文件，所以
  连装两次再回滚恢复的是第一次安装之前的状态，重复 `--rollback` 也是幂等的。`--install` 在第一次
  mutation 之前就按 `--rollback` 的同一套严格规则解析已存在的 `refresh.before`：格式不对（或它是符号链接）就非零
  退出、不打印状态行、不做任何 mutation，基线原样留着——不会在一份 `--rollback` 会拒绝的基线上
  装出一条 lane。处置：确认主机状态后按下面的"重置基线"删掉整套基线再装。
  **重置基线**：一次成功的 `--rollback` 之后，删掉 `refresh.before` 和
  `nhms-scheduler-file-provider-refresh.{service,timer}.before`。
- **`scheduler.before` 已废弃**：compute scheduler 的比较基线改为每次调用开始时在内存里捕获，
  不读、也不写任何跨调用的文件。node-22 上遗留的 31 字节 `scheduler.before` 被忽略，原样留着即可。
- **node-22 现状**：`install-state/` 里的基线写于 2026-07-15（`refresh.before` =
  `disabled\tinactive\nstatic\tinactive\n`），是一次**重装快照**：当时 unit 已经存在且已解除。
  对它 `--rollback` 恢复的是 7 月的 unit 文件、disabled 状态，而不是"没有这条 lane"。那是解除态，
  所以安全。
- **维护窗口内的演练顺序**（#1831 窗口前不得在 node-22 active checkout 上 pull）：timer 现在是
  armed。issue 原文的演练从 `--install` 开始：在 D4 下它会被拒绝，照原文跑完还会让生产停在
  解除态。所以演练顺序是：
  1. `git status --porcelain` → `git pull --ff-only`（仅窗口内）。
  2. 记录 before-state：`od -c` `install-state/refresh.before` 和 `scheduler.before`（后者仅供参考，
     installer 不读它），四个 unit 的 `systemctl --user show -p UnitFileState -p ActiveState`。
  3. **Pre-flight**：`systemctl --user is-active nhms-scheduler-file-provider-refresh.service` 必须是
     `inactive`。02:15-04:15Z 之外没有 tick 到期；若是 `failed`，先
     `systemctl --user reset-failed nhms-scheduler-file-provider-refresh.service`。每个动作的入口闸门
     只接受 `inactive`，这是原有行为，未改。
  4. `--rollback`：refresh units 读回等于基线（disabled/inactive、static/inactive），scheduler 未变。
  5. `--install`：基线保留（`refresh.before` 与第 2 步字节一致），lane 为 `installed_stopped`。
  6. **Manual refresh**：`scripts/scheduler_file_provider_refresh_once.sh`，然后
     `jq -r .outcome /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json`
     必须输出 `published`。`--enable` 的 `validate_current_receipt` 要求一份 `published` receipt，
     且其中各 provider 的 `after_sha256` 仍与磁盘一致；而每 5 分钟一次的 compute copyback 会在每次
     nightly refresh 之后挪动 `index-last.json`，所以必须紧挨着 `--enable` 现跑一次。
  7. `--enable` → `enabled_active`。**恢复**：若失败，再跑一次 manual refresh（第 6 步），然后再
     `--enable`。
  8. 失败路径演练：armed 状态下再跑 `--install`，必须拒绝且无 mutation。
  9. lane 保持 armed（第 7 步已 `--enable`），记录 after-state。
  10. 探针 installer：`--rollback` → `--install` → `--enable`，并记录其状态。

  每一步的 stdout、stderr、rc，以及前后两份 `show` 输出，都写进 `docs/runbooks/receipts/` 下的
  receipt。

##### `enabled` + `inactive` 是失败态（#2041 / #2146）

**`is-enabled` 不是活性证据。** 稳态核对必须把下面四列当作**互相独立**的信号分别读，
任何一列不合格都是失败，不能用另一列的绿色替代：

| 列 | 命令 | 合格 | 不合格的样子 |
| --- | --- | --- | --- |
| `is-enabled` | `systemctl --user is-enabled nhms-scheduler-file-provider-refresh.timer` | `enabled` | `disabled` / `masked` / `static` / `not-found`——重启或 reload 后不会自己回来 |
| `is-active` | `systemctl --user is-active nhms-scheduler-file-provider-refresh.timer` | `active`（`SubState=waiting`） | `inactive` / `failed`——**不管 `is-enabled` 是什么**，都没有 tick 会发生 |
| `list-timers` 的 `NEXT` | `systemctl --user list-timers nhms-scheduler-file-provider-refresh.timer --no-pager` | 具体时刻，且在 36 小时以内 | `-`（等价 `status` 里的 `Trigger: n/a`），或远得离谱 |
| manifest 年龄 | 见下方 `jq` 命令（含管道符，无法放进表格单元格） | 距今 < 120 小时 | ≥ 120 小时是预警，≥ 168 小时 consumer 已经 fail closed；`jq` 压根解不出（receipt 缺失/坏掉/`providers` 为空）**同样是不合格**——探针会先回退到 `history/`，两路都解不出才报 `manifest_unavailable`，看探针 receipt 的 `manifest_source` 字段定位 |

第四列的取值命令（表格单元格里的转义反斜杠会被 `jq` 当成语法错误，所以单独成块）。
取哪个字段看 receipt 的 `outcome`，与探针同一规则（见下文"manifest 年龄的取数顺序"）：

```bash
jq -r '.outcome as $o | .providers[] | select(.name == "registry")
  | if ($o == "published" or $o == "published_receipt_failed" or $o == "dry_run")
    then .after_generated_at else .before_generated_at end' \
  /scratch/frd_muziyao/nhms-prod/workspace/provider-refresh/receipts/latest.json
```

`replace_uncertain` receipt 的 `after_*`（#2297）：provider 回滚**已验证**（逐个 sha256 核对回到
发布前的字节）、只是另一条 lane 的结果未知而仍报 `replace_uncertain` 时，每个已回滚 provider 的
`after_sha256` / `after_schema_version` / `after_generated_at` / `after_payload_checksum` 描述的是
回滚后磁盘上的字节（等于它的 `before_*`，与 dry-run 同一替换；`entry_count` 仍是本次尝试的条数）。
回滚**未能验证**时，receipt 原样保留发布后的 `after_*`，那些字节可能已不在磁盘上——所以上面按
`outcome` 取字段的规则保留不变。

`enabled` + `inactive` + `NEXT=-` 是一个**看起来全绿、实际永不触发**的组合：
`UnitFileState=enabled` 只说明 unit 在 `default.target` 的 wants 里，不说明 timer
此刻装载着。`Persistent=false` 让这个洞更深——timer 停着的那段时间里错过的
calendar event **不会**在重新 `start` 时补跑，所以既没有失败日志，也没有追赶。

**2026-08-28 -> 2026-09-08 实例（反面教材）**：timer 在 `2026-08-28 00:11:18 CST`
进入 inactive（`InactiveEnterTimestamp`），`UnitFileState` 全程 `enabled`。
没有任何组件报警。11 天后 manifest 越过 168 小时 consumer 上限，compute 先
`file_manifest_stale`、随即 `db_free_registry_blocked`，journal retention 报
`frontier_block_missing`。运维在 `2026-09-08 13:47:49 CST` 手工恢复
（`ActiveEnterTimestamp`）。**该一次性恢复已经完成**，timer 现为 `enabled` +
`active`，不要再跑一遍 `--enable`：对健康 timer 重跑是无谓的生产 mutation。

##### refresh timer 健康探针（read-only，不自愈）

`scripts/node22_refresh_timer_health.py` 把上表四列读成四个独立字段、按固定优先级
定级、写一份 receipt、非 `ok` 一律非零退出。它**只**调用
`systemctl --user show` 与 `systemctl --user list-timers` 两个只读子命令，源码里
不存在任何 `enable`/`disable`/`start`/`stop`/`restart`/`daemon-reload`（有测试扫描源码
守着这一点），也不写 provider store 下的任何文件。**检测，不自愈**：修复动作永远是人做的。

判决**首个命中者胜出**，`ok` 是纯 `else`：

| 序 | verdict | 含义 |
| --- | --- | --- |
| 1 | `probe_failed` | **systemd** 查不动/报错，或 `ActiveState != active` 但 `InactiveEnterTimestamp` 为空/不可解析（dwell 算术无定义）。只管 systemd 这一路证据——manifest 读不出**不**走这条 |
| 2 | `manifest_expired` | manifest 年龄 ≥ 168 小时——consumer 已经 fail closed，比任何 timer 事实都严重 |
| 3 | `timer_stopped` | `ActiveState != active` 且已停超过 stopped-dwell。不带 `enabled` 谓词：`disabled` 且停着同样报这条 |
| 4 | `timer_not_enabled` | `UnitFileState` 不是 `enabled`。此刻恰好 active 也算——它熬不过一次 reload 或重启 |
| 5 | `timer_not_scheduled` | timer `active`，但 `NextElapseUSecRealtime` 为空、不可解析，或比 next-dwell 还远 |
| 6 | `manifest_stale` | manifest 年龄 ≥ manifest-age 阈值（且 < 168）。两个 manifest 比较都是"等于即命中" |
| 7 | `manifest_unavailable` | `latest.json` 与 history 回退都解不出 manifest 年龄。**排在 `ok` 之前、所有 timer 判决之后**：解不出的 manifest 不能说明 timer 的任何事，所以不许遮住 `timer_stopped`/`timer_not_enabled`/`timer_not_scheduled`；但它仍是缺失的证据，所以也到不了 `ok` |
| 8 | `ok` | 以上都不命中 |

两路证据**独立定级**：systemd 读不动是第 1 条，manifest 解不出是第 7 条，互不遮蔽。
年龄解不出时第 2、6 两条 manifest 比较是**跳过**，不是按 0 或按无穷大代入。

三个阈值均可用 env 覆盖，**三个都**在 config 阶段做区间检查。口径是"留够余量"，
不是"小于 168"：`< 168` 曾经收下 167——只给 consumer 的 168 小时预算留 0.6% 当预警。
实测 `STOPPED_DWELL_HOURS=167` 配 `MAX_MANIFEST_AGE_HOURS=167`，能把 08-28 那副几何
判成 `ok`、退出 0，整整 166 小时。现在的规则是：

- 三个阈值一律只收 `1..144` 小时——即在 consumer 的 168 小时硬界之下至少留 **24 小时**
  余量。24 小时是一个完整的 refresh cadence：报警必须在"还来得及跑完一次计划内
  refresh"之前响，否则报了也救不回来。
- `STOPPED_DWELL_HOURS` 再额外封顶 **24 小时**（一个 cadence）。timer 空转超过自己的
  周期就已经漏掉了一个 tick，不管那个手工发布窗口本来是为什么开的。

越界值在**采集任何证据之前**就被拒（退出 2，不写 receipt），不会变成一个判决。

| env | 默认 | 上限 |
| --- | --- | --- |
| `NHMS_REFRESH_HEALTH_MAX_NEXT_DWELL_HOURS` | 36 | 144 |
| `NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS` | 120 | 144 |
| `NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS` | 6 | 24 |

service unit **故意不带 `EnvironmentFile=`**：默认值就是 node-22 的生产值，多一个
未入库的 env 文件就多一条能悄悄放松告警阈值的路径。真要改阈值，用 drop-in 并把
drop-in 记回本节：

```bash
mkdir -p ~/.config/systemd/user/nhms-node22-refresh-timer-health.service.d
printf '[Service]\nEnvironment=NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS=96\n' \
  > ~/.config/systemd/user/nhms-node22-refresh-timer-health.service.d/10-thresholds.conf
systemctl --user daemon-reload
```

stopped-dwell 存在的原因就是上面 #1104 那个 stop/start 窗口：窗口期间 timer 的形状
正好是探针要抓的 `enabled` + `inactive`，而 refresh oneshot 自己的
`TimeoutStartSec=7200` 意味着合法窗口可以跑满两小时。6 小时是它的三倍，所以一次
最长的合法手工发布也不会吵；而 08-28 那种停了六天的，过 dwell 后第一个 tick 就报。
dwell 之内不是"静默放行"——其余信号照常定级，manifest 同时 stale 一样非零。

Receipt 落在 `/scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts/latest.json`，
目录 0700、文件 0600。字段是**封闭集合**，逐个列在下面——有测试拿 `build_receipt` 的
实际输出对着这份清单核，多一个字段或少一个字段都会红。**不回显任何路径或其它 env 值**：

`schema_version`、`generated_at`、`verdict`、`unit`、`unit_file_state`、`active_state`、
`sub_state`、`inactive_enter_timestamp`、`next_elapse`、`last_trigger`、
`manifest_age_hours`、`manifest_source`、`max_next_dwell_hours`、
`max_manifest_age_hours`、`stopped_dwell_hours`。

`manifest_source` 是三形状的封闭值，记录到底是哪一路证据回答了 manifest 年龄：

| 值 | 含义 |
| --- | --- |
| `latest` | 配置的 `latest.json` 直接解出了 registry 的生成时间（取哪个字段按下文的 `outcome` 规则） |
| `history:<文件名>` | `latest.json` 解不出，由 `history/` 里这份 receipt 回答（只是文件名，不是路径） |
| `unavailable` | 两路都解不出，对应 `manifest_unavailable` 判决 |

判决是 `manifest_unavailable` 时，这个字段是第一个要看的。

写 receipt 本身是 **write-temp + fsync + `os.replace`**：写失败既不会静默截断，
也不会把上一份好 receipt 搞没。写不下去时进程非零退出，而 verdict 与证据错误
在尝试写之前就已经进了 journal——journal 就是告警通道，receipt 写不成不能顺手把判决吃掉。

**本条告警只在 node-22 本机**：不合格判决让
`nhms-node22-refresh-timer-health.service` 进入 `failed`
（`systemctl --user list-units --failed` 可见）并留下上面那份 receipt。没有邮件、
没有 paging，今天也没有任何东西在轮询 node-22 的 failed unit。这是**已知限制**，
不是遗漏：它把"168 小时悬崖前完全不可见"换成了"任何上机的人几小时内可发现"，
off-host 路由是另一条有自己认证与投递面的告警链路，另案处理。

**manifest 年龄的取数顺序（有界 history 回退）**：refresh 的 `latest.json` 被**每一次**
运行覆写，包括在拼出 provider 列表之前就失败、`providers` 为空数组的那种。
所以探针先读 `latest.json`，读不出就回退到它旁边的 `history/`：

- 只考虑 runner 自己的文件名形状 `refresh_<YYYYmmddTHHMMSSZ>_<uuid12>.json`；
- 因为前缀是定宽 UTC 时间戳，**按文件名倒序**就是时间倒序——不解析时间戳，
  也**不信 `mtime`**（`mtime` 是文件系统的属性，不是运行的属性，rsync/恢复都会改它）；
- 目录列举封顶 200 条（高于 runner 自己的 `MAX_HISTORY = 32`），最多打开最新的 10 份，
  每份照样 `O_NOFOLLOW` + 尺寸封顶；
- **按 receipt 的 `outcome` 取字段**，`latest.json` 与每份 history 同一规则：`outcome` 是
  `published`、`published_receipt_failed`、`dry_run` 时取 `registry.after_generated_at`；
  其余 outcome（`replace_uncertain`、`restored_previous`、`failed`、`already_running`）
  取 `registry.before_generated_at`，该字段缺失或不可解析时这份 receipt 解不出，继续往下找。
  原因：回滚**未能验证**的 `replace_uncertain` receipt 仍可能带着一个已被回滚掉的、更新的
  `after_generated_at`（#2297 起，回滚已验证的 provider 的 `after_*` 已改写为磁盘上的字节），
  而它的 `before_generated_at` 不会比磁盘上的 manifest 更新。

效果：一次失败的演练不再产生告警，上一份成功 receipt 还能回答；
而两路都解不出时才报 `manifest_unavailable`（`manifest_source=unavailable`）。
处置是让 refresh 重新跑出一份成功 receipt，不是调探针。
注意这条判决排在所有 timer 判决**之后**：以前它在第 1 位，一份失败 receipt
能把真正停掉的 timer 遮一整天。

安装与回滚（只动探针自己的两个 unit）。脚本在每个动作前后断言四个受保护 unit
未变，但**按 unit 类型分别比**：

| unit | 比什么 | 为什么 |
| --- | --- | --- |
| `nhms-compute-scheduler.timer`、`nhms-scheduler-file-provider-refresh.timer` | `UnitFileState` **和** `is-active` | timer 的两个字段在安装期间都该是静止的 |
| `nhms-compute-scheduler.service`、`nhms-scheduler-file-provider-refresh.service` | 只比 `UnitFileState` | 这两个是 timer 驱动的 oneshot，`is-active` 本就会自己翻（compute scheduler 每 5 分钟一次，refresh 在 02:15-04:15Z 窗口内）。比它会在没人动过的 unit 上误报，而误报会触发 abort + 回退，把 arming 变成重试循环 |

探针 installer 用 `set -Eeuo pipefail`：
没有 `-E`，顶层的 ERR trap 不会被函数体继承，断言在函数里挂掉时脚本只会
退 1 而 trap 根本不跑，回退等于不存在（有测试拿去掉 `-E` 的副本实跑，探针 unit 被留在原地）。


```bash
scripts/install_node22_refresh_timer_health.sh --install    # 落 unit 文件，保持停用，读回确认
scripts/install_node22_refresh_timer_health.sh --enable     # 装载 hourly timer
systemctl --user list-timers nhms-node22-refresh-timer-health.timer --no-pager
scripts/install_node22_refresh_timer_health.sh --rollback   # 停用并复位到调用前状态，读回确认
```

"停用"指不会再触发，不是文件不在。`--rollback` 先 `disable --now` 探针 timer，再把两个
unit 文件复位成恢复基线里记录的样子——**第一次** `--install` 之前的内容（那时没有就删掉），
然后 `daemon-reload`；停用在复位文件之前，所以回滚**从不重新 arm**。

恢复基线只记录一次（#2294）：首次 `--install` 先存两个 unit 的 `.before` 文件，再经
`install.baseline.tmp` + `mv` 写出标记文件 `install.baseline`；标记在时后续 `--install` 保留这份
基线，所以连装两次再回滚，恢复的是第一次安装之前的文件；标记不在（首装被打断，或旧版本留下的
状态）时重新记录。`protected.before` 仍是每次调用各自捕获、不跨调用。探针 timer 或 service
仍处于 enabled/active 时 `--install` 直接拒绝（stderr `refusing --install: ...; run --rollback first`，
退出 1），而且拒绝发生在 `protected.before` 捕获之前，state root 与 unit 目录里的文件一个字节都不变。
node-22 旧状态没有这个标记：探针 timer 是 armed 的，所以 `--install` 会先要求 `--rollback`；那次
回滚会删掉探针 unit（旧状态没有 `.before` 文件），这是正确的，下一次 `--install` 就把"没有探针
unit"记为基线。已安装但未 arm 的旧主机，第一次新 `--install` 之前先跑一次 `--rollback`，否则已
安装的文件会被记成基线。

`--install` 与 `--rollback` 的成功都**以读回为准，不以 systemctl 调用是否报错为准**：
`--install` 在 `disable --now` 之后、`--rollback` 在 `daemon-reload` 之后，脚本对探针
timer 和 service **各自独立**读一次 `is-enabled` / `is-active`，只有两者都落在下表里才打印
`{"status":"installed_stopped",...}` / `{"status":"rolled_back",...}` 并退出 0；否则非零
退出、stderr 说明哪个 unit 处于什么状态、**不打印**状态行。`--install` 读回不过时 ERR trap
照常回退：unit 文件复位成恢复基线里的内容，探针 timer 不会因此被 arm。

| 读数 | 接受 | 为什么 |
| --- | --- | --- |
| `is-enabled` | `disabled`、`static`、`not-found`、空 | 没有东西会拉起它：`static` 是无 `[Install]` 的探针 service；unit 文件删掉后，视 systemd 版本 `is-enabled` 可能在 stdout 上什么都不输出（只在 stderr 报错），即"空"；`not-found` 是本 installer 自己给这种情况记的占位值，一并接受 |
| `is-active` | `inactive`、`failed` | 没在跑。`failed` 必须接受：探针在每个不合格判决上都会让 service 进入 `failed`，删掉文件后也要 `reset-failed` 才会清掉 |

其余一律拒绝，包括 `enabled`、`masked`、`active`、`activating`，以及 `is-active` 没有输出
（user manager 不可达）。所以正好撞上探针正在跑的那一刻会被拒——等这次 tick 跑完再执行一遍。

##### 探针自己的稳态核对（watchdog 不看自己）

探针只报"它跑了并且发现了什么"，报不了"它压根没跑"：timer 停在 `enabled` + `inactive`
永远不会进 `failed`，`systemctl --user list-units --failed` 干干净净，最后一份 receipt
永远读作 `verdict: "ok"`。`Persistent=true` **不**管这个——它只在 timer **转为 active**
（开机，或显式 `start`）时补跑漏掉的 tick，对一直停着的 timer 毫无作用。也就是说，
把 08-28 那个动作往上做一层（停掉探针 timer 而不重启），整条 lane 会悄悄退回改动前的
盲区，而所有表面依然全绿。

所以探针 timer 用和上面 refresh timer 完全一样的口径单独核对，两列都不合格即失败：

| 列 | 命令 | 合格 | 不合格的样子 |
| --- | --- | --- | --- |
| `list-timers` 的 `NEXT` | `systemctl --user list-timers nhms-node22-refresh-timer-health.timer --no-pager` | 具体时刻（hourly + 最多 5 分钟 jitter） | `-`——探针自己停了，从此不再产生任何判决 |
| 探针 receipt 的 `generated_at` | `jq -r .generated_at /scratch/frd_muziyao/nhms-prod/workspace/refresh-timer-health/receipts/latest.json` | 距今 < 2 小时（hourly cadence 的两倍） | 更旧——不管 `verdict` 字段写的是什么，那都是一份过期的判决 |

这条递归**没有**在代码里关掉：能关掉它的观察者必须不是 node-22 上的 user timer，
那是本次改动明确不做的 off-host 路由。本节欠的是把这件事说清楚，
外加一条运维能自己跑的检查命令。

预合并的只读验证不要在 `/scratch/frd_muziyao/NWM` 里 checkout 本分支——那棵树是
scheduler 与 refresh oneshot 的活执行根。把探针单文件 stage 到 checkout 之外，
用精确解释器跑，并显式指定 receipt 根，避免写进生产 workspace：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/tmp/<staged>/node22_refresh_timer_health.py \
  --health-receipt-root /scratch/frd_muziyao/tmp/<staged>/receipts --json
```

探针只用标准库，不 import 本仓任何包，正是为了能这样跑。

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
误用它会把 18 个现行流域一起隐藏（`core.basin_version` / `core.model_instance` /
file-registry manifest 三个 `active_flag` 各自的权威归属见 [`docs/spec/03_database_design.md` §5.2 / §5.5 的 `active_flag` 注记](../../spec/03_database_design.md#52-corebasin_version)）。

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
