# Tasks: cycle-pin-live-failure-domain

## Risk triage

- Issue type: bugfix (#1287)
- Project profile: NHMS
- Blast radius: medium（attempt 跳号→manifest `retry_attempt`；触发形状窄）
- Fixture level: expanded（domain trigger: `orchestrator` state machine；
  upstream suggested level 缺省——issue-scribe 产出无该字段）
- Repair intensity: medium（单谓词域修正；共享谓词的两个消费者都在同一文件，
  同域是目标不变量）
- Selected risk packs:
  - `oracle-discrimination`（selected：issue 给出两对判别值 1↔5 / 3↔5，测试必须
    红能力在案）
  - `invariant-state`（selected：两消费臂同域不变量 + arm 1/回落语义保留）
  - `spec-compliance`（selected：主 spec 两处字面 + 相邻 live-failure 子句
    与实现同步）
  - `integration`（not selected：下游 `scheduler_state_failure.py` 只消费返回值，
    无接口/字段变化）
  - `security/perf`（not selected：无此类面）
- Evidence floor: 见 §E

## 1. Implementation

- [x] 1.1 `_state_has_candidate_scope_failed_job`：job 行失败判据扩为
  `FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`（与 blocker 谓词同源常量形），
  repaired-evidence / placeholder 排除保留且对 cancelled 行同样生效。
- [x] 1.2 同函数追加 hydro 腿：`hydro_status ∈ {failed, cancelled,
  permanently_failed}`（blocker hydro 谓词的失败半边），字段读取口径与本模块
  blocker 扫描一致。
- [x] 1.3 spec 措辞同步：本 change 的 MODIFIED delta 已含主 spec 两处
  "no failed model-scoped job row" 字面（:330、:379-380）及相邻 live-failure
  子句的活失败域改写；本 task 在 PR 内的完成态 = delta 内容定稿 + strict 校验
  通过（主 spec 落库发生在 merge 后 `openspec archive`，不在本 PR diff 内）。

## 2. Tests（新判别测试放 tests/test_production_scheduler.py；既有护栏集在
tests/test_file_orchestration_migration.py:576-1010 一带，两文件都必须跑）

- [x] 2.1 判别对 1：own forecast=`cancelled` + 跨 stage cycle-download marker
  `retry_count=5` → `_manual_retry_new_attempt(prev=0) == 1`（当前 red=5）。
- [x] 2.2 判别对 2：jobs 全 succeeded + `hydro_status=failed` + 同 marker →
  `_manual_retry_new_attempt(prev=2) == 3`（当前 red=5）。
- [x] 2.3 hydro cancelled 变体 → 回落（与 2.2 同构，`hydro_status=cancelled`）。
- [x] 2.4 回归护栏：own ∈ {failed, permanently_failed, submission_failed,
  partially_failed} 仍 == 1；arm 1 同 stage 命中仍钉 marker 值；repaired stage
  evidence / unsubmitted placeholder 仍不算活失败（钉值不回归）；cancelled 的
  placeholder 形状行在 placeholder status 门（`{pending, submission_failed}`）
  之外，与 blocker 扫描一致地计为活失败（判别测试断言两侧同域）。
- [x] 2.4b arm 2 正向护栏（ACTIVE≠失败不变量）：own jobs 全 succeeded + 无
  hydro 失败 + 跨 stage cycle-download marker `retry_count=5` → 仍钉 5
  （`prev=0 → 5`）；且 `hydro_status` 为 ACTIVE 值（如 `running`）的变体仍钉 5
  ——防止实现误用整只 blocker 谓词（含 ACTIVE 半边）而非其失败半边。
- [x] 2.5 unresolvable 臂同域：无 failed_stage + own cancelled 行 +
  `job_cycle_*` 语法 unresolvable marker → 不钉（`_marker_event_pins_attempt`
  路径，两臂同域不变量的判别形）。
- [x] 2.6 红证明：2.1/2.2/2.3/2.5 在 pre-change 源上 red（批量 stash 法），
  实现后 green；输出入 implementer 报告。

## 3. Round-2 fix set（cand-INT1 P1 + cand-D/E P2，verifier 全 CONFIRMED）

- [x] 3.1 fallback 兜底 clamp（生产，仍仅本文件）：`_manual_retry_new_attempt`
  的 prev+1 回落在候选自身活失败无法解析 canonical failed stage 时（cancelled
  行 / hydro 失败），以候选域内（非 cycle-scope）行的 durable retry-suffix 记录
  （`effective_retry_attempt` 类派生）为下界 clamp `previous_attempt`——已消耗
  attempt N 的候选派生 ≥ N+1，绝不重铸已消耗身份。钉值路径不动。
- [x] 3.2 组合判别测试：`previous_attempt` 不再传字面量，改走生产派生
  `_state_retry_attempt(state, stage=_failed_stage(state))`——cancelled own
  行带 `_retry_2` 后缀 + 顶层 `retry_count 0` + 无 failed_stage + marker 5 →
  `new_attempt == 3`（当前 red=1）；hydro 失败同构一形；control（own failed
  行）仍 3。
- [x] 3.3 consequence-real 化（TE r2 note）：own-ACTIVE 正向护栏的 ACTIVE 行
  `updated_at` 调早于 cohort 行，使 `manual_retry_requested=True` 且
  `retry_policy.attempt=5` 端到端成立（own-ACTIVE 四参数决策不再落
  skip/active_duplicate；hydro-ACTIVE `running` 参数按设计仍落
  `("skip","active_duplicate_pipeline")`——in-flight hydro run 是真 active
  duplicate，测试以 `decision_pins=False` 编码，见 PR 偏离记录 8）。
- [x] 3.4 红证明：3.2 两形在 clamp 前 red（值=1），clamp 后 green；mutant
  （去掉 clamp）red。
- [x] 3.5 数字全量刷新（cand-D，终推同车）：tasks.md E1 与 PR body 的
  测试数/case 数/红证明数按最终 head 重新实测（record-accuracy 两轮重复的
  跨切闭合：终推前全量 re-derive 所有数字与规范句，不逐处补丁）。

## 4. Round-3 fix set（三轮硬门 depth retro 的 corrective action；
verifier 全 CONFIRMED：F-R3-1/2/3/4，见 .workplans/pr-1293/review/）

- [x] 4.1 gate 判别测试（F-R3-3，coverage 类不可延后）：round-2 gate
  （无 canonical failed stage 才 clamp）在生产组合下零判红——唯一 kill 是
  literal prev=0 伪影（生产组合对同 state 派生 1，HEAD 与 mutant 同值 2）。
  新增两形，`previous_attempt` 走 `_production_previous_attempt`：
  跨 stage 计费方向（failed_stage=forecast + own 非 forecast `_retry_4` 行 →
  1；gate 删除 → 5）与 C2 形（own forecast `_retry_2` cancelled + own convert
  failed → `_failed_stage` 解析 canonical convert → 1；gate 删除 → 3）。
  红证明：gate-deletion mutant 下两形 red（5/3），恢复后 green。
- [x] 4.2 literal 审计（depth retro invariant closure，界于本 PR 新增测试）：
  枚举本 PR 新增、以 literal `previous_attempt` 调 `_manual_retry_new_attempt`
  且可达回落路径的测试；逐个手推生产组合值，一致记 harmless，偏离且非
  issue-AC 判别值的转生产组合（如 cancelled-placeholder 护栏 literal 0 vs
  组合 1），AC 判别对（prev=0→1 / prev=2→3）保留 literal 并记录。
- [x] 4.3 规格/fixture 文本修正（F-R3-1/2/4，orchestrator openspec-only）：
  delta no-replay 句按 verifier 四轴改写（gate 触发=无 canonical stage、
  floor=可见候选域行 max(recorded,suffix)、identity-consumption 轴排除不适用、
  投影可见性限定）+ scenario bullet 改"fallback floor"并注明发出的
  `previous_attempt` 字段不 clamp；design 加 D5（clamp 决策 + gate 理由 +
  stage-blind max 残留）；proposal What Changes 补 clamp 条目；tasks 3.3
  括号注限定到 own-ACTIVE 参数。

## 5. Round-4 fix set（第二次 gate 的 depth retro corrective action；
F-R4-A CONFIRMED P1 + F-R4-B record 全 CONFIRMED，见 .workplans/pr-1293/review/verify-r4-*.md）

- [x] 5.1 轴统一重设计（生产，仍仅本文件）：gate 不变；gate-open 分支弃
  stage-blind max，改 restarted-stage-family floor——family = 活失败行 stage
  集合（行级判据与 `_state_has_candidate_scope_failed_job` 共享 helper，
  排除作用于成员资格）+ hydro 腿并入 canonical forecast stage；floor 复用
  `_state_retry_attempt(state, stage=s)`（两分支同轴）。空 family 不 clamp。
  假 mirror docstring（F-R4-B(2)）随重写移除，新 docstring 逐句带 file:line。
- [x] 5.2 红先行测试：六参数跨 stage 形（8→1、8→3 关键判别形、8→1 hydro、
  5→1 stale、5→1/7→1 单 basin 盖章）+ repaired 行不贡献 stage 护栏（4→2）；
  关键判别形与 hydro 形补端到端断言（`_candidate_state_decision` →
  retry_policy.attempt）。mutant：stage-blind 回退 7 red、floor 全删 4 red、
  gate 删除 Test B red（Test A 的 gate kill 因轴统一消失，已记录）。
- [x] 5.3 文本按实测值重写（orchestrator，代码落地后）：design D5 全段
  （family 轴 + 实测表 + 非 canonical stage 残留如实记录 + follow-up issue）、
  proposal What-Changes 条目、delta no-replay 句（family 轴 + 恢复分支限定）
  与 scenario bullet（跨 stage 子句）；PR body 偏离 6/9 更正、"12 测试函数"
  限定、"warm_start"→"warm_start_chaining"。结构性 prose bar 生效：新增
  normative 断言必须逐条 file:line 对齐，不可写"实践不可达"式免责句。

## E. Evidence floor

- [x] E1 `uv run pytest -q tests/test_production_scheduler.py
  tests/test_file_orchestration_migration.py`（后者承载本谓词的既有护栏集：
  arm 1 同 stage :576、placeholder :821、repaired :856、succeeded 目标 :881、
  arm 2 正向 :910、`fcst_...` 行 :981；CI 定向选择对 `scheduler_state_manual_retry.py`
  不会自动带上它——`scripts/select_ci_tests.py` 的 orchestrator 规则不含该文件，
  故本地 E1 必须显式跑）。结果（final head 实测）1148 passed / 1 failed——唯一失败
  `test_db_free_slurm_storage_root_check_masks_symlink_loop_path` 为 master
  基线既有（macOS 平台相关，PR #1286 期间已在独立 master worktree 复现，
  与本 diff 零共享代码）。
- [x] E2 `uv run ruff check .`
- [x] E3 `openspec validate cycle-pin-live-failure-domain --strict
  --no-interactive`
- [x] E4 Surface check：生产 diff 仅
  `services/orchestrator/scheduler_state_manual_retry.py`；spec 措辞仅经本
  change delta。
- [ ] E5 CI `Unit Tests` green on PR head。
