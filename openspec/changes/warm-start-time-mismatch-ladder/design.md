# Design: warm-start-time-mismatch-ladder (#1431)

## Risk triage

- Fixture level：**compact**（issue 无 suggested level——needs-triage 单，
  owner 已拍板裁决 C；M 规模单文件 lane，偏离记录在案）。
- Risk packs：terminal-state-semantics（三类坏快照终态统一 + escalate 新
  终态）、oracle-integrity（红证 + 既有锁改写不失守）。
- 裁决记录（AC-1）：三类坏快照终态一处说清——**checksum 不符**（阶梯，
  since 根提交）/ **头部形状畸形**（阶梯，#1429 转译）/ **头部时间不符**
  （本 change：阶梯 + 全-TIME_MISMATCH≥2 耗尽时 systemic escalate）。
  理由：单坏快照按坏快照处置（与前两类一致）；系统性时间漂移（rekey 成
  批坏）不该被逐个降级降解成低噪告警——耗尽点全员一致即响。

## D1 — 修法（fixture review round-1 修订）

`workers/shud_runtime/runtime.py` `_stage_initial_state`：

1. `:1298-1300` except 臂改为：
   `IC_TIME_SHIFT_HEADER_INVALID` → 照旧（`error_message = error.message`）；
   `WARM_START_TIME_MISMATCH` → 若 `_exact_warm_start_required(manifest)`
   先 `self._clear_staged_initial_states(input_dir)` 再**裸 `raise`**
   （P2-1：材料化坏 IC 不残留，与 `:1326` checksum 臂卫生一致；裸
   raise 保原码/原消息/原 traceback，「逐字节」限定于 error 语义）；否
   则 `error_message = f"WARM_START_TIME_MISMATCH: {error.message}"` 并
   置本轮 mismatch 标志；其它 code → 照旧 `raise`。
2. mismatch 标志**在 while 体首行（`:1268` state_id 取值之前）复位**
   （P2-2：checksum/staging 失败迭代不进 try，复位若在 except 内则上轮
   True 漏到本轮，假一致触发 escalate）。循环外初始化
   `rejections = 0` / `time_mismatch_rejections = 0` /
   `pending_mismatch_marks: list = []`；走到 `:1315` 拒绝通道时
   `rejections += 1`，mismatch 标志真时 `time_mismatch_rejections += 1`。
   **URI-only 候选（`state_id` 为假）照常计入两计数**——是真实候选拒
   绝；只可能出现在第 1 轮（后续候选均来自 `_next_usable_state` 必带
   id），不破 unanimity/AC-2；无 QC 记录，故 escalate 消息必须带两计数
   + 最后一条 mismatch 消息。
3. **mark 缓存（P1-1 裁决 (a)，忠实于 owner C「每 cycle 都响」）**：
   TIME_MISMATCH 拒绝的 `_mark_init_state_corrupted` 调用不即时落库，
   入 `pending_mismatch_marks`；checksum/形状拒绝的 mark 照旧即时。
   flush（逐个调 mark）发生在**除 systemic escalate 外的所有循环出
   口**：`:1313` warm 成功 return 前、`:1287` 无 URI 冷启动 return 前、
   `:1335` 耗尽冷启动 return 前、`:1327` exact-warm UNAVAILABLE raise
   前。escalate 出口**不 flush**——快照保持 usable，下一 cycle 阶梯重
   走、再次 escalate，系统性信号持续（今日对照：mismatch 逃逸早于
   mark，快照保持 usable、每 cycle 都响；(a) 保住该性质）。循环终止只
   依赖内存 `rejected_state_ids`，不依赖落库 mark（review 已核实）。
4. `:1333 next_state is None` 分支：若 `time_mismatch_rejections >= 2
   and time_mismatch_rejections == rejections` →
   `self._clear_staged_initial_states(input_dir)` 后
   `raise SHUDRuntimeError("WARM_START_TIME_MISMATCH_SYSTEMIC", <消息含
   两计数与最后一条 mismatch 消息>)`（不 flush pending marks）；否则
   flush 后照旧冷启动三行。`:1281-1289` 无 URI 提前冷启动**不评估
   escalate**（即便已记 2 次 mismatch）——escalate 只在 `:1333` 耗尽点。
5. 其余零改动：两处 raise（`:1650`/`:1657`）、`_verify_ic_time_consistency`
   判据、checksum 拒绝路径与其即时 mark、`_mark_init_state_corrupted`
   签名、`_next_usable_state`。
6. **retry 分类 parity（review Note 2 钉住）**：新码
   `WARM_START_TIME_MISMATCH_SYSTEMIC` 不注册进任何集合——
   `services/orchestrator/retry.py` 归 `unknown_failure`/不可重试/
   permanent，与今日 `WARM_START_TIME_MISMATCH` 完全同档；receipt 原样
   透传（`runtime.py:2177-2189`）。禁止后人加进
   `TRANSIENT_ERROR_CODES`（= 对永久数据缺陷开重试循环），seam 10 钉。
7. 模块级常量注释 `runtime.py:60-68`（"still fails the whole run"）随
   本 change 改真（P3-2）。

## D2 — 终态表

| 几何（非 exact-warm，除注明） | 今日 | 新 |
|---|---|---|
| 单快照 TIME_MISMATCH，邻座有好快照 | 整 run 失败（TIME_MISMATCH 冒泡） | mark corrupted（消息带 token）→ 换邻座 warm 继续 |
| 单快照 TIME_MISMATCH，无邻座 | 整 run 失败 | mark → 耗尽（1 拒绝 <2）→ `cold_start_no_state` 冷启动 |
| ≥2 快照全 TIME_MISMATCH 耗尽 | 整 run 失败（第一个即打掉） | 耗尽点 raise `WARM_START_TIME_MISMATCH_SYSTEMIC`（清 staged；**pending mark 不落库**——快照保持 usable，下一 cycle 重走阶梯再次 escalate，信号每 cycle 持续） |
| 混合拒因耗尽（checksum+mismatch） | 整 run 失败（若 mismatch 先出现）或冷启动 | checksum 即时 mark + mismatch pending mark flush → 冷启动（非全 mismatch，两种顺序同判） |
| exact-warm × TIME_MISMATCH | raise `WARM_START_TIME_MISMATCH`（**工作区留材料化坏 IC**，无 mark） | error 语义逐字节同（裸 raise 原码/原 traceback）；**新增先清 staged**（与 `:1326` checksum 臂卫生一致，P2-1 具名差异——工作区状态非 error 语义） |
| 合法降级复用（older state，header==valid_time） | 正常 warm | 逐字节同 |
| checksum / 形状畸形 | 阶梯 | 逐字节同（#1429 回归锁） |

## D3 — seams under test

1. mismatch → mark 消息含 `WARM_START_TIME_MISMATCH:` token（与形状畸形
   mark 可区分）+ 邻座 warm 继续（`prepare_workspace` 成功、init_mode=3）。
2. 单 mismatch 无邻座 → `cold_start_no_state`（AC-2）。
3. 2 快照全 mismatch 耗尽 → `WARM_START_TIME_MISMATCH_SYSTEMIC` 整 run
   失败，消息可 grep（含两计数），staged `*.cfg.ic` 已清（AC-3+AC-7）；
   **且断言两快照 usable_flag 未被置 False**（mark 未落库——下一 cycle
   信号可持续，P1-1 钉格）。
4. 混合拒因耗尽 → 冷启动照旧（非全 mismatch 不 escalate）——**两种顺
   序各测**（mismatch 先 / checksum 先，P2-2 假一致回归锁）；且
   mismatch 候选的 mark 在冷启动出口已 flush（QC 记录不丢）。
5. exact-warm × mismatch → 仍抛原码 `WARM_START_TIME_MISMATCH`（AC-4，
   注意不是 issue AC 文本臆测的 `WARM_START_UNAVAILABLE`——今日实码在
   `:1325` 前逃逸，Task 0 (e) 实测钉）；staged 已清（P2-1）、无 mark。
6. 合法降级复用零变化（`tests/test_warm_start_chaining.py:733` 起保绿，
   AC-5）。
7. 两处旧整-run 失败锁（`:704-731`/`:761-780`）按新语义**显式改写**为
   阶梯行为断言（AC-6，行号 review 校正）；`tests/test_warm_start.py`
   `:273-276` 注释口径同步。
8. 工作区卫生：seam 2/3/5 终态后 input_dir 递归无 `*.cfg.ic` 残留（含
   材料化 target；AC-7）。
9. 形状畸形转译路径逐字节回归锁（#1429 面不受扰动）。
10. retry 分类 parity：`WARM_START_TIME_MISMATCH_SYSTEMIC` 经
    `services/orchestrator/retry.py` 分类与 `WARM_START_TIME_MISMATCH`
    同档（permanent/不可重试）——防止后人误注册 transient。
11. warm 成功出口 flush：候选 1 mismatch + 候选 2 健康 → warm 继续且
    候选 1 的 mark 已落库（seam 1 断言补 usable_flag=False）。

## D4 — 红证

- R1：回退白名单扩展（还原单-code）→ seams 1/2/3 红（整-run 失败复现）。
- R2：纯 A 冒充（转译但无耗尽分派/escalate）→ seam 3 红（systemic 信号
  丢失）——判别「阶梯+升级信号」而非「不抛」。

## Task 0 探针（实现前，任一不符停下重裁）

- (a) 复现今日整-run 失败：单 mismatch 几何直调 `_stage_initial_state`
  → `WARM_START_TIME_MISMATCH` 冒泡。
- (b) `_materialized_model_input_dir(...) ⊆ input_dir` 且
  `_clear_staged_initial_states` 递归清掉材料化 `<project>.cfg.ic`
  （`_find_regular_files` descendant 遍历已读码确认，实测钉）。
- (c) `_mark_init_state_corrupted` 消息透传（token 前缀可达 state_manager
  的 mark 调用）。
- (d) 两处旧锁（`:705-731`/`:768-779`）几何与断言现状读定。
- (e) exact-warm × mismatch 今日实抛码 = `WARM_START_TIME_MISMATCH`
  （非 `WARM_START_UNAVAILABLE`）。

## Non-goals

见 proposal Out of scope。
