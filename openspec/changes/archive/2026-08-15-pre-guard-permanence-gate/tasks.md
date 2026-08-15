# Tasks: pre-guard-permanence-gate (#1313)

## Risk packs (considered)

- Selected: terminal-state-semantics · oracle-integrity · spec-compliance
  （理由见 design "Risk packs"）。
- Not selected: concurrency · performance · data-migration · security。

## Tasks

- [x] 0. 运行时探针（先于实现，结果记入 PR 偏离记录）：
  - (a) OOM + raw-manifest 缺失几何直调 `_candidate_state_decision` 复证
    现状 `repair_missing_raw_manifest` + `automatic_retry_allowed True`。
  - (b) 记录 `PARSE_FAILED` + durable SHUD 几何复证现状 resume。
  - (c) OOM + 顶层 `retryable: True` 复证现状洗白。
  - (d) **语义模拟反向探针**（round-1 P3 教训）：按 design D2-D5 终形
    monkeypatch 语义跑
    `tests/test_production_scheduler.py tests/test_retry.py
    tests/test_scheduler_state_index_copyback_replay.py`，红名单必须与
    design D4b 十行表一致——多红/少红均停下报告 orchestrator。
  - (e) classifier 覆盖核对（design D2）：`failure_classifier` 对
    OOM/TEMPLATE_NOT_ALLOWED/POLICY_BLOCKED/PERMISSION_DENIED 的实际映射
    落表（注意 TEMPLATE_NOT_ALLOWED 在 `policy_blocked`，retry.py:183）；
    **state-classifier-override 矩阵项**（round-2 NEW-1）：state 带
    `classifier: "unknown_failure"` + `error_code: OUT_OF_MEMORY` 必须仍
    被码臂拒收。
  - 任一探针与 design 断言不符 → 停下报告重裁。
- [x] 1. 共享判据源（design D2）：`_REMEDY_NON_CAUSAL_CLASSIFIERS` +
  `_remedy_permits_permanent_failure(failure, *, remedy)`；消费覆写前分
  类；model-package 拒绝名单迁同源（语义零变为验收线）；
  `scheduler_state_failure.py` 不再有第二份独立永久码拒绝名单。
- [x] 2. 通道 (a)/(b) 接线（design D3）：结构门后咨询判据，非因果永久码
  （classifier deny）`return None`；开放域（含 `SLURM_JOB_FAILED` 等
  unknown 码与 limit_exhausted 豁免）字节级不变。
- [x] 3. 通道 (c)（design D4）：`_downstream_failure_restartable` 黑名单
  删除，改 `code_recorded` 分域 permanence 判据（记录永久/unknown/耗尽
  拒；合成占位码维持现行为；记录瞬时码 resume 不变）；消费者 `:246` 传
  入 `code_recorded`。
- [x] 4. 表现面 3（design D5）：顶层 retryable 覆写加
  `classification["retryable"]` 条件；`permanent: True` 反向覆写逐字保
  留。
- [x] 5. 现绿测试重判改写（design D4b 十行表为完整清单，PR body 逐条记
  录裁决；本清单外既有测试零编辑）：
  - #1/#2 copyback 双 anchor：记录 PARSE_FAILED → guard；**阻塞子任务**
    ——新增瞬时码平行 anchor ×2（OBJECT_STORE_ROOT 内 +
    NHMS_OBJECT_STORE_COPYBACK_ROOT 各一，allowed-roots 判定的唯一承重
    钉，round-1 P2-5）。
  - #3 `:17994`：改合成占位码形（去 error_code + 顶层 retryable）保
    "restart at parse 不重跑 native"主题。
  - #4 `:18058`：该参数化半边改断 guard；补合成占位码半边保原主题。
  - #5-#8 `:3403`/`:18307`/`:18346`/`:18442`：fixture 码换瞬时码保原主
    题。
  - #9 `:19299`、#10 `:4655`：保绿不动（D2 重裁）。
- [x] 6. 新增测试（design D7 seams 1-12）：通道 (a)/(b) OOM 红转绿 +
  unknown 码开放判别 anchor、通道 (c) 记录永久码参数化
  （PARSE_FAILED/OUTPUT_INCOMPLETE/SLURM_JOB_FAILED/
  Q_DOWN_DISPLAY_NOT_READY）+ 原黑名单五码回归 + 合成占位码域 anchor、
  表现面 3 双向、联合 seam 8、梯尾双路径 seam 9、row-4/model-package/
  completed-upstream 回归钉、耗尽域、判据单元矩阵。
- [x] 7. 红证（design D8）：三面独立回退各红 + seam 8 联锁双向红；seam 4
  为通道 (c) 判别红证；输出留存 + `git stash list` 空核验。
- [x] 8. 回归：`uv run pytest -q tests/test_production_scheduler.py
  tests/test_retry.py tests/test_scheduler_state_index_copyback_replay.py`
  全绿；`uv run ruff check .`；`openspec validate
  pre-guard-permanence-gate --strict --no-interactive`。
- [x] 9. AC 对照自审：issue #1313 六条 AC 逐条映射（AC-1 的 recompute
  exempt 与合成占位码域作为记录在案偏离/路由写入 PR body）；archive 侧
  `align-oom-retry-classification` tasks 5.0(b)(c)(e) 引用本 issue 号核
  对。**Follow-up 路由（round-1 复审后落地）**：#1419
  （map_slurm_error_code 兜底分类，design D4 NEW-4 承诺）· #1420
  （code_recorded 全扫描面历史码分域裁决，V1-C2）· #1418（AC-1 钉测
  AST/行为级硬化，V3-C2）。

## Required evidence (maps every selected pack)

- terminal-state-semantics：seams 1-9/11 + task 8 全量回归 + D6 不变式
  （生产 remedy 不退役）。
- oracle-integrity：task 5 十行重判逐条裁决 + task 0(d) 反向探针 +
  task 7 红证（含联锁）。
- spec-compliance：spec delta 六场景 ↔ seams 映射 + OOM 条款调和语句 +
  task 9 AC 对照。

## Non-goals

见 design "Non-goals"。
