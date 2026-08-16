# Proposal: row-absent-pin-evidence-parity

## Why

Issue #1308（PR #1306 / #1292 的 task 5.0 残留披露出口）：row-absent pin
gate `_unresolvable_marker_entity_pins_attempt` 与 row-present twin 存在
**双向**判决分歧——row-borne staleness 证据（placeholder 形、repaired
flag、非 failed 状态）没有 state 级替身导致 **over-pin**（钉住 twin 会拒
的陈旧目标）；model-bearing `job_cycle_*` 目标 row-present 路由器无条件
pin、row-absent 臂施加 cycle-scope 逻辑导致 **under-pin**（运维钉值被
静默丢弃）。今天全部 latent（`record_manual_repair` 无非测试调用方），
#1186 接上执行入口即转 live，失效无日志。另有 D 项：journal 完成阶段
compaction 使 pin gate 活域只剩提交阶段，该事实只存在于散文、零测试锚。

## What Changes

- **方向裁定（issue 推荐采纳，design D1）**：沿用 #1306 的 `failed_stage`
  机制，把剩余 row-borne 证据搬进 marker 自己的记录——
  `file_orchestration_journal.record_manual_repair` 的事件 `details`
  追加目标行写入时字段：`target_status` / `target_repair_status` /
  `target_active_blocker` / `target_model_id` / `target_slurm_job_id`
  （键名避开 `stage`/`job_type` 两个 record-stage 消费键与
  `model_id` 这个 attribution 消费键，design D2）；
  `scheduler_state_identity_filter` retry-event 白名单同步放行。
- **row-absent 臂重构（design D3，核心机制）**：记录完整时从 marker 记录
  **重建目标行**（pseudo-row），运行与 row-present 侧**同一套**路由逻辑
  （model-bearing → 无条件 pin 对齐路由器；model-less → 走
  `_cycle_scope_marker_pins_attempt`，内嵌共享谓词
  `_job_row_is_live_failure`）——写入时形状的等价性 by construction；
  state 级两条 staleness 映射保留，覆盖写入后命运（post-write fate）。
  记录缺席（legacy）→ 现行臂原样保留为 backstop。
- **残留终裁（design D4/D5）**：写入后命运在两条映射之外的形
  （winner-eviction / copy 分支 / 队列 stage 成功、写入后 repaired 未被
  点名）**转为主 spec 永久限定条款** + 成对披露测试；B 的 token 推断上限
  spec 限定 + 合同测试；不采用投影墓碑与 completed-evidence 域拓宽
  （后者与 restart 路由耦合，`chain_repository_state.py:884-886`）。
- **D 项独立小修（design D6）**：journal 测试锚定完成阶段 compaction
  （parse/state_save_qc/publish 的 model-less 队列 marker 事件投影后无
  details、不被采信）；spec 尾句按「journal 活域 = 提交阶段」限定并修正
  「keeps the disclosed id-token backstop」的不实括注。
- 残留矩阵测试两格 `(False, True)` → `(False, False)` 收敛（红-绿协议：
  由生产行为驱动，非测试改动）。
- AC 中「`unresolvable-marker-evidence-equivalence` design.md Residues
  随之更新」按其归档冻结裁定（tasks.md:33-40）处置：**归档不回改**，
  最终裁定（哪些收口、哪些转永久限定）由本 change 的 design + 主 spec
  条款承载（与 #1302 AC6 同口径）。

## Risk Triage

- Fixture level: **high**（强制触发词：manual retry / attempt 记账 /
  orchestrator 状态机 / persisted evidence 记录 + 写入面（journal 事件
  details）+ sanitizer 白名单多面联动；同族 #1294 先例 high）。
  Upstream suggested level: 缺省（issue Readiness: needs-triage，方向
  裁定由本 fixture design 承载后即 implementation-ready——issue 自述
  「裁定后即为 implementation-ready，不需要拆分」）。
- Repair intensity: **high**（判决函数重构 + 写入面扩展 + 白名单镜像 +
  成对矩阵扩格；等价性主张必须逐格闭合）。
- Risk packs:
  - state-machine/attempt-accounting: **selected** —— pin/refuse 逐格
    真值表（design D4 矩阵），pseudo-row 与 row-present 共享谓词。
  - compatibility/regression: **selected** —— legacy（无记录）marker 的
    backstop 行为逐位不变；#1292/#1294/#1302 已交付判别锚保持绿；
    sanitizer 白名单新增键不得影响既有键。
  - spec-compliance: **selected** —— delivered-domain 句改写、尾句括注
    修正、永久限定条款与实现逐句对读。
  - integration/write-read-parity: **selected** —— 写入面
    （record_manual_repair）→ sanitizer → 决策态读取的全链路真实投影
    测试；DB 路径 SQL retry service marker 属 legacy 形（无新字段）走
    backstop，如实记载。
  - security/auth、file IO、performance: not selected —— 无权限/IO/热
    路径面（details 增 5 个标量键，evidence 体积可忽略）。

## Non-Goals

- twin `_cycle_scope_marker_pins_attempt` 的判决语义（本次不动；#1294
  已交付其状态域）。
- `cancelled` 状态域（#1294 已交付，不重叠）。
- #1186 db-free 人工重试执行入口本身（曝光门，非实现门）。
- DB 路径谓词与 SQL retry service 的 marker 写入面（其 marker 无新字段，
  走 legacy backstop，行为不变）。
- 归档 change 目录的历史文档编辑（冻结裁定）。
- `completed_stage_evidence` 生产者域拓宽（与 restart 路由耦合，明确
  否决——design D5）。
- 投影墓碑方案（issue 备选，成本与 #1288 候选态边界风险，否决——D1）。

## Impact

- `services/orchestrator/scheduler_state_manual_retry.py`
  （`_unresolvable_marker_entity_pins_attempt` 重构 + pseudo-row 构造）
- `services/orchestrator/file_orchestration_journal.py`
  （`record_manual_repair` details 扩展；D 项测试锚只读参照 compaction）
- `services/orchestrator/scheduler_state_identity_filter.py`
  （retry-event details 白名单追加 5 键）
- `tests/test_production_scheduler.py`（残留矩阵扩格与收敛、legacy
  backstop 合同、B 上限用例）
- `tests/test_file_orchestration_journal.py`（写读全链路 + D 项锚）
- `openspec/specs/job-retry-mechanism/spec.md`（merge 后 archive 回写）
