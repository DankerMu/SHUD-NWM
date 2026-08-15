# Proposal: pre-guard-permanence-gate (#1313)

## Why

db-free 决策梯（`scheduler_state_decision.py`）是逐通道各自持有拒绝名单的
结构：唯一 permanence 兜底 `_permanent_failure_evidence`（`:363-372`）排在
若干 evidence 通道之后，通道可在不咨询任何 permanence 判据的情况下把
`_failure_policy_payload` 算出的 `permanent=True` 覆写回
`retryable=True`，`automatic_retry_allowed: True` 直接进入调度侧。三个表现
面（#1313，同一根因）：(1) raw-manifest 修复双通道
（`scheduler_state_failure.py:1123-1176` / `:1178-1241`）对**所有**永久码
无条件复活——OOM 在该几何下重跑同一 `memory_gb` 必然复现；(2)
downstream-resume 的 `_downstream_failure_restartable`（`:1061-1075`）用
显式黑名单而非 permanence 判据，unknown-default 非瞬时码（如
`PARSE_FAILED`）照样 resume，与 spec `job-retry-mechanism:166-171`
（未列码默认非瞬时 MUST NOT 自动重试）直接冲突；(3) 候选 state 顶层
`retryable: True` 在 `_failure_policy_payload:110-112` 于分类后、全部拒绝
臂之前翻转 permanence——今日 latent（无生产 writer），但为
identity-filter 白名单承认的透传键（`scheduler_state_identity_filter.py:181/:596`）。

## What Changes

- 引入**单一 permanence-conscious 判据源**（shared refusal helper +
  remedy-类别裁决表，design D2；fixture round-1 重裁——生产码景观见
  design D0）：pre-guard 通道声明自己的 remedy 类别，由共享判据裁决"分
  类是否证明该 remedy 无关"；替换现有分散拒绝名单。
- 表现面 1：raw-manifest 双通道拒收 remedy-非因果类永久码（classifier
  deny：resource/configuration + policy/permission，至少
  `OUT_OF_MEMORY`）；**其余码（含 `SLURM_JOB_FAILED` 等 unknown-default
  与 `INVALID_MANIFEST` 类）保持开放、行为逐字不变**——几何本身（manifest
  实测缺失/修复晚于失败）即因果证据，生产 remedy 不退役（design D2/D3）。
- 表现面 2：`_downstream_failure_restartable` 黑名单删除，改按证据来源分
  域的 permanence 判据（design D4）：**真实记录码**且
  permanent/unknown-default/耗尽 → 拒 resume（`PARSE_FAILED` 记录形、
  `OUTPUT_INCOMPLETE` 等）；**合成占位码**（state 无 error_code 时 reader
  自造 `{STAGE}_FAILED`）不受 spec unknown-default 条款约束，维持现行
  为；记录瞬时码 resume 不变。受影响现绿测试共 10 条逐条重判（design
  D4b 表）。
- 表现面 3：`_failure_policy_payload` 顶层 `retryable` 覆写仅当分类本身可
  重试（瞬时码）时生效，不再能翻转永久码 permanence；`permanent: True`
  反向覆写保留（design D5）。
- 不动面：row-4 recompute 通道
  （`_missing_forecast_output_recompute_evidence`，按码门控是设计意图，含
  OOM 是显式裁决）与 model-package refresh 通道（已 permanence 门控 +
  #1161 拒绝臂）行为逐字不变；后者拒绝名单迁移到共享判据源（design D6）。
- 规格 delta：`job-retry-mechanism` ADD Requirement，把 pre-guard 通道必
  须咨询 permanence 判据、因果豁免类别、顶层键不可洗白钉进 spec。

## Impact

- Affected specs: `job-retry-mechanism`（ADDED Requirement "Pre-Guard
  Evidence Channels Consult Permanence"）。
- Affected code: `services/orchestrator/scheduler_state_failure.py`（共享
  判据 + 四通道接线）· `services/orchestrator/scheduler_state_decision.py`
  （无结构变更预期；若梯序注释需更新则随行）。
- Affected tests: `tests/test_production_scheduler.py`——受影响现绿测试
  10 条（round-1 语义模拟实测），逐条重判见 design D4b：2 条 copyback
  anchor 改断 guard + 新增瞬时码承重 anchor、2 条需求形命名测试按 spec
  胜改写保原主题、4 条 fixture 码换瞬时码保原主题、2 条保绿不动
  （compat + SLURM_JOB_FAILED 经通道 (b)）。PR body 记录逐条裁决理由。
- 平面边界：db-free 纯 Python，本地 pytest 即 oracle；不触 DB 平面
  （#1161 已对齐）、file-journal 平面（#1312 已交付）、手动重试路径、
  `auto_retry_skipped` 事件（#1314）。
