# Clone-lineage admission predicate convergence (#1739 + #1740 + #1741)

## Why

三个 issue 围着同一根承重字段转：`cloned_from_model_id` 自 #1735 起从「写完就没人读」的
provenance 变成了调度期的承重输入——最早的 clone 行决定模型的存在起点 `t*`，`t*` 之前的
cycle 被摘出完成度打分与 cohort 准入。围绕它的三个洞分别在**读侧口径**、**读侧缓存**、
**写侧铸造**：

- **#1739（契约无权威口径）**：两个持久化面用不同谓词认定「这行 clone 行是否成立血缘」。
  DB 面 `packages/common/state_manager.py:934` 要求 `clone_gate_fingerprint IS NOT NULL`
  与 `cloned_from_model_id IS NOT NULL` 同时成立；文件面 `_clone_entries_for_model_source`
  （`packages/common/state_manager.py:3705-3707`）只看 `cloned_from_model_id`，全函数从不读
  fingerprint。对唯一写入方 `_build_clone_row` 产出的任何一行两面答案一致（两字段成对写出、
  fingerprint 是必填 `str`），只在半写/损坏行上分叉——今天不是错答案，但契约面上没有权威
  裁定，任何一面后续「顺手对齐」都可能把良性分叉变成真实分歧。
- **#1740（瞬时失败被 memoize 成永久无血缘）**：`resolve_lineage_cutover`
  （`services/orchestrator/scheduler_lineage.py:138-181`）把「确实没有血缘」和「解析失败」
  都塌缩成 `None`，`scheduler_core._lineage_cutover_for_model_source`（`:754-763`）再把这个
  `None` memoize 进 `_lineage_cutover_cache`；而全仓唯一清这个 dict 的地方
  （`scheduler_core.py:355`）被 `scheduler_runtime.py:872-875` 的 `if
  self.config.db_free_required:` 闸住——**DB 面永不清**。于是 DB 后端长驻进程上一次瞬时抖动
  就把某个 `(model_id, source_id)` 在整个进程生命周期内锁死成「无血缘」，静默退回 #1735
  之前的语义，且无任何运维信号。
- **#1741（写侧无自克隆闸）**：`fingerprint_gated_state_clone` 的有序闸列表
  （`packages/common/state_clone.py:420-614`）从不比较 `m0_model_id` 与 `m1_model_id`。同模型
  入参下全闸空转（闸 3 拿同一棵 package 树自比必相等；闸 4 在 recalibration 下被显式跳过），
  落到闸 5 铸出的 `state_id` 与 source 行逐字相同，`upsert_state_snapshot` 会把**真实状态行
  原地改写成自指克隆行**。#1738 的 reader 守卫拒得掉这行，但救不回被覆盖的 source 行。

## What Changes

- **#1739 裁定：`clone_gate_fingerprint` 是纯 provenance，不是血缘准入闸。** DB 面
  `get_earliest_clone_row_for_model_source` 去掉 `clone_gate_fingerprint IS NOT NULL`，与文件面
  在 fingerprint 这一轴上收敛；spec 本就是这个口径（场景 "Absent provenance means no lineage,
  not an error" 只以 `cloned_from_model_id` 缺失为键），此处把它写实。publisher 的兄弟 reader
  `get_latest_clone_row_for_model_source` **保持更严、不跟随**，理由记进 design。
- **#1740 裁定：失败不缓存。** `resolve_lineage_cutover` 把「解析失败」从「确实无血缘」里分出来
  （抛 `LineageResolutionError`），`_lineage_cutover_for_model_source` 只在解析成功时写缓存，
  失败则打一条结构化 warn 并返回 `None`（保持调用侧语义不变：无血缘 = 今天的行为）。**不**采用
  「每 pass 无条件清缓存」的方案，从而不引入每 pass 每 `(model, source)` 一次额外查询，也不改变
  中途重标定在 DB 面的可见性语义。
- **#1741：** 在有序闸列表最前端补一条 `m0_model_id == m1_model_id` 的 fail-closed 拒绝闸，走既有
  `_refuse` 通道落 audit 记录，新增 refusal scope 并同步模块 docstring 的 scope 清单与计数。

## Impact

- Affected specs: `fingerprint-gated-state-clone`（MODIFIED 一条 + ADDED 一条）
- Affected code:
  - `packages/common/state_manager.py` — DB 面 earliest-clone reader 的 SQL + `_clone_entries_for_model_source`
    的 docstring（当前如实描述分叉，收敛后必须改写）
  - `services/orchestrator/scheduler_lineage.py` — `resolve_lineage_cutover` 对外契约 + 新异常类型
  - `services/orchestrator/scheduler_core.py` — `_lineage_cutover_for_model_source` 的缓存写入条件与 warn
  - `packages/common/state_clone.py` — 新 refusal scope + 闸 -1 + 模块 docstring
- Affected tests（以 `git diff --stat b675da88a -- tests/` 为准）：
  `tests/test_real_database_integration.py`（新增 #1739 谓词的**可执行** oracle——本 PR 里其余针对
  这两条 SQL 的断言都是文本形状钉或 stub repository，一行 SQL 都不执行；它的路径也是让 CI 的
  `database` path filter 命中、从而真的跑起 `real-db-integration` 的那一条）、
  `tests/test_scheduler_lineage.py`、`tests/test_scheduler_backfill.py`、`tests/test_state_clone.py`、
  `tests/test_scheduler_generation.py`（仅注释修正，断言不变）。
  `tests/test_state_clone_recalibration.py` **不改动**，按 design D8 第 5 条保持原样绿。
- Live receipt: `docs/runbooks/receipts/2026-09-15-issue-1739-clone-provenance-count-node27.md`
- Non-goals: 不改 `_build_clone_row` 的写入契约；不改 `(valid_time, created_at) ASC` 排序口径；
  不改 `usable_flag` 不过滤的决策；不改任何现有 clone 调用方；不对存量行做体检/回填；
  不动 `_lineage_provider_cache` 的 latch（#1740 已复核该猜想不成立）。
