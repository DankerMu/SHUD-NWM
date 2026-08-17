# Design: downstream-resume-code-scope

坐标系：master（#1419/PR #1508 merge 后）。关键既有事实：
`_state_error_code`（scheduler_state_failure.py:211-238）全扫描面：顶层 →
`hydro_run` → 全部 jobs 逆序（跳 `_pipeline_job_is_repaired_stage_evidence`，
scheduler_state_rows.py:643-644：`repair_status=="repaired"` 或
`active_blocker is False`）→ 全部 events 的
`details.error_code/last_error/previous_error`。消费点两类：
(a) `_failure_policy_payload`（:180-209）取 reason_code 文本；
(b) **:339** `recorded_error_code = _state_error_code(state)` → **:343**
`_downstream_failure_restartable(failure, code_recorded=recorded is not None)`
（:1315-1330：False ⇒ 占位域 classifier 黑名单放行；True ⇒
`not failure["permanent"]`）；(c) :1757/:1762 失败信号检测。
`_failed_stage`（:63-75）：顶层键优先，否则逆序首个失败态 job 的 stage。
`previous_error` 真实写入面（event details）：retry.py:448（auto，
`last_error` 在 :485，manual 在 :582）、
file_orchestration_journal.py:6917（auto）/:7061,:7190（manual）；
`last_error` 同为 event details（journal:6963/:6995）。两键**无任何
顶层/hydro_run 写入者**（fixture review 实证）。issue 已实测：几何 A/B 在
HEAD 误判 blocked、pre-#1417 检出（4904c2c6）与对照组均 retry_downstream。

## D1 裁决：`code_recorded` = 当前失败自身记录的码

「记录域 vs 占位域」的分域回答的是「**这次失败**有没有真实记录的码」——
占位域存在的理由就是当前失败 job 无码。历史已恢复 stage 的码与
`previous_error`（定义即「上一次尝试的错」）描述的是**别的失败**，把它们
当本次失败的证据是取景错误，不是语义选择。全扫描面保留给 reason_code
文本（`_failure_policy_payload` 不动）——文本面「找个最相关的码来展示」
与分域面「本次失败是否有码」是两个问题，本 change 只收窄后者。

## D2 新 helper：`_downstream_recorded_error_code(state)`

```python
def _downstream_recorded_error_code(state: Mapping[str, Any]) -> str | None:
    ...  # 返回当前失败自身记录的码，或 None
```

取景（按序首个非空命中）：

1. **顶层**：`error_code` / `reason_code` / `failure_reason`。`error_code`
   是候选/cycle 行的当前失败载体；`reason_code`/`failure_reason` 今日无
   生产写入者，作防御性保留（与 identity filter 的顶层失败载体键组
   scheduler_state_identity_filter.py:579-593 同构对齐，fixture review
   N5）。**排除 `last_error`/`previous_error`**：两键的全部真实写入面都
   是重试历史 event details（坐标见头部），且无顶层写入者。
2. **`hydro_run`**：同三键，**但加状态取景（fixture review P2-3）**——仅当
   `hydro_run.status` **∉** `{pending, created, succeeded, complete,
   parsed, published}`（即 journal 后端会清码的状态集，
   file_orchestration_journal.py:1507-1513）时采信。理由：SQL 后端
   `update_hydro_run_status`（chain_repository.py:487-497）只在
   `value is not None` 时赋值，**成功转换不清旧码**——成功态 run 行上的
   残码是陈旧载体，采信即 issue 原病在 hydro_run 腿复活（fixture review
   已实测该形）。status 缺失/None 时采信（无从判断，保守偏向 #1313 gate
   的安全方向）。
3. **失败 stage 的 job 自身**：逆序首个满足
   canonical-stage 相等（两侧均过 `_canonical_downstream_stage`，job 侧
   `job.get("stage") or job.get("job_type")`——生产 stage 可为别名，
   fixture review N2）且 `status ∈ FAILED_PIPELINE_STATUSES` 且非
   `_pipeline_job_is_repaired_stage_evidence` 的 job，取其
   `error_code` / `reason_code`；**命中首个匹配 job 后即停止：该行无码即
   返回 None，不回落更旧的同 stage 尝试**（fixture review N3）。status
   过滤使已恢复（succeeded）stage 行与**其它 stage 的失败行**（N4）
   天然出局——不再依赖 repair-marker 齐全（issue 指出的「另一半」由取景
   本身消解）。
4. **events 不参与分域——已闭合的裁决（fixture review P2-6 实证）**：
   event-only 的失败码在生产里不会只留在 event——
   `candidate_state_from_rows` 把 `_candidate_failed_task_from_events`
   的结果投影到顶层 `error_code`（chain_repository_state.py:846-848），
   且该路径对 error_code 有 `or "NODE_FAILURE"` 兜底
   （chain_source_cycle.py:685,693）：failed_task 成立则顶层码必非空。
   「当前失败码只存在于 event、job 与顶层皆无」在现有写入面上无真实形；
   若未来出现，按 Non-Goals 另立裁决。

`_failed_stage(state)` 为 None 时（消费点 :330 已提前 return，实际不可
达）helper 返回顶层/hydro_run 命中或 None——不 raise。**顶层陈旧码
残口（Note 级，fixture review (b)）**：`exposed_latest_job.error_code`
第三级兜底理论上可把 succeeded 行的码投影到顶层，但该路径要求
pipeline_status 为 terminal success，会先被
`scheduler_state_decision.py:253` 的 terminal_pipeline_success 分支 skip，
够不到 resume 通道——不算活口，记录不处理。

## D3 消费点收窄：只动 :339 一处

- `:339` 改为 `recorded_error_code = _downstream_recorded_error_code(state)`；
  `:343` 传参形式不变。**注意 :339 上方 #1313 D4 的
  READER-SYNTHESIZED 注释需随语义更新**（补一句「recorded 取景 = 当前
  失败自身，陈旧历史码不算证据——#1420 裁决」）。
- `_failure_policy_payload`（:180-209）**零改动**：几何 A 的分类文本仍会
  基于 broad-scan 码（如 CONVERT_CANONICAL_FAILED）——这正是期望行为：
  占位域的 restartable 判据走 **classifier 黑名单**
  （`_DOWNSTREAM_PLACEHOLDER_REFUSAL_CLASSIFIERS` =
  malformed_input/policy_blocked/resource_configuration），
  CONVERT_CANONICAL_FAILED/SLURM_JOB_FAILED 均落 unknown_failure ⇒ 放行，
  与 pre-#1417 master 行为一致（issue 实测锚）。**推论要在测试里显式钉**：
  陈旧码若属黑名单 classifier（如陈旧 OUT_OF_MEMORY →
  resource_configuration），占位域仍会拒——这是既有占位域语义（#1313
  原样），不是本 change 引入；测试记录该边界（D5）。
- 其余 `_state_error_code` 消费点（:1757/:1762、`_failure_policy_payload`
  内 :186）diff 级不动。
- `scheduler_state_decision.py:338` 的分域注释：实现时核对措辞，若含
  「state 任意处」口径则同步（一行注释）。

## D4 规格面：双 MODIFIED delta（载体同步）

- **`multibasin-state-idempotency`「Resumable downstream failures」**：
  requirement 正文 "genuinely recorded error code" 后补取景定义从句
  （"recorded by the failing stage itself — its own failed job row, or the
  candidate/run-level failure fields; stale codes left by recovered stages
  or by retry-history keys are not evidence for this judgement"）；新增
  场景「stale historical codes do not flip the resume domain」（几何 A/B
  两形 WHEN + THEN 落占位域，AND 保留 refused-classifier 拒绝语义、其余
  按无陈旧码等价 resume）。**两 spec 的既有占位域场景处置不对称是有意
  的**：multibasin 侧由新场景显式覆盖陈旧码形（且其既有场景本就带
  "outside its refused classifiers" 限定），故既有场景不动；job-retry 侧
  无对应新场景，故收窄其占位域场景 WHEN。
- **`job-retry-mechanism`「Pre-Guard Evidence Channels Consult
  Permanence」**（:1206-1313）：整 requirement 照抄，修改两处
  （fixture review P2-2；round-1 复审据此再校准措辞）——(i) recorded-code
  scoping 段：**捏造条件逐字保留** "(defaults fabricated when the state
  records no error code at all)"，另起一句写域条件 "The clause's
  recorded-code domain is scoped to codes recorded by the failing stage
  itself — stale codes from recovered stages or retry-history keys are not
  evidence for the domain split, even though they still supply the
  reason-code text."；(ii) 占位域场景「Synthesized placeholder codes keep
  existing downstream behavior」的 WHEN 同步收窄为**非合取**形："records no
  error code for the failing stage itself (stale codes elsewhere ... do not
  count, even when such a stale code still supplies the classification's
  reason code), so the classification rests on no code this failure recorded
  — a stage-derived placeholder the reader synthesizes when the state
  carries no code at all"；THEN 逐字不动。除此两处外场景逐字不动。
- **「捏造条件 ≠ 分域条件」（round-1 CONFIRMED P2 的成因）**：本 change 后
  两者是不同集合——分域按当前失败自身取景（新 helper），而
  `{STAGE}_FAILED` 默认是否真被合成仍由 `_failure_policy_payload`
  （`:186` `_state_error_code(state) or default_error_code`）的全扫描决定。
  几何 A 即两者分叉的实证：落占位域，但 `reason_code` 是陈旧的
  `CONVERT_CANONICAL_FAILED`（tests/test_production_scheduler.py:23128
  钉住）。凡描述该 clause 的文本（spec 两载体、`:405` 注释、
  `_downstream_failure_restartable` docstring）都不得把两者写成同一条件。
- **归档顺序约束（fixture review P2-4）**：
  `openspec/changes/fix-node22-scheduler-business-concurrency` 存在同
  requirement「Resumable downstream failures」的**陈旧** MODIFIED delta
  （#1313 之前的老文本）——它若在本 change 之后归档会把 #1313+#1420 两次
  收窄一起冲掉。本 change 归档时无碍；该陈旧 delta 属 pre-existing 仓库
  卫生问题，路由 issue-scribe 单独立项，不在本 change 修。

## D5 测试面

- **helper 单元判定表**（新测试函数，就近 #1313 测试族）：顶层码命中 /
  hydro_run 码命中（status=failed 形）/ **hydro_run 成功态残码 ⇒ None**
  （P2-3 状态取景格）/ **hydro_run status 缺失 + 码在场 ⇒ 采信**（保守
  方向格）/ 失败 stage job 码命中 / 已恢复 stage 残留码 ⇒ None /
  **其它 stage 的失败行带码 ⇒ None**（N4——「repair 标记不齐」另一半的
  消解证明）/ **event-derived 生产形（failed_task 投影顶层码）⇒ 非 None**
  （N4，钉 P2-6 结论）/ auto-retry event `previous_error` ⇒ None /
  顶层 `previous_error`/`last_error` ⇒ None / 同 stage 旧失败尝试有码 +
  最新失败无码 ⇒ None（命中即停）/ repaired-evidence 行跳过 / stage 别名
  经 canonical 归一命中（N2）/ `failed_stage` 顶层键与 job 推断两形。
- **几何 A 主锚（红-绿）**：issue 复现构造（publish 无码失败 + durable
  SHUD + `convert succeeded, error_code=CONVERT_CANONICAL_FAILED` 历史
  job）⇒ `retry_downstream` + `submitted_count=1`（接线前红：blocked +
  submitted_count=0）。
- **几何 B 主锚（红-绿）**：同底 + 带候选身份字段的 auto-retry event
  `details={"trigger":"auto","previous_error":"SLURM_JOB_FAILED"}`
  （retry.py:448 真实形状）；**event 的 `entity_id` 必须绑定当前失败
  job 行**（fixture review N1：绑不存在的 job 会落
  `skip/active_duplicate_pipeline`，红证假失败）⇒ 同上（接线前红同上）。
- **对照组回归**：同底无历史码 ⇒ `retry_downstream`（既有行为，若已有
  等价测试则引用不重写）。
- **#1313 anchors 原样绿**：`test_downstream_resume_refuses_recorded_non_
  transient_codes` / `..._keeps_recorded_transient_codes` /
  `..._exhausted_budget` / `test_repaired_raw_manifest_...`（这些用例的
  码在顶层或失败 job 自身——新取景下 code_recorded 仍 True，断言零改动）。
- **占位域既有拒绝边界记录**：陈旧码 classifier 属黑名单（如已恢复
  stage 残留 OUT_OF_MEMORY → resource_configuration）⇒ 占位域仍拒——
  钉一格，注明为 #1313 既有占位域语义非本 change 引入。

## Invariant Matrix（pin 的行为）

| # | 面 | 不变式 | 锚 |
|---|---|---|---|
| I1 | 分域 | 几何 A：已恢复 stage 残留码不翻分域，resume 放行 | tasks 2.2 |
| I2 | 分域 | 几何 B：`previous_error`（真实 event 形）不翻分域，resume 放行 | tasks 2.3 |
| I3 | 回归 | 对照组（无历史码）行为逐位不变 | tasks 2.4 |
| I4 | gate | 当前失败自身永久/unknown-default 码仍拒 resume（#1313 anchors 零改动） | tasks 2.5 |
| I5 | helper | 判定表逐格（含排除键、status 过滤、最新失败尝试语义） | tasks 2.1 |
| I6 | 文本 | `_failure_policy_payload` reason_code 文本面零回退（broad-scan 保持） | tasks 2.6 |
| I7 | 占位域 | 黑名单 classifier 的既有拒绝语义不变（陈旧码进文本→classifier 场景钉界） | tasks 2.6 |
