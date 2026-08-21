# Tasks — journal-concurrent-read-integrity

行号锚定 `origin/master` @ `8045615e`。**每一处引用在动手前必须按符号名 grep 复核**——
issue 正文的行号锚在 `c2439f62`，已被后续合并推移。

## 1. #1595 — cycle 写窗 owner 语义

- [x] 1.1 在 `FileOrchestrationJournalRepository.__init__`（`_write_lock` 定义处 `:544` 附近）
  新增 `self._cycle_write_owner: tuple[int, str, str] | None = None`。
  **是三元组 `(thread_ident, source_id, cycle_segment)`，不是裸线程 id**（design D1）。
- [x] 1.2 `_locked_cycle_write`（def `:6948`）置标记：
  **必须是既有 `try:`（`:6953`）内的第一条语句**，
  值为 `(threading.get_ident(), _normalize_file_source_id(source_id, field="source_id"),
  format_cycle_time(cycle_time))`。
  **必须存归一化后的 id**：比较侧 `_cycle_rows:4090` 比的就是归一化后的值，
  而 `_normalize_file_source_id`（`:9730`）非恒等（`packages/common/source_identity.py:5-9`
  把 `GFS→gfs`）。存原始参数会让任何以非规范 id 开的窗**静默失去** owner 快路径——
  方向安全但不可见，正是 #1595 警告的「漏一处 = 悄悄退化成永不免指纹」。
  归一化后标记的身份空间也与 flock 一致（`_cycle_file_lock_unlocked:6967` 同样用归一化 id）。
  在**已有的 `finally` 块**（`:6956-6958`）内清空为 `None`。
  **不得**新开 try/finally，**也不得**置于 `_ensure_root_unlocked()`（`:6952`）之前——
  后者在 `try` 之外且会抛（`:7113-7121`），那条路径永不进 `finally`，标记会带着
  一个即将被线程池回收的 ident 泄漏（design D1、D11 第一行）。
- [x] 1.3 `_cycle_rows`（`:4103`）判据改为
  `in_write_window = self._cycle_write_owner == (threading.get_ident(), source_id, cycle_segment)`
  （`source_id` 已在 `:4090` 规范化、`cycle_segment` 已在 `:4096` 算出，就地可用）。
  **注意 `None == <tuple>` 恒假**，无需额外判空；但要有测试锁死冷实例（owner 为 `None`）走 fingerprint 路径。
- [x] 1.4 更新 `:4098-4102` 的注释：写清楚免指纹的两条前提（cycle flock + append hook）
  只在 **该 cycle 写窗的持有线程**上成立，**并写明入口 clear（`:6950-6951`）
  是这条快路径的正确性前提**——可达的窗内 entry 带 `fingerprint=None`（`:4162`/`:6839`），
  只能经免校验支命中，抹掉进窗前的 entry 的是那次 clear 而不是重算指纹（design D2；
  `:4233-4237` 已由 D10.5 证实是守卫挡死的不可达 store，不得算作依据）。
  并按 design D8 写入与 #1567 的交叉裁定
  （免指纹分支的篡改暴露面从「任何线程」缩到「写窗 owner」，owner 自身的免指纹不做篡改检测，
  那是 #1567 的地盘）。
- [x] 1.5 **禁令**：不得把 `_write_lock` 改成 `RLock`；不得给其余 7 个 `with self._write_lock:`
  站点加 owner 标记（design D1 的收窄裁定）。若实现中发现该收窄不成立，
  **停下来报告**，不要自行扩回 8 站点。

## 2. #1600 — 结构化判别位 + 有界重试

- [x] 2.1 `packages/common/safe_fs.py:285` 的 raise 带上 `kind="identity_changed"`。
  **只改这一处 raise**；`:271`/`:273`/`:278`/`:283` 的 symlink 与非常规文件拒绝
  保持默认 `kind="unsafe"` 不变。
- [x] 2.2 在 `safe_fs.py` 顶部 `SafeFilesystemError`（`:10-15`）的 docstring 或紧邻注释中，
  按 design D4 写清 `kind="identity_changed"` 的确切含义：
  「打开窗口内目标 inode 被替换（常规文件掉包或正常 `os.replace`，**这一层无法区分**）」，
  以及它**不是** symlink 防御（symlink 走 `O_NOFOLLOW` 的 `ELOOP` 与 `S_ISLNK` 检查，到不了这里）。
- [x] 2.3 `file_orchestration_journal.py` 新增具名常量
  `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`（总尝试次数，含首次）。
- [x] 2.4 `_read_bytes_limited_cached`（`:4563`）把 `:4587` 的
  `read_bytes_limited_no_follow(...)` 包进有界重试循环：
  仅当 `isinstance(error, SafeFilesystemError) and error.kind == "identity_changed"` 时重试，
  **无 sleep**；尝试用尽后原样抛出最后一次的异常。
  重试前必须重取 stat 探针**并重置 `signature`**（`:4575` 的 `signature = None` 必须包含在
  重试体内）——否则 `signature` 留着上一轮的值，配上新一轮为 `None` 的 `probe`
  就在 `:4588` 的 `probe.st_size` 上炸。
  缓存查询（`:4583-4586`）**嵌在 `:4581` 的 `if` 内**，把它排除在重试体外需要拆那个 `if`；
  留在重试体内也无害（重 stat 出的新 signature 必然 miss）。
  **两种形状都可接受，选哪种由实现者定并在 PR 说明**——这里只约束
  「`signature` 必须与 `probe` 同轮重置」。
- [x] 2.5 **禁令**：不得按异常 message 字符串分流；不得在 `safe_fs.open_file_no_follow`
  内部加重试；不得删除或弱化 `safe_fs.py:284` 的比对；不得给重试加 `time.sleep`。

## 3. 测试

红证优先落在 `tests/test_file_orchestration_journal_read_cache.py`；新建并发文件须在 PR 中显式列出。

**并发用例统一纪律（每条并发用例都适用）**：复用既有 harness
`tests/test_file_orchestration_journal.py:7921` `_join_all`（daemon 线程 + 30s 总预算 + `stop` 事件）
与 `:7884` 的 `_hammer_until`（在 `_join_all` **之前**），**不要再造第三份 join-with-deadline 逻辑**。
屏障本身也必须带 timeout（`threading.Barrier(2, timeout=…)` / `Event.wait(timeout=…)` 并断言未超时）：
一个因对端提前死掉而永久停在 `Barrier.wait()` 的线程，会**持着 `_write_lock` 加 cycle flock
直到解释器退出**，把后续整个套件拖死。禁止用 `time.sleep` 制造时序。

### 3.1 #1595 红证（确定性）——**预热必须发生在进窗之后**

- [x] 3.1 两线程共享**同一** repository 实例，按以下**严格顺序**（顺序本身是断言的一部分）：
  1. 线程 A 进入 `_locked_cycle_write(C_x)` 并在带 timeout 的屏障上挂住；
  2. **在 A 的窗内**，线程 B 对**另一个** cycle `C_y` 调 `_cycle_rows` —— pre-fix 此次
     判据为真、存入 `fingerprint=None` 的 entry；
  3. 带外改写 `C_y` 的源文件。**禁止经同一个 repository 实例的写 API**
     （`ensure_forecast_cycle` / `upsert_pipeline_job` 等都要取 `_write_lock`，
     而 A 正持着它等屏障 → 死锁 → 吃满 `_join_all` 的 30s 预算并拖垮套件）。
     只允许：直接写文件，或另建**第二个** `FileOrchestrationJournalRepository`
     指向同一 root（它有自己的 `_write_lock`；`C_y` 的 flock 与 `C_x` 的不冲突）；
  4. 线程 B 再次调 `_cycle_rows(C_y)` —— **pre-fix 吃下未校验的陈旧命中，post-fix 重算返回新鲜行**。
  **第 2 步必须在窗内**：`_locked_cycle_write` 在**入口**（`:6950-6951`）就 `clear()`，
  进窗前预热的 entry 已被抹掉，B 必然 miss→重算→返回新鲜行，构造 pre-fix 即绿、
  3.5 的变异体结构性不可能红。#1595 验收标准原文写的「已热 cache key」正是这个陷阱，
  **不要照抄**（design D11 第四行）。

### 3.2 单线程等价性——同样需要窗内两次调用

- [x] 3.2 owner 线程在自己的写窗内仍走免指纹快路径。
  断言方式：spy `_cycle_rows_source_fingerprint`，断言 owner **命中**时调用次数为 0。
  **窗口以空缓存开始，所以必须在窗内调用两次同一 cycle 的 `_cycle_rows`**：
  第一次是 miss（必然算不出命中），第二次才是被断言的那次命中。
  只调一次会让"调用次数为 0"被一次纯 miss 满足，什么都没断言到。
- [x] 3.2b **keyed 标记的判别力**（design D1）：owner 在 `C_x` 的窗内读 **`C_y`** 时
  判据必须为**假**（走 fingerprint 校验）。
  **oracle 必须是 `_cycle_rows_source_fingerprint` 的 spy 调用次数 ≥ 1，不是取值新鲜度。**
  窗口以空缓存开始，所以窗内首次读 `C_y` 必然 miss→重算→返回新鲜值，
  **M2 变异体下同样新鲜**——用新鲜度当 oracle 会让 M2 恒绿，
  和原 P1-2 是同一种说谎变异体。若坚持用取值 oracle，就必须照 3.1 的四步
  在窗内对 `C_y` 走一遍 预热/改写/重读。
  变异体 M2：把标记退化成裸 `threading.get_ident()`、判据只比 ident —— 本条必须转红。

### 3.3 异常路径——两条，缺一不可

- [x] 3.3a 从 `_locked_cycle_write` 的 **yield body** 抛异常后，
  断言 `repository._cycle_write_owner is None`。
- [x] 3.3b 从**进入路径**抛异常：monkeypatch `_ensure_root_unlocked`
  （或其内部的 `ensure_directory_no_follow`）使其抛 `OrchestratorError`，
  断言 `repository._cycle_write_owner is None`。
  **这一条才是 P1 的守卫**：`_ensure_root_unlocked`（`:6952`）在 `try`（`:6953`）之外，
  3.3a 覆盖不到它；标记若放错位置，只有 3.3b 会红。

### 3.4 clear 粒度无关性——**仅限非 owner 读**

- [x] 3.4 断言：cohort X 的写不会让 **另一线程（非 owner）** 读到错值，
  且该断言在入口/出口两次 `clear()` 都被停掉时**仍然通过**。
  三个必须写死的约束：
  1. **只对非 owner 读成立**。owner 快路径的正确性**依赖**入口 clear（design D2 的反例），
     本条不得扩展到 owner 读——扩了会红，且红的原因 fixture 没给解释。
  2. **读必须发生在另一线程的写窗打开期间**。单线程「先写 X 再读 Y」时 owner 为 `None`、
     本来就走 fingerprint 路径，**pre-fix 也过**，等于什么都没守。
  3. **停掉 clear 的机制要写明**：`_cycle_rows_cache` 是普通 `dict`，
     `dict.clear` 不能在实例上 monkeypatch（`AttributeError: 'dict' object attribute 'clear' is read-only`）。
     用一个 `clear()` 为 no-op 的 `dict` 子类替换该属性，或 patch 两个调用点——二选一，写在用例注释里。

### 3.5-3.5b 变异体证死

- [x] 3.5 把判据改回 `self._write_lock.locked()`，3.1 必须转红。
- [x] 3.5b 把标记的置位从 `try:` 内第一条挪到 `_ensure_root_unlocked()` 之前，3.3b 必须转红。

### 3.6-3.9 #1600：**入口一律是 `_read_bytes_limited_cached`，不是 safe_fs 原语**

D3/2.5 明令重试不落在原语内，所以**任何直接对 `read_bytes_limited_no_follow` 写的用例，
post-fix 仍然会抛**——把它当成"post-fix 应返回正确内容"的载体是不可满足的。
3.6/3.7/3.8/3.9 的被测调用一律经 `FileOrchestrationJournalRepository._read_bytes_limited_cached`
（或经它的 `_read_optional_json` / `_read_jsonl`）发起；尝试次数也在这一层计。

- [x] 3.6 **红证（确定性）**：monkeypatch `os.open`（或等价注入点），
  在 `safe_fs.py:265` 的 stat 与 `:275` 的 open 之间强制插入**一次** `os.replace`。
  经 `_read_bytes_limited_cached` 读：pre-fix 稳定抛 `Target file changed while being opened`，
  post-fix 返回**替换后**的正确内容。
- [x] 3.7 **重试有界**：注入"每次都 replace"的写者，
  断言恰好 `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS` 次尝试后抛出，
  且抛出的仍是 `SafeFilesystemError`（fail-closed 未回退）。
- [x] 3.8 **安全语义不回退**：symlink 目标、窗口内换成 symlink（走 `ELOOP`，design D4 表第二行）、
  非常规文件、containment 越界 —— 四类场景**一次都不进重试分支**（断言尝试次数为 1）且仍然抛出。
- [x] 3.9 **按字段而非按串分流**：构造一个 message 含
  `"Target file changed while being opened"` 但 `kind != "identity_changed"` 的
  `SafeFilesystemError`，断言它**不**被重试（尝试次数为 1）。
- [x] 3.9b **原语自身不重试**（对应 safe-filesystem spec 的
  "The primitive itself does not retry" scenario）：直接调
  `safe_fs.read_bytes_limited_no_follow` / `open_file_no_follow`，
  在窗口内注入一次 replace，断言**第一次身份不匹配就抛出**、无第二次 `os.open`。
  没有这条，那个 spec scenario 在树里无对应断言。

### 3.10 端到端锁死——**必须去掉既有 carve-out，不是另加一个新用例**

- [x] 3.10 目标是 `tests/test_file_orchestration_journal.py:8048-8055` 那个 carve-out：
  注释写着「The writer owns a cycle of its own: readers must not open a file that
  is being atomically replaced, which is a pre-existing safe_fs race and not the cache
  defect under test」，实现是 `journal_files` 过滤掉 `writer_segment not in str(path)`。
  **处置二选一，且必须落字**：(a) 去掉该过滤让 reader 真的读被 replace 的文件，
  post-fix 稳定绿，并改掉那条现已不成立的注释；
  或 (b) 保留 carve-out 并写明理由（例如它是 #1380 的 dict-race hammer、oracle 不同），
  同时另建一个同 cycle 读写的用例承担本条。
  **只新建用例而不动那个 carve-out，等于让树里留着一条现在为假的注释**——不接受。
  **证据要求**：选 (a) 时「post-fix 稳定绿」不是一次绿就算——去掉 carve-out 后
  仍存在残余失败面（无节流写者 hammer 下连续三次尝试都撞进微秒窗口）。
  须给出**重复 10 次全绿**的实测输出，不是单次。

### 3.11-3.12 结构守卫

- [x] 3.11 AST 守卫：`_cycle_write_owner` 的**写入**只允许出现在 `_locked_cycle_write`
  与 `__init__` 内（`__init__` 只置 `None`）。扫描必须覆盖
  `ast.Assign` + **`ast.AnnAssign`** + `ast.AugAssign` + `setattr(self, "_cycle_write_owner", …)`。
  **`AnnAssign` 不可省**：task 1.1 写的正是带类型标注的赋值，只扫 `Assign` 会让守卫
  靠节点类型的巧合放行，且放任将来在别处新增同形写入。
- [x] 3.12 **不可重入前提锁死**（design D1）：断言 `_write_lock` 是 `threading.Lock`
  而非 `RLock`——朴素 set/clear 的正确性依赖不可重入。

## 4. 普查与留痕

- [x] 4.1 **safe_fs 读原语调用点普查**（#1600 验收项，design D7）：
  对每个调用模块给出「是否存在并发写方 + 写方是否走原子 replace」的结论与**依据**
  （不是"看起来像"，要指出写方代码位置或说明不存在）。表落在 design.md 新增小节。
  高嫌疑先验须逐一核实、不得直接采信：`packages/common/provider_atomic.py`、
  `services/orchestrator/scheduler_file_providers.py` ↔ `scripts/scheduler_file_provider_refresh.py`、
  `packages/common/object_store.py`、`packages/common/state_manager.py`。
- [x] 4.2 普查中新确证的受影响调用点：**报告立 issue，不在本 change 修**。
  issue 编号回填 design D7。若一个都没有，明确写"零新增"，不得留空。
- [x] 4.3 `_next_sequence`（`:6582`）生产无调用者（全仓仅测试引用）——立 issue，本 change 不动。
- [x] 4.4 `_locked_cycle_write` **出口** `.clear()`（`:6957-6958`）的性能收窄——立 issue。
  该 issue **必须把 design D2 的第 1 条作为硬约束写进正文**：
  **入口 clear（`:6950-6951`）是 owner 快路径的正确性前提，不得以「纯性能」为由收窄**。
  漏掉这句，执行 follow-up 的人会在假前提上动入口 clear——那才是真会出事的地方。
- [x] 4.5 立 issue：`_cycle_rows_by_model_unlocked` 的 `include_direct_jobs=True` 分支
  **全仓不可达**——`:4233-4237` 的 `_cache_cycle_rows` 被 `:4232` 守住，
  6 个调用点（生产 5 + `tests/test_gateway_reconcile.py:4764`）全部显式传 `False`，
  默认值 `True`（`:4171`）无人使用。**立单文本必须用这个口径**（死分支 / 从不写入），
  **不得**写成「永远命不中」或「窗外也存」——那是我先后写错的前两版，
  按错版本立单会让后来者去改一个不存在的缓存命中问题（design D10.5）。
  本 change 不动该分支。

## 5. 验证（Evidence Floor）

以下每一条都必须**实跑并贴出输出**，不得以论证替代测量：

- [x] 5.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`
  —— 全绿；新增用例数与名称列进 PR。
- [x] 5.2 `uv run pytest -q tests/test_orchestration_chain.py` —— 全绿（#1600 Verification 指定）。
- [x] 5.3 `uv run pytest -q $(grep -rl safe_fs tests/)` —— 全绿。
  **改了共享原语，此项为必跑**，不得用 `-k safe_fs` 替代（覆盖分散在各消费方套件里）。
- [x] 5.4 变异矩阵，逐个给出**实测**红/绿（凡填「预期红」而未实测的格子必须标明是推断）：

  | # | 变异体 | 应由哪条转红 |
  |---|---|---|
  | M1 | 判据改回 `self._write_lock.locked()` | 3.1（**前提是 3.1 已按新构造在窗内预热**，否则 M1 结构性不可能红） |
  | M2 | owner 标记退化成裸 `threading.get_ident()` | 3.2b |
  | M3 | 标记置位挪到 `_ensure_root_unlocked()` 之前 | 3.3b（3.3a 覆盖不到） |
  | M4 | 重试上限改为 1 | 3.6 |
  | M5 | 重试条件放宽到全部 `SafeFilesystemError` | 3.8 / 3.9 |
  | M6 | 重试判据改成 message 子串匹配 | 3.9 |
  | M7 | owner 清空从 `finally` 挪到正常路径末尾 | 3.3a |

  M1 那一格是本 fixture 初版的已知陷阱：初版 3.1 在进窗**前**预热，
  被入口 clear 抹掉，导致 pre-fix 即绿、M1 恒绿。落笔前先确认 3.1 的顺序已按新版实现。
- [x] 5.5 `uv run ruff check $(git ls-files '*.py')` —— clean。
  （**不要跑 `uv run ruff check .`**，会命中本地未跟踪的 `skills/` 工具。）
- [x] 5.6 `openspec validate journal-concurrent-read-integrity --strict --no-interactive` —— valid。
- [x] 5.7 全量本地 `uv run pytest -q -m "not e2e and not grib and not integration"`：
  与合并基线对比，**新增红为零**。基线红数必须**自己实测**，不得采信本文件或 brief 里的任何数字。
  **同一时刻只允许一个 pytest 进程**（并发跑会因 CPU 争用产出假红）。
- [x] 5.8 **node-22 实机 NFS 时序确认：挂起。** 受既有 node-22 禁令阻塞（design D6）。
  必须在 PR body 显式记账，不得静默省略。两条 issue 的验收清单本身全部本地可判定，
  故该挂起不阻塞合并。

## 6. 交付纪律

- [x] 6.1 每一处与本 tasks/design 的 departure 写入 PR 的 `偏离记录` 段（what/why/impact）；
  无偏离也要显式写"无偏离"。
- [x] 6.2 design D1（收窄到单站点 + keyed 三元组标记 + 置于 `try` 内首条）、
  D2（两次 clear 都保留；入口 clear 是正确性前提）、D3（重试落调用方）、
  D5（3 次无退避）四条裁定若在实现中被推翻，**停下来报告**，不要自行改道。
- [x] 6.3 本 fixture 经两路只读评审后已修正 2 P1 + 6 P2 + 6 P3（记录在 design D11）。
  **凡本文件与两条 issue 正文冲突之处，以本文件为准**——issue 正文的行号锚在 `c2439f62`，
  且它对 `safe_fs.py` 身份比对作用的描述（「对 symlink 掉包的防御」）已被 design D4 证伪。

## 7. 实现期证据账本（2026-08-21，Phase 2）

### 7.1 实现与红证

- 生产改动：`safe_fs.open_file_no_follow` 的 inode mismatch 新增
  `kind="identity_changed"`；journal 增加 keyed cycle owner 与 3 次、无 sleep、仅按 kind 分流的
  cached-read retry。其余 7 个 `_write_lock` 站点、两次 cache clear、所有安全拒绝均未改。
- 新增 16 个 mutation-sensitive 测试，覆盖 3.1–3.9b、3.11、3.12 与冷实例；3.10 选择方案 (a)，
  删除既有 same-cycle carve-out。并发 harness 复用 `_join_all`，用有界 Event（不是 sleep）协调。
- Batched red proof：只 stash 两个生产源文件、保留新测试，运行
  `uv run pytest -q tests/test_file_orchestration_journal_read_cache.py` → **14 failed**；
  stale-hit 用例实测 `running != succeeded`，replace 用例实测抛
  `Target file changed while being opened`。随即 pop，`git stash list` 无 `red-proof` 残留。
- 变异矩阵实测：M1→3.1 红；M2→3.2b 红；M3→3.3b 红；M4→3.6 红；
  M5→3.8/3.9 红；M6→3.9 红；M7→3.3a 红；**7/7 均转红且均恢复**。

### 7.2 最终本地验证

- `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`
  → **403 passed**。
- `uv run pytest -q tests/test_orchestration_chain.py` → **357 passed**。
- `uv run pytest -q $(grep -rl safe_fs tests/)` → **2938 passed, 6 skipped**。
- same-cycle hammer 定向用例连续运行 10 次 → **10/10 passed**。
- `uv run ruff check $(git ls-files '*.py')` → **All checks passed**。
- `openspec validate journal-concurrent-read-integrity --strict --no-interactive` → **valid**。
- 独占运行 `uv run pytest -q -m "not e2e and not grib and not integration"` →
  **13068 passed, 19 skipped, 162 deselected, 1 failed**。唯一失败是 entropy hard-gate
  `ENT-0001`；clean `git archive origin/master` 同命令/同 finding 可复现，故本 PR **新增红为零**。
  master 基线回归已路由 [#1662](https://github.com/DankerMu/SHUD-NWM/issues/1662)。
- node-22 NFS 时序确认按 D6 **挂起、未执行**；既有禁令不允许探针类负载，该限制会在 PR body
  继续显式记账，不冒充 PASS。

### 7.3 普查路由与计划偏离

- D7 逐模块普查见 design D7.1：新确证的非 journal 受影响调用面已路由
  [#1660](https://github.com/DankerMu/SHUD-NWM/issues/1660)；其余无新增 AFFECTED。
- 已知越界项：出口 clear 性能收窄 [#1658](https://github.com/DankerMu/SHUD-NWM/issues/1658)、
  `_next_sequence` 死码 [#1659](https://github.com/DankerMu/SHUD-NWM/issues/1659)、
  `include_direct_jobs=True` 死分支 [#1661](https://github.com/DankerMu/SHUD-NWM/issues/1661)。
- 偏离 1：3.1 用有界 `Event.wait` + coordinator 代替二方 Barrier；二方协议无法表达“两次读 + 带外改写”，
  仍满足无 sleep、有界等待和同一红/绿 oracle，影响仅测试 harness。
- 偏离 2：3.4 采用 tasks 明许的 `NoopClearDict` 子类，不 patch 两个调用点；行为覆盖等价。
- 偏离 3：测试中撤掉 `chain_types` 直接 import，改由已有 journal module 引用 `OrchestratorError`，
  避免为纯测试 import 新造 selector 路由缺口；测试语义不变。
- Phase 2 修复：初版实现为 owner 清理多开一层 nested `try/finally`，违反 D1；已改回**单一既有
  `try/finally`**，owner assignment 是 try 首条，出口 clear 与 owner clear 同在既有 finally；最终无行为偏离。
