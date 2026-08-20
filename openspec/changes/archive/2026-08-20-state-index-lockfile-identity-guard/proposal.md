# state-index merge 的 lockfile 级身份守卫 + #1192 探针姿态补测（#1609 + #1610）

## Why

两条都是 #1192（PR #1608）交付后**自己声明**的残余，同域、同证据链，合批交付。

### #1609 —— root 级守卫之下，lockfile 级仍会自死锁

#1192 把 copyback same-root 判定从「resolve 后路径字符串」改成了 `(st_dev, st_ino)` 身份，
但判定只发生在 **root 级**。死锁本身发生在 **lockfile 级**：

- `packages/common/state_manager.py:1894` `merge_state_snapshot_index_copyback` 取 source 侧 `provider_destination_lock`
- `:1954` `_merge_state_snapshot_index_copyback_locked` 在其内部取 destination 侧的锁
- `packages/common/provider_atomic.py:330` 默认 `blocking=True`；`:162` 进程内 registry 按
  `os.path.abspath(lock_path)` 取 key（**字符串**）；`:219` 阻塞态不带 `LOCK_NB`
  ⇒ 同一 inode 的 lockfile 被同一进程锁两次即**永久阻塞**

于是 **root 之下**的别名仍可走到同一个 lockfile：把某个 export 挂在 `<destination_root>/scheduler/…` 之下，
两个 root 本身身份不同（#1192 的守卫放行），但两者的 `.index-last.json.lock` 落在同一 inode 上。
失效模式与 #1192 完全相同：不退出、不产 receipt、无结构化 reason，且长期占着 private index 锁，
把默认非阻塞的写者饿成持续 `provider_already_running`。

### #1610 —— #1192 的探针失败姿态几乎全无测试（8 条分支里 7 条）

verifier 亲自复现的 sentinel 实验：把 5 处探针失败处理器换成 `raise AssertionError(...)`，
并把 object-store 探针与宽松预检探针移出各自 try —— **192 passed，无一触发**。
唯一被覆盖的是 apply 路径（`tasks.md` B.2(d) 当初唯一强制的那条）。
（issue 标题写「6 条里 5 条」是漏数：master 上 `directory_identity_no_follow` 有 **8 个生产调用点**，
未覆盖 **7 条**。逐条表见 tasks.md D.1。）
后果：`openspec/specs/forcing-copyback-backfill/spec.md` 里
"An object-store-root probe failure is diagnosed on the object-store side" 这条 SHALL
**随 #1192 归档进了基线却零强制**；日后有人把探针挪出 try，CI 全绿而运行时退化成通用错误码。

## What Changes

### #1609

在 `merge_state_snapshot_index_copyback` 取**任何**锁之前，判定两侧 provider lockfile 是否为同一个文件，
是则 fail-closed。判据**两支并用**：

1. **父目录身份 + lock 文件名**（主判据，**不要求 lockfile 已存在**）：
   `directory_identity_no_follow(source_path.parent) == directory_identity_no_follow(destination_path.parent)`
   且 `provider_lock_path(source_path).name == provider_lock_path(destination_path).name`。
   这一支覆盖真实威胁形状（root 之下的别名目录），且在 lockfile 尚未创建时同样成立。
2. **lockfile 自身 `(st_dev, st_ino)`**（补充判据，**仅当两侧都已存在**）：经 `stat_no_follow`
   （`packages/common/safe_fs.py:270`）取身份比对。这一支覆盖「两个真实不同目录、lockfile 被 hardlink」
   这种支 1 看不见的形状。

**路径不存在一律使该支「不适用」，两支皆然、两侧皆然** —— 不存在的路径没有 inode，不可能与已存在者互为别名。
这条尤其覆盖 **bootstrap copyback**：首次写入时 `<copyback_root>/scheduler/state-index/` 由取锁动作本身创建，
守卫运行时它还不存在（round-0 实测：该形状今天 `MERGE OK`，既有用例
`tests/test_run_tree_copyback.py:198` 正是它）。判成「探针失败 → fail-closed」就是**打断 userspace**。

同时，两个 reason 必须登记进 replay 工具的 pre-commit 白名单 —— 见 design「两个调用方，两套分类」。

复用 #1192 已落地的 `directory_identity_no_follow` 与既有 `stat_no_follow`，**不新增原语**。

### #1610

给 7 条未覆盖的探针失败分支逐条补 posture 测试（monkeypatch 各模块命名空间里的
`directory_identity_no_follow` 使其抛异常，断言该站点**自己**的 error code），
并补一条「把探针移出 try」的变异必红。**不改任何生产姿态**——#1192 已逐点核实 9 行姿态表与设计一致。

## Non-Goals

- **不改锁语义**。`provider_destination_lock` 仍 `blocking=True`、仍不可重入；
  `packages/common/provider_atomic.py` **一字不动**。本单只在取锁**之前**加一道判据。
- 不把 `merge_state_snapshot_index_copyback` 的外层锁改 `blocking=False`（#1192 已考虑并拒绝：
  会把合法排队的并发 merge 变成需调用方重试的立即失败）。
- 不动 #1192 已交付的五处 **root 级**守卫（它们各自的 mutant 证据链保持逐字成立）。
- 不改任何 #1192 探针失败姿态的**生产行为**（#1610 是纯证据面）。

## Known Limits

- 支 1（父目录身份）**看不见** hardlink：两个真实不同目录下被硬链到同一 inode 的 lockfile，父目录身份不同。
  这正是支 2 存在的理由。
- 支 2（lockfile 身份）**只在两侧 lockfile 都已存在时**可判；首次 merge 时它们尚未创建，此时只有支 1 生效。
  两支合起来仍非全覆盖：**「两个不同目录 + lockfile 尚不存在 + 未来会被硬链」无法预判**——这在物理上也无从判起。
- `(st_dev, st_ino)` 仍只覆盖 **same-superblock** 别名（同 #1192：`nosharecache` 双挂载 fileid 相同但
  `st_dev` 不同，仍绕过）。
- 与 #1192 不同的是：**lockfile 是文件，文件可以 hardlink**，所以支 2 有**真实、免 root、可移植**的红证构造。
  这是 #1192 对目录做不到的那一条。
