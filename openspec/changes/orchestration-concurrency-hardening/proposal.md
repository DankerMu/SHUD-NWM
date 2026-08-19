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
全在 `_write_lock` 之外，`_cycle_rows` 是 15+ 调用点的热路径——两读线程
并发插入/驱逐同一 dict 即得该 RuntimeError；**窗口最宽的一条是读-写
交错**：写线程持 `_write_lock` 在 `_apply_record_to_cycle_rows_cache`
（`:6592`，落地后 `:6622`）整表遍历 `_cycle_rows_cache`，读线程锁外插入/驱逐同一 dict
（读侧填充不持 `_write_lock`）。全 git 史无并发修复记录。

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
  get/store/evict/**iterate** 全部收进一把专用 `threading.Lock`
  （新 `_cache_lock`，不复用 `_write_lock`——写路径持 `_write_lock` 时
  仍会读 cache（`_apply_record_to_cycle_rows_cache` 读
  `_cycle_rows_cache`、`:4885` 类站点经 `_read_optional_json` 走
  `_read_bytes_limited_cached`），非重入 Lock 复用必死锁）。锁内只做
  dict 存取——零 IO、零 JSON 解析、**clone（`_clone_cycle_rows`）也在
  锁外**，避免热路径串行化。锁序单向：`_write_lock → _cache_lock` 允许，
  反向禁止（cache 辅助函数均纯 dict/纯内存 reducer，已核可达）。
- **变更站点全集**（round-0 审补齐，缺一即修复不完整）：
  三处 `next(iter(...))` 驱逐（`:4200-4202` / `:4121-4123` /
  `:4388-4394` 及 `_read_bytes_cache_total` 更新）；
  **`_apply_record_to_cycle_rows_cache` 全体**（`:6592` `stale_keys`
  整表遍历——全套 cache 里唯一整字典 iterate，窗口最宽、最可能真凶——
  加 `:6595/:6596/:6604/:6608` 的 pop/get/store；其内 reducer
  `_apply_journal_record` 为纯内存无锁无 IO，整段入 `_cache_lock` 不
  违反锁序）；`_write_pipeline_job_direct_unlocked` 的
  `_direct_jobs_cycle_cache.pop`（`:6139`）；`_locked_cycle_write`
  两处 `.clear()`（`:6719` 与 `:6725`）。
- 捕获点 `scheduler_execution.py:716` except 分支把 traceback 尾部
  （**最后 3 帧、总长上限 2000 字符**，过 `context.evidence_safe`
  ——其口径是密钥/URL 脱敏，**不承诺路径脱敏**；traceback 本身要用于
  file:line 归因，路径保留是设计意图）写入 model_run_evidence 新字段
  `error_traceback_tail`（issue 建议 2）。体积影响已声明：一趟最多
  candidate_count × 2000 字符，17 候选 ≈ 34KB 上限，可接受。

### Lane B（#1356）——已触发 De-batch 出口，拆回 #1356

实现 pass 内四种构造（mandated 缝位屏障 / 屏障下移 `reserve_candidate`
CAS 内部 / 错峰启动 / 双参数化 ×20 轮受压 soak）均无法产出确定性
`forecast_attempts == 3`，按本 proposal「De-batch 出口」条款拆回
#1356（descope，勘察结论已评论落账至该 issue）。关键反证：两个并发
pass 铸的是**同一把** retry 键（`...:forecast:retry_1`，恰一个
created=True）——主嫌（retry 铸键绕过守卫）在该缝位被证伪；
`_reservation_already_inflight` 是 hook 前已算好的 `ReservationResult`
纯函数，mandated 缝位在构造上翻不了盘，下一轮排查应 instrument 上游
idempotency 键选取。本 PR 保留的唯一 Lane B 产物：flaky 首轮断言失败
时 dump 本 cycle 全部 job 行（断言值不变——下次 CI 红自带定性证据）。

## Non-Goals

- #1380 建议 3（同趟重试/告警）：前沿停滞告警属 #1368 车道，不做。
- CI targeted-lane 选择器稳定性（#1182/#1254）。
- PG repository（`chain_repository.py`）的并发行为——共享单例路径只发
  file journal（db-free 生产面），PG 侧无本事故几何。
- 三个 cache 的容量/命中率调优——只加互斥，不改语义。
- **`in_write_window` ownership-blind 陈旧命中（pre-existing 残余
  风险，声明不修）**：`_cycle_rows:3905` 用 `_write_lock.locked()` 判
  写窗口，不认 owner——共享单例下 B 线程会因 A 线程持写锁而跳过
  fingerprint 校验直接吃 cache 命中，单线程绝不发生。加 `_cache_lock`
  不改这条；修它要动 `_write_lock` ownership 语义，超出本 change。
  路由 issue-scribe 另立（编排者），不阻塞本 PR。同类已知项：
  `_locked_cycle_write` 的全局 `.clear()` 会跨 cohort 抹掉他人 cache
  entry（纯性能、pre-existing），一并记入该 issue。

## Risk triage

- Fixture level: expanded（两缺陷车道 + 一条 investigation fork；
  并发域 + 生产事故域）。Repair intensity: medium。
- Risk packs: **concurrency selected**（Lane A 锁序：`_cache_lock` 与
  `_write_lock`/flock 的嵌套顺序必须单向——cache 锁内不得进写锁；
  ~~Lane B 的 hook 必须生产 no-op~~——Lane B 已 descope，hook 未落地）；**state-semantics selected**
  （submit-once 不变量的定性分叉；cache 加锁不得改变读值语义）；
  **test-evidence selected**（#1380 红证是概率性竞态——必须给出
  "在 pre-fix 上 N 秒内稳定红"的压测构造 + 修后同构造长跑绿；
  #1356 红证必须是确定性屏障交错，不许 sleep 碰运气）；其余 not selected。

## De-batch 出口（round-0 审新增）

Lane A 是生产事故的确定性修复，Lane B 是可能卡住的调查——若 B.1 在
一个 implementer pass 预算内产不出确定性 `forecast_attempts == 3`
红形，**Lane B 整体拆出本 PR**：A 单独走完交付（tasks B.* 从本
change 删除、本 proposal Lane B 节改为「已拆回 #1356」、重跑
openspec validate），B 连同 hook 缝位勘察证据回 #1356 记录。拆分
不算失败，算 descope，按 gates 口径落终态账。

## Must preserve

- cache **单线程语义逐字不变**：同 key 同值、驱逐策略不变（只加
  互斥），`tests/test_file_orchestration_journal.py` 全绿零断言改动。
  并发下的口径以 spec 场景为准（不抛 iteration error、无撕裂 entry）
  ——「并发读值与单线程等价」**不承诺**（`in_write_window` pre-existing
  陈旧命中见 Non-Goals）。
- `orchestrate_cycle` 单线程行为逐字不变（三套件全绿）。
- evidence schema 向后兼容：`error_traceback_tail` 是**新增**字段，
  现有消费者（evidence 校验、display）不得受影响。
- `test_..._submit_one_retry` 既有断言口径：首轮 ==2、次轮 ==3 的
  语义仅在定性为守卫洞且修复后按新契约同步（作为例外申报），
  test-only 定性下断言值不变。

## Seams under test

- Lane A：直接对单个 repository 实例多线程 hammer `_cycle_rows` /
  `_read_bytes_limited_cached`（小容量强制持续驱逐 + 多 key 碰撞）
  **+ 1 个真实写线程**（`_locked_cycle_write`+append，覆盖 :6592
  读-写交错），pre-fix 秒级红（RuntimeError），post-fix 长跑绿；
  catch 点单测断言 evidence 含 traceback tail。
- ~~Lane B：注入 hook 屏障~~——已 descope，hook 未落地；保留的 dump
  诊断走真实 file journal（`query_pipeline_jobs_by_cycle` 公共 API）。

## Evidence mapping

- #1380 验收（并发不再炸 + 可归因）→ tasks A.2 hammer 红证 + A.3
  traceback 单测。
- #1356 验收 → **descope**（tasks B.* 已删）：验收 1 未达成（四构造
  记录 + 反证行 dump 已评论落账 #1356）；验收 2 定性 unqualified；
  验收 5 的 40 轮本地受压全绿；接力载体 = #1356 评论 + 保留的失败时
  dump 诊断。
- Verification：`uv run pytest -q tests/test_orchestration_chain.py
  tests/test_file_orchestration_journal.py tests/test_production_scheduler.py`
  + ruff + openspec validate；merge 后 node-27 receipt 记 #1380
  （#1356 已 descope）。
