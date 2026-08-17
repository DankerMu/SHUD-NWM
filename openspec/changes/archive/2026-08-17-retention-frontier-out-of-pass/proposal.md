# Proposal: retention-frontier-out-of-pass

## Why

Issue #1407（#1307/PR #1404 design D5 的显式 scope-out 路由）：前沿豁免只保护
调度 pass 自己发起的 retention（下界取自 pass 内存态）。pass 外仍有两个真实
删除面不知道流水线活跃下界：

- **面 A 手动 `cleanup --execute`**（`cli.py:92-105` `_run_cleanup` 直调
  `run_retention` 不传 `active_lower_bound`）——追赶期运维手动一跑即可原样
  重演 #1307 的「产出→删除」自旋；该入口零测试覆盖。
- **面 B node-27 daily timer**（`scripts/node27_raw_retention.py`）——锚点是
  display watermark（`MAX(cycle_time)` 上界水位），非活跃下界：一个新 cycle
  成功入库水位即前推，而追赶/backfill 仍在处理的更老 cycle 不受保护；且该
  进程 always-on、无 `enabled`/`dry_run` 闸，出错无软着陆。

## What Changes

- **面 A：receipt 前沿来源 + fail-closed（design D1/D2，issue 推荐方案）**：
  新 helper 读取**最新一份 scheduler pass evidence receipt**
  （`<NHMS_SCHEDULER_EVIDENCE_ROOT 派生 evidence_dir>/<pass_id>.json` 的
  `retention.frontier` 块，PR #1404 已保证该块在 evidence 压缩后仍在），
  附新鲜度上限；`_run_cleanup` 消费它。receipt 缺失/不可读/无 frontier 块/
  过旧 → **强制 dry-run**（fail-closed，绝不退化为无保护删除），blocker
  reason 记入 cleanup receipt；新鲜 receipt 的 `active_lower_bound: null`
  **原样镜像**（pass 自己就按纯墙钟跑，CLI 不比 pass 更严）。
- **面 A 测试**：`cleanup` click 与 argparse 两条入口各至少一条测试（当前
  `grep _run_cleanup tests` 为空）；追赶场景 `--execute` 产出
  `pipeline_frontier_exempt` 跳过项而非删除；「取不到 ⇒ 不删」钉死。
- **面 B：范围裁定 = 维持 watermark 锚点（design D4，具名记录）**：
  receipt 与 journal 都在 node-22 私有 `/scratch`（`NHMS_SCHEDULER_EVIDENCE_
  ROOT=/scratch/...`，infra/env/compute.example:61），node-27 结构性不可达；
  跨节点发布前沿需要新的 shared-store 写面，超出本 issue 规模——按 AC4 第二
  臂具名记录理由与残余风险（仓内双载体：spec + design；issue 评论为 PR
  body 转述），**不接判据**。
- **面 B 附带硬化（design D5）**：`node27_raw_retention` 补 `enabled` /
  `dry_run` 闸（env 驱动，默认保持现行为，供灰度与回滚）；summary 增加
  锚点披露块（anchor 语义 + 残余风险指针），使「前沿保护是否生效/为何
  不适用」从 receipt 可读（AC5）。
- 规格：`production-scheduler-orchestration` ADDED requirement（pass 外
  删除面的前沿契约 + 面 B 裁定记录）；#1307 的既有 requirement 原文不动。

## Risk Triage

- Fixture level: **expanded**。Upstream suggested level: 无；issue 预估 M
  （两面接入 + 新读取面 + 跨面裁定），删除面安全语义 + fail-closed 契约 +
  多载体（code/receipt/spec/infra env），高于 compact；无状态机/attempt
  记账面，不到 high。divergence：无。
- Repair intensity: standard。
- Risk packs:
  - deletion-safety/fail-closed（file-IO pack 变体）: **selected** ——
    fail-closed 全形枚举（缺失/不可读/畸形/过旧/null 镜像）逐格闭合；
    强制 dry-run 不得被任何路径绕过；不引入新删除路径。
  - compatibility/regression: **selected** —— pass 内路径零改动（PR #1404
    语义不回改）；`run_retention` 签名不变；node-27 默认行为逐位不变
    （闸默认开、锚点不变）；既有 retention/node27 测试全绿。
  - spec-compliance: **selected** —— ADDED requirement 场景与实现逐句对
    读；面 B 裁定记录仓内双载体一致（spec/design）。
  - ops-contract/receipt: **selected** —— 两面 receipt 可读出前沿状态
    （面 A frontier/blocker 块；面 B anchor 披露块）。
  - state-machine/attempt-accounting、security/auth、performance:
    not selected —— 无编排状态/权限/热路径面（helper 为单目录扫描 +
    单文件大小上限，目录总量由 evidence retention 兜底，非热路径）。

## Non-Goals

- pass 内路径既有语义（PR #1404 已交付，不回改）。
- #1307 本体与其 live receipt 兑现项。
- `retention.py` 双 run_id 解析器统一（卫生债，D3 另属）。
- `WORKSPACE_ROOT/runs` 工作区回收（#1318）。
- journal 查询 API 方案（备选否决——pass 外新增 journal 读取面 + 并发一致
  性考量，且跨节点同样不可达，成本高于 receipt 读取且不解决面 B）。
- 跨节点前沿发布面（shared store sidecar）——面 B 接判据的前置，规模与
  写面治理超出本 issue；若未来需要按 stage-change-pipeline 另立。
- `scripts/node27_timeseries_retention.py` / `node22_scheduler_evidence_
  retention.py`（不同域）。
- CLI 前沿 escape hatch（如 `--allow-stale-frontier`）——重新引入无保护
  删除的脚枪；长期停摆期的真删除需求走显式人工路径，不给一键旁路。

## Impact

- `services/orchestrator/retention_frontier.py`（新，receipt 前沿读取 helper）
- `services/orchestrator/cli.py`（`_run_cleanup` 接入 + receipt blocker）
- `scripts/node27_raw_retention.py`（enabled/dry_run 闸 + anchor 披露块）
- `tests/test_retention_frontier.py`（新）·`tests/test_cli.py` 或就近测试文件
  （cleanup 双入口）·`tests/test_node27_raw_retention.py`（闸 + 披露）
- `openspec/specs/production-scheduler-orchestration/spec.md`（archive 回写）
- `infra/env/compute.example` 等 env 模板（新 env 注释，如适用）
