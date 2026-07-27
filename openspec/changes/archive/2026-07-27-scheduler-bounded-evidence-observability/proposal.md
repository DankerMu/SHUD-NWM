# Proposal: scheduler-bounded-evidence-observability

Issue: #1168 · Suggested fixture level: expanded · Minimal mergeable slice: 两缺陷一个 PR(同一 fallback 代码路径 + 同一测试面)

## Why

Scheduler pass evidence 的 5MB 超限 fallback 在两处销毁事故期最需要的可观测性,#1160/#1165 事故响应中两次实测拉长 MTTR:

1. **真实 pass 状态被覆盖**:`bounded_evidence_payload`(`services/orchestrator/scheduler_evidence_payload.py:782`)硬编码 `"status": "resource_limit_blocked"`,把计算出的真实状态(如 `submission_failed`、`planned`)在产物/CLI/pass-result 三处同时顶掉。事故期它掩蔽了 `submission_failed`;2026-07-26 node-22 满编 pass(`scheduler_2026072620_0afba8680eba`、`scheduler_2026072623_99b08d07782a`)再次被顶。
2. **事故关键明细在超限时归零**,由两个独立机制共同造成(fixture 评审 P1-1 修正定位):
   - candidate 明细:`bounded_evidence_payload` 构造时(803-808 行)无条件把 `candidates`/`blocked_candidates`/`skipped_candidates` 置 `[]`,`_fit_bounded_evidence_payload`(99-105 行)第一降级档也是整体清空,两档间无 per-candidate 压缩层。
   - reconcile 明细:`evidence["restart_reconcile"]` 块(含 `reserved_unbound_error`、quarantine outcome 行)**不在 fallback 构造的保留键集里**(765-818 行只保留 `restart_reconcile_proof`),超限即整块消失——今天 node-22 实测 `restart_reconcile: {}`。issue AC #2 点名的 `reserved_unbound_error`/`quarantine_reason`/`quarantine_field` 正存于此块(`scheduler_runtime.py:1360,1526-1539`、`reconcile.py:1303-1304`),不在 candidate `state_evidence` 里。

## What Changes

- **D1(状态保真,不破坏 userspace)**:顶层 `status` 保持 `resource_limit_blocked` 不变(下游 `production_contract.py:378` 映射 `blocked`、`readiness_scheduler_evidence.py:76,112` 消费该值,fail-closed 契约保留);真实计算状态记入 `limit.pre_limit_status`,取自入参 payload 的 `status` 字段;入参缺失 `status` 时该键省略(不写 null)。`limit.reason = "evidence_size_limit_exceeded"` 不变。
- **D2(candidate 摘要档)**:单一摘要 helper,固定 key 集(**仅收录入参中存在且非 None 的键**;值原样透传——入参在 `scheduler_evidence.py:367` 已过 `_evidence_safe` 脱敏,摘要层不引入新敏感值):
  `candidate_id`、`source`、`source_id`、`cycle_time`、`cycle_time_utc`、`scenario_id`、`run_id`、`forcing_version_id`、`basin_id`、`model_id`、`status`、`reason`,以及 candidate `state_evidence` 内真实存在的事故子集展平(行级 `decision` 无生产者——`SchedulerCandidate.to_dict` 不产出,勿收):`decision`(← `state_evidence.decision`,`scheduler_state_decision.py:69,81`)、`missing_forcing_repair_status`(← `state_evidence.missing_forcing_repair.status`,`scheduler_candidates.py:721-729`)、`quarantined_skip_reason`(← `state_evidence.journal_predecessor_identity.quarantined_skip_reason`,`scheduler_candidates.py:2054`)。
  `source_id`/`cycle_time_utc`/`scenario_id`/`run_id`/`forcing_version_id` 为 readiness identity reader(`readiness_scheduler_evidence.py:645-682`)的每行必读键(fixture 评审 P2-2)。行语义:**保持逐行基数**——mapping 条目一个固定键都没有时产出 `{}`(基数与脱敏都保住);非 mapping 条目产出 `{"summary_error": "unrecognized_candidate_shape"}`(fail-safe,不抛)。
  摘要逻辑落点(fixture 评审 P2-1 裁定,双落点收敛同一 helper):(a) `bounded_evidence_payload` 构造时对三张列表应用摘要(替代无条件 `[]`);(b) `_fit_bounded_evidence_payload` 在既有 droppable 清空档**之前**新增摘要档(对尚未摘要化的列表行应用同一 helper)——覆盖 context 注入自定义 `bounded_evidence_payload` 后再过 `_fit` 的路径(`scheduler_evidence_payload.py:61-65,740-756`)。摘要后仍超限 → 既有 droppable 档照旧清空。
- **D2b(restart_reconcile 紧凑保留)**:fallback 构造保留键集新增 `restart_reconcile`,以紧凑形态保留(入参存在该块时):块级 `status`、`reserved_unbound_error`、`inflight_error`(reserved-unbound 与 inflight 两段各自记录自己的失败键,`scheduler_runtime.py:1542,1572`,任一段可能是唯一失败者,故两键都必须留),以及 `reserved_unbound.outcomes[]` 每行的固定摘要(`job_id`、`action`、`status`、`reconciliation_reason_class`、`quarantine_reason`、`quarantine_field`,present-and-non-None 规则同 D2)。outcome 行键集严格取自唯一生产者 `scheduler_runtime.py:1515-1538`——行级 `reason` 全仓无生产者,不得收录(同 D2 的死键纪律)。该块在 `_fit` 中的归属:摘要化后作为 retained 紧凑字段;仍超限时随既有 droppable 档一起清空(列于 candidate 列表之后)。无该块的 payload 不写该键(与现有 `restart_reconcile_proof` 的 present-only 处理一致)。
- **D3(可区分的空)**:`limit` 块新增 `candidate_lists` 字段:fallback 构造时置 `"summarized"`;droppable 档按字段顺序**渐进**清空、一放得下即停(故部分清空时靠后的列表仍是摘要行),仅当该档清空的 candidate 列表**清空前非空**时翻为 `"dropped"`——清空一张本就为空的列表什么都没丢,标记保持 `"summarized"`。不超限路径不写该键。**豁免**(fixture 评审 P3-1):`_fit` 最深兜底档会把整个 `limit` 换为 `_compact_limit`(仅存 `reason`,`scheduler_evidence_payload.py:165-168,314-315`),该档下 `pre_limit_status`/`candidate_lists` 随之消失——此为既有 fail-closed 终极档,不改。
- **D4(注释与规格同步)**:`scheduler_runtime.py:1391-1397` 注释更新为新契约措辞;`runtime-evidence-and-operations` 规格补充 bounded 观测底线场景。

## Risk Triage

- Level: **expanded**(上游建议一致)。生产 fallback 路径改动,消费面包括 readiness 链与 production contract,但顶层键集只增不减、状态枚举不变。
- 风险轴:
  - 契约回归:`production_contract`/`readiness_scheduler_evidence` 对 fallback 形状的假设——顶层 `status` 枚举、`limit.reason` 不变;摘要行携带 identity reader 必读键,避免 acceptance_errors 语义漂移(P2-2)。
  - 体积回归:摘要行失控由 droppable 清空档兜底,5MB 硬上界与最终抛错不变。
  - 测试基线迁移:三处既有钉死断言随新契约更新(见 tasks 2.0 清单),除此之外零断言削弱。

## Must-Preserve

- `MAX_EVIDENCE_BYTES = 5_000_000` 硬上界;所有降级档穷尽后仍不可满足时抛 `SchedulerEvidenceWriteError("evidence_size_limit_exceeded")`。
- 顶层 `status` 枚举与 `resource_limit_blocked` fail-closed 语义;pass-result / 落盘 / CLI 三处状态一致(`write_evidence` 将 `payload_to_write` 回灌内存 evidence,`scheduler_evidence.py:381-384`)。
- `limit.reason` 取值;`_REQUIRED_BOUNDED_EVIDENCE_FIELDS` 键集仍全部在场;`_compact_limit` 终极档行为不变。
- `SchedulerResourceLimitError` 分支(`scheduler_runtime.py:1413-1439`)字节不动(issue 已裁定 out of scope)。
- 不超限路径(`_serialized_evidence_within_limit` 第一分支)字节等价:payload 原样、无新增键。
- readiness identity reader 对 fallback 的 verdict 不翻(`resource_limit_blocked` ∈ `SCHEDULER_REVIEW_BLOCKED_STATUSES` 恒 blocked,`readiness_scheduler_evidence.py:76,509-515`);acceptance_errors 内容随摘要行出现的变化以 2.6b 测试显式钉住新基线。
- 既有脱敏纪律(`_evidence_safe`、`[local-path]`/`[redacted]` 占位);摘要行绝不透出非固定键(如 `secret_token` 类)。

## Seams Under Test(上游声明,消费不重谈)

- `bounded_evidence_payload(payload, *, reason, max_evidence_bytes)`(`scheduler_evidence_payload.py:759`)— fallback 构造本体(D1/D2/D2b/D3)。
- `_fit_bounded_evidence_payload(payload, *, max_evidence_bytes)`(同文件 :89)— 新摘要档 → 既有 droppable → 既有 compactors 档序。
- `_serialized_evidence_within_limit`(同文件 :48)— 不超限分支字节等价 + 超限分支走新 fallback。
- `ProductionScheduler._write_evidence`(`scheduler_core.py:907`,调用点 `scheduler_runtime.py:1400`)与 `scheduler_evidence.write_evidence`(:381-384 回灌)— 端到端落盘形态(P2-3)。

## Review Packs

- Selected: **contract**(fallback 形状是跨模块契约,消费者在 production_closure)、**test-integrity**(#1163/#1165 连续两单 recurring class 是 test-oracle 强度;本单含既有断言迁移,必须零削弱)。
- Not selected: security/performance(无新 IO、无新路径输出、每 pass 一次列表映射);migration(evidence 产物是一次性快照,旧产物无新键是合法历史形态)。

## Evidence Mapping(AC → 交付物)

| Issue AC | 交付物 |
|---|---|
| `limit.pre_limit_status` 记录真实状态,顶层 `status`/`limit.reason` 不变,下游无需改动 | D1 + tasks 2.2/2.6 |
| 满编超限 pass 产出非空摘要行,事故关键字段可读 | D2(candidate 侧)+ D2b(reconcile 侧)+ tasks 2.3/2.3b |
| 摘要仍放不下退化清空,5MB fail-closed 不回归 | D2/D3 + tasks 2.4 |
| 单测覆盖(happy/摘要/退化/状态保真) | tasks §2 全部 |
| 注释与规格同步 | D4 + spec delta |
| node-22 真实满编 pass 摘要行非空(issue Verification 第 3 条) | tasks §4.1(合并后运维,非合并门) |

## Non-Goals

- 不调 `MAX_EVIDENCE_BYTES`;不缩减 candidate `state_evidence` 原始体积(另一治理线)。
- 不做 sidecar 明细文件(issue 备选,已裁定代价过大)。
- 不分离 `SchedulerResourceLimitError` 与 evidence-size fallback 的状态字面量语义(需改状态枚举,单开 issue)。
- 不改 evidence 读取方(CLI/readiness reader)代码——新增字段纯增量;readiness reader 对摘要行的 acceptance_errors 新基线由测试钉住,reader 行为改进(如识别 summarized 标记)留给后续。
- 摘要键集不含 `blocked_reason`(全仓无生产者,blocked candidate 走 `reason`,`scheduler_types.py:127`;不引入死键)。
