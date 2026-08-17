# Tasks: downstream-resume-code-scope

## 1. 实现

- [x] 1.1 `services/orchestrator/scheduler_state_failure.py`（design D2）：
      新 `_downstream_recorded_error_code(state)`——顶层三键
      （`error_code`/`reason_code`/`failure_reason`，排除
      `last_error`/`previous_error`）→ hydro_run 三键**加状态取景**
      （status ∈ journal 清码集 {pending,created,succeeded,complete,
      parsed,published} ⇒ 跳过；缺失 ⇒ 采信，P2-3）→ 失败 stage 的失败态
      job 自身两键（逆序首个 canonical-stage 相等（两侧过
      `_canonical_downstream_stage`，job 侧 `stage or job_type`，N2）∧
      status ∈ FAILED_PIPELINE_STATUSES ∧ 非 repaired-evidence；**命中
      首个匹配 job 即停止，该行无码返回 None，不回落更旧尝试**，N3）；
      events 不参与（D2 第 4 点已闭合裁决）；不 raise。
- [x] 1.2 消费点收窄（design D3）：`:339` 换用新 helper（`:343` 传参形式
      不变）；上方 READER-SYNTHESIZED 注释补 #1420 取景句；
      `_failure_policy_payload`/`_state_error_code` 及其余消费点 diff 级
      零改动；`scheduler_state_decision.py:338` 注释核对（如含「任意处」
      口径则一行同步，否则记录不改）。
- [x] 1.3 双 spec delta（design D4，fixture 已写好，不需编辑；archive 时
      回写）：`multibasin-state-idempotency` 取景定义 + 陈旧码场景；
      `job-retry-mechanism` scoping 段收窄（其余逐字照抄）。

## 2. 测试（先红后绿；红证锚定 2.2/2.3 主形）

- [x] 2.1 helper 判定表（design D5 逐格，含新增格）：顶层/hydro_run
      （status=failed）/失败 job 命中三形；**hydro_run 成功态残码 ⇒
      None**；**hydro_run status 缺失 ⇒ 采信**；已恢复 stage 残留码 ⇒
      None；**其它 stage 失败行带码 ⇒ None**；**event-derived 生产形
      （failed_task 投影顶层）⇒ 非 None**；auto-retry event
      `previous_error` ⇒ None；顶层 `previous_error`/`last_error` ⇒
      None；同 stage 旧失败尝试有码 + 最新失败无码 ⇒ None（命中即停）；
      repaired-evidence 行跳过；stage 别名 canonical 归一命中；
      `failed_stage` 顶层键/job 推断两形。
- [x] 2.2 几何 A 主锚（红-绿）：publish 无码失败 + durable SHUD +
      `convert succeeded, error_code=CONVERT_CANONICAL_FAILED` 历史 job
      ⇒ `run_once()` 后 `retry_downstream` + `submitted_count=1`
      （接线前红：blocked + submitted_count=0，红证记录）。
- [x] 2.3 几何 B 主锚（红-绿）：同底 + 带候选身份字段的 auto-retry event
      `details={"trigger":"auto","previous_error":"SLURM_JOB_FAILED"}`
      （retry.py:448 真实形状）；**event `entity_id` 必须绑定当前失败
      job 行**（绑不存在的 job 会落 skip/active_duplicate_pipeline，
      红证假失败——N1）⇒ 同 2.2（接线前红同上）。
- [x] 2.4 对照组：同底无任何历史码 ⇒ `retry_downstream`（如已有等价既有
      测试则指认引用，不重写）。
- [x] 2.5 #1313 anchors 零改动全绿：`uv run pytest -q
      tests/test_production_scheduler.py -k "downstream or permanent or
      resume or code_recorded"` 全绿，且 refuses/keeps/exhausted/
      repaired_raw_manifest 各用例断言 diff 级不动。
- [x] 2.6 文本面与占位域边界（I6/I7）：几何 A 的 evidence/reason_code 文本
      仍含 broad-scan 码（`_failure_policy_payload` 不回退）；陈旧码
      classifier 属占位域黑名单形（如已恢复 stage 残留 OUT_OF_MEMORY）⇒
      占位域仍拒——钉一格并注明属 #1313 既有语义。

## 3. 验证（Evidence Floor，per issue Verification）

- [x] 3.1 `uv run pytest -q tests/test_production_scheduler.py -k
      "downstream or permanent or resume or code_recorded"` 通过。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate downstream-resume-code-scope --strict
      --no-interactive` 通过。
- [x] 3.4 events 裁决复核（结论已闭合于 D2 第 4 点，fixture review P2-6
      实证：event-only 码经 chain_repository_state.py:846-848 投影顶层 +
      chain_source_cycle.py:685,693 兜底）：复核该结论在实现 HEAD 仍成立
      并记 PR body 一行；不重开裁决。
