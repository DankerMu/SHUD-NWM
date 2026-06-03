# Worklog: #260 — M23-9 node-22 E2E 证据 + 确定性测试 + runbook 校准

## 现状评估(Explore + 亲读)

已就绪(复用,不重造):
- **E2E 机器** `services/production_closure/e2e_validation.py`:9 stage(download/canonical/forcing/
  slurm/parse/frequency/tile/api/frontend),`validate_e2e` 写 stage evidence;BLOCKED 机制
  (PRODUCTION_E2E_DEPENDENCY_BLOCKED + evidence-path 绑定 + safe_fs no-follow);no-false-readiness。
- **live 测试** `tests/test_two_node_22_e2e.py`(opt-in NHMS_RUN_22_NODE_E2E=1):health/slurm/db/
  shud-dry-run/download/canonical 证据。
- **确定性测试**:`test_production_slurm_validation.py`(43 tests:preflight blocker/stub solver/
  missing lib)、`test_production_scheduler.py`(dry-run 非变更 line 2041、slurm preflight DB/storage
  line 3413-3829、canonical readiness block)。
- **runbook**:`two-node-production-e2e-plan.md`(826 行)、`two-node-deployment-overview.md`(327 行)。
- Acceptance Criteria(PASS-only-when-deps-succeed / BLOCKED-with-evidence-path / no-false-readiness /
  evidence under artifacts|scratch)**大体已被现有代码满足**。

真实缺口:
- **runbook**:未区分 no-flag scheduler-once 业务验证 vs `--workspace-root` 诊断(7.3);DB 表
  (ops.pipeline_job/event)与 API payload 字段名未分别标注成映射表(7.4);22/27 职责无操作权限矩阵(7.5)。
- **publish_qdown 接线**:`publish_qdown_cycle`(#259)无 CLI/调用方(死代码)→ 加 CLI 入口暴露 evidence。
- **确定性测试补缺**:mocked gateway 状态转移(用 #258 可注入 `_slurm_gateway_check(probe=)`)、
  artifact-root placement 显式断言。

## Boundaries (YAGNI / Out-of-Scope)

- 不加新 runtime feature,除非为暴露 deps 已实现的 E2E evidence(publish-qdown CLI 入口属此例)。
- 不改 flood `publish-tiles` 契约;不动 e2e_validation 的 9-stage 核心。
- 不重复造已有确定性测试(preflight/dry-run/gateway-DB 已覆盖)。

## Lanes(disjoint write-set)

- **Lane A docs**:`docs/runbooks/two-node-deployment-overview.md` + `two-node-production-e2e-plan.md`
  (7.3/7.4/7.5)。
- **Lane B wiring+test**:`services/orchestrator/cli.py`(publish-qdown 子命令,click+argparse 双入口)
  + `tests/test_cli_publish_qdown.py`(新:publish-qdown PASS/BLOCKED + gateway 状态转移确定性 +
  artifact-root placement)。

## Progress

- [x] State assessment + 分支 feat/issue-260-e2e-evidence-runbook
- [ ] Lane A runbook 文档
- [ ] Lane B publish-qdown 接线 + 确定性测试
- [ ] 验证:ruff + 指定 pytest + openspec validate + node-22 真库
- [ ] cross-review → clean → PR + merge
