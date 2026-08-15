# Design: master-row-permanent-failure-marking (#1312)

## Risk triage

- Fixture level: **expanded**（初 triage compact；fixture-review round-1 P1-1
  证实点修前提为假——master 行无任何可用 typed 终态写入口，需新增一条
  journal authority transition；round-2 P1-A 又证实需给 cohort projection
  master 写面加终态粘性。triage 改判记录之）。
- 风险轴：journal authority 写面新增 API + projection 粘性（最高风险）·
  终态语义（误增/误丢重投 + 抹标振荡）· 幂等/双事件 · 既有 anchor 不弱化。

## Decisions

### D1 — 修代码而非 spec deviation（含 projection 反论的正面回应）

最强反论（round-1 提出）：master 行终态是 cohort projection 的投影——
`project_forecast_cohort_tasks` 把 `master_status` 限死在
`{succeeded, partially_failed, failed}`，可主张"master 终态权威在
projection，retry service 不应直写"。回应：(a) projection 的
`master_status` 是从 candidates 算出的派生量，不是从 master 行读回的权威
源——落标不与 projection 争权威，但 projection 写面需要终态粘性（D9，
round-2 P1-A：否则下一趟 resume 会把标抹回 `failed` 并制造事件振荡）；
(b) 非 master 行早已携带 `permanently_failed` 且全部消费面两状态同集合；
(c) spec deviation 方案永久丢失可观测差异。裁决：修代码。

### D2 — 调用方臂：capability 门控落标，短路语义不变

`chain_forecast_orchestrator_cycle.py:195-197`：`return None` 前落标，但
**不能只靠 `getattr(…, "mark_permanently_failed", None)`**——round-1 P1-2：
`RetryService` 同名方法存在，db-free gate 测试以 store-less
`RetryService(None, …)` 注入（`tests/test_orchestration_chain.py:1600-1604`），
调用会在 `retry.py:422` `self.store.session.add` 上 AttributeError。落标须
按 service 形状门控：仅当
`getattr(self.retry_service, "repository", None) is not None`
（`FileJournalRetryService.__init__` 设 `repository`，
`file_orchestration_journal.py:6582`；`RetryService` 只设 `store`，
`retry.py:317-320`）才调 `mark_permanently_failed(job)`。store-less/DB 形
状保持现状（round-2 复核：DB 平面持久层无
`accepted_submit_contract_version` 字段，master 臂在 DB 平面不可达——门
控不留真实缺口）。短路不取消（`506d99dd` legacy adapter 动机保留），
PipelineResult 仍 `"failed"`（`chain_forecast_execution.py:225-231`）。落
标后以 repository 读回断言写穿。`job` 来自 `_retry_job_for_stage_result`
（`chain_forecast_execution.py:468-507`，携 journal 行 job_id `:495`）。

PR round-1 C-1 修订：落标是 decline 臂上**新增的 2 记录 journal I/O**，其
异常面（`FILE_JOURNAL_WRITE_FAILED`/锁/byte-segment 上限/出站校验）在
`chain_forecast_execution.py:219/:547` 之上无任何 handler，会把既有
`PipelineResult("failed")` 收场替换成 pass 级
`production_orchestration_failed`。裁决：mark 调用以窄捕获包裹（仅
`OrchestratorError` + `FileOrchestrationJournalError`，绝不裸 `Exception`）
后仍 `return None`——幂等落标下一趟 pass 自愈；不静默：按
`chain_forecast_submission.py:157-166` 既有形发运维可见信号。休眠臂对称
处理（同两类异常回退 pre-#1312 返回值 `_file_retry_namespace(current)`）。
回归测试：mark raise 时 `orchestrate_cycle` 仍产出
`PipelineResult("failed")`、零新行。

### D3 — 休眠臂同判据换出口

`file_orchestration_journal.py:6607-6608` False 臂改为经
`self.mark_permanently_failed` 落标后返回（返回形 `_file_retry_namespace`
不变）。幂等按 D4。该臂生产 dormant，但短路条件将来收窄即唤醒，必须与
D2 同判据且被单臂红证独立钉住（D8）。**Phase-7 C-P2 补充**：spec delta
的写失败韧性 THEN 承诺"the decline exits"（复数）都出运维信号——休眠臂
的窄捕获回退不得静默，须在回退前 best-effort 追加与调用方臂同形的
`permanent_failure_mark_failed` 事件（emission 自身不得抛），并有测试钉
住该事件存在；不以收窄 spec 文本代偿（oracle-integrity：不为通过而弱化
规格）。

### D5 — 新 typed authority transition 的精确形状（round-1 P1-1 + round-2 P1-B）

背景实测：现 `mark_permanently_failed`（`:6704`）持久化走
`self.repository.update_pipeline_job_status`（`:6710`），后者对
current-contract master 行在 `:3240-3244`（锁前）/`:3251-3255`（锁后）无
条件抛 `file_journal_authority_transition_requires_typed_api`；
`transition_pipeline_job_runtime_status` 拒绝全部终态（`:2060-2064`）；
`permanently_failed` 是合法持久 master 状态
（`accepted_submit_identity.py:53-69`，值在 `:68`，
`_validate_outgoing_record` 同集合校验）。

裁决：新增 `mark_pipeline_job_permanently_failed`（名可依仓内惯例调整），
**只从 `reject_pipeline_job_submit_attempt` 借写序**——
`_locked_cycle_write` + `_journal_record_for_write` /
`_validate_outgoing_record` + `_append_journal_records_unlocked` +
`_write_pipeline_job_direct_unlocked`。round-2 P1-B 明确**不可迁移**项：

- reject 的前置（`status=="reserved"` + attempt 匹配 + slurm_job_id 未
  绑，`:2274-2288`）对落标目标（已绑定、已终态的 master）三条全反——不
  迁移；本 API 的**源状态前置**（round-2 P2-D，PR round-1 C-2/R-1 +
  Phase-7 C-P1 裁决收束）：合法源限定 `{failed, submission_failed}`，非法
  源（`running`/`reserved`/`succeeded`/`cancelled`/`partially_failed`/
  `reservation_lost` 等）返回 `stale` 不 raise、零写入零事件。
  **`partially_failed` 整体移出源集**（初版含之，Phase-7 终审 C-P1 裁决移
  除，verifier 双侧运行时探针 CONFIRMED）：`partially_failed` master 唯一
  能到达 decline 的入口是嵌套 partial-array-retry 调用点
  （`chain_forecast_execution.py:547`；主 decline 臂 `:217` 的状态集不含
  它），而 partial cohort 受 #1202 partial-advance 契约约束——成功 basin
  继续走下游 stage。落标会使二趟 resume 经粘性 + projection-commit 白名
  单把 cycle 终态从 `parsed_partial` 翻成 `failed_run`+error_code、下游
  stage 整体跳过（pre-PR 对照探针实证）。partial cohort 的失败成员语义
  是"部分失败、整体推进"，非"整职永久失败"；decline-不落标保持 #1202 契
  约，与 reservation_lost 同属"整职非死局则不落标"原则。
  **`reservation_lost` 整体移出源集**（初版含之，PR round-1 裁决移除）：其两个已知子形都不该落标——
  `identity_mismatch_released`（实施偏离 1 曾特判：
  `accepted_submit_identity.py:608` 禁止该 decision 与其它状态共存，落标
  必 raise 证据不变式错误）与 `absence_retry_permitted`（落标是单向门，
  会同时关死两条只认字面 `status=="reservation_lost"` 的重收路径——
  reclaim 谓词 `:1665-1679` 与 reconcile-verified 重试捷径；identity
  drift 跨版本后该形可达 decline 臂，verifier C-2 存证）。lost
  reservation 语义上是"待重收"而非"永久失败"，decline 方向对 liveness
  fail-safe；原 decision 特判守卫随源集收束成为死码一并移除，两个子形改
  由源集判据统一拒收（测试钉住）。
- reject 的 `AcceptedSubmitTransition.rejected()` 会整组替换 accounting 元
  组（`accepted_submit_identity.py:296-311`）并把 submit_outcome 改
  `"rejected"`——不迁移；本 API **保 accounting 元组原样**：用
  `AcceptedSubmitTransition.accounting(existing 的
  reconciliation_decision/submit_outcome/matched_slurm_job_id, …,
  status="permanently_failed")` 或按
  `_defer_forecast_cohort_projection_unlocked` 形在锁内直接构行；
  `matched_slurm_job_id`/`reconciliation_decision`/`submit_outcome` 落标前
  后逐字段相等（seam 4 断言）。注意
  `AcceptedSubmitTransition(None, status=…)` 会 `__post_init__` ValueError
  （`:213-227`），不可走裸构造。
- reject 的 cohort hydro 成员级联改状态（`:2301-2331`）与硬编码
  `event_type="submission"`/`status_from="reserved"` 事件（`:2333-2350`）
  ——不迁移；本 API 事件为 `event_type="permanently_failed"` + 真实
  `status_from`，仅随真实翻转追加。
- 幂等前置：以 job_id 重读持久行；已是 `permanently_failed` 原样返回、零
  事件。
- `update_pipeline_job_status` 的 `:3240/:3251` 禁令不放宽，负向测试保留。

### D4 — 幂等以持久行为准，service 层 stale 闸对 master 绕开（round-2 P2-E）

`FileJournalRetryService.mark_permanently_failed` 的第一道闸 `:6705-6707`
读**传入对象**的 status——快照已 `permanently_failed` 而持久行仍
`failed` 时会早退、行永不落标（D4 目标失效模式的镜像）。裁决：master 分
支**先按 job_id 重读持久行**再判幂等（对 master 行绕开 `:6705-6707` 快照
闸），幂等与事件语义统一由 D5 新 API 承担。测试双向：stale-复标方向（快
照 failed/持久行 permanently_failed 双驱动 → 事件计数 1）与 stale-漏标方
向（快照 permanently_failed/持久行 failed → 仍落标）。DB 平面
`retry.py:410-411`（ORM 前置）行为不同，分别断言不混用。

### D6 — 落标域裁决：整条 `should_auto_retry` False 臂（round-2 P1-C）

False 臂覆盖两个域：非瞬时码（含 unknown-code 默认非瞬时）**与瞬时码重试
耗尽**（`retry.py:128-149` `limit_exhausted` → `permanent=True`）。裁决：
**整臂落标**，理由：(a) 已批准 spec 本就有两条 SHALL——
`spec.md:153`（非瞬时立即标）与 "Max Retries Exhausted — Permanent
Failure" requirement（耗尽标）——整臂恰好是两者的并集；(b) 非 master 行
今天走 `:6619-6621` 对两个域同样落标，本裁决把 master 对齐到非 master，
无新例外。**Phase-7 C-P1 补充限定**：整臂落标沿 error-code 轴成立，但沿
source-status 轴受 D5 源集约束——嵌套 partial-array-retry 出口
（`chain_forecast_execution.py:547`）带来的 `partially_failed` 源经源集
判据拒收（decline 照旧、零落标），保 #1202 partial-advance 契约；混合
cohort 两趟 e2e 钉 pass-2 与 pre-PR 完全同构。随之而来的 upstream-refresh 语义变化见 D7。spec delta 的 WHEN
显式列出耗尽域并援引 Max-Retries requirement。测试补耗尽域用例（master
行 `NODE_FAILURE` + `retry_count >= max_retries` → 落标 + refresh 不重
投）；低 retry_count 瞬时码反向控制照旧。

### D7 — 后果不变式与 upstream-refresh 裁决

- **无额外重投**：`:166` 终态集合两状态同列；测试断言无新 job 行、无
  `schedule_auto_retry`。
- **upstream-refresh 重投闸门（round-1 P2-3 + round-2 P1-C 合并裁决）**：
  `_terminal_stage_can_retry_after_upstream_refresh`（`:169-178`）白名单不
  含 `permanently_failed`——落标后该分支对 master 行不再触发，**含耗尽类
  瞬时码**。接受，理由：非瞬时域上游刷新治不了病；耗尽域非 master 行今天
  已因正确落标而失去该路径（`:6619-6621`），master 对齐之，且全仓 spec 无
  任何"终态行须经 upstream refresh 复投"的条款（round-2 Q4 检索确认；
  `spec.md:239` 方向相反）。测试钉住：已落标行 +
  `refreshed_upstream_finished_at` → 不重投。
- 手动重试资格保持：`FAILED_PIPELINE_STATUSES` 两状态同组
  （`scheduler_state_types.py:33`；predicate
  `scheduler_state_manual_retry.py:703`）。

### D9 — cohort projection 终态粘性（round-2 P1-A，新增）

静态链（`chain_forecast_execution.py:182-190` →
`chain_stage_execution.py:843-860`（`permanently_failed` 排除被
accepted-submit reconcile 分支覆盖）→ `chain_array_accounting.py:308-330`
→ `project_forecast_cohort_tasks` `file_orchestration_journal.py:2993-3021`
无条件用 `projected_master_status ∈ {succeeded, partially_failed, failed}`
改写 master 行）：第二趟 pass 会把标抹回 `failed` + 追加 `status_change`
事件，第三趟再落标——振荡 + 每 pass 双事件。对照 defer 路径 `:3173-3175`
**已有**终态 idempotent 短路（`permanently_failed` ∈
`TERMINAL_PIPELINE_STATUSES` `:247-255`）。裁决：给
`project_forecast_cohort_tasks` 的 master 写面加同形粘性——existing 行已
`permanently_failed` 时保留状态（projections/证据字段照常更新），与
`:3173-3175` 同构；两个 projection 入口（`chain_array_accounting.py:308-330`
与 `reconcile.py:1035-1050`）经同一函数自动覆盖。实施前运行时探针复证抹
回链（与 task 0 同批）。e2e 必须驱动**第二趟** `orchestrate_cycle`：断言
二趟后行仍 `permanently_failed` 且 `permanently_failed` 事件计数仍 1。

实施修订（偏离回写）：(a) 本设计静态链漏掉 projection-commit 验证器
`chain_array_accounting.py:405-426`——其 durable-outcome 白名单不含
`permanently_failed`，仅加粘性会让二趟 resume 抛
`ACCEPTED_SUBMIT_PROJECTION_COMMIT_UNAVAILABLE`；已把该状态补入白名单
（`:412-424`，实施偏离 2）。(b) 二趟 e2e 实测走 defer 分支（resume 路径
的 `master_slurm_job_id` 传的是 pipeline job id，命中 identity-mismatch
defer 的 `:3173-3175` 既有终态短路），咬不到粘性本体——D9 红证由
journal 级粘性测试承担，e2e 保留钉端到端不变式（实施偏离 3；defer 误路
由本身是 pre-existing，另行路由）。

### D8 — 红证：两次单臂回退（round-1 P2-5；PR round-1 S-2 更正）

仅回退 caller 臂 → 仅 caller 测试红、journal 测试绿；仅回退 journal 臂 →
反之（可达性已核：caller 测试止于 `:197`，journal 测试直连
`handle_failed_job`，路径不相交）。两组 pytest 输出各留一份。D9 projection
粘性红证由 **journal 级粘性单测**承担
（`test_cohort_projection_keeps_a_permanently_failed_master_sticky`，
D9-revert 突变下唯一变红）；二趟 e2e 因 resume defer 误路由（#1410）咬不
到粘性本体，只钉端到端不变式，不参与该红证。

## Seams under test（round-2 修订）

1. 调用方臂：file-journal service + master 行非瞬时码 → None + 读回
   `permanently_failed` + 单条事件。
2. 休眠臂：`handle_failed_job` 直连 master 行非瞬时码 → namespace 与持久
   行均 `permanently_failed` + 事件。
3. store-less 负向：`RetryService(None,…)` + master 行 → None、不抛、零落
   标（anchor `tests/test_orchestration_chain.py:1637-1652` 原样通过）。
4. 新 API 单测：终态失败源合法写入 + accounting 元组逐字段保持 +
   `running`/`reserved` 源 → stale 零写入零事件 +
   `update_pipeline_job_status` 对 master 仍 raise。
5. 幂等双向：快照 stale 双驱动 → 事件计数 1；快照 permanently_failed/持久
   行 failed → 仍落标（round-2 P2-E 镜像）。
6. 反向控制：master 行 `NODE_FAILURE`（低 retry_count）→ retry identity 流
   不变、零落标。
7. 耗尽域：master 行 `NODE_FAILURE` + `retry_count >= max_retries` → 落标
   + refresh 不重投（D6）。
8. upstream-refresh：已落标行 + `refreshed_upstream_finished_at` → 不重投
   （D7）。
9. e2e 两趟：OOM master 行第一趟落标、第二趟 resume 后行仍
   `permanently_failed`、事件计数仍 1、无 raise、两趟 PipelineResult 均
   "failed"（D9；fake 装配复用
   `tests/test_orchestration_chain.py:8843-8853` 形，OOM 经
   `_write_task_outcome_receipt` `:1389` 注入）。
10. 码覆盖：两臂各参数化 `OUT_OF_MEMORY` + `INVALID_MANIFEST`。

## Risk packs

- Selected: terminal-state-semantics（重投双向 + 抹标振荡）· idempotency
  （事件计数 + 双向 stale）· oracle-integrity（anchor 不弱化 + 单臂红证）
  · authority-surface（新 typed transition 前置/元组保持 + projection 粘
  性，不放宽 master 写禁令）。
- Not selected: concurrency（新 API 与粘性均在既有 `_locked_cycle_write`
  锁形内）· performance（每失败 O(1)）· security（无新输入面）·
  data-migration（状态值早已存在于全部消费面）。

## Evidence mapping

- AC"两处副本一致 + 负向测试锁几何" → seams 1/2 + D8 单臂红证。
- AC"覆盖 NON_TRANSIENT（至少加 INVALID_MANIFEST）" → seam 10；耗尽域并
  集 → seam 7。
- AC"非 master 零回归、无额外重投" → seams 3/6 + D7 + 四套件全量回归。
- AC"事件追加" → seams 1/2/5（单事件断言）。
- Spec delta 场景 ↔ seams 1/2/5/7/8/9/3 一一对应。

## Non-goals

- `spec.md:154` `auto_retry_skipped` payload（#1314）。
- db-free 决策梯 permanence 语义与 store-less 平面落标（#1313）。
- DB 平面 `handle_failed_job`（#1161 已对齐）。
- 其它 error code 分类审计；手动重试路径；projection 对非
  `permanently_failed` 终态的粘性语义（保持现状）。
