# Proposal: warm-start-time-mismatch-ladder (#1431)

## Why

`workers/shud_runtime/runtime.py` 的 warm-start 降级阶梯
（`_stage_initial_state:1244-1342`）对三类「坏快照」处置分裂：checksum
不符与头部形状畸形（#1429 转译 `IC_TIME_SHIFT_HEADER_INVALID`）都走
`_mark_init_state_corrupted` → `_next_usable_state` → 冷启动回退的阶梯，
而 `WARM_START_TIME_MISMATCH`（`_verify_ic_time_consistency:1650`/`:1657`
两处 raise）被 `:1298-1300` 的单-code 白名单挡在阶梯外，原样冒泡出
`prepare_workspace` ——**单个** `valid_time` 与自身头部漂移的快照即打掉
整个 forecast cycle，哪怕旁边躺着完好的上一周期快照。

## 裁决（owner 已拍板：C 阶梯 + 升级信号）

用户裁决走 issue 推荐的 **C**：进降级阶梯保 cycle，同时保留系统性损坏
（如 rekey 成批坏）的 fail-loud 信号——阶梯耗尽且拒绝全部源于
TIME_MISMATCH 且 ≥2 个时，以专用 error code 整 run 失败。

## What Changes

- `:1298-1300` 白名单扩到 `{IC_TIME_SHIFT_HEADER_INVALID,
  WARM_START_TIME_MISMATCH}`；TIME_MISMATCH 转译臂内先查
  `_exact_warm_start_required` —— 为真则清 staged 后**裸 `raise`**
  （error 语义逐字节保持——今日该几何在 `:1325` 之前逃逸、抛的就是
  TIME_MISMATCH 本码；清 staged 为 fixture review P2-1 的卫生补齐）。
- 转译消息带可 grep token（`WARM_START_TIME_MISMATCH: <原 message>`）喂
  `_mark_init_state_corrupted`，与形状畸形的 mark 消息可区分。
  **TIME_MISMATCH 的 mark 缓存为 pending、在除 escalate 外的循环出口
  flush**（fixture review P1-1：即时落库会把快照标 unusable，下一 cycle
  静默冷启动，systemic 信号只响一次；缓存后 escalate 出口不落 mark，快
  照保持 usable，下一 cycle 重走阶梯再次响——保住今日「每 cycle 都响」
  的性质，忠实于裁决 C）。
- 循环记两计数（总拒绝数 / TIME_MISMATCH 拒绝数，URI-only 候选照常计
  入）；阶梯耗尽（`:1333 next_state is None`）时：TIME_MISMATCH 拒绝
  ≥2 且 == 总拒绝数 → `_clear_staged_initial_states` 后 raise 专用
  `WARM_START_TIME_MISMATCH_SYSTEMIC`（不 flush pending mark）；否则
  flush 后照旧冷启动。新码不注册 retry 集合——与旧码同档 permanent。
- 其余零改动：`_verify_ic_time_consistency` 判据本身、checksum/形状两
  路、合法降级复用（older state）、exact-warm 臂全部逐字节不变。

## 对 issue C 描述的具名偏离

- 无 mid-ladder 阈值：escalate 只在耗尽点评估（阶梯长度受限于可用快照
  数，走完成本极低且证据完整；避免发明第二个魔数触发面）。
- 阈值 N=2 = 最小复数：单坏快照 + 无邻座 → 冷启动（AC-2 显式要求）；
  「全部拒因 TIME_MISMATCH 且 ≥2」才构成系统性怀疑——N=2 是 AC-2 与
  AC-3 在单候选几何上冲突的唯一调和解。

## Out of scope

- 时间一致性判据口径、`_consume_packaged_initial_state`、
  `_shift_cfg_ic_time` 形状守卫（#1429）、`state_cli` 头部消费
  （#1430）、快照漂移根因。

## Impact

- Affected specs: forecast-warm-start（ADDED 1 requirement）、
  cross-cycle-warm-start-chaining（MODIFIED：three-way blocker 粒度改
  判候选级，run 级终态归 forecast-warm-start 阶梯 requirement 管辖）。
- Affected code: `workers/shud_runtime/runtime.py`（`:1298-1300` 转译
  臂 + 循环计数/pending mark + 耗尽分派 + `:60-68` 模块注释改真）、
  `tests/test_warm_start_chaining.py`（`:704-731`/`:761-780` 两处整-run
  失败锁按新语义显式改写）、`tests/test_warm_start.py`（`:273-276` 注
  释口径同步）。
- 工作区卫生（issue 附带事实）：转译后 TIME_MISMATCH 走阶梯——下一轮
  `:1291` 或耗尽 `:1334` 的 `_clear_staged_initial_states`（递归
  `_find_regular_files(input_dir, "*.cfg.ic")`）覆盖已材料化的
  `<project>.cfg.ic`；escalate 出口显式先清（镜像 `:1326` exact-warm
  臂）。Task 0 探针验证 model_input_dir ⊆ input_dir。
