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
  占位符化的整行做 round-trip。**但**：迄今仓内没有实测到一条把占位符喂进 cohort 投影
  `log_uri` 参数的活调用方——所以"活体可达的是机理，不是已发生的故障"。措辞不得过诺。
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
| `_append_validated_record_unlocked:6286` | 通用（已另有 `:6280` 的 strip） |
| `project_forecast_cohort_tasks` payloads 循环 `:3429` | **旁路 A** |
| `_write_pipeline_job_unlocked:6154` | **旁路 B**（含 defer 腿；本身有 16 个调用方） |
| `:2601` submission_failed 拒收 | event details，评审逐条读过，无 URI 类字段 |
| `:2706` `mark_pipeline_job_permanently_failed` | `event_details` 来自 `:7236-7245`，全是计数/分类 |
| `:2931` reservation_lost / absence | 同上，无 URI 类字段 |

后三条与 16 个 `_write_pipeline_job_unlocked` 调用方**不各配用例**——全覆盖正是咽喉的意义所在；
上表的 payload 审计就是它们的证据。

**充分性已实测查实**：`_write_pipeline_job_unlocked` 里除 `record` 之外还有 `row` 的下游——
`_sync_reconcile_inventory_for_row_unlocked(row)`（`:5718` 与 `:6205` 两次）、
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

**适用范围：两条腿，不是一条**（fixture review P1 更正）。投影腿 `:3377-3383` 与 defer 腿
`:3562-3568` 的条件覆写谓词**形状完全相同**，且 defer 腿 `:3583` 直通同一个咽喉。
初版 D3 只写了投影腿，那会让本单**亲手在 defer 腿上交付本节判定为"比现状更糟"的终值**。

defer 腿的可达链（评审实测，记录备查）：第 1 趟 defer 写
`status="reconcile_unverified"`（`:3557`）+ 真实 `log_uri`（`:3568`）；
`reconcile_unverified` **不在** `TERMINAL_PIPELINE_STATUSES`，故 `:3539` 的整行短路
**拦不住第 2 趟**；第 2 趟携带 `"[object-uri]"` → `:3567` 放行覆写 → 新 strip 抹成 `None`。
第 2 趟入口存在（`chain_array_accounting.py:466` 的 `deferrer(..., log_uri=log_uri)`）。
与投影腿同一诚实标注：**机理可达，仓内无活体喂占位符的调用方**——但这正是本单立项
#1592 所用的同一条可达性标准，不能一边用它立项一边用它豁免。

`:3377-3383` 的四个谓词形如 `if log_uri is not None: cohort_row["log_uri"] = log_uri`。
它的**意图**是"调用方给了真值才覆写"。占位符 `"[object-uri]"` 非 `None`，谓词误判为真值。

**作用域精确到"条件覆写谓词"**——两条腿的无条件写形状**不同**，须分别说：

| | 无条件写（无谓词可修，占位符交咽喉兜底） | 条件谓词（D3 的落点） |
|---|---|---|
| 投影腿 | `error_code`（`:3373`，在 `cohort_row.update` 内） | **4 个**：`finished_at:3377` / `exit_code:3379` / `master_error_message:3381` / `log_uri:3383` |
| defer 腿 | `error_code` + `error_message`（`:3558-3559`，在 `row.update` 内） | **3 个**：`finished_at:3562` / `exit_code:3564` / `log_uri:3566` |

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

**被拒方案 A：只粘 `error_code`（issue 标题的字面范围）。** 拒因：`error_message`（`:3382`）
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
2. `permanently_failed` 之外的 master 状态，投影腿的写入行为逐字节不变（含 `error_code`）。
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
  → `error_code` 照常被派生值覆写。参数化同时杀死"扩成 `in TERMINAL_PIPELINE_STATUSES`"
  与"窄扩成 `{"permanently_failed","cancelled"}`"两类变异；只用 `failed` 一个代表抓不住后者。
- **J9**：粘性触发且无其它字段变化时 `cohort_changed` 为 False，不产生空写（must-preserve 7）。
  该几何为**单元构造、生产不可达**（评审 P3-5：要求 `candidate_projections` 不变，
  而那意味着投影已全、行根本不会被扫到）——docstring 须标注，spec 场景亦然。
- **J10（P2-1）**：手动重试 round-trip（`_record_manual_retry_submission_success`）后，
  durable `log_uri` 为 `None` 而非字面量 `"[object-uri]"`——把 D8 的翻转钉成有意行为。
- **J11**：`tests/test_file_orchestration_migration.py:263` 的子串存活 + `:9068` 的 master
  frozen 大声拒绝，两条既有用例改动后仍绿（must-preserve 10/11 的现状锁；
  若已被覆盖则直接引用，不重复造）。

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

**判别力关键的四条**：J4b（defer 腿）、J5（投影腿）、J7、J8——J7/J8 是 D4/D5 两个方向的反向钉，
J4b/J5 是 D3 两条腿各自的钉。缺任何一个，对应裁定就没有 oracle，下一次重构可以自由翻向。

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
