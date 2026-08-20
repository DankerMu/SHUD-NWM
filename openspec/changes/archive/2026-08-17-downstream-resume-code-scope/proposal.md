# Proposal: downstream-resume-code-scope

## Why

Issue #1420（PR #1417 round-1 V1-C2 CONFIRMED-DEFER，判据裁决缺口）：
downstream-resume 的 `code_recorded` 分域用「state **任意处**是否存在错误码」
（`_state_error_code` 全扫描面：顶层 → hydro_run → 全部 jobs 逆序 → 全部
events）回答「当前失败有没有真实记录码」。当前失败 job 无码（占位域存在的
理由）+ state 残留历史码（已恢复 stage 的 `error_code` / auto-retry event 的
`previous_error`）的候选被误路由进记录域、拒绝 master 会放行的 resume。
几何 B 不是边角：**任何有过一次自动重试的候选都带 `previous_error`**——
SHUD 已算完、publish 无码失败的常规 run 因一条已恢复的重试记录落
`permanent_failure_guard` 等人工，白扔已产出算力。

## What Changes

按 issue 推荐方案裁决：**`code_recorded` = 当前失败自身记录的码**。

- **裁决与新取景 helper（design D1/D2）**：新
  `_downstream_recorded_error_code(state)`，只看当前失败的载体——顶层
  `error_code`/`reason_code`/`failure_reason`（`last_error`/
  `previous_error` 是重试历史键，排除）→ `hydro_run` 同三键**加状态
  取景**（成功态 run 行的残码是陈旧载体：SQL 后端成功转换不清码，
  status ∈ journal 清码集时跳过）→ `_failed_stage` 对应的**失败态** job
  自身 `error_code`/`reason_code`（canonical-stage 相等、status ∈
  FAILED_PIPELINE_STATUSES、非 repaired-evidence，命中首个匹配 job 即停）。
  events 不参与分域（已闭合裁决：event-only 码必经 failed_task 投影到
  顶层，见 D2 第 4 点实证）。
- **消费点收窄（design D3）**：仅 `scheduler_state_failure.py` 的
  downstream-resume 消费点（`:339` `recorded_error_code = ...`）换用新
  helper。`_state_error_code` 通用契约与 `_failure_policy_payload` 的
  reason_code 文本全扫描面**不动**；其余消费点（`:1757`/`:1762` 失败信号
  检测）不动。
- **规格（design D4，双 MODIFIED delta——两载体同步，防 spec-carrier-lag）**：
  `multibasin-state-idempotency`「Resumable downstream failures」定义
  "genuinely recorded" 的取景 + 新增陈旧历史码场景；`job-retry-mechanism`
  「Pre-Guard Evidence Channels Consult Permanence」两处同步——
  recorded-code scoping 段（"when the state records no error code" →
  失败自身取景）+ 占位域场景 WHEN 同步收窄。
- **测试（design D5）**：几何 A/B 红-绿主锚（B 用 `retry.py:448` 真实
  event 形状含候选身份字段）、对照组不变、#1313 既有 anchors 全绿、
  helper 单元判定表（14 格）。

## Risk Triage

- Fixture level: **expanded**。issue 预估 S-M，无 suggested level；改动是
  决策梯 resume 判据的取景收窄——错一格即「该拒的放行」（削弱 #1313
  permanence gate）或「该放的继续拒」（issue 原病不愈），双向都有真实
  代价；双 spec 载体 + 既有 anchor 密集区。无删除/文件面，不到 high。
  divergence：无。
- Repair intensity: standard。
- Risk packs:
  - decision-ladder/permanence（state-machine pack 变体）: **selected** ——
    分域翻转双向钉死：陈旧码不得翻入记录域（几何 A/B）；当前失败自身
    永久码/unknown-default 仍必须拒（#1313 anchors 原样绿）；
    `limit_exhausted` 先于分域的既有序不变。
  - compatibility/regression: **selected** —— `_state_error_code` 契约
    零改动（其余消费点 diff 级不动）；`_failure_policy_payload` 文本面
    不回退；对照组行为逐位不变。
  - spec-compliance: **selected** —— 双 MODIFIED requirement 与实现逐句
    对读；两载体措辞一致（#1407 教训：spec 是归档后唯一活契约）。
  - deletion-safety、security/auth、performance: not selected —— 无删除/
    权限/热路径面（纯取景函数）。
- Seams under test：`_downstream_recorded_error_code` 纯函数直测；分域
  行为经 `ProductionScheduler.run_once()` 端到端（issue Verification 的
  复现构造）+ 既有 `_durable_downstream_failure_state` 测试族。

## Non-Goals

- `_state_error_code` 通用契约与 `_failure_policy_payload` 其他消费点
  （reason_code 文本、失败信号检测）。
- #1313 permanence gate 判据本体（`_downstream_failure_restartable`
  `:1315-1330` 不动）。
- `map_slurm_error_code` 映射（#1419 已交付）。
- `scheduler_state_rows.py:643-644` repair-marker 判据本身——新取景下已
  恢复 stage 行被 status 过滤天然排除，不再依赖标记齐全；标记判据留给
  broad-scan 文本面（后果仅文案）。
- events 作为分域证据的任何扩展（若未来出现「当前失败码只在 event」的
  真实写入形，按新 issue 裁决）。

## Impact

- `services/orchestrator/scheduler_state_failure.py`（新 helper + `:339`
  消费点）
- `openspec/specs/multibasin-state-idempotency/spec.md` ·
  `openspec/specs/job-retry-mechanism/spec.md`（archive 回写）
- `tests/test_production_scheduler.py`（几何 A/B/对照组 + 既有 anchors）
- 核对不改：`scheduler_state_decision.py:338` 分域注释（如措辞涉及取景则
  随裁决更新，实现时指认）
