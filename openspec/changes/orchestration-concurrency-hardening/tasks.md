## A. Lane #1380（journal 读 cache 并发 + 捕获点归因）

- [x] A.1 `FileOrchestrationJournalRepository` 新增 `_cache_lock`
      （`threading.Lock`），三个 cache（`_cycle_rows_cache` /
      `_direct_jobs_cycle_cache` / `_read_bytes_cache`+`_read_bytes_cache_total`）
      的所有 get/store/evict/**iterate** 收进锁内。**站点全集**：
      三处 `next(iter(...))` 驱逐（:4200-4202/:4121-4123/:4388-4394）、
      **`_apply_record_to_cycle_rows_cache` 全体**（:6592 `stale_keys`
      整表遍历 + :6595/:6596/:6604/:6608——唯一整字典 iterate、窗口最宽；
      reducer `_apply_journal_record` 纯内存可整段入锁）、
      `_write_pipeline_job_direct_unlocked` 的 pop（:6139）、
      `_locked_cycle_write` 两处 `.clear()`（:6719/:6725）。
      锁内**只做 dict 存取**——零 IO/零 JSON 解析/`_clone_cycle_rows`
      在锁外。锁序单向：`_cache_lock` 内不得获取 `_write_lock`/flock
      （写锁内进 cache 锁允许）
- [x] A.2 红证（概率竞态的压测构造）：单 repository 实例、小容量强制
      持续驱逐、2+ 读线程 hammer `_cycle_rows`/`_read_bytes_limited_cached`
      **+ 1 个真实写线程**（走 `_locked_cycle_write`+append，覆盖 :6592
      整表遍历站点——审者本机实测 2 线程 74ms 得 "size changed"、加扫描
      线程 114ms 得 "keys changed" 变体，两种消息都算红，不得按消息串
      排除站点）——**pre-fix 秒级稳定红**（捕获逐字红形 + 复现参数：
      线程数/时长/key 分布）；post-fix 同构造跑 ≥10× pre-fix 红所需
      时长全绿。测试常驻套件限时 2-3s 预算，不得引入慢测
- [x] A.3 捕获点归因：`scheduler_execution.py:716` except 分支写
      `error_traceback_tail`（**最后 3 帧、总长 ≤2000 字符**，过
      `context.evidence_safe`——密钥/URL 脱敏口径；路径保留是设计意图，
      file:line 归因靠它）进 model_run_evidence；单测断言字段存在、
      含抛错帧文件名、长度守上限
- [x] A.4 cache 语义回归锁：`tests/test_file_orchestration_journal.py`
      全绿零断言改动；同 key 命中值与驱逐行为单线程逐字不变

## B. Lane #1356（先定性后修，issue 验收逐条）

- [ ] B.1 确定性复现：hook 缝位 **`chain_stage_execution.py:239-247`**
      （`_reserve_cycle_stage` 返回后、`_reservation_already_inflight`
      判定前），模块级 no-op 默认（**不复用** :232
      `_before_cycle_stage_submit`——在 reserve 前且做实事）；测试
      monkeypatch 成 Barrier 钉交错窗口，稳定产出
      `forecast_attempts == 3`；**复现命令 + 注入点落账**；失败时 dump
      `query_pipeline_jobs_by_cycle` 全部行（job_id / idempotency_key /
      status / submission_attempt / reconciliation_decision）随 PR 附
      逐字证据；**补一条 no-op 断言**（默认 hook 生产零行为）。
      **预算与出口**：一个 implementer pass 内产不出确定性红 ⇒ 触发
      proposal「De-batch 出口」，Lane B 拆回 #1356，A 单独交付
- [ ] B.2 书面定性（判据 = 第 3 次提交所用 job_id）：`#retry-N` 新键 =
      守卫被绕过；同 job_id = CAS 本体洞；若钉死交错后仍无法产出 3 而
      只能证明用例时序假设过紧 = test-only。结论落 PR body +
      proposal 修订（守卫洞路线须追加 pipeline-job-persistence
      submit-once spec delta 并重跑 openspec validate——编排者职责）
- [ ] B.3 按定性修复：
      守卫洞 ⇒ retry 铸键纳入同一 submit-once 判据 + 确定性并发负向
      用例（未修必红/修后必绿）+ 说明生产链路（node-22 实投）是否
      曾/可能触发；
      test-only ⇒ 用例改屏障同步 + 注释写明被保护不变量，生产代码零改动
- [ ] B.4 `uv run pytest -q "tests/test_orchestration_chain.py::test_file_journal_post_window_concurrent_public_cycles_submit_one_retry"`
      两参数化 ×20 轮全绿——**慢环境 oracle 在 node-27，且为合并门
      （pre-merge）**（issue 明确本地 macOS 不够慢；主树被 #1341 占用
      时用隔离 git worktree）；本地受压跑（与 A.2 hammer 并跑）作先导，
      C.4 仅把 node-27 结果转录归档为 receipt

## C. Verification

- [x] C.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_file_orchestration_journal.py tests/test_production_scheduler.py
- [x] C.2 uv run ruff check .
- [ ] C.3 openspec validate orchestration-concurrency-hardening --strict --no-interactive
      ——若 B.2 定性触发 fixture 修订（追加 submit-once delta 或
      De-batch 裁剪），C.3 必须在该修订**之后**重跑（终态一推纪律）
- [ ] C.4 merge 后 node-27 receipt（C.1 三套件 + B.4 20 轮；#1513
      已知例外口径）记 #1380 + #1356
