# Design: no-progress-circuit-evidence (#1118)

## D1 部署模型决定持久化形态

生产调度器是 systemd oneshot（`nhms-compute-scheduler.timer` 每 tick 起新进程
跑一个 `run_once`，docs/runbooks/current-production-ops.md:134；
infra/compose.compute.yml:121-139 同形）。`run_continuous` 存在但非生产路径。
因此跨 pass 计数**必须**落盘；仓内唯一跨 pass 计数先例 `identity_blocked_streak`
走 journal 原子替换（file_orchestration_journal.py:2868/:5993）。本单不用
journal（per-job 行存储，而 circuit 主体含 candidate 级观察且属 evidence 域），
改用 evidence_root 下独立 JSON 状态文件。写原语**不可照搬**
`write_new_regular_file`（O_EXCL 创建即独占，scheduler_evidence.py:888-919）——
tracker 需覆盖写：目录 fd 相对创建 `.tmp`（O_NOFOLLOW，0o644）→ 写 + fsync →
dir_fd 相对 `os.replace`；读取侧同样 O_NOFOLLOW。`.tmp` 兄弟文件对 retention
无害（`_has_inflight_sibling` 谓词，脚本 :205-209）。代价：状态文件不进
journal 事务边界——可接受，observe-only：正常丢失最多推迟开闸 N pass；落盘
持续失败（不可删 `.tmp` 残留等）不再受该上界约束，改由 `state_write_failed`
+ 独立 WARNING 每 pass 兜底（round-1 C1 修复后的真实代价模型）。

## D2 观察源与「不发明判据」

两个适配器读**完整 pass 的未压缩 payload**（不读内部对象、不读 bounded 键表
——scheduler_evidence_payload.py:26-35 是压缩期 hoisting 映射，观察期真实路径
是 `row["state_evidence"]["operator_action_required"]`）：

| 适配器 | 源（未压缩路径） | subject | reason | 覆盖 |
|---|---|---|---|---|
| A1 | `candidates`+`blocked_candidates` 中 status=="blocked" 且 reason 非空的行（`skipped_candidates` 排除：status 仍 selected，含成功跳过） | candidate | `blocked:{reason}` | (a) permanent_failure_guard 类、#1152 predecessor-pending 类 |
| A3 | `restart_reconcile.reserved_unbound.outcomes[]`（仅当 status=="completed" 且键在场） | job | `{action}:{reason_class}` | #1116 wedge、#1173 尾迹 |

#1152 的 `operator_action_required` 不设独立适配器：该类行本身就是 A1 的
blocked 行（1:1 同现，`decision` 顶层字段不存在——唯一生产者
scheduler_generation_gate.py:749-789 只发 `continuity_policy.decision`），
独立适配器会与 A1 撞同一 subject 导致两 reason 互相重置、永不开闸。降级为
A1 观察条目上的布尔标注，消费义务闭合且状态模型保持 subject 键。

契约风险（payload 键形状漂移静默饿死适配器）用契约测试钉住：每适配器一条
「真实形状 payload → 观察产生」，形状漂移即红。

## D3 连续语义、误清防护与内存安全

严格连续（subject 键，镜像 #1173）：同 (subject, reason) 连续**完整观察 pass**
累加；换 reason 重置为 1；**源在场**且 subject 缺席才清除。两层防误清：

1. **pass 级**：只在 `scheduler_runtime.py:1417` 的完整 pass 写盘点前观察。
   其余 11 个写盘点（早退 :711/:765/:805/:839/:897/:985、异常 :1458、prelock
   :596/:641/:681、callback scheduler_runtime.py:2006）payload 候选列表为空/无
   restart_reconcile——在那里观察等于每次早退清空全部计数；`lock_contended`
   （:711）还在未持租约下运行，共享写有并发风险。实现上**不得**挂
   `scheduler_core.py:914` 的共享 `_write_evidence` 方法（8 站点全走它，是最
   自然也最错的落点）。
2. **适配器级**：源在场判据（A1 候选列表键在场；A3 status=="completed" 且
   `reserved_unbound` 在场——sacct 抖动走 :1561-1563 只写
   `reserved_unbound_error`，dry-run/store 不可用返回 None/skipped）。源不在场
   → 该适配器名下条目原样保留。否则一次 sacct 抖动就把 #1116 wedge 计数清零。

推论：状态文件条目数 ≤ 当 pass 观察数（源在场的适配器域内全量重建交集），
无历史累积。open 列表与 WARNING 截断 50 条。**不做**间歇容忍（缺席一完整
pass 即断）——宁漏报间歇不把偶发恢复误报为持续卡死。运维口径：
`consecutive_passes` 数的是完整观察 pass，墙钟跨度可能大于同数 timer tick
（中间的早退/中止 pass 不计数也不清零），runbook 写明。

## D4 告警通道现实

evidence JSON 当前零消费端（无 ops API 路由、retention 只删不读）。当前架构
唯一真实通道是 journalctl——WARNING 是一等出口：聚合单条、token
`SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN` 可 grep、含 subject/reason/计数（截断
50）。subject 键模型下同一 wedge 只产一条 open 条目，无聚合去重问题。
evidence block 是给未来消费端与人工取证的结构化对账面；消费端建设显式另立。

## D5 与近邻机制的互斥边界

- `_SchedulerProgressGuard`（scheduler_runtime.py:31-70）：intra-pass 阶段
  熔断，抛 `SchedulerResourceLimitError` **改变行为**；其异常分支（:1430-1458）
  正是本单「不观察」的中止 pass 类——两机制无交互由集成点选择直接成立。
- #1431 warm-start ladder：cycle 内阶梯，不落 mark、下一 cycle 重走。本单在
  其外层数「连续完整 pass 都 blocked」——A1 观察的是候选失败残影
  （permanent_failure_guard），无需触碰 runtime worker。
- #1173 streak：per-row、驱动行为（释放终态）。本单 per-(subject,reason)、
  纯证据。A3 观察 streak 尾迹属预期重叠（释放后 subject 离场自动清除）。

## D6 禁用与默认

默认 3（与 #1173 一致；observe-only 无误伤面）。≤0 禁用 = 完全旁路（不读不写
不注入不日志，**含压缩路径**——bounded 层的键守卫保证禁用态超预算 pass 不
凭空出现 null 键），回退是纯配置动作。
