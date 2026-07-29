# Design: scheduler-completion-verdict-absence-tolerance

## Risk triage

- **Fixture level: expanded**(生产调度正确性 + 水文血统保护;错放宽 = 血统污染,错收紧 = 断产)。
- 风险轴:①判定放宽的边界必须精确——只容"缺",不容"错";②cohort 行新增字段不得破坏 accepted-submit 不变量闸(8 处 `__post_init__`/normalize 门,见 #1180);③verdict 侧比对语义改造的字段集合选择影响既有 pass 行为(candidate 侧比对不换向,见 Seams C1 修订)。

## Must-preserve(不得回退的既有行为)

1. init-state 记录**存在且冲突** → verdict 仍 `gap`、candidate 仍 mismatch(strict 保护不回退)。
2. successor state 缺失或 `usable_flag=False` → 仍 `gap`(物理连续性证明是容缺的先决条件)。
3. 旧 per-basin 行(带 `init_state_id`)的既有匹配行为不变(0718/0719 等 cycle 的 complete 判定不受影响)。
4. #1173 的 L1/L2 行为(identity-blocked release、strict-warm-start 预算降级)不变。
5. accepted-submit 三个封闭状态集与 `ORDINARY_UPSERT_FIELDS` 白名单语义:新增字段走白名单显式扩项,不得绕过不变量闸。
6. 零 migration:旧 journal 行原样可读,新字段可选(absent-tolerant reader);新字段不改变任何历史行的 `forecast_cohort_digest` 校验结果(F3)。
7. `successor_evidence is None` 是第三态("未表态",非 db_free / 无 next allowed cycle / 非 strict 窗口等路径返回 None,`scheduler_discovery.py:187-198`、`scheduler_core.py:773-803`):**None ≠ 连续性证明,容缺分支一律不生效,verdict 保持 gap**(F4)。
8. verdict 之后的 #1107 §8.7 journal predecessor identity 门(`scheduler_discovery.py:147-149/279-325`)行为不变;"072000 complete → 下一 pass 放行 072012"以该门对 072000 返回非 stale 为前提,回归锁覆盖(F5)。将来若把身份写进 hydro_run 会首次激活该门对 cohort 的路径——本次明确不写 hydro_run。

## Seams under test(上游声明,实现消费)

- `_cycle_completion_verdict`(`scheduler_discovery.py:180-223`)的 4 条件 AND——注入点:`_terminal_decision_matches_strict_warm_start` 的语义改造。
- 统一 helper 的落点(cross-review C1 修订):共享小模块,**verdict 侧(discovery)消费三态 helper**;candidate 侧 wrapper 的 `hydro_run` 腿**保留 `_warm_state_record_matches` 的 selected-驱动严格比对,行为逐字节不变**——helper 的 observed-驱动语义会把 legacy id-only 行从 mismatch 翻成 match,将 #1173 有预算的 mismatch 决策路径改道到无预算的 `strict_warm_start_terminal_run_manifest_missing` 路径(违反 must-preserve #4)。candidate 侧可复用共享模块的字段别名读取器,但比对方向不换。
- accepted-submit 写侧(修订,fixture review F2/F3):**记录点在预约期,不在终态**——终态写发生在 reconcile 侧 `project_forecast_cohort_tasks`(纯 Slurm accounting,无 planning 上下文,`reconcile.py:1041` → `file_orchestration_journal.py:2598/2806-2824`),且 `ORDINARY_UPSERT_FIELDS` 是冻结表("终态首次写入+白名单扩项"自相矛盾)。正确数据流(iteration-2 修订,N1):预约期 `chain_forecast_orchestrator_cycle.py:509-527` 的 `context.active_basins` 已带 `init_state_*`(`scheduler_candidate_manifest.py:282-308` + `apply_cohort_warm_start`),身份落账为 **master 行新字段——按 `array_task_id`/`model_id` 键控的逐 model 身份映射**(N7:init 身份逐 basin/model 各不相同,标量会让 17/18 个 model 判 conflict;结构与 `cohort_members` 同构但在 digest 输入之外——digest 输入是 `accepted_submit_identity.py:738-750` 的固定 dict,新字段天然在外);**不经 member 传播**——`ordered_cohort_members()` 投影恰好 `_MEMBER_FIELDS`(`accepted_submit_identity.py:701-708`),终态构行唯一读径 `file_orchestration_journal.py:2668` 会剥掉非成员字段。终态逐 model 行构造时从作用域内的 `existing`(master 行,`file_orchestration_journal.py:2806-2824`)按本行 `array_task_id` 取映射中的对应项。机械触点(N5):`_pipeline_job_row` 封闭 43 键构造器(`:5563-5633`)必须显式加字段,否则预约写静默丢值、后续 ordinary upsert 冻结校验抛错;`_CYCLE_SCOPE_JOB_PROJECTION_KEYS`(`:8312-8333`)决定该字段对 candidate_state evidence 的可见性,按消费需要显式取舍并写测试锁定。
- **digest 禁区**(F3):身份字段**不得进入** `_MEMBER_FIELDS` / `forecast_cohort_digest` 的输入集——否则 digest 重算与全部历史行不符,`forecast_cohort_identity_is_valid` 为 False,normalize 抛错导致在飞 cohort 行不可读。身份存放于 digest 计算之外的显式白名单新字段。

## 判定真值表(核心设计)

| 终态 | init 记录 | successor ready+usable | verdict |
|---|---|---|---|
| succeeded | 匹配 | 任意 | complete(现状) |
| succeeded | **缺失** | 是 | **complete(本次新增)** |
| succeeded | 缺失 | 否 | gap(现状) |
| succeeded | 冲突 | 任意 | gap(现状,不放宽) |
| failed/其他 | 任意 | 任意 | gap(现状) |

统一 helper 契约(修订,fixture review F1/F6):**逐在场字段比对**——`absent` = 终态行无任何 init-state 身份字段;有 `init_state_id` 且所有**在场**字段(checksum/uri/valid_time 缺谁跳谁,同现有 redaction-skip 语义)一致 → `match`;任一在场字段矛盾 → `conflict`。**禁止**"部分字段即 conflict":legacy hydro_run 行结构上只有 `init_state_id`(`file_orchestration_journal.py:1203/1234`),升格 4 字段必判会把全部历史 cycle 打成永久 gap(比现病更重)。特例分支归属(iteration-2 修订,N2):共享 helper 只做**纯字段比对**;candidate 侧两条既有特例分支(`terminal_source=pipeline_job` 的 `candidate_state` 分支、`COLD_START_QUARANTINED` 逃生门,`scheduler_candidates.py:1849-1867`)保留在 candidate 侧 wrapper 中、先于 helper 短路返回 match——**不上提进 verdict 路径**(discovery 今天对这两种形状判 gap,上提会让无连续性证明的形状直接 complete)。verdict 侧消费裸 helper。边界钉死(N3):strict 侧 resolution 无 `candidate_state` 时(`COLD_NEW_MODEL`/`COLD_DECLARED_CUTOVER` ready 但无 state,`scheduler_generation_gate.py:349-376`),verdict 路径**不进 helper、按现状短路判 gap**(`scheduler_discovery.py:334-335` 行为不变)。字段形状补全(N6):有 checksum/uri/valid_time 但无 `init_state_id` → `conflict`(生产不可达,契约显式化)。统一范围仅限 verdict 侧的 init 身份比对(cross-review C1 修订):candidate 侧 wrapper 的最终字段比对**不换向**——`hydro_run` 腿维持 `_warm_state_record_matches`(selected-驱动:selected 在场而 observed 缺失的字段判 mismatch),连同三段准入梯子(`scheduler_candidates.py:418-439`)其余两段一并不动;wrapper 级测试锁定"selected 带 checksum + observed 仅 id → not-match(预算路径)"。

## Evidence mapping

- 单测:三形态真值表(tests/test_production_scheduler.py 或 test_warm_start_chaining.py);helper 字段集合锁定;cohort 行新字段写入 + 旧行 absent-tolerant 读取;不变量闸负测(新字段非法值被拒)。
- 实机(tasks 4.x):node-22 部署后 ≥1 自然 pass:072000 `complete`、072012 候选非 blocked 并提交;链推进 receipt 回贴 issue #1183。

## 接受的残余风险(具名,F8)

`successor_state.ready` 校验 index key(含生产 cycle_id)、lead_hours、usable_flag、对象 checksum、包版本+checksum(`state_manager.py:1172-1257/2407-2453`),是强证明;但它证明不了产出该状态的 run 用的是规范 warm init——cold-seed 准入的 run 会以同一 cycle_id 入册。容缺后这类 lineage 断裂会被判 complete(今天判 gap)。**接受该交换**:代价是罕见 cold-seed 场景下血统审计信号后移,收益是消灭"缺账即永久断链"的结构性卡死;冲突场景的保护零回退。

## Non-goals

- 不提供 skip-cycle/mark-complete 运维工具(若实现中发现必要,报 deviation 由上游决策)。
- 不修 manual-retry 空承诺(另行 issue)、不动 #1179 truncation seam、不改 lookback/backfill 选择语义、不做任何 journal/index 手术。

## Risk packs

- Selected: state-machine-invariants(判定表穷举)、persistence-compat(零 migration 读写)、production-parity(node-22 live receipt)。
- Not selected: security/perf(无新攻击面;判定为纯内存比较,无热路径变化)——理由:改动不触 IO 形态与权限面。
