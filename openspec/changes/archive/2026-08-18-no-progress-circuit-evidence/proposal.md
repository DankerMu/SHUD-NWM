# Proposal: no-progress-circuit-evidence (#1118)

## Why

Issue #1118：同一主体连续多个 pass 重复同一 no-progress 理由时，调度器零升级
信号。两起实证事故：(a) IFS `ifs_2026071200` 连续六天每 pass 重复 warm-state
mismatch，`WARM_START_TIME_MISMATCH` 不在 `TRANSIENT_ERROR_CODES`
（services/orchestrator/retry.py:28）→ 兜底 `unknown_failure` → permanent 分类
永不进重试计数（retry.attempt 恒 0），六天六次「首次即永久失败」无任何计数器
观察其连续性；#1431 的 systemic escalate 是 **cycle 内**检测，跨 cycle 每次重走
（design D1.3 明文）。permanent 归类使候选呈 `status="blocked"` + 稳定 token
`permanent_failure_guard`（scheduler_state_failure.py:369-378），对 A1 适配器
可见。(b) GFS `identity_mismatch_blocked` 逐 pass 重复——卡死本体已被 #1173
收敛（streak≥3 → `identity_mismatch_released` 终态），剩纯可观测性缺口。另有
本批新增的 `comment_accounting_unproven`（#1116）：生产集群上 reserved 行
**设计性永久**逐 pass 重复，正是本 marker 的直接受益者。

上游边界已由两单明文划定：#1152 Non-Goals「#1118 落地时应消费本单新增的
`operator_action_required` 字段而非另起判据」；#1173 design D3「跨 reason 聚合、
`no_progress_circuit_open` 告警升级仍归 #1118」。

## What Changes

**observe-only 跨 pass 熔断标记**（不改任何调度行为），新模块
`services/orchestrator/scheduler_no_progress.py`：

- **持久化跟踪器**（部署是 systemd oneshot——docs/runbooks/
  current-production-ops.md:134，每 pass 新进程，内存计数活不过一个 tick）：
  状态文件 `<evidence_root>/no-progress-tracker.json`，原子写原语见 tasks 1.1
  （既有 `write_new_regular_file`（scheduler_evidence.py:888-919）是
  O_EXCL 创建即独占语义，**不可照搬**——tracker 需覆盖写）。文件缺失 → 空态 +
  block 标 `state_reset: "missing"`；损坏 → 空态 + `state_reset: "corrupt"`；
  两者都**不抛**（observe-only，fail-open）。enabled 的完整 pass **总是**写
  状态文件（哪怕零条目），故健康期 `state_reset` 只会在首次启用出现一次。
- **集成点唯一且钉死**：`scheduler_runtime.py:1417` 那一次 `_write_evidence`
  （完整 pass 组装完毕）之前 observe+注入。**其余全部写盘点**——:711/:765/
  :805/:839/:897/:985/:1458 的早退分支、:596/:641/:681 的 prelock 写盘、
  :1430-1458 的 `SchedulerResourceLimitError` 分支（含 `_SchedulerProgressGuard`
  抛出）、:711 的 `lock_contended`（未持租约，并发风险）——**一律不观察、
  不读写状态文件**：早退/中止 pass 的候选列表是空的，在那里观察会把全部
  连续计数误清零。
- **观察源 = 完整 pass 已组装（未压缩）payload 的两个适配器**（消费不发明）：
  - **A1 候选适配器**：`candidates` + `blocked_candidates` 列表中
    `status == "blocked"` 且 `reason` 非空的行（行形状 =
    `SchedulerCandidate.to_dict`，scheduler_types.py:97-136，`candidate_id`/
    `status`/`reason` 顶层）；subject=("candidate", candidate_id)，
    reason=f"{status}:{reason}"。`skipped_candidates` **显式排除**（其行
    status 仍为 `selected`，含 `terminal_hydro_success` 类成功跳过，计入即
    永久假警报）。行的 `state_evidence.operator_action_required`（未压缩真实
    路径；bounded 键表 :26-35 是压缩期 hoisting 映射，非观察期路径）为 true
    时，作为该观察条目的布尔标注随行进入 block（#1152 消费义务由此闭合——
    该类行本身就是 blocked+稳定 reason 的 A1 行，1:1 同现，不设独立适配器、
    不撞 subject）。
  - **A3 reconcile 适配器**：仅当 `restart_reconcile.status == "completed"`
    且 `reserved_unbound` 键在场时，读
    `restart_reconcile.reserved_unbound.outcomes[]`（未压缩路径与 bounded
    白名单同形，scheduler_runtime.py:1535-1559）；subject=("job", job_id)，
    reason=f"{action}:{reconciliation_reason_class}"（reason_class 为 None 时
    仅 action）；覆盖 #1116 wedge 与 #1173 尾迹。
- **观察前置条件（防误清）**：只在完整 pass 观察；**每适配器带「源在场」
  判据**（A1：候选列表键在场；A3：如上）——源不在场（sacct 抖动走 :1561-1563
  只写 `reserved_unbound_error`、dry-run/store 不可用返回 None 或 skipped）时
  该适配器名下既有条目**原样保留**而非清除。
- **严格连续语义**（subject 键，镜像 #1173）：同 (subject, reason) 连续完整
  观察 pass count+1；同 subject 换 reason → 重置为 1；源在场且 subject 缺席 →
  条目清除（推进或离场即断连续）。状态文件条目数 ≤ 每 pass 观察数，天然有界。
  同一 subject 同 pass 多行时取首行（实现断言去重规则并测试钉死）。
- **开闸**：count ≥ 阈值 → open；payload 顶层 `no_progress_circuit` block：
  `{"threshold": N, "tracked": n, "state_reset": <absent|"missing"|"corrupt">,
  "open": [{subject_kind, subject_id, reason, consecutive_passes,
  first_pass_id, last_pass_id, operator_action_required?}]}`（pass_id 实存：
  `evidence["pass_id"]`，scheduler_evidence.py:232）；open 按 count 降序截断
  50 条 + truncated 计数。存在 open 时每完整 pass 记一条**聚合** WARNING
  （token `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`）——journalctl 是当前唯一被
  运维实际消费的通道。
- **配置**：`ProductionSchedulerConfig` 新增 `no_progress_circuit_passes`
  （env `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`，默认 3；≤0 禁用——不读
  不写状态文件、不注入键、零日志，逐字等旧），照 scheduler_config.py:221 邻位
  `_env_int` 惯例与范围校验。
- **压缩层**：`bounded_evidence_payload`（scheduler_evidence_payload.py:
  920-993）是字面量白名单重建——加 `no_progress_circuit` 键 + 照 :983-992
  模式的「源 payload 缺席即弹出」守卫（否则禁用态走压缩路径会凭空得到
  `null` 键，违反禁用逐字等旧）。**字节压力下先舍本块（两道门都要）**：
  第一道 5MB 判定若带 block 超限，弹块后**重试一次**再定超限与否（round-1
  C3：否则近限健康 pass 被 marker 单独顶过闸 → 全量降级
  `resource_limit_blocked`，终态被 observe-only 功能改写且 readiness 面读作
  blocked）；bounded 重建路径同样在任何既有字段被摘要/丢弃之前整块弹出
  （否则新键把既有 proof 块顶出 2400 字节级预算，既有保真测试真红）。两道
  之后：尺寸裁决、各压缩档与终态与本功能不存在时逐字相同。超预算 pass 的
  产物没有该块，但聚合 WARNING 与状态文件计数不受影响（D4：journalctl 才是
  真通道），runbook 写明「没这个块 ≠ 没开闸」。
- **落盘失败必须可见**（round-1 C1）：`write_state` 返回 False → block 置
  `state_write_failed: true` + 独立 WARNING token
  `SCHEDULER_NO_PROGRESS_CIRCUIT_STATE_WRITE_FAILED`；`os.open` 失败路径同样
  清理 `.tmp`（与其余三条失败路径对齐，unlink 失败再试 rmdir）：**可删**
  残留（悬空 symlink/空目录）下一 pass 自愈；**不可删**残留（非空目录/异主
  不可删文件）不自愈但每 pass 持续报警——计数绝不静默冻结（原缺陷突破
  design D1「丢失最多推迟开闸 N pass」的代价上界）。
- **WARNING 行携带 `last=<last_pass_id>`** + runbook 注明 adapter 级 gap
  （源缺席期条目保留、last_pass_id 冻结即陈旧信号）——round-1 C2 的可辨识性
  随行；preservation 语义本身是本 delta 明令，不改。
- **观察路径整体 fail-open**：`observe_pass` 外层 catch-all → WARNING token
  `SCHEDULER_NO_PROGRESS_CIRCUIT_OBSERVE_FAILED` + 返回 None（observe-only
  不得拖垮生产 pass，同 reconcile recovery 惯例）；测试断言键存在，吞异常
  仍红，不掩盖缺陷。
- **retention 兼容已核实为天然不命中**：`no-progress-tracker.json` 不以
  `scheduler_` 开头，归 `unrecognised` 跳过删除（scripts/
  node22_scheduler_evidence_retention.py:212-215/:268-277）；以测试钉住现状，
  无需白名单改动。
- `docs/runbooks/current-production-ops.md` 补一小节：block 形状、WARNING
  grep 姿势、`consecutive_passes` 是**完整观察 pass** 数（墙钟跨度可能大于
  同数 timer tick——早退/中止 pass 不计数不清零）、三类 reason 对应的下游
  runbook 交叉指引（#1152/#1173/#1116）。

## Non-Goals

- **不改任何调度行为**：不停重试、不熔断执行、不新增终态/decision/reason-class
  token（封闭集零改动）。circuit 是证据标记，不是执行闸。
- 不新建 ops API/告警通道（evidence 当前零消费端是既成事实——本单出口=block +
  WARNING，消费端建设另立）。
- 不做 (a) 类错误码的重试分类修正（permanent 归类是 #1431 的裁决）。
- 不做跨 subject 聚合、不做间歇容忍（缺席一完整 pass 即断——宁漏报间歇不误报
  持续；design D3）。
- 事故 (b) 本体（#1173 已收敛）与 #1431 cycle 内阶梯零改动。
- 早退/中止 pass 的 evidence 形状零改动（那些分支连 tracker 都不知道存在）。

## Risk triage

- Fixture level: expanded（新模块 + 持久化 + 配置 + evidence 契约 + runbook；
  observe-only、单集成点）。
- Repair intensity: medium。
- Risk packs: state-semantics selected（跨 pass 持久状态的连续/重置/清除/
  源在场保留语义 + oneshot 忠实性——测试必须以独立 scheduler 实例逐 pass 驱动
  共享 evidence_root；早退/中止 pass 不触碰 tracker 的锁）；test-evidence
  selected（每适配器红证 + 健康 pass 零观察锁 + disabled 逐字等旧锁含压缩
  路径）；payload-contract selected（未压缩观察路径 vs bounded hoisting 的
  区分、压缩层键守卫、block 有界、状态文件原子写与损坏恢复）；env-divergence
  not selected；其余 not selected。

## Must preserve

- 禁用（≤0）时逐字等旧：payload 无新键（**含超预算压缩路径**）、evidence_root
  无 tracker 文件、零新日志。
- 默认启用（3）下健康完整 pass 零观察、block 注入但 open 空、无 WARNING。
- 早退/prelock/资源中止/lock_contended 各分支的 evidence 与行为逐字不变，且
  **不读写 tracker**（`_SchedulerProgressGuard` 触发即属此类——两机制无交互
  由此成立）。
- 既有 evidence 键与 bounded 压缩行为逐字不变（只新增顶层键 + 缺席弹出守卫）；
  5MB 预算不因 block 突破（≤50 条）。
- 调度决策路径零改动（tracker 只读已组装 payload，不反馈决策）。
- 状态文件缺失/损坏不使 pass 失败（fail-open + 两值 `state_reset` 标注）。
- retention 脚本行为逐字不变（tracker 天然不命中，测试钉住）。

## Seams under test

- `scheduler_no_progress` 纯函数层（适配器抽取/源在场判据/merge/清除/open/
  截断）直接驱动；状态文件 round-trip（tmp_path evidence_root）；`run_once`
  集成层（`_config(tmp_path,...)` + `ProductionScheduler` 轻构造先例
  tests/test_production_scheduler.py:31013-31024/:419-457，逐 pass 新实例共享
  evidence_root；A3 需 dry_run=False + reconcile store，先例 :40138）；
  retention 谓词层（scripts 脚本 import）。
- 配置注入缝：env 与直接构造两路。

## Evidence mapping

- 验收 1（同 (subject,reason) 连续 N 完整 pass → open + WARNING）→ tasks 2.2
  红证（A1 主线）。
- 验收 2（换 reason 重置 / 源在场缺席清除 / 健康 pass 零观察 / 源不在场保留 /
  早退中止 pass 不触碰）→ tasks 2.3。
- 验收 3（A3 开闸 + operator_action_required 标注随行）→ tasks 2.4。
- 验收 4（disabled 逐字等旧含压缩路径 + 默认启用空 block 形状）→ tasks 2.5。
- 验收 5（持久化：oneshot 多实例、missing/corrupt 两值恢复、原子写、enabled
  完整 pass 恒写文件）→ tasks 2.6。
- 验收 6（bounded 压缩保留 + 缺席弹出 + 截断有界）→ tasks 2.7。
- 验收 7（retention 天然不命中钉住）→ tasks 2.8。
- Verification：`uv run pytest -q tests/test_production_scheduler.py` + ruff +
  openspec validate（本地）；merge 后 node-27 receipt；node-22 实机观察一个
  真实完整 pass 的 block 形状（post-merge 义务，记 #1118）。
