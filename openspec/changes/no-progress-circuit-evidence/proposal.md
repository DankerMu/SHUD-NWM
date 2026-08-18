# Proposal: no-progress-circuit-evidence (#1118)

## Why

Issue #1118：同一主体连续多个 pass 重复同一 no-progress 理由时，调度器零升级
信号。两起实证事故：(a) IFS `ifs_2026071200` 连续六天每 pass 重复 warm-state
mismatch，`WARM_START_TIME_MISMATCH` 不在 `TRANSIENT_ERROR_CODES`
（services/orchestrator/retry.py:28）→ 兜底 `unknown_failure` → permanent 分类
永不进重试计数（retry.attempt 恒 0），六天六次「首次即永久失败」无任何计数器
观察其连续性；#1431 的 systemic escalate 是 **cycle 内**检测，跨 cycle 每次重走
（design D1.3 明文）；(b) GFS `identity_mismatch_blocked` 逐 pass 重复——卡死
本体已被 #1173 收敛（streak≥3 → `identity_mismatch_released` 终态），剩纯可
观测性缺口。另有本批新增的 `comment_accounting_unproven`（#1116）：生产集群上
reserved 行**设计性永久**逐 pass 重复，正是本 marker 的直接受益者。

上游边界已由两单明文划定：#1152 Non-Goals「#1118 落地时应消费本单新增的
`operator_action_required` 字段而非另起判据」；#1173 design D3「跨 reason 聚合、
`no_progress_circuit_open` 告警升级仍归 #1118」。

## What Changes

**observe-only 跨 pass 熔断标记**（不改任何调度行为），新模块
`services/orchestrator/scheduler_no_progress.py`：

- **持久化跟踪器**（部署是 systemd oneshot——docs/runbooks/
  current-production-ops.md:134，每 pass 新进程，内存计数活不过一个 tick）：
  状态文件 `<evidence_root>/no-progress-tracker.json`，每 pass 读→更新→原子写
  （仿既有 evidence 写盘纪律）。文件缺失/损坏 → 从空起步并在 block 里如实标注
  `state_reset`（observability-only，fail-open 不 fail-closed）。
- **观察源 = 当 pass evidence payload 的三个既有字段面**（消费不发明，全部
  在 bounded 压缩幸存键内）：
  - A1 candidate summaries（`_BOUNDED_CANDIDATE_SUMMARY_KEYS`
    scheduler_evidence_payload.py:10-25）：subject=("candidate", candidate_id)，
    reason=f"{status}:{reason}"；**仅当 status 属非推进类且 reason 非空**才产生
    观察（推进/成功行零观察——implementer 依真实 status 词表甄别并测试钉死）。
  - A2 candidate state evidence `operator_action_required=true`
    （scheduler_evidence_payload.py:26-35，#1152 字段）：subject 同上，
    reason=`operator_action_required:{decision}`。
  - A3 `restart_reconcile.reserved_unbound.outcomes[]`
    （scheduler_evidence_payload.py:41-51）：subject=("job", job_id)，
    reason=f"{action}:{reconciliation_reason_class}"（reason_class 为 None 时仅
    action）；覆盖 #1116 wedge 与 #1173 streak 尾迹。
- **严格连续语义**（镜像 #1173）：同 (subject, reason) 连续出现 count+1；同
  subject 换 reason → 重置为 1；本 pass 未观察到的 subject → 条目清除（推进或
  离场即断连续）。状态文件规模因此天然有界（≤ 每 pass 观察数）。
- **开闸**：count ≥ 阈值 → 该条目标记 open；pass evidence 顶层新增
  `no_progress_circuit` block：`{"threshold": N, "tracked": n, "state_reset":
  bool, "open": [{subject_kind, subject_id, reason, consecutive_passes,
  first_pass_id, last_pass_id}]}`，open 列表按 count 降序**截断至 50 条**并带
  truncated 计数（block 自身有界）。存在 open 条目时每 pass 记一条**聚合**
  WARNING（token `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`，列 subject/reason/计数，
  截断同上）——journalctl 是当前唯一被运维实际消费的通道。
- **配置**：`ProductionSchedulerConfig` 新增 `no_progress_circuit_passes`
  （env `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`，默认 3，与 #1173 阈值口径
  一致；≤0 禁用——不读不写状态文件、不注入 block、行为与今日逐字相同），照
  scheduler_config.py:221 既有 `_env_int` 惯例与范围校验。
- **集成点单一**：`run_once` 组装 payload 之后、`write_evidence`
  （scheduler_evidence.py:367）之前一步 observe+注入；bounded 压缩保留
  `no_progress_circuit` 顶层 block（体量固定小，压缩层新增顶层键保留）。
- **retention 兼容**：核查 `scripts/node22_scheduler_evidence_retention.py` 的
  删除 glob 不会吞状态文件；若会，加入其白名单（tracker 文件不是 evidence，
  不受 168h 年龄裁剪）。
- `docs/runbooks/current-production-ops.md` 补一小节：如何从 evidence block /
  WARNING 定位 open circuit，与 #1152/#1173/#1116 各自 runbook 的交叉指引。

## Non-Goals

- **不改任何调度行为**：不停重试、不熔断执行、不新增终态/decision/reason-class
  token（`ACCEPTED_RECONCILIATION_DECISIONS` / `ACCEPTED_RECONCILIATION_REASON_CLASSES`
  封闭集零改动）。circuit 是证据标记，不是执行闸。
- 不新建 ops API/告警通道（当前 evidence 零消费端是既成事实——本单出口=evidence
  block + journalctl WARNING，消费端建设另立）。
- 不做 (a) 类错误码的重试分类修正（`WARM_START_TIME_MISMATCH` 的 permanent
  归类是 #1431 的裁决，不动）。
- 不做跨 subject 聚合分析（同 reason 多 subject 各自独立计数）。
- 事故 (b) 本体（#1173 已收敛）与 #1431 cycle 内阶梯零改动。

## Risk triage

- Fixture level: expanded（新模块 + 持久化 + 配置 + evidence 契约 + 脚本兼容 +
  runbook；但 observe-only、单集成点）。
- Repair intensity: medium。
- Risk packs: state-semantics selected（跨 pass 持久状态的连续/重置/清除语义 +
  oneshot 进程模型忠实性——测试必须以独立 scheduler 实例逐 pass 驱动共享
  evidence_root，不许共享内存对象）；test-evidence selected（每个适配器一条
  红证场景 + 健康 pass 零观察锁 + disabled 逐字等旧锁）；payload-contract
  selected（bounded 压缩保留 block、5MB 预算下 block 有界、状态文件原子写与
  损坏恢复）；env-divergence not selected（无宿主环境轴——文件系统语义已由
  evidence 写盘先例覆盖），其余 not selected。

## Must preserve

- 禁用（≤0）时行为与今日逐字相同：payload 无新键、evidence_root 无 tracker
  文件、零新日志。
- 默认启用（3）下**健康 pass 零观察零 block 条目**（成功/推进候选不产生
  observation；无 open 时不发 WARNING；block 仍注入但 open 为空——运维可据此
  区分「功能在跑」与「功能关闭」）。
- 既有 evidence 键与 bounded 压缩行为逐字不变（只新增顶层键）；5MB 预算不因
  block 突破（block 有界 ≤50 条）。
- 调度决策路径零改动（tracker 观察的是已组装 payload，不反馈进决策）。
- `_SchedulerProgressGuard`（scheduler_runtime.py:31-70，单 pass 内阶段熔断）
  语义与命名空间不受影响（两者作用域不同：intra-pass vs cross-pass）。
- 状态文件损坏/缺失不使 pass 失败（fail-open + `state_reset` 标注）。
- retention 脚本既有删除行为对 evidence 文件逐字不变。

## Seams under test

- `scheduler_no_progress` 纯函数层（observe/merge/prune/open 判定）直接驱动；
  状态文件 round-trip 层（tmp_path evidence_root）；`run_once` 集成层（既有
  test_production_scheduler 桩式 scheduler 构造，逐 pass 新实例共享
  evidence_root——oneshot 忠实）；retention 脚本的文件筛选谓词（若可 import）。
- 配置注入缝：`no_progress_circuit_passes` 经 env 与直接构造两路。

## Evidence mapping

- 验收 1（同 (subject,reason) 连续 N pass → block open + WARNING）→ tasks 2.2
  红证（A1 路径）。
- 验收 2（换 reason 重置 / 离场清除 / 健康 pass 零观察）→ tasks 2.3。
- 验收 3（A2/A3 适配器各自开闸）→ tasks 2.4。
- 验收 4（disabled 逐字等旧 + 默认启用空 block 形状）→ tasks 2.5。
- 验收 5（持久化：oneshot 多实例、损坏恢复、原子写）→ tasks 2.6。
- 验收 6（bounded 压缩保留 + 截断有界）→ tasks 2.7。
- 验收 7（retention 不吞 tracker）→ tasks 2.8。
- Verification：`uv run pytest -q tests/test_production_scheduler.py` + ruff +
  openspec validate（本地）；merge 后 node-27 receipt；node-22 实机观察一个
  真实 pass 的 block 形状（post-merge 义务，记 #1118）。
