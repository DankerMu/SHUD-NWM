# Tasks: accepted-submit-identity-write-semantics

> **坐标基线**：本文件所有代码行号引用为**改动前坐标**（base `a2d50fd4`），实现后已漂移；
> 唯一例外是显式标注「终态坐标」的条目。old→new 映射见 PR 终报；复跑 6.4 的变异配方时
> **按名索引而非行号**，直接照搬本文件行号会变异错行并得到假的「变异体存活」。

## 1. 红先行 / 现状锁（先写先跑，记录 pre-change 红或现状绿）

- [x] 1.1 **J9（#1187 主腿，红先行）**：per-model（candidate）终态行的公共 round-trip upsert
  （`get_pipeline_job` → `upsert_pipeline_job`）**不得**把 durable `s3://` URI 变成 `[object-uri]`；
  按对称冻结语义断言 raise，并断言 **durable jsonl 载荷层**该行 `init_state_uri` 仍是 `s3://…`
  （不只看公共返回值）。记录 pre-change 红证据（改动前是静默洗白并持久化）。
- [x] 1.2 **J13（#1188 主腿，现状锁）**：attempt #1 记账映射 A → **经 absence 路径进入
  `reservation_lost` + `absence_retry_permitted`**（versioned master 的 reclaim 分支
  `file_orchestration_journal.py:1844-1871` 要求 status/submit_outcome/reconciliation_source/
  reconciliation_decision/reason_class/matched_slurm_job_id 六项齐备；`{submission_failed,
  reservation_lost}` 那个宽集合在 `:1889-1895`，**只对非 versioned candidate 行生效**——
  fixture review P1-2 更正，原文写 `submission_failed` 走不通）→ 以携带映射 B（不同
  `init_state_id`）**且带 `expected_submission_attempt` + `expected_submission_attempt_started_at`**
  的请求行 reclaim 成功；断言 durable 行的 `init_state_identities` 为 **A**（keep-first 裁定）。
  改动前后同绿（这是裁定锁不是缺陷修复），判别力由 6.4 的变异体证死。

## 2. #1180 不变量负向 oracle（八个 raise 点，各至少一条；零写入是硬要求）

- [x] 2.1 **J1**：构造期 streak 非负 `int`（参数化 `-1` / `1.0` / `True` / `"1"` 四类非法值）→ `ValueError`。
- [x] 2.2 **J2**：pre-outcome（`submit_outcome is None`）transition 携带非零 streak → `ValueError`。
- [x] 2.3 **J3**：`decision == identity_mismatch_released` 而 `status != "reservation_lost"` → `ValueError`。
- [x] 2.4 **J4**：非 identity-mismatch decision 携带非零 streak → `ValueError`。
- [x] 2.5 **J5-J8**：归一化侧**四条**一一对应——`:566-568`（streak 类型）、`:600-602`（decision 为
  None 而 streak≠0）、`:626-628`（`identity_mismatch_released` 而 `status != "reservation_lost"`）、
  `:630-632`（非 identity decision 携带 streak）。落盘入口**默认** `upsert_pipeline_job` 打在已存在
  的 versioned master 行上，期望 typed error **且** `get_pipeline_job(job_id)` 与写前逐字节一致；
  `release_identity_blocked_reservation` 结构上够不到 `:626`/`:630`（fixture review P1-5）。
  **原文前提「评审实测四条全部由该入口可达」已被 round-1 实测证伪**，三处 carve-out 如下（机制与
  入口枚举见 design「入口可达性账」）：
  - `:566-568`（J5）：**无 carve-out，原地不动**。它在该入口上结构性安全——断言
    `file_journal_evidence_type_invalid`，而 #1183 冻结闸只可能抛 `..._invariant_invalid`；
    双站点同删（J5+J6 守卫）后仍红。
  - `:600-602`（J6）：**改由 `reserve_pipeline_job` 入口隔离**。normalize 的 candidate 臂在 `:539`
    （终态坐标 `:560`）提前 return（该组不变量只对 master 行生效），而对 contract-current master 行，全覆盖冻结表
    （`ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS` 含 `identity_blocked_streak`）抢先拒掉任何
    ordinary upsert 分歧，且抛出的 `(reason, field)` 与归一化守卫**完全相同** → 单独删守卫该腿仍绿。
    reserve 入口的 clean-reservation dirty-field 集合不含该字段，故守卫在此承重：零写入改以
    「raise 后 `get_pipeline_job(...) is None`」表达，并带同形 `streak=0` 的合法 record 作污染对照。
    **不得**复用 `_assert_invariant_left_no_trace`（insert 语义下没有「先前的行」）。
  - `:626-628`/`:630-632`（J7/J8）：**落盘腿原样保留，另各追加一条
    `normalize_accepted_submit_evidence` 直调隔离腿**。reserve 的 clean-reservation 闸在调归一化
    **之前**抢先拒绝带 decision 的新预约，且载荷无法同时合法（带 decision 时归一化要求
    `submit_outcome` 非空，而它本身是该闸的 dirty field）；**迄今实测的落盘入口**（upsert /
    reserve / generic transition / release）**皆无法隔离这两个站点**——该枚举是实测边界，
    **不是**「没有任何落盘入口能隔离」的证明，将来若有人找到可隔离入口，直调腿可被取代。
    两个站点经四入口实测无任何活调用方能违反，即**纯防御性守卫**，直调是目前唯一可能的 oracle。
  - **fixture review P1-5 的部分修订（必须落字）**：P1-5 反对的是用直调**替代**落盘腿——那会让
    零写入断言变空断言。此处是**追加**：J7/J8 的落盘腿与其零写入断言原样保留，直调腿只承担
    「单删该守卫即转红」的隔离声明。保留的落盘腿旁须加注释，写明其对自身守卫的判别力依赖持久行
    decision-free/source-free（一次自然的 fixture 编辑即可使其静默空转）。
  - **同时记录**：`get_pipeline_job` 「逐字节一致」这半边在 upsert 入口是**恒真**的（终态坐标
    `file_orchestration_journal.py:1757` 对 contract-current structural master 无条件 return），
    断言保留但不得再被叙述成「证明了零写入」。

## 3. #1187 对称冻结（D-B）

- [x] 3.1 **J10**：per-model 行显式传 `None` / `[]` → 按冻结语义 raise，durable 值不被抹平。
- [x] 3.2 **J11**：per-model 行传结构合法但**内容错误**的映射 → raise（这是占位符容忍 merge 覆盖不到的面）。
- [x] 3.3 **J12（D-B2 现状锁）**：master 行 PUBLIC 快照零变更逐字复放**继续硬失败**
  （`file_journal_evidence_invariant_invalid`）——锁住「公共视图不是合法写入载荷」的裁定，
  未来任何放宽必须显式改这条测试。
- [x] 3.4 **J16（must-preserve 8）**：新增 candidate 冻结闸自身的负向 oracle——避免制造新的
  「变异存活的守卫」（与 D-A 同标准）。
- [x] 3.5 实现：`accepted_submit_identity.py` 加最小 per-model 不可变表；
  `file_orchestration_journal.py` 在 existing 分支加 `accepted_submit_row_kind(existing) == "candidate"`
  臂。master 闸门逐字节不动。**两条落点纪律必须遵守（design D-B1/D-B1b，fail-close 风险源）**：
  (i) 走**后置比较**（与 `persisted_master_state` 闸同形 `:1720-1728`，合并只拷 explicit key），
  或前置放置但显式 `if field not in explicit_fields: continue`——否则 `_pipeline_job_row:6046`
  无条件注入的缺省 `[]` 会让所有 no-key candidate upsert 被拒（今天是 silent-keep，转红即行为回归）；
  (ii) candidate 臂同样受 `accepted_submit_contract_is_current(existing)` 守卫，否则无 marker 的
  历史 per-model 行行为改变，与 must-preserve 4 冲突。
- [x] 3.6 复核并记录**安全性 receipt**：审计所有 `upsert_pipeline_job` 调用方是否会以
  `accepted_submit_row_kind(existing) == "candidate"` 为目标显式携带**分歧**的该键——
  **含整行 get→upsert round-trip**（至少覆盖 `file_orchestration_journal.py:7849→:7865` 的
  `_record_manual_retry_submission_success`，它确实携带该键的占位符化值，只是该行 row_kind 为
  `None` 故闸门不触发）。字面量 grep 不是合格测法（fixture review P1-3）。结论落 PR body。
- [x] 3.7 **J17（must-preserve 10，fail-close 反向钉）**：不带该键的 candidate 行普通 upsert
  保持 silent-keep（durable 值不变、不 raise）——3.5 的落点纪律 (i) 若实现错误将立刻转红。

## 4. #1188 keep-first 落字（D-C）

- [x] 4.1 **J14（连带断言，防假绿）**：J13 同一用例断言 `submission_attempt` 已递增、
  `submission_attempt_started_at` 已重打、`status == "reserved"`——证明确实走了 reclaim 成功路径，
  而非被身份等值闸拒绝。
- [x] 4.2 **J15（终态传导）**：reclaim 后走完 bind + `project_forecast_cohort_tasks`，断言逐 model
  终态行的 `init_state_identities` 与裁定值（A）一致——证明语义传到血统证据而不只 master 行。
- [x] 4.3 实现：注释**落在真实成因处**——reclaim 回填 key 元组（`file_orchestration_journal.py:1944`）
  的遗漏本身，以及 `row` 由 `existing` 经 `apply_accepted_submit_transition(..., begin_attempt())`
  派生处（`:1905-1911`）；说明该字段有意不随新 attempt 刷新，理由与 `submission_attempt_started_at`
  anchor 同源。**不要**把注释挂在 `if not versioned_master:` 守卫上——该守卫不是成因，且文件里有
  **两个**同名守卫（`:1849` 与 `:1943`）（fixture review P1-1）。spec 措辞收紧见 5.1。

## 5. 规格与文档

- [x] 5.1 spec delta：`pipeline-job-persistence` 的 init-state identity forward-only 需求收紧
  「值自预约起不变」→「自**首次**预约起不变，reclaim 进入新 attempt 不刷新」，并加 per-model 行
  对称冻结与 master PUBLIC 复放两条 scenario；identity-mismatch 收敛需求加不变量 test-anchored scenario。
- [x] 5.2 spec 注记：keep-latest 的重新裁定触发器（#1185 落地后若证明 keep-first 产生真实误判）。

## 6. 验证与证据

- [x] 6.1 `uv run pytest -q tests/test_file_orchestration_journal.py tests/test_gateway_reconcile.py tests/test_warm_start_chaining.py` 全绿。
- [x] 6.2 `uv run ruff check .` 干净。
- [x] 6.3 `openspec validate accepted-submit-identity-write-semantics --strict --no-interactive` 通过。
- [x] 6.4 变异证死三组：删 candidate 冻结闸 → J9-J11 红；**把 `INIT_STATE_IDENTITY_FIELD` 加入
  reclaim 回填 key 元组（`:1944`）并让该拷贝对 versioned master 也生效** → J13/J15 红
  （**单独翻转 `if not versioned_master:` 杀不死这两腿**——该守卫包住的元组根本不含该字段，
  评审已实测变异存活，fixture review P1-1）；删**构造期「released ⇒ reservation_lost」守卫**
  （改动前 `:262` / 终态 `accepted_submit_identity.py:267-268`）→ J3
  （`test_identity_released_transition_must_abandon_the_reservation`）红。
  ⚠️ **不要照改动前坐标 `:262` 删**：终态上那一行是另一条闸（`if decision == "matched_bound":`），
  照旧坐标施变异会删错守卫、产出假阴性；以守卫名 + 终态坐标为准（round-2 P2-1）。
  各自应用→红→回退→绿，记录红-绿对照。
  **#1180 归一化四条的变异表（round-1 补全 + 按终态 oracle 重指；`:566`/`:600` 原文漏列）**——
  每条守卫**单独删除**均须转红，且**只能**由下列 oracle 证死：

  | 守卫（改动前 / 终态坐标） | 变异体 | 终态 oracle |
  |---|---|---|
  | `:566` / `:586-589` | 删 G5 | `test_normalization_invariants_reject_the_ordinary_upsert_path[J5_streak_type]` |
  | `:600` / `:620-623` | 删 G6 | `test_reserve_rejects_a_streak_carried_without_a_decision`（**新增行**：原 `J6_streak_without_decision` 参数在 upsert 入口被冻结闸兜底，删守卫仍绿，已实测；reserve 入口删守卫后 `NO RAISE` 且行落盘） |
  | `:626` / `:646-649` | 删 G7 | `test_normalization_isolates_the_released_reservation_invariant`（直调；删守卫后归一化 `DID NOT RAISE`。原 `J7_...` upsert 腿今天也会一并转红，属附带、非隔离证据） |
  | `:630` / `:650-653` | 删 G8 | `test_normalization_isolates_the_foreign_decision_streak_invariant`（直调；同上，原 `J8_...` upsert 腿一并转红属附带） |

- [x] 6.5 所有代码行号引用在**终态**上重新自核（本仓注释/docstring 行号会静默漂移；Batch R 的 V6 即此类）：
  已在终态逐条自核并记录 old→new 映射，**映射表落在 PR body 的「fixture 坐标 old→new 映射表」一节**
  （改动前该引用悬空——只写「见终报」而终报未落表，round-2 P2-2(b)）；**fixture 文本按已登记
  deviation 保留改动前坐标、未回改**，该口径由本文件顶部的坐标基线 banner 声明，
  显式标注「终态坐标」的条目除外（该 carve-out 经 round-2 逐条验证成立，14 处全部正确）。
  **round-2 更正**：首轮自核漏审了 6.4 里的 `:262`（唯一一处陈旧坐标会造成实害的地方，P2-1），
  已在上一条补名 + 补终态坐标；本勾以此次补审为准。
