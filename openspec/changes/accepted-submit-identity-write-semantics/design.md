# Design: accepted-submit-identity-write-semantics

> **坐标基线**：本文件所有代码行号引用为**改动前坐标**（base `a2d50fd4`），实现后已漂移；
> 唯一例外是显式标注「终态坐标」的条目。old→new 映射见 PR 终报；阅读与复算变异时
> **按名索引而非行号**，直接照搬本文件行号会变异错行。

## 风险三角（fixture level: expanded）

- **风险**：journal 行是 scheduler 判定的真值源；三条缺口都在「写入证据」侧。#1187 已能把 durable
  `s3://` 血统 URI 洗成字面占位符并持久化；#1188 的 keep-first 无裁定无锁；#1180 守的是
  `identity_mismatch_released` 终态语义的唯一守卫。三者当下均**无活的错判**（见下「可达性账」），
  风险是 future-regression。
- **可控性**：db-free 纯文件态逻辑，本地 pytest 全闭环；改动集中在两个文件的窄区块。
- **不确定性**：中——两条需要**语义裁定**（不是找 bug），裁定错会把不变量钉在错的方向上；故本 design
  对两条裁定各给出理由、被拒方案与可证伪的钉。

### 可达性账（三条都是 latent，必须在评审里保持诚实）

- #1187：in-tree 无调用方会以 `row_kind(existing) == "candidate"` 为目标携带**分歧**值——注意整行
  round-trip（`:7849→:7865` 的 `_record_manual_retry_submission_success`）**确实携带**该键的占位符化
  值，只是该行 `row_kind` 为 `None` 故闸门不触发（详见 D-B 安全性依据；issue 原文的「grep 零命中」
  是错的测法，fixture review P1-3）。且读侧三态比对把占位符按「值 withheld」处理，被洗白只会退化成
  absent、不产生**错误** verdict。
- #1188：方向 fail-safe（陈旧身份 → conflict → 重跑，读侧只能「拒绝跳过」不能「准许跳过」），
  且当前 cohort 几何下该映射对 verdict 不可见（#1185 未落地）。
- #1180：当前仓内所有合法写入方都天然满足这些不变量。
- #1180 归一化第三、四条（`:626-628`/`:630-632`，终态坐标 `:646-649`/`:650-653`）**是纯防御性守卫**：
  迄今实测的落盘入口（`upsert_pipeline_job` / `reserve_pipeline_job` / generic
  `transition_pipeline_job_submit_evidence` / typed `release_identity_blocked_reservation`，机制见
  D-A）**皆无法隔离这两个站点**——每条入口都有更早的闸抢先，或根本无法表达该非法几何。
  该枚举是**实测边界，不是「没有任何入口」的证明**（写路径入口按 `grep "def .*pipeline_job"` 枚举，
  `record_pipeline_job_reconciliation` / `permit_pipeline_job_retry` / `project_forecast_cohort_tasks`
  等未穷尽）。**推论**：对一个无活可达路径的防御性守卫，`normalize_accepted_submit_evidence` 直调是
  目前**唯一可能**的 oracle，而不是「落盘腿的廉价替代」；将来若有人找到可隔离的落盘入口，直调腿可被取代。
  这同时也是 spec「每道守卫都有单独删除即转红的负测」（`specs/pipeline-job-persistence/spec.md:91`）在这两个站点上的**正当兑现方式**——
  不是绕开该承诺，而是在无活可达路径的前提下唯一能兑现它的形式。

**推论**：本 change 的价值全部在「把未裁定的语义钉死 + 给防御性守卫配 oracle」，任何声称修复了活体
故障的措辞都是过诺。

## D-A（#1180）：八个 raise 点的负向 oracle

对象（当前 master 坐标，实现时须重新自核——本仓 docstring/注释行号引用会静默漂移，Batch R 的 V6 即此类）：

- 构造期 `AcceptedSubmitTransition.__post_init__`：`:229`（streak 必须非负 `int`，`type(...) is not int` 同时挡 `bool`）、`:239`（pre-outcome transition 必须是干净 reserved 起始、streak 为 0）、`:262`（`identity_mismatch_released` 时 status 必须 `reservation_lost`）、`:264`（非 identity-mismatch decision 不得携带非零 streak）。
- 归一化 `normalize_accepted_submit_evidence` **四条**（fixture review P1-5 更正——原文漏了第三条，
  而它恰是 #1180 称为「唯一守卫」的归一化对应臂）：`:566-568`（streak 类型）、`:600-602`
  （decision 为 None 而 streak≠0）、**`:626-628`（`identity_mismatch_released` 而
  `status != "reservation_lost"`）**、`:630-632`（非 identity decision 携带 streak）。
- 落盘入口**首选** `upsert_pipeline_job` 打在已存在的 versioned master 行上。**不要**用 issue #1180
  建议的 `release_identity_blocked_reservation`——它硬编码 `status="reservation_lost"`（终态坐标
  `file_orchestration_journal.py:3030`），只能产出**合法**形状，结构上够不到 `:626`/`:630`。
- **round-1 实测更正（原文两处前提已被证伪，机制见下「入口可达性账」）**：
  (i)「评审实测四条 raise 全部由该入口可达」**为假**——`:600-602` 在该入口上被 #1183 的
  `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS` 冻结闸（终态坐标
  `file_orchestration_journal.py:1747-1754`）抢先，且抛出的 `(reason, field)` 与归一化守卫**完全相同**，
  单独删守卫该腿仍绿；`:626`/`:630` 今天虽红，但判别力是 fixture 偶然（持久行恰好 decision-free/
  source-free，写入在更靠前的字段上也分歧）。
  (ii)「写前写后 `get_pipeline_job` 逐字节一致」在该入口是**恒真**的——contract-current structural
  master 分支恒以 `return _public_scheduler_row(existing)`（终态坐标
  `file_orchestration_journal.py:1757`）结束，写入对该类行不可达。该半边断言保留（删断言易被误读为
  削弱 oracle），但**不得**再被叙述成「证明了零写入」。真正的零写入证据在新增的 reserve 腿上，
  以「raise 后 `get_pipeline_job(...) is None`」表达。

做法：在 `tests/test_gateway_reconcile.py` 已有的 `file_journal_evidence_invariant_invalid` 家族旁加两组
参数化——一组打构造（期望 `ValueError`），一组打落盘路径（期望 `AcceptedSubmitEvidenceError` /
`FileOrchestrationJournalError` **且** `get_pipeline_job(job_id)` 与写前逐字节一致）。参数矩阵至少
覆盖 `streak = -1 / 1.0 / True / "1"`。**零写入断言是硬要求**，只 `pytest.raises` 不够（issue AC 原文）。

### 入口可达性账（round-1 三方独立实测；决定终态腿形状，勿凭直觉推翻）

| 归一化守卫（改动前 / 终态坐标） | 结论 |
|---|---|
| `:566` / `:586-589`（streak 类型） | upsert 入口**结构性安全**：断言 `..._type_invalid`，冻结闸只可能抛 `..._invariant_invalid`，双站点同删仍红。**原地不动** |
| `:600` / `:620-623`（decision 为 None 而 streak≠0） | upsert 入口**不可隔离**（冻结表含该字段，`(reason, field)` 相同）；`reserve_pipeline_job` 可达且双向翻转已验（HEAD `RAISE` + 行不存在；删守卫 `NO RAISE` + 行落盘）。**移到 reserve 入口** |
| `:626` / `:646-649`、`:630` / `:650-653` | 四个落盘入口**皆无法隔离**（机制见下表）。**落盘腿原地保留 + 追加直调隔离腿** |

四个落盘入口的出局机制（**这是实测枚举，不是「没有任何入口」的证明**——措辞纪律见「可达性账」）：

| 入口 | 抢先者 |
|---|---|
| `upsert_pipeline_job` | 全覆盖冻结表（终态 `file_orchestration_journal.py:1747-1754`）抢先，`(reason, field)` 与断言相同 |
| `reserve_pipeline_job` | clean-reservation 闸（终态 `:1810`）在调归一化（终态 `:1814`）**之前**抛 `file_journal_clean_reservation_required`；且载荷无法同时合法——带 decision 时归一化要求 `submit_outcome` 非空，而它本身是该闸的 dirty field |
| generic `transition_pipeline_job_submit_evidence`（终态 `:2158`） | 两重独立阻断：`AcceptedSubmitTransition` 孪生守卫构造期 `ValueError`（终态 `accepted_submit_identity.py:267`/`:269`）；且 decision 白名单（终态 `file_orchestration_journal.py:313-321`，`:2185-2193` 强制）排除 `identity_mismatch_released`。**注意**：该路径**确实会归一化**（transition 后载荷经 `_validate_outgoing_record`（终态 `:6402`）送进 `normalize_accepted_submit_evidence`），杀死隔离的是**上游抢先**，不是「该路径不归一化」 |
| typed `release_identity_blocked_reservation`（终态 `:2957`） | 硬编码 `status="reservation_lost"`（终态 `:3030`），只能产出合法形状，无法表达非法几何 |

可复用结论：任何候选入口须**同时**满足「带 decision 的 master 载荷在此合法」与
「无冻结表/前置闸/孪生 dataclass 守卫抢先于归一化」。

## D-B（#1187）裁定：per-model 行走**对称冻结**

`accepted_submit_row_kind(existing) == "candidate"` 时，对一张最小 per-model 不可变表（至少含
`INIT_STATE_IDENTITY_FIELD`）做与 master 同形的分歧拒绝；表放在 `accepted_submit_identity.py`
与 master 两张表并列。

### D-B1 落点纪律（**fail-close 风险源，必须写死**；fixture review P1-4）

`_pipeline_job_row` 是闭合构造器，`:6046` **无条件**注入该字段（缺省 `[]`）。因此闸门的实现形态不是
自由选择：

- **推荐：后置比较**——与 master 的 `persisted_master_state` 闸同形（`:1720-1728`），合并循环只拷
  explicit key，天然免疫缺省值。
- 若坚持前置放置，则**必须**显式加 `if field not in explicit_fields: continue`（master identity 闸
  `:1687-1688` 正是这么做的）。

否则任何**不带该键**的 candidate 行普通 upsert 都会拿缺省 `[]` 比持久非空值 → 拒绝。评审实测：今天这种
no-key upsert 是 silent-keep（durable 仍是 `s3://…`，只有公共返回值被占位符化），一旦 fail-close
就是行为回归。**这才是本 change 真正的 fail-close 风险源**（重投影路径经实证不构成风险，见下）。

### D-B1b contract-current 守卫（同为必钉项）

candidate 臂**必须**同样受 `accepted_submit_contract_is_current(existing)` 守卫（master 臂有，
`:1682-1684`）：`normalize_accepted_submit_evidence` 的 candidate 分支
（`accepted_submit_identity.py:514-538`）也只在 contract-current 时才规范化该字段；无 marker 的历史
forecast per-model 行（`row_kind` 仍可能返回 `"candidate"`）若落进闸门会改变历史行行为，与
must-preserve 4 直接冲突。

### D-B1c 重投影路径经实证不构成 fail-close（评审实测，记录备查）

`project_forecast_cohort_tasks` 的 per-model 写入经 payloads → `_journal_record_for_write` → 直写
（`:3382-3400`），**完全不经 `upsert_pipeline_job`**，闸门对它不可见；即便重投影，candidate job_id
内嵌 `master_slurm_job_id`+`task_id`（`:3207`），换 master id 是新行而非覆写，同 master id 下已终态
task 在 `verified` 里被跳过（`:3169-3172`），且映射从 master 逐字拷（`:3211-3214`）。reconcile 侧写
路径（`release_identity_blocked_reservation:2918`、`_defer_forecast_cohort_projection_unlocked`）
均 master-only。

**裁定理由**：与 spec「值自预约起不变」一字对齐；不引入「哪些值算占位符」的新判定面；对「结构合法但内容错误」的映射同样有防护。**安全性依据**：无调用方会以 candidate 行为目标携带**分歧**的该键，故新增拒绝不可能打断现有调用方。
**receipt 的测法必须是审计而非字面量 grep**（fixture review P1-3）：字面量 grep 会漏掉**整行
round-trip**——`file_orchestration_journal.py:7849` `row = get_pipeline_job(job_id)` → `:7865`
`upsert_pipeline_job(row)`（`_record_manual_retry_submission_success`）传的是整张 public 行，该键
**确实被携带**且是占位符化后的值。结论仍成立（该 retry 行 `candidate_id`/`array_task_id` 被置 None
（`:7354/:7363`），`accepted_submit_row_kind` 返回 `None` 而非 `"candidate"`，闸门不触发；failure
侧走 `update_pipeline_job_status` 不经 upsert），但必须按「审计所有调用方是否会以
`row_kind(existing)=="candidate"` 为目标显式携带分歧值，含整行 round-trip（至少覆盖 `:7865`）」
的口径复核。

**被拒方案（占位符容忍 merge）**：入参为 `_PERSISTED_REDACTION_PLACEHOLDERS` 或整体为空时回退 persisted 值。
拒绝理由：把「占位符 = 未提供」这条规则从读侧扩散到写侧，判定面变大且更易漂移；且对结构合法但内容错误的
映射毫无防护，必须与对称冻结组合才完整——那就不如只做冻结。

### D-B2 附带裁定：master PUBLIC 行零变更逐字复放**继续硬失败**

`0f617268` 之后，master 行拿公共快照做零变更复放会硬失败（公共读已把 `init_state_uri` 占位符化，
冻结闸拿 sanitized 值比 durable 值必然分歧）。裁定**维持硬失败、不特例化**：

- 该类别对 `log_uri` **早已存在**（`:8800-8808` 对 `*_uri` 结尾键占位符化；`slurm_job_id` **不**被占位符化，不属该类别——fixture review Note 更正），即「公共视图可作为写入载荷」这个模式本来就不被支持；为 `init_state_identities` 单独开洞会造成同表内两套规则。
- 公共视图是**展示投影**，不是合法写入载荷。让调用方拿着自己并不持有的 URI 去声明血统，正是 #1187 要防的事；硬失败是诚实信号。
- 代价（复放不友好）由「in-tree 无此调用方」这一事实兜住；若将来出现真实复放需求，正确出口是给调用方提供 durable 读接口，而非放宽写入闸。

该裁定须在 spec 或就地注释落字，并配一条**记录现状**的测试（复放抛错），使未来任何放宽都必须显式改测试。

## D-C（#1188）裁定：reclaim 取 **keep-first**

reclaim 进入新 attempt 时**不**刷新 `init_state_identities`：回填 key 元组不含该字段的现状维持不变，
并在**真实成因处**补显式注释——回填 key 元组的遗漏本身（`:1944`）与 `row` 由 `existing` 经
`apply_accepted_submit_transition(..., begin_attempt())` 派生处（`:1905-1911`）——说明该字段有意
不随新 attempt 刷新。**注释不挂在包住该元组的 `if not versioned_master:` 守卫上**（`:1943`；注意
`:1849` 另有同名守卫）：该守卫不是成因（fixture review P1-1 实测单独翻转它变异存活）。
spec 措辞从「自预约起不变」收紧为「自**首次**预约起不变，reclaim 不刷新」。

**裁定理由**：与 reclaim 路径既有姿态同源（`submission_attempt_started_at` 的注释已写下同一理由：
authority anchor 绝不从 lock 外的陈旧请求行拷贝）；方向 fail-safe。

**被拒方案（keep-latest）**：记账更贴近「这次真正跑的 init state」，但需要新增一条「该字段可被 lock 外
请求行改写」的例外，与「master 行状态只由持久行说了算」的整体口径冲突，并给伪造请求行开新写入面。
收益不抵判定面扩张——**除非** #1185 落地后证明 keep-first 会产生真实误判；该条件写进 spec 注记，
作为将来重新裁定的触发器。

## Must-preserve（评审红线）

1. #1183 既有用例全绿（`tests/test_file_orchestration_journal.py` 的 init_state_identities 族、`tests/test_warm_start_chaining.py` 同族）——**按测试名索引，不用行号锚**。
2. master 行现有冻结负测全绿；master 冻结闸行为逐字节不变（本 change 只加 candidate 臂）。
3. reclaim 既有用例全绿（`tests/test_gateway_reconcile.py` 的 reclaim 族）；`submission_attempt` 递增、`submission_attempt_started_at` 重打、dead-status 谓词（#1173 定稿）不动。
4. 历史行（无该字段）行为逐字节不变，零 migration。
5. `forecast_cohort_digest` 的 member field set 与历史行校验结果不变。
6. `_CYCLE_SCOPE_JOB_PROJECTION_KEYS` 的既有取舍不动。
7. 读侧三态比对（`scheduler_init_state_match.py` 的占位符→withheld 处理）不动——本 change 只动写侧。
8. 新增的 candidate 冻结闸**本身**必须有负向 oracle（与 D-A 同标准，避免制造新的「变异存活的守卫」）。
9. `project_forecast_cohort_tasks` 的 per-model 行写入路径不受影响（经实证安全，正因如此更要钉成红线，回归时立刻可见）。
10. **no-key candidate upsert 保持 silent-keep**（D-B1 的反向表述——这条一旦转红就是 fail-close 回归）。
11. manual/auto retry 的整行 round-trip（`:7865`）与复用分支（`:7105`）不得转红。

## Seams under test

- `upsert_pipeline_job` 的 existing 分支（candidate 行 round-trip / 显式空值 / 内容错误映射）——写侧闸门 oracle，断言须穿透到 **durable jsonl 载荷层**，不只公共返回值。
- `reclaim_pipeline_job_reservation` + 后续 bind + `project_forecast_cohort_tasks`——keep-first 的终态传导（证明语义传到血统证据而不只 master 行）。
- `AcceptedSubmitTransition.__post_init__` 与 `normalize_accepted_submit_evidence` 直调——不变量 oracle。

## Evidence mapping

J1-J4 = #1180 构造期四条；J5-J8 = #1180 归一化四条（终态腿形状见下表）；
J9-J11 = #1187（round-trip 不洗白、显式空值不抹平、内容错误被拒，均至 durable 层）；
J12 = D-B2 master PUBLIC 复放现状锁；
J13-J14 = #1188（reclaim 后 master 行 keep-first + 连带断言 attempt/anchor/status，
证明确实走成功路径而非被身份闸拒绝）；J15 = #1188 终态传导；
J16 = D-B 新闸门的负向 oracle（must-preserve 8）；
J17 = D-B1 落点纪律的反向钉（must-preserve 10）——不带该键的 candidate upsert 保持 silent-keep。
变异证死：删 candidate 闸门 → J9-J11 红；**把 `INIT_STATE_IDENTITY_FIELD` 加入 reclaim 回填
key 元组（`file_orchestration_journal.py:1944`）并让该拷贝对 versioned master 也生效** → J13/J15 红；
删**构造期「released ⇒ reservation_lost」守卫**（改动前 `:262` / 终态
`accepted_submit_identity.py:267-268`）→ J3 红；**勿照 `:262` 旧坐标施变异**——终态该行是
`if decision == "matched_bound":`，是另一条闸（round-2 P2-1）。

**#1180 归一化四条的变异矩阵（原文只列了 `:626`/`:630` 两条，漏 `:566`/`:600`；round-1 按终态
oracle 归属补全）**——四条守卫**各自单独删除**均须转红，红-绿对照见 tasks 6.4：

| 守卫（改动前 / 终态坐标） | 终态 oracle | 判别力从何而来 |
|---|---|---|
| `:566` / `:586-589` | J5 原 upsert 腿（原地不动） | `reason` 分歧：断言 `..._type_invalid`，冻结闸只能抛 `..._invariant_invalid` |
| `:600` / `:620-623` | **新的 `reserve_pipeline_job` 腿** | raise + `get_pipeline_job(...) is None`（该入口无冻结闸兜底） |
| `:626` / `:646-649` | **新的直调隔离腿**（原 upsert 腿保留） | 直调 `normalize_accepted_submit_evidence`；删守卫后归一化**不再抛错**（非被兄弟守卫接住） |
| `:630` / `:650-653` | **新的直调隔离腿**（原 upsert 腿保留） | 同上 |

保留的 J7/J8 upsert 腿今天确实转红，但那是**fixture 偶然**（持久行 decision-free/source-free）；
它们钉的是落盘路径的 typed 拒绝，隔离声明由直调腿承担。

**#1188 变异体的重要更正**（fixture review P1-1 实测）：单独翻转 `if not versioned_master:`
（`:1943`；注意文件里**另有一个**同名守卫在 `:1849`）**杀不死** J13/J15——被它包住的回填 key 元组
（`:1944-1958`）根本不含 `INIT_STATE_IDENTITY_FIELD`。keep-first 的真实成因是「回填表遗漏该字段
＋ `row` 由 `existing` 经 `apply_accepted_submit_transition(..., begin_attempt())` 派生
（`:1905-1911`）」，不是那个守卫。有效变异必须同时改守卫**和**回填表。

## Disposition 记录

- **#1188 AC-1 的落字位置**：issue 字面要求写进
  `openspec/changes/scheduler-completion-verdict-absence-tolerance/tasks.md:7`，但该 change 已归档
  （`openspec/changes/archive/2026-07-29-…`），归档物是不可变记录。本 change 改为落在它当初 delta 进
  的 live spec（`pipeline-job-persistence` 的 init-state identity forward-only 需求）——等价且正确，
  显式记录以免被当成 AC 遗漏（fixture review Note）。

## 已知形状（非缺陷）

spec delta 的 MODIFIED 需求正文超长行违反 MD013——那是 baseline
（`openspec/specs/pipeline-job-persistence/spec.md:272,396` 同样违反）的**逐字复刻**，
折行会破坏 byte-faithful 复制与 OpenSpec parser。CI 的 markdownlint 只 gate `docs/`，不影响门。
