# Proposal: scheduler-refusal-source-and-repaired-arm-oracles

## Why

两条已交付的调度器验收线今天**没有能拦住复发的守卫**，都是纯 oracle 缺口、无活体生产缺陷：

- **#1418**：#1313 AC-1「`scheduler_state_failure.py` 里不存在第二份 permanent-code
  拒绝清单」被写成对生产源码的**字符串扫描**。实测（本仓 HEAD、隔离副本）：把旧黑名单
  改写成单行、改成 4 空格缩进、或换个名字挂到第三个函数上，守卫**全绿**；只有多行 8 空格
  缩进且保留原字面相邻对时才红。
- **#1451**：`_pipeline_job_is_repaired_stage_evidence` 的 `active_blocker is False` 臂
  **删掉后全套件仍 1731 全绿**（实测，零判别力）。该臂现在处在 #1294 统一后的活失败域主干上，
  回归后果是行为级的（活失败被静默吞掉 → 候选被判无阻塞 → downstream resume 放行）。
  注：issue #1451 同时声称把该臂改写成 truthy 判断也不会亮红——**实测证伪**，truthy 变异
  打红 319 条（两种变异的判别域不同，见 design B-2）。本 change 对 truthy 方向买的是
  指名定位，不是新增判别力。

两条都是 db-free、纯本地可验的测试有效性缺陷，合批交付。

## What Changes

- **#1418**：把 `test_scheduler_state_failure_holds_no_second_permanent_code_refusal_list`
  的字符串扫描段替换为两条合取守卫——
  - **结构守卫**：AST 反查 `scheduler_state_failure.py` 全部模块级 set/frozenset 常量的
    **消费函数映射**，与钉死的期望映射整体比较。新增常量、既有常量多出第二个消费者、
    换名的第二份清单，三者都改变该映射。
  - **行为守卫**：参数化 `_downstream_failure_restartable` 在 `code_recorded=True` 域上的
    裁决，钉死其**与 `reason_code` 取值无关**——任何按码清单的二次拒绝（模块级或函数内联、
    任何写法）都会打红。
  - 该测试既有的四条常量取值断言（#1313 round-1 V1-C1 加入）**保留不动**。
- **#1451**：在既有直接谓词单测旁新增参数化矩阵，覆盖 `active_blocker` 的
  `False` / `True` / 缺失 三态 × `repair_status` 有无，同时断言下游
  `_job_row_is_live_failure` 取值；同一矩阵施加于 `chain_source_cycle` 的兄弟副本。
- 两条 ADDED 需求写进 `job-retry-mechanism`，把「单一拒绝判据源」与「repaired 臂两个
  输入形状」升格为**带回归覆盖义务**的规范线。

## Impact

- 受影响代码：`tests/test_production_scheduler.py`（唯一写入面）。
- 只读被断言对象：`services/orchestrator/scheduler_state_failure.py`、
  `services/orchestrator/scheduler_state_rows.py`、
  `services/orchestrator/scheduler_state_manual_retry.py`、
  `services/orchestrator/chain_source_cycle.py`。
- 规格：`openspec/specs/job-retry-mechanism/spec.md` +2 需求。
- 验证节点：**本地**。db-free、纯逻辑单测，不需要 node-22 / node-27 oracle。

## Non-Goals

- **不改任何生产行为**。两个模块只作为被断言对象读取。
- 不合并 `_pipeline_job_is_repaired_stage_evidence` 的两处兄弟副本，不删 `active_blocker`
  防御臂——是否保留该臂属另一场裁决（#1451 边界明写 out of scope）。
- 不重开 #1313 的语义裁决，不动该测试既有的常量取值断言。
- 不清理同文件其它源码扫描型断言（如 `_live_failure_closure_row_field_reads` 的
  `inspect.getsource` + 正则字段扫描，`tests/test_production_scheduler.py:8720`）——
  #1418 边界明写「同文件其它测试」out of scope；本 change 只报不修。
