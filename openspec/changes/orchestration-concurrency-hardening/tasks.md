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

## B. Lane #1356（已触发 De-batch 出口——B.1 四种构造产不出确定性红，
## 拆回 #1356；反证与结构性结论见 proposal Lane B 节与 #1356 评论。
## 以下任务随 descope 撤销，不在本 PR 交付；唯一保留产物 = flaky 断言
## 失败时的行 dump 诊断）

（B.1-B.4 任务体已随 descope 删除；勘察证据与结构性结论
以 #1356 评论为准——含四种构造清单、同 retry 键反证行 dump、
mandated 缝位不可翻盘论证、上游 idempotency 键选取的下一步指向）

## C. Verification

- [x] C.1 uv run pytest -q tests/test_orchestration_chain.py
      tests/test_file_orchestration_journal.py tests/test_production_scheduler.py
- [x] C.2 uv run ruff check .
- [x] C.3 openspec validate orchestration-concurrency-hardening --strict --no-interactive
      ——已在 De-batch 裁剪**之后**重跑通过（终态一推纪律）
- [ ] C.4 merge 后 node-27 receipt（C.1 三套件；#1513 已知例外口径）
      记 #1380（#1356 已 descope 拆回，不在本 PR 记账）
