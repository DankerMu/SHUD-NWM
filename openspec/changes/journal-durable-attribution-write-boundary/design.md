# Design: journal-durable-attribution-write-boundary

> **坐标基线**：本文件所有代码行号引用为**改动前坐标**（base `66e1c5e5`），实现后会漂移。
> 阅读与复算变异时**按名索引而非行号**——issue #1592/#1589 原文锚在 `c2439f62` / `8a26fe6e`，
> 行号已全部作废，本文件的坐标是在 `66e1c5e5` 上逐条重锚过的。
>
> **fixture review P3-1 的差-1 漂移已修正**：`:6182`→`:6204`（direct 落盘）、
> `:6184`→`:6183`（`_public_scheduler_row`）、`_append_validated_record_unlocked`
> 调用点 `:6287`→`:6286`（def 在 `:6269`）、`sticky_master_status` `:3355`→`:3354`、
> 既有注释 `:3346`→`:3347`。评审核对的其余坐标全部无误。

## 风险三角（fixture level: expanded）

- **风险**：journal 行是 scheduler 判定的真值源。两条都在**写入证据**侧，都是「写进去的内容是错的
  而没有任何人报错」的静默污染：#1592 把展示用占位符洗白成 durable 值，#1589 让终态行的归因
  自相矛盾且每趟 reconcile 再漂一次。二者叠加还能**丢失** durable 真值（见 D3）。
- **可控性**：db-free 纯文件态逻辑，本地 pytest 全闭环；改动集中在单文件两个窄区块，无并发面、
  无远端依赖、无 migration。
- **不确定性**：中——#1589 的**粘性边界**（哪些字段跟着 status 粘）是一次语义裁定，不是找 bug；
  裁错会把不变量钉反。D4/D5 各给理由、被拒方案与可证伪的钉。

### 可达性账（保持诚实）

- **#1592 是活体可达的**：两条旁路都是生产路径（cohort 终态投影 / defer 腿），只要调用方
  round-trip 一行公共查询结果就触发。`2026-08-20-accepted-submit-identity-write-semantics`
  的 proposal 已实证 `_record_manual_retry_submission_success`（`:7865` 旧坐标）**确实携带**
  占位符化的整行做 round-trip。
  **round-2 更正：初版这里那句「迄今仓内没有实测到一条把占位符喂进 `log_uri` 参数的活调用方」
  是假的，已删除，不得再出现。** 实证链：`query_pipeline_jobs_by_cycle:1126` 逐行返回
  `_public_scheduler_row` 投影（`log_uri` 被渲染成 `[object-uri]`）；
  `chain_forecast_control.py:256` 遍历这些投影；`:344`（`complete_pipeline_job_cancellation`）
  与 `:359`（`update_pipeline_job_status`）**两处都**把 `log_uri=job.get("log_uri")`
  直接喂回写路径。仓内早就知道这件事：`chain_stage_execution.py:905-915` 的注释写明
  调用方快照可能携带一个「日志其实发布过」的 sanitized `[object-uri]`，而 `None` 才是唯一诚实的
  "never published"。可达性档位因此从「机理可达」升为**实证可达**。
- **#1589 是活体可达的**：`permanently_failed` 由 #1312 的终态标记写入，之后
  **"durable 投影不全 + 本趟带 `complete=True`"这一种几何**走 `project_forecast_cohort_tasks`
  会覆写 `error_code`（**不是"任一趟"**——resume/reconcile 两条腿各有闸门，
  精确的可达性论证见下 P3-3 段）。#1410（PR #1591，
  2026-08-19 已合）修好 resume 腿的 master id 传参后，该路径**从恒判身份不匹配的静默零写入
  转为真正会写**——即 #1589 原文说的「#1410 合入后转生产可达」。**前置状态已核实：
  #1410 CLOSED / PR #1591 MERGED @2026-08-19T10:45:16Z。**

## D0：批 F 拆成 F-a / F-b 的 sizing 裁定（须记录，不得静默）

用户确认的批计划把 F 列为**一个**单元（#1600 #1595 #1592 #1589）。本单把它拆成两半，属
「可以合并的合并处理」授权内的编排裁量，但按纪律显式记录：

| | 内容 | 理由 |
|---|---|---|
| **F-a（本单）** | #1592 + #1589 | 同一段合并代码，且**互相咬合**（D3）——分开修会造出比现状更差的结果，必须同 PR |
| **F-b（后续）** | #1595 + #1600 | 都是 journal 读路径**并发**语义，共用「两线程 + 单 repository + barrier」测试机械；两 issue 各自声明「不碰同一行、可并行」 |

F-a 先行的额外机械理由：本单会挪动 8700+ 行文件的行号，F-b 的 fixture 必须在本单合并后按
函数名重锚再定稿。

## D1（#1592）：下沉到 `_journal_record_for_write`，而不是给每条旁路各打一个补丁

`_journal_record_for_write`（`:8014`）是**journal record 的唯一构造点**——措辞收紧
（fixture review P2-2：初版写"全部 durable 写入的咽喉"，字面为假，见下"绕过咽喉但无害"）。
它有 **6 个**调用点，全部在改动后受 strip 约束：

| 写路径 | payload 来源 |
|---|---|
| `_append_validated_record_unlocked:6286` | **event lane 例外，见 D2b**——该调用点在 `:6284` 的公共渲染**之后**，不适用咽喉 strip |
| `project_forecast_cohort_tasks` payloads 循环 `:3429` | **旁路 A** |
| `_write_pipeline_job_unlocked:6154` | **旁路 B**（含 defer 腿；本身有 16 个调用方） |
| `:2601` submission_failed 拒收 | event details，评审逐条读过，无 URI 类字段 |
| `:2706` `mark_pipeline_job_permanently_failed` | **round-2 更正：该调用点是 `payloads` 循环，喂两条 payload**——`payloads[0]`（`:2660-2672`）是**整行 master pipeline_job**，`payloads[1]` 才是 event。初版只审了后者。前者受咽喉覆盖正是本单要的；后者的 `event_details` 来自 `:7236-7245`，全是计数/分类 |
| `:2931` reservation_lost / absence | 同上，无 URI 类字段 |

后三条与 16 个 `_write_pipeline_job_unlocked` 调用方**不各配用例**——全覆盖正是咽喉的意义所在；
上表的 payload 审计就是它们的证据。

**充分性已实测查实**：`_write_pipeline_job_unlocked` 里除 `record` 之外还有 `row` 的下游——
`_sync_reconcile_inventory_for_row_unlocked(row)`（调用点 `:6173` 与 `:6205` 两次；
`:5718` 是该函数**内部**的 `_atomic_write_json_unlocked`，不是调用点——第 2 轮 P3-D 更正）、
`:6193` 的 direct 路径选择、`return _public_scheduler_row(row)`。
inventory（`:5711-5728`）只写 `schema_version / job_id / source_id / cycle_time / row_kind`
五个身份字段，路径选择同样只用身份字段，**均不携带任何可被占位符化的字段**；
`_write_pipeline_job_direct_unlocked` 落盘的是 `record`（`:6204`），同样被覆盖。
返回值不是 durable 态 → 见 D2 尾的已知边界。

**绕过咽喉但无害的 durable 文件**（评审枚举 `_atomic_write_json_unlocked` 全部调用点）：
`:4759/:4848/:4858/:4919/:4930`（迁移 marker / 回滚 receipt）、`:5061/:5069/:5098`
（fence / marker / rollforward）内容全部本地构造，不含调用方证据；`:5718` 见上。
唯一含调用方证据的是 `:6730` 的 `latest/` 物化产物——它由 `_cycle_rows` 从 journal / direct row
**重放派生**（`:6694-6737`），两个输入都已被咽喉覆盖，故 `latest/` **间接**成立。
该论证初版未写出，此处补上（P2-2）。

**被拒方案**：在两条旁路各自调用处插 strip。拒因是它把「durable 写必反洗白」这条不变量
散成 N 份拷贝，下一条新写路径照样会漏——正是本 issue 的成因本身。

## D2（#1592）：`:6280` 的原调用点**保留**，不删

> **D2 的幂等论断只对 `pipeline_job` 成立**——实现期实测推翻了它对 `pipeline_event` 的适用性，
> 更正见 **D2b**。以下段落的"双层无语义差"仅在 `pipeline_job` lane 内有效。

同族 sanitizer `_redact_durable_error_message_fields` 今天就是**双层布放**：
`_append_validated_record_unlocked:6281` 与 `_journal_record_for_write:8023` 各一次
（外加 `_write_pipeline_job_unlocked:6147`）。strip 是幂等的（占位符 → `None`；`None` 保持 `None`；
其余原样），双层不产生任何语义差。照抄现有先例 = 零新概念、零判别力损失。

**被拒方案**：删 `:6280` 求"唯一真源"。拒因：(a) 与同族 sanitizer 的现有布放不一致，
制造两套规矩；(b) `:6280` 与 `:6281` 是有序对（strip 在 redact 之前），删一半会让这段顺序
关系变得不可读；(c) 收益为零。

**已知边界（声明，不修）**：`_write_pipeline_job_unlocked` 的返回值 `_public_scheduler_row(row)`
（`:6183`）取的是未 strip 的 `row`。该值不是 durable 态；若某调用方拿它再 round-trip 回写，
第二趟写在写边界仍会被 strip 兜住，故不构成持久污染。记录以便回归时可见。

## D3（咬合）：覆写谓词必须看得懂占位符——否则本 PR 比现状更糟

> **round-2 重述（本节初版的适用范围为假，以 D9 为准）**：D3 的真实射程**不是两条腿**，
> 而是**每一条既接受调用方证据又抵达 durable 写的腿**；并且**被比较的值必须等于被持久化的值**——
> 只在 `is not None` 谓词之前 strip 是不够的，还必须在**相等闸门**之前。逐腿普查、逐腿裁定、
> 唯一具名裁决点 `_resolved_caller_evidence` 全部见 **D9**。以下 D3 原文保留为病理与
> 「只修 #1592 更糟」论证的出处，其"两条腿"计数已被 D9 取代。

**适用范围：两条腿，不是一条**（fixture review P1 更正）。投影腿 `:3377-3384` 与 defer 腿
`:3563-3568` 的条件覆写谓词**形状完全相同**，且 defer 腿 `:3583` 直通同一个咽喉。
初版 D3 只写了投影腿，那会让本单**亲手在 defer 腿上交付本节判定为"比现状更糟"的终值**。

defer 腿的可达链（评审实测，记录备查）：第 1 趟 defer 写
`status="reconcile_unverified"`（`:3557`）+ 真实 `log_uri`（`:3568`）；
`reconcile_unverified` **不在** `TERMINAL_PIPELINE_STATUSES`，故 `:3539` 的整行短路
**拦不住第 2 趟**；第 2 趟携带 `"[object-uri]"` → `:3567` 放行覆写 → 新 strip 抹成 `None`。
第 2 趟入口存在（`chain_array_accounting.py:466` 的 `deferrer(..., log_uri=log_uri)`）。
**round-2 更正**：初版这里的标注「机理可达，仓内无活体喂占位符的调用方」**为假，已删除**。
喂占位符的活调用方是实测存在的（`chain_forecast_control.py:256` 遍历
`query_pipeline_jobs_by_cycle` 的公共投影，`:344`/`:359` 两处回喂 `log_uri`），
见上「可达性账」段。

`:3377-3383` 的四个谓词形如 `if log_uri is not None: cohort_row["log_uri"] = log_uri`。
它的**意图**是"调用方给了真值才覆写"。占位符 `"[object-uri]"` 非 `None`，谓词误判为真值。

**作用域精确到"条件覆写谓词"**——两条腿的无条件写形状**不同**，须分别说：

| | 无条件写（无谓词可修，占位符交咽喉兜底） | 条件谓词（D3 的落点） |
|---|---|---|
| 投影腿 | `error_code`（`:3373`，在 `cohort_row.update` 内） | **4 个**：`finished_at:3377` / `exit_code:3379` / `master_error_message:3381` / `log_uri:3383` |
| defer 腿 | `error_code` + `error_message`（`:3558-3559`，在 `row.update` 内） | **3 个**：`finished_at:3563` / `exit_code:3565` / `log_uri:3567` |

共 **7 处**。注意投影腿的 `error_message` 是**条件**写（`:3381`）、defer 腿的是**无条件**写，
两腿不对称，不可照抄。
（已核 `_durable_error_message(None)` 返回 `None`，即便将来扩到无条件字段也不会炸。）

现状 vs 只修 #1592 vs 本单：

| | durable `log_uri` 终值 |
|---|---|
| 现状（master） | `"[object-uri]"` 字面量（洗白，#1592 报的病） |
| **只修 #1592** | `None` —— 真实 URI **被顶掉后又被抹成 None，净损失** |
| 本单（D3） | 真实 `s3://…` 原值保留 |

**修法**：合并前先把入参过一遍 strip，再判 `is not None`。占位符 → `None` → 谓词自然拒绝覆写 →
existing 真值留存。一处改动，恢复谓词的本意。

**对非终态行的连带后果（接受并声明）**：若调用方确实想清空某字段而传了占位符，现在得到的是
"保留旧值"而不是 `None`。这正是 `_strip_redaction_placeholders` docstring 写的语义
（"store `None`（value withheld）"）在**覆写谓词**下的正确投影：withheld ≠ "请清空"。

## D4（#1589）裁定：**归因族**随 `status` 粘，**观测族**继续刷新

`:3347-3353` 的现有注释把设计意图写得很清楚：终态标记"keeps that status **while the projection
still refreshes its evidence fields**"。所以"全部字段一起冻"违背既有意图，"全部字段一起刷"
就是今天的 bug。分界线必须落在字段的**语义类别**上：

| 族 | 字段 | 裁定 | 理由 |
|---|---|---|---|
| **归因** | `error_code`、`error_message` | **随 `status` 粘** | 它们回答"为什么判死"。status 被钉在 `permanently_failed` 而归因被派生投影改写 = 自相矛盾的行，且每趟再漂一次 |
| **观测** | `finished_at`、`exit_code`、`log_uri` | **继续刷新** | 它们是关于 master Slurm 作业的客观事实，刷新它们正是注释所说的 "refreshes its evidence fields"，与 status 无矛盾 |

**被拒方案 A：只粘 `error_code`（issue 标题的字面范围）。** 拒因：`error_message`（谓词 `:3381`，赋值 `:3382`）
同源同病，留着它就是留半个 bug，且下一个人会照 `error_code` 的新形状去问"为什么 message 不粘"。

### D4 的诚实成本（fixture review P3-4，必须写明，不得过诺）

1. **粘住的不保证是"判死原因"。** `mark_pipeline_job_permanently_failed:2678-2681` 的
   `error_code` 是调用方**可选**参数，缺省沿用行上原有值——很可能就是上一趟投影算出的派生值。
   故 spec 措辞取「the error code and message the row already carries」，
   **不写**「the attribution that explains it」。
2. **有一种正当更新被本裁定丢掉。** 唯一生产可达的几何（见下 D4 可达性）恰是
   "durable 投影不全 → 本趟 `complete=True` 首次完整核算"，那一趟派生出的 `error_code`
   是这行**第一次**拿到的逐任务真实成因，D4 保留的却是判死时那个较弱的码。
   **缓解**：逐任务明细仍照常写入 `candidate_projections`（`:3370-3372`），丢的只是标量。
   这是本裁定最实的代价，接受并声明。

### D4 / #1589 的可达性更正（P3-3）

初版写"标记之后**任一趟** resume/reconcile 走 `project_forecast_cohort_tasks` 就会覆写"——**为假**。
resume 腿被 `settled_cohort_master`（`chain_array_accounting.py:312-333`，集合含
`permanently_failed`）在 `chain_stage_execution.py:901-903` 拦掉；reconcile 腿被
`query_inflight_jobs:1167-1172` → `_job_needs_restart_reconcile:8378-8400` 拦掉。
**只有"durable 投影不全 + 本趟带 `complete=True`"这一种几何**能走到 `:3354`
（投影不全而本趟 `complete=False` 时 `:3168` 就转去 defer 并 return）。
#1589 **确实可达，但不是"任一趟"**——该限定须写进 J6/J8 的 docstring。

**被拒方案 B：照 defer 腿 `:3539` 整行短路。** 拒因：defer 腿的语义是"终态了就别写"，
投影腿的语义是"终态了但证据要继续刷"——两者的职责本就不同，把投影腿改成短路会**停掉**
`finished_at` / `exit_code` / `log_uri` 的刷新，违背 must-preserve 3，是 never-break-userspace 的直接违反。

## D5（#1589）裁定：触发条件仍是 `permanently_failed`，**不**扩到全 `TERMINAL_PIPELINE_STATUSES`

`TERMINAL_PIPELINE_STATUSES` 有 7 个成员（`:268-276`），其中 `succeeded` / `partially_failed` /
`failed` **恰恰是 `projected_master_status` 自己会算出来的派生值**。把粘性扩到它们，等于让
第一趟算出的派生结论把后续所有 reconcile 都钉死——那不是修 bug，是把投影腿废掉。
`permanently_failed` 特殊在它是**外部显式打上的判死标记**（#1312），不是派生值，所以只有它需要
被保护不被派生值覆盖。

**可证伪的钉（措辞已按 fixture review P2-4 收紧）**：初版判据"该状态能否由
`projected_master_status` 自行算出"**与结论自相矛盾**——按面值它同样覆盖
`cancelled`/`submission_failed`/`reservation_lost` 这三个非派生值，而 D5 只点名三个真派生值
就排除了全集。收紧后的判据是**两个条件的合取**：

> 该状态既是**外部显式打上、投影无法自行派生**的，**又**是**今天已受 status 粘性保护**的。

`permanently_failed` 是当前唯一同时满足两者的状态。将来若有第二个这样的标记，
粘性集合应随之扩张。

**`cancelled` 的既存缺口：报告不修。** 评审实测 `cancelled`（accepted + 投影不全）
**能**被扫到，而它今天连 `status` 本身都会被派生值覆写（`:3354-3358` 只护
`permanently_failed`）——这是**既存问题、非本单引入**，扩粘性去修它属于未受邀的越界。
已另立 issue 跟踪（见 Disposition 记录）。`submission_failed` 经核实不可达
（`_job_needs_restart_reconcile:8382-8386` 以 `submit_outcome="rejected"` 挡在扫描外）。

## D6：收据档位（fixture 内先裁，不在 merge 时临时决定）

两条 issue 均声明本地可闭环。本单**不买** node-27 全量 receipt（~80 分钟），
**不碰** node-22（无并发面、无 NFS 语义、无调度行为改动）。本地档位：

- 定向：`tests/test_file_orchestration_journal.py`、`tests/test_gateway_reconcile.py`
- 加保：`tests/test_file_orchestration_journal_read_cache.py`（同文件邻接读缓存族，防连带）
- `uv run ruff check $(git ls-files '*.py')` + `openspec validate --strict --no-interactive`

理由：改动是纯文件态逻辑，无 DB、无 Slurm、无展示面；node-27 的判别力相对本地 pytest 为零。
F-b（#1600）会带并发/NFS 面，那一单再定它自己的档位。

## Must-preserve（评审红线）

1. `_defer_forecast_cohort_projection_unlocked` 的整行短路（`:3539`）行为逐字节不变。
2. `permanently_failed` 之外的 master 状态，投影腿的**粘性语义**不变——`error_code` /
   `error_message` 照常被本趟派生值覆写，不得因本单获得任何粘性。
   **该红线不覆盖证据谓词**：D3 对所有状态的行都改条件谓词（占位符不再算真值），
   那是本单有意的行为变更、已在 D3 尾段声明。初版把本条写成"写入行为逐字节不变"，
   与 D3 直接互否（fixture review 第 2 轮 P2-B）——红线清单里放一条实现必然违反的项，
   会让评审无法判断"是不是真红"。
3. **终态行的 `finished_at` / `exit_code` / `log_uri` 继续刷新**（D4 观测族）——这条一旦转红
   就是 D4 被误实现成 B 方案。
4. `[local-path]` / `[redacted]` 继续被持久化，**不**被 strip 掉（`_PERSISTED_REDACTION_PLACEHOLDERS`
   只含 `{"[object-uri]", "[uri]"}`）。
5. pipeline_event 的公共 sanitization（`_public_pipeline_event_payload`）与私有 recovery
   记录路径不受影响。
6. `_redact_durable_error_message_fields` 的**四处**现有布放全部保留，顺序不变
   （strip 在 redact 之前）：`:6003`（`_write_hydro_run`）、`:6147`、`:6281`、`:8023`。
   初版写"三处"、漏 `:6003`（fixture review P3-2）——枚举数错会让"全部保留"无法逐条核。
7. `cohort_changed` 的字段比较列表（`:3385-3399`）语义不变——粘性使某字段不再变化时，
   `cohort_changed` 应自然转 False 而非被特判。
8. 无 migration：历史行（已含字面量占位符的 durable 行）读取行为逐字节不变。
9. `_journal_record_for_write` 的其余产出（`sequence` / `record_type` / 顶层提升字段等）不变。
10. **strip 是整串相等匹配，不是子串**（`:8820` `value in _PERSISTED_REDACTION_PLACEHOLDERS`）——
    嵌在 `message` / `error_message` 里的 `[object-uri]` 子串必须存活。
    `tests/test_file_orchestration_migration.py:263`（`assert "[object-uri]" in raw_journal`）
    依赖这一点，改动后必须仍绿（fixture review must-preserve 漏项 3）。
11. **`upsert_pipeline_job` 的 master frozen 大声拒绝语义不变**（`:1747-1754`）——
    `tests/test_file_orchestration_journal.py:9068`（#1187 的 round-trip
    `init_state_uri="[object-uri]"` 必须抛 `file_journal_evidence_invariant_invalid`
    且 durable 不变）改动后必须仍绿。救济归属见 D7。
12. **`latest/` 物化产物**（`:6730`）的内容变化必须可由"journal / direct row 已被 strip"解释，
    不得出现第三种差异（fixture review must-preserve 漏项 5）。
13. **strip 的类型强转边界**：`:8816-8819` 把任意 Mapping 转 `dict`、任意非 str Sequence 转
    `list`，改动后作用于**每一条** record payload，且发生在 `_strip_internal_fields:8031`
    与 `_validate_outgoing_record` 之前。既有校验结果不得因此改变
    （fixture review must-preserve 漏项 6）。

## D7（P2-5）：两种救济的归属，一句话钉死

同一个占位符进入写路径，本仓现在有**两种**救济，delta 必须说清哪行归哪种：

- **master 行的 frozen 证据字段**（`init_state_identities` 等，`:1747-1754`）→ **大声拒绝**，
  抛 `file_journal_evidence_invariant_invalid`，durable 零变化。这是既有 Requirement
  "Accepted-submit cohort forecast terminal rows SHALL record init-state identity forward-only"
  的规定，本单不动。
- **非 frozen 的证据字段**（`log_uri` / `error_message` 等）→ **静默归 `None`**（value withheld）。
  这是本单的规定。

机制上 frozen 检查先触发，两者不冲突（评审已实测 `:9068` 用例仍通过）。

## D8（P2-1）：手动重试路径的 durable 残值从字面量翻成 `None`——认领为"修复生效"

`_record_manual_retry_submission_success`（`:7893`）取的是**公共行**
（`get_pipeline_job` → `_public_scheduler_row:8797`），`:7897-7908` 的 `row.update` 不碰
`log_uri`，`:7909` 直接 `upsert_pipeline_job(row)`；`log_uri` 在
`_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS:209`、非 master 行无 frozen 门，于是落到
`_write_pipeline_job_unlocked` → 新 strip。

**这不是新增的数据丢失**：真实 URI 今天就已被字面量顶掉。变的是 durable 残值
**从 `"[object-uri]"` 翻成 `None`**。该翻转是**有意的、正确的**：
`chain_stage_execution.py:905-915` 的注释已把 `None` 定义为
"the only honest 'never published'"，而字面量是个谎。
因为它改变下游真值性判断，用 **J10** 在 durable 层钉住"这是有意的"，不留作偶然。

> ### D8 被 round-2 裁定取代（**注意：这是对一条已评审、已钉死决定的翻案**）
>
> 上面这段的结论「durable 残值 = `None`」**作废**。`upsert_pipeline_job` 的合并环是一次
> **无条件写**、且接受调用方证据，因此正是 round-2 ruling「withheld 意味着保留」
> 所辖的同一类腿。同一个问题在同一个 PR 里给两种答案，就是本 design 自己反对的
> 「把不变量散成 N 份拷贝」。
>
> **新终值**：`upsert_pipeline_job` 把占位符解析成**持久化行上的真值**，
> `_record_manual_retry_submission_success` round-trip 之后 durable `log_uri`
> **仍是 `s3://…`**。这比 D8 的 `None` 与改动前的字面量都严格更好——D8 之所以接受 `None`，
> 理由是「真实 URI 今天就已被字面量顶掉」；有了 keep 语义，这个前提本身不再成立。
>
> **J10 相应重写**（`test_manual_retry_round_trip_keeps_the_real_log_uri`）：断言真值存活，
> 不再断言 `None`。「单元构造、非流程可达」的标注保留不变。
>
> **must-preserve 11 未被削弱**：解析只对**整值占位符**生效，mapping/list 值永不解析，
> 因此一份分歧的 `init_state_identities` 仍然抵达 #1183/#1187 的 frozen 检查并被大声拒绝
> （`tests/test_file_orchestration_journal.py:9068` 实测仍绿）。

## Seams under test

- `_journal_record_for_write` 直调——反洗白的隔离 oracle（不经任何写锁/落盘）。
- `project_forecast_cohort_tasks` 端到端——断言须穿透到 **durable jsonl 载荷层与 direct row 文件**，
  不只公共返回值（`_public_scheduler_row` 会再洗一遍，只看返回值的断言无判别力）。
- `_defer_forecast_cohort_projection_unlocked` → `_write_pipeline_job_unlocked`——第二条旁路的
  独立 oracle（证明不是只修了 payloads 循环那一条）。

## Evidence mapping

- **J1**：`_journal_record_for_write` 直调，payload 含 `[object-uri]` / `[uri]` → record 载荷为 `None`。
- **J2**：同上，payload 含 `[local-path]` / `[redacted]` → **原样保留**（must-preserve 4 的正向钉）。
- **J3**：旁路 A（`project_forecast_cohort_tasks`）端到端，占位符不落 durable jsonl。
- **J4**：旁路 B（defer 腿 → `_write_pipeline_job_unlocked`）端到端，同上。
- **J4b（fixture review P1 新增，必须有）**：defer 腿的 displacement 镜像——第 1 趟 defer 写入
  真实 `log_uri="s3://…"`（行落 `reconcile_unverified`，不受 `:3539` 短路保护），
  第 2 趟携带 `log_uri="[object-uri]"` → **durable 仍是 `s3://…`**。
  没有这条，defer 腿的 D3 修复零 oracle，且 **J4 会把 D3 禁止的结果判绿**
  （J4 只断言"占位符不落 durable"，`None` 同样满足）。
- **J5（咬合，本 PR 的核心用例）**：投影腿的 displacement——durable 行有真实
  `log_uri="s3://…"` 且 master 为 `permanently_failed`，一趟携带 `log_uri="[object-uri]"`
  的重投影到来 → **durable 仍是 `s3://…`**。
- **J6**：`permanently_failed` master + 重投影 → `error_code` / `error_message` 保持 existing。
  两个字段**各自独立断言**，不共用一条（否则漏改 `error_message` 的变异杀不掉）。
  docstring 须写明可达几何限定（D4 可达性更正）。
- **J7**：同上 → `finished_at` / `exit_code` / `log_uri`（真值，非占位符）**确实被刷新**
  （must-preserve 3 的反向钉）。
- **J8（按 P3-5 参数化）**：**三个派生终态各一**（`succeeded` / `partially_failed` / `failed`）
  → `error_code` 照常被派生值覆写。参数化杀死"扩成 `in TERMINAL_PIPELINE_STATUSES`"这类变异
  （三臂全红），只用 `failed` 一个代表则只能抓到其中一臂。
  **诚实更正（fixture review 第 2 轮 P2-A）**：初版声称参数化"同时杀死窄扩成
  `{"permanently_failed","cancelled"}`"——**为假**。J8 三臂里没有 `cancelled`，该变异下
  J8 必然全绿、零判别力。真实状况是：**`cancelled` 窄扩没有 oracle，而按 D5 我们也不想要它**
  （`cancelled` 是声明的既存缺口，扩粘性去修属越界）。变异表里那一行是**防误伤的守卫**
  （断言 J8 仍绿），不是杀伤测试。
- **J9**：粘性触发且无其它字段变化时 `cohort_changed` 为 False，不产生空写（must-preserve 7）。
  该几何为**单元构造、生产不可达**（评审 P3-5：要求 `candidate_projections` 不变，
  而那意味着投影已全、行根本不会被扫到）——docstring 须标注，spec 场景亦然。
- **J10（P2-1，round-2 重写）**：手动重试 round-trip（`_record_manual_retry_submission_success`）后，
  durable `log_uri` **仍是真实 `s3://…`**。初版断言 `None`（D8 的翻转），已被 round-2 的
  「withheld 意味着保留」裁定取代，见 D8 尾的取代块。
- **J13-J20（round-2 修复轮）**：其余六条写腿的 idempotency-replay 与 displacement 对偶，
  外加一条参数化的类守卫。逐条定义、逐腿裁定与新增变异行见 **D9**。
- **J11**：`tests/test_file_orchestration_migration.py:263` 的子串存活 + `:9068` 的 master
  frozen 大声拒绝，两条既有用例改动后仍绿（must-preserve 10/11 的现状锁；
  若已被覆盖则直接引用，不重复造）。
- **J12（D2b 的现状锁，实现期新增）**：`tests/test_file_orchestration_migration.py:1637`
  （`test_historical_pipeline_event_runtime_roots_are_redacted_but_retry_recoverable`）
  改动后仍绿——event lane 的公共渲染产出（`object_store_prefix` 渲染成 `"[object-uri]"`）
  必须原样落盘，不被咽喉 strip 抹成 `null`。既有用例，直接引用不重复造。

**变异证死**（红-绿对照，逐条单独施加）：

| 变异 | 预期转红 |
|---|---|
| `_journal_record_for_write` 的 strip 调用删除 | J1、J3、J4 |
| strip 改成恒等（`return value`） | J1、J3、J4、J5 |
| `_PERSISTED_REDACTION_PLACEHOLDERS` 加入 `"[local-path]"` | J2 |
| strip 改成子串匹配（`any(p in value …)`） | must-preserve 10（`test_file_orchestration_migration.py:263`） |
| D3 的**投影腿**入参 strip 删除（谓词退回裸 `is not None`） | **J5** |
| D3 的 **defer 腿**入参 strip 删除 | **J4b** |
| 归因粘性改回无条件覆写（`error_code`） | J6 |
| 归因粘性只覆盖 `error_code`、漏 `error_message` | J6（须能单独区分，不得与上一条同一断言） |
| 粘性触发条件扩成 `in TERMINAL_PIPELINE_STATUSES` | **J8**（三个参数臂全红） |
| 粘性窄扩成 `{"permanently_failed","cancelled"}` | **J8** 必须仍绿（不得误伤——`cancelled` 是声明的既存缺口，不在本单射程） |
| 观测族也被粘住（D4 误实现成 B 方案） | **J7** |
| **D2b 的 event lane carve-out 删除**（咽喉 strip 也作用于 `pipeline_event`） | **J12**（`test_file_orchestration_migration.py:1637`） |

**判别力关键的四条**：J4b（defer 腿）、J5（投影腿）、J7、J8——J7/J8 是 D4/D5 两个方向的反向钉，
J4b/J5 是 D3 两条腿各自的钉。缺任何一个，对应裁定就没有 oracle，下一次重构可以自由翻向。
四条**实测全部逐字命中**（M5→J5、M6→J4b、M9→J8 三臂全红、M11→J7），M10 守卫实测 J8 全绿。

**实测与预测的两处出入（实现期记录，非偏离）**——两条变异均**未存活**，只是杀死它的 oracle 与
预测不同：

| 变异 | 预测转红 | **实测转红** | 解释 |
|---|---|---|---|
| 咽喉 strip 调用删除 | J1、J3、J4 | **J1、J10** | D3 的入参 strip 在**更上游**：两条腿的 `log_uri` 占位符在进 `cohort_row`/`row` 之前就已成 `None`，故 J3/J4 不再需要咽喉即绿。咽喉本身仍被 J1（直调）与 J10（旁路 B 端到端）钉住 |
| strip 改成子串匹配 | migration `:263` | **J2** | D2b 之后 event lane 不过咽喉，`:263` 的子串存活改由 carve-out 保护而非整值匹配；整值-vs-子串的判别力落在 J2 的 `message` 断言（job lane）上。`:263` 仍绿，作为 J11 现状锁不变 |

### 结构性 oracle 的缺口（声明，不造）

spec 的"新增写路径不能重新引入 bypass"是**架构约束，没有结构性 oracle**：J1 + 变异表第 1 行
（从咽喉删 strip → J1 红）钉住的是**布放位置**，不是"将来没有第二个构造点"。
AST 守卫（断言无第二处构造含 `{schema_version, sequence, record_type, source_id, cycle_time}`
的 record dict）在实现中若属平凡可加；否则记为已知边界，**不得造一个假的**。

## Disposition 记录（fixture review 逐条处置，无一留空）

| finding | 处置 |
|---|---|
| **P1** defer 腿保留裸谓词 | **采纳收法 (a)**：D3 同时施加到 defer 腿；新增 J4b + 对应变异行。收法 (b)（把 Req 2 与 D3 限定在投影腿）被拒——它会交付 design 自己判定为"比现状更糟"的终值 |
| **P2-1** 手动重试残值 literal→None | 采纳：新增 D8 认领该翻转为"修复生效"，J10 在 durable 层钉住 |
| **P2-2** 咽喉声明过宽 / 缺结构性 oracle | 采纳：D1 措辞收紧为"journal record 的唯一构造点"，6 个调用点全列 + payload 审计，`latest/` 的派生论证补出；结构性 oracle 记为**已知边界**（AST 守卫若平凡则加，否则不造假的） |
| **P2-3** "Historical rows are not rewritten" 为假 | 采纳：spec 场景改写为与 must-preserve 8 同口径 |
| **P2-4** D5 判据与结论不自洽 | 采纳：判据收紧为"外部显式打上 **且** 今天已受 status 粘性保护"的合取；`cancelled` 缺口报告不修，另立 issue |
| **P2-5** 两种救济未做归属裁定 | 采纳：新增 D7 + spec 补一句 |
| **P3-1** 差-1 坐标漂移 | 采纳：文件头逐条修正 |
| **P3-2** must-preserve 6 数错（三处→四处） | 采纳：补 `:6003` |
| **P3-3** #1589 可达性过诺 | 采纳：可达性账与 D4 两处都改成单一几何 |
| **P3-4** "attribution that explains it" 过诺 | 采纳：spec 改措辞 + D4 诚实成本段 |
| **P3-5** J8 单薄 / J9 生产不可达 / pipeline_event 句子只在一条路成立 | 采纳：J8 参数化三个派生终态；J9 标注单元构造；spec 删去该 pipeline_event 一般性断言 |
| 评审 Note：`_mark_master_permanently_failed:7234` 的 `error_message` round-trip | **报告不修**（同类 laundering 的另一入口，`[redacted]` 不在 strip 集合内，本单不改变它），另立 issue |
| 评审 Note：`reconcile.py:1095-1101` 不传证据参数 ⇒ D3 在该腿为 no-op | 记录，无动作（不构成缺陷） |
| 评审"验证缺口" 1（J1-J9 改动前红/绿未实证） | tasks §1 本就要求先跑先记；不额外动作 |
| 评审"验证缺口" 2（`latest/` 无落盘对照） | 采纳为 must-preserve 12，实现须给对照 |
| 评审"验证缺口" 3（SQL 版 `chain_repository.py` 未审） | 无动作：proposal 已声明不改，评审亦确认证据面为 file-journal 独有 |
| 评审"验证缺口" 4（`cancelled` 完整生产链路未回溯） | 转入新立的 `cancelled` issue，由它承担 |

### fixture review 第 2 轮（delta 复核）处置

| finding | 处置 |
|---|---|
| **P2（不对称写反）** tasks 3.1 收尾句称"两条腿的 `error_code`/`error_message` 都无条件"，与同 bullet 上方三行及 D3 表互否 | 采纳：3.1 收尾句重写，明确"不在射程的只有投影腿 `error_code` + defer 腿 `error_code`/`error_message`；**投影腿 `error_message`（`:3381`）是条件写，在 7 处之内**" |
| **P2-A** J8 参数化"同时杀 `cancelled` 窄扩"为假 | 采纳：J8 条目与 tasks 1.7 都改成诚实措辞——三臂无 `cancelled`，该变异下 J8 必然全绿；**`cancelled` 窄扩没有 oracle，按 D5 也不想要**，变异表那行是**防误伤守卫**不是杀伤测试 |
| **P2-B** must-preserve 2 与 D3 直接互否 | 采纳：收窄到**粘性语义**（`error_code`/`error_message` 不得获得粘性），显式把证据谓词排除出该红线 |
| **P3-C** defer 腿谓词新引入差-1 | **本轮复核前已自核修正**（`a1c25b96`，`:3563/:3565/:3567`、range `:3563-3568`）——复核读的是 `8daa5097` |
| **P3-D** `:5718` 被当成 inventory 的调用点 | 采纳：调用点是 `:6173`/`:6205`，`:5718` 是该函数内部的 atomic write |
| **P3-E** J9 的"生产不可达"承诺 spec 未兑现 | 采纳：spec 场景补 unit-constructed 限定 |
| **P3-F** spec 场景 1 无限定的 "store `None`" 与新场景拉扯 | 采纳：场景 1 补"行上原本无真值"限定 + 指向 displacement 规则，消除又一处 J4 式负判别力 |
| 复核"未能核实"：两条 Note 的 issue 号 | 由 issue-scribe 另路交付，号回来后补进本表 |

## D2b（实现期更正）：strip 绝不跑在 journal 自己的公共渲染下游

**D2 初版论断「strip 是幂等的，双层不产生任何语义差」对 `pipeline_event` 为假。**
`_append_validated_record_unlocked` 的实际顺序是：

```
:6280  payload = _strip_redaction_placeholders(payload)     # 原有：作用于**原始调用方 payload**
:6281  payload = _redact_durable_error_message_fields(...)
:6284  payload = _public_pipeline_event_payload(payload)     # ← 在这里**制造**占位符
:6286  record  = _journal_record_for_write(...)              # ← 本单新加的 strip
```

`_sanitize_public_field`（`:8907-8916`）会**主动把真值渲染成** `[object-uri]` / `[local-path]`
——那是 event lane 有意的、要落盘的公共值，不是调用方洗白。咽喉 strip 跑在它下游，
会把 `"s3://nhms-historical"` 渲染出的 `"[object-uri]"` 抹成 `null`，
实测打红 `tests/test_file_orchestration_migration.py:1637`。
两层看到的**不是同一个 payload**，中间隔着一个会新造占位符的 sanitizer——幂等前提不成立。

**裁定：咽喉 strip 跳过 `pipeline_event`。** 但落地要写成**原则**而非例外：

> 反洗白 strip 清除的是**从调用方来的**占位符。它绝不能跑在 journal 自己的公共渲染下游，
> 因为那个渲染是**有意产出**占位符作为 durable 公共值的。

由此得到的分层是自洽的，不是丑陋的补丁：
**event lane 在调用方边界 strip（`:6280`，作用于未 sanitize 的原始 payload——对 event 而言那才是
真正的洗白面）；job lane 在 record 构造点 strip。**

**被拒方案（B）**：接受该翻转、照 D8 模式认领为"修复生效"并改掉 `:1637` 的断言。
拒因：(a) 与 must-preserve 5 和 proposal Non-Goals 字面冲突；(b) 这里根本不是洗白——
无调用方 round-trip，是 journal 规范的公共渲染；(c) `[object-uri]`→`null` 在此是**丢信息**
（"这里曾有个对象 URI" 变成 "这里什么都没有"），与 D8 认领的调用方 round-trip 翻转性质不同。

**A 的代价，声明不掩饰**：carve-out 意味着 `pipeline_event` 的**结构性保证弱于** `pipeline_job`
——将来若有写路径不经 `_append_validated_record_unlocked` 直接发 event，就没有 strip。
今天安全（投影腿 event 分支的 details 取自 `ACCEPTED_PROJECTION_FIELDS`
= `{array_task_id, array_task_outcome, candidate_id, model_id, native_shud_resubmitted,
restart_stage, run_id}`，无 URI 类字段；`:2601/:2706/:2931` 三处审计同口径）。
记为**已声明的已知边界**。

### D8 可达性更正（实现期实测）

`_create_pending_manual_retry_job:7410` 显式把 `log_uri` 置 `None`，因此完整
`attempt_manual_retry` 流程**够不到**那个洗白点。D8 原文「真实 URI 今天就已被字面量顶掉」
是**单元可构造**而非流程可达。J10 按 tasks 1.9 的字面（点名的是
`_record_manual_retry_submission_success` 这个函数）直调构造，**docstring 须与 J9 同样标注
「单元构造」**——不放第二条过诺的可达性进来。

### 实现期第 3 轮处置（implementer STOP-and-report）

| finding | 处置 |
|---|---|
| D2 幂等论断对 `pipeline_event` 为假，打红 `test_file_orchestration_migration.py:1637` | 采纳方案 A，见本节 D2b；D1 表相应条目改写；must-preserve 5 与 Non-Goals 由 A **保住**而非违反 |
| D8 可达性过诺 | 采纳更正，J10 标注单元构造 |
| 另立 issue 号回填 | `cancelled` 无 status 粘性 → **#1629**；`_mark_master_permanently_failed` 的 `error_message` 回灌 → **#1630** |

**#1630 的口径更正（issue 复验实测，比初版更硬）**：被洗白的**不是**整值 `[redacted]`
（`error_message` 非 sensitive key，且耐久路径与公共路径共用 `redact_payload`，重放是幂等空操作），
而是**嵌在文本里的** `[local-path]` / `[object-uri]` 子串：

```
durable BEFORE : 'sbatch stderr at /ghdc/.../slurm-99.err and s3://nhms/logs/99.out'
durable AFTER  : 'sbatch stderr at [local-path] and [object-uri]'
```

本单**不碰也不该碰**这条路径：strip 是整串相等匹配，must-preserve 10 明确要求嵌入子串存活，
且该写路径（`:2706/:2722/:2727`）既不经 `:6280` 也不经 `_write_pipeline_job_unlocked`。
只能在调用方侧（`:7234`）修 —— 归 #1630。

## D9（round-2 修复轮）：D3 的真实射程 —— 全类普查 + 唯一裁决点

> 坐标：本节引用**改动后**的实现坐标（`file_orchestration_journal.py`，HEAD `cfa88909` 之后）。
> 本文件其余部分仍是 base `66e1c5e5` 坐标，见文件头。

### D9.0 病理的第二半（round-1 漏掉的那半）

round-1 只把「占位符不是真值」施加到 `is not None` **覆写谓词**上。真正的规则更强：

> **被比较的值必须等于被持久化的值。**

咽喉 strip 使 durable 态**已 strip**。一条腿若拿**未 strip 的调用方入参**去和 durable 态做
相等判定，它**永远不可能收敛**——每一趟都判「变了」，每一趟追加一条记录、烧一个 sequence。
defer 腿之所以在 round-1 就是对的，恰恰因为它的 strip 落在 `changed_fields` 相等判定**之前**，
而不只是落在谓词之前。

实测后果（评审在 HEAD 上量到，本轮复现为 J13/J17）：

- `transition_pipeline_job_runtime_status` 用同一个 `log_uri="[object-uri]"` 连打三次 →
  `['applied','applied','applied']`，durable payload 2 → 5。**无界增长**，走向
  `MAX_FILE_JOURNAL_CYCLE_SEGMENTS` / `MAX_FILE_JOURNAL_JSON_BYTES`。
- `complete_pipeline_job_cancellation` 第 2 趟 → `stale` 而非 `idempotent` → `committed=False`
  → `chain_forecast_control.py:346-347` `continue`，**取消事件与 `cancelled` 条目一起被丢掉**。

### D9.1 唯一裁决点

```python
_resolved_caller_evidence(value, *, durable=None)   # :8899 附近
```

- 占位符进 → `durable` 出（**withheld 意味着保留**）。
- **真正的 `None` 进 → `None` 出。** 这是对 lead 字面裁定「stripped 为 `None` 就取 existing」的
  **收窄，须显式记录**：按字面执行会把「调用方确实传了 `None`（= 清空）」也翻成「保留」，
  那是无关本 bug 的语义变更。withheld 的定义是**占位符**，不是 `None`。
  在仓内唯一的活体喂食点上两者等价：`chain_forecast_control.py:344/:359` 喂的是**同一行**的
  公共投影，故「真 `None` 而 durable 有真值」这种输入不可达。
- 其余原样（非字符串证据不受影响）。
- **谓词腿省略 `durable=`**：`None` 让谓词拒绝覆写，而**拒绝覆写就是保留**；这同时避开
  `datetime` 型入参的陷阱（解析成行上已格式化的字符串会撞 `_format_utc`）。
- **无条件写的腿必须传 `durable=`**：那里没有谓词可以拒绝，只 strip 会把真值抹成 `None`。

**为什么是一个 helper 而不是 N 份显式写法**：design D1 的被拒方案理由（「把不变量散成 N 份拷贝，
下一条新写路径照样会漏」）对**比较侧**同样成立；round-1 恰恰是把它散开才漏掉了 6 条腿。

### D9.2 普查表：全部 16 个 `_write_pipeline_job_unlocked` 调用方 + 3 条不经它的 durable 写腿

| # | 腿 | 收调用方证据？ | 谓词 / 无条件 / 相等闸门 | 裁定 |
|---|---|---|---|---|
| 1 | `upsert_pipeline_job` | **是**（整行 merge，`log_uri` 在 `_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS`） | 无条件（`key in explicit_fields` 只是"带没带"）；无相等闸门 | **已修**：merge 环走 `durable=existing.get(key)`。**翻掉 D8/J10** |
| 2 | `append_historical_pipeline_job` | 是 | 新建行；existing 存在即直接返回 | 不受累：无可位移的前值，第二趟根本不写 |
| 3 | `reserve_pipeline_job` | 是 | 构造时把 `log_uri`/`error_*`/时间族全部强制 `None` | 不受累 |
| 4 | `reclaim_pipeline_job_reservation` | 是（身份字段回填，`not in (None,"")` 守卫） | 有守卫；无相等闸门 | 不受累：回填集合内**无任何 URI 类字段**（已逐字段核） |
| 5 | `bind_pipeline_job_reservation` | 是（`slurm_job_id`/`status`/`array_task_id`） | `is not None` 守卫 | 不受累：无 URI 类字段；第二趟被 `slurm_job_id` 已绑挡回 |
| 6 | `commit_pipeline_job_submit_attempt` | **是**（`exit_code`/`error_code`/`error_message`/`log_uri`） | **无条件写**；有绑定后幂等闸（不含这些字段） | **已修（统一性）**：传 `durable=`。实际不可位移（reserved 行这一族恒为 `None`），但规则不留例外 |
| 7 | `transition_pipeline_job_submit_evidence` | **是**（6 个证据参数） | 6 个 `is not None` 谓词 + `changed_fields` **相等闸门** | **已修**：谓词腿解析，落在闸门之前。J15/J16 |
| 8 | `transition_pipeline_job_runtime_status` | **是**（6 个） | 同上 | **已修**。J13/J14 |
| 9 | `request_pipeline_job_cancellation` | 是（`reason` → `error_message`） | 无条件写；`cancellation_pending` 状态闸**在写之前**答 idempotent | **不修，裁定见 D9.3** |
| 10 | `complete_pipeline_job_cancellation` | **是**（5 个） | **无条件写**；`reconcile_unverified` 臂有 `desired` 比较器 | **已修**：`durable=` 解析，且**解析一次**供比较器与落盘共用。J17/J18 |
| 11 | `record_pipeline_job_reconciliation` | 是（accounting + `candidate_projections`） | `is not None` 守卫；无相等闸门 | 不受累：`_bounded_candidate_projections` 白名单**无 URI 类字段**；accounting 全是枚举 |
| 12 | `release_identity_blocked_reservation` | 否（只有 int/CAS 参数） | — | 不受累 |
| 13 | `_defer_forecast_cohort_projection_unlocked` | **是**（3 个） | 3 谓词 + `changed_fields` 闸门 | round-1 已正确；本轮改用 helper（行为等价）。J4b/J20 |
| 14 | `update_pipeline_job_status` | **是**（6 个） | 4-tuple 谓词环 + `error_*` 分支；**无相等闸门** | **已修**，裁定见 D9.3。J19 |
| 15 | `_write_pipeline_job` | — | 全仓**零调用方**（死壳） | 不受累 |
| 16 | `_create_pending_manual_retry_job` | **是**（`{**failed_job}`，而 `failed_job` 来自 `query_pipeline_jobs_by_run` 的**公共行**） | 新建行；`log_uri`/`error_*` 显式置 `None` | **报告不修**，见 D9.4 |
| A | `project_forecast_cohort_tasks` payloads 循环 | **是**（4 个） | 4 谓词 + `cohort_changed` 比较 | round-1 已正确；本轮改用 helper。J5/J20 |
| B | `reject_pipeline_job_submit_attempt` payloads 循环 | 是（`error_code`/`error_message`/`stage`/`job_type`） | 无条件；`rejected`+`submission_failed` 幂等闸在写之前 | 不受累：闸门先答 idempotent，且无 URI 类字段 |
| C | `mark_pipeline_job_permanently_failed` payloads 循环 | 是（`error_code`/`error_message`/`finished_at`） | `not in (None,"")` / `is not None` 守卫；`permanently_failed` 幂等闸在写之前 | 不受累 |

hydro_run 侧一并扫过：`create_hydro_run` / `create_hydro_run_from_basin` 的 URI 来自 run
context/manifest（不是 journal round-trip）且都是新建行；`update_hydro_run_status` 不收任何 URI
参数。三者都走 `_append_validated_record_unlocked`，**改动前就已在调用方边界被 strip**，
本单没有给它们造出任何新的 interlock。

### D9.3 两条被点名要求单独裁定的腿

**`update_pipeline_job_status`：只解析，不加 `durable=`，不加相等闸门。**
`row = dict(existing)`，所以谓词拒绝覆写时行上留着的就是持久值——**拒绝就是保留**，
`durable=` 是多余的。它无条件追加一条记录、没有相等闸门，这是**既有行为**：本单欠的是
「值收敛」，不欠「不写」；给它补闸门是未受邀的行为变更。J19 因此只钉值收敛，
J20 用 `appends_on_replay=True` 显式承认它会多写一条。

**`request_pipeline_job_cancellation`：不动。** `reason` 是操作者现场文本，不是 round-trip 的
journal 证据；把占位符 reason 解析成「保留上一条不相干的 `error_message`」会配出一个更坏的谎
（新 `error_code` + 旧 message）。且 `cancellation_pending` 状态闸在写之前就答 idempotent，
不存在不收敛。

### D9.4 次要发现的裁定（全部实测过）

**1. durable JSONL 里的 `init_state_uri: null` —— 声明，不修。**
`_bounded_init_state_identities`（`:8360-8388`）的 docstring 承诺「未记录的字段保持**缺席**，
而不是变成 null 值的断言」，它跑在咽喉 strip **之前**，所以 strip 之后 durable 文件确实与该散文
不变量相抵触。实测（`uv run python`，直调构造点）：

```
DURABLE JSONL payload: {"init_state_identities": [{"array_task_id": 0, "init_state_uri": null, "model_id": "m0"}, {"array_task_id": 1, "model_id": "m1"}], "job_id": "probe"}
read-path normalization: [{'array_task_id': 0, 'model_id': 'm0'}, {'array_task_id': 1, 'model_id': 'm1'}]
```

两半都成立：durable 里确有 `null`；读路径把它**丢掉**，因此重写一次即恢复「缺席」，
**自愈**。#1187 frozen 闸仍先触发（`:9068` 实测仍绿）。
**不修的理由**：唯一的修法是让 strip 在 mapping 里**删键**而不是置 `null`，那会改变
**每一个**被 strip 字段的 durable 形状（`log_uri: null` → 键缺席），远超本单射程且会动到读侧契约。
**代价声明**：D9.2 第 1 行的 upsert keep 语义**不消除**这条通路——mapping/list 值永不整值解析，
嵌套占位符照旧被咽喉置 `null`。

**2. `_journal_record_for_write` 注释过诺 —— 已改（纯注释，行为逐字节不变）。**
原文「the event lane strips at the caller boundary」对**全部** event 为假：三条 inline payload
循环产生的 event（`reject_pipeline_job_submit_attempt` / `mark_pipeline_job_permanently_failed`
/ reservation-lost）既不过 `:6280` 的调用方 strip，也不过 `_public_pipeline_event_payload`。
它们今天安全只因为 `details` 里没有 URI 类字段（D1 逐条审计），那是**声明的代价、不是结构保证**。
注释已改写成这个口径。

**3. AST 守卫按函数名索引 —— 已收紧。** 原守卫只断言「唯一命中落在名为
`_journal_record_for_write` 的函数里」，第二个同名函数即可满足它。现在**额外**断言模块内
该名字的 `def` 恰好一个。其余已知边界（只看本模块、只看字面 dict）继续写在 docstring 里。

**4. must-preserve 12（`latest/` 落盘对照）在 round-2 下仍成立 —— 声明，无新差异类。**
round-2 的 keep 语义在 upsert 腿上把「durable 残留 = `None`」改成「真实 URI 存活」，
表面上是 `latest/` 的第三种差异。实际不是：`latest/` 物化**派生自** journal record 与
direct row 两者，而这两者的 keep 语义已被 J10 同时钉住（J10 两条断言分别打在
`durable["log_uri"]` 与 direct row 上）。因此 `latest/` 唯一的新差异类就是
**D8 已声明的那条 supersession**，不需要新增对照。

**5. tuple → list 强转在 upsert 腿上提前了 —— 声明（must-preserve 13 类）。**
`_resolved_caller_evidence` 在 `upsert_pipeline_job` 的合并环里跑在**内存 `row`** 上，
其内部 `_strip_redaction_placeholders` 会把 tuple 值重建成 list。于是一个 tuple 值字段
在 `_pipeline_job_conflicts_unlocked` / inventory 同步**之前**就已是 list，而不是像 round-1
那样只在 record 构造点才转。两者在**相等语义**上不同（`(1,2) != [1,2]`），所以这是真实的
时序变化，不是纯表示差异。证据：定向四套件 962 绿 + 全量 backstop 零回归（下方 D9.6 尾）——
仓内没有依赖「合并期仍是 tuple」的比较点。

### D9.5 J20：行为守卫，不是又一个 AST 守卫

`test_log_uri_write_legs_converge_on_a_replayed_placeholder` 参数化覆盖**六条**接受
`log_uri` 的公共写腿（submit_evidence / runtime_status / complete_cancellation /
update_status / defer / project）：先用真值喂一趟，再用同一个占位符打两趟，断言
durable 真值**两趟都不动**，且（除 `update_pipeline_job_status` 外）第二趟**不追加记录**。
它约束的是**类**，不是七个实例；新增写腿必须进这张表。

**两处显式排除，不假装覆盖**：`commit_pipeline_job_submit_attempt` 的前置行按预留契约
恒无 `log_uri`，构造不出可位移的真值；`upsert_pipeline_job` 的证据是以**整行 record**
而非 `log_uri=` 关键字进来的，由 J10 单独钉。

### D9.6 变异证死（round-2 新增行）

逐条单独施加、sha256 快照 + 拷回核验（每条均打印
`restored ok (dc1c2b7a16c7)`，与施加前指纹逐字节一致）。**实测转红的用例名照抄**：

| # | 变异 | 预期转红 | **实测转红** |
|---|---|---|---|
| M-R1 | `_resolved_caller_evidence` 改成恒等（`return value`） | J13-J20 全族 + round-1 的 J4b/J5/J10 | **16 red**：J4b、J5、J10、J13、J14、J15、J16、J17、J18、J19、J20 六臂全红。**一处施加打红全部 8 条腿**，正是「唯一裁决点」的判别力证据 |
| M-R2 | `complete_pipeline_job_cancellation` 只 strip、不传 `durable=` | **J18**（J17 抓不到） | **2 red**：J18 + J20[complete_cancellation]。J17 **确实仍绿**——strip-only 下两趟都收敛到 `None`，收敛性不受损、被毁掉的是值。两条用例的分工由此实证不冗余 |
| M-R3 | 比较器与落盘用两个不同表达式（`desired` 回退用原始入参） | **J17** | **1 red**：J17。J18 仍绿（落盘侧仍是 resolved）。「比较的值 = 持久化的值」这条要求由 J17 单独把守 |
| M-R4 | `upsert_pipeline_job` merge 环退回 `row[key] = incoming[key]` | **J10** | **1 red**：J10 |
| M-R5 | `update_pipeline_job_status` 六行解析删除 | **J19** + J20[update] | **2 red**：正如预期 |
| M-R6 | `transition_pipeline_job_runtime_status` 六行解析删除 | J13、J14 + J20[runtime] | **3 red**：正如预期 |
| M-R7 | `transition_pipeline_job_submit_evidence` 六行解析删除 | J15、J16 + J20[submit_evidence] | **3 red**：正如预期 |
| — | helper 把真正的 `None` 也解析成 `durable` | 无 oracle —— **已知缺口，声明** | 该变异会让「调用方传 `None` 清空」失效；仓内无活体输入能区分（见 D9.1），**故不造假 oracle** |

M-R2/M-R3 的分离结果是本表最有信息量的部分：它证明 replay 收敛与值保全是**两个**性质，
一条用例杀不掉另一条的变异，所以每腿两条用例不是冗余。

**红-绿实证**（选择表达式显式记录，便于复现）：

```
uv run pytest -p no:randomly tests/test_file_orchestration_journal.py \
  -k "cannot_launder or does_not_displace or keeps_the_real_log_uri or replay_stops_appending \
      or replay_is_idempotent or converge_on_a_replayed_placeholder or record_for_write"
```

共 21 条。把 HEAD `cfa88909` 的源文件按 sha256 快照换入（`163966c71399`）后跑同一选择 →
**`12 failed, 9 passed`**；拷回修复版并核验（`restored ok (dc1c2b7a16c7)`）后 → **`21 passed`**。
9 条在 HEAD 上就绿的是 J1/J2（构造点直测）、`per_model_row` round-trip、两条 round-1
`cannot_launder`、两条投影位移用例，以及 **J20 的 defer / project 两臂**——即**类守卫在
round-1 已修的腿上不误报**。

**红-绿逐腿（12 条实测 FAILED@HEAD，逐条照抄）**

| 腿 | 用例 | @HEAD | @fixed |
|---|---|---|---|
| upsert（#1） | `test_manual_retry_round_trip_keeps_the_real_log_uri` | FAILED | PASSED |
| runtime_status（#8） | `test_runtime_status_placeholder_replay_stops_appending_records` | FAILED | PASSED |
| runtime_status（#8） | `test_runtime_status_placeholder_does_not_displace_a_real_log_uri` | FAILED | PASSED |
| submit_evidence（#7） | `test_submit_evidence_placeholder_replay_stops_appending_records` | FAILED | PASSED |
| submit_evidence（#7） | `test_submit_evidence_placeholder_does_not_displace_a_real_log_uri` | FAILED | PASSED |
| cancellation（#10） | `test_cancellation_completion_placeholder_replay_is_idempotent` | FAILED | PASSED |
| cancellation（#10） | `test_cancellation_completion_placeholder_does_not_displace_a_real_log_uri` | FAILED | PASSED |
| update_status（#14） | `test_update_pipeline_job_status_placeholder_does_not_displace_a_real_log_uri` | FAILED | PASSED |
| 类守卫 | `…converge_on_a_replayed_placeholder[transition_pipeline_job_submit_evidence]` | FAILED | PASSED |
| 类守卫 | `…converge_on_a_replayed_placeholder[transition_pipeline_job_runtime_status]` | FAILED | PASSED |
| 类守卫 | `…converge_on_a_replayed_placeholder[complete_pipeline_job_cancellation]` | FAILED | PASSED |
| 类守卫 | `…converge_on_a_replayed_placeholder[update_pipeline_job_status]` | FAILED | PASSED |

`commit_pipeline_job_submit_attempt`（#6）无对应红——它按预留契约不可位移，J20 已显式排除，
这条修改是**统一性**改动、没有 oracle，如实记账。

定向四套件全量：改源前 `949 passed`，改源后 `962 passed`（+13 = 新增 7 条单腿用例 + 6 个参数臂），
**零回归**。

**全量 backstop**（`uv run pytest -q -p no:randomly -m "not e2e and not grib and not integration"`）：
`1 failed, 12780 passed, 19 skipped, 152 deselected in 2310.59s`。唯一失败是
`tests/test_entropy_audit_script.py::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings`，
**与本单无关**：把 HEAD `cfa88909` 用 `git archive` 展开成纯净树、对它跑同一个 hard-gate
审计，得到**逐字段相同**的唯一门控发现
（`production-topology-node22-local-postgres` @ `openspec/specs/production-scheduler-orchestration/spec.md:103`，
`hard_gate_failing_count=1`）；该 spec 文件本单一个字节都没动。
