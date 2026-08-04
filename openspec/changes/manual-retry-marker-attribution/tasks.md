# Tasks — manual-retry-marker-attribution (#1205)

Anchors verified at master af66f164 (this session, explorer re-read):
`services/orchestrator/scheduler_state_manual_retry.py`（全文 399 行）
`_manual_retry_requested:31-40`、`_manual_retry_markers:42-80`
（扫描环 `:66-79`，state 源 `:44-64`）、
`_manual_retry_marker_record:82-115`、
`_latest_manual_retry_marker:117-121`、
`_manual_retry_marker_repairs_historical_failure:123-147`（issue 只
引了首分支 :123-129）、`_manual_retry_marker_bound_to_blocker:313-326`、
`_event_is_manual_retry_marker:334-338`、`_manual_retry_payload:357-380`
（`:366` 第二份无作用域扫描）、`_manual_retry_new_attempt:382-399`
（`:388` 第三份）；`_state_jobs`/`_state_events`
（`scheduler_state_rows.py:373-402`/`:404-409`；job 行携带
`job_id`/`pipeline_job_id`/`model_id`/`stage`；event 行携带
`event_id`/`entity_type`/`entity_id`/`details`）；泄漏通道
`file_orchestration_journal.py:8443-8462`/`:8477-8500`、
`chain_repository_state.py:535-559`、`_pipeline_event_target:3311-3329`
（forecast_cycle → model_id 恒 None）；放大链
`scheduler_state_decision.py:126/:240-245`、
`scheduler_state_evidence_owner.py:108`（每分支无条件填 payload）、
`scheduler_state_failure.py:1080-1127`、
`scheduler_candidate_manifest.py:239-242/:263-279`、
`chain_runtime_utils.py:305-326`、
`chain_forecast_orchestrator_cycle.py:146-148`（attempt 短路；
`:156-166` 为 `_terminal_stage_needs_manual_retry`）；归属先例
`scheduler_state_identity_filter.py:594-622`
`_pipeline_terminal_success_is_candidate_scoped`（cycle-scope 显式
return False）。

Risk triage: fixture level **expanded**（M-size；生产语义改动位于
调度决策主路径；fixture review round 1 以 in-memory 等价实现实测
证伪初版全量 fail-closed 谓词与 site 4 合取——修订为两刀窄修复，
取舍与否决记录见 design.md）。Risk packs selected: **oracle-discrimination**
（负向用例必须配同构归属对照，证明翻转由谓词导致；修复前红证明
必需）+ **record-forensic**（evidence/payload 的字段级断言：
`manual_retry.new_attempt` 不得出现 vs 出现且值正确——错误方向是
"证据被静默点亮"）。Not selected: concurrency-lifecycle（无线程/
锁语义）、performance/UI/migration（n/a——migration 测试文件只是
用例载体）。

ORACLE ROUTING（本 run 常设纪律：不使用 node-22）：

- issue Verification 引用的 `tests/test_scheduler_replay_admission.py`
  在 master **不存在**（parked 于未合并分支
  feat/issue-1164-six-basin-replay，explorer 以 git log --all 证实）
  ——记录为 deviation，验证命令替换为存在的四套件（review P2-3：
  真正锁定决策主路径语义的是 production_scheduler/orchestration_chain，
  必须纳入）：journal（基线 199）+ migration（25）+
  production_scheduler（1046）+ orchestration_chain（数值见 E2），
  本机 af66f164 实测。
- issue 验收末条"node-27 或 node-22 上任选一个真实 cycle 快照验证"
  ——**node-27 read-only** 满足（本 run 允许对 27 只读取证）：ssh 27
  `cat` 一份含 cycle 粒度 marker 的 latest 快照（issue 现场
  event_id 907 / IFS/2026070512，若已被清理则取任一含
  `entity_type=forecast_cycle` + `trigger=manual` 事件的快照；均无
  则记录"现网无此形事件"并以本地合成 state 覆盖），在本地对该
  真实 JSON 跑 `_manual_retry_requested`/`_manual_retry_payload`：
  修复前 True/点亮、修复后 False/不点亮。零写入、零 env 变更。
  27 不可达或快照缺失不阻塞 merge（该项是 confirmatory receipt，
  判定力已由判别用例承担），但结果必须如实记录进 PR body。
- **Decision-path 判别对规则（round-3 retro 修正动作 F-E，防复发）**：
  两刀的每个测试家族必须含至少一个经
  `_candidate_state_decision_state`（或 `_candidate_state_decision`）
  求值的判别对——raw-state helper 层的绿**不构成**生产 decision
  路径的证据（round-3 A1/A2 教训：identity_filter 消毒使 17 个
  raw 层测试对两刀在 decision 路径的失效全盲）。

Must-preserve behavior:

- 本 model 自己 job 的 manual retry marker 仍被采信：
  `tests/test_file_orchestration_journal.py:2473-2513`、
  `tests/test_file_orchestration_migration.py:169-207/:445-466/
  :469-497/:500-523/:526-557` 全部既有用例零改动、保持通过。
- state 顶层 `manual_retry`/`manual_retry_marker` 标志（"state" 源，
  order=-1）语义不变，不过归属谓词。
- `_manual_retry_marker_bound_to_blocker`/`_latest_manual_retry_blocker`
  /`_manual_retry_marker_repairs_historical_failure` 的既有判定逻辑
  不变（只上游收窄进入 marker 列表的事件集）。
- 读侧行可见性契约零 diff：cycle-wide 事件仍 materialize 进各
  model 快照（`tests/test_file_orchestration_journal.py:1780-1825`
  保持通过）——修的是采信不是可见性。
- cycle 级 stage 人工重试语义（spec 锁定面）：
  `tests/test_production_scheduler.py` 全部既有用例（基线
  **1046 passed**，review 实测，含 `:2671/:5753/:15154/:15562/
  :15700` 五个曾被初版谓词翻红的 marker 用例）与
  `tests/test_orchestration_chain.py`（基线数值见 E2）零改动
  保持通过。
- 套件计数：journal 199 → 199+新增、migration 25 → 25+新增（新增
  用例放哪个文件由 implementer 按就近 fixture 风格定，两文件计数
  合计 +6±1；数值在 PR body 复述）。
- Frozen（零 diff）：proposal Impact 列出的 9 个下游/读侧生产文件
  + `scheduler_state_rows.py`。**Round-3 修订**：
  `scheduler_state_identity_filter.py` 从冻结面移出——round-3
  verifier CONFIRMED 其 decision-event 消毒白名单剥离刀 1 判据字段
  （`entity_type`/`model_id`），冻结前提"零 diff 即安全"被证伪；
  改动限定为消毒白名单放行三个判据键（r4-diagnosis 实测 378 个
  decision-state 差分纯增量、1287+1522 例零回归）。冻结面规则
  同步修订为"零**未经诊断定标**的 diff"。

Seams under test (upstream-declared, consumed not renegotiated):

- 派生候选 model 集（`_state_jobs` 非空 model_id）仅用于刀 1 的
  显式归属出口比对——不再是全量采信的关键路径（review P1-1 指出
  journal `_job_matches_candidate:8443-8461` 首分支不查 model_id，
  行过滤不变量在 journal 路径上弱于 DB 路径，故初版"查回即采信"
  被否决；修订后该弱不变量不再被依赖）。
- 生产事件行无 model 维度（journal `:3225-3234` 不持久化、DB
  `:537-546` 不 SELECT）：刀 1 出口的关键前提——生产
  forecast_cycle marker 现状必然 fail-closed；合成/历史 state 把
  model_id 放事件顶层（`tests/test_production_scheduler.py:15165`），
  故出口同时读顶层字段。
- cycle-wide 事件可见性（`ff91e722` #841 + `cd952225` 有意保留）：
  诊断/evidence 需要看到 cycle 事件——所以修采信侧而非读侧。
- job id 文法 `fcst_<source>_<stamp>_<model>_<stage>[_r<N>]` 与
  cycle-scope `run_id=cycle_<source>_<stamp>[_suffix]`+model_id None
  （`chain_runtime_utils.py:378/:382`）。

Non-goals: 读侧可见性收窄；marker 新鲜度（#1201）；
`skipped_duplicate_submission` 穿透（#1202）；manual retry 写入侧/
db-free 执行入口（#1186）；job_limit 截断降 0（#1179）；
写入侧为 forecast_cycle marker 补
`details.model_id` 的运维出口文档化（本 change 只交付读侧对齐面）。

Minimal mergeable slice: 刀 1 + 刀 2 + 判别对回归（2.1/2.2/2.3）
一起（刀无接线不生效，接线无负向用例不可证；两刀分拆会让 issue
两条验收通道只闭合一半）。

## 1. 谓词与接线

- [x] 1.1 `scheduler_state_manual_retry.py` 刀 1（采信侧，窄）：
  新增谓词——事件 `entity_type == "forecast_cycle"` 且显式归属
  （`details.model_id` 或事件顶层 `model_id`）∉
  `_candidate_model_ids(state)`（`_state_jobs` 非空 model_id 集，
  空集→出口关闭）时不采信；其余 marker 采信不变。接线于
  `_manual_retry_markers` 扫描环、`_manual_retry_payload`、
  `_manual_retry_new_attempt` 三处（continue 形）。marker 形判据
  （event_type + trigger/marker 标志）抽为共享 helper，三份逐字
  重复收敛。`_event_is_manual_retry_marker` **不接线**（design.md
  否决记录；其 blocker 排除语义 scope 无关，第二消费者
  `scheduler_state_failure.py:1034` 因此同样不受影响）。
- [x] 1.2 刀 2（钉值侧；本条 round-1/2 修订后形态，初版"无条件
  回落"已被 round-2 实测证伪并废止）：最新携 `retry_count` 的
  adopted marker 定权（终止性）；正向解析为 cycle-scope job
  （`model_id` 空 ∧ `run_id` 前缀 `cycle_` 文法）的事件按 stage
  感知钉值规则裁定——resolved job 活失败 ∧（failed_stage ==
  job.stage ∨ 候选无自身 model 域活失败行）→ 钉 `retry_count`，
  否则终止回落 `previous_attempt + 1`。采信（requested/marker
  点亮）不受刀 2 影响。函数签名保持 `(state)` 形，零调用点扩散。

## 2. 回归测试

- [x] 2.1 通道 1 判别对：cycle 粒度（entity_type=forecast_cycle、
  无归属）manual marker → sibling 候选 `_manual_retry_requested`
  False、`_manual_retry_payload` 不点亮 marker；同事件
  `details.model_id` =候选自身 → True，事件顶层 `model_id` 变体
  同断言（出口两形验证）。出口用例的 state **必须含至少一条带
  model_id 的 job 行**（派生集合空则出口关闭——这是规定行为，
  不得为让用例转绿而放宽谓词）。
- [x] 2.2 通道 2 判别族（round-2 有 spec 依据修订，verifier 裁定
  原 `== 1` 断言在 rc=0 下无判别力且编码了过度推广语义；oracle 是
  `_manual_retry_new_attempt` 与 `_manual_retry_state_evidence` 的
  **值**）：同 stage cycle-scope marker（rc=5）→ new_attempt ==
  **5**（master parity，`test_same_stage_cycle_scope_manual_marker_
  pins_attempt`）；通道 2 危害负向移到真实形——候选 forecast 失败
  + 交叉 stage cycle marker → new_attempt == previous+1（cycle
  计数不越 forecast 预算）；stale（resolved job 已 succeeded/
  repaired）→ 回落；同构对照：本 model job marker rc=5 → 钉住
  语义与既有一致。
- [x] 2.3 site 4 守卫（防回归而非新语义）：外来 marker 形事件
  （`status_to="pending"`，`retry.py:517` 生产形）与本候选自身
  marker 共存 → `_manual_retry_requested` 仍 True（外来 marker 未
  被当成 active blocker 压制）。

## 2b. Round-4 decision-path 修复（三轮门 retro-r3 修正动作）

- [x] 2b.1 F-A1 identity_filter 消毒白名单放行三判据键
  （`entity_type` + 顶层与 details `model_id`，附判据注释）；
- [x] 2b.2 F-A2 刀 2 entity-unresolvable N1′ 窄化
  （`^job_cycle_<src>_<stamp>_...` fullmatch → 不钉；镜像
  journal `_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE`）；
- [x] 2b.3 F-B `_cycle_scope_marker_pins_attempt` 状态臂补
  placeholder 排除（活失败域第三处对齐）+ 判别测试；
- [x] 2b.4 F-C `test_model_less_candidate_run_job_marker_still_pins_
  attempt_in_cohort_shape` 改造为 run-id 合取的真判别锚
  （交叉 failed_stage + 活失败 model 域行；mutant 必红验证）；
- [x] 2b.5 decision-path 判别对 T1-T11 落地
  `tests/test_production_scheduler.py`（含 A2 杀手 T7/T8、parity
  守卫 T9、`:15700` 守卫 T10、顶层 model_id 唯一杀手 T3、
  `_candidate_state_decision_event` 消毒契约 T11）；
- [x] 2b.6 F-D proposal/tasks 与 round-2/3 已交付语义对齐
  （本节与 1.2/2.2/Frozen/ORACLE ROUTING 修订即是）；
- [x] 2b.7 A3（requested 翻转）经 32 格矩阵定标为**不加规则**
  （C1 对照 == 矩阵行 1；抑制在新 max 上正确重求值），decision
  reason 收紧（`manual_retry_requested`→`retry_failed_candidate`
  于 own-target-repaired 形）在 design.md 与 PR body 披露。

## 2c. Round-5 N1′ 证据等价化（第二次 depth retro 修正动作，硬顶轮）

- [x] 2c.1 F-N1 `_marker_event_pins_attempt` job-is-None 分支实现
  fixA（verifier 五树实测定标：entity `(source,stamp)` 与 state
  `run_id` 比对 cycle 归属；同 cycle 同 stage 钉 / failed_stage
  缺失走 arm 2 / 异 cycle 或无法判定不钉 / 非 cycle 文法
  fail-open 不变）；
- [x] 2c.2 F-N2 测试锚：同 cycle cohort 判别锚（HEAD 红==1）、
  真实 `candidate_state_from_rows` 截断锚（HEAD 红==1）、T10
  rc=7 判别力恢复、M6 文法锚、V-E 4 格 characterization；
- [x] 2c.3 F-N3 文本：spec N1′ 子句改写为证据等价语义（消除与
  requirement 主句自相矛盾）、design.md round-4 修订节（含 F5′
  保守残留与方向 (ii) 否决记录）、identity_filter 注释假不变量
  更正、V-E 披露两方向补齐；
- [x] 2c.4 V-D 13 形矩阵作为回归断言复跑一致 + mutant 复核
  （helper 恒 False / fullmatch→startswith 必红）。

## 3. Spec + validation

- [x] 3.1 Spec delta: ADDED requirement in `job-retry-mechanism` ——
  manual retry marker SHALL 经候选归属裁定后才被采信；3 scenarios
  （cycle 粒度 fail-closed 与显式归属出口；cycle-scope job 不钉
  候选级 attempt；本 model marker 正向不回归）。
- [x] 3.2 `openspec validate manual-retry-marker-attribution --strict
  --no-interactive` green.

## Evidence Floor

- [x] E1 修复前红证明（orchestrator backup-copy + `cmp` restore）：
  仅还原 `scheduler_state_manual_retry.py` 至 master 形态 → 2.1/2.2
  负向用例红（requested 翻 True / new_attempt 被钉 5，实测
  `assert 5 == 1`）、2.3 与
  归属出口及全部既有正向用例绿（2.3 在 master 上本就绿——它守卫
  的是"修复不引入 active-blocker 回归"，非判别 oracle，如实标注）；
  恢复后全绿。`cmp` 确认还原字节一致。
- [x] E2 计数对齐：journal 199 / migration 25 /
  production_scheduler **1046** / orchestration_chain **280**
  （基线均本机 af66f164 实测；chain 套件 ~12min 属慢套件，修复后
  复跑一次即可）→ 修复后仅差新增用例数；
  四套件既有用例零改动。验证命令（替换 issue 引用的不存在文件）：
  `uv run pytest -q tests/test_file_orchestration_journal.py
  tests/test_file_orchestration_migration.py
  tests/test_production_scheduler.py tests/test_orchestration_chain.py`。
- [x] E3 `uv run ruff check .` green; openspec strict green.
- [x] E4 Surface check: `git diff master...HEAD --name-only` = 2
  生产文件（`scheduler_state_manual_retry.py` +
  `scheduler_state_identity_filter.py`，后者 round-3 修订经诊断
  定标解冻，见 Frozen 节）+ 2 测试文件 + 本 openspec change +
  review-gate 状态文件；其余 frozen 面零 diff。
- [ ] E5 CI `Unit Tests` green on PR head。
- [ ] E6 node-27 read-only 真实快照 receipt（confirmatory，见
  ORACLE ROUTING：结果如实记录，缺失不阻塞）。
