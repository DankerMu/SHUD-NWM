# Design — journal-concurrent-read-integrity

所有行号锚定 `origin/master` @ `8045615e`（issue 正文锚在 `c2439f62`，已被 #1636 等推移，
下文全部重新核对过；**任何后续引用必须按符号名 grep 复核，不得沿用 issue 正文的行号**）。

## 风险三角（risk triage）

| 轴 | 判定 | 依据 |
|---|---|---|
| Fixture level | **expanded** | 两条 pre-existing 并发缺陷合批；触及共享原语（`safe_fs`）与 submit-once 判据；红证必须是确定性并发构造 |
| Blast radius | 高 | `safe_fs` 是 100+ 调用点的共享原语；journal 是 node-22 调度面唯一耐久状态 |
| Reversibility | 中 | 纯进程内逻辑，无迁移、无数据格式变更，revert 即回退 |
| Oracle 可得性 | **受限** | 见 D6：node-22 实机 NFS 时序确认被既有禁令挡住，本 change 以本地确定性红证交付并显式记账 |

选中的 risk pack：`correctness-durable-state`（判据正确性）、`blast-radius-oracle-integrity`
（共享原语的调用面）、`concurrency-determinism`（红证不得靠 sleep 碰运气）。
未选：`data-migration`（无迁移）、`display-boundary`（不碰展示面）、`db-integrity`（db-free 面）。

## must-preserve

1. **单线程语义逐字节不变**：owner 线程在自己的 cycle 写窗内仍走免指纹快路径。
   修复不得把写路径打回「每次重算」。
2. **`safe_fs` 的全部拒绝行为不变**：symlink 目标、非常规文件、containment 越界、
   父组件 symlink —— 一条都不放行，一条都不重试。
3. **重试有界且耗尽后 fail-closed**：不得出现无限重试或静默降级为「读不到就当空」。
4. **`_cache_lock` 锁序不变**：`file_orchestration_journal.py:548-550` 已钉死
   「持 `_write_lock` 的线程可以取 `_cache_lock`，反向绝不」。新增的 owner 标记不得引入反向取锁。

## Seams under test

- `_cycle_rows`（`:4080`）的写窗判据 —— 可由两线程共享同一 repository 实例 + 屏障驱动。
- `_locked_cycle_write`（`:6947`）—— cycle 写窗的唯一上下文管理器，可被屏障挂住。
- `_read_bytes_limited_cached`（`:4563`）—— journal 读的唯一 chokepoint，
  `_read_optional_json:4620` 与 `_read_jsonl:4643` 都经它。
- `safe_fs.open_file_no_follow`（`:257`）的 `:265` stat ↔ `:275` open 窗口 ——
  可用 monkeypatch 在 `os.open` 上注入一次 `os.replace` 得到确定性红证，无需 sleep。

---

## 裁定

### D0 — 合批边界：为什么是 #1595 + #1600 同 PR，而不是各自一单

#1595 的修复把非 owner 线程从「免指纹」改回「算指纹」。
`_cycle_rows_source_fingerprint`（`:4330`）逐 source segment 做 stat，
这些 stat 与随后的 `_read_optional_json` 读都落在 #1600 那个窗口的上游。
**修 #1595 单独交付 = 提高 #1600 的命中率**，即用一个正确性修复去放大另一个已实测存在的故障。
这与 F-a（#1592 单独交付会让真实 URI 被抹成 `None`）是同一种耦合形状：
不是「顺手一起做」，是「拆开做会交付一个已知更糟的中间态」。

**反向不成立**：#1600 单独交付是安全的（它只减少故障，不影响判据正确性）。
所以若本 change 必须拆分，**唯一合法的拆法是 #1600 先行、#1595 后行**，绝不能反过来。
记录在此，供 round-ceiling split 时直接引用。

### D1 — #1595 修复形状：owner 标记，且**只挂在 cycle 写窗上**，不是全部 8 个 `_write_lock` 站点

issue #1595 的推荐项是「在 8 个 `with self._write_lock:` 站点成对维护 owner 标记」。
**本 change 采纳 owner 标记，但把作用域收窄到 `_locked_cycle_write` 一处——这是对 issue 推荐项的显式偏离。**

理由是判据自己的注释（`:4098-4102`）：

> Inside a locked write window the cycle flock excludes external writers and the append hook
> keeps the cache coherent, so hits are trusted as-is.

免指纹的前提有两条：**cycle flock** 与 **append hook**。实测 master 上 8 个 `_write_lock` 站点：

| `with` 行 | 所属函数（def 行） | 取 cycle flock？ | 窗内跑 append hook？ |
|---|---|---|---|
| `:6949` | `_locked_cycle_write`（`:6948`） | **是**（`:6954` `_cycle_file_lock_unlocked`） | **是**（窗内 `_append_journal_record{,s}_unlocked:6709/:6724` → `_apply_record_to_cycle_rows_cache:6797`） |
| `:4829` | `_iter_reconcile_inventory_records`（`:4823`） | 否（reconcile inventory flock） | 否 |
| `:4885` | `_ensure_reconcile_inventory_migrated`（`:4882`） | 否 | 否 |
| `:4946` | `_prepare_reconcile_inventory_rollback_under_scheduler_lease`（`:4915`） | 否 | 否 |
| `:5096` | `_require_reconcile_inventory_rollback_prepared`（`:5081`） | 否 | 否 |
| `:5127` | `current_generation_scheduler_rollback_blocker`（`:5124`） | 否 | 否 |
| `:5158` | `_complete_reconcile_inventory_rollforward_under_scheduler_lease`（`:5148`） | 否 | 否 |
| `:6582` | `_next_sequence`（`:6582`） | 否 | 否 |

该表由 AST 遍历产出（对每个含 `with self._write_lock:` 的函数扫 `_cycle_rows*` /
`_cycle_file_lock*` 调用），不是肉眼枚举；实现者须以同一手法复核而非采信本表。

也就是说 8 个站点里**只有 1 个**满足免指纹的前提。其余 7 个持锁时，
免指纹的两条依据都不成立——这不是并发才有的问题，**单线程下同样是错的**，
只是恰好没有读点在这 7 个窗口内调 `_cycle_rows`（已核：这 7 处内部无 `_cycle_rows` 调用），
所以今天没爆。

因此正确的判据不是「我是否持有 `_write_lock`」，而是「**我是否在 cycle 写窗内**」。
一个 `self._cycle_write_owner: int | None`，只由 `_locked_cycle_write` 在
`with self._write_lock:` 内置为 `threading.get_ident()`、在 `finally` 清空，
同时修掉 ownership 盲和 scope 盲，且**只有一个站点需要维护成对性**。

- 采纳该收窄后，#1595 验收项「所有 `with self._write_lock:` 站点的 owner 标记成对性」
  的满足方式变为：**只有一个站点参与，成对性由单一 contextmanager 的 `finally` 结构性保证**，
  另加一条结构守卫断言 `_cycle_write_owner` 的赋值语句只出现在 `_locked_cycle_write` 内。
- `_write_lock` 是普通 `Lock`（`:544`），不可重入，因此 `_locked_cycle_write` 不可能嵌套
  （嵌套即死锁），**朴素 set/clear 成立，不需要计数器**。这一点必须由测试锁死
  （若将来换成 `RLock`，朴素 set/clear 会在嵌套退出时提前清空标记）。

**未采纳的备选**：`threading.RLock` + `_is_owned()`。依赖 CPython 私有 API，
且 RLock 的可重入语义会掩盖真实的重入 bug（今天的 `Lock` 让嵌套立刻死锁暴露，是特性不是缺陷）。

### D2 — #1595 附带项：`_locked_cycle_write` 的全局 `.clear()` **保留不动**

`:6950-6951` 与 `:6957-6958` 各做一次全局 `self._cycle_rows_cache.clear()`，
与 source/cycle 无关，cohort X 的写会打空 cohort Y、Z 的 entry。

**裁定：保留全局 clear。** 理由：

1. 它是**语义中性**的——clear 只会导致重算，永远不会给出错值。
2. 修复后正确性由 fingerprint 校验保证，**不依赖 clear 粒度**；
   这一点必须由一条断言锁死（见 tasks 3.4），锁死之后 clear 粒度就是纯性能选择。
3. issue #1595 自己写明「若它引入任何语义风险，优先保留全局 clear」。
   收窄成前缀失效要在 `_cycle_rows_cache` 的 4 元组 key 上做前缀匹配，
   与 `_apply_record_to_cycle_rows_cache:6816-6839` 已有的按 key 遍历逻辑交叠，
   属于 KISS 意义上不该在正确性修复里夹带的改动。

性能收窄另行立 issue，不在本 change 做。

### D3 — #1600 修复形状：journal 读 chokepoint 有界重试 + `safe_fs` 结构化判别位

采纳 issue 推荐项：

- `safe_fs.py:285` 那一处 raise 带上 `kind="identity_changed"`（`SafeFilesystemError.__init__`
  已有 `kind` 参数，`:13`，默认 `"unsafe"`；现有取值 `"unsafe"`/`"io"`/`"indeterminate"`）。
  **只加取值，不改任何既有 kind 的语义，不改任何拒绝行为。**
- 重试落在 `_read_bytes_limited_cached`（`:4563`）——journal 读的唯一 chokepoint，
  `_read_optional_json` 与 `_read_jsonl` 都经它，一处覆盖两条路径。
- 调用方按 **`kind` 字段**分流，**不得按 message 字符串匹配**（脆，且 message 含路径）。

**未采纳**：在 `safe_fs.open_file_no_follow` 内部循环重试。那是共享原语，
在此加重试等于替**全部**调用点（含未来的）默认放宽判据；
而其中一些调用点（产物/receipt 校验）可能宁可要「任何 inode 变动都拒收」的强语义。
判别位加在原语、重试策略留给调用方，是唯一能让两类调用点各取所需的分工。

### D4 — `safe_fs.py:284` 那条检查到底防的是什么（本 change 最重要的一条裁定）

issue #1600 写「不得为此弱化或删除 `:238` 的身份比对：它对 **symlink 掉包** 的防御必须保留」。
**这句话对该检查的作用描述是错的，实现前必须澄清，否则重试会被当成安全回退驳回。**

逐条核对 `open_file_no_follow`（`:257-292`）：

| 攻击 | 被谁挡住 |
|---|---|
| 目标本身是 symlink | `:270` `stat.S_ISLNK(expected.st_mode)`（打开前）+ `:277` `O_NOFOLLOW` 导致的 `ELOOP` |
| 打开窗口内换成 symlink | `:277` `ELOOP` —— `_READ_FLAGS` 含 `O_NOFOLLOW`（`:20`） |
| 目标是目录/设备/FIFO | `:272` `S_ISREG(expected)` + `:282` `S_ISREG(opened)` |
| 父组件是 symlink / 越出 containment | `_open_parent_dir` + `_verify_fd_matches_path`（`:263`/`:286`） |
| **窗口内被换成另一个常规文件** | **`:284` 的 `(st_dev, st_ino)` 比对——只有这一条** |

即：`:284` 挡的是**常规文件掉包**，不是 symlink 掉包（symlink 走 ELOOP，根本到不了 `:284`）。
而 `os.replace` 正是常规文件掉包，**两者在这一层完全同形，这就是缺陷本身**。

进一步：能在窗口内 `replace` 的攻击者，同样能在读**开始之前** replace——
那种情况 `expected` 与 `opened` 都看到攻击者的文件，`:284` **一声不吭**。
所以 `:284` 不是攻击屏障，是**一致性检查**：保证返回的 fd 与 `:265` stat 出来的那个 inode 是同一个，
让依赖 stat 结果（如 size）的调用方不至于拿错对象。

**由此得到重试的安全论证**：有界重试保留了上表**全部五行中的四行**（那四行一次都不重试，
只有 `kind="identity_changed"` 进重试分支），只放宽第五行的一致性检查，且有界、耗尽 fail-closed。
换来的是「正常并发写不再被渲染成 containment 故障」。
这条论证必须写进 spec delta，不能只留在 design 里——否则下一个读这段代码的人会重新把它当攻击屏障。

### D5 — 重试次数与退避

上限 **3 次尝试**（初始 1 + 重试 2），**无 sleep**。理由：
`os.replace` 完成后新 inode 立即稳定，重读一次即可；连续三次都恰好撞进微秒级窗口，
在正常写频下概率可忽略，而在攻击场景下正是应当 fail-closed 的信号。
加 sleep 会把一个微秒级自愈事件变成毫秒级停顿，且给攻击者更长的操纵窗口。

次数必须是**具名常量**并由测试锁死其有界性（注入一个「每次都 replace」的写者，
断言恰好尝试 3 次后抛出，且抛出的仍是 `SafeFilesystemError`）。

### D6 — 验证档位：本地确定性红证；node-22 实机 NFS 时序确认**显式挂起**

issue #1600 的「真实 oracle 路由」写着并发行为最终判据在 node-22 实机
（NFS 上 `os.replace` 与身份比对的时序特征）。

**本 change 不取 node-22 证据**，原因与处置：

- 本会话对 node-22 有既有禁令（#1192 的逐字禁令：不得再跑探针类负载），
  只允许只读挂载/环境查看。竞态探针属于被禁止的一类，**不在预授权范围内**。
- 两条 issue 的验收清单本身**全部是本地可判定的**（屏障/注入驱动的确定性红证），
  node-22 那句是**确认性时序刻画**，不是任何一条验收项。
- 因此：本地闭环交付，**在 design、tasks、PR body 三处显式记账**
  「node-22 实机 NFS 时序确认已挂起——受既有 node-22 禁令阻塞」，
  不得静默缩窄成「不需要」。

需要时由用户显式解禁后补做；这不阻塞合并（预授权范围内），但必须可见。

### D7 — safe_fs 调用点普查：本 change 只做普查与立案，不做跨调用点修复

`grep` 出的读原语调用点遍及 `services/orchestrator/`、`services/production_closure/`、
`packages/common/`、`workers/`、`scripts/`，逾百处。判别条件是
**「该文件存在并发写方，且写方走原子 replace」**——只有同时成立才受本缺陷影响。

本 change 必须产出一张普查表（tasks 4.1），对每个**模块**给出结论与依据。
已确证受影响的是 journal；**普查中新确证的其他调用点，报告立 issue，不在本 change 修**
（全局 CLAUDE.md：out-of-scope findings 报告不修）。
高嫌疑先验（须逐一核实，不得直接采信）：`packages/common/provider_atomic.py`、
`services/orchestrator/scheduler_file_providers.py` ↔ `scripts/scheduler_file_provider_refresh.py`、
`packages/common/object_store.py`、`packages/common/state_manager.py`。

### D8 — 与 #1567 的交叉裁定（#1595 验收项）

#1567 的验收标准列有「`in_write_window` 免指纹命中的处置有明确裁定」，
并给出备选「承认写窗内 flock 已排除外部写者、篡改场景不适用，在注释里钉死」。

**本 change 使该备选在修复后成立，但成立范围比 #1567 设想的窄得多。**
修复后 `in_write_window` 为真当且仅当**本线程正在 cycle 写窗内**，
此时 cycle flock 确实排除了其他写者（含跨进程），append hook 确实维持了 cache 相干——
`#1567` 那句「flock 已排除外部写者」对**这一个**线程成立。
对任何其他线程，判据现在返回假，走完整 fingerprint 校验，
因此 #1567 关心的 `_stat_signature` 指纹强度问题**照常适用于它们**，一分不减。

结论写进代码注释与 spec：免指纹分支的篡改暴露面**变窄了**（从「任何线程」缩到「写窗 owner」），
但 owner 自己的免指纹仍不做篡改检测——那是 #1567 的地盘，本 change 不动。

### D9 — journal 内其余 3 个 `read_bytes_limited_no_follow` 调用点为何不加重试

| 位置 | 读什么 | 裁定 |
|---|---|---|
| `:4587` `_read_bytes_limited_cached` | cycle 文件 / latest view | **加重试**（唯一 chokepoint） |
| `:6769` | 写窗内读 existing 段 | **不加**：在 `_locked_cycle_write` 内，`_write_lock` 排除进程内写者、cycle flock 排除跨进程写者，无并发写方 |
| `:412` | workspace run manifest（外部产物） | **不加**：已 `except … → None` 兜底，是该读点既定的 fallback 语义；若普查确证其有并发原子写方，立 issue |
| `:8910` | object-store run manifest（外部产物） | **不加**：同上 |

`:412` / `:8910` 的裁定依赖 D7 普查结果；若普查推翻「无并发原子写方」，
处置是**立 issue**而非在本 change 扩面。

### D10 — 已声明的已知边界

1. `_next_sequence`（`:6582`）在生产侧**无调用者**（全仓仅 `tests/test_file_orchestration_journal.py`
   5 处引用）。它是 8 个 `_write_lock` 站点之一，本 change 因 D1 收窄而不动它。
   死代码本身是 out-of-scope finding，报告立 issue。
2. 重试只覆盖 `_read_bytes_limited_cached`。若将来新增一条绕过该 chokepoint 的 journal 读路径，
   它不带重试——与 F-a 的 event lane carve-out 同形的**分层保证**，是声明的边界不是覆盖的情形。
3. 本 change 不改变「读者不取 flock」这一架构选择。跨进程读 vs 写的竞态被重试**吸收**，
   不是被**消除**；攻击者仍可通过持续 replace 把重试耗尽，此时行为是 fail-closed。
4. 0.38% 是饱和微基准数字，**不是生产命中率**。生产真实频次未测量，本 change 不声称改善幅度。
