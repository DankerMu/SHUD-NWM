# Design: no-progress-circuit-evidence (#1118)

## D1 部署模型决定持久化形态

生产调度器是 systemd oneshot（`nhms-compute-scheduler.timer` 每 tick 起新进程
跑一个 `run_once`，docs/runbooks/current-production-ops.md:134；
infra/compose.compute.yml:121-139 同形）。`run_continuous` 存在但非生产路径。
因此跨 pass 计数**必须**落盘；仓内唯一跨 pass 计数先例 `identity_blocked_streak`
走 journal 原子替换（file_orchestration_journal.py:2868/:5993）。本单不用
journal（它是 per-job 行存储，而 circuit 主体含 candidate 级观察且属 evidence
域而非账本域），改用 evidence_root 下独立 JSON 状态文件 + 原子写（tmp+rename，
与 evidence 写盘同纪律）。代价：状态文件不进 journal 的事务边界——可接受，
observe-only，丢失最多推迟开闸 N pass。

## D2 观察源与「不发明判据」

#1152/#1173 明文把「跨 pass 聚合」划给 #1118 并要求消费其事实字段。三个适配器
全部读**已组装的 evidence payload**（bounded 幸存键），不读内部对象：

| 适配器 | 源字段 | subject | reason | 覆盖事故 |
|---|---|---|---|---|
| A1 | candidate summaries（status/reason） | candidate | `{status}:{reason}` | (a) warm-state mismatch 六天类 |
| A2 | state evidence `operator_action_required` | candidate | `operator_action_required:{decision}` | #1152 predecessor-pending 类 |
| A3 | reserved_unbound outcomes | job | `{action}:{reason_class}` | #1116 wedge、#1173 尾迹 |

读 payload 而非内部对象的理由：(1) 字段契约已被 bounded 白名单钉死、有测试；
(2) 集成点天然单一（write_evidence 前）；(3) observe-only 与决策层解耦在
类型层面即成立。风险：payload 键形状变化会静默饿死适配器——用契约测试钉住
（每适配器一条「真实形状 payload → 观察产生」的测试，形状漂移即红）。

## D3 连续语义与内存安全

严格连续（镜像 #1173）：`(subject, reason)` 逐 pass 相同才累加；换 reason 重置
为 1；缺席即清除。推论：状态文件条目数 ≤ 当 pass 观察数（每 pass 全量重建交集），
无历史累积、无泄漏。open 列表与 WARNING 截断至 50 条（count 降序 + truncated
计数）。**不做** grace/间歇容忍（缺席一 pass 即断）——宁可漏报间歇性问题也不
把「偶发恢复」误报为持续卡死；间歇聚合是显式 non-goal。

## D4 告警通道现实

evidence JSON 当前零消费端（无 ops API 路由、retention 脚本只删不读）。issue
的「让 monitor 看到」在当前架构里唯一真实通道是 journalctl——所以 WARNING 是
一等出口而非附属：聚合单条、含 subject/reason/计数、token
`SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN` 可 grep。evidence block 是给未来消费端与
人工取证的结构化对账面。消费端建设（API/推送）显式另立。

## D5 与近邻机制的互斥边界

- `_SchedulerProgressGuard`：intra-pass 阶段熔断（每 run_once 新建，抛
  SchedulerResourceLimitError 改变行为）。本单 cross-pass、observe-only、永不抛。
  两者共存无交互。
- #1431 warm-start ladder：cycle 内阶梯 + escalate，不落 mark、下一 cycle 重走。
  本单在其外层数「连续 cycle 都 escalate/失败」的次数——A1 观察到的是每 pass
  candidate summary 的失败残影，无需触碰 runtime worker。
- #1173 streak：per-row、驱动**行为**（释放终态）。本单 per-(subject,reason)、
  纯证据。A3 观察到 streak 尾迹属预期重叠（circuit 开闸早于/伴随释放，释放后
  subject 离场自动清除）。

## D6 禁用与默认

默认 3（与 #1173 一致；两起事故分别是 ~数十 pass 与 ~数天，3 足够早且
observe-only 无误伤面）。≤0 禁用 = 完全旁路（不读不写不注入不日志），保证
回退路径是纯配置动作。
