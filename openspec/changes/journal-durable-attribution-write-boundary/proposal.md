# Proposal: journal-durable-attribution-write-boundary

## Why

批 F 原定 4 个 issue（#1600 #1595 #1592 #1589）一单交付。**本 fixture 只收 #1592 + #1589**，
批 F 拆成 F-a/F-b 两单（拆分理由与另一半的边界见 design D0）。

两条都以 `services/orchestrator/file_orchestration_journal.py` 的 **cohort 投影写路径**为对象，
都属「durable 行被写进错误内容而无人报错」的静默污染类；且两者在同一段合并代码上**互相咬合**
（见下 #3），必须同 PR 裁定，分开修会造出比现状更差的结果。

1. **#1592 反洗白护栏只挂在一条写路径上**：`_strip_redaction_placeholders`
   （`:8804`）全仓只有一个调用点 `_append_validated_record_unlocked:6280`。cohort 投影的两条
   durable 写路径都不经过它——`project_forecast_cohort_tasks` 的 payloads 循环
   （`:3429` 直呼 `_journal_record_for_write`）与 `_defer_forecast_cohort_projection_unlocked`
   → `_write_pipeline_job_unlocked`（`:6154` 同样直呼）。于是调用方把一行**公共查询结果**
   round-trip 回写时，`[object-uri]` / `[uri]` 字面量被原样持久化进 journal record 与 direct row
   文件。该敞口是 `2026-08-20-accepted-submit-identity-write-semantics` 的 proposal 正文
   点名过的机理，并在其 Non-Goals 里显式推迟（"`log_uri` 的「sanitized 值复放即分歧」
   同源类别不在必修范围"）——本单即该推迟项的落地。
2. **#1589 终态粘性只护住 `status`，attribution 跟着漂**：`:3354-3358` 的
   `sticky_master_status` 把 `permanently_failed` 的 master 状态钉住，但紧接着 `:3373`
   `"error_code": projected_master_error_code` **无条件覆写**。结果是一行
   `status="permanently_failed"` 配着一个由本次派生投影算出来的 `error_code`——状态说
   "已被永久判死"，错误码说的却是这一次 reconcile 看到的派生结论，**自相矛盾的归因**，
   而且每一趟 resume/reconcile 都会再改一次。同族的 `error_message`（`:3381` 条件写）同病：粘性分支下它同样会被本趟派生值改写。
   对照组是 defer 腿 `:3539`：它对**任何** `TERMINAL_PIPELINE_STATUSES` 整行短路，一个字节不写。
3. **两者的咬合（本单的真正理由）**：`:3377-3383` 的证据字段覆写谓词是
   `if <value> is not None:`。若调用方 round-trip 一行公共结果，`log_uri` 进来是
   `"[object-uri]"`——**非 None**，谓词放行，把 durable 的真实 `s3://` URI 顶掉；随后
   #1592 的 strip 在写边界把它变成 `None`。**真实 URI 就此丢失，比现状（至少还留着字面量）更糟。**
   只修 #1592 会造出这个新洞；只修 #1589 则只在终态行上碰巧躲开。正解是让谓词看得懂占位符
   （design D3）。
   **咬合的真实射程（round-2 更正）**：不是两条腿，是**每一条既接受调用方证据又抵达
   durable 写的腿**；而且**被比较的值与被持久化的值必须是同一个值**——咽喉 strip 让
   durable 态变成已 strip 的，凡是拿**未 strip 的调用方入参**去和 durable 态做相等判定的闸门
   （`transition_pipeline_job_submit_evidence` / `transition_pipeline_job_runtime_status` /
   `complete_pipeline_job_cancellation`）**永远不可能收敛**：每一趟都判「变了」，每一趟再追加一条
   记录、再烧一个 sequence，直到撞上 `MAX_FILE_JOURNAL_CYCLE_SEGMENTS` /
   `MAX_FILE_JOURNAL_JSON_BYTES`；cancellation 那条更进一步答 `stale`，
   `chain_forecast_control.py:346-347` 于是 `continue`，**整个取消事件被丢掉**。
   可达性不再是「机理可达」而是**实证可达**：`query_pipeline_jobs_by_cycle:1126`
   返回 `_public_scheduler_row` 投影，`chain_forecast_control.py:256` 遍历它，
   `:344` 与 `:359` 两处都把 `log_uri=job.get("log_uri")` 喂回写路径。

## What Changes

- **#1592**：`_strip_redaction_placeholders` 下沉到 durable 写边界
  `_journal_record_for_write`（`:8014`）——与同族 sanitizer
  `_redact_durable_error_message_fields` **现有的双层布放完全同形**（它已同时在 `:6281`
  与 `:8023` 出现）。两条旁路（都只写 `pipeline_job`）都过该函数，job lane 持久态由此全覆盖。
  `:6280` 的原调用点**保留**（见 design D2）。
  **`pipeline_event` 走 carve-out**（实现期裁定 **D2b**）：咽喉 strip 跳过 event，因为
  `_append_validated_record_unlocked` 在 `:6284` 的公共渲染**有意产出**占位符作为 durable
  公共值，咽喉跑在它下游会把它抹成 `null`（实测打红
  `tests/test_file_orchestration_migration.py:1637`）。原则是「strip 只清除**从调用方来的**
  占位符，绝不跑在 journal 自己的公共渲染下游」；event lane 因此在 `:6280`
  的**调用方边界**受保护，job lane 在 record 构造点受保护。
- **#1589**：终态粘性从「只护 `status`」扩到「**归因族**随 `status` 一起粘」——粘性分支触发时
  `error_code` / `error_message` 保持 existing 值；`finished_at` / `exit_code` / `log_uri`
  这一族**观测事实**继续照常刷新（裁定与理由见 design D4）。触发条件仍是
  `permanently_failed`，**不**扩到全 `TERMINAL_PIPELINE_STATUSES`（design D5）。
- **咬合修复（round-2 重述，射程为全类）**：新增**唯一**的具名裁决点
  `_resolved_caller_evidence(value, *, durable=None)`——「占位符 = withheld，withheld 意味着
  **保留**」这条不变量只写一遍、只能在一处改。它被布放在**每一条**接受调用方证据的 durable 写腿上，
  且**跑在覆写谓词与相等闸门两者之前**：`upsert_pipeline_job` 的合并环、
  `commit_pipeline_job_submit_attempt`、`transition_pipeline_job_submit_evidence`、
  `transition_pipeline_job_runtime_status`、`complete_pipeline_job_cancellation`、
  `update_pipeline_job_status`、投影腿、defer 腿。逐腿普查与逐腿裁定见 design **D9**。
  无条件写的腿（cancellation / commit / upsert）额外传 `durable=`：那里没有谓词可以「拒绝覆写」，
  只 strip 会把真值抹成 `None`。
- 三项各配变异体证死（红-绿对照，design 变异表）。
- **报告不修、已另立 issue 跟踪**：`cancelled` cohort master 完全无 status 粘性（既存缺口，
  非本单引入，见 design D5）；`_mark_master_permanently_failed:7234` 把公共行的
  `error_message` 回灌 durable（同类 laundering 的另一入口，`[redacted]` 不在 strip 集合内）。

## Non-Goals

- **不改** `_PERSISTED_REDACTION_PLACEHOLDERS` 集合内容（`{"[object-uri]", "[uri]"}`），
  不动 `[local-path]` / `[redacted]` 的「有意持久化」语义，不动 pipeline_event 的公共 sanitization。
- **不改** `_defer_forecast_cohort_projection_unlocked:3539` 的整行短路语义（它已是正确形态）；
  该腿只改 `:3563-3568` 的三个条件覆写谓词。
- **不给 `update_pipeline_job_status` 加相等闸门**：它今天无条件追加一条记录，那是既有行为，
  本单只欠「值收敛」，不欠「不写」（design D9 裁定）。
- **不改** `request_pipeline_job_cancellation` 的 `reason → error_message` 无条件写：
  `reason` 是操作者现场文本、不是 round-trip 的 journal 证据，把占位符 reason 解析成
  「保留上一条不相干的 error_message」会配出一个更坏的谎（新 error_code + 旧 message），
  且该腿的 `cancellation_pending` 状态闸在写之前就答 idempotent，不存在收敛问题（design D9）。
- **不做**任何 migration / 历史行回填：已被洗白进 durable 的字面量占位符行保持原样。
- **不修** `_create_pending_manual_retry_job` 从**公共行**派生 retry_record 造成的
  `init_state_uri` 血缘退化（字面量 → 缺席）：那是**新建行**，没有可「保留」的 durable 前值，
  且该退化在本单之前就已存在（字面量本身即谎）。报告不修（design D9 secondary）。
- **不修** `_write_pipeline_job_unlocked` 的**返回值** `_public_scheduler_row(row)`
  （`:6183`）走的是未 strip 的 `row`——该值不是 durable 态，仅是给调用方的返回；作为
  **已声明的已知边界**记录（design D2 尾），不在本单修。
- **不碰** #1595（`in_write_window` ownership 失明）与 #1600（`open_file_no_follow`
  inode 身份 vs `os.replace` 竞态）——两者归 F-b，且 F-b 的 fixture 在本单合并后再定稿
  （行号会被本单挪动，必须按函数名重锚）。
- 不改 SQL 版 `chain_repository.py`（cohort 投影证据面目前 file-journal 独有）。

## Impact

- Affected specs: `pipeline-job-persistence`（ADDED 两条 Requirement：durable 写边界反洗白；
  终态归因族粘性）
- Affected code: `services/orchestrator/file_orchestration_journal.py`
  （`_journal_record_for_write`、新增 `_resolved_caller_evidence`、
  `project_forecast_cohort_tasks` 的合并段、`_defer_forecast_cohort_projection_unlocked`
  的条件覆写段，以及 round-2 补齐的 `upsert_pipeline_job` /
  `commit_pipeline_job_submit_attempt` / `transition_pipeline_job_submit_evidence` /
  `transition_pipeline_job_runtime_status` / `complete_pipeline_job_cancellation` /
  `update_pipeline_job_status` 六条腿）
- Affected tests: `tests/test_file_orchestration_journal.py`、`tests/test_gateway_reconcile.py`
- Closes #1592, closes #1589。
