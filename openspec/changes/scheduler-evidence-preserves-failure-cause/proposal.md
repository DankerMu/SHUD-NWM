# Proposal

## Why

一个 unit 的整条链被 stage span 内的未治理异常静默吞掉时，`scheduler_execution.py:730-782`
的 `except Exception` 会为该 unit 的每个已提交候选写一条执行证据行，标 `submission_failed`
并把崩溃现场写进该行的 `error_code` 与 `error_traceback_tail`（:752-754）。该模块**不 import logging、
也没有 print** —— 这两个字段是崩溃现场的**唯一**记录，从不落 stderr。

这些字段落在顶层的 **`model_run_evidence`**（`scheduler_runtime.py:1376`），**不在候选行上**：
`candidates` 来自 `candidate.to_dict()`，字段集写死在 `scheduler_types.py:97-135`，不含任何 error 键；
`submission_failed` 也是 pass 级状态，事故 pass 里候选行的 `status` 仍是 `selected`。

**缺口只在 bounded fallback**：`scheduler_evidence_payload.py:1279-1349` 的 `bounded_evidence_payload`
从头构造字典，**压根不带 `model_run_evidence` 这一键**，整块蒸发。非阻塞摘要档不受影响
（它把该字段 verbatim 留下，live spec `runtime-evidence-and-operations/spec.md:146` 正是绑这一档）。

node-22 现场：扫描 279 份 pass evidence，「某个 stage span 以
`basin_count=0 && submitted_count=0` 收尾」出现 **4 次**（2026-09-21T04:02 76/76、
04:19 15/76、09-22T18:14 48/96、09-22T21:13 47/96，最后一次即 #2570 事故，IFS 链停摆 4 趟）。
四趟产物全部 `status=resource_limit_blocked`（即走 fallback），`grep error_traceback_tail` **零命中**。
同一个崩溃模式约每天一次，四次全部无法归因 —— #2570 A 组因此只能加固、不能对因修复。

补充事实（已核实，避免重复走弯路）：非阻塞摘要档本身**已在 master**
（`_non_blocking_summary_payload:369`，`:210` 于 fallback 前调用，#1905 由 `69508125b` / PR #2561 交付）。
node-22 当前 HEAD `40efecce` **不含**该提交（`git merge-base --is-ancestor 69508125 40efecce` 为假），
所以事故 pass 走的是旧两档路径。**部署滞后是另一回事**，由运维 ff 解决，不在本单。
即使部署到位，本单这个缺口依然存在：只要 pass 掉进 fallback，失败原因仍整块蒸发。

## What Changes

- 在 `bounded_evidence_payload` 里新增一份**有界的失败原因投影**：只取
  `model_run_evidence` 中 `error_code` 非空的行，每行保候选身份 + `status` + `error_code`
  + `error_traceback_tail`，行数设上限（沿用 `_BOUNDED_SOURCE_CYCLE_PROJECTION_LIMIT = 64`
  的先例：超出部分在 `limit` 里报总数与保留数，不静默截断）。
- 把该键排进 `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS`（`scheduler_evidence.py:81-88`）的**末位** ——
  它是诊断地板，应当最后才被牺牲；终极 limit-compaction 档仍可连同它一起丢（fail-closed 不变）。
- 字段沿用生产者侧既有上界（`scheduler_execution.py:153` 的
  `ERROR_TRACEBACK_TAIL_MAX_CHARS = 2000`），摘要层不再放宽；同行的 `error_message` 有意排除（与 tail 的摘要段冗余）；
  值按现有「源值缺失或为 null 时键不出现」的规则透传，保持摘要行幂等
  （`summary(summary) == summary`）。
- 同步 MODIFY live spec 里枚举摘要行字段的两条场景，使枚举与实现不再互相打架。

## Out of Scope

- **不改 `no_progress_circuit` 的剥离次序**。`openspec/specs/production-scheduler-orchestration/spec.md:175-192`
  明文要求它「在每一层最先剥」，是 #1118 的刻意设计（让 observe-only 标记永不改变 pass 终态）。
  circuit 的可见性缺口由 #2570 C 组的告警规则解决 —— stderr 本来就打印了
  `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`，缺的是告警出口，不是产物字段。
- **不摘要化非阻塞摘要档的 `model_run_evidence` / `slurm_cancellation_evidence`**：live spec
  `runtime-evidence-and-operations/spec.md:146` 钉死该档 verbatim 保留，本单不碰那一档；
  新投影只加在 bounded fallback 里（fallback 今天根本没有这个键）。
- 不改 `MAX_EVIDENCE_BYTES`、不改 readiness 对 `resource_limit_blocked` 的解读。
- #2570 A 组（`chain_stage_execution.py:1229/1241/1263` 异常隔离）与 C 组（`monitoring.py` 告警）：另单另 PR。
- node-22 ff 到含 `69508125` 的 master：运维动作，追平后执行。

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent（自定 expanded：产物是 readiness / operator-action / CLI 共读的持久化格式，且改的是 fail-closed 降级路径的字段枚举）
Blast radius: 写错则 fallback 的字段集与 live spec 漂移，或新投影撑爆本就超限的产物、把 fallback 推进 droppable/终极 compaction 档，反而丢更多
Selected risk packs: Schema/字段名；Resource limits/大输入；Error handling/部分输出；Legacy compatibility
Evidence floor: fallback 保住失败原因的 red + 行数上限与溢出计数 + 降级次序末位 + within-limit byte-identical + fail-closed 不回归 + 既有 decidability/operator-action 套件全绿
```
