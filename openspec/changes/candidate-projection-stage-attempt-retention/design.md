# Design: candidate-projection-stage-attempt-retention

Fixture level: expanded
Project profile: NHMS

## Change surface

- `services/orchestrator/chain_repository_state.py` `candidate_state_from_rows`（:667-683 截断块，
  head 行号）——唯一**投影层**改动点
- `tests/test_production_scheduler.py`：逆序几何回归腿 + 既有友好序用例
  `test_strict_warm_start_budget_binds_on_the_truncated_production_geometry`(:42219) 保持
- 只核对不改码：`scheduler_state_rows.py` attempt 推导（:425-479）、
  `scheduler_candidates.py:2225`（L2 预算）、`scheduler_state_failure.py`
  :188/:1444/:1900/:1917、`scheduler_state_manual_retry.py:982`、
  `scheduler_state_evidence_owner.py:110`
- 钉住（不改行为）：`file_orchestration_journal.py` reservation 写点 :1778/:1903
  （`error_code: None` 的真实载体）与释放写点 :2804/:2954 + `retry.py`
  `classify_failure`/`should_auto_retry` 链
- 文档：`docs/runbooks/failed-basin-retry.md` 补一行（预算在逆序几何下现在真实绑定）

## D0: 范围裁决——file-journal 投影 only（fixture review P1-1）

**DB 读路径在 SQL 里就截断**：`chain_repository_state.candidate_state` 的查询
（:519-525 `ORDER BY COALESCE(updated_at, …) DESC … LIMIT %s`，:535 绑定 `job_limit + 1`）
用与 `_pipeline_job_truth_sort_key` 相同的新鲜度序在数据库侧丢弃旧行——逆序几何下携带最大
attempt 的行**根本到不了投影层**，投影层保留救不了它，且该路径的 `pipeline_jobs_total`/
`state_truncated` 也只能看到 `job_limit+1` 内的溢出。该路径 live
（`chain_repository.py:126` → 默认 repository @ `chain_forecast_orchestrator_cycle.py:79`）。

裁决：本 change **收窄到 file-journal 投影路径**（`file_orchestration_journal.py:887` 读
cycle 全行后投影——生产实际路径，#1173 归档 receipt 佐证生产跑 journal），spec 措辞显式
排除 DB 路径（spec 只写排除事实；tracking 归本文件与 PR body——durable spec 不背未编号
引用，round-2 P3）；DB 缺口已由 issue-scribe 路由独立 issue（编号见 tasks 3.1 / PR body；
家族边界：本批次是纯文件态逻辑 + 本地 pytest oracle，SQL 侧修复需真实 DB oracle）。
E 面无 DB 腿——这是显式排除，不是遗漏。

## D1: 保留规则（核心不变量，file-journal 投影）

**不变量：被截断的 `pipeline_jobs` 投影必须保留每个 canonical downstream stage 的
attempt 上界所在行——保护的是"数值上界"，不是行群体（见 D2 可见性诚实条）。**

实现（在现有倒序排序、切 `[:job_limit]` 之前）：

1. 对每个出现于输入的 canonical downstream stage 找 attempt 最大的行。stage 判定**必须
   import 消费侧同一函数**：`from services.orchestrator.scheduler_state_rows import
   _canonical_downstream_stage`（已探测无 import cycle——scheduler_state_rows 只依赖
   scheduler_state_common/types/retry_identity/production_contract）；**禁止**使用本文件的
   `_STAGE_ALIASES`（:24-45）——它含 `download`（非 canonical）且漏 `copyback`（canonical，
   `scheduler_state_types.py:34`），用它保留会错行。attempt 值复用
   `effective_retry_attempt(job_id, retry_count)`（`retry_identity.py:41`），与
   `_job_retry_attempt` 同构；绝不解析 job-id 子串。同 stage 同 attempt 并列取 truth
   timestamp 最新者。
2. 保留集并入：**fill 必须实现为对既有倒序排序列表的过滤**（先标记保留行，再顺列表取
   剩余名额），不得重排——窗口边界完全并列（同 stage/attempt/truth timestamp/created_at）
   时今天的结果由 `enumerate` 索引上的稳定排序决定（:657），过滤式 fill 原样保持该行为。
3. 结果照旧正序回排，保留行落在自然排序位置。
4. 退化边界：保留集自身 > `job_limit` 时按 truth timestamp 取最新 `job_limit` 条——
   hard cap 优先于上界全保留。
5. attempt 为 0 的 stage 不占保留名额（上界 0 时任何行都推导出 0，保留无信息量；
   该规则只"不加行"，绝不删新鲜度窗本会保留的行）。

## D2: 必须保持 + 可见性变化诚实条（round-2 P2-2 / round-3 收窄）

- `state_truncated`（state 键，:827）/ `pipeline_jobs_total`（:825）语义不变（仍按输入总数）。
- 友好序几何逐元素一致：保留行在窗内时 `{R} ∪ top(k-1 \ R) == top(k)`（review 已代数
  验证），正序回排后与现状全同。既有 :42219 用例是回归钉，**不得放宽**。
- `event_limit` 截断完全不动；`_record_allowed_for_compute_state_terminal` 过滤先于保留。
- **可见性变化诚实条（两个半面，round-3 收窄）**：保留有两个效果——
  **(i) 加入面**：一条窗外异常行对行扫描谓词新变可见。**round-3 实测：生产行形
  （cycle-scope、model-less、`cycle_` run id、failed/reserved 状态）被全部四个无条件
  扫描者过滤掉**——`_restarted_stage_family`（scheduler_state_manual_retry.py:929-935）与
  `_state_has_candidate_scope_failed_job`（:566-568）经 `_job_is_cycle_scope_row`
  （:174-186）排除 cycle-scope 行；`_state_active_jobs`（scheduler_state_rows.py:600-611）
  与 `_state_has_only_unsubmitted_auto_retry_placeholders`（:633-641）按
  `ACTIVE_PIPELINE_STATUSES`（scheduler_state_types.py:28）门滤，failed/reserved 不在集内。
  加入面在生产行形下**无已证实的消费者**；未来若有扫描者放宽 cycle-scope 或 ACTIVE 过滤，
  该面重新打开（残余风险落字，无腿——诚实收窄而非扩大接受风险）。
  **(ii) 挤出面（唯一带腿的半面）**：每保留一条窗外行挤掉窗内第 `job_limit` 新的一条。条件可达的
  消费者：`_failed_stage`（scheduler_state_failure.py:63-74）先查 state 级
  `failed_stage`/`stage`/`restart_stage` 三键，**真实投影恒发全三键**（`restart_stage` 常
  非空），行扫描仅在三键全空时可达（E11 必须自建该可达性前提，见下）；经
  `_state_status`（scheduler_state_rows.py:575-589，key-presence 抑制）路由的
  `_state_hydro_run_is_live_failure`/`_durable_shud_output_exists` hydro 分支在真实投影上
  同样到不了行扫描——暴露弱于字面。同函数内 `_source_cycle_download_repair_state`:729-740、
  `_candidate_manual_stage_repair_state`:741-752 属挤出面；`candidate_jobs`/`latest_job`
  已被 review 证明安全（保留行必老于全部窗内行、挤出的是最老窗内行，`[-1]` 不变）。
  **接受该风险**（逆序几何本身就是投影已失真的场景，真值可见优先），E-leg：
  - E11（挤出面，与 E12 共享同一几何）：显式构造 `failed_stage`/`stage`/`restart_stage`
    三键全空的 state 使 `_failed_stage` 行扫描可达，钉逆序几何下解析结果（腿内注明可达性
    前提，防 vacuous）。
  不变量保护的是数值上界，不是行群体——任何未来需要"特定行在场/不在场"的消费者仍受
  新鲜度窗与保留双重摆布（残余风险落字）。

## D3: 消费面核对 + 行为变化表（fixture review P2-2）

stage-scoped 调用点逐一核对并在 PR 记录结论。其中 **`scheduler_state_failure.py:1917` 是
真实行为变化——它在 manual-retry 路径上**（round-2 review 实测修正）：`:1909`
`_manual_retry_state_evidence` → `:1913` `_failure_policy_payload(state, manual=True)` →
`:1917` `previous_attempt`。limit 门确实先跑（:1913 在 :1917 之前），但 `manual=True`
**设计上解除**它（`classify_failure` 的 `permanent = not manual and (...)`，retry.py:199），
且 evidence 无条件 `"decision": "manual_retry"`、`manual_retry.allowed: True`——手动重试
越过已耗尽预算是**既有的有意行为**，不是待堵的洞。mint 链（正确引用）：
`:1917-1918` → evidence `manual_retry.new_attempt`（:1928，`manual_retry` 块 :1925-1928；
:1934 是 `failure` 块内的同名键，勿混）→
`chain_runtime_utils._retry_attempt_from_basins:310-325` → `chain_forecast_control.py:149` →
`_retry_cycle_stage_job_id`（chain_forecast_orchestrator_cycle.py:151-163）→
`_pipeline_retry_job_id:387`（`f"{base}_retry_{attempt}"`）。两腿钉住（E12）：

**E12 可达性前提（round-3/4，与 E11 共享几何——geometry B 配方）**：`:1917` 是
`_state_retry_attempt(state, stage=_failed_stage(state))`——`_failed_stage` 先读 state 级
三键，必须让它落空到行扫描才能读出保留行的 N。**三键空性是投影后属性**（`stage`/`failed_stage`
由截断后 jobs 派生 :837-840，`restart_stage` :861-885），不是输入属性。**天然 filler
（succeeded convert）构造不出**：`restart_stage` 会解析成 'forcing' 短路行扫描（round-4
实测 geometry A 双侧 vacuous）。可行配方（geometry B，round-4 实测）：filler 用 succeeded
**publish**（`TERMINAL_PIPELINE_COMPLETION_STAGES = {parse, state_save_qc, publish}`，
chain_repository_state.py:20）——`_has_terminal_completion_stage_success` 为真跳过
:875-885 的 `restart_stage` 派生，三键全空，行扫描可达；publish 行 attempt 0 不占保留名额
（D1.5），几何干净。实测：today `_failed_stage=None`/`previous_attempt=0` → after
`'forecast'`/`87`。**E11 的可达性前提必须是硬断言不是注释**：
`assert _failed_stage(today_state) is None`（geometry A 下 E11 会 vacuous 绿）。

- E12a（操作员路径恢复）：上述几何 + 已 adopt 的 manual-retry marker **不带显式
  attempt**（`_manual_retry_new_attempt:985-990` 优先 marker 自带值，无则 fallback）→
  `manual_retry.new_attempt == N+1`、mint `_retry_{N+1}`（今天：读 0 → `_retry_1` 撞既有键
  → 提交静默跳过，`scheduler_state_manual_retry.py:953-956` docstring 已载）。**一次今天
  不会发生的提交现在会发生**——有意恢复，钉住。
- E12b（manual 越过预算是既有设计）：同几何 + `N >= retry_limit` → manual 路径照常
  `manual_retry` / `allowed: True` / `new_attempt == N+1`——预算**不**门 manual retry
  （`manual=True` 解除 permanent 判定），钉住该既有语义在真值下的形状（今天同路径也
  allowed，只是 new_attempt 是错的 1）。

其余：`scheduler_candidates.py:2225`（E5 覆盖——auto 侧预算门在此，逆序真值下 blocked）、
`:1444`/`:1900`（evidence attempt 记账，数值变真，无提交面——核对说明）、
`manual_retry:982`（family floors 只在 `_failed_stage` 非 canonical/空时可达（`_fallback_previous_attempt:978-983` 早退）——与 E12 几何互斥，且生产行形被 cycle-scope 过滤排除（D2 加入面），核对说明即可）。
无 stage 调用点走 flat-first，真实投影恒有顶层 `retry_count`，不受影响（review 已探测证实）。

## D4: released 行钉住（不改行为；fixture review P2-4 修订）

现状保护机制：`error_code` 为空来自 **reservation 写点**（:1778/:1903 显式
`"error_code": None`）；释放转换 `apply_accepted_submit_transition`
（accepted_submit_identity.py:310-327）整行拷贝不触碰 `error_code`，只换 accounting 元组与
status。`should_auto_retry` → `classify_failure(None)` → `UNKNOWN_FAILURE` 不可重试
（`SLURM_RESERVATION_LOST` 在 transient 集合 retry.py:37——未来在这两个写点盖瞬时 code
即打开重复提交）。钉法：

- E6a（shape 钉，**必须驱动真实 reserve→release 序列**，手搓行无效——真实回归向量是
  :1778/:1903 的未来编辑）：释放后行 `status == "reservation_lost"` 且 `error_code` 为空。
- E6b（判定钉）：该行 `should_auto_retry` 为假。
- 不变量注释落**四处**：reservation 写点 :1778/:1903（真实载体）+ 释放写点 :2804/:2954。
- 诚实措辞：钉住的是"写点不产生 error_code + 判定为假"这对事实——一旦有人加瞬时 code，
  钉测试立刻红。**不加** status 级行为守卫。
- 与既有 requirement "Lost reservations are not mark sources"（job-retry-mechanism
  spec :1194-1199，两扇 reclaim 门保持开）不冲突：本钉只管 auto-retry 分类决策，
  reclaim 门显式排除（spec delta 已落字）。

## D5: #1173 tasks-4.1 receipt（fixture review P3 修订）

归档的 `openspec/changes/archive/2026-07-27-scheduler-identity-blocked-convergence/tasks.md:39-40`
已含 2026-07-29 receipt（且佐证 D4：`error_code=null` 实机成立、released 后无新
`*_retry_N`）。**归档文件不编辑**；PR body 引用该 receipt 即可。orchestrator 可选跑一次
fresh 只读计数（`ssh … grep -c`）补记当下几何，不可达不阻塞（oracle 是本地 pytest）。

## D6: 性能

保留集计算是对已过滤行的一次线性扫描 + 每 stage 一个 max，O(n)；非热循环，无新枚举面。
Python 3.11 兼容（#1566 教训：禁 3.12+ API）。

## Seams under test

- 投影 seam：`candidate_state_from_rows`（逆序保留 / 友好序全同 / 退化 cap / 零 attempt /
  copyback-in download-out / eviction-`_failed_stage` 交互）。
- 预算 seam：`_strict_warm_start_terminal_mismatch_decision` 逆序 + `N >= retry_limit` →
  `("blocked", "strict_warm_start_retry_budget_exhausted")`。
- mint seam：manual-retry 路径（:1917 经 evidence 链至 `_pipeline_retry_job_id`）两腿（E12a/b）。

- 钉住 seam：真实 reserve→release 行 shape + `should_auto_retry`。

## Non-goals

- **DB 读路径的 SQL 截断**（D0 裁决，独立 issue 路由）；attempt 词表 `Literal`/`Enum` 化；
  `event_limit` 同类问题；跨 pass no-progress 断路器（#1118）；#1173 已合并的 L1/L2 逻辑
  本身；释放路径的行为守卫；截断策略的其它启发式。
