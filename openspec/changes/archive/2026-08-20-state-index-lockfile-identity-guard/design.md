# Design — state-index lockfile 身份守卫 + 探针姿态补测（#1609 + #1610）

## 风险三角

```text
Issue type: bugfix (#1609) + test-evidence (#1610)
Project profile: NHMS (two-node scheduler / file-provider)
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent（两条都是 #1192 自报残余；按 issue-risk-contract 的强制触发词
  concurrency + persisted/shared state + `path` 直接判 expanded）
Why:
- 改的是双锁路径的前置判据（concurrency / shared state）
- 解冻了 #1192 明文冻结的 packages/common/state_manager.py
- 新的 raise 点落在一条被审计过的错误分类不变量里（见「分类不变量」节）
- #1610 要补的是一条已归档进基线却零强制的 SHALL
Selected risk packs:
- Concurrency / shared state / ordering
- File IO / path safety / overwrite
- Error handling / rollback / partial outputs
- Public API / CLI / script entry（round-0 P1 后新增：replay CLI 的 pre-commit reason 白名单要加两项，
  直接决定该 CLI 在这一新失败类下的退出码与 receipt 形状；函数签名本身仍不变）
OpenSpec change: state-index-lockfile-identity-guard (generated)
Evidence floor: 见 tasks.md 的 C 段
```

未选风险包及理由：

| 包 | 结论 |
|---|---|
| Config / project setup | not selected —— 无 env / 配置字段变动 |
| Schema / columns / units / field names | not selected —— index / receipt payload 不变 |
| Auth / permissions / secrets | not selected —— 不涉凭据；支 1 的父目录 walk 与 #1192 已引入的读权限面同源，不新增（两侧父目录本就要被 provider 锁打开） |
| Resource limits / large input / discovery | not selected —— 每次 merge 多 2~4 次 fd stat，O(1) |
| Legacy compatibility / examples | not selected —— 判定只会**更严**；今天能正常 merge 的输入（两个真不同 lockfile）行为逐字不变 |
| Release / packaging / dependency compatibility | not selected —— 无新依赖 |
| Documentation / migration notes | **selected**（round-0 iteration-2 P2）—— `docs/runbooks/current-production-ops.md:1979` 的 exit-code 解码表明写「allowlist 之外（含未来新增 reason）… **不会**出现在 exit 2 里」，本单新增两个 reason 后该括注**变成假的**，必须同步。挂载表治理仍是 #1192 划出的 out of scope |

## #1609 的判据与落点

落点：`packages/common/state_manager.py:1867` `merge_state_snapshot_index_copyback` 函数体**最顶端**，
早于 `:1894` 的 `with provider_destination_lock(source_path, ...)`。

```text
same_lockfile :=
    (A) directory_identity_no_follow(source_path.parent)
        == directory_identity_no_follow(destination_path.parent)
        and provider_lock_path(source_path).name == provider_lock_path(destination_path).name
 or (B) both lockfiles exist
        and stat_no_follow(src_lock).(st_dev, st_ino) == stat_no_follow(dst_lock).(st_dev, st_ino)
```

- **支 A** 覆盖真实威胁形状（root 之下的别名目录），且**不要求 lockfile 已存在** —— 首次 merge 时
  `provider_destination_lock` 才会创建它，若只判文件身份就会在死锁**之后**才有得判。
- **支 B** 覆盖「两个真实不同目录、lockfile 被 hardlink」——支 A 结构上看不见。

**路径不存在 ⇒ 该支不适用，两支皆然、两侧皆然（round-0 P1）。** 原稿只给支 B 开了这个口子，
支 A 仍按「探针失败 → fail-closed」处理，那会**打断 bootstrap copyback**：
`STATE_INDEX_OBJECT_KEY = "scheduler/state-index/index-last.json"`（`run_tree_copyback.py:24`）在
copyback root 之下有两级目录，`:49` 的 `ensure_directory_no_follow` 只建 root，两级由取锁动作本身创建 ——
守卫运行时 `destination_path.parent` **不存在**。round-0 实测：
`dest parent exists before merge: False` / `branch-A probe RAISED FileNotFoundError` / `MERGE OK today`。
既有用例 `tests/test_run_tree_copyback.py:198` 正是这个形状，按原稿会**变红**。
不存在的路径没有 inode，不可能与已存在者互为别名，所以「不适用」是正确语义，不是放水。
实现上 `except FileNotFoundError` 必须**先于** `except OSError`。

复用 `directory_identity_no_follow`（#1192 交付，`safe_fs.py:32`）与既有 `stat_no_follow`（`:270`）。
**不新增原语。**

### 分类不变量（这是本单最容易踩坏的东西）

`services/orchestrator/run_tree_copyback.py:137-155` 对 merge 抛出的
`(ProviderAtomicError, StateManagerError)` 做二分：带**非 `precommit`** 的 `phase` → 归
`OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN`；否则（含 `phase is None`）→ 归
`OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED`（**fail-closed**）。

`:117-132` 的注释把「no-phase 桶安全」显式挂在一条**已审计的不变量**上：
「every no-phase raise point in the merge call graph sits before the destination compare-and-swap」。

因此本单的新 raise 点：

- **必须不带 `phase` 属性、也不带 `evidence["phase"]`**（或显式 `phase="precommit"`）——它在取锁之前，
  是这条不变量的**新成员**，落 fail-closed 桶正确；
- **绝不可**带非 `precommit` 的 phase —— 那会把一个「什么都没碰」的前置拒绝谎报成「可能已提交」，
  操作员会去查 shared entry_count，属误导。

### 两个调用方，两套分类（round-0 P1，我原稿这里断言错了）

原稿写「replay 侧同理…符合既有形状」——**错的**。replay **不看 phase，按 reason 白名单分类**：

- `scripts/scheduler_state_index_copyback_replay.py:405`
  `if typed and error_reason in MERGE_PRE_COMMIT_REFUSAL_REASONS:` → refusal；**否则 commit-uncertain**。
  `typed`（`:403`）只买到「不是裸崩」，买不到分类。
- 白名单 `:191` = `_PRE_COMMIT_PROVIDER_REASONS | _PRE_COMMIT_INDEX_REASONS`（`:152-181`），
  其 docstring（`:183-190`）明写：「Anything else — an empty reason, an unknown reason,
  **a reason added to the merge later** — is classified commit-uncertain (exit 3)」。
- 后果链：`:417-427` 置 `merge_uncertain` → `:431` `index_may_be_committed = True` →
  `:437-438` receipt 写 `merge_commit_state: "uncertain"`，并跑 `_verify_committed_destination`。

**所以本单必须把两个新 reason 显式加进 `_PRE_COMMIT_INDEX_REASONS`**，否则一个「什么都没碰」的前置拒绝
会在 replay 这条产线上被报成「可能已提交」——正是本节要防的误诊，只是发生在另一个调用方。
既有套件只逐条抽查个别 reason（`tests/test_scheduler_state_index_copyback_replay.py:564/648/691`），
**没有白名单覆盖度测试**，所以漏加是**假绿**：必须有 B.3(b) 那条钉子。

同时这也决定了错误的**构造方式**：必须用 `_state_index_error(reason, field=..., evidence=...)`
（`state_manager.py:3141`，设 `.reason`/`.field`/`.evidence`，不设 `.phase`），
**不能**裸 `StateManagerError("…路径…")` —— 后者 `.reason` 为空，
`run_tree_copyback.py:141` 得到 `error_reason=None`，replay 合成 `merge_unexpected_exception:StateManagerError`。
路径进 `evidence`、不进 message。**但脱敏不是自动的**（round-0 iteration-2 P3）：
`_state_index_error`（`:3141-3146`）只做 `dict(evidence or {})`，**不调用** `_state_index_evidence_safe`（`:3090`）——
实现必须**显式**把 evidence 过一遍该函数，否则 spec 里那条 SHALL 不成立。
已知限制：**两个调用方都不外露 `.evidence`**（`run_tree_copyback.py:139` 与
`scheduler_file_provider_refresh.py:1094` 只读 `["phase"]`），所以操作员的诊断实际只靠 reason 名 ——
这正是把 reason 起成自解释的 `state_snapshot_index_copyback_lock_identical` 的理由。

### 必须保持不变（must-preserve）

1. `packages/common/provider_atomic.py` **一字不动**（`blocking=True` 默认、registry 的字符串 key、
   `:219` 的 flock 语义都不动）。
2. `merge_state_snapshot_index_copyback` 与 `_merge_state_snapshot_index_copyback_locked` 的**签名不变**；
   两个生产调用点的**调用形式**不改。replay 工具是**受限例外**：只允许往
   `_PRE_COMMIT_INDEX_REASONS`（`:152`）加那两个 reason 常量，**不得**改其分类逻辑、退出码或 receipt 结构
   （原稿写「两个调用点不改」与 A.7 自相矛盾，此处按 round-0 P1 收敛）。
3. #1192 交付的**五处 root 级守卫一字不动**，其 mutant 证据链保持逐字成立。
4. 两侧 lockfile 确实不同时，行为**逐字不变**（不新增任何日志/receipt 字段）。
5. #1610 **不改任何生产姿态**，只补测试。

### 探针失败姿态

`FileNotFoundError`（**两支皆然、两侧皆然**）⇒ 该支不适用，**不是**失败，见上。

其它 `OSError`/`SafeFilesystemError` ⇒ 用
`_state_index_error("state_snapshot_index_copyback_lock_identity_unavailable", …)` 包裹、
**不带 phase**、fail-closed，且该 reason 同样要进 replay 白名单。
不得吞、不得降级为「判定不出就放行」——放行等于把死锁留在原地。

## #1610 的补测清单（纯证据面）

七条未覆盖的探针失败分支，逐条 monkeypatch **该模块命名空间里**的 `directory_identity_no_follow` 使其抛异常，
断言该站点自己的 error code：

| 站点 | 期望 code |
|---|---|
| `scripts/scheduler_state_index_copyback_replay.py` `_root_identity` | `root_unavailable`（带 `field`） |
| `services/orchestrator/run_tree_copyback.py` `_directory_identity` | `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE`，且**消息按 field 区分操作数**（PR #1608 的 F1 修的就是这条） |
| `services/tile_publisher/publisher.py` `_copyback_run_products` | `OBJECT_STORE_COPYBACK_FAILED` |
| `services/tile_publisher/publisher.py` `_copyback_qdown_products` | `OBJECT_STORE_COPYBACK_FAILED` |
| `services/tile_publisher/forcing_copyback_backfill.py` dry-run | `COPYBACK_ROOT_UNSAFE` |
| `forcing_copyback_backfill._verify_object_store_root`（object-store 侧） | `OBJECT_STORE_ROOT_UNSAFE`，且 details 指向 **object-store root 而非 copyback root** |
| `forcing_copyback_backfill` 宽松预检（`:401-408`） | **静默 `return`**，不 raise（姿态守恒） |

外加**宽松预检**那条：探针失败时断言**静默返回**（不 raise），这是 #1192 明文的姿态守恒。

apply 路径已有覆盖（`tests/test_forcing_copyback_backfill.py`），**不重复造**。

## 红证构造：这次有真的

与 #1192 最大的不同：**lockfile 是文件，文件可以 hardlink**。所以支 B 有**真实、免 root、可移植**的构造：
两个真实不同目录，各放一个 index 文件，把两者的 `.<name>.lock` 用 `os.link` 硬链到同一 inode。
修前：两把锁按路径字符串各取各的 → 走到 `fcntl.flock` 同 inode 第二次加锁 → **真挂**。
修后：支 B 命中 → 结构化 fail-closed。

**因此本单的「不 hang」断言是有判别力的**（#1192 那条不是，见 PR #1608 的更正）。
round-0 已用 `subprocess.run(..., timeout=25)` 实测证实死锁：
`same lock inode: True` / `abspath keys differ: True` / `TIMEOUT -> deadlock confirmed`。

**权限陷阱（round-0 P2）**：两侧 lockfile 父目录必须 `umask 077` + `chmod 0o700`。
否则 `provider_atomic.py:206-208` 的 `provider_lock_parent_unsafe`
（`st_uid != geteuid()` 或 `S_IMODE & 0o022`）会在**取源锁时**先抛，于是 mutant「红了」但不是因为挂起 ——
判别力自证会假成立。既有套件已有先例（`tests/test_run_tree_copyback.py:212` 显式 `os.umask(0o077)`）。
故 B.1(d) 必须断言红因**就是 join-timeout tripwire**，不是旁路。
但正因为**修前真的会挂**，红证必须在**有界**壳里跑：
`threading.Thread(..., daemon=True)` + `join(5.0)` + `pytest.fail`，或 `subprocess.run(..., timeout=5)`。
**禁止裸调用** —— 裸调用在修前会挂死整个 pytest 进程。

支 A 无法用真实 FS 构造（需要 bind mount / root），仍走探针注入：monkeypatch
`state_manager.directory_identity_no_follow` 让两个真实不同的父目录报同一身份。
这要求 `state_manager.py` 以 `from packages.common.safe_fs import directory_identity_no_follow`
导入到自身命名空间——**patch 点必须就是产线调用点**。

## node-22 / node-27 路由

- **node-22：不跑任何东西。** #1192 的禁令继续有效（禁止死锁探针）。本单的死锁红证只在**本地/CI 的
  有界壳**里跑，绝不上计算节点。
- **node-27**：merge 后全量 receipt，`umask 022`，独立 detached worktree，**不得触碰 `/home/nwm/NWM` 主树**。
  注意 master 上目前有三条已知红：`test_entropy_audit_script.py::…hard_gate…`、
  `test_scheduler_file_provider_refresh.py::test_provider_snapshot_rejects_replacement_between_metadata_and_read`
  （两条 #1192 前就红），以及 #1613 那条顺序依赖。**收据判读以「相对 master 基线不新增」为准**，
  不是「零红」。
