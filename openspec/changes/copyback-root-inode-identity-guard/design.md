# Design — copyback root inode 身份守卫（#1192）

> round-0 fixture 审已跑过一轮（REVISE → 本稿）。审出的两条 P1（L1 层按字面不可实现、同形守卫是五处不是三处）
> 与四条 P2/P3 已在本稿逐条落地，修法记在各节末尾的「round-0 修正」。

## 风险三角

```text
Issue type: bugfix
Project profile: NHMS (two-node scheduler / file-provider)
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent（issue 未带 Suggested fixture level；按 issue-risk-contract 的强制触发词
  `path` / `symlink` / `CLI` / concurrency+shared state 直接判 expanded，不下调）
Why:
- 五处 fail-closed 守卫的判等谓词同时改动（shared entrypoint 级）
- 涉及 no-follow fd / symlink / 路径包含语义（file IO 路径安全）
- 失效模式在 provider 双锁上（concurrency / shared state）
- 真实红证形状（bind mount）在 CI 与本地都不可构造，证据链必须显式分层
Selected risk packs:
- File IO / path safety / overwrite
- Concurrency / shared state / ordering
- Error handling / rollback / partial outputs
- Public API / CLI / script entry
OpenSpec change: copyback-root-inode-identity-guard (generated)
Evidence floor: 见 tasks.md 的 C 段（C.1-C.8），此处不复制以免两处漂移
```

未选风险包及理由：

| 包 | 结论 |
|---|---|
| Config / project setup | not selected —— 不新增/不改任何 env 或配置字段，只改已有两个 root 的判等方式 |
| Schema / columns / units / field names | not selected —— index/receipt 的 payload 结构一字未动，沿用既有 reason 字符串 |
| Auth / permissions / secrets | not selected —— 不涉及凭据、不新增可写面。注意 `O_RDONLY\|O_DIRECTORY` 需要祖先目录**可读**而非仅可搜索，这条已在下面「行为变更的诚实边界」单列，不足以选入本包 |
| Resource limits / large input / discovery | not selected —— 每次调用多两次 fd open+fstat，O(1)，与输入规模无关 |
| Legacy compatibility / examples | not selected —— 判定只会**更严**；无持久化格式、无外部调用方契约变化（唯一例外见「行为变更的诚实边界」） |
| Release / packaging / dependency compatibility | not selected —— 无新依赖，`os.fstat` 是 stdlib |
| Documentation / migration notes | not selected —— 运维侧挂载表治理已被 issue 划为 out of scope；本单不写 runbook |

## 单一缝位（这是整单的设计核心）

新增**一个** helper，五处守卫都只经由它取 root 身份：

```python
# packages/common/safe_fs.py
def directory_identity_no_follow(path: Path) -> tuple[int, int]:
    """Return (st_dev, st_ino) for an existing directory, opened without following symlinks."""
```

实现约束：

- 必须复用既有 `_open_directory_no_follow(target)`（`packages/common/safe_fs.py:654`，逐分量 `O_NOFOLLOW` 打开），
  然后 `os.fstat(fd)`，`finally` 关 fd。**不得**新增 `os.stat(path)` 这类 follow 调用 —— 那会引入一条新的 follow 面，
  与本仓库既有的 no-follow 纪律相悖。
- 返回裸 tuple，不返回 `os.stat_result`（避免调用方误用其他字段做判等）。
- **只能对已 `.resolve()` 的路径调用**。`_open_directory_no_follow` 对任意 symlink 分量（**包括末段**）直接 raise
  （macOS 走 ENOTDIR `safe_fs.py:627`，Linux 走 ELOOP `:630`），所以拿 raw 路径去探是错的。
  五处站点今天都已在比较前 `.resolve()`，探针接在 resolve 之后即可。

为什么必须是「一个」helper 而不是五处各自 `os.fstat`：

1. 生产实现与红证注入点是**同一个函数**，被 monkeypatch 的那一个就是产线真正调用的那一个；
2. 五处判等语义天然一致，日后有人改 superblock 语义只有一个地方要改。

> round-0 修正：原稿漏了「只能探 resolve 后路径」这条硬约束，而它正是 L1 层塌陷的同一个根因。

## 五处调用点的确切改法

| 站点 | 今天 | 改后 | 探针失败姿态 |
|---|---|---|---|
| replay `_refuse_root_conflicts:766` | `reference == destination` → `roots_identical` | 身份相等 → **同一个** `roots_identical`（payload 字段不变） | 复用既有 `root_unavailable`（带 `field`），不新增 code |
| `run_tree_copyback:46` | `object_root == target_root` → skip | 身份相等 → **同一个** skip 分支（含其中 `_validate_run_tree` 循环与返回 dict，一字不动） | 复用既有 `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE` |
| `publisher._copyback_run_products:737` | `copyback_root == object_store_root` → skip | 身份相等 → **同一个** skip 分支 | 复用**本方法内**既有的 `PublishError("OBJECT_STORE_COPYBACK_FAILED", "Object-store staging root is unsafe for copyback.", ...)`（`:718-728`） |
| `publisher._copyback_qdown_products:875` | 同上 | 同上 | 同款，用本方法内的 `:845-855`（另一可选同款是 `_prepare_copyback_root` 的 `:1106-1114`，message 为 "Failed to prepare object-store copyback root."） |
| `backfill._reject_same_copyback_root:397` | `copyback_root != object_store_root` → return | **改签名，接收已算好的身份**（见下） | 由各调用点自负，逐点列明 |

### backfill 的四个调用点：姿态逐点钉死

`_reject_same_copyback_root` **改为接收 identity 而非自行探测**：

```python
def _reject_same_copyback_root(
    *, copyback_root: Path, object_store_root: Path,
    copyback_identity: tuple[int, int], object_store_identity: tuple[int, int],
) -> None:
```

**`object_store_identity` 的出处（单点，四个调用点都不重算）**：在 `_verify_object_store_root`
（`services/tile_publisher/forcing_copyback_backfill.py:309`，今天已是唯一的 object-store root 校验点，严格，抛
`OBJECT_STORE_ROOT_UNSAFE`）内**一次算出**，随 `Path` 一并返回（改成返回 `tuple[Path, tuple[int, int]]`），
由 `run_backfill:130` 持有并向下透传到 `:176`、`_validate_copyback_root_boundary:327` →
`_reject_existing_same_copyback_root:352` → `:359`/`:366`、以及 `_dry_run_copyback_root:369` → `:384`。
探针失败沿用既有 `OBJECT_STORE_ROOT_UNSAFE`。
**明令禁止**把 object-store 侧探针塞进 `_dry_run_copyback_root` 的 try（`:373-378`）或 `_reject_existing_same_copyback_root`
的 try（`:360-365`）—— 那会把 object-store root 的故障误报成 `COPYBACK_ROOT_UNSAFE` 且 `details.copyback_root` 指向错的那个 root，
正是本单要防的误诊。四个调用点**只**负责各自 copyback 侧的探针：



| 调用点 | 今天 | 改后 |
|---|---|---|
| `:176`（apply 路径，无 try） | 无探针 | 探针**必须**用 try 包住，失败 → raise `COPYBACK_ROOT_UNSAFE`（与兄弟严格路径 `:378` 同码）。**不得**让 `OSError` 裸逃出 `run_backfill` —— 那会被 CLI 的 `except (...)`（`:221`）兜成通用 `BACKFILL_FAILED`（`:224`），错误码从 `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT` 退化，属搭车行为变更 |
| `:359`（raw 字符串已相等的短路） | 传同一个 `object_store_root` 两次 | **不探针**：两侧传同一个 `object_store_identity`。该分支在 resolve 之前，语义就是「两个 raw 串相同」，无需 FS 探测 |
| `:366`（宽松：`:360-365` 两个 except 都 `return`） | 探针不在 try 内 | 探针**必须移进**那个 try（或自带 try → `return`）。原稿只说「失败同样 return」却把改动落在函数本体，导致异常会从 `:366` 逃出 —— 字面要求用原稿改法达不成 |
| `:384`（严格 dry-run） | — | 探针失败 → raise 既有 `COPYBACK_ROOT_UNSAFE`（`:378` 同码） |

> round-0 修正：以上整表是 P2「posture-coverage-incomplete」的落地。原稿只覆盖 2/4 个调用点，且 `:366` 的要求自相矛盾。

### 必须保持不变（must-preserve）

1. **overlap 判定一字不动**。`_paths_overlap`（replay `:779`、run_tree `:55`、backfill `:332`、publisher `:1095`/`:1116`）
   继续用 `Path.relative_to` 字符串包含。inode 判等无法表达父子关系；别名造成的 overlap 因此仍检测不到 —— issue 认这条取舍。
2. **判定顺序不变**。replay `:766`→`:772`、run_tree `:46`→`:55`、backfill `:327`→`:332` 与 `:384`→`:385`：identity 先于 overlap。
   publisher 是例外且**保持例外**：`_prepare_copyback_root` 的两处 overlap（`:1095`/`:1116`）在 `:737`/`:875` 的 same-root 判定**之前**。
   典型别名对（两个平行挂载点）字符串既不相等也不包含 ⇒ overlap 不触发 ⇒ 落到身份分支；raw 相等形状被 `!=` 豁免放过 ⇒ 同样落到身份分支；
   symlink 形状被更早的 `_reject_existing_symlink_components:1099` 拒掉。
   **即使**构造出包含型别名（`mount --bind /a /a/sub`），先触发的 overlap 拒绝本身也是 fail-closed，不是绕过。
   故**不得**为了「统一顺序」去调整 publisher。
3. **每个站点的错误/宽松姿态不变，只换判等谓词**（明细见上两张表）。
4. `ensure_directory_no_follow` 在 `run_tree_copyback:44`、`_prepare_copyback_root:1102` 的**创建**语义不变（先建后 stat）。
5. `packages/common/state_manager.py`、`packages/common/provider_atomic.py` **一字不动**。

### 行为变更的诚实边界（must-preserve 的唯一例外）

`directory_identity_no_follow` 用 `O_RDONLY|O_DIRECTORY`（`safe_fs.py:18`）打开**每一级祖先**，而
`run_tree_copyback._existing_directory:180-186` 今天走的 `_reject_symlink_ancestors:425-429` 只做逐级 `lstat`（只需 `+x`）。
祖先若是 `0711`（仅搜索、不可读），今天正常的 copyback 改后会变成 `OBJECT_STORE_COPYBACK_ROOT_UNAVAILABLE`。
实际影响小（target 侧的 `ensure_directory_no_follow` 早已走同样的 walk，本部署下两个 root 共享绝大部分祖先），
但「既有真不同 root 输出完全不变」这句话**过强**，必须限定为「祖先目录可读时不变」。

> round-0 修正：P3「new-read-permission-surface」。

## 红证构造：为什么 issue 的验收字面不可满足，缝位如何补位

issue 验收写「用 hardlink 或同 inode 构造（不依赖 root 权限的 bind mount）」。逐条排掉：

- **目录不能 hardlink**（POSIX 普遍 EPERM/不支持）—— 而这五处比较的对象都是**目录**；
- **symlink 别名会被 `resolve()` 折叠**；更致命的是 no-follow walk **直接拒绝末段 symlink**
  （round-0 实测：`ERR .../alias  SafeFilesystemError Path component is not a directory`），
  所以 symlink 连「给 helper 喂一个同 inode 的不同串」都做不到；
- **bind mount 需要 root** —— CI 与本地都没有；
- **macOS 大小写折叠**（`/tmp/Foo` vs `/tmp/foo`）确实给出「realpath 字符串不同 + inode 相同」，但在 Linux（node-27 / CI）
  上就是两个不同目录 ⇒ **不可移植，不得进入交付套件**。

**结论：不存在可移植、免 root 且「同时存在」的「不同 realpath / 同 inode」目录对。**
唯一可移植的真实构造是**时序上先后**的一对——`os.rename` 后同一 inode 出现在第二个 realpath 上（round-0 实测
`same identity across two genuinely different realpaths: True`）。它造不出守卫层要的「两条路径并存」，
但足以给 helper 层做**真判别力**证明。证据链因此**必须分三层**，缺一层就是假绿：

**L1 — helper 是身份语义而非字符串语义（真实 FS，可移植，不需 root）**

L1 必须能证伪一个纯字符串实现（例如 `return (0, hash(str(_expand_path(path))))`）。只喂「不同输入串给同一目录」
**做不到**这一点——那种构造只考验 `_expand_path` 的归一化，假实现照样全绿。所以 L1 由四条组成：

1. **不同输入字符串 / 同一目录 → 同一 tuple**：`~/real`（monkeypatch `HOME`，`_expand_path:721-724` 会 `expanduser()`）
   vs 绝对路径；`os.chdir` 后的相对路径 vs 绝对路径。（归一化层）
2. **`os.rename` 前后身份不变**：目录改名到另一个 realpath，身份 tuple 不变。**这一条杀掉字符串实现**——
   路径串变了而身份没变。
3. **oracle 相等**：`directory_identity_no_follow(p) == (os.stat(p).st_dev, os.stat(p).st_ino)`。
   把「返回的确实是内核给的那对数」钉死，同样杀掉任何自造数值的实现。
4. **两个真实不同目录 → 不同 tuple**。

**诚实边界**：L1 证明 helper 消费的是 inode 身份；它**不**证明「两条同时并存的不同 realpath 会给出同一身份」——
那个形状（bind mount）无可移植免 root 构造。

> round-0 iteration-2 修正：P2「l1-still-not-discriminating」。原稿 L1 只有第 1、4 条，对纯字符串实现全绿。

**L2 — 守卫接线（在 L1 的 helper 上注入别名身份）**
monkeypatch 各守卫模块命名空间里的 `directory_identity_no_follow`，让两个**真实不同**的 root 返回相同身份，
断言守卫走结构化拒绝/skip 分支。这要求四个模块（safe_fs 之外的 replay / run_tree_copyback / publisher / backfill）都以
`from packages.common.safe_fs import directory_identity_no_follow` 导入到自身命名空间 —— **实现必须这样导入**，
否则 patch 点不是产线调用点（本设计对实现的硬约束）。
修前（字符串判等）该注入**不改变**任何行为 ⇒ 断言必红；修后必绿。

**L3 — 不回归（真实 FS）**
symlink 别名 root 仍被拒（靠 `.resolve()` 折叠这条路，**不是**靠 helper —— helper 根本不接受末段 symlink）、
真不同 root 仍放行、既有 `roots_identical`/`roots_overlap` 用例
（`tests/test_scheduler_state_index_copyback_replay.py::test_replay_refuses_identical_and_overlapping_roots:739`，断言在 `:778-780`）继续绿。

L2 的诚实边界：它证明的是「守卫消费身份而非字符串」，**不**证明内核在真实 bind mount 上给出同 inode。
后者由 POSIX 语义（同 superblock ⇒ 同 `st_dev`）+ proposal 的 Known Limits 共同承担。
真实 bind mount 未经实机验证，必须写进 PR 的诚实记账。

> round-0 修正：P1「l1-layer-unimplementable」。原稿 B.1(a) 要求 helper 对 symlink 路径返回相同 tuple，
> 与同段 B.1(c)「symlink 分量抛异常」对同一输入要求相反结果，L1 整层塌陷。

## 「不 hang」怎么断言

别名 root 在**取任何锁之前**就被拒，所以修后本就不可能 hang；断言的作用是把「回归重新变成挂起」钉死。

机制约束：
- 被测调用放进 **`threading.Thread(..., daemon=True)`**，`thread.join(5.0)`，`if thread.is_alive(): pytest.fail("hang regression")`。
- **`daemon=True` 不可省**。round-0 实测：非 daemon 线程在 `join(1.0)` 超时后 `pytest.fail` 确实触发，
  但解释器退出仍等线程结束，整进程挂了 30.05s（sleep 全长）。真实回归是 `fcntl.flock` **永久**阻塞
  （`provider_atomic.py:219` 阻塞态不带 `LOCK_NB`），非 daemon 就是永久挂死 —— 与本节想避免的后果一模一样。
- 用例注释里注明：挂住的 daemon 线程会在本次 session 剩余时间里继续持有 lockfile fd。
- **禁止裸调用**。`subprocess.run(..., timeout=5)` 亦可接受，但 monkeypatch 缝位跨不进子进程，故默认线程 + join。

> round-0 修正：P2「hang-tripwire-ineffective」。

## node-22 / node-27 路由

- **node-22：只做只读挂载复核，禁止再跑死锁探针**（issue 原文的禁令，逐字继承）。允许的唯一动作是
  `grep -E "ghdc|nwm" /proc/mounts` + `grep -hE "OBJECT_STORE_ROOT|COPYBACK_ROOT" infra/env/compute.env`。
  任何 reviewer/implementer 都不得在 22 上触发 copyback / merge / 锁获取。若有人要 live hang 确认，
  那是**本地 macOS** 上「硬链接 lockfile + subprocess + timeout」的 reviewer 侧探针，绝不进交付套件、绝不上计算节点。
  另：`nosharecache` 语义无法从 22 的只读复核中证实或推翻，故规格口径只声称 same-superblock。
- **node-27：merge 后全量 receipt，`umask 022`**（默认 umask 会有 ~80 条 #1513 预置红淹掉真实回归），
  在独立 detached worktree 里跑，**不得触碰 `/home/nwm/NWM` 主树**（被另一 session 的分支占用）。

## 备选方案为何不采纳

见 proposal 的 Non-Goals。round-0 复核确认：issue 那条验收项自带前提（「若采纳备选项」），不采纳不留未满足项；
且并发两个 copyback 进程在 `blocking=True` 下是**合法排队**而非死锁，改成非阻塞会把合法排队变成需调用方重试的立即失败。

## 已知残余（不在本单修，另开 follow-up）

守卫只在 **root 级**比身份，死锁发生在 **lockfile 级**（`state_manager.py:1894` / `:1954`）。
root 之下的别名（例如把某 export 挂在 `<destination_root>/scheduler/...`）仍能走到同一个 lockfile 上自死锁。
不改并发语义的兜底是：在 `_merge_state_snapshot_index_copyback_locked` 取内层锁前比较两个 `provider_lock_path` 的
`(st_dev, st_ino)`，不等才取。issue 把范围明确划在 root 级守卫，且本单 must-preserve 冻结了 `state_manager.py`，
故**报告不修**，另立 issue 跟踪。
