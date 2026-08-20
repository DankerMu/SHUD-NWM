# Copyback root 等价守卫改为 inode 身份判定（#1192）

## Why

**五处** copyback same-root 守卫都只做 **realpath 字符串**比较（issue 只点名了前三处，round-0 审 grep 出后两处）：

| # | 站点 | 现状 |
|---|---|---|
| 1 | `scripts/scheduler_state_index_copyback_replay.py:766` `_refuse_root_conflicts` | `reference == destination` → `roots_identical` |
| 2 | `services/orchestrator/run_tree_copyback.py:46` | `object_root == target_root` → skip |
| 3 | `services/tile_publisher/forcing_copyback_backfill.py:397` `_reject_same_copyback_root` | `copyback_root != object_store_root` → return |
| 4 | `services/tile_publisher/publisher.py:737` `_copyback_run_products` | `copyback_root == object_store_root` → skip |
| 5 | `services/tile_publisher/publisher.py:875` `_copyback_qdown_products` | 同上 |

`Path.resolve()` 能折叠 symlink 别名（symlink 形状今天确实被拦住），但**折叠不了「realpath 不同、inode 相同」的别名**：bind mount，或把同一个 NFS export 挂到第二个挂载点。

站点 1/2 的后果不是 fail-closed，是**自死锁**：别名被判成「两个不同 root」放行 → `merge_state_snapshot_index_copyback`
先取 source 侧 `provider_destination_lock`（`packages/common/state_manager.py:1894`），再在其内部取
destination 侧的**同一个 lockfile**（`:1954`）。该锁默认 `blocking=True`（`packages/common/provider_atomic.py:330`）
且不可重入（`:162` 进程内 registry key 是路径字符串 + `:219` 阻塞态不带 `LOCK_NB`），于是进程**无限挂起并持有 private index 锁**：
不退出、不产 receipt、无结构化 reason。同时该锁被长期占用，会让默认 `blocking_lock=False` 的写者
（`atomic_replace_provider_bytes`、`scheduler_file_provider_refresh` 的恢复路径）持续以 `provider_already_running` 失败——饥饿。

站点 3/4/5 不进双锁路径，后果是「误把别名当成不同 root，跨『两个 root』复制」。

当前 node-22 挂载表无别名（issue 已只读复核），**无 live trigger**；这是加固，不是在线故障。

## What Changes

- **新增单一身份探针** `packages/common/safe_fs.py::directory_identity_no_follow(path) -> tuple[int, int]`：
  复用既有 `_open_directory_no_follow` 的逐分量 no-follow fd 语义 + `os.fstat(fd)`，返回 `(st_dev, st_ino)`。
  不新增任何 follow 面。五处守卫**都**经由这一个 helper 取身份 —— 单一缝位既是生产实现也是红证注入点。
- 五处 same-root 判定由「resolve 后字符串相等」改为「身份相等」，命中**既有的**结构化分支：
  `roots_identical`（replay）/ `copyback_root_matches_object_store_root`（run_tree 与 publisher 两处 skip reason）/
  `COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT`（backfill，`details.reason` 仍为 `copyback_root_matches_object_store_root`）。
  **沿用现有 reason，不新增 error code。**
- overlap（父子包含）判定**保持字符串比较不变** —— inode 判等表达不了「一个是另一个的子目录」。
- 探针只作用于**已 `.resolve()` 的路径**（见 design 的硬约束：no-follow walk 会拒绝末段 symlink，raw 路径不可探）。
- 探针失败 → 各站点**既有的**结构化错误，fail-closed，每个调用点的宽松/严格姿态逐一钉死（design 表格）。

## Non-Goals

- **不改锁语义**。issue 的「备选」方案（把 `merge_state_snapshot_index_copyback` 外层锁改 `blocking=False`）**明确不采纳**：
  它会把原本合法排队的并发 merge 变成立即 `provider_already_running` 失败、需要调用方补重试——这是并发行为变更搭车加固；
  且它只治症状，别名 root 仍会被误判为不同 root 继续跨「两个 root」复制。推荐项（守卫层身份判定）已独立满足全部验收锚点，
  包括 ≤5s 结构化返回（别名 root 在取任何锁之前就被拒）。issue 该条验收本身带前提（「若采纳备选项」），不采纳不留未满足项。
  记为「已考虑、已拒绝」，不另立 follow-up。
- 不把 `provider_destination_lock` 改成可重入锁（issue 显式 out of scope）。
- **不做 lockfile 级身份判定**。守卫只在 **root 级**比身份；root 之下的别名（例如把某 export 挂在
  `<destination_root>/scheduler/...`）仍能走到同一个 lockfile 上自死锁。issue 把范围明确划在 root 级守卫，
  `packages/common/state_manager.py` 本单一字不动 —— 该残余**另开 follow-up issue 跟踪，不在本单修**。
- 不做运维侧挂载表治理/文档。
- 不动 #1189 本体语义与 PR #1190 已交付的 winning-entry 收敛范围。

## Known Limits（必须写进 PR 与 tasks，不得含糊）

- `(st_dev, st_ino)` 只能证明**同 superblock** 的别名：bind mount 天然同 superblock，必被拦。
  NFS「同 export 二次挂载」在默认 `sharecache` 下共享 superblock ⇒ 被拦；显式 `nosharecache` 双挂载会给出
  相同 fileid 但**不同 `st_dev`** ⇒ 仍然绕过。规格与 PR 一律只声称「same-superblock aliases」，**不得**声称覆盖全部挂载别名。
- **别名造成的父子 overlap 仍然检测不到**（overlap 按 issue 要求保持字符串判定）。这是设计取舍，显式声明。
- **不存在可移植、免 root 且「同时存在」的「不同 realpath / 同 inode」目录对**（目录不能 hardlink；symlink 会被 `resolve()` 折叠，
  且 no-follow walk 直接拒绝末段 symlink；bind mount 需 root；macOS 大小写折叠在 Linux 不成立）。
  唯一可移植的真实构造是**时序上先后**的一对：`os.rename` 前后同一 inode 出现在两个不同 realpath 上（round-0 实测）——
  它足以给 helper 层做真判别力证明，但造不出「两条路径同时并存」的守卫层输入。
  红证因此仍走三层证据链（design「红证构造」节），真实 bind mount **未经实机验证**。
