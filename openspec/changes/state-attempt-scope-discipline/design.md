# Design — state-attempt-scope-discipline（#1298 + #1299 + #1300 合批）

行号口径：**符号名为准，行号仅导航**（as of master fd9f2e0b；#1179 合并后）。

## D0: 批边界与风险三角

- 风险轴：**state-semantics / correctness**（跨作用域记账 → 静默 replay 死循环 / 错误
  permanent 判定）为主；test-evidence（红先行判别腿）次之。无 DB / 调度面（纯内存
  投影，oracle = 本地 pytest）。
- fixture 级别：expanded（三 issue 合批、S+S+M、强交互设计）。
- 必须保全的行为（must-preserve）：
  1. `_state_retry_attempt(state, stage=None)` 臂**逐字节不变**（evidence-owner /
     manual-retry 无 stage 消费者）。该臂"无扁平键时跨行取 max（含 cycle-scope 行）"的
     既有面是 #1579 的账，**且在决策路径上真实可达**——identity filter 的
     `_strip_top_level_pipeline_decision_fields` 会剥掉顶层 `retry_count`/`attempt`/
     `retry_attempt` 而在剥离**之后**重挂行；投影恒写顶层 `retry_count` 不等于该臂读得到它。
     本批不收窄它，但措辞不得声称"生产不可达"。
  2. canonical 臂**逐字节不变**（含对 model-less cohort 同 stage 行的既有统计——那是
     #1586 的行扫描通道面，本批不碰）。
  3. #1287 AC：`download` 列 `new_attempt == 3`（PR #1293 兑现），在 #1298 加臂后
     **不得回归**——这是本批最大的交互风险（见 D1）。
  4. #1179 floors 全部语义（selection 不变 / 收窄 / no-writeback / stage-less 不渗漏）。
  5. PR #1293 round-4 判别形（stage-blind 世界 8 / 无 floor 世界 1 / 正确世界 3）。
  6. 携 `job_id` 的真实行为活失败判定不变（cancelled 行仍算活失败——#1287 语义）。
- seams under test（上游声明，消费不重谈）：`_state_retry_attempt` 的 stage 轴、
  `_job_is_live_candidate_scope_failure` 谓词、`_failed_stage` 派生——三者都是既有缝，
  issue body 已逐个定死复现形状与判别断言。

## D1 (#1298): 非 canonical stage 的第三条臂——带 scope 纪律

`_state_retry_attempt(state, stage=S)`，`_canonical_downstream_stage(S) is None` 且
`S` 非空时：

```
return max(flat or 0,
           max(effective_retry_attempt(job_id, recorded)
               for job in _state_jobs(state)
               if _job_stage_name(job) == S            # 原始值相等，非 canonical 归一
               and not _job_is_cycle_scope_row(job),   # ← scope 纪律，见下
               default 0))
```

**为什么必须排除 cycle-scope 行（本批的交互核心）**：`_restarted_stage_family` 的成员
本身是候选作用域的（live-failure 域已减 cycle-scope 行），但 `_state_retry_attempt`
扫的是**未过滤的 cycle-wide 行列表**；`download` 这样的非 canonical stage 恰好存在
model-less、`cycle_` run-id 的 cohort 行，且 identity filter 对 source-cycle download 行
有**保留 carve-out**（`_is_source_cycle_download_stage`）。无 scope 排除的裸原始值匹配
会让 family floor / `previous_attempt` 吃到 cohort download 行的持久 `retry_count`——
即 #1300 同类缺陷在新臂上的复刻，并直接回归 #1287 的 download AC（3→8）。测试 E4 以
"删掉 scope 排除"变异钉住此设计。

- canonical 臂不对称是**故意的**：canonical 臂对 cohort 同 stage 行的统计是 master 既有
  行为（#1586 族），改它超出本批（会动 #1179 floors 的对账基线）。新臂从第一天就带纪律，
  不新增一条串味通道。
- **floors 不覆盖非 canonical stage**：`stage_retry_attempt_floors` 只记 canonical
  stage（#1179 设计）。非 canonical 臂因此是**窗口敏感**的——最大 attempt 行被截断出窗
  即读不到。显式边界：#1298 的触发形状（单 basin cycle、行数少）离 `job_limit=100` 截断
  几何很远，不为它扩 floors 键域；若未来生产出现"非 canonical 大行数"几何再议（记录于
  spec 措辞，不开新 issue）。
- `_job_is_cycle_scope_row` 从 manual_retry **下沉至 rows.py**（rows 是 import 底座，
  manual_retry / failure 都能用；manual_retry 保留同名 re-export，语义逐字节不变）。
  下沉而非复制：#1179 D5 的教训——同一判据两套实现是漂移源。

### D1.1 兄弟调用点核对（#1298 AC 第 4 条）

stage-scoped 读点（#1179 design D3.0 矩阵为底）逐个裁定"变宽是否预期"：

| 读点 | stage 来源 | 非 canonical 可达？ | 裁定 |
|---|---|---|---|
| `scheduler_candidates` 预算读点 | 常量 `"forecast"` | 否 | 不受影响 |
| `failure._failure_policy_payload`（经 `_failed_stage`）| D3 改为 `_candidate_failed_stage` | 是（download 等） | **扁平键存在时预期变宽**：候选自身 model-scoped download 失败的 attempt 真值进入分类——方向正确（E13 对照，含 `retry_limit=3` 的分类/决策后果格） |
| `failure` cancelled 分支 / manual fallback `previous_attempt` | 同上 | 是 | 扁平键存在时预期变宽，E1 主判别腿 |
| `manual_retry._fallback_previous_attempt` family floor | `_restarted_stage_family`（候选作用域） | 是 | **本 issue 的修复目标** |
| `manual_retry` 其余 / `evidence_owner` | `stage=None` 臂 | — | 逐字节不变（E3） |

"变宽"限定于**扁平键存在**：`_state_flat_retry_attempt` 为 None 时（identity filter 剥掉顶层
`retry_count` 而保留行），master 该子域走 stage-blind 的跨行 max，新臂改走候选作用域窄扫描，
取值**可低于** master。方向与 #1293 round-4 一致（正是要减掉跨 stage / 跨作用域记账），
本批以 E3 的 `_state_retry_attempt(flat_less, stage="download") == 4` 钉住该子域。

## D2 (#1299): 合成行的行身份判据 + 文本对齐

`_job_is_live_candidate_scope_failure` 前置：

```
if job.get("job_id") in (None, "") and job.get("pipeline_job_id") in (None, ""):
    return False          # _state_jobs 的无 id 合成行；合法生产行恒带 id
```

- 改动面 = 该谓词的两个消费者（`_state_has_candidate_scope_failed_job`、
  `_restarted_stage_family`），其余 `_state_jobs` 调用点（30 处）零影响——**不动**
  `_state_jobs`（备选打标方案的 32 处回归面被 #1299 明确不建议，遵从）。
- 行为变化域：**只有无 id 的合成行**。single-mapping（`pipeline_job`/`job` 内嵌 id）与
  顶层平铺**带 id** 的历史形状走 guard 的 id 分支，取值不变（E8 钉）。无 id + 顶层失败
  态的形状：arm-2 由"拒钉"变"允钉"（E6 表格，红先行）；无 id 合成行不再向
  `_restarted_stage_family` 贡献 stage（E8 一并钉住此侧翼——family 收窄方向与
  live-failure 域定义一致，fail-open 的 pin 语义不受影响，因 arm-2 本就以"无活失败"为开）。
- 文本对齐：`_state_has_candidate_scope_failed_job` docstring 的"enforced by the
  projection SHAPE … tracked by #1299"段改写为"模块自身以行身份判据兑现排除；projection
  形状是纵深而非唯一防线"；spec 挂账句同步（delta）。

## D3 (#1300): `_candidate_failed_stage`——作用域修在产生点的候选变体

新函数（`scheduler_state_failure.py`，紧邻 `_failed_stage`）：

```
def _candidate_failed_stage(state):
    # 显式键优先（projection 顶层 failed_stage/stage/restart_stage 是候选作用域的，
    # 由 projection 自身铸造——与 _failed_stage 同一循环）
    # 行扫描：跳过 repaired-stage-evidence（同 _failed_stage）+ 跳过 cycle-scope 行（新）
```

- **四个消费点切换**（fixture review P1-1 增补第四个）：`_failure_policy_payload` 的
  stage 轴（auto-retry 分类）、cancelled 分支的 `retry_policy.attempt` 派生、
  manual-retry fallback 的 `previous_attempt`，以及 **`_fallback_previous_attempt`
  内部的 family-floor gate**（`scheduler_state_manual_retry.py` 中对 `_failed_stage`
  的 lazy import 读取）——不切 gate 则 #1300 主形状下 gate 仍读到 cohort 的 canonical
  stage 而关闭 family floor，`new_attempt` 从现状 8 恶化为 1（重铸，正是 PR #1293 防的
  形状）；四点齐切才得 3。**这是对 #1300 声明边界（manual_retry.py out of scope）的
  显式偏离**：该 gate 消费的正是被修的派生，属"消费此值"而非"产生此值"的例外，记入
  PR 偏离记录。gate 切换后打开面扩大（`_candidate_failed_stage` 为 None 而
  `_failed_stage` 为 canonical 的所有几何）——方向安全（floor 只抬值、family 本身是
  候选作用域），E11 增加一条 gate 打开面回归钉。其余 `_failed_stage` 调用（重启路由、
  downstream 证据等）**逐字节不变**——修在产生点但只换真正吃 attempt 语义的消费者，
  路由/证据消费者的"cycle 行也可命名"语义留在原函数。
- cohort-only 几何（候选自身无可命名失败行）下 `_candidate_failed_stage` 返回 **None**，
  切换的消费点全部回到 stage-less / family-floor 路径（gate 同步判 None → family floor 打开）：manual fallback 走 PR #1293 的
  `_restarted_stage_family`（候选作用域）→ 正确答案 3（E9）；auto-retry 分类
  attempt 回 flat → 不误判 exhausted（E10）。**不做"回落到 cycle 行 + 分层标注"**：
  四个切换点没有一个需要 cycle stage（需要它的消费者留在 `_failed_stage`），分层返回值
  是无消费者的复杂度（YAGNI）。
- **显式键前提的事实口径**（fixture review P2-2 修正）：顶层 `failed_stage`/`stage` 并非
  恒候选作用域——`chain_repository_state` 的 `failed_stage` 可由
  `active_source_cycle_failure`（cycle-scope 行）铸造。结论不变：**显式键分支保持
  `_failed_stage` 同语义、逐字节不变**（#1287 的 download AC 依赖它——显式键铸出
  'download' 时三消费点经由它读到的行为已被 PR #1293 判别形钉住），本批只改**行扫描**
  分支的作用域纪律；显式键的 cycle-scope 铸造面并入可达枚举，不在本批收窄。
- **可达形状枚举**（#1300 AC 要求，实现时闭合、写入测试注释；E9 的头部形状在 DB 投影下
  不可直接构造——投影会把候选存活行的 stage 写进顶层——**该头部形状的真实可达通道只有
  identity filter 的 top-level 剥离（下第 4 条）**，测试注释必须点明）：
  1. 候选有存活 candidate-scoped 行 → projection 顶层 `stage` 命中显式键，行为不变
     （E11 第二腿）。
  2. 活失败是 cancelled hydro run、无存活 candidate 行 → 顶层 `stage=None`、cohort 行
     存活 → 本批修复的主形状（E9/E10）。
  3. repaired-stage-evidence 分支（`chain_repository_state.py` 的
     `elif failed_task is None and not candidate_jobs and isinstance(repaired_stage_evidence, Mapping)`）
     清空 failed job 并保留全部行，但**要求候选自身无行**，与 E9 头部形状不符；其兄弟
     nulling 块（`if restart_stage not in (None, "")` 内显式置 `stage`/`failed_stage`
     为 None）恒同时写非空 `restart_stage`，显式键循环第三个键即命中。故该分支**不产出**
     "候选有行 + 顶层无 stage 键"。E9 的 `explicit_none` 轴因此是**合成形状**，钉的是
     "键缺失 ≡ 键为 None"的等价性（防投影未来改写成写 null 时答案翻转），不是第二条
     可达通道。
  4. identity filter 对 model-less canonical cohort 行的过滤：部分状态下 cohort 行被滤掉
     （形状不可达即无害）；source-cycle download 行有保留 carve-out（download 非
     canonical，不进 `_candidate_failed_stage` 的候选行扫描——被 cycle-scope 排除）。
  5. 顶层显式键由 cycle-scope 来源铸造 → 显式键分支，行为不变（本批边界，见上）。两条
     铸造通道：`active_source_cycle_failure`（cycle-scope source-cycle download 行）铸
     `failed_stage`/`stage`；`_best_completed_stage_success_evidence` 扫**未过滤**行后
     铸顶层 `restart_stage`（`chain_repository_state.py` 的 `elif not
     _has_terminal_completion_stage_success(jobs) and (completed_stage_evidence := ...)`，
     注释自陈 state_save_qc 是无 run_id/model_id 的 cycle-scope cohort 行）。故"cohort 行
     的 stage 绝不成为候选失败 stage 轴"只在**行扫描**分支为真——显式键分支上 cycle-scope
     铸造仍可命名 stage，这是本批显式声明的边界（#1287 的 download AC 依赖它），delta 措辞
     必须带此限定。

## D4: 交互矩阵（三修复 × 关键读点）

| 读点 | #1298 臂 | #1299 guard | #1300 变体 | 净效应 |
|---|---|---|---|---|
| family floor（fallback） | 非 canonical 成员现在有真值 | 无 id 行不再入 family | gate 切至候选变体（cohort-only 时打开） | E1: 1→5 |
| `previous_attempt`（:1088 形） | download 等可解析时读真值（排 cohort） | — | cohort-only 时 stage=None→flat | E9: 8→3 |
| auto-retry 分类 | 同上 | — | 同上 | E10: exhausted 误判消失 |
| 预算读点（#1173/#1179） | 常量 forecast，不变 | — | 不变 | E14 回归钉 |
| pin arm-2 | — | 顶层 pipeline_status 不再泄漏 | — | E6: 1→5 |

三者叠加无二阶效应的论证：#1298 臂只在 stage 非 canonical 时激活，#1300 变体只改四个
切换点的 stage **来源**，二者组合的唯一交叉是"family/`_candidate_failed_stage` 给出非
canonical stage → 新臂读取"——该路径上 scope 纪律由新臂自身兑现（D1）；#1299 guard 只动
无 id 合成行，与前两者的行几何（真实行）不相交。

## D5: 平台与回归

- 纯内存、无 3.12+ API 面；CI Unit Tests（3.11）终门。
- 回归面：四套件（test_production_scheduler / orchestration_chain / gateway_reconcile /
  file_orchestration_journal）+ `_public_candidate_state` 下游套件抽查。
- 不选 pack：performance（新增一条线性扫描臂，与既有同阶）、security、release（无依赖）。
