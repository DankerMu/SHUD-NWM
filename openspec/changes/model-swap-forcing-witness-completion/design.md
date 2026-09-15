# Design — model-swap forcing witness completion

## Risk triage

- **Fixture level: `expanded`**（round 1 三个 reviewer 席位）。首版把四个 issue 定为 `high`；#1845 降范围后，
  剩下两处代码改动：
  - 一处是调度决策面的单点包裹，复用 #1843 已加固的见证 helper；
  - 一处是链路 retry 序号的单函数解析改动。

  两处都会影响真实 Slurm 提交，但各自只有一个 seam，与 precedent `enforce-per-model-forcing-completeness`
  （`expanded`）同级。
  **与 issue 建议级别的偏离**：三个 issue 都没有 `Suggested fixture level` 字段，此处是首次定级。
- **Risk packs selected**：
  - `correctness`：决策与序号的等价性与边界。
  - `invariant-state`：#1843 不变量的覆盖完整性、breaker 反复活形状、retry 身份单源。
  - `test-evidence`：被修改的钉住测试、public-seam 红证。
- **Risk packs not selected**：
  - `security-perf`：无鉴权面、无外部输入面、无新增 I/O。见证 helper 在本车道新增一次对象存储探针，
    只在 quarantine 触发时发生。
  - `integration`：reservation / gateway / producer 均不改。#2254 的公开入口测试已经覆盖到 gateway 边界。

## 行号勘误

issue 引用的行号取自 `92fb2dbe`。以下是当前 HEAD `36266332b` 的位置：

| issue 引用 | 当前位置 | 是什么 |
|---|---|---|
| #1844 `scheduler_candidates.py:613` | `:613-624` | quarantine 调用点（替换 skip 决策） |
| #1844 `:2352-2388` | `:2304-2402` | `_journal_predecessor_identity_quarantine` |
| #1844 `:2365` / `:2392` | `:2405-2429` / `:2432-2475` | retry / breaker evidence |
| #1844 `:641` | `:638-650` | 既有见证闸（仅 `strict_warm_start is not None`） |
| #1844 `tests/test_production_scheduler.py:11065` | `:11125` | 钉住的 E2E 测试 |
| #1846 `:1609` / `:1785` | `:1616-1636` / `:1703-1827` | stable blocker 谓词 / 运维授权修复 |

## D1 — #1844：见证闸只接在未设防的那条车道

`:613` 的 quarantine 在两条车道上可达：

1. `strict_warm_start is None`，reason ∈ `_JOURNAL_IDENTITY_QUARANTINE_SKIP_REASONS`（`:86-88`）。
   **未设防**：后续的 `:638` 闸因 `strict_warm_start is None` 被跳过。
2. `strict_warm_start is not None` 且 reason == `terminal_completed_cycle`。该 reason 不在
   `_STRICT_WARM_START_TERMINAL_SKIP_REASONS`（`:74`）里，于是 fall through 到 `:613`，
   随后**已经**会走到 `:650` 的见证闸。

**决定**：在 `:613` 调用点、`state_decision = identity_quarantine` 之后，只在 `strict_warm_start is None`
时调用 `_strict_warm_start_forcing_witness_decision(candidate, raw_candidate_state, state_decision)`，
车道 2 不重复探测。

备选方案是在 helper 内部无条件包裹。它是正确的，但车道 2 的对象存储会被探两次，helper 签名也要多加
`raw_candidate_state`。因此拒绝。

**breaker 臂不加代码**：
- `_strict_warm_start_forcing_witness_decision` 对非 `retry` 决策是 no-op（`:2242-2243`）。
- breaker 出口本就是 `blocked`，不提交任何作业。
- 其 reason `blocked_journal_predecessor_identity_quarantine` 不在两份 forced-resubmit 白名单里
  （`chain_forced_resubmit.py:14-28`、`chain_runtime_utils.py:200-211`；已由
  `tests/test_warm_start_chaining.py` 钉住）。

所以「breaker 与见证缺席同时成立时哪个 reason 胜出」不成问题：breaker 先返回 `blocked`，见证闸对它 no-op。

**`manual_retry_requested` 不受管**：这是由**位置**保证的，不需要额外检查。
- `:613` 位于 `state_decision.action == "skip"` 分支内（`:530-534`），而 `manual_retry_requested`
  恒为 `retry`。
- 三个 quarantine skip reason 在 `scheduler_state_decision.py:232-281` 都先于 `:283` 返回。

本批仍写一条负向测试钉住这一结构排斥（#1844 验收 4）。

**可排空性（fixture review P1-1 修正）**：新落出的 `blocked` 满足 `_decision_is_stable_missing_forcing_blocker`
（`:1616`），因为 helper 原样返回 #1843 的 blocker。但**精确 cycle 修复通道在本车道不可达**
（读码确认，未执行）：

- `:655` 包在 `if strict_warm_start is not None:`（`:638`）内。
- `:1680` 的调用点 `:489`/`:500` 在 `:613` quarantine 之前执行，`:744`/`:1048` 位于 `active_slurm_job`
  resync 分支内，都拿不到 quarantine 决策；
  而 `:1723` 要求 `action == "blocked"`，所以原样返回。
- 即便补一次调用，`:1779` → `_verified_repair_warm_state(candidate, None)` 在 `:1884-1885` 返回
  `warm_state_missing`，产出 `missing_forcing_repair.status=rejected`，拿不到 `restart_stage: "forcing"`。
  修复策略的语义在 must-preserve 里，不能为此改动。

**决定**：
- 本车道的 blocker 只经 `scripts/node22_backfill_forcing_for_model_ids.py` 排空。
- 精确 cycle 修复通道明确限定在 strict warm-start 车道。
- 测试钉住本车道 blocker 满足 stable 谓词。
- runbook（tasks 3.1）按车道分别写排空通道。
- **不**补修复策略调用。

## D2 — #1844：钉住的 E2E 测试就是 bug 本身（对 issue 验收前提的偏离）

issue 验收 3 要求 `test_completed_forecast_cycle_stale_journal_identity_is_quarantined_end_to_end`
（`tests/test_production_scheduler.py:11125`）「保持绿」，并称它覆盖「**有** forcing 前提下」的 retry 路径。
**这一描述与 fixture 不符**（读码确认，未执行）：

- 它的 `active_repository` state（`:11181-11205`）没有 `forcing_package_uri`。
- 它不设 `OBJECT_STORE_ROOT`；本文件唯一的 autouse fixture（`:430-442`）只设 `SHUD_EXECUTABLE`。
- 见证链因此是：`_missing_upstream_forecast_artifact_evidence` → `_forcing_sidecar_provenance` →
  `store_unconfigured` → `forcing_version_row_absent`。

该测试断言 `submitted_count == 1` 的，恰恰是一次**无 forcing 见证**的 forecast retry，也就是 #1844 要封的
那次发射。按字面「保持绿」，就等于不修 #1844。

**决定**：修改该测试的 fixture，为候选种入本模型的 forcing 包。配方须**同时**做到（fixture review P3-1）：

1. 用 `_seed_recorded_forcing_packages`（`:130`，用法见 `:159`、`:46984`、`:48595`）写出包。
2. 在 state 里加一个绑定本模型 `<basin_version_id>/<model_id>` 的 `forcing_package_uri`
   （参照 `:46970-47000`）。

3. 从该测试 state 的 `copyback_evidence` 中删掉 `copyback_source_uri`（stage/status/身份字段保留）。
   见证 helper 过了 forcing 腿之后还会走 copyback 腿（`scheduler_state_failure.py:797-903`）；
   配置 `OBJECT_STORE_ROOT` 后，那个目录形的 `s3://…/output/` uri 不可探，结果是 `blocked / missing_copyback_source`。
   删掉之后 `_copyback_source_required` 对该 state 返回 False，`terminal_completed_cycle` 仍成立
   （fixture review 第二轮在 scratch 中直接调用 helper 实测，未经 pytest）。

只做第 1 步时，见证会走 sidecar 层，结果仍是 `sidecar_absent` → blocked。测试需要 `monkeypatch`
设置 `OBJECT_STORE_ROOT`，且 `_seed_recorded_forcing_packages` 必须在构造 `ProductionScheduler` 之前调用。

**附带契约**：本车道接入的 helper 是完整的上游产物守卫，除两个 forcing blocker 外，也可能落出
`missing_copyback_source` / `copyback_source_withheld`。仓内没有生产代码写这些 `copyback_*` 键
（grep 为空，推断），生产影响有限，但 spec 如实写入。

`submitted_count == 1` 与 quarantine retry 形状的断言保持不变。另新增一个红孪生：同一 state、不种 forcing，
断言落入具名 `blocked` 且 `submitted_count == 0`。

这是**对 issue 验收前提的偏离**，写入 PR 偏离记录。它不是弱化 oracle：原断言的语义（有 forcing 时隔离 retry
照常提交）被完整保留，只是补上了原本缺失的前提。

## D3 — #1846 Decision：保持 `blocked` + 人工排空

**裁决（用户已定夺，本节是书面落档）**：不引入无人值守车道自动发射 `restart_stage: "forcing"`。
自己没有 forcing 的重铸身份模型，稳定落在 `blocked`（`missing_forcing_package_uri` /
`forcing_version_row_absent`），由运维排空。

逐条回应 #1843 的三条否决理由：

1. **「等于把重跑生产 forcing 的权限交给无人值守车道」——维持否决。**
   - 现有的运维授权修复通道（`_apply_explicit_missing_forcing_repair_policy`，`:1703-1827`）受两道把关：
     `_is_explicit_missing_forcing_repair_target`（运维显式指定 cycle）与 `_decision_is_stable_missing_forcing_blocker`。
     这正是「授权由人给出」的形状。
   - 自动重入要么复制这条通道并去掉人工那道闸，要么绕开它。两者都会把一个跨 cycle 的生产决策下放给调度循环，
     换来的只是省掉一次人工命令。
2. **「raw 被裁剪时，降级完全依赖 `canonical_readiness` 门扛住」——维持否决。本批勘察加强了这一条。**
   - 链路侧唯一的「raw 在场」探针 `cycle_download_success_missing_raw_manifest`（`chain_forecast_cycle.py:507-520`）
     守的是 `stage.stage == "download"`。
   - 当前 `ForecastOrchestrator.stages`（`M3_STAGES`，`chain_stages.py:14-63`）与 `LEGACY_FORECAST_STAGES`
     都没有 `download` stage，所以**该探针在调用点不可达**（读码确认，未执行）。
   - 结论：自动重入在 raw 已裁剪时，确实没有链路侧的第二道防线。
3. **「blast radius 与 #1843 不同」——维持。** 本批 #1845 的核查进一步说明，即便只在链路执行侧做一次性重投，
   也会带来 cohort 级停摆这类新的 blast radius（见「Descoped: #1845」）。自动重入属于更大一圈的决策，
   不在本批。

**被接受的成本**：每次模型换代都必须跑 `scripts/node22_backfill_forcing_for_model_ids.py`。漏跑的代价是一批候选
长期停在具名、可见的 `blocked`，而不是静默烧毁。这项成本写进 runbook（tasks 3.1），并在 #1826 下留言定性为
已接受（tasks 3.2）。

**被否决的中间态**（issue 备选：聚合排空清单/告警）：不在本批范围，不做。它能降低「得有人记得」的成本，
但不改变裁决。

## D4 — #2254：最后一个 suffix 为准，只改解析

**公开入口复现**（诊断任务；scratch 测试已删除）：
- 内存 SQLite 仓库中有 `B`、`B_retry_1`、`B_retry_1_retry_2`、`B_retry_1_retry_2_retry_3`，均由真实
  `RetryService.handle_failed_job` 写出。
- 证据为自动的 `retry_missing_forecast_output`，`context.retry_attempt=None`。
- 结果：`orchestrate_cycle` 铸出 `B_retry_2`；reserve 收到的 idempotency key 为 `…:forecast:retry_2`；
  gateway comment 为 `nhms_idem:…:forecast:retry_2`。

**契约证据（可执行）**：`retry_identity.retry_suffix_attempt("B_retry_1_retry_2_retry_3") == 3`，
`split_retry_job_identity(...) == ("B_retry_1_retry_2", 3)`。

**期望值的形状要分开说**：

- **序号 4**：由上述契约，以及 `chain_forecast_orchestrator_cycle.py:262-264` 的
  `max(retry_count, suffix_attempt, …)+1` 共同支持。
- **扁平 id `B_retry_4`**：这是链路自己的铸造规则（`_pipeline_retry_job_id(base, n)`）套用序号 4 的结果。
  `retry_identity._next_current_master_retry_identity` 给出的则是叠加形 `B_retry_1_retry_2_retry_4`，
  那是 FileJournal accepted-submit master 的另一条铸造规则。**本批不统一两种 id 形状**：链路侧 base 永远是
  `_pipeline_job_id(run_id, stage)`，扁平是它既有的约定，改形状会动 reservation key 的可见格式。

**决定**：
- `_next_retry_attempt_for_stage` 保持前缀过滤 `job_id.startswith(base + "_retry_")`。
  `_job_matches_stage` 是模型盲的，去掉前缀会把兄弟行的序号拉进来。
- 逐行序号改为 `retry_suffix_attempt(job_id)`，序号为 0 的行不计入；其余 `max(..., default=0) + 1` 不变。
- **只看 suffix，不看 `retry_count` 列**，即不用 `effective_retry_attempt`。malformed tail（如 `B_retry_garbage`）
  必须继续被跳过（issue 验收 3），而 `effective_retry_attempt` 会让该行的 `retry_count` 把它捞回来。
  `retry_suffix_attempt` 对无法解析的尾返回 0。
- `context.retry_attempt` 的优先级（`chain_forecast_orchestrator_cycle.py:191` 的 `or`）不动；PR #2253 的
  「同一 `existing_jobs` 快照」不动。
- 同一快照里的扁平 `B_retry_1`、`B_retry_2`：max+1 与原行为逐字相同，不会冲突。
- fixture review 核对过：`tests/test_orchestration_chain.py:15977` 用的是扁平 id，结果不变；没有既有测试断言
  旧的「跳过叠加 id」行为。

**证据链上尚未执行的一环**：复现里 `retry_missing_forecast_output` 的证据是手工构造的，没有让它的生产者
（`scheduler_state_failure.py:534-585`）从真实 state 推出来。本批 red 测试同样用构造证据驱动公开入口，
标注为「构造证据、公开入口」。

## Descoped: #1845

用户裁决降为 follow-up，#1845 保持 open。核查结论写回 issue（tasks 4.1），供后续设计：

- **对称先例不可达**：`cycle_download_success_missing_raw_manifest` 在 M3/LEGACY stage 列表下都不会被调用（见 D3.2）。
- **不是镜像而是循环**：unscoped 分支正是多模型 cohort。`_cycle_pipeline_job_model_id` 在
  `len(all_basins) != 1` 时返回 `None`（`chain_runtime_utils.py:69-72`），必须逐 basin 探测。
- **key 派生的大小写陷阱**：必须是 `normalize_source_id(src).lower()`（`scheduler_state_failure.py:1022`）。
  下游 `workers/shud_runtime/runtime.py` 只读 primary `OBJECT_STORE_ROOT`、没有 copyback 回退，
  所以链路侧见证应只探 primary。
- **强制重投没有上限**：`_retry_cycle_stage_job_id` 路径不受 `RetryConfig.max_retries` 约束，照抄会每 pass 重投一次。
- **整 stage fail-closed 没有干净出口**：
  - 走 `failed` 会经 `_schedule_cycle_stage_retry` → `retry.py:513-527`，把已成功的 forcing 行改写成 `permanently_failed`；
  - 用其他 status 会被覆盖为 `UNRECOGNIZED_STAGE_STATUS`；
  - 不写持久失败，调度侧也就不会分类出具名 `blocked`；
  - 一个缺 forcing 的模型会让整个 cohort 每个 pass 停在 forcing，直到人工回补。
- **复用部分阵列失败机制结构上不可行**：resume 路径的聚合按 `task_id` 位置，把被匹配 job（可能属于兄弟模型）的
  Slurm 账目套到当前 `context.active_basins` 上（`chain_array_evidence.py:325-343`、
  `chain_array_accounting.py:282-299/687-712`）。任务数相同就是误归属的假成功，不同就抛
  `SLURM_ARRAY_ACCOUNTING_INCOMPLETE`。这本身也是 #1845 危害面的一部分。
- **既有 unscoped resume 用例**（如 `tests/test_orchestration_chain.py:6201`，以及 `:6279/6364/6445/6522`
  可能受影响）没有种 manifest，任何见证落地都要一次前提补齐 sweep。

## Must-preserve behavior

- 本模型 forcing 包在场时，quarantine retry 的决策、reason、`restart_stage: "forecast"`、提交数不变
  （仅当某一 provenance 层自报来源时 evidence 才多出 `forcing_provenance`，例如 journal 层 `forcing_version_source=journal`）。
- breaker 臂的 `blocked` 与其反复活形状不变；两份 forced-resubmit 白名单成员不变。
- `manual_retry_requested` 的抢先语义不变。
- `_decision_is_stable_missing_forcing_blocker` 与 `_apply_explicit_missing_forcing_repair_policy` 的既有语义不变。
- retry 序号：扁平 suffix max+1、malformed tail 跳过、`context.retry_attempt` 优先、snapshot-authority 均不变。
- 两条自动 producer 的 id 形状与状态行为不变：DB（`retry.py:465`）与 FileJournal
  （`file_orchestration_journal.py:10708-10730`）。

## Seams under test

- `build_candidates`（`scheduler_candidates.py`，#1844 单元级），以及 production scheduler 的 E2E harness
  （`tests/test_production_scheduler.py`）
- `ForecastOrchestrator.orchestrate_cycle`（#2254 公开入口）
- `retry_identity.retry_suffix_attempt`（契约面）

## 越界发现（只报不修）

- `cycle_download_success_missing_raw_manifest` 在当前 stage 列表下不可达（tasks 4.2 立 follow-up）。
- #2393：下游 stage 继承上游 reservation 写回的 `context.retry_attempt`，第二轮 recovery 以
  `skipped_duplicate_submission` 撞上既有 `_retry_1` 行。#2254 修复后该问题仍在，已立单。
