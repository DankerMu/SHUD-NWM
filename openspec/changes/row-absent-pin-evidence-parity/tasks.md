# Tasks: row-absent-pin-evidence-parity

## 1. 实现

- [ ] 1.1 `file_orchestration_journal.record_manual_repair`：details 追加
      **8 键**（design D2 表）：`target_status` / `target_repair_status` /
      `target_active_blocker` / `target_model_id` / `target_slurm_job_id` /
      `target_retry_count` / `target_manual_retry_marker` /
      `target_array_task_id`（取写入时 `failed_job`；None/"" 不写键，
      0/False 必须写键）；注释记录键名裁定（避开 stage/job_type/model_id
      三个消费键）与 D2 闭包不变式（键集对
      `_job_row_is_live_failure` 传递闭包读取字段闭合）。
- [ ] 1.2 `scheduler_state_identity_filter` retry-event details 白名单
      （:487-505 carve-out）追加同名 8 键；锚定 0/False 透传
      （`not in (None, "")` 语义）。
- [ ] 1.3 `_unresolvable_marker_entity_pins_attempt` 重构（design D3）：
      cycle guard 后——记录完整（完备性 gate：`target_status` ∧
      `failed_stage` 均在场，P2-3）→ pseudo-row 重建 → model-bearing
      （且 `target_model_id == 候选自身 model`，fail-closed）无条件 pin /
      model-less 走 `_cycle_scope_marker_pins_attempt(state, pseudo_row)`
      + 两条 staleness 映射保留；记录缺席或半记录 → 现行臂逐位保留
      （token backstop 不退化）。docstring 重写：新 delivered domain
      （同构主张 + model-bearing 短路例外）、D4 永久限定枚举（含
      reactivation 形处置）、pseudo-row 与 `_job_is_cycle_scope_row` 的
      语义映射声明、B token 上限。
- [ ] 1.4 twin 与路由器**逻辑/行为 diff 为零**的证明义务（实现报告）；
      twin `_cycle_scope_marker_pins_attempt` docstring 中「row-absent
      arm … reading no row status at all … tracked by #1308」段落
      **必须随之改写**指向新 delivered domain（P2-2——散文更新不算
      逻辑 diff，报告中单列）。
- [ ] 1.5 D4 第 5 类（写入后重新激活）可达性核实：追 typed API
      （`update_pipeline_job_status` :3424-3428 终态守卫不含
      `submission_failed`）的 master-row 路径，判定
      `submission_failed → pending/queued` 是否生产可达；可达 → 并入
      D4 永久限定条款 + 矩阵记录（spec 措辞已预留），不可达 → 矩阵记
      不可达理由。

## 2. 测试（先红后绿；写入面口径构造，禁手搓与生产写入面不一致的 details）

- [ ] 2.1 矩阵收敛主锚：`test_same_stage_marker_target_staleness_residue_matrix`
      两分歧格构造改为携带写入面口径 `target_*` 键（**防漂移双断言**：
      键集与 `record_manual_repair` 写入面一致 + 键集对
      `_job_is_unsubmitted_auto_retry_placeholder`/共享谓词的读取字段
      闭合，D2 闭包不变式），期望 `(False, True)` → `(False, False)`
      （修前红：gate 不读新键仍 `(False, True)`）。
- [ ] 2.2 A-4 队列 stage **防御性合同锚**（fixture review P2-1 定性）：
      download / state_save_qc / publish 各一形「记录形状良构、
      `target_status ∈ 成功态`」→ row-absent refuse 与 row-present twin
      一致（不依赖 completed_stage_evidence——断言该映射对这三个 stage
      为 None/缺席，`chain_repository_state.py:251-256`）。**如实标注**：
      该取值在生产写入面域外（`_manual_retry_source_for_run` 只选
      `MANUAL_RETRY_SOURCE_STATUSES` 行），构造为 §2 纪律的显式例外——
      锚的对象是 gate 对记录的合同而非写入面可产形；写入后才 succeeded
      的真实人群归 2.5 披露锚 + D4 条款。
- [ ] 2.3 A-5 后缀几何：`_retry_1` 与 `..._retry_1_retry_2_retry_3` 两种
      entity_id 在 A-1/A-2 + 记录携带形下同样收敛。
- [ ] 2.4 C 成对用例：model-bearing `job_cycle_*` 目标 cross-stage
      （F5′ 形）与 same-stage（staleness 映射点名形）各一对——
      row-present 路由器 pin 与 row-absent 记录腿 pin 一致（修前
      row-absent 为 False/refuse，红-绿）；外加 fail-closed 负例：
      `target_model_id` 非候选自身 model 的记录**不** pin（design D3
      Note 合取）；外加 **truncation 防线格**：`target_model_id` =
      候选自身 model 且决策态**无任何 model-scoped job 行**（行窗截断
      形）→ 仍 pin——锁死「model-ness 不得从幸存行派生」（防止推导源
      被换回 `_candidate_model_ids` 类 job-row 派生后测试仍全绿）。
- [ ] 2.5 D4 永久限定成对披露锚：winner-eviction 形（写入后成功被更晚
      stage 挤出）与「写入后才 succeeded 的队列 stage」形各一对——
      row-present refuse / row-absent pin **钉现状**，docstring 指向
      spec 限定条款（无 red-proof 要求，钉值锚）。
- [ ] 2.6 legacy backstop 回归：无 `target_*` 键的 marker 在既有矩阵
      全部格 + #1292 交付锚下判决逐位不变。
- [ ] 2.7 B 合同：stage-less legacy marker 显式判决用例 + `token != row
      实际 stage` 上限用例（钉既定行为，docstring 记录「文本推断非记录
      证据」）+ **半记录形归属锚**（fixture review P2-7）：
      `target_status` 在场、`failed_stage` 缺席的半记录，判决与同形
      legacy marker 逐位一致（token backstop 不退化）。
- [ ] 2.8 写读全链路锚（integration）：真实 `record_manual_repair` →
      真实投影 + identity filter → 决策态：新键存活、判决按 D3；同锚
      反向验证既有白名单键行为不变。
- [ ] 2.9 D6 compaction 锚：完成阶段 model-less 队列 marker 事件投影后
      无 `details` 且不被 pin gate 采信（`_manual_retry_marker_shape`
      不成立）；提交阶段对照组保留 details 且采信。

## 3. 验证（Evidence Floor）

- [ ] 3.1 `uv run pytest -q tests/test_production_scheduler.py` 通过。
- [ ] 3.2 `uv run pytest -q tests/test_file_orchestration_journal.py` 通过。
- [ ] 3.3 `uv run ruff check .` 通过。
- [ ] 3.4 spec-compliance 人工证据：delta 的 delivered-domain 改写句、
      D4 永久限定条款、D6 括注修正与最终实现逐句对读；
      `openspec validate row-absent-pin-evidence-parity --strict
      --no-interactive` 通过。
