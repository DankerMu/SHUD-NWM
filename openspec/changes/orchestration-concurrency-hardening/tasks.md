## A. Lane #1380（journal 读 cache 并发 + 捕获点归因）

- [ ] A.1 `FileOrchestrationJournalRepository` 新增 `_cache_lock`
      （`threading.Lock`），三个 cache（`_cycle_rows_cache` /
      `_direct_jobs_cycle_cache` / `_read_bytes_cache`+`_read_bytes_cache_total`）
      的所有 get/store/evict 收进锁内；锁内零 IO/零 JSON 解析；
      `_locked_cycle_write` 的两处 `.clear()` 同改。锁序单向：
      `_cache_lock` 内不得获取 `_write_lock`/flock（写锁内进 cache 锁允许）
- [ ] A.2 红证（概率竞态的压测构造）：单 repository 实例、小容量强制
      持续驱逐、2+ 线程 hammer `_cycle_rows`/`_read_bytes_limited_cached`
      ——**pre-fix 须在秒级稳定红**（`RuntimeError: dictionary changed
      size during iteration`，捕获逐字红形并记录复现参数：线程数/时长/
      key 分布）；post-fix 同构造跑 ≥10× pre-fix 红所需时长全绿。
      测试常驻套件需限时（如 2-3s 预算），不得引入慢测
- [ ] A.3 捕获点归因：`scheduler_execution.py:716` except 分支写
      `error_traceback_tail`（格式化 traceback 尾部、evidence-safe 截断）
      进 model_run_evidence；单测断言字段存在、含抛错帧文件名、且过
      evidence 安全化（不泄内部路径全量——按既有 evidence_safe 口径）
- [ ] A.4 cache 语义回归锁：`tests/test_file_orchestration_journal.py`
      全绿零断言改动；同 key 命中值与驱逐行为单线程逐字不变

## B. Lane #1356（先定性后修，issue 验收逐条）

- [ ] B.1 确定性复现：提交决策路径注入测试可 monkeypatch 的模块级
      no-op hook（生产零行为），测试用 Barrier 把两个并发 public pass
      钉进「reclaim CAS 判定后、sbatch 前」交错窗口，稳定产出
      `forecast_attempts == 3`；失败时 dump `query_pipeline_jobs_by_cycle`
      全部行（job_id / idempotency_key / status / submission_attempt /
      reconciliation_decision），随 PR 附逐字证据
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
      两参数化 ×20 轮（受压：与 A.2 hammer 或人工负载并跑）全绿

## C. Verification

- [ ] C.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_file_orchestration_journal.py tests/test_production_scheduler.py
- [ ] C.2 uv run ruff check services tests
- [ ] C.3 openspec validate orchestration-concurrency-hardening --strict --no-interactive
- [ ] C.4 merge 后 node-27 receipt（C.1 三套件；#1513 已知例外口径）
      记 #1380 + #1356
