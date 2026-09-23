# Design

Change surface: `services/orchestrator/scheduler_evidence_payload.py`
（`bounded_evidence_payload:1279-1349` 新增失败原因投影 + 其投影 helper），
`services/orchestrator/scheduler_evidence.py`（`_DROPPABLE_BOUNDED_EVIDENCE_FIELDS:81-88`
的次序、新投影的行数上限常量），`tests/test_production_scheduler.py`（`tests/test_scheduler_evidence_decidability.py` 与
`tests/test_production_readiness_validation.py` / `tests/test_operator_action_status_closure.py`
只作 must-remain-green oracle，不改动）。

Must preserve:
- **#1118 不变量**：`no_progress_circuit` 仍是「每一层最先剥」，本单不碰
  `_serialized_evidence_within_limit:165-173` 与 `_fit_bounded_evidence_payload:262-275` 的次序。
- **非阻塞摘要档不变**：`model_run_evidence` 在该档 verbatim 保留（live spec
  `runtime-evidence-and-operations/spec.md:146`），本单只改 fallback。
- bounded fallback 的既有契约：顶层 `resource_limit_blocked`、`limit.reason`、
  `limit.pre_limit_status`、`limit.candidate_lists` 的 `summarized`/`dropped` 单调性、
  restart_reconcile 双 lane 计数属性、`source_cycles` 的 breaker-released 投影与其溢出计数、
  终极 limit-compaction 档、硬上限 `SchedulerEvidenceWriteError` fail-closed。
- within-limit 路径 byte-identical。
- 既有降级次序中每个字段的相对位置不变（新键只追加到末位，不得把已有字段往后挪，
  否则 `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS` 的既有断言会漂）。

Must add/change:
- `bounded_evidence_payload` 新增一个顶层键（建议 `model_run_failures`），内容是
  `model_run_evidence` 中 **`error_code` 非空**行的有界投影：每行保候选身份
  （`candidate_id` / `model_id` / `basin_version_id` / `source_id` / `cycle_time_utc` / `run_id` ——
  注意是 `basin_version_id`：`_candidate_identity_evidence`
  （`scheduler_candidate_execution_evidence.py:293-325`）写的是它，行里没有 `basin_id`，
  按既有 `_present_bounded_summary_keys` 规则，源值缺失或 null 时键不出现）
  + `status` + `error_code` + `error_traceback_tail`。
- 行数上限沿用 `_BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT = 64` 的先例（新增独立常量），
  **溢出必须可见**：在 `limit` 块里报失败总行数与保留行数，照 `limit.source_cycles` 的形状做。
- **封顶前按「是否带 `error_traceback_tail`」分区，tail 行优先填槽**。写 `error_code` 进同一个
  `evidence` 列表的有**六处**，只有 dispatch 兜底（`scheduler_execution.py:742-754`）带 tail，
  且它排在全部 blocked 行（`:592` output_uri / `:640` slurm preflight / `:658` secret manifest /
  `:664` resource profile / `:697` raw staging）**之后**；不分区则一趟先攒够 64 条无 tail 的
  blocked 行就能把唯一带崩溃现场的行整类挤掉，产物退化成与本单要修的失效等价。
  `failed_total` 的计数口径不变（全部 `error_code` 非空的行），分区内部保持原始顺序。
- 源 payload 没有 `model_run_evidence`、或没有任何 `error_code` 非空的行时，
  **不得**捏造空键——照既有「lane 缺席就缺席、不给伪造空列表」的纪律。
- **二次降级（re-entry）**：`write_evidence`（`scheduler_evidence.py:465-468`）会把调用方的
  evidence dict 原地替换成 fallback 产物，所以「源本身就是 fallback 产物」是可达路径。
  此时源已没有 `model_run_evidence`，其自身的投影就是失败原因的唯一残留，必须**透传再投影**
  而不是丢弃；投影行本就是该固定键子集，再投影是 no-op。`limit` marker 同理：
  `dropped` 不可降级回 `summarized`，`failed_total` 取 `max(本次, 源 marker)`。
  `bounded_evidence_payload:1275-1278` 对 `source_cycles` 已有同款既有范式。
- 新键排进 `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS` **末位**：它是诊断地板，
  比 `model_discovery` / `source_cycles` / 候选列表 / `restart_reconcile` 都更晚被牺牲。

Governing invariant: **一次 pass 失败的原因（`error_code` / `error_traceback_tail`），
在体积降级中的牺牲次序必须排在所有 verbose 明细之后 —— 因为它是崩溃现场的唯一记录：
生产者只写进证据产物，从不写 stdout/stderr。**

预算口径：`error_traceback_tail` 由生产者截断为
`ERROR_TRACEBACK_TAIL_MAX_CHARS = 2000` 字符（`scheduler_execution.py:153`，
摘要段再受 `_ERROR_TRACEBACK_TAIL_MESSAGE_CHARS = 500` 约束），是有界值；
投影层**直接透传、不再额外截断也不放宽**。最坏增量 = min(失败行数, 64) × 2000 字符
≈ **128 KB**（含 JSON 转义同量级），相对 `MAX_EVIDENCE_BYTES = 5_000_000` **不到 3%**，
且只在存在失败行时发生。

需要守的回归形状：新键**不得**把 fallback 推进更狠的档。
`_fit_bounded_evidence_payload` 的 droppable 档（`scheduler_evidence_payload.py:277-306`）
会按次序清空字段，终极 limit-compaction 档还会把 `limit` 块压成 reason-only。
若新投影让一个今天停在「summarized」的 payload 掉到「dropped」，那是净损失。
由 tasks 2.2.7 的边界用例守住。

Sibling surfaces:
- `_fit_bounded_evidence_payload:262-306` 与 `:342-349`（两处按 `_DROPPABLE_...` 迭代）；
- `_mark_bounded_source_cycles:293/349` 是「溢出计数写进 limit」的既有范式，新投影照抄形状；
- 既有 oracle：`tests/test_scheduler_evidence_decidability.py`、`tests/test_operator_action_listing.py`、
  `tests/test_production_scheduler.py` 里 bounded fallback 的 exact-shape 断言
  （**新增顶层键会打到这些 exact-shape 断言，必须逐一确认是「接纳新键」而不是「削弱断言」**）；
- 下游读者：`services/production_closure/readiness_scheduler_evidence.py:79,128`、
  `services/orchestrator/operator_action_listing.py:20-26`、`production_contract.py`
  —— 须确认没有「未知顶层键即拒」的校验。

Seams under test: `scheduler_evidence.write_evidence` 的公开边界 + 落盘字节；
`bounded_evidence_payload` 作为函数的直接单测。无需 live Slurm / DB。

Required evidence:
- **red（source-only，master 上必须因缺键而红）**：pass 掉进 bounded fallback 时，
  产物带失败原因投影，每行含 `error_code` 与 `error_traceback_tail`；
  复现形状用真实 `ProductionScheduler.run_once()` + 抛异常的 orchestrator
  （implementer 已验证该探针可复现：候选行 `status` 为 `selected`、
  `model_run_evidence` 行带 `PRODUCTION_ORCHESTRATION_FAILED`）。
- **red**：失败行数超过上限时，`limit` 报总数与保留数，且保留行数等于上限。
- **red**：降级次序里新键在末位 —— 构造一个必须清空某些字段才能放下的 payload，
  断言先清 `model_discovery`/`source_cycles`/候选列表/`restart_reconcile`，新键仍在。
- must-remain-green：within-limit byte-identical；硬上限 fail-closed；
  `limit.candidate_lists` 单调性；restart_reconcile lane 属性；`source_cycles` 投影与溢出计数；
  非阻塞摘要档仍 verbatim 保留 `model_run_evidence`（`tests/test_production_scheduler.py:21741/:21760/:21781/:23589`）；
  circuit 剥离 `fitted == baseline`（`:54630-54660`）。

Non-goals: circuit 次序；摘要档的 `model_run_evidence` 摘要化；`MAX_EVIDENCE_BYTES`；#2570 A/C。

Review focus:
1. 新投影只在 fallback 生效，摘要档与 within-limit 路径零改动。
2. 行数上限与溢出计数是否照 `source_cycles` 的既有范式，溢出不得静默。
3. 新键是否在降级次序**末位**，且既有字段的相对次序未被挪动。
4. 被新顶层键打到的 exact-shape 断言，改动是「接纳新键」还是「削弱断言」——逐条说明。
5. 源无失败行时不得捏造空键。
