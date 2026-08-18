# Proposal: comment-absence-capability-gate (#1116)

## Why

Issue #1116：生产集群 `AccountingStoreFlags = (null)`（node-22 2026-08-18 实测，
Slurm 23.11.4）——accounting 不存 sbatch `--comment`。reserved-unbound 恢复路径
（`services/orchestrator/reconcile.py` `reconcile_reserved_unbound_jobs`
:1318-1741）以 comment 查询为**绝对缺席证明**：`_index_comment_sacct_matches`
:561-562 把空 Comment 行在建索引前静默丢弃，真实在飞的 job 对查询永久不可见 →
`global_absence` + `coverage_complete=true` → 过 grace（:1560-1664）判
`reservation_lost` → 下一 pass `reserve_candidate`（services/orchestrator/
reservation.py:181-258，:258 经 reclaim 调用点把行翻回 reserved；实现在
chain_repository.py:621 等 repository 层）再 sbatch——**同 cohort 双重提交**，
全程静默。被证伪的不变量：「comment 查询查不到 ⇒ 作业不存在」在不存 comment
的集群上恒假。前提修复 #1559（分页窗时区）已 landing，本单是该 issue 本体。

## What Changes

镜像仓内既有 fail-closed 能力探针模式（`default_global_accounting_visibility_probe`
reconcile.py:214-234 + querier 内 :373-377 的 `accounting_authority_unproven` 抛点）：

- 新增 `default_comment_storage_probe(slurm_bin_path)`：shell `scontrol show config`
  （经 `_bounded_visibility_stdout` 有界读取），解析 `AccountingStoreFlags` 行
  （helper `_accounting_store_flags_allow_comment`），仅当 flag 逗号列表含
  `job_comment` token 时返回 True；行缺失、`(null)`、scontrol 失败一律 False
  （fail-closed）。失败路径 WARNING 一次且**原因可区分**：「探针无法执行」
  （scontrol 失败/不可达——可能是配置错误）vs「flags 不含 job_comment」（集群
  确证不存 comment）。
- `default_comment_sacct_querier` 新增可注入参数 `comment_storage_probe`（默认由
  bin_path 构造）；插入点**钉死**：contract-version 检查（:371-372）**之后**、
  visibility 门（:373-377）**之前**（必须早于 visibility 门以覆盖 owner scope；
  必须晚于 contract-version 使 unsupported-version 抛点归属不变）。nonlocal 缓存
  每 querier 实例探测一次（镜像 `visibility_proven`）；未证明抛
  `ReconcileQueryUnavailable("accounting does not store job comments",
  reason_class="comment_accounting_unproven")`。
- **决策层零改动**：`reconcile.py:1443-1476` 的 `except ReconcileQueryUnavailable`
  块已把该异常统一收成 transient-deny（`:1477-1501` 是 coverage_incomplete 分支）
  → 行保持 reserved、不判 `reservation_lost`、不产生 absence 结论；reason_class
  沿既有管道进 pass evidence。
- 既有 15 处实构 querier 的测试用例（tests/test_gateway_reconcile.py:4621,4648,
  4669,4898,5038,5084,9876,9894,10217,10280,10327,10396,10420,10462,10494）全部
  注入 `comment_storage_probe=lambda: True`（照抄 `global_visibility_probe` 注入
  约定），保证单测永不触达真实 scontrol、既有抛点归属断言（:9876 visibility 消息、
  :9894 legacy 分页）不变。
- `docs/runbooks/failed-basin-retry.md`：新增「comment_accounting_unproven 卡住的
  reserved 行」处置小节（非只补 token 一句）：按 reason token 从 pass evidence
  定位卡住行 → 复用 :187-189 已有 `sacct -a --name nhms_forecast` + user/account/
  提交窗兜底核实在飞与否 → 写明当下真实存在的处置出口（在飞：等作业终态后按既有
  流程处理；确证死亡：人工降级的具体机制由 implementer 依既有 journal/评价面工具
  查明并如实记载；若无安全人工降级工具则明说并路由 tooling follow-up——不许写
  不存在的命令）。

## Non-Goals

- 保守自动匹配兜底（user+account+job_name+提交窗自动找回并绑定）——issue 给的
  是「或」选项；本单取 fail-closed + runbook 人工流程（issue 明示可接受）。
  自动匹配作为 follow-up 另立 issue（PR 时经 issue-scribe 归档，含
  `_reserved_record_identity_matches:2178` 需对称放宽的先决记录）。
- 提交侧 post-submit comment 回读验证（改 submit 路径，超出最小健全修）。
- inflight/terminal-file-cohort 路径（80b3aca7 已修，空 comment 落穿后续身份门）。
- legacy 配置键 `AccountingStoreJobComment`（Slurm ≤20.02）识别：不识别 → False，
  错向安全侧（多禁不误放），YAGNI。
- `_index_comment_sacct_matches:561-562` / `_parse_comment_sacct_matches:622` 的
  空 Comment 丢弃行为本身：在存 comment 的集群上它是正确过滤；门在其上游。
- 探针与 visibility 探针的 `scontrol show config` 合并读（每 querier 两次 scontrol
  的小冗余，YAGNI 接受）；`_record_file_reconciliation` 的 journal transition
  逐 pass 重写不去重（既有行为，记录不改）。

## Deliberate non-convergence（与既有收敛需求的关系）

既有需求「Reserved-unbound identity-mismatch outcomes SHALL converge instead of
wedging the pipeline」（openspec/specs/pipeline-job-persistence/spec.md:246-253）
的 scope 是 `identity_mismatch_blocked` 结局族及其 streak/release 出口；本单的
`comment_accounting_unproven` 属 query_unavailable 族，**刻意不收敛**：不进
identity-mismatch streak 计数（streak 只统计 identity_mismatch_blocked）、不新增
自动 release 出口、行保持 reserved 直到人工按 runbook 处置。理由：无能力集群上
不存在可靠的自动缺席证明，任何自动出口都在「双重提交」与「误弃活作业」之间二选
一；issue 明示「保持 reserved 并要求人工确认」可接受。该取舍在 spec delta 中
显式声明（正面消解与 :246 的张力），代价（该 cohort 的 cycle 维持
PIPELINE_ALREADY_ACTIVE 直至人工处置）写入 runbook。

## Risk triage

- Fixture level: compact（单模块新探针 + 单抛点，决策层零改动；模式有仓内先例）。
- Repair intensity: low。
- Risk packs: state-semantics selected（reservation 状态机——必须证明 gate 只把
  「绝对缺席证明」降级为 transient-deny，不影响 owned_match 集群、不触碰
  identity-mismatch streak/#1173 ladder、不新增状态）；test-evidence selected
  （红证必须打在 e2e 层且改动前形状为 reservation_lost 被判出）；env-divergence
  selected（scontrol 输出格式轴：实测 `(null)`、`job_comment` 单/多值、行缺失、
  命令失败；单测不得触达真实 scontrol）；其余 not selected。

## Must preserve

- 存 comment 集群（probe True）：sacct 查询行为不变（owned_match 绑定、
  global_absence 过 grace 判 reservation_lost、coverage/分页/page_key 全部现状，
  #1559 刚修）；每 querier 实例仅多一次 scontrol 探测子进程。
- 抛点优先级归属不变：unsupported contract version 仍抛原消息（:371-372）；
  visibility 门（:373-377）在 comment 门之后仍按原条件生效
  （tests/test_gateway_reconcile.py:9876/:9894 期望不变，靠注入 probe=True 保持）。
- **任何单测不得触达真实 scontrol**（15 处实构 querier 用例全部注入
  `comment_storage_probe=lambda: True`）。
- probe 每 querier 实例至多执行一次（预算纪律，镜像 visibility_proven）；querier
  每 pass 重建，故 scontrol 抖动只影响单 pass、下一 pass 自动重试。
- `ReconcileQueryUnavailable` 既有语义：transient-deny、不递增 absence 结论、
  行保持 reserved；`ReconcileQuerySaturated` 是其子类（:140），探针内 catch 父类
  即覆盖饱和档。
- identity-mismatch streak（#1173 ladder）不受影响：pre-query 的 identity_blocked
  分支（:1408-1420）在门之前 continue；accounting_unavailable 写不重置 streak 的
  既有行为保持。
- 探针失败不得抛出到 querier 之外（吞为 False + 可区分 WARNING）。
- `tests/test_gateway_reconcile.py` 全绿（493+ 条）。

## Seams under test

- 新 kwarg `comment_storage_probe`（callable 级注入）；模块级缝
  `_bounded_visibility_stdout`（fake scontrol 输出）与 `_bounded_sacct_stdout`
  （fake sacct 分页输出）——querier **无**可注入 subprocess runner，e2e 红证必须
  打在模块缝（见 tasks 2.3）；既有 `comment_query` 可调用级 fake
  （tests/test_gateway_reconcile.py:3638-3728 先例）；探针纯函数
  `_accounting_store_flags_allow_comment` 直接驱动。

## Evidence mapping

- 验收 1（不存 comment 集群不再判 reservation_lost）→ tasks 2.3 红证 e2e。
- 验收 2（存 comment 集群零变化）→ tasks 2.4 + 全量套件。
- 验收 3（探针解析各档 + 可区分 WARNING）→ tasks 2.1。
- 验收 4（探针缓存 + 零 sacct 调用 + 抛点优先级）→ tasks 2.2 + 2.6。
- 验收 5（runbook 处置小节）→ tasks 1.3。
- 验收 6（既有用例注入迁移全绿）→ tasks 1.4 + 2.5。
- Verification：`uv run pytest -q tests/test_gateway_reconcile.py` + ruff + openspec
  validate（本地）；merge 后 node-27 receipt + node-22 真探针 receipt（真实
  `scontrol show config` → False）记入 #1116（tasks 3.5）。
