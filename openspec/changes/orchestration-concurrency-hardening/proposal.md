# Proposal: orchestration-concurrency-hardening (#1380 + #1356)

## Why

两单同域（forecast 编排链并发卫生），合批处理（用户裁定）。

**#1380（生产事故，偶发）**：`scheduler_2026081411_99f6e3ed6e15` 一趟内
IFS 整源 17 run 全部 `submission_failed`，error_message =
`dictionary changed size during iteration`，捕获点
`scheduler_execution.py:716` 只留消息串无 traceback。explorer 勘察定位
唯一高置信候选：**`FileOrchestrationJournalRepository` 三个无锁实例级
dict cache**（`_cycle_rows_cache` / `_direct_jobs_cycle_cache` /
`_read_bytes_cache`，`file_orchestration_journal.py:543-553`）——
`scheduler_core.py:107-112` 每趟构造**单例** `active_repository`，
`:477-481` 把它交给**每个** cohort 的新鲜 orchestrator；
`scheduler_execution.py:364-370` 经 `run_concurrent_submissions`
（ThreadPoolExecutor，`NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` 生产可 >1）
跨源/cohort 并发调 `orchestrate_cycle`。三个 cache 的读侧填充与
`next(iter(...))` LRU 驱逐（`:4200-4202` / `:4121-4123` / `:4388-4394`）
全在 `_write_lock` 之外，`_cycle_rows` 是 15+ 调用点的热路径——两线程
并发插入/驱逐同一 dict 即得该 RuntimeError。全 git 史无并发修复记录。

**#1356（CI 偶发红，按 P1 对待直到证伪）**：
`test_file_journal_post_window_concurrent_public_cycles_submit_one_retry[IFS]`
首轮并发断言 `forecast_attempts == 2` 在慢 runner 实测 3——两个并发
public pass **都**打到了 forecast sbatch。四个候选竞态面自 2026-07-22
起零实质变化（explorer 逐一复核，仅行号漂移）；主嫌：in-pass 重试
铸新 job_id（`chain_forecast_orchestrator_cycle.py:218-220`
`RETRY_JOB_ID_MARKER`）换掉幂等键，submit-once CAS 不再是同一把锁——
败者 pass 拿到 `reservation_lost` 走 in-pass retry 即以新 id 再投。
**此为推断，必须先确定性复现+定性，不得据此直接改代码**（issue 明确
要求）。测试侧注意：per-thread 各自 new repository（`:9436`），
`_write_lock` per-instance 故跨线程互斥完全压在 flock 上。

## What Changes

### Lane A（#1380，确定性修复）

- `FileOrchestrationJournalRepository` 三个读侧 cache 的
  get/store/evict 全部收进一把专用 `threading.Lock`（新 `_cache_lock`，
  不复用 `_write_lock`——写路径已持 `_write_lock` 时仍会读 cache，复用
  会自锁或扩大临界区）。锁内只做 dict 操作，不做 IO/JSON 解析（解析在
  锁外完成后再 store），避免热路径串行化。
- `_locked_cycle_write` 内两处 `self._cycle_rows_cache.clear()`
  （`:6718-6725`）同样改经 `_cache_lock`。
- 捕获点 `scheduler_execution.py:716` except 分支把
  `traceback`（格式化尾部若干帧，evidence-safe 截断）写入
  model_run_evidence 的新字段 `error_traceback_tail`——下次异常可归因，
  不再靠错误串猜位置（issue 建议 2）。

### Lane B（#1356，先定性后修）

1. **确定性复现装置**：在 reclaim CAS 与 sbatch 之间给测试注入交错点
   ——`_run_cycle_chain` 提交决策路径上加一个测试可注入的同步 hook
   （模块级 no-op 默认，测试 monkeypatch 成 threading.Barrier/Event；
   生产零行为变化）。用它把两个 pass 钉进 issue 预言的交错窗口，稳定
   产出 `forecast_attempts == 3`，并 dump `query_pipeline_jobs_by_cycle`
   全部行（job_id / idempotency_key / status / submission_attempt /
   reconciliation_decision）。
2. **定性**（判据 = 第 3 次提交所用 job_id）：
   - 新 `#retry-N` job_id ⇒ **守卫被绕过**（主嫌成立）：修复 =
     in-pass retry 铸出的新 job_id 也必须过同一 submit-once 判据
     （reclaim/reserve 绝对性证明覆盖 retry 键），补确定性并发负向
     用例（未修必红/修后必绿）；此时**追加** pipeline-job-persistence
     spec delta（submit-once 不变量覆盖 retry 铸键），由编排者在定性
     落账后修订 fixture 并重跑 openspec validate。
   - 同一 job_id ⇒ 真·双重提交同一预约（CAS 本体洞）：按实修 CAS。
   - 复现证明纯 test-only 时序假设 ⇒ 用例改屏障同步 + 注释写明被保护
     不变量，**不动生产代码**（issue 备选路线的准入条件）。
3. 20 轮受压重复验证（issue 验收）。

## Non-Goals

- #1380 建议 3（同趟重试/告警）：前沿停滞告警属 #1368 车道，不做。
- CI targeted-lane 选择器稳定性（#1182/#1254）。
- PG repository（`chain_repository.py`）的并发行为——共享单例路径只发
  file journal（db-free 生产面），PG 侧无本事故几何。
- 三个 cache 的容量/命中率调优——只加互斥，不改语义。

## Risk triage

- Fixture level: expanded（两缺陷车道 + 一条 investigation fork；
  并发域 + 生产事故域）。Repair intensity: medium。
- Risk packs: **concurrency selected**（Lane A 锁序：`_cache_lock` 与
  `_write_lock`/flock 的嵌套顺序必须单向——cache 锁内不得进写锁；
  Lane B 的 hook 必须生产 no-op）；**state-semantics selected**
  （submit-once 不变量的定性分叉；cache 加锁不得改变读值语义）；
  **test-evidence selected**（#1380 红证是概率性竞态——必须给出
  "在 pre-fix 上 N 秒内稳定红"的压测构造 + 修后同构造长跑绿；
  #1356 红证必须是确定性屏障交错，不许 sleep 碰运气）；其余 not selected。

## Must preserve

- cache 语义零变化：同 key 同值、驱逐策略不变（只是互斥），
  `tests/test_file_orchestration_journal.py` 全绿零断言改动。
- `orchestrate_cycle` 单线程行为逐字不变（三套件全绿）。
- evidence schema 向后兼容：`error_traceback_tail` 是**新增**字段，
  现有消费者（evidence 校验、display）不得受影响。
- `test_..._submit_one_retry` 既有断言口径：首轮 ==2、次轮 ==3 的
  语义仅在定性为守卫洞且修复后按新契约同步（作为例外申报），
  test-only 定性下断言值不变。

## Seams under test

- Lane A：直接对单个 repository 实例多线程 hammer `_cycle_rows` /
  `_read_bytes_limited_cached`（小容量强制持续驱逐 + 多 key 碰撞），
  pre-fix 秒级红（RuntimeError），post-fix 长跑绿；catch 点单测断言
  evidence 含 traceback tail。
- Lane B：注入 hook 屏障（монkeypatch 模块级 no-op），确定性交错两个
  public pass；行 dump 走真实 file journal。

## Evidence mapping

- #1380 验收（并发不再炸 + 可归因）→ tasks A.2 hammer 红证 + A.3
  traceback 单测。
- #1356 验收 1（确定性复现 + 行 dump）→ tasks B.1。
- #1356 验收 2（书面定性 + 判据）→ tasks B.2（落账进 PR body 与本
  proposal 修订）。
- #1356 验收 3/4（按定性修复 + 负向测试）→ tasks B.3。
- #1356 验收 5（20 轮受压全绿）→ tasks B.4。
- Verification：`uv run pytest -q tests/test_orchestration_chain.py
  tests/test_file_orchestration_journal.py tests/test_production_scheduler.py`
  + ruff + openspec validate；merge 后 node-27 receipt 记两 issue。
