# Design: slurm-error-code-transient-coverage

坐标系：master（#1407/PR #1501 merge 后）。关键既有事实：
`SLURM_STATE_MAP`（real_backend.py:126-141）终态失败形含
FAILED/TIMEOUT/NODE_FAIL/OUT_OF_MEMORY/PREEMPTED/DEADLINE；未映射态经
`_map_slurm_state`（:1447-1453）warning + 归 FAILED；`map_slurm_error_code`
（:144-152）四路 + 兜底，仅在 `state == FAILED` 时被调（:1426）；
`_normalize_slurm_state`（:187-189）把不可识别 raw 归一 `UNKNOWN`。
retry.py：`TRANSIENT_ERROR_CODES`（:27-39）/`NON_TRANSIENT_ERROR_CODES`
（:40-50）/`failure_classifier`（:194-228，尾部默认 `unknown_failure`）/
`is_retryable_failure` = `is_transient_error`。**瞬时性有第二个分类面**
（fixture review P1-1）：`scheduler_state_types.py:57-69`
`TRANSIENT_RETRY_REASON_CODES` 与 `TRANSIENT_ERROR_CODES` 逐项重复（11 码），
消费点 `scheduler_state_failure.py:305`（`_permanent_reason`——瞬时码预算
耗尽报 `retry_limit_exhausted`，否则 `permanent_failure_guard`）与 `:402`
（missing-forecast-output recompute 通道）；`job-retry-mechanism`
spec:176 已具名承认该双面结构。PR #1417 后 downstream-resume 判据在
`scheduler_state_failure.py:1315-1329`（`_downstream_failure_restartable`，
`return not failure.get("permanent")` 在 :1329），`code_recorded` 分域调用
点在 `:339-343`。

## D1 映射面：`map_slurm_error_code` + `SLURM_STATE_MAP`

新映射表（完整枚举，改后）：

| normalized state | error_code | 裁决 |
|---|---|---|
| TIMEOUT | SLURM_TIMEOUT | 既有，不动 |
| NODE_FAIL, PREEMPTED, **BOOT_FAIL** | NODE_FAILURE | BOOT_FAIL 新增——节点引导失败是基础设施故障，与 NODE_FAIL 同族同处置 |
| OUT_OF_MEMORY | OUT_OF_MEMORY | 既有，不动 |
| **DEADLINE** | **SLURM_DEADLINE**（新码） | 见下 |
| 其余（裸 FAILED、UNKNOWN、REVOKED 等） | SLURM_JOB_FAILED | 真·未知兜底，D2 |

- **`SLURM_DEADLINE` 为何是新码而不并入 `SLURM_TIMEOUT`**：TIMEOUT 是作业
  自身墙钟耗尽（处置=调大 time limit 或接受重试），DEADLINE 是
  `--deadline` 调度策略窗口关闭（处置=下一 cycle 窗口重排）。运维处置面
  不同，码分立；重试分类相同（都是瞬时基础设施/调度性故障）。
- **`BOOT_FAIL` 同步进 `SLURM_STATE_MAP` → `SlurmJobStatus.FAILED`**，
  两处后果（fixture review P1-2，具名裁决）：
  (a) 消除 `_map_slurm_state` 的 "Unmapped Slurm state" warning；
  (b) **`reconcile.py:1224`（`_file_cohort_task_projections`）行为翻转**：
  该处裸 `SLURM_STATE_MAP.get(normalized)` 无默认值，今天 BOOT_FAIL array
  task 落 `else` 分支（outcome 停 `unverified`、accounting 不完整）；映射
  后 outcome=`failed`、accounting 可判完整、`error_code=NODE_FAILURE`
  （:1241）并落 durable cohort 投影。**这是有意的正确化**——BOOT_FAIL 本就
  是 Slurm 终态（slurm_validation.py:73-85 `TERMINAL_SLURM_STATES` 早已
  枚举它），停在 unverified 才是缺陷形；须配 cohort 投影方向测试
  （tasks 2.1）。其余两个消费点（reconcile.py:728/:977）带
  `.get(..., FAILED)` 默认，行为不变。
- **`REVOKED`、`SPECIAL_EXIT` 具名不映**（后者 fixture review N2）：
  REVOKED 是 federation 专属状态（本部署不用）；SPECIAL_EXIT 同为
  `TERMINAL_SLURM_STATES` 已枚举但本控制面未观测的形。真出现即异常部署
  形，落真·未知（拒自动 resume、要人来看）是正确语义。写注释记录裁决，
  不写映射。
- **事实记录（fixture review N1）**：本仓 sbatch 模板无一处设置
  `--deadline`，当前提交路径产生不出 DEADLINE 终态——`SLURM_DEADLINE` 是
  防御/前瞻性映射（`SLURM_STATE_MAP` 早已列 DEADLINE，同一防御口径）；
  本次改动的**现实运维收益集中在 BOOT_FAIL**（节点引导失败真实可发生）。
  issue「以 node-22 实际 sacct 观测为准」按此落定：观测面没有 DEADLINE，
  裁决依据 Slurm 语义而非观测。
- **requeue 形态**：`REQUEUED` 已映 SUBMITTED（非终态、无 error code），
  无需改；requeue 后再失败呈现为裸 `FAILED`，与应用级失败不可区分——保持
  真·未知（见 Non-Goals，spec 显式写明）。
- raw state 照旧入 manifest `slurm_raw_state`（:1422-1423 不动）——码变粗
  不丢原始诊断。

## D2 分类面：`SLURM_DEADLINE` 入瞬时族（三面登记）；`SLURM_JOB_FAILED` 显式真·未知

- `SLURM_DEADLINE` **三面登记**（fixture review P1-1 推荐修法）：
  `retry.TRANSIENT_ERROR_CODES` + classifier `transient_slurm_runtime`
  分支集合 + **`scheduler_state_types.TRANSIENT_RETRY_REASON_CODES`**。
  据此 `is_retryable_failure("SLURM_DEADLINE") == True`、`classify_failure`
  `permanent=False`（限额内）、`_downstream_failure_restartable`
  （scheduler_state_failure.py:1315-1329）放行 downstream-resume，**且**
  预算耗尽时 `_permanent_reason`（:305）报 `retry_limit_exhausted` 而非
  `permanent_failure_guard`、missing-forecast-output recompute 通道（:402）
  认它——与 `SLURM_TIMEOUT`/`NODE_FAILURE` 同族同待遇，不造「半瞬时码」。
  瞬时病拿到瞬时码，不靠放宽 gate 补偿。
- `SLURM_JOB_FAILED` 显式裁决 = **真·未知，双不入**：
  - **不入** `TRANSIENT_ERROR_CODES`（放入会让应用级 FAILED 进自动重试
    自旋，烧预算并削弱 #1417 gate——issue 备选方案的否决理由）；
  - **不入** `NON_TRANSIENT_ERROR_CODES`（放入会把 auto_retry_skipped
    reason 从 `unknown_error_code_defaulted_non_transient` 变
    `non_transient_error`，丢掉「未知码需人工裁决」的审计信号，且 PR
    #1417 anchor 以它为 unknown 码——判别力依赖「双不入」现状）；
  - `failure_classifier` 增显式分支 `if code == "SLURM_JOB_FAILED":
    return "unknown_failure"`（置于尾部默认之前）——**行为等价、语义显式**：
    裁决从「恰好落 default」变「有人签过字的 default」，防未来分支扩张时
    被顺手收编；注释点名本 change 与 resume 后果。
  - `warn_unknown_error_code` 的 "add to classification list" 文案对该码
    语义略偏（它是有意未知，不是待补分类）——**接受不改**：警告把运维引到
    分类表，表内注释即裁决记录；改文案要为一个码特判，得不偿失。
- **码归属一致性不变式**（AC2，按 P1-1 重写）：瞬时性有两个分类面
  （`retry.TRANSIENT_ERROR_CODES` 与 `scheduler_state_types.
  TRANSIENT_RETRY_REASON_CODES`），**每个瞬时码必须同时在两面**——两集合
  相等钉测（今天逐项重复，钉相等使未来分歧成为有意编辑）；
  `SLURM_TIMEOUT`/`NODE_FAILURE`/`SLURM_DEADLINE` ∈ 双瞬时面，
  `OUT_OF_MEMORY` ∈ NON_TRANSIENT（且双瞬时面缺席，job-retry-mechanism
  spec:176 既有钉），`SLURM_JOB_FAILED` ∈ 显式全不入；
  `TRANSIENT ∩ NON_TRANSIENT = ∅` 全域钉测。

## D3 规格面：双 spec delta

**`real-slurm-gateway-contract` MODIFIED「Retryable Slurm errors are stable」**：

- requirement 正文保留原主语 `RealSlurmGateway`（capability 施动者），在其
  后补分类显式契约句。
- 新增场景「Deadline termination becomes retryable」：sacct 报 DEADLINE ⇒
  `error_code=SLURM_DEADLINE` + RetryService 视为可重试（限额内）。
- 「Node failure becomes retryable」场景 WHEN 扩为 `NODE_FAIL`/`PREEMPTED`/
  `BOOT_FAIL`。
- 「Unknown terminal failure preserves raw state」场景补重试分类契约：
  未知终态码非瞬时、自动 downstream resume 拒绝、需操作员裁决后人工
  retry；raw state 保留供裁决。resume 拒绝的契约本体归
  `job-retry-mechanism`（spec:1274-1279 既有场景）所有，本场景措辞用
  引用式（"as the job-retry-mechanism unknown-code default prescribes"）
  避免双载体漂移（fixture review P2-5）。
- 有意收紧一处并记录：原文 "a stable generic error code **such as**
  `SLURM_JOB_FAILED`" 改为确指 "the stable generic error code
  `SLURM_JOB_FAILED`"——码是既有实现事实，示例式措辞才是漂移源。
- 其余场景（TIMEOUT/OOM/poll timeout）原文不动。

**`job-retry-mechanism` MODIFIED「Retry Guard — Non-Transient Error
Exclusion」**（fixture review P2-5）：「Transient error codes allow
auto-retry」场景的显式瞬时清单加一行 `SLURM_DEADLINE`（含语义注释），其余
场景逐字照抄不动——该清单是分类契约的权威载体，gateway spec 只写映射。

## D4 受影响面核对（全部「不改」，裁决记录）

| 位置 | 形 | 裁决 |
|---|---|---|
| slurm_validation.py:1571 | array task record 缺 error_code 时回退 | 不改——缺码 = 未知，与 D2 真·未知语义一致 |
| slurm_validation.py:1616 | 合成 evidence 样本字面量 | 不改——静态样本数据，非行为 |
| slurm_validation.py:1712 | retry-cancel evidence 无 failed task 时回退 | 不改——同 :1571 语义 |
| file_orchestration_journal.py:3142 | projection 缺码回退 | 不改——同上；改它才是行为漂移 |
| reconcile.py:1040/:1074/:1241 · slurm_validation.py:1135 · real_backend.py:1426 | `map_slurm_error_code` 调用点 | 自动获得新映射，无本地硬写，无需改 |

## D5 测试面与 anchor 保全

- `tests/test_real_slurm_gateway.py`：`map_slurm_error_code` 逐格
  （DEADLINE→SLURM_DEADLINE、BOOT_FAIL→NODE_FAILURE、REVOKED/SPECIAL_EXIT/
  裸 FAILED/垃圾串→SLURM_JOB_FAILED）——纯函数直测**并存**于既有
  sacct-fake 端到端风格，不替换（fixture review N3）；
  `_record_from_sacct_fields` 层 DEADLINE 终态端到端（error_code +
  `slurm_raw_state` 保留）；BOOT_FAIL 经 `_map_slurm_state` 无 "Unmapped"
  warning。**既有断言更新两处**（fixture review P2-3）：:967 参数化
  `("BOOT_FAIL","SLURM_JOB_FAILED")` 改 `NODE_FAILURE`；
  `test_unknown_terminal_produces_slurm_job_failed_error_code`
  （:1008-1009）的代表态从 BOOT_FAIL 换 REVOKED（D1 具名不映，正好当
  未知场景见证人）。
- `tests/test_retry.py` + `tests/test_real_slurm_gateway.py`：
  `SLURM_DEADLINE` 瞬时 + classifier `transient_slurm_runtime` + 预算耗尽
  `_permanent_reason == "retry_limit_exhausted"`；「码归属对齐」**扩充既有**
  `test_slurm_error_codes_align_with_retry_sets`
  （test_real_slurm_gateway.py:1029-1035）而非新写；`SLURM_JOB_FAILED`
  classifier 显式分支 + 全不入钉测；`TRANSIENT ∩ NON_TRANSIENT = ∅` +
  **两瞬时面相等钉测**（`TRANSIENT_ERROR_CODES ==
  TRANSIENT_RETRY_REASON_CODES`）。
- **resume 两方向主锚（AC4，红-绿，seam 具名）**：
  `test_downstream_resume_keeps_recorded_transient_codes`
  （test_production_scheduler.py:22633，参数化 SLURM_TIMEOUT/NODE_FAILURE/
  PREEMPTED，`_durable_downstream_failure_state` 注入）**加一格
  `SLURM_DEADLINE`**——接线前必红（未知码 → permanent → action="blocked"）；
  反方向回归钉现成：`test_downstream_resume_refuses_recorded_non_
  transient_codes`（:22600-22628，参数化已含 SLURM_JOB_FAILED）保绿。
- **anchor 保全（判别力重定位，fixture review 4）**：真正靠「全不入」活着
  的主 anchor 是 :22600-22628 的 refuses 参数化 +
  `test_unlisted_production_error_codes_default_to_the_unknown_reason_
  and_warn`（tests/test_retry.py:352-383，`_UNLISTED_PRODUCTION_ERROR_
  CODES` 含 SLURM_JOB_FAILED）；
  `test_repaired_raw_manifest_allows_stale_downstream_failure_retry`
  （test_production_scheduler.py:22135）零改动附带保绿（它测 raw-manifest
  修复通道，对本裁决判别力较弱）。

## Invariant Matrix（pin 的行为）

| # | 面 | 不变式 | 锚 |
|---|---|---|---|
| I1 | gateway | DEADLINE ⇒ SLURM_DEADLINE；BOOT_FAIL ⇒ NODE_FAILURE；raw state 保留 | tasks 2.1 |
| I2 | retry | SLURM_DEADLINE 三面登记；限额内 permanent=False；预算耗尽 reason=retry_limit_exhausted | tasks 2.2 |
| I3 | resume | SLURM_DEADLINE ⇒ downstream-resume 放行（红-绿主锚，:22633 参数化加格） | tasks 2.3 |
| I4 | resume | SLURM_JOB_FAILED ⇒ 拒绝 resume（:22600 refuses 参数化）；:22135 附带保绿 | tasks 2.3/2.5 |
| I5 | retry | SLURM_JOB_FAILED 全不入 + classifier 显式 unknown_failure；集合互斥 + 两瞬时面相等 | tasks 2.2 |
| I6 | 全域 | 既有映射（TIMEOUT/NODE_FAIL/PREEMPTED/OOM）与 #1417 gate 逐位不变 | tasks 2.4/2.5 |
| I7 | reconcile | BOOT_FAIL cohort 投影 unverified→failed + error_code=NODE_FAILURE（具名正确化） | tasks 2.1 |
