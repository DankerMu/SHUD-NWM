# Tasks — journal-concurrent-read-integrity

行号锚定 `origin/master` @ `8045615e`。**每一处引用在动手前必须按符号名 grep 复核**——
issue 正文的行号锚在 `c2439f62`，已被后续合并推移。

## 1. #1595 — cycle 写窗 owner 语义

- [ ] 1.1 在 `FileOrchestrationJournalRepository.__init__`（`_write_lock` 定义处 `:544` 附近）
  新增 `self._cycle_write_owner: int | None = None`。
- [ ] 1.2 `_locked_cycle_write`（`:6948`）在 `with self._write_lock:` 之内、
  `_ensure_root_unlocked()` 之前置 `self._cycle_write_owner = threading.get_ident()`；
  在**已有的 `finally` 块**（`:6956-6958`）内清空为 `None`。
  清空必须与既有的 `_cycle_rows_cache.clear()` 同在该 `finally`，
  **不得**新开一个 try/finally（多一层就多一处漏配可能）。
- [ ] 1.3 `_cycle_rows`（`:4103`）判据改为
  `in_write_window = self._cycle_write_owner == threading.get_ident()`。
  **注意 `None == <int>` 恒假**，无需额外判空；但要有测试锁死冷实例（owner 为 `None`）走 fingerprint 路径。
- [ ] 1.4 更新 `:4098-4102` 的注释：写清楚免指纹的两条前提（cycle flock + append hook）
  只在 **cycle 写窗的持有线程**上成立，并按 design D8 写入与 #1567 的交叉裁定
  （免指纹分支的篡改暴露面从「任何线程」缩到「写窗 owner」，owner 自身的免指纹不做篡改检测，
  那是 #1567 的地盘）。
- [ ] 1.5 **禁令**：不得把 `_write_lock` 改成 `RLock`；不得给其余 7 个 `with self._write_lock:`
  站点加 owner 标记（design D1 的收窄裁定）。若实现中发现该收窄不成立，
  **停下来报告**，不要自行扩回 8 站点。

## 2. #1600 — 结构化判别位 + 有界重试

- [ ] 2.1 `packages/common/safe_fs.py:285` 的 raise 带上 `kind="identity_changed"`。
  **只改这一处 raise**；`:271`/`:273`/`:278`/`:283` 的 symlink 与非常规文件拒绝
  保持默认 `kind="unsafe"` 不变。
- [ ] 2.2 在 `safe_fs.py` 顶部 `SafeFilesystemError`（`:10-15`）的 docstring 或紧邻注释中，
  按 design D4 写清 `kind="identity_changed"` 的确切含义：
  「打开窗口内目标 inode 被替换（常规文件掉包或正常 `os.replace`，**这一层无法区分**）」，
  以及它**不是** symlink 防御（symlink 走 `O_NOFOLLOW` 的 `ELOOP` 与 `S_ISLNK` 检查，到不了这里）。
- [ ] 2.3 `file_orchestration_journal.py` 新增具名常量
  `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS = 3`（总尝试次数，含首次）。
- [ ] 2.4 `_read_bytes_limited_cached`（`:4563`）把 `:4587` 的
  `read_bytes_limited_no_follow(...)` 包进有界重试循环：
  仅当 `isinstance(error, SafeFilesystemError) and error.kind == "identity_changed"` 时重试，
  **无 sleep**；尝试用尽后原样抛出最后一次的异常。
  重试前必须重取 stat 探针（`:4576-4582` 那段），否则用旧 signature 去存新内容会污染 cache。
- [ ] 2.5 **禁令**：不得按异常 message 字符串分流；不得在 `safe_fs.open_file_no_follow`
  内部加重试；不得删除或弱化 `safe_fs.py:284` 的比对；不得给重试加 `time.sleep`。

## 3. 测试（红证优先落在 `tests/test_file_orchestration_journal_read_cache.py`；新建并发文件须在 PR 中显式列出）

- [ ] 3.1 **#1595 红证（确定性，非 sleep）**：两线程共享**同一** repository 实例。
  线程 A 进入 `_locked_cycle_write` 后在屏障上挂住；线程 B 对**另一个** cohort 的已热 cache key
  调 `_cycle_rows`，其源文件在缓存后被带外改写。
  pre-fix 稳定返回陈旧行，post-fix 返回重算后的新鲜行。
  **屏障必须用 `threading.Barrier`/`Event`，禁止 `time.sleep` 碰运气；
  所有线程 join 必须带 timeout，超时即 fail，不得留悬挂线程。**
- [ ] 3.2 **单线程等价性**：owner 线程在自己的写窗内仍走免指纹快路径。
  断言方式：monkeypatch/spy `_cycle_rows_source_fingerprint`，
  断言 owner 命中时**调用次数为 0**（仅断言"结果正确"不够——那在两条路径下都成立）。
- [ ] 3.3 **异常路径**：`_locked_cycle_write` 内抛异常时 `_cycle_write_owner` 必须为 `None`。
  线程 id 会被线程池复用，泄漏标记等于给不相干任务发免检通行证——
  必须有一条断言直接读 `repository._cycle_write_owner is None`。
- [ ] 3.4 **clear 粒度无关性**（design D2）：断言 cohort X 的写不会让 cohort Y 读到**错值**，
  且该断言在 `_cycle_rows_cache.clear()` 被 monkeypatch 成 no-op 时**仍然通过**——
  这才叫"正确性不依赖 clear 粒度"。
- [ ] 3.5 **#1595 变异体证死**：把判据改回 `self._write_lock.locked()`，3.1 必须转红。
- [ ] 3.6 **#1600 红证（确定性，非 sleep）**：monkeypatch `os.open`（或等价注入点），
  在 `safe_fs.py:265` stat 与 `:275` open 之间强制插入一次 `os.replace`。
  pre-fix 稳定抛 `Target file changed while being opened`，
  post-fix 返回**替换后**的正确内容。
- [ ] 3.7 **重试有界**：注入一个"每次都 replace"的写者，
  断言恰好 `MAX_FILE_JOURNAL_IDENTITY_RETRY_ATTEMPTS` 次尝试后抛出，
  且抛出的仍是 `SafeFilesystemError`（fail-closed 未回退）。
- [ ] 3.8 **安全语义不回退**：symlink 掉包、非常规文件、containment 越界三类场景
  **一次都不进重试分支**（断言尝试次数为 1）且仍然抛出。
  须显式覆盖「窗口内换成 symlink」这一形状（design D4 表第二行，走 `ELOOP`）。
- [ ] 3.9 **按字段而非按串分流**：断言重试分支的判据读的是 `error.kind`；
  构造一个 message 含 "Target file changed while being opened" 但 `kind != "identity_changed"`
  的异常，断言它**不**被重试。
- [ ] 3.10 **端到端锁死**（#1600 验收项）：两线程共享同一 repository 实例、
  读写**同一个 cycle**（PR #1598 端到端用例当初被迫"读写分 cycle"规避的那个构造）。
  post-fix 稳定跑通。该用例须显式注释说明它就是本 issue 的回归锁，
  **且不得再靠读写分 cycle 绕开**。
- [ ] 3.11 **结构守卫**：断言 `_cycle_write_owner` 的**赋值语句**（AST，`ast.Assign`/`ast.AugAssign`
  且 target 为 `self._cycle_write_owner`）只出现在 `_locked_cycle_write` 函数体内。
  这是 design D1 收窄之后"成对性"的结构性保证。
- [ ] 3.12 **不可重入前提锁死**（design D1）：断言 `_write_lock` 是 `threading.Lock`
  而非 `RLock`——朴素 set/clear 的正确性依赖不可重入；若将来换成 `RLock`，
  嵌套退出会提前清空标记。该断言即为那个前提的守卫。

## 4. 普查与留痕

- [ ] 4.1 **safe_fs 读原语调用点普查**（#1600 验收项，design D7）：
  对每个调用模块给出「是否存在并发写方 + 写方是否走原子 replace」的结论与**依据**
  （不是"看起来像"，要指出写方代码位置或说明不存在）。表落在 design.md 新增小节。
  高嫌疑先验须逐一核实、不得直接采信：`packages/common/provider_atomic.py`、
  `services/orchestrator/scheduler_file_providers.py` ↔ `scripts/scheduler_file_provider_refresh.py`、
  `packages/common/object_store.py`、`packages/common/state_manager.py`。
- [ ] 4.2 普查中新确证的受影响调用点：**报告立 issue，不在本 change 修**。
  issue 编号回填 design D7。若一个都没有，明确写"零新增"，不得留空。
- [ ] 4.3 `_next_sequence`（`:6582`）生产无调用者（全仓仅测试引用）——立 issue，本 change 不动。
- [ ] 4.4 `_locked_cycle_write` 全局 `.clear()` 的性能收窄（design D2 裁定保留）——立 issue。

## 5. 验证（Evidence Floor）

以下每一条都必须**实跑并贴出输出**，不得以论证替代测量：

- [ ] 5.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`
  —— 全绿；新增用例数与名称列进 PR。
- [ ] 5.2 `uv run pytest -q tests/test_orchestration_chain.py` —— 全绿（#1600 Verification 指定）。
- [ ] 5.3 `uv run pytest -q $(grep -rl safe_fs tests/)` —— 全绿。
  **改了共享原语，此项为必跑**，不得用 `-k safe_fs` 替代（覆盖分散在各消费方套件里）。
- [ ] 5.4 变异矩阵：至少覆盖 3.5（判据改回 `.locked()`）、
  「重试上限改为 1」、「重试条件放宽到全部 `SafeFilesystemError`」、
  「owner 清空从 `finally` 挪到正常路径末尾」四个变异体，逐个给出**实测**红/绿。
  凡填"预期红"而未实测的格子，必须标明是推断。
- [ ] 5.5 `uv run ruff check $(git ls-files '*.py')` —— clean。
  （**不要跑 `uv run ruff check .`**，会命中本地未跟踪的 `skills/` 工具。）
- [ ] 5.6 `openspec validate journal-concurrent-read-integrity --strict --no-interactive` —— valid。
- [ ] 5.7 全量本地 `uv run pytest -q -m "not e2e and not grib and not integration"`：
  与合并基线对比，**新增红为零**。基线红数必须**自己实测**，不得采信本文件或 brief 里的任何数字。
  **同一时刻只允许一个 pytest 进程**（并发跑会因 CPU 争用产出假红）。
- [ ] 5.8 **node-22 实机 NFS 时序确认：挂起。** 受既有 node-22 禁令阻塞（design D6）。
  必须在 PR body 显式记账，不得静默省略。两条 issue 的验收清单本身全部本地可判定，
  故该挂起不阻塞合并。

## 6. 交付纪律

- [ ] 6.1 每一处与本 tasks/design 的 departure 写入 PR 的 `偏离记录` 段（what/why/impact）；
  无偏离也要显式写"无偏离"。
- [ ] 6.2 design D1（收窄到单站点）、D2（保留全局 clear）、D3（重试落调用方）、
  D5（3 次无退避）四条裁定若在实现中被推翻，**停下来报告**，不要自行改道。
