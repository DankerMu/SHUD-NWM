# Design — manual-retry-marker-attribution (#1205)

## 修订史

fixture review round 1 证伪了初版设计的两个核心选择（P1-1/P1-2，
均经 in-memory 等价实现在既有套件上实测复现回归），本版为修复后
形态；初版要点保留在下文"否决记录"以防回退。

## 核心判断：哪一类 marker 是缺陷、哪一类是产品语义

既有产品语义（被 spec + 测试锁定，**不得**收窄）：

- cycle 级 stage（download_source_cycle / convert 等）的人工重试
  就是要被该 cycle 的候选采信——
  `openspec/specs/job-retry-mechanism/spec.md`（Retry Scope：
  cycle-level stage failure 重试整个 stage）、
  `openspec/specs/retry-runtime-roots/spec.md:34`、
  `openspec/specs/multibasin-state-idempotency/spec.md:61-62`；
  `tests/test_production_scheduler.py` 5 个用例
  （`:2671/:5753/:15154/:15562/:15700`）实测锁定：无 entity_id 的
  marker、无 job 行的 state、cycle-scope job 的 marker 都必须能让
  候选走 `manual_retry` 决策。
- 真正的缺陷只有两条（issue 验收通道）：
  1. `entity_type=forecast_cycle` 的 cycle 粒度 marker 无任何 model
     归属却被全体 sibling 采信（#1164 现场 event 907 即此形）；
  2. cycle-scope job marker 的 `retry_count` 钉死候选 forecast 级
     `new_attempt`/`retry_attempt`（采信合法、**钉值**越界）。

因此修复分两刀，互不越界：

**刀 1（采信侧，窄）**：仅当 `event.entity_type == "forecast_cycle"`
且无显式 model 归属时，marker 不被采信。显式归属出口：事件
`details.model_id` **或事件顶层 `model_id`**（合成/历史 state 把
model 放顶层，`tests/test_production_scheduler.py:15165`；生产
journal/DB 事件行均无 model 列——`file_orchestration_journal.py:
3222-3231` 不持久化、`chain_repository_state.py:537-546` 不 SELECT
——所以生产 forecast_cycle 事件现状 100% 走 fail-closed，出口是
为写入侧未来补 model_id 预留的对齐面）∈ 候选 model 集合。
其余 marker（entity 是 job、无 entity_id、任何非 forecast_cycle
形）采信语义一律不变。

**刀 2（钉值侧）**：`new_attempt` 派生（`_manual_retry_new_attempt`
及 payload 的 attempt 字段）跳过**正向解析为 cycle-scope job** 的
事件的 `retry_count`（entity_id 在 `_state_jobs` 查回且该 job
`model_id` 为空 **且 `run_id` 匹配 `cycle_<source>_<stamp>[_suffix]`
文法**——镜像 journal `_is_model_less_cycle_scope_job:8405-8414`
的双合取；round-1 修订，见下）——回落 `previous_attempt + 1`，
且回落是**终止性**的：最新携带 `retry_count` 的 adopted marker
定权（与 payload 环 break-at-newest 同语义），不回扫更早 marker
（否则落到候选自己过期 marker 的 retry_count，产出
`new_attempt == previous_attempt` 的已消耗 attempt 号）。采信（requested/
marker 点亮）不受刀 2 影响；entity 查不到 job 的事件（含无
entity_id）钉值行为不变（`:15154` 型合成 state 依赖）。刀 2 只切
attempt：payload 其余字段（`previous_job_id`/`prior_failure_reason`/
`slurm_job_id` 的 setdefault）与 markers 列表记录里的 `attempt`
（`_manual_retry_marker_record:106`，参与 bound_to_blocker 判定与
max() 排序）**保留外来值**——采信语义不变即应如此，仅 forecast 级
new_attempt 派生不越界。

**site 4 不接线**：`_event_is_manual_retry_marker` 保持 scope
无关。理由（review P1-2 实测）：它的唯一语义是把 marker 形事件
排除出 blocker 扫描（`scheduler_state_manual_retry.py:198`；
第二消费者 `scheduler_state_failure.py:1034` 同语义），blocker
扫描无 event_type 过滤、真实人工重试事件带 `status_to="pending"`
（`retry.py:517`）∈ ACTIVE——合取归属谓词后外来 marker 会变成
active blocker 直接压死候选自己的人工重试（实测 requested
True→False 回归）。marker 形事件无论归属都不该当 blocker；且
候选自己的真实 blocker 事件不是 marker 形，从不会被误跳——
初版立论（"泄漏使真实 blocker 被跳过"）不成立。

## 候选 model 集合（仅刀 1 出口比对用）

`_candidate_model_ids(state)` = `_state_jobs(state)` 非空
`model_id` 值集。空集 → 出口关闭（forecast_cycle 无归属 marker
照拒）。该派生仅用于**新增能力**（显式归属出口）的比对，不参与
任何既有采信路径，故空集退化无既有语义可破坏——这与初版"全量
fail-closed + 派生身份"不同，后者把派生集合放上了全部 marker 的
关键路径（P1-1 翻红 5 用例的根源）。

## 否决记录

- **初版全量 fail-closed 谓词**（job 查回非空 model 采信 /
  cycle-scope 拒 / 查不到拒）：review 实测
  `tests/test_production_scheduler.py` 5 failed——cycle 级 stage
  人工重试对全体候选失效，违反 3 份既有 spec。否决。
- **路线 B（candidate 线程穿签名）**：`scheduler_state_identity_filter
  .py:594-622` 先例确为该形状，但本修复收窄后不再需要真实
  model 相等比对（刀 1 只看 entity_type + 显式归属出口；刀 2 只看
  job 行自身的 model_id 空否），签名扩散（≥3 模块，evidence owner
  为冻结面）无对应收益。维持否决，先例矛盾在此记录并解释。
- **site 4 合取**：见上，P1-2 实测否决。

## Round-1 cross-review 修订（PR #1286）

round-1 交叉审核 + 独立 verifier（3 CONFIRMED / FIX_NOW）修订三处，
上文刀 2 描述为修订后形态：

1. **回落终止性**（P1，attempt-derivation）：初版 `continue` 形在
   多 marker 状态回扫到候选自己过期 marker 的 `retry_count`，实测
   `new_attempt == previous_attempt`（already-consumed attempt →
   静默 no-op）。修订为最新携值 adopted marker 定权、cycle-scope
   命中即终止回落 `previous_attempt + 1`。
2. **cycle-scope 谓词补 run-id 文法合取**（predicate-domain）：
   初版仅 `model_id` 空判据在多流域生产 cycle 误伤全部 marker——
   `chain_runtime_utils.py:65-68`（`len(all_basins) != 1` →
   所有 stage 行 model-less）+ `accepted_submit_identity.py:316-328`
   （forecast-cohort 行强制 model-less）。修订为 `model_id` 空 ∧
   `run_id` 前缀 `cycle_` 文法（候选 run 是 `fcst_...`）。
3. **归属出口 mutation 盲区补断言**（test-coverage）：verifier
   实测出口成员判断换 `return True` 后套件全绿；补 foreign-model
   负向与派生集空关出口两断言。

已披露的残留（多流域 cohort 形）：全部 job 行 model-less 时
`_candidate_model_ids` 恒空 → 刀 1 显式归属出口在该形**永久
fail-closed**（今日生产事件行本就无 model 列、出口本就不可达，
性质为保守方向；写入侧未来补 model_id 时需同时评估 cohort 形
的派生集来源，记录于 PR body 兼容性节）。

## Round-2 cross-review 修订（PR #1286，pattern escalation）

round-2 全量复审（integration 全量 scope + verifier 实测裁定 + 只读
诊断在 5 个形上定标）证伪了 round-1 修订的"无条件 previous+1 终止
回落"——它把 issue 通道 2（不钉 **forecast 级** 预算）过度推广到了
同 stage 的 cycle 级预算，实测把 operator 钉的 download attempt 从
5 塌成 1（master parity 破坏；生产投影 cohort-download 形在完整
decision 路径复现）。修订为 **stage 感知钉值**：

- cycle-scope marker 钉值判据（诊断五形 A/B/C/D/E 实测定标）：
  resolved job 仍 ∈ FAILED 态 ∧（raw `failed_stage` == job.stage
  ∨ 候选无自身 model 域 failed 行）→ 钉 marker retry_count
  （A 手搭同 stage=5、B 生产投影=5 恢复 parity）；否则终止回落
  previous+1（C stale=1、D 多 marker=4、E 双失败=4 不越预算）。
- **2.2 oracle 修订**（verifier r2 裁定：原断言 `new_attempt==1`
  在 rc=0 下无判别力且编码了过度推广语义）：改为同 stage 正向
  （download job rc=4 + marker rc=5 → 钉 5，master parity）；
  通道 2 负向移到真实危害形（候选 forecast 失败 + 交叉 stage
  cycle marker → 1）。这是有 spec 依据的 oracle 变更，非削弱。
- cohort master 前提句更正（r2-cand-02）：cohort master 行 run_id
  恒为 `cycle_` 前缀（`cycle_<src>_<stamp>_<stage>_<model_id>` /
  `..._cohort_<digest>`），会被谓词判为 cycle-scope；钉/不钉由
  钉值判据决定。round-1 §2 的"候选 run 是 `fcst_...`"表述仅对
  真 fcst 行成立。
- 诊断陷阱备案：投影形 `failed_stage` 为 None（`stage` 从候选
  succeeded 行填充）；`download` 不在 DOWNSTREAM_STAGE_ALIASES →
  `_state_retry_attempt(stage="download")` 走 flat 分支；
  identity_filter 对 details 无 job id 字段的 marker 整体消毒
  （测试 marker 必带 `previous_job_id`）。
- Phase 6.2 审计补钉（活失败域）：钉值判据中"失败行"一律取
  repo 既有**活失败**域——status ∈ FAILED 且非
  repaired-stage-evidence 且非 unsubmitted auto-retry
  placeholder（与本模块 blocker 扫描同域）；spec 的
  "failed model-scoped job row" / "still in a failed status"
  均按此域解读。arm 2 误关（repaired/placeholder 行挡钉值，
  实测 5→2/1 回归）与状态臂半开（repaired cycle-scope 行钉 5）
  两洞同轮修复。

## 回归测试 oracle（与刀对齐）

- 通道 1 判别对：forecast_cycle 无归属 marker → sibling
  `_manual_retry_requested` False、`_manual_retry_payload` 不点亮；
  同事件 `details.model_id`（及顶层 `model_id` 变体）=候选自身 →
  True。
- 通道 2 判别对：cycle-scope job marker（entity_id 查回、model_id
  空、`retry_count=5`）→ `_manual_retry_new_attempt(state,
  previous_attempt=0)` == 1（未被钉成 5+1 或 5），
  `_manual_retry_state_evidence` 的 `manual_retry.new_attempt` ==
  previous+1；同构对照：本 model job marker `retry_count=5` →
  new_attempt 与既有语义一致（钉住）。
- site 4 守卫：外来 marker 形事件（status_to=pending）+ 本候选
  自身 marker 共存 → requested 仍 True（marker 形事件未被当
  blocker）。
- 修复前红：backup-copy + `cmp` restore，通道 1/2 负向必红；
  全部既有正向绿。
