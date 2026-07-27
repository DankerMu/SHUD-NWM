# Tasks: scheduler-bounded-evidence-observability

Issue: #1168

## 1. Implementation

- [x] 1.1 `scheduler_evidence_payload.py::bounded_evidence_payload`:入参 payload 携带 `status` 时,`limit` 块写入 `pre_limit_status = <入参 status>`(缺失则省略该键,不写 null);顶层 `status` 保持 `"resource_limit_blocked"`,`limit.reason` 保持传入 reason。
- [x] 1.2 新增 candidate 摘要 helper(单一职责,固定 key 集,present-and-non-None 规则):`candidate_id`、`source`、`source_id`、`cycle_time`、`cycle_time_utc`、`scenario_id`、`run_id`、`forcing_version_id`、`basin_id`、`model_id`、`status`、`reason` + `state_evidence` 展平三键:`decision`(← `state_evidence.decision`;行级 `decision` 无生产者,必须从 state_evidence 取)、`missing_forcing_repair_status`(← `state_evidence.missing_forcing_repair.status`)、`quarantined_skip_reason`(← `state_evidence.journal_predecessor_identity.quarantined_skip_reason`)。mapping 条目零固定键 → `{}`(保基数);非 mapping 条目 → `{"summary_error": "unrecognized_candidate_shape"}`。
- [x] 1.3 摘要双落点收敛同一 helper:(a) `bounded_evidence_payload` 构造时对 `candidates`/`blocked_candidates`/`skipped_candidates` 应用摘要(替代无条件 `[]`);(b) `_fit_bounded_evidence_payload` 在既有 `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS` 清空档之前新增摘要档,对尚未摘要化的三张列表应用同一 helper(幂等:已是摘要行的输入再过一遍输出不变)。`source_cycles`/`model_discovery` 维持现状(仍清空)。
- [x] 1.4 D2b `restart_reconcile` 紧凑保留:fallback 构造保留键集新增 `restart_reconcile`(入参存在时,present-only 同 `restart_reconcile_proof` 处理):紧凑形态 = 块级 `status`、`reserved_unbound_error`、`inflight_error`(两段各自的失败键,`scheduler_runtime.py:1542,1572`,任一段可能单独失败)+ `reserved_unbound.outcomes[]` 每行固定摘要(`job_id`、`action`、`status`、`reconciliation_reason_class`、`quarantine_reason`、`quarantine_field`,present-and-non-None);outcome 行键集严格取自唯一生产者 `scheduler_runtime.py:1515-1538`,行级 `reason` 无生产者不得收录;`_fit` 中仍超限时随 droppable 档清空(列于三张 candidate 列表之后)。**两处形状陷阱**(fixture 复核 P3-b):(a) `_fit` 清空档硬编码 `{} if field_name == "model_discovery" else []`(`scheduler_evidence_payload.py:102`)——`restart_reconcile` 是 mapping,追加进 droppable 时必须同步扩该条件为 `{}`,不得写成 `[]`;(b) `_drop_empty_optional_bounded_fields`(`:260-273`)需把 `restart_reconcile` 列入,否则清空后的空壳残留。
- [x] 1.5 `limit.candidate_lists`:fallback 构造时置 `"summarized"`;droppable 档按字段顺序渐进清空、一放得下即停,仅当被清空的 candidate 列表清空前非空时翻 `"dropped"`(清空本就为空的列表不丢任何行,标记保持 `"summarized"`);不超限路径不写该键;`_compact_limit` 终极档(仅存 `reason`)行为不变——该档下两个新键随 limit 块压缩消失(既有 fail-closed,豁免)。
- [x] 1.6 `scheduler_runtime.py:1391-1397` 注释同步新契约(三处状态一致仍成立 + `limit.pre_limit_status` 保真语义)。
- [x] 1.7 `SchedulerResourceLimitError` 分支(`scheduler_runtime.py:1413-1439`)零改动(diff 不触碰)。

## 2. Tests(requirement-driven,`tests/test_production_scheduler.py`)

- [x] 2.0 既有断言迁移清单(fixture 评审 P1-2;**预期仅此三处**——新增键抬高构造体积可能使紧预算测试(`:9480` 2_000、`:9694` 2_200、`:9805` 1_500、retention 2_800)的降级档路径偏移,若因此第四处必须调整,须附字节预算迁移理由且保持零削弱,禁止改预算数字凑绿;逐条附迁移理由,新断言必须严格强于或等价旧断言语义):
  - `:9491-9494` `limit` 全等断言 → 扩为含 `pre_limit_status`(源 payload `:19977` 带 `"status": "submitted"`)与 `candidate_lists` 的新全等形态。
  - `:9850` `limit` 全等断言 → 同上(源 `:9827` `"status": "submitted"`)。
  - `:9464` `candidates == []`(输入 `[{"secret_token": "rawsecret"}]`)→ `== [{}]`(保基数;并保留/强化 secret 不泄漏断言:序列化产物不含 `rawsecret`)。
- [x] 2.1 不超限路径字节等价:payload 在限内 → 产物无 `pre_limit_status`、无 `candidate_lists`,candidate 明细完整原样。
- [x] 2.2 状态保真:超限 fallback 产物 `status == "resource_limit_blocked"` 且 `limit.pre_limit_status == <入参真实状态>`(覆盖 `submission_failed` 与 `planned` 两值);`limit.reason == "evidence_size_limit_exceeded"`;入参无 `status` → 无 `pre_limit_status` 键。
- [x] 2.3 candidate 摘要档命中:构造超限 payload(三张列表齐备,行含 identity 键 + `state_evidence.missing_forcing_repair.status` + `state_evidence.journal_predecessor_identity.quarantined_skip_reason` + 应被剔除的冗长非固定键)→ 三张列表非空、每行 key ⊆ 固定集、identity 键(`source_id`/`cycle_time_utc`/`scenario_id`,candidates 行另有 `run_id`/`forcing_version_id`)与事故字段在场、冗长键消失;`limit.candidate_lists == "summarized"`;体积 ≤ max_evidence_bytes。
- [x] 2.3b D2b 命中:超限 payload 携带 `restart_reconcile`(含 `reserved_unbound_error` 与生产者可产 outcome 行:`journal_quarantined` 带 quarantine 键、`query_unavailable` 带 `reconciliation_reason_class`、`bound` 仅剩 identity)→ fallback 产物保留紧凑 `restart_reconcile`,`job_id`/`status`/`reserved_unbound_error`/`reconciliation_reason_class`/`quarantine_reason`/`quarantine_field` 可读;另一场景:只有 inflight 段失败(块内仅 `status` + `inflight_error`,无 `reserved_unbound_error`)→ `inflight_error` 仍可读;无该块的 payload 不写该键。
- [x] 2.4 退化清空:max_evidence_bytes 压到摘要也放不下(但高于 `_compact_limit` 终极档阈值)→ droppable 档按字段顺序渐进清空、一放得下即停(故部分清空时靠后的列表仍是摘要行、`restart_reconcile` 排在三张列表之后可独立存活)、清空到清空前非空的 candidate 列表时 `limit.candidate_lists == "dropped"`(三张列表本就全空时不翻,保持 `"summarized"`)、体积仍 ≤ 上限;彻底不可满足时仍抛 `SchedulerEvidenceWriteError("evidence_size_limit_exceeded")`。
- [x] 2.5 `_fit` 注入路径(fixture 评审 P2-1):经 context 自定义 `bounded_evidence_payload` 返回未摘要列表 → `_fit` 摘要档先于清空档生效(列表为摘要行而非 `[]`)。
- [x] 2.6 下游契约不回归:`production_contract` 对 `resource_limit_blocked` 的映射与 `readiness_scheduler_evidence` 的 verdict(恒 blocked)不变——除 2.0 清单外不修改任何既有断言。
- [x] 2.6b readiness identity reader 新基线(fixture 评审 P2-2):bounded 摘要行(含 identity 键)喂给 readiness scheduler evidence reader → 不再产生 `candidates_missing_source_id`/`_missing_scenario_id`/`_missing_run_id` 类噪音,verdict 仍 blocked;显式断言 acceptance_errors 新基线。
- [x] 2.7 端到端落盘(fixture 评审 P2-3):经 `write_evidence` 真实落盘超限 payload(扩展 `:9694` 起的既有 seam 测试)→ 磁盘产物与回灌内存 evidence 均为摘要行 + `pre_limit_status`,非空列表不塌缩。
- [x] 2.8 红前证据:2.2/2.3/2.3b/2.4 在实现前必须能红,失败输出逐字留档到 `.workplans/pr-<N>/red-before.log`。

## 3. Verification(合并门)

- [x] 3.1 `uv run pytest -q tests/test_production_scheduler.py`(全绿)
- [x] 3.2 `uv run pytest -q tests/test_production_readiness_validation.py`(2.6b 涉及面)
- [x] 3.3 `uv run ruff check .`
- [x] 3.4 `openspec validate scheduler-bounded-evidence-observability --strict --no-interactive`

## 4. Post-merge ops(非合并门)

- [ ] 4.1 node-22 拉取后跑一次满编 `--plan` pass,确认超限产物 candidate 摘要行非空、`limit.pre_limit_status` 在场、`restart_reconcile` 紧凑块可读(issue Verification 第 3 条 live receipt)。
