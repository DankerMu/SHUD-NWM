# Tasks

## 1. 实现

- [x] 1.1 在 `services/orchestrator/scheduler_evidence_payload.py` 的
      `bounded_evidence_payload:1279-1349` 新增顶层键（建议 `model_run_failures`）：
      取 `model_run_evidence` 中 `error_code` 非空的行，每行按 `_present_bounded_summary_keys`
      规则保候选身份（`candidate_id`/`model_id`/**`basin_version_id`**/`source_id`/`cycle_time_utc`/`run_id`
      —— 行里没有 `basin_id`，写它是死键，见 `scheduler_candidate_execution_evidence.py:293-325`）
      + `status` + `error_code` + `error_traceback_tail`。
      **不要**改候选摘要行的字段元组：`candidates` 行没有 error 键
      （`scheduler_types.py:97-135`），加进去是永不触发的白名单项。
- [x] 1.1b 封顶前按「是否带 `error_traceback_tail`」分区，**tail 行优先填槽**，
      剩余槽位给无 tail 行；分区内部保持原始顺序；`failed_total` 口径不变。
      理由：只有 dispatch 兜底带 tail 且它排在 blocked 行之后，不分区会被整类挤掉（见 design）。
- [x] 1.2 行数上限：新增常量（照 `scheduler_evidence.py:110` 的
      `_BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT = 64` 先例），溢出在 `limit` 块里报
      失败总行数与保留行数，形状照 `_mark_bounded_source_cycles:293/349`。
- [x] 1.3 值直接透传，不额外截断也不放宽（上界由生产者
      `scheduler_execution.py:153` 的 `ERROR_TRACEBACK_TAIL_MAX_CHARS = 2000` 保证）。
      同行的 `error_message`（:752）**有意排除**：与 tail 结尾的 `Type: message` 摘要冗余。
- [x] 1.4 源 payload 无 `model_run_evidence`、或无任何 `error_code` 非空行时，
      **不得捏造空键**（照既有「lane 缺席就缺席」的纪律）。
- [x] 1.4b 二次降级：源本身就是 fallback 产物（无 `model_run_evidence`）时，
      透传并再投影其自身的 `model_run_failures`；marker 的 `dropped` 不可降级、
      `failed_total` 取 `max(本次, 源 marker)`。理由见 design。
- [x] 1.5 把新键**追加到** `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS`
      （`services/orchestrator/scheduler_evidence.py:81-88`）**末位**，
      不得改动既有字段的相对次序。
- [x] 1.6 不碰 `no_progress_circuit` 剥离次序；不碰非阻塞摘要档对
      `model_run_evidence` / `slurm_cancellation_evidence` 的 verbatim 保留。

## 2. 测试

### 2.1 须先在 origin/master 上红（source-only red proof）

- [x] 2.1.1 pass 掉进 bounded fallback 时，产物带失败原因投影，每行含
      `error_code` 与 `error_traceback_tail`。复现用真实 `ProductionScheduler.run_once()`
      + 抛异常的 orchestrator（该探针已验证可复现事故形状：候选行 `status` 为 `selected`、
      `model_run_evidence` 行带 `PRODUCTION_ORCHESTRATION_FAILED`）。
- [x] 2.1.2 失败行数超上限时，`limit` 报总数与保留数，保留行数等于上限。
      断言须**先用字面量行数**证伪行为（`failed_total > retained == len(投影)`），
      再用常量断言精确上限——否则 master 上红在 import 常量而非行为。
- [x] 2.1.2b 异质行：`[64+ 条带 error_code 无 tail 的 blocked 行] + [1 条带 tail 的崩溃行]`，
      断言投影里至少一行带 `error_traceback_tail`（同质行的溢出用例观察不到该失效）。
- [x] 2.1.3 降级次序：构造必须清空字段才放得下的 payload，断言先清
      `model_discovery` / `source_cycles` / 候选列表 / `restart_reconcile`，新键仍在。

### 2.2 须保持绿

- [x] 2.2.1 非阻塞摘要档仍 verbatim 保留 `model_run_evidence`：
      `tests/test_production_scheduler.py:21741 / :21760 / :21781 / :23589`。
- [x] 2.2.2 circuit 剥离 `fitted == baseline`：`tests/test_production_scheduler.py:54630-54660`。
- [x] 2.2.3 within-limit byte-identical；硬上限 `SchedulerEvidenceWriteError` fail-closed。
- [x] 2.2.4 restart_reconcile 双 lane 计数属性、`limit.candidate_lists` 单调性、
      `source_cycles` breaker-released 投影与溢出计数。
- [x] 2.2.5 `tests/test_scheduler_evidence_decidability.py`、`tests/test_operator_action_listing.py` 全绿。
- [x] 2.2.6 源无失败行时产物不含新键（无 red 要求：master 上本就不含）。
- [x] 2.2.6b 二次降级：对已是 fallback 产物的 payload 再跑一次，投影与 marker 均不丢失。
- [x] 2.2.7 droppable 边界：构造「带新投影后仍能停在 `summarized`」的 payload，
      断言 `limit.candidate_lists == "summarized"`、候选行数未被清空、新键在位 ——
      新投影不得把今天停在 summarized 的 pass 推到 `dropped`，那是净损失。

### 2.3 exact-shape 断言的处置

- [x] 2.3.1 新顶层键会打到 bounded fallback 的 exact-shape 断言（`tests/` 里若干处）。
      **逐条在报告里说明**改动是「接纳新键」还是「削弱断言」；后者不允许。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_scheduler_evidence_decidability.py tests/test_operator_action_listing.py`
- [x] 3.1b `uv run pytest -q tests/test_production_readiness_validation.py tests/test_operator_action_status_closure.py`
      （两者引用 `bounded_evidence_payload`，须实测而非静态推断）
- [x] 3.2 `uv run pytest -q tests/test_production_scheduler.py -k "bounded or evidence_size or non_blocking_summary or model_run"`
- [x] 3.3 `uv run ruff check services/orchestrator/scheduler_evidence_payload.py services/orchestrator/scheduler_evidence.py tests/test_production_scheduler.py tests/test_scheduler_evidence_decidability.py`
- [x] 3.4 `openspec validate scheduler-evidence-preserves-failure-cause --strict --no-interactive`
- [x] 3.5 `git diff --check`

## 风险包（selected / not selected）

- **Schema / 字段名**：selected —— fallback 新增顶层键是对外契约，live spec 须同步；由 2.1.1 + 2.3.1 + spec delta 覆盖。
- **Resource limits / 大输入**：selected —— 新投影占预算（最坏 64×2000 ≈ 128 KB，<3%），且不得把 pass 推到更狠的降级档；由 1.2 + 2.1.2 + 2.2.7 覆盖。
- **Error handling / 部分输出**：selected —— 本单保的就是失败路径的诊断字段；由 2.1.1 覆盖。
- **Legacy compatibility**：selected —— within-limit、摘要档、fallback 既有字段与降级次序不得漂移；由 2.2 全组 + 1.5 覆盖。
- **Concurrency / shared state / ordering**：not selected —— 降级**字段次序**属本单改动面（新键追加末位，由 2.1.3 守），但无并发/共享状态语义；写盘路径单线程。
- **Public API / CLI**：not selected —— 新键对 CLI 是纯增量，无接口形状变更。
- **File IO / 路径安全**：not selected；**Auth / 权限 / 机密**：not selected —— tail 已由生产者经 `evidence_safe` 脱敏（`scheduler_execution.py:738`），投影层不得绕过。
- **Config / 项目设置**、**Release / 依赖**、**文档 / 迁移说明**：not selected。

## 非目标

- `no_progress_circuit` 剥离次序（`production-scheduler-orchestration/spec.md:175-192` 的 #1118 契约）。
- 非阻塞摘要档的 `model_run_evidence` 摘要化（live spec `:146` verbatim）。
- #2570 A 组（poll 落盘写异常隔离）与 C 组（monitoring 告警）。
- node-22 ff 到含 `69508125` 的 master（运维动作）。
