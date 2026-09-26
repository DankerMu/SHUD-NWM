**分册：已知卡点：state-index / journal / scope gate**

本页是当前生产值守手册的 §8.8-§8.11 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

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

### 8.9 state-index checksum mismatch 与受控 repair

症状：调度器 pass 上**所有** candidate 被 `state_snapshot_index_checksum_mismatch`
挡住，`submitted=0`。单条 candidate 的 retryable 外观**不是**可操作的 retry——index 在
解析 `entries` 之前就 fail-closed，任何 retry/backfill 都不会前进。

两根角色（不是整文件镜像，禁止互相覆盖）：

- private/reference（默认 `OBJECT_STORE_ROOT`）：node-22 scratch 生命周期 index，
  `scheduler/state-index/index-last.json`。copyback 的 source。
- shared/destination（默认 `NHMS_OBJECT_STORE_COPYBACK_ROOT`）：调度器实际读的
  canonical index，同相对路径。可保留 shared 已归档对象的历史 entry。

**禁止**手改 JSON、重算 checksum、或把一份 index 整文件拷到另一份。两份 entry 集
本来就可以不同；整文件拷贝会丢生命周期历史或把归档对象复活进 shared。

enforce 前必须冻结所有会写这两份 index 的 writer：scheduler、state-save 作业、
copyback / replay、recalibration clone、provider-refresh。以 provider 属主
`frd_muziyao` 在 node-22 跑。archive / receipt 根必须事先存在、属主私有
（0700 目录）。

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/archives
NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/receipts
set +a
install -d -m 0700 \
  "$NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT" \
  "$NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT"

# 1) 两根 dry-run（默认：不加锁、不写 archive/receipt/index）
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  recompute-checksum --lane destination
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  remove-entry --state-id STATE_ID

# 2) 核对 stdout 预览：每 lane 的 checksum_valid、selector 命中、before/after
#    counts、untouched_reason。确认目标 identity 后才 enforce。
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  recompute-checksum --lane destination --enforce
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  remove-entry --state-id STATE_ID --enforce
```

操作口径：

- `recompute-checksum` 必须带 `--lane reference|destination`，只修那一 lane 的
  顶层 checksum；sibling 只读校验、字节不变。raw `entries` 语义和顺序必须保持。
- `remove-entry` 三选一：`--state-id`、`--run-id`、或完整
  `--model-id --source-id --valid-time`。默认两 lane 各恰好命中同一 identity
  key + `state_id`。零命中 / 多命中 / 跨 lane 不一致 → 拒绝，两份 index 都不写。
- 一 lane 缺目标时，必须先 dry-run 证明缺席，再带对应的
  `--allow-missing-reference` 或 `--allow-missing-destination`；不得两边同时缺。
- enforce 锁序与 copyback 相同：reference 然后 destination，**不按路径排序**。
  两份所需 archive（owner-private 0600、lane 标签、exact preimage）全部写完并
  回读后，才做第一次 CAS；remove 先 reference、后 destination，防止 destination
  失败后 stale reference 经 copyback 复活。
- 工具只重建顶层 checksum 以接纳 checksum-invalid 但结构完整的 payload；畸形
  JSON、不支持 schema、unsafe URI、重复 identity、字段非法、字节/entry/JSON
  复杂度超限一律拒绝。

退出码：

| exit | 含义 | 处置 |
|---|---|---|
| 0 | 完成成功；stdout 是 per-lane receipt | 存档 receipt 与 archive |
| 2 | **可证明零 index 写**（预检拒绝、archive 失败、第一次 CAS 之前的 provider 错） | 修输入/根/archive 后重跑 dry-run |
| 3 | 任一 lane 可能已替换：部分提交、committed-incomplete、commit-uncertain。stdout 仍打印已知 per-lane 摘要 | **不得**当拒绝。先按摘要看哪 lane 已变 |

source-first 部分恢复：reference 已去掉目标、destination 仍失败时，exit 3。
**不要**用 destination 回填 reference。从新的 dry-run 完成 destination 修复后再
解冻 writer。回滚是显式操作员决定，用各 lane 自己的 archive 字节，且必须先核对
当前 preimage；CLI 不会静默覆盖后来的合法发布。

### 8.10 NHMS_SCHEDULER_JOURNAL_ROOT 必须是 realpath

**约束**：`NHMS_SCHEDULER_JOURNAL_ROOT` 的**每一段路径组件**都必须是真实目录，
任何一段都不得是 symlink。file journal 的每一次读取都从文件系统锚点起、逐段
以 `O_NOFOLLOW` 打开（`packages/common/safe_fs.py` 的 `_open_directory_no_follow`），
所以祖先目录上任意一段 symlink 都会让每一次读取失败。

**症状（本 PR 之前）**——三条同时出现才是这个卡点：

- db-free preflight **PASS**：`_db_free_path_check` 只拒绝 leaf 或直接父目录是
  symlink 的情况，祖先层的 alias 它判不出来，所以配置能过 preflight。
- 每个 cycle 每一行都是 blocked row：跨 model 车道（`model_id=None`）报
  `file_journal_unsafe_scanned_entry`，model-scoped 车道报
  `file_journal_unreadable`。
- 诊断文本里**没有 symlink 这个词**：darwin 上是 `Path component is not a
  directory`（ENOTDIR），Linux 上是 ELOOP 文案。

**一行自检**（在 node-22 上执行，结果必须与配置值逐字相同）：

```bash
readlink -f "$NHMS_SCHEDULER_JOURNAL_ROOT"
```

**处置**：把 `NHMS_SCHEDULER_JOURNAL_ROOT` 改成 `readlink -f` 的输出，重启
`nhms-compute-scheduler.timer`。同一约束已写在
`infra/env/compute.scheduler-dbfree.env.example`、`infra/env/compute.example`
和 `infra/README.two-node-docker.md` 的该变量上方。

**本 PR 之后**：调度器在构造 file journal repository 时就验证该 root，不合规
直接以 `FILE_JOURNAL_INVALID_ROOT: <message>` 写 stderr 并 exit 1（oneshot unit
随之失败），不再退化成"每轮全是 blocked row"。**适用范围**：这条 exit 1 只覆盖
**能通过 db-free preflight** 的 root——上面那种祖先层 alias 正是这一类。leaf 本身
是 symlink、symlink loop、以及 root 根本不存在这三种形状，preflight 自己就先拦
下了：`from_env` 走 blocked 分支、`active_repository` 为 `None`，这一趟以
preflight 自己的 redacted blocker 结束，根本走不到 root 验证。message 里带 `readlink -f` 与
"real directory" 的处置指引，不含路径、traceback 或模块名；配置值与它来自哪个
设置项（调度器是 `NHMS_SCHEDULER_JOURNAL_ROOT`，demote CLI 是 `--journal-root`）
放在错误的结构化 details 里。operator 的 `demote-reserved-job` 与 8.11 的
`census-job-id-scope` 走的是同一个 seam，同码同文案；这两条 CLI 前面没有
preflight，所以上面"只覆盖能通过 preflight 的 root"这句只说调度器车道——CLI 上
**所有**不合规形状都由这个 seam 拒绝，包括自 #1944 起补上的空值与相对路径
（`details["error_type"]` 为 `RelativeJournalRoot`；`~` 无法展开时为
`UnexpandableJournalRoot`；否则它们会被 `safe_fs` 锚到当前工作目录上）。

**#1955 之后**：同一个 seam 覆盖了余下**七条** operator 写车道，不再只有
`demote-reserved-job` 与 `census-job-id-scope`——`recover-released-identity-blocked-reservation`、
`prepare-file-journal-rollback`、`launch-file-journal-rollback-writer`、
`complete-file-journal-rollforward`、`import_historical_scheduler_state`
（`migrate-scheduler-state` 经它继承），以及两个 node-22 脚本
`scripts/node22_manual_retry_failed_runs.py` 与
`scripts/ops/node22_repair_placeholder_hydro_uris.py`。验证发生在构造
repository **之前**，因此不合规的 root 一个字节都不会落盘：五条 CLI（上面四条
再加 `migrate-scheduler-state`）打一行
`FILE_JOURNAL_INVALID_ROOT: <message>` 到 stderr 并 exit 2（两个脚本同样
exit 2、不出 receipt），rollback 车道的
`.reconcile-inventory-rollback-execution.lock` 也因此不会出现在当前工作目录里。
rollback 锁路径由**已验证**的 root 直接派生、不再 `resolve()`，所以锁与
repository 读的永远是同一棵树。`migrate-scheduler-state` 自己不调 seam：它经
`import_historical_scheduler_state` 继承这条拒绝，并在 click 与 argparse 两个入口
渲染出同一行带 code 前缀的 typed line、同样 exit 2。
还有一条行为变更：三条 rollback 车道现在先验证再动手，所以**不存在**的 root 也是
`FILE_JOURNAL_INVALID_ROOT` 拒绝——`_rollback_execution_lock` 过去会替你把缺失的
root 建出来，现在不会建任何东西；rollback 前 root 必须已经存在（唯一仍会创建 root
的车道是 `import_historical_scheduler_state`）。

### 8.11 #1760 scope gate 与既存分叉 job_id 行

**什么叫分叉行**：一行 pipeline_job 的 `job_id` 里编码的 `(source, cycle)` 与
这一行自己的 `(source_id, cycle_time)` 不一致。判定口径就是写入侧 gate
`_require_job_id_cycle_scope` 本身（token `file_journal_job_id_scope_mismatch`，
field `job_id`）；census 只调用这一个谓词，不做第二套比较。

**先把 `$NHMS_SCHEDULER_JOURNAL_ROOT` 放进当前 shell**。下面的命令块是照抄粘贴
的，变量为空就等于没给 root。活值只有一处权威：调度器进程自己的环境（口径同
本文件第 3 节的 `/proc/$pid/environ` 自检）；oneshot 在 tick 之间是 inactive，
进程不在时退回 EnvironmentFile
`/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env`（模板见
`infra/env/compute.scheduler-dbfree.env.example:47`）。

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 '
pid=$(systemctl --user show -p MainPID --value nhms-compute-scheduler.service)
if [ "${pid:-0}" != "0" ]; then
  tr "\0" "\n" < /proc/$pid/environ | grep "^NHMS_SCHEDULER_JOURNAL_ROOT="
else
  grep "^NHMS_SCHEDULER_JOURNAL_ROOT=" /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
fi'
```

把取到的值 `export NHMS_SCHEDULER_JOURNAL_ROOT=...` 之后再跑普查。自 #1944 起，
空值或相对路径**不再**退化成"普查当前工作目录然后 exit 0"——journal-root seam
在任何读取之前就以 `FILE_JOURNAL_INVALID_ROOT`（exit 1）拒绝，见 8.10。

**普查命令**（node-22，只读，写作详见 8.10 的 root 前提）：

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli census-job-id-scope --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" --max-records 5000000
```

退出码：`0` = 没有分叉行；`2` = 发现分叉行（stdout 是 JSON receipt，
`divergent_rows` 逐条列出 `job_id`、`own_scope`、`job_id_scope`、
`anchor_present`、`flat_direct_present`、`reconcile_abort_trigger`）；`1` = typed
失败（stderr 是 `error_code: message` 或 `reason: field`，无 traceback）。receipt
是在写 `--output` **之前**就打到 stdout 的，所以 receipt 发出之后才发生的 typed
失败（例如 `--output` 不可写，`CENSUS_OUTPUT_UNWRITABLE`）会以 `1` 收场，
**即使这一趟真的查出了分叉行**；判分叉一律读 stdout receipt 里的 `exit_code`，
不要只看 `$?`。反过来，在 census **跑之前**就发生的 typed 失败——root 不合规
（`FILE_JOURNAL_INVALID_ROOT`）、`--output` 落在 root 里面
（`CENSUS_OUTPUT_INSIDE_JOURNAL_ROOT`）、`--output` 的 `~user` 展不开
（`CENSUS_OUTPUT_UNEXPANDABLE`，#1955 起；此前是裸 `RuntimeError` traceback）——
同样是 `1`，但 **stdout 是空的**，所以"没有 receipt"和"receipt 过期"永远不会混淆。

**为什么带 `--max-records 5000000`**：整树 replay 只有**一个** record 预算，默认
`MAX_FILE_JOURNAL_RECORDS = 100_000`，计费单位是：每一个 latest view 里
**物化出来的每一行 pipeline_job 各记一次**（一个 view 可以带很多行，这才是
预算的大头）＋每个 journal segment 的**每一行 JSONL 各记一次**（不分记录类型）。
direct 记录只在 `include_direct=True` 的 replay 上计费，而 census 传的是
`include_direct=False`，所以 direct 文件**一次都不计**。node-22 在 2026-09-02
用默认预算跑就以 exit 1 + `file_journal_record_limit_exceeded:
pipeline_job_records` 停住。补救就是 `--max-records N`；5000000 是实测跑通的
经验余量（headroom），不是算出来的数——receipt 不记录实际消耗量，所以只知道它
够用，不知道离预算还剩多少。

**`--max-files` 不是这个旋钮**——它管的是单次目录遍历发现的文件数（默认同为
100000，超限报的是 `file_journal_file_limit_exceeded`，anchor 列举那一处报的是
`file_journal_record_limit_exceeded: reconcile_inventory`），而实际文件数离
100,000 还很远；调它不会让 record 预算变宽。预算跳闸是 fail-loud，不写任何字节，抬预算是唯一支持的处置，
不存在"跳过"这个选项。

**实测记录**（PR #1951，worktree SHA `de33bd87`，2026-09-02 09:46 UTC，
node-22 detached worktree + `/scratch/frd_muziyao/NWM/.venv/bin/python -m`）：

- 第 1 趟按默认预算跑 → exit 1，`file_journal_record_limit_exceeded:
  pipeline_job_records`，无 traceback，零写入。
- 第 2 趟加 `--max-records 5000000` → **exit 0**，`divergent_total` 0，
  `reconcile_abort_triggers` 0；per-surface：flat direct 5,125 行 / by-cycle
  direct 9,362 行 / journal replay 14,922 行（5,998 latest view + 275 segment）/
  reconcile-inventory 3 个 anchor、residue 0 / `active_reconcile` 不存在。
- `rows` 的口径按 surface 不同：`journal_replay.rows` 数的是 replay 去重之后的
  **唯一 `job_id` 个数**（所以 14,922 可以大于它读的 6,273 个文件——一个 latest
  view 里可以有很多行），其余 surface 的 `rows` 数的是**读进来的文件个数**
  （一个文件一行；`reconcile_inventory.rows` 是 anchor 个数，不含 residue）。
  receipt schema 不变。
- receipt 与 transcript：`docs/runbooks/receipts/journal-scope-census/node22-2026-09-02-de33bd87.json`
  与同目录 `-transcript.md`。

**默认预算在生产树上一定跳闸，这是既定事实不是偶发**（#1953，2026-09-14 实测，
receipt：同目录 `node22-2026-09-14-7b38bcb8-record-budget.json` + `-record-budget.md`）：
node-22 活树的 `include_direct=False` 原始计费是 **148,381**（`latest/**` 7,898 个
文件 54,258 行 + `journal/**.jsonl` 325 个文件 94,123 行），去重之后只有 **17,025**
个唯一 job——预算约束的是**读取工作量**，不是结果规模。所以：

- **默认预算下的整树 replay 必然以 `file_journal_record_limit_exceeded:
  pipeline_job_records` 拒绝**（实测 ~92 s 才拒绝），stderr 一行，零写入。
- 该拒绝的结构化 evidence 现在带 **`lane`**：整树车道是 `full_tree_replay`，
  单 cycle 车道是 `cycle_replay`。**stderr 那一行没有变**，仍然是
  `file_journal_record_limit_exceeded: pipeline_job_records`——census 渲染的是
  `reason: field`，lane 只在 evidence 里，用来区分"整棵树太大"和"这一个 cycle
  太大"这两种同码同 field 的拒绝。**注意 census 的 stderr 永远看不到 lane**：
  它只渲染 `reason: field`，把 evidence 整个丢掉。要读 lane，只能看 `query_*`
  返回的合成行 `file_journal.evidence`，或消费该行的 receipt。
- **`--max-records` 是唯一被认可的处置**，`--max-files` 不是这个旋钮（见下）。
- **默认预算不会被调高**：#1810 的 `MAX_FILE_JOURNAL_RECORDS` docstring 已经记
  过"抬高预算只是把悬崖往后挪"，而这次实测说明悬崖已经落在生产树里面了——再调
  一次，下次树长大还得再调。预算保持 100,000，逃生口是命令行旋钮。
- 结论：node-22 当前**没有**分叉行，规划中的 guarded repair 命令
  （`repair-job-id-scope`）因此没有实现；真要修就走本节下面的人工恢复步骤。
- 并发口径：两趟都是在 `nhms-compute-scheduler.service` 处于 `activating`
  时跑的（node-22 上这个 oneshot 跑得久）。**只读 census 与调度器同时跑是被接受
  的模式**：census 不持有任何 repository 锁，撕裂读会以 `file_journal_unreadable`
  fail loud，重跑即可。只有下面的人工恢复步骤才必须先停 timer。

**什么时候跑**：任何 post-#1939 的 checkout 在 node-22 上生效之前跑一次（gate
生效后分叉行才会真正咬人）；以及任何一次手工编辑 journal root 之后。命令对
journal 树零写入——`--output` 拒绝任何位于 root 内的路径，`reconcile-inventory/`
里的 `.tmp` 残留只统计不删除。

**后果 (a)：整趟 reconcile scan 中止。** 一行分叉行如果同时满足"有
`reconcile-inventory/<job_id>.json` anchor" + "没有 flat direct 文件"，
`_iter_reconcile_inventory_records` 的非严格修复分支会去
`_restore_derived_master_direct_unlocked`，出站校验撞上 gate；该分支没有 `try`，
于是**排序在它之后的 anchor 一个都不会被 yield**，这个 anchor 也不会被 prune。
`scheduler_runtime.py` 用 `except Exception` 兜住，pass 表面上活着，但
`evidence["status"]` 变成 `error`、恢复 0 个 cohort，**每一趟都如此**，而且没有
任何 receipt 会说出这个 `job_id`。census 把这个组合命名为
`reconcile_abort_trigger`。

**后果 (b)：这一行无法通过任何 API 退休。** `update_pipeline_job_status`、
`upsert_pipeline_job`、`permit_pipeline_job_retry` 都把持久化的 `job_id` 原样
带进出站记录，所以 gate 会反复触发：

- **非终态行**：朝任何目标状态都被拒。
- **终态行**（`succeeded` / `failed` / `cancelled`）：朝普通目标会在 gate 之前
  短路，但朝 `partially_failed` / `permanently_failed` 仍然进入 gate
  （`terminal_guarded` 的第二个合取项）。

也就是说**终态行和非终态行都不能通过 API 退休**。生产上真正会撞到它的路径是
auto-retry 拒绝分支上的 `FileJournalRetryService.mark_permanently_failed`
（`retry.py` / `chain_forecast_orchestrator_cycle.py`）以及
`chain_array_accounting.py` 的 `partially_failed` 汇总。

**人工恢复**（唯一支持的路径；顺序不可省）：

1. `systemctl --user stop nhms-compute-scheduler.timer`，确认
   `nhms-compute-scheduler.service` 为 `inactive`。
2. 备份：把该 `job_id` 的 flat direct、by-cycle direct 和 anchor 三个文件拷到
   root 之外的目录。
3. 删除该行的 `pipeline-jobs/<job_id>.json`、
   `pipeline-jobs/by-cycle/<source>/<cycle>/<job_id>.json` **以及它的
   `reconcile-inventory/<job_id>.json` anchor——三者必须一起删**。只删 direct
   文件而把 anchor 留下，下一趟 scan 会在同一个 anchor 上以同样的方式再次中止。
4. 重跑上面的 census，确认该 `job_id` 的 `reconcile_abort_trigger` 已消失。
5. `systemctl --user start nhms-compute-scheduler.timer`。

**segment 里的那一份留着**：journal segment 是 append-only 历史。anchor 与
direct 文件删除之后，这一行不再喂给 reconcile scan、也无法被 transition，但它会
继续出现在 census 的 `journal_replay` surface 上，`reconcile_abort_trigger` 为
`false`。这是可接受的终态；为退休它而重写 segment 不在范围内。

### 8.12 state-index 容量与 `prune-retention`（#2548）

**为什么要剪**：`scheduler/state-index/index-last.json` 从不自动裁剪。发布与读取都
有硬上限——`MAX_STATE_SNAPSHOT_INDEX_JSON_NODES=300_000`、
`MAX_STATE_SNAPSHOT_INDEX_BYTES=16 MiB`、`MAX_STATE_SNAPSHOT_INDEX_ENTRIES=100_000`——
任一越限，`state_save_qc`、`set_usable_flag`、copyback 与 warm-start 读取全部
fail-closed。2026-09-25 的 node-22 private index 是 9,950 条 / 203,581 个 JSON 节点 /
11.33 MB，节点与字节两个上限大约在 14.6k 条时同时触顶。

**信号**：`state_index_evidence()`、`validated_entries_for_renewal` 的 evidence
（provider-refresh 的 state 证据）、`publish_state_snapshot_index` 的返回、以及本操作
每 lane 的 `retention.capacity_before/after` 都带 `capacity`；各 reader 回答里嵌套的
`state_snapshot_index` 块**不带**（它们整块进 scheduler pass evidence，node-22 最大
pass evidence 已是 4,526,683 / 5,000,000 B、含 384 个这种块）。字段：
`entry_count/max_entries`、`json_nodes/max_json_nodes`、`index_bytes/max_bytes`、
`utilization_ratio`（三者最大值）、`warning_threshold`（0.70）、`warning`。
`warning` 为 true 时 `packages.common.state_manager` 每次加载打一条 WARNING
（`... run the prune-retention repair (known-issues-state-index 8.12)`）。这只是
证据，不会让任何 reader / publisher 比原有硬上限更早失败。**`warning` 出现就跑本节
流程**；定时器是后续工作，当前靠人工节奏。

**剪什么、留什么**（每 lane 各自按自己的 entry 规划，两份 index 不是镜像）：按
已校验 identity key 的 `(model_id, 规范化 source_id)` 分组（`gfs` / `GFS` 同组），
generation 为 `str(model_package_checksum or "")`（无 checksum 即 `""` 这一代），
排序 `(valid_time, state_id)`。组内 `W_g = 该组最新 valid_time − retention_days`，
满足任一条即保留：

- K1 `valid_time ≥ W_g`；
- K2 每个 generation 的最早 usable、最新 usable、`W_g` 之前最新 usable；
- K3 在 usable 子序列上（unusable 行跳过、不断段），每段同 generation 连续条目的
  首条——只有换代才断段，所以对剪过的 index 再剪一次不会再删任何条目；
- K4 带非空 `cloned_from_model_id` 的 clone 行；
- K5 被任何保留行 `cloned_from_state_id` 指向的 source 行（传递闭包）。

其余条目**只从 index 移除，state 对象一律不删**。保留条目按原始 raw mapping、原顺序
重发（与 `remove-entry` 同口径：只去掉读时注入的 `index_generated_at` /
`object_evidence`）。

**窗口下限**（预检，零写入拒绝）：`retention_days × 24` 必须 **大于**
`MAX_LOOKBACK_HOURS`（336，`packages/common/scheduler_limits.py`，scheduler 原名
re-export）+ `cycle_lag_hours` + 48，否则 `repair_retention_window_too_short`
（evidence 带 `floor_hours`）。lag 取 `--cycle-lag-hours`，缺省读
`NHMS_SCHEDULER_CYCLE_LAG_HOURS`；两者都没有（或非整数）→ `repair_cycle_lag_unset`。
node-22 lag 16：下限 400 h，默认 21 天（504 h）通过，17 天是最小可用值。
`--state-id/--run-id/...`、`--lane`、`--allow-missing-*` 对本操作一律拒绝。

**流程**（node-22，provider 属主 `frd_muziyao`；根、archive、receipt 口径同 8.9）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
cd /scratch/frd_muziyao/NWM
set -a
. infra/env/compute.scheduler-provider-refresh.env
NHMS_SCHEDULER_CYCLE_LAG_HOURS=$(grep '^NHMS_SCHEDULER_CYCLE_LAG_HOURS=' infra/env/compute.scheduler-dbfree.env | cut -d= -f2)
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/archives
NHMS_SCHEDULER_STATE_INDEX_REPAIR_RECEIPT_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/receipts
set +a
: "${NHMS_SCHEDULER_CYCLE_LAG_HOURS:?lag 为空，停}"

# 1) dry-run：不加锁、不写 archive/receipt/index；stdout 一行 JSON
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  prune-retention > /tmp/prune-dryrun.json

# 2) 审核（见下）后，冻结 writer 再 enforce
systemctl --user stop nhms-compute-scheduler.timer
systemctl --user is-active nhms-compute-scheduler.service   # 必须是 inactive 再往下
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.scheduler_state_index_repair \
  prune-retention --enforce
systemctl --user start nhms-compute-scheduler.timer
```

**审核 dry-run**：先看每 lane 的 `lanes.<lane>.checksum_valid`——**两 lane 都必须是
`true`**。任一为 `false` 说明 payload 被带外改过，prune 会用新 checksum 把它重新签名；
停下来走 8.9，查清并修好之后再回来。然后看每 lane 的 `lanes.<lane>.retention`：

- `entry_count_before/after`、`json_nodes_before/after`、`index_bytes_before/after`、
  `utilization_ratio_before/after`、`removed_count`；
- `groups[]`（有移除的组：`model_id`、`source_id`、`window_start`、`removed_count`、
  `removed_valid_time_min/max`、`kept_count`；每 lane 摘要有 256 KiB 字节预算，放不下
  的组数记在 `group_summaries_truncated`，保证 enforce receipt 落在 1 MiB 以内）；
- `capacity_before` / `capacity_after`（完整 capacity 对象，见"信号"）；
- `kept_out_of_window_count` 与 `kept_out_of_window_by_anchor`（各锚点命中数，可重叠）；
- `removed_state_ids`（仅 dry-run 列全量）与 `removed_state_ids_sha256`
  = `sha256(json.dumps(sorted(ids), separators=(",", ":")))`。
- `entry_count_before` 必须与现网条数一致（0 = 根写错 / NFS 没挂，别 enforce）；
  没有可剪的 lane 是 `action=skip`、`untouched_reason=nothing_to_prune`。

enforce 在锁内**重新规划**（锁序 reference → destination，与 copyback 相同），所以
dry-run 与 enforce 之间落地的 upsert 不会丢；它可能让窗口后移、使 enforce 多剪几条。
用 receipt 里的 `removed_state_ids_sha256` 与 dry-run 比对，不一致就先看新增条目。
enforce receipt 只有摘要和 digest（有 1 MiB 上限），不含 id 列表。
`upsert_state_snapshot` / `set_usable_flag` 与 enforce 共用 `.index-last.json.lock`，
所以 state-save 作业是串行而不是竞争；copyback / replay、recalibration clone、
provider-refresh 仍按 8.9 冻结。

**退出码与部分提交**：同 8.9（0 / 2 / 3）。先写 reference、后写 destination；
destination CAS 失败时 exit 3，reference 已剪、destination 字节不变——**不要**用
reference 回填 destination，重新 dry-run 后再跑一次即可：planner 是不动点（K3 跳过
unusable 行），已剪的 reference 必然是 `nothing_to_prune`，只剪 destination（已测）。两 lane 都无可剪时 enforce 的 `status=untouched`，不写 archive。

**已知限制**：

- 回溯 clone：`scripts/node22_clone_direct_grid_cutover_states.py --cutover-time`
  需要 M0 在 cutover 时刻的精确 entry。cutover 早于 M0 的窗口且不是锚点时，以
  `missing_qualified_source` fail-closed。处置：选窗口内或锚点时刻的 cutover，
  或从 archive 恢复。
- `GET /state-snapshots/{state_id}` 对被剪 id 返回 404；预像在 archive 里。
- 早于窗口的 cycle 的手工 retry：精确 checkpoint 已剪时 strict warm start 报
  `state_snapshot_index_exact_checkpoint_missing`，transition 只会变成 block
  （例如 `warm_continue` → `block_predecessor_pending`；被剪的错代 entry 也会让
  `block_wrong_generation` 读成 `block_predecessor_pending`），不会变成 warm/cold
  admit。变成 `block_predecessor_pending` 的候选**可能触发 8.6 的 predecessor 补账
  工作**——它仍然是 block，只是多了一次补账尝试。
- 回滚：用各 lane 自己的 archive 字节，先核对当前 preimage；CLI 不会覆盖后来的发布。

**2026-09-25 生产副本 dry-run**（本地 staging，lag 16，默认 21 天）：两 lane 都从
9,950 条剪到 5,772 条（移除 4,178，192 组里有 98 组有移除）；private 203,581 →
106,639 节点、11,334,892 → 6,048,768 字节，utilization 0.679 → 0.361；shared
183,681 → 106,639 节点、10,439,392 → 6,048,768 字节，0.622 → 0.361。窗口外保留 196 条
（earliest/run-start/latest-before-window 各 98，clone 行 46）。private 的字节降幅
有一部分来自去掉读时注入的两个字段，不只是剪条目。对剪完的副本再跑一次 dry-run：
两 lane 都是 `nothing_to_prune`。

**reference lane 的回弹与余量**（同一副本实测）：repair 发布的是不含
`index_generated_at` / `object_evidence` 的条目；reference lane 的下一次
`upsert_state_snapshot` 会把这两个字段重新注入每一条。实测剪完 5,772 条 /
106,639 节点 / 6,048,768 B（0.361），一次 upsert 后 5,773 条 / 118,202 节点 /
6,569,110 B（0.394）——每条约 +2.0 节点、+90 B。所以 reference 的余量按回弹后的形状
算：约 20.5 节点、1,138 B / 条，节点上限约在 14,652 条、字节上限约在 14,744 条触顶，
即剪完后还能再长约 8,900 条，按 192 条/天约 46 天；到 0.70 再次告警约在 10,256 条，
约 4,500 条、约 23 天。shared lane 由 copyback 写入、不回弹（18.5 节点、1,048 B / 条）：
字节上限约 16,010 条（余量约 10,200 条、约 53 天），0.70 约 11,200 条（约 28 天）。
这些是"不再剪"的上限；实际节奏按 `warning` 触发重跑。
