# Design: candidate-projection-stage-attempt-retention

Fixture level: expanded
Project profile: NHMS

> **round-1 cross-review 后机制修订（v2）**：初版机制是"截断时保留每 stage attempt 上界行"。
> PR #1574 round-1 评审 + 独立 verifier 以差分探针证伪了该机制的可见性安全性
> （S1 latest_job 翻转把已 publish 候选读成 failed、S2 保留行挤掉 completed-stage 成功证据、
> S3 陈旧 ACTIVE 行复活、S4 flat retry_count 被挤压低——S1/S2 与未截断真值矛盾）。
> 机制改为 **attempt-floor 载带**：`pipeline_jobs` 选集回到纯新鲜度 `[:job_limit]`
> （逐字节与改前一致，可见性零变化），截断前从全量投影输入按消费链提取
> `stage_retry_attempt_floors`，stage-scoped attempt 推导读取时并入 floor。
> proposal 的原不变量措辞——"保护的是数值上界，不是行群体"——正是本方向；
> 初版把不变量过度具体化成了行保留机制。

## Change surface

- `services/orchestrator/chain_repository_state.py` `candidate_state_from_rows` 截断块——
  选集回退为纯新鲜度 `[:job_limit]`；截断前构建 floors 并落 state 新键
- `services/orchestrator/scheduler_state_rows.py` `_state_job_retry_attempt`——stage-scoped
  推导并入 floor（stage-less flat-first 逐字节不变）；floors builder 同时产出贡献行
  identity 元数据
- `services/orchestrator/scheduler_state_identity_filter.py`——floors 随候选域过滤收窄
  （D1.6）+ strip 列表加两个键
- `tests/test_production_scheduler.py`：逆序几何回归腿 + 三个已证伪几何的回归钉 +
  既有友好序用例 `test_strict_warm_start_budget_binds_on_the_truncated_production_geometry`
  (:42219) 保持
- 只核对不改码：`scheduler_candidates.py:2225`（L2 预算）、`scheduler_state_failure.py`
  :188/:1444/:1900/:1917、`scheduler_state_manual_retry.py:982`、
  `scheduler_state_evidence_owner.py:110`
- 钉住（不改行为）：`file_orchestration_journal.py` reservation 写点 :1778/:1907
  （`error_code: None` 的真实载体）+ reclaim 重 seed 写点（C2）+ 释放转换两处 + `retry.py`
  `classify_failure`/`should_auto_retry` 链
- 文档：`docs/runbooks/failed-basin-retry.md`（机制描述随 v2 更新）

## D0: 范围裁决——file-journal 投影 only（fixture review P1-1；round-1 C3 补充）

**DB 读路径在 SQL 里就截断**：`chain_repository_state.candidate_state` 的查询
（:519-525 `ORDER BY COALESCE(updated_at, …) DESC … LIMIT %s`，:535 绑定 `job_limit + 1`）
用与 `_pipeline_job_truth_sort_key` 相同的新鲜度序在数据库侧丢弃旧行——逆序几何下携带最大
attempt 的行**根本到不了投影层**，投影层载带救不了它。该路径 live
（`chain_repository.py:126` → 默认 repository @ `chain_forecast_orchestrator_cycle.py:79`）。

裁决：guarantee **收窄到 file-journal 投影路径**（`file_orchestration_journal.py:887` 读
cycle 全行后投影——生产实际路径，#1173 归档 receipt 佐证）；DB 缺口已路由 #1572。

**共享函数的事实澄清（round-1 C3）**：`candidate_state_from_rows` 是两条路径共享的投影函数，
DB 路径喂进来的 `job_limit+1` 行窗口上 floors 同样会被计算——这是**纯数值面**的顺向改良
（+1 行的 attempt 可抬高 floor），**行选集在两条路径上都逐字节不变**（v2 机制不改选集）。
"outside this guarantee" 指的是窗外上界行救不回来的保证边界，不是"DB 路径行为一个比特
都不变"。

## D1: attempt-floor 载带（核心不变量，v2）

**不变量：截断不得改变 attempt 推导的结果——对每个 canonical downstream stage，
`_state_retry_attempt(state, stage=S)` 在截断后投影上的返回值 == 在未截断输入上的返回值。
保护数值上界，不动行群体。**

实现：

1. **选集不变**：`pipeline_jobs` 维持现状纯新鲜度倒序切 `[:job_limit]` 再正序回排，
   逐字节与改前一致。所有由行群体派生的 state 键（`pipeline_status`/`stage`/`failed_stage`/
   `restart_stage`/completed-stage 证据/active 扫描/`retry_count` 聚合/`latest_job`）
   自动与改前全同——S1/S2/S3/S4/C3 的结构性修复。
2. **floors 构建**：截断前对全量（post-filter）行一次线性扫描，得
   `{canonical_stage: max_effective_attempt}`（仅收非零；attempt 0 无信息量）。
   stage/attempt 推导**必须与消费链同构且同函数**：
   `_canonical_downstream_stage(_job_stage_name(job))` + `effective_retry_attempt(job_id,
   retry_count)`（builder 放在 `scheduler_state_rows`，与消费者同模块，杜绝分叉）；
   **禁止** `chain_repository_state._STAGE_ALIASES`（含 download 漏 copyback）、禁止
   纯 job-id 子串解析。round-1 C1 钉死两个必须进腿的行形：attempt 只活在持久化
   `retry_count` 的行（如 `retry.py:1101-1116` mint 的 `_retry_active` + :1013 的
   `retry_count=N`）与 stage 空只有 `job_type` 的行（`_job_stage_name` 回退半边）。
3. **state 新键** `stage_retry_attempt_floors`（恒在，可为空 dict）。新键对既有 Mapping
   消费者惰性（全部走 `.get`/显式键读取，无 key 枚举面）。
4. **读取并入**：`_state_job_retry_attempt(state, canonical_stage)` 在 canonical_stage
   非 None 时把 `floors.get(canonical_stage, 0)` 并入 max。**stage-less flat-first 语义
   逐字节不变**（:440-444 docstring 已钉——floors 绝不渗入无 stage 读取）。非本投影产出的
   state（无 floors 键）行为不变。
5. **DB 路径**：同一 builder 在 `job_limit+1` 窗口上跑（D0 澄清条），无门控——门控会让
   两条路径分叉，代价高于收益（round-1 C3 verifier 口径）。
6. **identity 收窄（round-2 R2-A，P1）**：floors 必须随候选域过滤收窄。floors 条目携带
   贡献行 identity 元数据（平行键 `stage_retry_attempt_floor_sources`：stage → 贡献行
   whitelist 投影列表，字段覆盖 `_legacy_identity_values` 别名 + job-id 引用键 +
   source-cycle blocker 谓词读的 status/stage 字段；同 stage 取到 max 的全部行都算贡献
   行）。`scheduler_state_identity_filter` 在重写行群体的同时，**仅当某 stage 的全部贡献
   行被同一 authority/scope 谓词判出局时**删除该 stage 的 floor 条目。三处谓词：
   - authority（evidence 路径 :69-79 的语义）——**落在公共尾 `_candidate_state_filtered_
     decision_state`，不是 :69-79 行循环内**：行删除按 `legacy_sources` 的**下标**走，
     而贡献行没有下标（它可能在任何 filter 看到之前就被截断出行集），所以只能重跑
     `_candidate_state_identity_validation` 判定所依据的谓词
     `_state_row_has_authoritative_candidate_proof`（+ source-cycle blocker 逃生门）；
     放公共尾还顺带堵住"legacy_sources 为空即整函数早退"的漏洞——那条路径上窗外外来
     贡献行本来一次也不会被判。
   - `_inconclusive_source_cycle_decision_state`（:147）：`_state_row_references_job_ids`；
     跑在该函数自己的 :156 strip 之前，是活收窄（E13f 钉，删调用必红）。
   - `_candidate_scoped_shared_cycle_aggregate_state`（:320）：
     `_shared_cycle_row_is_candidate_scoped`（+ blocker 逃生门）。该臂的**非** blocker 分支
     :337 无条件 strip，floors 由 strip 半边兜住（E13b/E13d）；收窄调用只在 blocker 分支
     且 top-level blocker 成立时（:316-317 跳过 strip）承重（E13e 钉）。**round-3 R3-B 已删**
     非 blocker 分支上原有的第二次收窄调用——它恒在该无条件 strip 之后拿到空 floors，行为等价。
   - **round-3 R3-A 第四处**：`scheduler_candidates.py:2238` 的 raw 读点前直接调
     `_candidate_authoritative_stage_retry_attempt_floor_state`（见 D3.0）。
   - 逃生门与可达性备注：两处 `_global_source_cycle_download_blocker_job` 逃生门**当前恒不
     可达**（floor 贡献行的 stage 必是 canonical downstream stage，`download` 不在其中），
     与行循环谓词同构保留、防 download 转正漂移，代码内已注明。:316 那处的门控前提
     `_top_level_source_cycle_download_blocker` 拿 **state 顶层 `run_id`** 与候选身份比，
     而任何 producer 写的都是候选自己的 run_id——**当前无投影可产出该形状**（round-3 实测；
     该谓词是 master 既有代码，口径问题不在本 change 范围）。E13e 因此是 guard 钉：几何用
     真实投影 + 替换该一个字段，作用是让这条收窄不能被当死代码删掉。
   `STAGE_RETRY_ATTEMPT_FLOORS_KEY`（连同 sources 键）同时加入
   `_strip_top_level_pipeline_decision_fields`（:678 起）——它与 `retry_attempt`/
   `attempt`/`retry_count` 同列；在 shared-cycle-aggregate 臂上这半边是**承重**的
   （E13b 走的就是它）。**陷阱（verifier (e) 裁定）**：
   不得"从过滤后幸存的 pipeline_jobs 重算 floors"——E1/E5 的贡献行在 filter 之前就已被
   截断出行集，重算会把 floor 归零打死整个 change；E1/E5 的 wedge 行（裸 cycle run id、
   model-less）在 `_state_row_has_authoritative_candidate_proof` 下为 True，收窄式修法
   保留其 floor。反向锚：tests/test_file_orchestration_journal.py:1493-1505（非候选行
   retry_count 不得成为本候选 attempt）。
7. **flat 分量边界（round-2 R2-C，双镜头证实）**：floors 只保证 **stage 行扫描分量**的
   截断不变；候选级 flat `retry_count` 聚合（chain_repository_state.py:829/:867，本 change
   未动）仍随窗口塌陷，其跨 stage 串味（convert 行 retry_count 经 flat 通道进 forecast
   预算）是既有行为，显式排除在保证外（follow-up issue **#1579**）。**归因按通道分**：
   #1579 只管 flat 通道；**行扫描通道**在 :2238 预算读点上的窗内 cycle-wide 串味是另一个
   既有面，见 **#1586**。spec、
   本节与 `_state_retry_attempt`/`_state_job_retry_attempt` docstring 的措辞一律按此收窄，
   不得写绝对的"截断不变等式"。

## D2: 必须保持（v2 后大幅收缩——选集不变使可见性面按构造关闭）

- `state_truncated`（:900 附近）/ `pipeline_jobs_total`（:898）语义不变。
- `event_limit` 截断完全不动；`_record_allowed_for_compute_state_terminal` 过滤先于 floors
  构建（floors 只看进入投影的行——与行扫描的可见域一致）。
- **选集与派生键零变化是本设计的承重墙，必须用 round-1 证伪几何做回归钉**（E-v2 腿）：
  - S1 再入几何（candidate-scoped retry 行 + 更新 run 成功行全窗外 + cycle-scope filler）→
    `pipeline_status`/`failed_stage`/`latest_job` 派生与改前全同（None/None），
    **且** `_state_retry_attempt(state, stage="forecast")` 读出真值；
  - S2 几何（窗外 wedge + 窗内 completed-stage 成功行）→ `restart_stage='forcing'`、
    completed-stage 证据保持；
  - S3 几何（窗外 ACTIVE running + slurm binding 行）→ `_state_active_jobs` 为空、决策不变；
  - S4 几何（retry_count=4 载体行在窗内）→ `state["retry_count"] == 4` 保持。
- 既有 :42219 用例是回归钉，**不得放宽**。

## D3: 消费面核对 + 行为边界（v2 修订）

### D3.0 消费者矩阵（round-3 retro 纠正动作；下一轮 review 的 focus 首条）

不变量：**载带的 stage-attempt 真值必须在每一个消费点上受与行群体相同的候选身份/可见域
纪律约束**。矩阵按 `grep -rn '_state_retry_attempt(' 与
'STAGE_RETRY_ATTEMPT_FLOORS?_?…_KEY'` 全集列出。**本文档全部行号以符号名为准，行号只是
写作时的快照、仅供定位参考**——随后的编辑会让它们漂移，核对请按符号名 grep，不要把行号
当作断言。判据只有两条：
**stage-less 读点结构性不可能读到 floors**（`_state_retry_attempt` 的 `canonical_stage is
None` 分支走 flat-first / `_state_job_retry_attempt(state, None)`，后者不并入 floor——
E12'' 钉）；**stage-scoped 读点必须吃已收窄的 state**。

services/（生产读点）：

| 读点 | stage 参数 | state 来源 | 收窄 | 结论 |
|---|---|---|---|---|
| `scheduler_state_failure.py:188` `_failure_policy_payload` | `_failed_stage(state)` | 8 个调用方（:418/:473/:1481/:1557/:1641/:1689/:1723/:1914）各自的 `state`，全部由 `scheduler_state_decision.py` 以 `decision_state` 传入 | 是（`_candidate_state_decision_state` 公共尾） | 安全；E13a/E14 钉 |
| `scheduler_state_failure.py:1444` `_completed_upstream_stage_retry_evidence` | `restart_stage` | decision.py:145 `decision_state` | 是 | 安全 |
| `scheduler_state_failure.py:1900` `_cancelled_state_evidence` | `_failed_stage(state)` | decision.py:382 `decision_state` | 是 | 安全 |
| `scheduler_state_failure.py:1917` `_manual_retry_state_evidence` | `_failed_stage(state)` | decision.py:273 `decision_state` | 是 | 安全；E15 钉 |
| `scheduler_candidates.py:2238` strict-warm-start L2 预算 | 常量 `"forecast"` | **raw** provider 直出（decision_state 是 `_candidate_state_decision_evaluated` 的局部量，不回流） | 部分——**读点前显式施加** `_candidate_authoritative_stage_retry_attempt_floor_state(state, terminal_evidence)`（round-3 R3-A 修法 1a），**只有 authority 一臂**；跳过 inconclusive（:147）与 aggregate（:320）两臂 | E16 钉；**禁止**改用完整 `_candidate_state_decision_state`（aggregate 臂 strip 打死 E5） |
| `scheduler_state_manual_retry.py:982` `_fallback_previous_attempt` | `_restarted_stage_family(state)` | 唯一调用链 `_manual_retry_new_attempt` ← failure.py:1918，同 `decision_state` | 是 | 安全 |
| `scheduler_state_manual_retry.py:115/:125` marker 默认 attempt | 无 | `_manual_retry_markers(state)` | 不适用 | stage-less，floors 结构性不可达 |
| `scheduler_state_manual_retry.py:693/:705` blocker 记录 | 无 | `_latest_manual_retry_blocker(state)` | 不适用 | 同上 |
| `scheduler_state_evidence_owner.py:110` evidence `retry.attempt` | 无 | **raw**（evidence 在 filter 之前构建） | 不适用 | 同上——这是 raw 读点仍安全的唯一理由，改成 stage-scoped 必须同时接收窄 |
| `scheduler_state_rows.py:545` `_state_stage_retry_attempt_floor` | — | 仅被 `_state_job_retry_attempt` 在 `canonical_stage is not None` 时调用 | — | floors 的唯一读函数，无旁路 |
| `scheduler_state_identity_filter.py:191/:194/:208/:213-215` | — | 收窄器 `_narrow_stage_retry_attempt_floors` 自身 | — | 三处调用点：:125 authority（公共尾，承重）、:147 inconclusive 臂（E13f）、:320 aggregate-blocker 子分支（E13e） |
| `scheduler_state_identity_filter.py:720-721` strip 列表 | — | `_strip_top_level_pipeline_decision_fields` | — | 与 `retry_attempt`/`attempt`/`retry_count` 同列；aggregate 臂 :337 无条件 strip 由它承重（E13b/E13d） |
| `chain_repository_state.py:693` | — | 写点（唯一 producer） | — | `stage_retry_attempt_floors(jobs)` 于截断前构建 |

tests/（读点性质，逐条不列）：`tests/test_production_scheduler.py` 内 stage-scoped 读点
分三类——(1) `_retention_*` 投影 raw state 上直接读（:42296/:42450/:42507-42508/:42555-42558/
:42618-42622/:42650-42651/:42702/:42759/:42833/:42882/:42907/:42943）：**有意读 raw**，钉的是
floors 载带本身（截断不变量），不涉及候选身份面；(2) `decision_state` / `_retention_decision_state`
上读（:5862/:5940/:6006/:6256-6257/:43081/:43128-43133/:43178-43180/:43356-43357/
:43436-43446/:43491-43502 + E14/E15 消费点）：钉收窄后的真值；(3) 手写 state（:12205/:12261/
:12337/:12345/:12355-12356/:12443、:42645 `foreign`）：不带 floors 键，钉"非本投影产出的
state 行为逐字节不变"。`tests/test_file_orchestration_journal.py:3466` 走 decision_state。
`tests/test_production_scheduler.py:5700` `_production_previous_attempt` 是生产 composition
的镜像 helper，随其调用方吃到的 state 走。

矩阵的维护义务：**新增任何 stage-scoped 读点，必须在此表登记并说明它吃的是哪层 state**。

### D3.1 各消费点行为边界

stage-scoped 调用点逐一核对并在 PR 记录结论：

- `scheduler_candidates.py:2238`（auto L2 预算，stage 是常量 `"forecast"`）：floors 直接
  命中——逆序几何下 `("blocked", "strict_warm_start_retry_budget_exhausted")`（E5）。
  **这是 issue #1179 的目标面，不依赖 `_failed_stage` 行可见性。** round-3 R3-A 修订：
  该读点吃 raw state，收窄在读点上显式施加（D3.0 表 + D1.6 第四条），窗外 cohort 行的
  attempt 不再花候选的预算（E16）。**窗内串味按通道分别归因**：floors 通道已收窄；
  flat `retry_count` 通道未收窄，是既有面 **#1579**；**行扫描通道**未收窄，也是既有面，
  见 **#1586**。
- **`scheduler_state_failure.py:188/:1444/:1900` 是决策级变化，不只是记账（round-2 R2-B
  修订）**：stage 可命名时 `_failure_policy_payload` 的 attempt 读出真值，`classify_failure`
  在真值 ≥ retry_limit 时把 transient 失败判成 `permanent=True / limit_exhausted=True`，
  remedy 通道被 permanent 门（failure.py:418/:473/:1481/:1557/:1641/:1689/:1723）关闭、
  `_permanent_failure_evidence` 由 None 变 ("permanent_failure","retry_limit_exhausted")。
  **这是本 issue 预算绑定语义的有意后果，必须有腿钉住**（E14）。注意这些消费点吃的是
  identity-filtered `decision_state` 而非 raw state——floors 的 identity 收窄（D1.6）是
  该面正确性的前提。
- **`:1917` manual-retry mint 分两支（round-2 R2-D 修订）**：`:1917` 是
  `_state_retry_attempt(state, stage=_failed_stage(state))`。**stage 可命名支**（候选权威的
  failed/cancelled 行在窗内——"failed 行在窗内"不等于"最大 attempt 行在窗内"，两者可分离）：
  最大 attempt 行在窗外时 mint 由窗口局部值（如 `_retry_3`）变为真值 `_retry_{N+1}`——
  持久化身份变化，本 change 的有意行为，必须有腿钉住（E15）。geometry B 支（三键全空 +
  行在窗外）：`_failed_stage` 恒 None → stage-less flat-first 读 0 → mint `_retry_1` 撞键
  静默 no-op——**维持现状，不在本 change 修**（E11-v2 硬断言 + E12-v2 现状钉，#1577 路由：
  manual mint 需要 stage 解析不依赖行可见性）。
- `manual_retry:982`（family floors）：`_fallback_previous_attempt:978-983` 早退语义不变
  （选集不变）。
- 无 stage 调用点走 flat-first，**floors 不得改变其行为**（E12''-v2 变异钉）。

## D4: released 行钉住（不改行为；round-1 C2/C4 修订）

现状保护机制：`error_code` 为空来自 **reservation 写点**（:1778/:1907 显式
`"error_code": None`——:1907 是 reclaim 重 seed 写点，round-1 C2 证实它同样 seed released
行且此前无钉）；释放转换 `apply_accepted_submit_transition`
（accepted_submit_identity.py:310-327）整行拷贝不触碰 `error_code`。
`should_auto_retry` → `classify_failure(None)` → `UNKNOWN_FAILURE` 不可重试
（`SLURM_RESERVATION_LOST` 在 transient 集合 retry.py:37——未来在写点盖瞬时 code
即打开重复提交）。钉法：

- E6a（shape 钉，真实 reserve→release 序列）：释放后行 `status == "reservation_lost"` 且
  `error_code` 为空。
- E6b（判定钉）：该行 `should_auto_retry` 为假。
- **E6c（round-1 C2）**：reclaim 链路钉——reserve → permit（absence_retry_permitted）→
  `reclaim_pipeline_job_reservation`（:1835-1839 前置条件即该形状；:1911 重 seed
  `error_code: None`）→ release，断言 released 行 `error_code` 为空 + `should_auto_retry`
  为假（:1911 塞瞬时 code 的变异必须咬红）。
- 不变量注释：reservation/reclaim 写点 + 释放转换两处。**`permit_pipeline_job_retry`
  处（round-1 C4）措辞必须写成**："本行产出 absence_retry_permitted——spec 显式排除的
  reclaim door 之一，null `error_code` 在此只是转换未引入新码的事实；auto-retry isolation
  契约的主语是 identity_mismatch_released 子形。"不得把 isolation 契约的帽子扣在有意可
  重试的行形上（`_verified_accepted_submit_forecast_retry`
  chain_forecast_orchestrator_cycle.py:899-908 与 reclaim 谓词 :1835-1839 吃的正是它）。
- 与既有 requirement "Lost reservations are not mark sources"（job-retry-mechanism
  spec :1194-1199，两扇 reclaim 门保持开）不冲突：本钉只管 auto-retry 分类决策。

## D5: #1173 tasks-4.1 receipt（不变）

归档的 `openspec/changes/archive/2026-07-27-scheduler-identity-blocked-convergence/tasks.md:39-40`
已含 2026-07-29 receipt（佐证 D4：`error_code=null` 实机成立、released 后无新
`*_retry_N`）。**归档文件不编辑**；PR body 引用即可。

## D6: 性能

floors 构建是一次线性扫描 + 每 stage 一个 max，O(n)；非热循环。Python 3.11 兼容
（#1566 教训：禁 3.12+ API）。

## Seams under test

- 投影 seam：`candidate_state_from_rows`（floors 正确性 / 选集逐字节不变 / 三个证伪几何
  回归钉 / C1 两个行形）。
- 读取 seam：`_state_job_retry_attempt` floor 并入 + stage-less flat-first 不变。
- 预算 seam：`_strict_warm_start_terminal_mismatch_decision` 逆序 + `N >= retry_limit` →
  `("blocked", "strict_warm_start_retry_budget_exhausted")`。
- 边界 seam：geometry B 下 `_failed_stage` 保持 None + mint 现状钉（follow-up issue 路由）。
- 钉住 seam：真实 reserve→release + reclaim 链路行 shape 与 `should_auto_retry`。

## Non-goals

- **DB 读路径的 SQL 截断**（D0 裁决，#1572）；geometry-B 下 manual mint 的 stage 解析
  （follow-up issue，v2 显式边界）；attempt 词表 `Literal`/`Enum` 化；`event_limit` 同类
  问题；跨 pass no-progress 断路器（#1118）；#1173 已合并的 L1/L2 逻辑本身；释放路径的
  行为守卫；截断策略的其它启发式。
