# Design — chain-manual-retry-marker-freshness（#1201）

行号口径：符号名为准，行号仅导航（as of master 66e55398）。
（rev-1：fixture review P0/P1/P2×2/Note 全部并入——判别器下沉 manifest 铸造点。）

## D0: 风险三角与边界

- 风险轴：state-semantics / correctness（静默自锁 → stage 永久无法重投）；p1。
- fixture 级别：expanded（P1 + 双通道判别 + 消费者审计面）。
- must-preserve：
  1. 新鲜 marker 的精确 attempt 身份（`test_manual_retry_terminal_stage_submits_new_attempt_identity`
     ——scheduler 判定 fresh 时 chain 必须瞄准指定 `retry_<N>`）。**限定**：仅当该
     manual-retry 决策面活到 manifest 铸造点。更高优先的**在途 evidence 变换**
     （典型：strict warm start 的 `_upgrade_retry_for_strict_warm_start_manifest`
     以 `**dict(retry_evidence)` 重写 `decision`/`reason`、marker 块逐字保留）抢占该
     决策面后，claim 按"无活跃决策"处置——不铸两键、留丢弃记录、attempt 降级为该
     lane 自身派生（该 decision 在 force 集合内，仍真实提交）。这是有意行为（markerless
     等价优先于 pin），非缺陷；残余的是审计面不一致（evidence 仍带
     `retry_policy.attempt=N`），由 E11 钉住现状。
  2. **运维/API 当次显式传入**的 `basin.retry_attempt`/`manual_retry_attempt` 语义
     不变；但 **scheduler manifest 从持久 marker 铸出的同名字段**必须在铸造点受判
     （P0 修正：两者是不同来源的同名字段，纪律打在铸造点而非消费点）。
  3. reserve/reclaim fail-closed 语义不放宽。
  4. `_replacement_retry_scoped_cycle_execution` 的既有判定几何。
  5. manifest 的其它键（state_evidence 透传、restart_stage 等）逐字节不变。
  6. **丢弃 claim 后的行为收敛到 markerless 等价**（re-check P1 裁定）：被判无活跃
     决策的 claim 不得再以任何方式改变执行——包括不得把本属 markerless 语义的
     `_resume_cycle_stage` 出口"救"成强制重投。force 集合外 decision + 终态失败行
     的 resume 是该 lane 的 markerless 既有语义（本 issue 移除的是比 markerless
     更糟的 wedge，不重开各 lane 的重投政策）；E1 双格 + markerless 孪生断言钉。
     **等价比较面限于**：重投决策（resubmit vs resume）、attempt 派生值、是否真实
     提交；scoping 谓词按 D3 保持现状、不在等价面内（mint-gate 后 marker 候选的
     `_manual_retry_scoped_cycle_execution` 仍 True 而真 markerless 为 False——生产
     无差异因单 basin 候选恒带 `orchestration_run_id`；孪生几何应带该键贴生产形状）。
     实现者不得为拉平等价面去 gate scoping 谓词的 evidence 臂（撞 D3 边界）。
     wedge 的正确出口佐证（round-1 更正机理）：未耗尽行经 resume 失败结果进
     `_schedule_cycle_stage_retry` 自动重试 lane；已耗尽行的正门是运维再发一次
     manual retry——新发一次会**新增一条 marker event**，`_manual_retry_payload`
     从最新一条已采纳 marker event 重算 payload（`reversed(_state_events)` 扫到即
     `break`），`new_attempt` 取该新事件的 `retry_count`（过 `_marker_event_pins_attempt`）
     或 `_fallback_previous_attempt(...) + 1`。**保护来自 payload 被重新派生，不是
     evidence 的字面量顺序**：`_manual_retry_new_attempt` 第一件事就是返回 payload
     自带的 `new_attempt`（`scheduler_state_manual_retry.py:996-1004`），所以"计算值
     覆盖 raw echo"的说法为假。生产 state provider（`chain_repository_state.
     candidate_state_from_rows`）不产出 state 级 `state["manual_retry"]` mapping，
     marker 恒为 event 派生，故 `dict(marker)` + `setdefault` 不会遮蔽新事件；测试
     fixture 用的 state 级 mapping 形状则会（该形状下旧值原样再铸，正是 E7/E10 钉的
     判别器输入，不是出口佐证）。
- oracle：本地 pytest。issue AC-6 的 node-22 live 复跑取"说明不可得"分支：纯内存编排
  逻辑，node-22 是 Slurm 行为 oracle，本家族 oracle 为本地 pytest；P0 暴露的真实缺口
  是 **scheduler→chain 接缝**，以 seam 腿（E9）闭合，无需 node-22。

## D1: 判别器——单一谓词、两个消费点、铸造点优先

新鲜性判别谓词（单处实现，共享）：`state_evidence` 的决策面存在活跃 manual-retry
决策——`decision == "manual_retry"` 或 `reason == "manual_retry_requested"`（fresh
写点 `_manual_retry_state_evidence` 同时写两键；raw echo 通道
`scheduler_state_evidence_owner.py:108` → `_manual_retry_payload` 不写任何一个）。

**两个消费点**（P0 修正——生产主通道在 manifest）：

1. **`scheduler_candidate_manifest._candidate_manual_retry_attempt`（生产主通道）**：
   该函数手里就有整份 `state_evidence`，现仅以 `allowed is False` 拦截（陈旧 raw
   echo 不带 `allowed`，直接放行）→ 铸出 `manifest["manual_retry_attempt"]` **和**
   `manifest["retry_attempt"]`，经 `chain_forecast_cycle` 透传成 direct 字段，
   **完全遮蔽** chain 侧 evidence 分支。判别加在这里：决策面无活跃 manual-retry
   决策 → 该函数返回 None → manifest **不铸**这两个键。
2. **`chain_runtime_utils._retry_attempt_from_basins` 的 evidence 分支**（直接
   basin/测试/API 路径的纵深）：同一谓词判别；direct 字段两分支不经判别（来源
   语义见 must-preserve 2——运维 API 显式传入走这里；manifest 铸出的同名字段已在
   铸造点受判）。

谓词单处实现防漂移：落点定为 `services/orchestrator/retry_identity.py`（纯 stdlib
依赖、chain 侧与 scheduler-state 侧均已 import 的中立公共位——放 chain_runtime_utils
会让 scheduler_candidate_manifest 反向 import chain 层），两个 gate 同源调用；禁止
两处各写一份。compat 面：`_candidate_manual_retry_attempt` 是 compat-guarded 名字
（`docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` / `scheduler.py` forwarder），
只改行为、不改签名与落点。

- **fail-safe 方向**：判别不出（无 decision/reason 键）→ 不采信 → fall through 到
  `_next_retry_attempt_for_stage`。scheduler 正常 evidence 恒带 decision 键。
- **判别措辞（P1 修正）**：判别命题是"**本 evidence 上没有活跃 manual-retry 决策**"，
  不声称等价于 scheduler 的完整新鲜性判定——`resume_after_completed_stage`（
  `scheduler_state_decision.py:252`，早于 manual_retry lane 返回）等 lane 的 evidence
  同样 `**base_evidence` 携带 raw echo，其 marker 未必"陈旧"（可能只是被更高优先
  lane 抢占）。这些形状下丢弃 attempt claim 依然是安全方向（降级为下一空闲 attempt，
  仍提交），但日志与 spec 措辞必须说"无活跃 manual-retry 决策"而非"stale"。实现时
  探针核查 resume_after_completed_stage + marker 组合的可达性并记入测试注释。

## D2: 消费者面（P2 修正——三个消费者 + 一处回写）

| 消费者 | 现状 | 修后 |
|---|---|---|
| `_retry_cycle_stage_job_id`（`chain_forecast_orchestrator_cycle.py` :158）`or` 短路 | 陈旧 marker 钉死目标 id → reserve 必输 → 静默跳过 | context 源头净化；短路语义保留 |
| `:581` `submission_attempt = max(int(context.retry_attempt or 1), 1)` | 同一污染源 | 随净化愈合；E4 钉 |
| **`chain_manifests.py:423` `submission_attempt`（写进 SHUD runtime manifest）** | 同一污染源（fixture review 补） | 随净化愈合；E4 一并钉 |
| **`chain_stage_execution.py:249` 回写 `context.retry_attempt = reservation.submission_attempt`** | 后续 stage 经同一 `or` 短路**继承**首 stage 预留号 | 行为保留（预留号是真实占位）；E1 多 stage 断言把继承链算进预期 |
| **`chain_forecast_orchestrator_cycle.py:173` `_terminal_stage_needs_manual_retry`（`context.retry_attempt is None → False`）** | claim 在时该谓词开启终态行重投 lane | claim 丢弃 → None → 谓词 False：decision 在 `_FORCE_TERMINAL_RESUBMIT_DECISIONS` 内仍强制重投（#1201 live 几何走此路）；集合外 + 终态失败行 → `_resume_cycle_stage`——**markerless 等价**（must-preserve 6 裁定，E1 out-of-force 格 + 孪生断言钉；spec 披露） |
| `chain_forecast_control.py:149` 注入点 | 裸 max | 不变 |

## D3: `_manual_retry_scoped_cycle_execution` 现状保持（理由按 fixture review 重写）

裁定不变：现状保持 + 显式边界。**正确理由**（P2 修正）：该谓词经
`_candidate_scoped_cycle_execution` 参与选择喂给 `_next_retry_attempt_for_stage` 的
job 集合（`chain_runtime_utils.py:97` / `chain_forecast_cycle.py:479-489`）——它并非
与 attempt 派生无关；但 (a) job id 以 run_id 命名空间化（`chain.py:864-871` 前缀过
滤），跨 run 的 job 混入不改变本 stage 前缀 max；(b) 生产单 basin 候选恒带
`orchestration_run_id`（`scheduler_execution.py:352`，cohort run id 跨 pass 确定），
该谓词**在这个消费者上**不是 scoping 的决定项。陈旧 marker 在此的残余效应是 fail-open
的作用域放宽，无 attempt 铸造、无自锁。

**该谓词有第二个消费者**（round-1 更正，原文口径过窄）：
`_replacement_retry_scoped_cycle_execution` 首行短路（`chain_runtime_utils.py:186-188`），
其返回值喂 `_active_orchestration_conflicts`——True 时活着的 unsubmitted-retry
placeholder 不再阻塞，且直接判"无冲突"而不再问 `has_active_pipeline`。这条臂上
(b) 的论证**不成立**：`orchestration_run_id` 摆不平它，带 marker 的候选可以穿过
markerless 孪生会被拦住的重复编排冲突门。该分叉是**既有行为**（修前经铸出的 direct
字段进同一臂、修后经 evidence 臂，前后同 True，must-preserve 4 未破），因此它不在
must-preserve 6 的三项等价面内——这也是等价句必须限定到三项面、不能写成"落在
markerless 孪生的同一处"的原因。同源的还有 scheduler 侧
`scheduler_state_decision._candidate_state_is_candidate_scoped_retry`（读同一
`marker`/`requested`/`allowed` 三元组放宽候选准入，同样未 gate，pre-existing、
fail-open 无 wedge）；全仓再无第三个未判别 marker 读者。

E6 钉住"scoped 谓词选择的 job 集合与 fall-through
派生的耦合"（陈旧 marker 存在时 `_next_retry_attempt_for_stage` 输入集合的行为现状），
另加注释挂账。mint-gate 对 scoping 中性依赖 D3 现状保持：两键不铸后
`_manual_retry_scoped_cycle_execution` 仍从 evidence 分支（`marker`/`requested`/
`allowed` truthy）返回 True——该耦合由 E6 行为钉覆盖。

## D4: 非静默证据（AC-4；措辞按 P1 修正）

丢弃 attempt claim 时结构化 warning：措辞为"manual_retry attempt claim ignored —
no active manual-retry decision on this evidence"（不用 "stale"），含 basin/cycle
标识、claim 值、实际 decision/reason（终评 Note-1 后：止于写点可知处，不含任何
下游 targeting/fall-through 从句——tasks 1.4 同口径）。铸造点（manifest）与
evidence 分支两处共用同一日志 helper（或各发一条同 schema 的记录——实现者裁定，
但字段 schema 必须一致）。E5 caplog 钉。

## D5: spec 对账（Note 修正）

delta 的新 requirement 与主 spec :467 大 requirement（cycle-scope marker 不得 pin
候选 attempt）交叉引用：后者治 event 派生分支的 pin 纪律
（`_marker_event_pins_attempt`），state 级 `state["manual_retry"]` 整包 copy 不过
pin 测试正是 #1201 形状存活的通道——新 requirement 补上 chain/manifest 侧的最后
一道判别，两处 attempt-pin 纪律互为引用不各说各话。

## D6: 平台与回归

- 纯内存 + 日志；无 3.12+ API。回归面：`tests/test_orchestration_chain.py` 全量 +
  `tests/test_production_scheduler.py` 全量（manifest 铸造点在 scheduler 侧，其投影
  钉 `tests/test_production_scheduler.py:10022` 一带必须保持）。
- 不选 pack：performance / security / release。
