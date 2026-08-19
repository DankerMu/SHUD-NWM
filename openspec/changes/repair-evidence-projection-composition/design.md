# Design: repair-evidence-projection-composition

## 风险三角（fixture level: expanded）

- **风险**：candidate-state 投影是调度决策的唯一输入面，两处缺陷的下游都是「静默错值」（活 blocker 复活 / attempt 号重铸），无日志无 evidence；消费者横跨 `scheduler_state_failure` / `scheduler_state_manual_retry` / `scheduler_state_decision` / `scheduler_state_evidence_owner`。
- **可控性**：纯内存投影逻辑，本地 pytest 全闭环（两 issue 均 local-only oracle）；生产改动集中在单文件两个函数区块。
- **不确定性**：低——两缺陷均已被 verifier 在 master 比特级复现，#1461 附逐值对照表（`_fallback_previous_attempt` 4 vs 0、`_manual_retry_new_attempt` 5 vs 1）。

## D1（#1460）：break 去除 → 跨 retry job 累积，first-write-wins

`chain_repository_state.py:359-400` 循环改动：

- 删 `:400` 的 `break`；`repaired_by_failed_job_id` / `repair_events` 的写入改为 **key 已存在则跳过**（`if failed_job_id not in repaired_by_failed_job_id`）。外层 `sorted(..., reverse=True)` 已按 truth key 倒序，first-write-wins == newest-wins：同一 failed 行被多个 retry job 认领时保留最新那次。
- `_jobs_share_stage` 扩面腿：**判据不动**（仍是单 retry_job 迭代体内的 OR(stage, job_type)），但聚合标注面按设计**扩张**为各 retry job 扩面集合的并——这正是 #1460 要的多修复共存。E1/E3 须显式断言只有被修复 stage 的行被标注，防止同 `job_type` 无关行被过度认领（fixture review P2-3）。
- **latest_repair 等价性证明**（must-preserve）：今日 winner = 倒序首个非空 retry job 的 pair
  集合；累积后 `max`（key = retry truth, failed truth）在更大集合上取值——较早 retry job 的 pair
  其 retry truth 严格更低，且最新 retry job 认领的 failed_job_id 因 first-write-wins 不被覆盖，
  故 `max` 命中的 pair 与今日完全一致。`repaired_stage_evidence` 命名
  （`original_failed_job_id`/`repairing_retry_job_id`）与 `restart_stage` 零漂移。
- **备选已拒**（issue 原文）：保留 break、另起一趟补标注扫描——两趟状态域判据必然漂移（#1294 反复踩的坑）且多一份 O(jobs×retries) 遍历。

### D1a：source-cycle 兄弟腿核对（AC-5 就地闭合）

`chain_source_cycle._source_cycle_download_repair_state`（`:61-150`）经逐行 read-only 核对**无同形 single-winner**：它按 failed_job 逐个累积 `repaired_by_failed_job_id`（无 break 截断标注面），`latest_repair` 的单赢家仅用于 evidence 选择——正是 D1 的目标形状。无需改动，本节即 AC-5 的显式记录；实现阶段不动该文件。

## D2（#1461）：拆开互斥——completed-stage 分支独立化

`chain_repository_state.py:875-902` 结构改动（推荐方案「拆开互斥」，issue 两方案之首选）：

```python
repaired_restart_projected = False
if isinstance(repaired_stage_evidence, Mapping):
    state["repaired_stage_evidence"] = dict(repaired_stage_evidence)
    restart_stage = repaired_stage_evidence.get("restart_stage")
    if restart_stage not in (None, ""):
        repaired_restart_projected = True
        # ……既有投影 + 清空五键，逐字节不动……
if (
    not repaired_restart_projected
    and not _has_terminal_completion_stage_success(jobs)
    and (completed_stage_evidence := _best_completed_stage_success_evidence(...))
):
    # ……既有 completed-stage 投影，逐字节不动……
```

三个形状的语义账：

| 形状 | 修前 | 修后 |
|---|---|---|
| manual-stage 带 restart_stage | repaired 投影 + 清空五键，scan 跳过 | **byte-identical**（flag 拦住 scan） |
| 无 repaired 证据 | scan 照跑 | **byte-identical** |
| gap：source-cycle 变体（恒无 restart_stage）、manual-stage 终段 stage（`_stage_after` 为 None） | `repaired_stage_evidence` 落 state 但 completed-stage 投影被 elif 抑制 | `repaired_stage_evidence` 保留 **且** scan 照跑——两证据共存，`restart_stage` 来自候选自身完成事实 |

语义正当性（issue 原文）：`repaired_stage_evidence`（上游某失败已被修复）与 `completed_stage_evidence`（本候选完成到哪）正交，cycle-scope download 修复不该抹掉候选自身的 forecast 完成事实。

- **备选已拒**：给 `_source_cycle_repaired_stage_evidence` 补 `restart_stage = _stage_after(stage)`——`_FORECAST_STAGE_ORDER` 不含 download，对 `stage="download"` 恒 None，要么触及 #1308 A-4 域要么落回内层守卫失败，只挪不治。
- **不新增 Batch S 行扫描臂可达性，论证是单调性**（fixture review P2-1 修正后的口径）：D2 只会**新增**非空
  `restart_stage` 键，而 `_resolve_failed_stage`（`scheduler_state_failure.py:107-110`）显式键循环
  第三位读它——多一个非空显式键只会让行扫描更**不**可达。注意 `chain_repository_state.py:822-827`
  分支（`failed_task is None` 且候选零行、仅剩 repaired 证据）下 `stage`/`failed_stage` 本来就是
  None，不能以「五键未清空故非 None」为据。
- **D2 新开几何（须 must-preserve 钉）**：`:822` 分支 + gap 形状 + cycle 内有 succeeded
  convert/forcing/forecast 行时，候选（自身零行）首次拿到 cohort 派生的 `restart_stage`；
  `_candidate_failed_stage` 显式键腿 scope-blind（`scheduler_state_failure.py:100-104` 注释自陈），
  `_state_retry_attempt(state, stage=...)` 会读 cohort 计数——以 E13 用**绝对期望值**钉住
  （不做对照组等式：flat `retry_count` 因 `:829-831` fallback 天然与无 source-cycle 对照组不等，
  非本 change 引入，见 tasks 2.11）。

## Must-preserve（评审红线）

1. 单次修复既有场景零行为变化（`tests/test_production_scheduler.py:6800,7021` 一带全部现有用例）。
2. manual-stage 变体带 restart_stage 时投影 + 清空五键（`pipeline_status/stage/failed_stage/error_code/error_message`）逐字节不变。
3. `_has_terminal_completion_stage_success` cohort QC 守卫（cycle-wide job base 扫描，`ce25f729`）在新结构下仍拦住已终态完成候选的 restart 重武装。
4. `latest_repair` 选择与 `restart_stage` 计算不变（D1 等价性证明 + 测试钉）。
5. failed/cancelled `REPAIRABLE_PIPELINE_STATUSES` parity（PR #1449 裁定）不被破坏。
6. #1179/Batch S 的 floor 语义、explicit-key 语义、`_state_retry_attempt` 三臂口径不动。
7. `:814-816` 两变体取值优先级（source-cycle 先于 manual-stage）不动。
8. **gap 形状新组合「repaired 证据在场 + 带 `job_id` 的扫描产 completed 证据」的四个消费面**（fixture review P1-2）：
   - `scheduler_state_manual_retry.py:412` `_state_completed_stage_evidence_names_job`（#1308 row-absent
     pin gate 的输入 helper）——**helper 级**判定对 gap 形状 state 须与对照组（无 source-cycle 修复行）
     同值（E11 钉）。gate 整体（`:400-421`）对被 repaired 证据命名的 entity_id 有 `:410-411` 的先于
     本 change 的不对称短路，不做 gate 级等式（fixture review P2-4）。gate 算法本身仍 out of scope，
     但其**输入形状**变化由本 change 负责。
   - `scheduler_state_failure.py:1457-1499` `_completed_upstream_stage_retry_evidence`——gap 形状首次可能产出 `retry_after_completed_stage` 决策；须与对照组同判（E12 钉，这是决策通道不是纯记账）。
   - `scheduler_state_decision.py:451` `_terminal_stage_or_copyback_evidence`——gap + succeeded parse 时证据来源从 job 行改为 state key，`_terminal_evidence_matches_candidate` 两来源须同判（实现时核对字段判等，异常则记录）。
   - `scheduler_state_manual_retry.py:226-251,363-399` 两处 docstring 的「repaired-copy 无 `job_id`」陈述在 D2 后不再穷尽，须随实现更新（tasks 3.3）。
9. `:822` 分支 + gap + cohort succeeded 行的新几何（上文 D2 新开几何，E13 钉）。

## Seams under test

- `candidate_state_from_rows`（合成行喂入，#1461 verifier 复现口径）——投影层 oracle。
- `_candidate_manual_stage_repair_state` 直调——标注面 oracle。
- 下游接缝：`_failed_stage` / `_manual_retry_new_attempt` / `_restarted_stage_family` / blocker 扫描在同一合成几何上的收敛断言（跨文件接缝，不 mock 投影）。

## Evidence mapping（红先行腿列在 tasks 2.x）

E1-E4 对应 #1460 AC 四项，E5-E9 对应 #1461 AC 五项，E10 钉 manual-stage 终段 gap 形状，E11-E13 钉 must-preserve 8/9 的下游消费面与 `:822` 新几何；红先行腿 E1/E5（master 形状必红），其余构造性/回归性两侧同绿。
