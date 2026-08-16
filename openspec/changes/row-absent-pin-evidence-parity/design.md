# Design: row-absent-pin-evidence-parity

> 行号坐标：`services/orchestrator/scheduler_state_manual_retry.py` 与
> `chain_repository_state.py` 按 master c5996b4a；issue #1308 引用的是
> 36075f80（#1306 分支 HEAD），两者在本 change 涉及面上无漂移（已核）。

## Context

`_unresolvable_marker_entity_pins_attempt`（`scheduler_state_manual_retry.py:244-321`）
是 row-absent pin gate 唯一实现，唯一调用点是路由器
`_marker_event_pins_attempt:490-497`。row-present 侧：非 cycle-scope 行
`:495-496` **无条件 pin**；cycle-scope 行走
`_cycle_scope_marker_pins_attempt:460-488`（读行状态 =
`_job_row_is_live_failure`，#1294 共享谓词）。row-absent 侧只有两条
state 级 staleness 精确点名（`repaired_stage_evidence.original_failed_job_id`
:176-193 / `completed_stage_evidence.job_id` :195-220）+ stage 臂 + arm-2，
**读不到任何行状态/形状/model-ness** —— 双向分歧由此而来（issue A/C）。
marker 写入面 `file_orchestration_journal.record_manual_repair`（:7015 起）
已记录 `previous_job_id` 与 `failed_stage`（#1306），写入时手里就握着完整
的目标行 `failed_job`。

## Decisions

### D1: 方向 = marker 记录扩展（采纳 issue 推荐；否决墓碑与 producer 拓宽）

- **采纳**：写入面在 `details` 追加目标行的写入时字段（D2），row-absent
  臂从记录重建目标行（D3）。机制是 #1306 `failed_stage` 的直接延伸，
  写入点唯一、读取点唯一、sanitizer 白名单一处镜像。
- **否决投影墓碑**（issue 备选）：动 identity filter 证据体积与 #1288
  候选态成员边界，还需为墓碑定义「算不算行」新语义；把 pin 判据重新绑回
  投影形状——比 5 个标量键贵一个量级。
- **否决 `completed_stage_evidence` 域拓宽**（A-3/A-4 的表面解法）：该
  映射同时驱动 `restart_stage`/`restart_from_stage`
  （`chain_repository_state.py:884-886`），把 download/state_save_qc/
  publish 纳入其域会改变 restart 路由——完成判定证据与路由证据耦合，
  不在本 issue 边界内动。

### D2: 记录字段与键名（写入时快照）

`record_manual_repair` 的 `details` 追加（全部取自写入时的 `failed_job`）：

**闭包不变式（fixture review P1-1 裁定，本表的生成规则）**：记录键集
必须对 `_job_row_is_live_failure` **传递闭包内的每个行字段读取**闭合——
即其自身（status 链）+ `_pipeline_job_is_repaired_stage_evidence`
（repair_status / active_blocker）+
`_job_is_unsubmitted_auto_retry_placeholder`
（`scheduler_state_rows.py:619-631` 读 **6 个**字段：status、
manual_retry_marker、slurm_job_id、array_task_id、retry_count、job_id）
——外加路由所需的 model-ness。谓词未来加读字段时本表必须同步扩键，
实现须在测试内断言键集与谓词读集一致（tasks 2.1 防漂移断言）。

| 键 | 取值 | 服务的残留 |
|---|---|---|
| `target_status` | `failed_job["status"]` | A-1（placeholder 状态半边）、A-3 写入时已非 failed 的 legacy 形 |
| `target_repair_status` | `failed_job.get("repair_status")` | **gate 合同键**（round-1 复审 F1 改判）：`repair_status` 是投影期注解，不进持久行（`_pipeline_job_row` 白名单），当前写入面**恒缺席**——A-2 未经此键在生产收口，归 D4「写入时已被注解 repaired」永久限定；写入面补注解方案路由 follow-up issue #1482 |
| `target_active_blocker` | `failed_job.get("active_blocker")` | 同上（repaired 注解第二形态，同为投影期注解，同罪成对处置） |
| `target_model_id` | `failed_job.get("model_id")` | C（model-ness） |
| `target_slurm_job_id` | `failed_job.get("slurm_job_id")` | A-1（placeholder 无 slurm id 半边） |
| `target_retry_count` | `failed_job.get("retry_count")` | A-1（placeholder 硬短路 `retry_count <= 0 → False`） |
| `target_manual_retry_marker` | `failed_job.get("manual_retry_marker")` | A-1 反向（marker 形行不是 placeholder——缺此键会把 retry_record 目标误判 placeholder 而 under-pin） |
| `target_array_task_id` | `failed_job.get("array_task_id")` | A-1（placeholder 无 array id 半边） |

注意与 marker 自身键区分：details 已有 `retry_count`（**下一次**尝试号，
:7059）、`manual_retry_marker: True`（marker 自属性，:7073）、
`slurm_job_id: None`（:7072）——`target_` 前缀承载目标行快照，两组键
并存不冲突。数值/布尔语义：`0` 与 `False` 是有效值必须写键（sanitizer
的 `not in (None, "")` 对 0/False 均透传，实现须锚定）；None/"" 不写键，
pseudo-row 相应键缺席=行上字段缺席（谓词默认值语义一致）。记录完备性
判定见 D3 完备性 gate（`target_status` ∧ `failed_stage` **双在场**）。

键名裁定：
- 避开 `details.stage` / `details.job_type`（record-stage 消费键，同
  #1306 的 `failed_stage` 命名理由——否则 marker 事件自己会在生产终态
  口径下从候选态消失）。
- 避开 `details.model_id`：attribution knife 在 adoption 前读
  `details.model_id`（sanitizer 注释自证，`scheduler_state_identity_filter.py:487-490`）
  ——那是 **marker 归属的 model**；目标行的 model 是另一个语义轴
  （model-bearing cohort 行的 model 恒等于候选自身，#1288 已排除
  foreign，但语义上不得复用同一键）。前缀 `target_` 统一命名。
- `previous_job_id` 已有（placeholder 的 `_retry_<n>` 后缀文本半边 +
  精确 id 比较基准），不重复记录。
- 空值语义：字段值为 None/"" 时**不写键**（与 sanitizer `value not in
  (None, "")` 的既有透传口径一致）；记录完备性判定统一以 D3 的双在场
  gate 为准（`target_status` ∧ `failed_stage`），此处不另立标准。

sanitizer 白名单（`scheduler_state_identity_filter.py:487-505` retry-event
carve-out）追加同名 8 键——该 carve-out 的注释已declare「stripping this
key would strip the evidence on exactly the path it serves」，新键沿用。

### D3: row-absent 臂重构 = 记录重建行 + 复用 row-present 路由（核心机制）

`_unresolvable_marker_entity_pins_attempt` 在 cycle-grammar 与本 cycle
归属 guard（:306-311，不动）之后：

```
1. 记录完整（完备性 gate，fixture review P2-3 裁定：
   details.target_status 在场 ∧ details.failed_stage 在场——半记录
   （目标行 stage 为空、sanitizer 不透传空值产出的形）走第 2 支，
   token backstop 与 B 项既定行为不退化）→ 从记录重建 pseudo-row：
     {job_id: entity_id, status: target_status,
      repair_status / active_blocker / model_id / slurm_job_id /
      retry_count / manual_retry_marker / array_task_id:
      对应 target_* 键（仅在场键写入，缺席=行上缺席）,
      stage: details.failed_stage, run_id: 由 entity_id 文法直接判定
      cycle-scope（model-less + job_cycle 文法 ≙ _job_is_cycle_scope_row
      的语义等价，见下）}
   然后运行与 row-present 路由器同构的判定：
   - pseudo-row model-bearing（target_model_id 非空 **且等于候选自身
     model**——fail-closed 合取（fixture review Note）：非候选自身
     model 的记录不 pin，对齐 #1288/#1302「行被剔除、事件幸存」防线。
     **比较源裁定（fixture review P1-2）**：候选自身 model 从
     `state["run_id"]` 尾巴推导——`fcst_<source>_<stamp>_<model_id>`
     取 stamp 之后的**全部**（model id 含下划线，如 `model_a`；扩
     `_CANDIDATE_RUN_ID_RE` 第三捕获组或等价实现，禁 `[^_]+`）。
     **禁用 `_candidate_model_ids`**：它派生自 job 行，row-window
     truncation 把候选 model-scoped 行截光后集合为空 → 合取恒假 →
     C 残留原样复活（under-pin）。state 无 run_id / 推导不出 model →
     fail-closed 不 pin，docstring 记录）
     → **无条件 pin**（对齐 _marker_event_pins_attempt:495-496）——
     C-a（F5′ 跨 stage）与 C-b（映射点名同 stage）同时收敛；staleness
     映射对 model-bearing 不再先行拒绝（row-present 路由器就是这么做的）。
   - pseudo-row model-less → `_cycle_scope_marker_pins_attempt(state,
     pseudo_row)`——内嵌 `_job_row_is_live_failure(pseudo_row)`（共享
     谓词，经 D2 闭包不变式其全部读取字段都有替身）→ A-1/A-2 与写入时
     非 live 形收敛 by construction。
   - state 级两条 staleness 映射精确点名（:312-315）**保留在
     model-less 腿**，覆盖写入后命运（D4）——refusal 与 pseudo-row
     refusal 是并集。
2. 记录缺席或半记录（legacy / SQL retry service / #1306 前 marker /
   failed_stage 缺席形）→ 现行臂逐位保留（staleness 映射 → stage 臂
   （含 token backstop）→ arm-2）。
```

pseudo-row 的 cycle-scope 判定不走 `_job_is_cycle_scope_row`（它读
`run_id.startswith("cycle_")`，而 pseudo-row 无真实 run_id）：entity_id
已通过 `_CYCLE_SCOPE_JOB_ID_RE` guard，文法即 cycle-scope；model-ness 由
`target_model_id` 直接给出。两个谓词的语义映射在 docstring 里显式记录。

**等价性主张（新 delivered domain）**：携带记录的 marker，其 row-absent
判决与「同一目标行以记录快照的形状在场时」的 row-present 判决**同构**；
唯一保留的分歧类 = 写入后命运（D4）。

### D4: 写入后命运（post-write fate）—— 永久限定条款

记录是写入时快照，twin 读的是当前行。写入后目标行状态变化、随后行被删
的形，row-absent 侧依赖两条 state 映射：

| 写入后命运 | state 替身 | 终裁 |
|---|---|---|
| 目标被修复且 `repaired_stage_evidence` 点名 | 有（:312-313） | 已闭合（#1306 交付） |
| 目标成功且 `completed_stage_evidence` 点名（仅 `_stage_after` 有后继的 stage） | 有（:314-315） | 已闭合（#1306 交付） |
| 目标成功但被 winner-eviction 挤出 / copy-of-repaired 分支（payload 无 job_id）/ download·state_save_qc·publish 队列 stage（producer 恒 None，`chain_repository_state.py:251-256`） | 无 | **永久限定条款**进主 spec + 成对披露锚（over-pin 保留、如实钉住） |
| 目标写入后被修复但 `repaired_stage_evidence` 未点名（winner 语义） | 无 | 同上，并入同一条款 |
| **目标写入后被重新激活**（fixture review 残余风险 1：`submission_failed` 不在 `update_pipeline_job_status` 终态守卫内，:3424-3428，可能被 resubmit 回 ACTIVE——记录读 live、当前行 ACTIVE，twin refuse / 记录腿 pin） | 无 | 实现期核实 typed API 路径可达性（tasks 1.5）；可达则并入同一条款并在矩阵记录，不可达则记不可达理由（实现实测：legacy-contract 可达，已并入） |
| **目标写入时已被投影注解 repaired**（round-1 复审 F1：注解在 `payload = dict(job)` 副本上打（`chain_repository_state.py:200-203` / `chain_source_cycle.py:471-477`），写入者读持久行看不见；多 repaired + 单 winner 使「注解在、映射未点名」生产可构造） | 无（记录两键恒缺席） | 并入同一永久限定条款；`target_repair_status`/`target_active_blocker` 降级为 gate 合同键；写入面补注解属 D1 否决同量级扩张，路由 follow-up issue #1482 |

理由：这些形的闭合需要 producer 域拓宽（D1 已否决，restart 路由耦合）
或墓碑（已否决）。issue AC 的 escape hatch（「若判定不修，则搬进主 spec
显式限定条款并保留成对披露测试」原文针对 C，两处 AC 尾款同理）承载。
**A-4 的收敛范围如实声明**：succeeded 队列目标在「写入时已 succeeded」
（仅 legacy/synthesized marker 可产生——`record_manual_repair` 只选
failed 行）与「携带记录且 target_status 非 live」形收敛；「写入后才
succeeded」形属本条款。

### D5: B 项 —— token 推断上限

记录扩展后 `failed_stage` **通常**在场（#1306 起写入；但目标行 stage 为
空时 sanitizer 不透传，产出半记录——该形走 backstop 臂，见 D3 完备性
gate，P2-3），token 回落域 = 真 legacy 集 + 半记录形。处置：保留回落 + 合同测试（stage-less legacy
marker 的显式判决）+ 一条 `token != row 实际 stage` 用例锁既定行为 +
docstring 与 spec 双限定「token 是文本推断非记录证据，上限于 legacy 集 + 当前写入面产出的半记录形」。

### D6: D 项 —— compaction 域测试锚 + spec 括注修正（独立小修）

- journal 测试锚：完成阶段（parse/state_save_qc/publish）model-less
  cycle-scope 队列 marker 事件经 `_compact_cycle_scope_event`
  （`file_orchestration_journal.py:8808`，挑选 :722、施加 :793）投影后
  `details` 整块丢弃 → `_manual_retry_marker_shape` 不成立 → **从不被
  采信**（不是「退回 id-token backstop」）。锚断言：投影后事件无
  `details` 且 pin gate 对其无感。
- spec 修正：现行尾句括注「the journal read path's completion-stage
  compaction domain **keeps the disclosed id-token backstop**」与代码
  不符（marker-shape 需要 details，丢 details = 整个 marker 不被采信）
  ——delta 改为「journal 路径该三 stage 的 marker 事件失去 details、
  不被采信，pin gate 的 journal 活域 = 提交阶段」。

### D7: 可达形状与测试构造纪律

- 残留矩阵 `test_same_stage_marker_target_staleness_residue_matrix`
  （`tests/test_production_scheduler.py`，#1306 交付）：两分歧格
  `unsubmitted_placeholder` / `repaired_flag_not_named_by_the_state`
  期望值 `(False, True)` → `(False, False)`。**红-绿协议**：格子的
  marker 构造改为携带写入面口径的 `target_*` details，修前该构造仍
  `(False, True)`（gate 不读新键）→ 红；修后收敛 → 绿。**收敛口径
  修正（round-1 复审 F1）**：`unsubmitted_placeholder` 格是写入面可产
  形的真收敛；`repaired_flag_not_named_by_the_state` 格的
  `target_repair_status` 属 gate 合同键（写入面恒缺席），该格按 2.2
  先例显式标注为域外合同锚，**不得**声称生产写入面收敛——A-2 的生产
  人群归 D4「写入时已被注解 repaired」条款。防漂移断言只证键 schema
  一致，不证可产值，docstring 不得作反向断言。
- 写读全链路（integration pack）：真实 `record_manual_repair` 写 marker
  → 真实投影 + identity filter → 决策态上验证新键存活与判决——至少一条
  端到端锚，防「写了键但 sanitizer 没放行」类断裂。
- legacy backstop 回归：无 `target_*` 键的 marker 在全部既有形状下判决
  逐位不变（既有矩阵格 + #1292 交付锚全绿即证）。
- C 成对用例：cross-stage（F5′ 形）与 same-stage（映射点名形）各一对
  （row-present 路由器 vs row-absent 记录腿），断言两侧一致 pin。
- D4 永久限定成对披露锚：winner-eviction 形与队列 stage succeeded 形
  各一对（row-present refuse / row-absent pin），钉现状 + docstring
  指向 spec 限定条款。

## Invariant Matrix

Governing invariant: row-absent pin 判决对「记录携带的写入时形状」与
row-present 路由对「同形状在场行」的判决同构；分歧只允许存在于写入后
命运且必须被 spec 永久限定条款逐形枚举。
Source-of-truth identity/contract: 目标行形状证据 = marker 记录的
`target_*` 键族（写入面 `record_manual_repair` 唯一产生，sanitizer
白名单唯一放行面，pin gate 唯一消费）；行级 live-failure 域 =
`_job_row_is_live_failure`（#1294 共享谓词，pseudo-row 复用同一对象）。
Surfaces:
- Producers: `record_manual_repair`（details 扩展）；SQL retry service
  不动（legacy 形）
- Validators/preflight: `_manual_retry_marker_shape` 不动（新键不参与
  shape 判定）
- Storage/cache/query: journal 事件持久化透传；compaction（D6 只锚不改）
- Sanitizer: identity-filter retry-event 白名单 +8 键（镜像义务）
- Consumers: `_unresolvable_marker_entity_pins_attempt`（唯一读取点）；
  路由器与 twin 不动
- Failure paths: over-pin（attempt 复用已消耗号）/ under-pin（运维钉值
  丢弃）——修复靶点；legacy backstop 行为逐位保留
- Evidence/audit: decision evidence 的 `new_attempt` 语义在记录携带域
  内与 row-present 一致
Regression rows:
- 矩阵两格 (False,True)→(False,False)（红-绿协议，D7）
- legacy 无记录 marker：全部既有锚逐位不变
- 半记录形（target_status 在场、failed_stage 缺席）：判决与同形 legacy
  marker 逐位一致（tasks 2.7 归属锚）
- C 两对 + truncation 防线格（自身 model 记录 + 无任何 model-scoped
  行 → 仍 pin）：row-present 与 row-absent 同 pin，model-ness 不得从
  幸存行派生
- D4 两形成对披露：row-present refuse / row-absent pin 钉现状
- B：stage-less legacy 合同 + token≠stage 上限用例
- D6：compaction 锚 + spec 括注修正对读
- sanitizer：新键存活 + 既有键行为不变（全链路锚）

## Risks / Trade-offs

- pseudo-row 是内存构造，键名 `stage` 只存在于内存 dict，不落盘——与
  「details 不得用 `stage` 键」不冲突；docstring 显式声明。
- model-bearing 无条件 pin 对齐路由器，意味着映射点名的 staleness 对
  model-bearing 目标不再拒绝（C-b 方向收敛到 row-present 语义）——这是
  「与 twin 一致」的定义本身；若未来判定路由器 :495-496 过宽，两侧一起
  改（单点：路由器语义）。
- 写入后命运的 over-pin 保留为披露——#1186 接上后该形仍可能钉stale
  目标；缓解：范围被 D4 条款精确枚举，且两条映射覆盖主干命运。
- evidence 体积：8 标量键/marker 事件，可忽略；不进
  `_CYCLE_SCOPE_JOB_PROJECTION_KEYS`（那是 job 行投影，不相干）。

## Migration

无数据迁移。旧 marker 无新键 → backstop 臂，行为不变。主 spec 随
archive 回写。
