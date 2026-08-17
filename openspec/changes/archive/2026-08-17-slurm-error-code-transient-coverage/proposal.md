# Proposal: slurm-error-code-transient-coverage

## Why

Issue #1419（#1313/PR #1417 design D4 承诺的 follow-up）：`map_slurm_error_code`
（real_backend.py:144-152）只有四路映射，`DEADLINE`、裸 `FAILED`、一切未识别
终态全部塌缩为兜底码 `SLURM_JOB_FAILED`。该码在 retry.py 两个集合都不在，
classifier 落 `unknown_failure`、`permanent=True`；PR #1417 把 downstream-resume
从码黑名单改为永久性判据后，兜底码第一次成为 resume 否决位——瞬时 Slurm 故障
（deadline 到点、未识别基础设施终态）不再自动续跑，落梯尾等人工。

## What Changes

按 issue 推荐方案「双侧收口」：

- **映射侧（design D1）**：`map_slurm_error_code` 增 `DEADLINE` →
  **`SLURM_DEADLINE`**（新码）、`BOOT_FAIL` → `NODE_FAILURE`（既有瞬时码，
  节点引导失败属基础设施故障族）；`BOOT_FAIL` 同步进 `SLURM_STATE_MAP`
  （→ FAILED，消除 "Unmapped" warning **并**具名承担 reconcile.py:1224
  file-cohort 投影 unverified→failed 的行为正确化，见 D1）。`REVOKED`、
  `SPECIAL_EXIT` **具名不映**（federation-only/未观测形，落真·未知是正确
  语义）。raw state 照旧写入 manifest（real_backend.py:1422-1423 行为不
  回退）。
- **分类侧（design D2）**：`SLURM_DEADLINE` **三面登记**——
  `retry.TRANSIENT_ERROR_CODES`、classifier `transient_slurm_runtime`、
  `scheduler_state_types.TRANSIENT_RETRY_REASON_CODES`（瞬时性的第二分类
  面，漏登会造「半瞬时码」：resume 放行但预算耗尽 reason 错报
  `permanent_failure_guard`）。`SLURM_JOB_FAILED` **保留真·未知兜底**：
  不入任何集合（保住 `unknown_error_code_defaulted_non_transient` 审计
  lane 与 anchor 判别力），但在 `failure_classifier` 给显式分支（行为
  等价、语义显式），spec 写明「未知终态码 = 非瞬时、拒自动 resume、需
  人工裁决」。
- **规格（design D3，双 delta）**：`real-slurm-gateway-contract` MODIFIED
  「Retryable Slurm errors are stable」——新增 DEADLINE 场景、BOOT_FAIL 并入
  node-failure 场景、未知终态场景补重试分类显式契约（resume 契约本体引用
  `job-retry-mechanism`）；`job-retry-mechanism` MODIFIED「Retry Guard —
  Non-Transient Error Exclusion」——显式瞬时清单加 `SLURM_DEADLINE`。
- **受影响面核对（design D4）**：四处硬写 `SLURM_JOB_FAILED` 兜底点逐一
  裁决（全部「不改」，理由具名记录——缺码回退语义与 D2 真·未知裁决一致）。
- **anchor 保全（design D5）**：主 anchor = `test_downstream_resume_
  refuses_recorded_non_transient_codes`（tests/test_production_scheduler.py
  :22600-22628 参数化，含 SLURM_JOB_FAILED）+
  `test_unlisted_production_error_codes_default_to_the_unknown_reason_
  and_warn`（tests/test_retry.py:352-383）——靠「全不入」活着；
  `test_repaired_raw_manifest_allows_stale_downstream_failure_retry`
  （:22135）零改动附带保绿。

## Risk Triage

- Fixture level: **expanded**。issue 预估 S-M，无 suggested level；跨三模块
  （gateway 映射 / retry 分类表 / resume gate 后果）语义一致性 + spec 显式
  契约 + anchor 判别力保全，多载体高于 compact；无状态机/删除面，不到
  high。divergence：无。
- Repair intensity: standard。
- Risk packs:
  - compatibility/regression: **selected** —— PR #1417 permanence gate 零
    回归（scheduler_state_failure.py 分域逻辑不动）；既有 anchor 测试判别力
    保全；`PREEMPTED`/`TIMEOUT`/`OOM` 既有映射不动。
  - classification-consistency（state-machine pack 变体）: **selected** ——
    「瞬时码双面登记」不变式（TRANSIENT ∩ NON_TRANSIENT = ∅；两瞬时面
    `retry.TRANSIENT_ERROR_CODES` == `scheduler_state_types.
    TRANSIENT_RETRY_REASON_CODES` 相等钉测；新码同时入两面 + classifier；
    `SLURM_JOB_FAILED` 显式全不入）；resume 两方向钉子（瞬时码放行 /
    未知码拒绝）。
  - spec-compliance: **selected** —— MODIFIED requirement 场景与实现逐句
    对读；重试分类从「未约束」变「显式契约」。
  - deletion-safety、security/auth、performance: not selected —— 无删除/
    权限/热路径面（纯映射与集合成员）。
- Seams under test（upstream 已声明）：`map_slurm_error_code` 纯函数直测；
  retry.py 集合与 classifier 直测；downstream-resume 方向经
  `scheduler_state_failure` 既有测试面注入 error_code。

## Non-Goals

- PR #1417 permanence gate 本体（`scheduler_state_failure.py:339-343` 分域
  调用点与 `:1315-1329` `_downstream_failure_restartable` 判据零改动）。
- `OUT_OF_MEMORY` 非瞬时裁决（#1161 已定，#1323 另议）。
- `auto_retry_skipped` 审计事件（#1314）。
- `chain_forecast_execution.py` 的 `{JOB_TYPE}_{STATUS}` 合成码族分类
  （推断性关联，需单独裁决立 issue）。
- `REVOKED` 及其它 federation/未观测状态的专属映射（具名不映，落真·未知）。
- 裸 `FAILED` 的瞬时化（应用级失败与 requeue 后残留形不可区分，保持真·未知
  是 permanence gate 的正确方向；spec 显式写明）。

## Impact

- `services/slurm_gateway/real_backend.py`（SLURM_STATE_MAP + map_slurm_error_code）
- `services/orchestrator/retry.py`（TRANSIENT_ERROR_CODES + failure_classifier）
- `services/orchestrator/scheduler_state_types.py`（TRANSIENT_RETRY_REASON_CODES）
- `openspec/specs/real-slurm-gateway-contract/spec.md` ·
  `openspec/specs/job-retry-mechanism/spec.md`（archive 回写）
- `tests/test_real_slurm_gateway.py` · `tests/test_retry.py` ·
  `tests/test_production_scheduler.py`（:22633 参数化加格）· reconcile
  cohort 投影测试（test_gateway_reconcile.py / test_reconcile_sacct_parse.py
  就近）
- 核对不改：`services/production_closure/slurm_validation.py:1571,1616,1712`、
  `services/orchestrator/file_orchestration_journal.py:3142`、
  `services/orchestrator/reconcile.py` 调用点（:728/:977 带默认值不变；
  :1224 行为翻转具名承担，见 D1）
