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

Repair intensity：**high**。文件身份、共享读原语、持久状态与并发重试同时被触及；首个可复用的
P0/P1 模式必须先做 invariant inventory，不能只补审查点。

### Risk packs considered

Core packs：

- Public API / CLI / script entry：**selected** —— `SafeFilesystemError.kind` 是共享调用方可见的错误契约；
  不改变任何 CLI 形状。
- Config / project setup：**not selected** —— 不读写配置，也不改变部署或初始化。
- File IO / path safety / overwrite：**selected** —— 保留 no-follow、containment、regular-file 与 inode 一致性拒绝；
  仅由 journal 调用方吸收结构化的正常原子替换。
- Schema / columns / units / field names：**selected** —— 新增稳定的 `kind="identity_changed"` 取值，既有取值不变。
- Auth / permissions / secrets：**not selected** —— 不涉及主体、凭据或权限判定，现有路径拒绝不得弱化。
- Concurrency / shared state / ordering：**selected** —— cycle 写窗 owner、跨线程 cache 命中与 stat→open 竞态是本 change 核心。
- Resource limits / large input / discovery：**selected** —— 总尝试次数固定为 3、无 sleep，重试耗尽必须 fail-closed。
- Legacy compatibility / examples：**selected** —— owner 的单线程快路径、所有既有 error kind 与非 journal 调用方行为保持不变。
- Error handling / rollback / partial outputs：**selected** —— 只重试 identity change，其他拒绝原样传播；异常退出清 owner。
- Release / packaging / dependency compatibility：**not selected** —— 无依赖、打包或运行时版本变化。
- Documentation / migration notes：**selected** —— 原语 docstring、OpenSpec 与 PR 明确区分一致性拒绝和 symlink 防御，并记账 node-22 限制。

NHMS domain packs：

- Geospatial / CRS / basin geometry：**not selected** —— 无空间数据或几何语义。
- Hydro-met time series / forcing windows：**not selected** —— cycle key 仅作为 journal 身份，不改变 forcing 时间窗。
- SHUD numerical runtime / conservation / NaN：**not selected** —— 不触及求解器或数值结果。
- PostGIS / TimescaleDB domain behavior：**not selected** —— file repository 专属，DB-free。
- Slurm production lifecycle / mock-vs-real parity：**selected** —— journal 的 submit-once/resume 判据会影响调度；
  以确定性本地并发测试闭环，node-22 NFS 时序确认受既有禁令挂起（D6）。
- External hydro-met providers / snapshot reproducibility：**not selected** —— 不改变 provider 数据发现或快照身份。
- Run manifest / QC provenance：**not selected** —— 不改变 manifest/QC 格式、签名或接纳规则。
- Published NHMS artifacts / display identity：**not selected** —— 不触及发布、object URI 或展示面。

原先的项目化简称映射为：`correctness-durable-state` → Concurrency + Error handling，
`blast-radius-oracle-integrity` → File IO + Legacy compatibility，`concurrency-determinism` →
Concurrency + Resource limits；不是额外 vocabulary。

### Invariant Matrix

- **Governing invariant**：只有当前线程拥有目标 source/cycle 的 cycle 写窗时才可免 fingerprint；
  journal 读只吸收有界的正常 inode replacement，任何其他安全拒绝及重试耗尽均 fail-closed。
- **Source-of-truth identity/contract**：`(thread ident, normalized source_id, cycle_segment)` owner tuple；
  `SafeFilesystemError.kind == "identity_changed"`；总尝试常量 `3`。
- **Producers**：`_locked_cycle_write` 唯一设置/清除 owner；`open_file_no_follow` 唯一产生新 kind。
- **Validators/preflight**：`_cycle_rows` 比较完整 owner key；journal chokepoint 只按 kind 分流。
- **Storage/cache/query**：`_cycle_rows_cache` 的 fingerprint / `None` entry 与
  `_read_bytes_limited_cached` 的 stat-signature cache。
- **Public routes/entrypoints**：`FileOrchestrationJournalRepository` 的 journal/jsonl 读者；
  `safe_fs` 共享 reader 保持一次失败即抛。
- **Frontend/downstream consumers**：scheduler submit-once/resume/candidate-state 消费者；PG repository 与展示面不变。
- **Failure paths/rollback/stale state**：window body/opening exception 清 owner；持续 replacement 第 3 次后原异常抛出；
  symlink、非 regular、containment、非匹配 kind 一次即抛。
- **Evidence/audit/readiness**：确定性 barrier/monkeypatch 红证、变异矩阵、safe_fs 全调用面测试、
  10 次同-cycle hammer；node-22 NFS 确认按 D6 显式挂起。
- **Regression rows**：
  - owner + 同 source/cycle + 热 cache → 不算 fingerprint，既有快路径不变；
  - non-owner / wrong cycle / identity_changed 连续出现 → 重校验或最多 3 次后 fail-closed；
  - symlink、非 regular、containment、其他 kind、非 journal safe_fs caller → 不重试且原契约不变。

### Boundary-surface checklist

- Shared helper roots：`packages/common/safe_fs.py` 只新增判别位，不内置 retry；全调用模块做并发 replace 普查。
- Read surfaces：journal cached chokepoint 覆盖 optional JSON 与 JSONL；三处旁路逐一裁定。
- Write/overwrite surfaces：cycle 原子 replace 与 `_locked_cycle_write`；不改变写格式、flock 或两次 cache clear。
- Stale-state/idempotency boundaries：非 owner 必须 fingerprint；owner key 必须精确到 source/cycle；重试无副作用。
- Unchanged downstream consumers：PG repository、manifest fallback、所有非 journal safe_fs 调用方、#1567 fingerprint 强度。

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
- `_locked_cycle_write`（def `:6948`，`with self._write_lock:` 在 `:6949`）—— cycle 写窗的唯一上下文管理器，可被屏障挂住。
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
| `:6583` | `_next_sequence`（def `:6582`） | 否 | 否 |

该表由 AST 遍历产出（对每个含 `with self._write_lock:` 的函数扫 `_cycle_rows*` /
`_cycle_file_lock*` 调用），不是肉眼枚举；实现者须以同一手法复核而非采信本表。

也就是说 8 个站点里**只有 1 个**满足免指纹的前提。其余 7 个持锁时，
免指纹的两条依据都不成立——这不是并发才有的问题，**单线程下同样是错的**，
只是恰好没有读点在这 7 个窗口内调 `_cycle_rows`（已核：这 7 处内部无 `_cycle_rows` 调用），
所以今天没爆。

因此正确的判据不是「我是否持有 `_write_lock`」，而是「**我是否在这个 cycle 的写窗内**」。

**标记形状：`self._cycle_write_owner: tuple[int, str, str] | None`
= `(threading.get_ident(), <归一化后的 source_id>, cycle_segment)`，不是裸线程 id。**
`source_id` 必须存 `_normalize_file_source_id`（`:9730`）的输出——该函数非恒等
（`packages/common/source_identity.py:5-9`：`GFS→gfs`），比较侧 `:4090` 比的正是归一化值，
且 flock 本身也键在归一化 id 上（`:6967`）。存原始参数会让非规范 id 开的窗静默失去快路径。

裸 ident 会留下第三种「判据为真但理由不对」：owner 线程在 C1 的写窗内读 **C2** 的行，
判据仍返回真、仍免指纹，而 C1 的 flock **不保护 C2**——另一进程可以正在写 C2。
今天在生产上不可达，但**不可达的理由不是「所有读点都显式传本窗 cycle」**——那句是错的。
窗内可达的 5 个 `_cycle_rows` 读点里有 2 个是**自行推导** cycle 的：
`_hydro_run_for:6189`（从 run_id 解析）与 `_pipeline_job_for_id_unlocked:1056`
（从**持久化的** job 记录 `:1052-1053` 推导，不是从本窗）。
它们与本窗 cycle 一致，靠的是另一条不变量：**job_id / run_id 自身编码了它的 cycle**
（`_source_cycle_from_cycle_id:8782` 经 `_cycle_id_for_file_source` 往返并拒绝不匹配）。
把理由写准很重要：**将来若改动 job-id 的 cycle 作用域，这条就会从不可达变成可达**，
而那时按错误的理由去读 D1 会以为没事。真发生分歧时，keyed 标记让那条读丢掉快路径去重算——
慢，但绝不会错。这正是本 change 要根除的缺陷类：
一个判据恰好为真、而它声称的前提并不成立。keyed 标记比裸 ident 只多两个元素，
却让判据字面等于它的含义，故采纳。

**放置位置：标记必须是既有 `try:`（`:6953`）内的第一条语句。**
`_ensure_root_unlocked()`（`:6952`）在 `try` **之外**且会抛
（`:7113-7121` → `OrchestratorError("FILE_JOURNAL_WRITE_FAILED")`）。
若在它之前置标记，那条路径永不进 `finally`，标记就带着一个即将被线程池回收的 ident 泄漏出去——
正是 #1595 验收项 4 与本 change spec 明令禁止的形态。
置于 `try` 内第一条，既由既有 `finally` 兜底、又不需要新开 try/finally；
锁获取与 `try` 之间的那两句（cache clear、root ensure）本身不读 cache，无快路径可发。

- 采纳该收窄后，#1595 验收项「所有 `with self._write_lock:` 站点的 owner 标记成对性」
  的满足方式变为：**只有一个站点参与，成对性由单一 contextmanager 的 `finally` 结构性保证**，
  另加一条结构守卫断言 `_cycle_write_owner` 的赋值只出现在 `_locked_cycle_write`
  与 `__init__` 内（`__init__` 只置 `None`）。
- `_write_lock` 是普通 `Lock`（`:544`），不可重入，因此 `_locked_cycle_write` 不可能嵌套
  （嵌套即死锁），**朴素 set/clear 成立，不需要计数器**。这一点必须由测试锁死
  （若将来换成 `RLock`，朴素 set/clear 会在嵌套退出时提前清空标记）。

**未采纳的备选**：`threading.RLock` + `_is_owned()`。依赖 CPython 私有 API，
且 RLock 的可重入语义会掩盖真实的重入 bug（今天的 `Lock` 让嵌套立刻死锁暴露，是特性不是缺陷）。

### D2 — `_locked_cycle_write` 的两次全局 `.clear()`：**保留，且入口那次是正确性前提而非性能项**

`:6950-6951`（入口）与 `:6957-6958`（`finally`）各做一次全局 `self._cycle_rows_cache.clear()`，
与 source/cycle 无关，cohort X 的写会打空 cohort Y、Z 的 entry。

**本条裁定在 fixture 评审中被推翻过一次，此处是更正后的版本。**
初版写「正确性由 fingerprint 校验保证，不依赖 clear 粒度，所以 clear 是纯性能项」。
**这句是假的**，实测依据：

`_cycle_rows_cache` 的 entry 可以带 `fingerprint=None`，而 `None` **永远无法通过**
`:4111` 的 `cached[0] == fingerprint` 比较（右侧是真元组时恒假）。三个存入点：

| 存入点 | 何时存 `None` |
|---|---|
| `:4162` `_cache_cycle_rows(..., fingerprint=fingerprint)` | `in_write_window` 为真时（`:4104-4108`） |
| `:4233-4237` `_cycle_rows_by_model_unlocked` | 形参上 `fingerprint=None` 恒定，但**该 store 全仓不可达**——被 `:4232` 的 `if include_direct_jobs:` 挡住，而 6 个调用点（生产 5 + 测试 1）**无一**不传 `False`。**不构成 D2 的依据**，见 D10.5 |
| `:6839` append hook | 无条件 `(None, updated)` |

（D2 的结论只靠 `:4162` 与 `:6839` 两处成立，第三行是不可达的死代码——
这一格我先后写错过三版，见 D10.5，**引用 D2 时不要把它算进依据**。）

于是这些 entry **只能**经 `:4111` 的 `in_write_window` 那一支被命中，
而那一支**不做任何校验**。让 owner 快路径安全的不是「重算指纹」，
而是**入口那次 clear 把进窗前的一切 entry 抹掉了**。反例（单线程、同 cycle）：

1. 窗外 `_cycle_rows(C1)` 算出真指纹 F1，miss，存 `(F1, rows_old)`；
2. 另一进程（#1600 列举的 CLI 路径）写 C1 的 journal；
3. 同一线程进 `_locked_cycle_write(C1)`。若入口 clear 被停掉，缓存仍持 `(F1, rows_old)`，
   此时该线程是 owner → 判据为真 → 免指纹 → `:4111` 返回 `rows_old`，**陈旧且未校验**。

**更正后的裁定**（三条，须分开读）：

1. **入口 clear（`:6950-6951`）是 owner 快路径的正确性前提**，不是性能项。
   任何收窄都必须保证：进窗时该 cycle 的既有 entry 被清掉。**不得以「纯性能」为由改动它。**
2. **出口 clear（`:6957-6958`）才是粒度自由的**——它只影响窗后的重算量。
3. 二者**都保留不动**。收窄成前缀失效要在 4 元组 key 上做前缀匹配，
   与 `_apply_record_to_cycle_rows_cache:6819-6826` 已有的按 key 遍历交叠，
   属于 KISS 意义上不该在正确性修复里夹带的改动；
   issue #1595 也自己写明「若它引入任何语义风险，优先保留全局 clear」。

该缺陷**pre-existing 且非本 change 引入**（初版 D2 的错误是把它描述反了，不是造出来的）。
出口 clear 的性能收窄已路由 [#1658](https://github.com/DankerMu/SHUD-NWM/issues/1658)，
其正文把上面第 1 条写成硬约束：否则执行 follow-up 的人会在一个假前提上收窄入口 clear，
那才是真会出事的地方。

对应地，tasks 3.4 与 spec 的对应 scenario 必须限定在**非 owner 读**上：
「正确性不依赖 clear 粒度」这句只对非 owner 读成立，对 owner 快路径为假。

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

#### D7.1 — 实现期逐模块普查结果

判定词：**AFFECTED** 表示合法并发原子替换被误报、调用方应吸收；**GUARDED** 表示锁/CAS/快照或
既定 fail-closed 已正确处理；**NO CONCURRENT WRITER** 表示同键写者由阶段/路径/生命周期隔离；
**PRIMITIVE** 表示定义层，不能替调用方选择策略。逐模块结果如下（行号按实现期 HEAD 复核）：

| 模块 | 判定 | 同键 writer / 现有边界的依据 |
|---|---|---|
| `packages/common/evidence_io.py` | GUARDED | `:106/:326` 固定同一 fd，并在 `:94-141/:320-361` 做 fstat/digest/长度身份校验，变更按证据不稳定拒绝。 |
| `packages/common/manifest_index.py` | NO CONCURRENT WRITER | `:97-116` 读 stage index；`chain_manifests.py:317-340` 在 array submit 前写完，worker 启动后才消费。 |
| `packages/common/object_store_forcing.py` | **AFFECTED → #1660** | `:503` 直读 station CSV；`forcing_producer/producer.py:1970-2005,2101-2104` 对同 key 原子 replace；`:541-556` 将竞态误报为 malformed。 |
| `packages/common/object_store.py` | GUARDED | `:191-239` / `:207-214` 是泛型 read/write primitive；是否吸收必须由具体 caller 裁定，不能在这里全局重试。 |
| `packages/common/provider_atomic.py` | GUARDED | `:99/:135/:365` 的读由 `:117-145` preimage→read→postimage/digest 或 `:342-421` flock+CAS 包住。 |
| `packages/common/rollback_execution_binding.py` | GUARDED | `:194-198` 读 active/archive binding；`:93-161` 原子写由 migration 的 rollback execution lock 串行，身份变化按权限协议 fail-closed。 |
| `packages/common/safe_fs.py` | PRIMITIVE | `:277-368` 定义 no-follow reader，`:138-208` 定义 atomic writer；本 change 只暴露 kind，原语自身不重试。 |
| `packages/common/state_cli.py` | GUARDED | `:493/:966-975` 读提交前 index 或 completed-run checkpoint；publish admission 在 `:719-765/:879-907` 重验 hash。 |
| `packages/common/state_manager.py` | GUARDED | `:243/:2800-2804` 读 IC/index；index 更新用 `:1762-1771/:3026-3082` flock + `:2807-2834` CAS，unlocked lookup 有意报 unreadable。 |
| `scripts/audit_first_cycle_initial_state.py` | NO CONCURRENT WRITER | `:231/:402/:464` 审计既有 registry/package 对象；本脚本只写独立 receipt，读异常全部阻断。 |
| `scripts/scheduler_file_provider_refresh.py` | GUARDED | `:650-748` 从 refresh flock 和 preimage/digest 开始；receipt publication 的 `:1532-1565` 也在 destination lock 内。 |
| `scripts/validate_two_node_docker_source_trust.py` | NO CONCURRENT WRITER | `:373-401` 只在同一 CLI 的 publication-failure 路径读自身 PASS，`:308-340/:424-441` 顺序原子写。 |
| `services/orchestrator/chain_manifests.py` | GUARDED | `:288-314` 比对 control/worker registry mirror；任何不可读/不等直接 `SCHEDULER_REGISTRY_MIRROR_MISMATCH`。 |
| `services/orchestrator/file_orchestration_journal.py` | **AFFECTED，本 PR 修复** | cached chokepoint `:4623` 与 `:6789-6850/:6986-6998` 同 cycle 原子 writer 并发；仅此读点加有界重试。 |
| `services/orchestrator/scheduler_file_providers.py` | GUARDED | `:1674-1723` 读 provider；renewal `:1777-1823` 走 snapshot，`:1756-1774` 写走 lock+CAS。 |
| `services/orchestrator/scheduler_generation.py` | NO CONCURRENT WRITER | `:735-844` 每 planning pass 加载一次 operator cutover declaration；缺失/漂移成为 `_load_error` 并阻断依赖候选。 |
| `services/orchestrator/source_cycle_raw_manifest.py` | GUARDED | `:386-519` 在 destination lock 内重读 source 并逐字节比对；`:587-611` target writer 原子替换，变化显式报 `source_manifest_changed_*`。 |
| `services/production_closure/e2e_validation.py` | NO CONCURRENT WRITER | `:1109-1122` 读 lane 自己已产出的 SHUD raw output；同 lane 顺序写且既有路径先拒绝。 |
| `services/production_closure/object_store_validation.py` | GUARDED | `:1943-1960` 读自建 staging；`:1927-1940` 顺序写新路径，workspace-empty 与 prefix identity 先钉死。 |
| `services/production_closure/readiness_dependency_summaries.py` | NO CONCURRENT WRITER | `:98-159` 聚合既有 summary root；模块无同路径 writer，读失败即 blocked。 |
| `services/production_closure/readiness_scheduler_evidence.py` | NO CONCURRENT WRITER | `:270-344` 聚合既有 scheduler evidence；无同 lane 并发 publisher 契约，读失败即 blocked。 |
| `services/production_closure/readiness_shared_artifacts.py` | NO CONCURRENT WRITER | `:253-335` 读外部 proof；`:135-147` 写的是另一 readiness bundle path。 |
| `services/production_closure/readonly_db_validation.py` | NO CONCURRENT WRITER | `:1211-1276` 读既成 source bundle 并复核 artifact hash/run_id；无并发 bundle publisher lane。 |
| `services/production_closure/scale_validation.py` | NO CONCURRENT WRITER | `:885-934/:1379-1406` 读外部 contract/threshold 输入；本模块只写另一 lane evidence。 |
| `services/production_closure/two_node_e2e_evidence.py` | NO CONCURRENT WRITER | `:1024-1076/:3253-3309` 读 producer evidence 并复核 approved-root/run/hash；模块无同键 writer。 |
| `services/production_closure/two_node_e2e_manual_ops_lane.py` | NO CONCURRENT WRITER | `:840-914` 读 manual receipt 后校验 sha256；无同路径 publisher 在 lane 内定义。 |
| `services/production_closure/two_node_e2e_readonly_db_lane.py` | NO CONCURRENT WRITER | `:896-974` 读 readonly DB artifact 后校验 metadata/hash；无同路径 writer。 |
| `services/slurm_gateway/real_backend.py` | NO CONCURRENT WRITER | `:1736-1755` 读 Slurm 外部 log；本模块 workspace writer 是 create-exclusive，不 replace log。 |
| `services/tile_publisher/publisher.py` | GUARDED | `:2290-2324` 从既有 rollback backup 读到新 clone path；source/target 不同，且有 symlink/type/tree-count guards。 |
| `workers/model_registry/qhh_production_bootstrap.py` | NO CONCURRENT WRITER | `:2336/:2387` 读 no-mutation-expected QHH/package 输入；derived JSON 是同进程顺序生成。 |
| `workers/shud_runtime/runtime.py` | GUARDED | `:2188-2205/:2644-2661` 读 manifest/checksum 约束的 forcing 与本 run staging；读后再次 hash，变化应 fail-closed。 |

普查结论：除本 PR 的 journal 外，仅新确证 station CSV display 直读竞态，已路由
[#1660](https://github.com/DankerMu/SHUD-NWM/issues/1660)；没有第三个 AFFECTED 模块。
泛型 `object_store`、`state_manager` 与 provider 系列虽有原子 writer，但其调用契约要求 lock/CAS/身份拒绝，
不应把 journal 的重试政策扩散过去。

### D8 — 与 #1567 的交叉裁定（#1595 验收项）

#1567 的验收标准列有「`in_write_window` 免指纹命中的处置有明确裁定」，
并给出备选「承认写窗内 flock 已排除外部写者、篡改场景不适用，在注释里钉死」。

**本 change 使该备选在修复后成立，但成立范围比 #1567 设想的窄得多。**
修复后 `in_write_window` 为真当且仅当**本线程正在这个 cycle 的写窗内**，
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
   死代码本身是 out-of-scope finding，已路由
   [#1659](https://github.com/DankerMu/SHUD-NWM/issues/1659)，本 change 不动。
2. 重试只覆盖 `_read_bytes_limited_cached`。若将来新增一条绕过该 chokepoint 的 journal 读路径，
   它不带重试——与 F-a 的 event lane carve-out 同形的**分层保证**，是声明的边界不是覆盖的情形。
3. 本 change 不改变「读者不取 flock」这一架构选择。跨进程读 vs 写的竞态被重试**吸收**，
   不是被**消除**；攻击者仍可通过持续 replace 把重试耗尽，此时行为是 fail-closed。
4. 0.38% 是饱和微基准数字，**不是生产命中率**。生产真实频次未测量，本 change 不声称改善幅度。
5. `_cycle_rows_by_model_unlocked` 的 `_cache_cycle_rows`（`:4233-4237`）
   **是全仓不可达的死代码**——被 `:4232` 的 `if include_direct_jobs:` 守住，
   而它全部 6 个调用点（生产 `:1581`/`:2618`/`:2970`/`:3307`/`:6853`
   + 测试 `tests/test_gateway_reconcile.py:4764`）**无一**不显式传 `include_direct_jobs=False`；
   形参默认值 `True`（`:4171`）无人使用。即该函数的整个 `include_direct_jobs=True` 分支是死的。
   **这是本条的第三版描述，前两版都是我写错的**：
   v1「一段永远命不中的缓存写入」——假（措辞把不可达说成命不中，且当时以为它会存）；
   v2「窗外也存、窗内 owner 可命中」——也假（它根本不存）。
   v3 才是实测：不是命不中，是**从不写入**。
   **已按该准确口径路由 [#1661](https://github.com/DankerMu/SHUD-NWM/issues/1661)，本 change 不动。**
   它**不是** D2 的依据——
   D2 只靠 `:4162` 与 `:6839` 成立。

### D11 — fixture 评审中被推翻/收紧的裁定（保留记录，避免后续重新发明）

| 初版 | 终版 | 推翻依据 |
|---|---|---|
| owner 标记置于 `_ensure_root_unlocked()` 之前 | 置于既有 `try:` 内第一条 | `_ensure_root_unlocked`（`:6952`）在 `try`（`:6953`）之外且会抛，那条路径永不进 `finally`，标记泄漏给线程池复用的下一个任务——违反本 change 自己的 spec |
| owner 标记 = 裸 `threading.get_ident()` | `(ident, source_id, cycle_segment)` | 裸 ident 下「C1 窗内读 C2」判据仍为真而 C1 的 flock 不保护 C2；今天不可达，但正是本 change 要根除的「判据为真、前提不成立」缺陷类 |
| D2「正确性不依赖 clear 粒度，clear 是纯性能项」 | 入口 clear 是正确性前提，仅出口 clear 粒度自由 | 窗内 entry 带 `fingerprint=None`、只能经免校验支命中；让 owner 快路径安全的是入口 clear 而非重算指纹（反例见 D2） |
| 红证：B 读「已热 cache key」 | 预热必须发生在 A 进窗**之后** | `_locked_cycle_write` 入口就 clear，进窗前预热的 entry 已被抹掉，构造 pre-fix 即绿、变异体结构性不可能红 |

前两条与第四条来自 fixture 评审，第三条推翻的是我自己写的裁定。
第四条的陷阱是从 #1595 验收标准原文（「已热 cache key」）照抄来的——
**验收标准里的构造描述同样要当作待验证的断言，不能照抄进 tasks**。

### D12 — fixture 修复自身引入的缺陷（第二轮复核所得）

第一轮修复在闭合 2 条 P1 的同时新造了 4 条，全部已在第二轮闭合。记录形状，因为它们是同一类错误：

| 新造缺陷 | 形状 |
|---|---|
| 3.1 第 3 步未禁止经同一实例写 API 改写源文件 | 那会让 B 阻塞在 A 正持有的 `_write_lock` 上——**红证构造自己死锁**，吃满 30s 预算拖垮套件 |
| 3.2b 未指定 oracle | 窗口以空缓存开始，窗内首读必然新鲜，用取值新鲜度当 oracle 会让 M2 恒绿——**与被它取代的 P1-2 是同一种说谎变异体** |
| 标记存原始 `source_id` | 比较侧比的是归一化值，非规范 id 开的窗静默失去快路径 |
| 4.5 同一格连写错三版 | v1「永远命不中」→ v2「窗外也存、窗内可命中」→ v3（实测）**该 store 被 `:4232` 守死、全仓 6 个调用点无一触发，是死代码**。三版分别错在：把不可达当命不中、没查守卫、没查调用点实参。**每一版我都先断定结论再补理由**，见 D10.5 |
| D1 的不可达理由写成「所有读点都显式传本窗 cycle」 | 5 个窗内读点里 2 个自行推导 cycle；真正的不变量是「job_id / run_id 自身编码 cycle」。结论对、理由错——理由错会让将来改 job-id 作用域的人误以为安全 |

**教训 1**：修复一个「构造在某条路径上不成立」的缺陷时，新构造必须**沿同一条路径重新走一遍**，
而不是只检查它是否覆盖了原缺陷。前两条都是新构造在**另一条**路径上失效，
第二条更是把刚拆掉的说谎变异体在隔壁重新装了一遍。

**教训 2（本轮最贵）**：4.5 那一格我连写错三版，每版都是「先断定结论、再补理由」。
v1 凭印象说「永远命不中」；v2 被指出后改成「窗外也存、窗内 owner 可命中」——
仍然没去看那个 `if include_direct_jobs:` 守卫；v3 才实测出**它根本不写入**。
连评审也只走到 v2 的一半（指出守卫存在，却漏了「五个调用点**全部**传 `False`」这一格）。
**纪律：断言一段代码「会 / 不会」发生某事之前，必须把它的守卫条件与全部调用点实参都查一遍**——
读 docstring 不够，docstring 描述的是**意图**，而这里意图（窗内 memo）与实际（死分支）恰好不一致。
一个基于假前提的 follow-up issue，会让后来者去改一个不存在的问题，比不立单坏得多。

**教训 3**：D1 的结论对但理由错，两轮评审都没拦住（第一轮复核了表格，没复核理由句）。
结论正确会让理由的错误特别难被发现——**「所以今天没爆」这类句子必须单独验证，
不能因为结论没错就跳过**。
