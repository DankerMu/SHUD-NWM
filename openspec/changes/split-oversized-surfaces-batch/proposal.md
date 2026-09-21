## Why

`.large-file-guard.json` 的 1,000 行提交门槛是单向收敛的治理装置：越线即拒绝提交，豁免是逃生舱而非终态（`openspec/specs/orchestrator-structural-burndown/spec.md:264-266` 已把「the deleted monolithic path SHALL no longer be excluded from the large-file guard, and no replacement module SHALL receive an exclusion」写成需求）。当前 master 上仍有 9 张 open issue 对应的越线面，全部处于「touch 即死锁」或「已豁免、信号永久关闭」两种状态之一，实测行数（`wc -l`，HEAD `e5ea7f788`）：

| issue | 文件 | 行数 | guard 状态 |
|---|---|---|---|
| #1611 | `tests/test_scheduler_state_index_copyback_replay.py` | 1378 | 已豁免 |
| #2259 | `services/orchestrator/retention.py` | 1107 | 已豁免（PR #2257 新增） |
| #2259 | `tests/test_retention_copyback_mutex.py` | 1119 | 已豁免（PR #2257 新增） |
| #1101 | `tests/test_scheduler_file_provider_refresh.py` | 9614 | 已豁免 |
| #1099 | `scripts/scheduler_file_provider_refresh.py` | 3639 | 已豁免 |
| #1102 | `tests/test_publish_scheduler_file_registry.py` | 3218 | 已豁免 |
| #1100 | `scripts/publish_scheduler_file_registry.py` | 1495 | 已豁免 |
| #1823 | `tests/test_entropy_audit_script.py` | 9860 | 已豁免 |
| #1842 | `scripts/governance/audit_repo_entropy.py` | 9000 | 已豁免 |
| #1103 | `docs/runbooks/current-production-ops.md` | 6249 | 已豁免 |

合计约 46,000 行。这 10 条 exclude 占 `.large-file-guard.json` 现有 96 条豁免的十分之一，且集中在三条治理主线（scheduler registry refresh、entropy audit、retention/copyback）——每一条都还在持续演进，信号关闭的代价按 issue body 的实测轨迹是继续单调增长。

## What Changes

纯结构搬运，零行为变更。按 9 个独立验证单元拆分上述 10 个文件，并在同一 commit 内删除各自的 guard exclude，不新增任何替代 exclude：

- **#1611**：`tests/test_scheduler_state_index_copyback_replay.py` → 多个 collectible 分区（32 用例）。
- **#2259**：`services/orchestrator/retention.py` 抽 copyback-mutex lane 到独立 owner；`tests/test_retention_copyback_mutex.py` 按行为切分区 + 并入 `tests/retention_test_helpers.py`（25 用例）。
- **#1101**：`tests/test_scheduler_file_provider_refresh.py` → 按 §-boundary 分区（315 用例）+ 非收集 helper。
- **#1099**：`scripts/scheduler_file_provider_refresh.py` → `scripts/scheduler_refresh/` 包，旧路径保为 facade（保 `python -m` CLI 契约）。
- **#1102**：`tests/test_publish_scheduler_file_registry.py` → CLI / manifest / cutover-audit 分区（59 用例）。
- **#1100**：`scripts/publish_scheduler_file_registry.py` 下沉 `package_version_for_model` 与 manifest publisher。
- **#1823**：`tests/test_entropy_audit_script.py` → `tests/test_entropy_audit_*.py` 分区（410 用例）+ `tests/entropy_audit_helpers.py`；顺带落盘 #1809 遗留的 5 处 expected-command glob 迁移与 2 处 frozen literal 移除。
- **#1842**：`scripts/governance/audit_repo_entropy.py` → 按 `_check_*` 家族分模块 + shared support module。
- **#1103**：`docs/runbooks/current-production-ops.md` → `docs/runbooks/production-ops/` sub-runbook，主文件降为索引 landing page（保锚点与 165 处外链）。

**BREAKING**：无。CLI 参数、退出码、env、public import、测试断言、检查 id、schema version 均不变。

## Impact

- Affected specs: `orchestrator-structural-burndown`（ADDED 9 条 requirement）。
- Affected code: 上表 10 个文件及其拆出模块；`.large-file-guard.json`（-10 条 exclude，+1 条已记录的非替代豁免 `tests/test_node22_refresh_timer_health.py`，见下）；`scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`（新分区的定向路由与完备性守卫）；`scripts/governance/audit_repo_entropy.py` 的 `_ScopedAgentContextConfig` 路径字面量；4 个 scoped `AGENTS.md`、`docs/governance/*_INVENTORY.md`、`docs/governance/entropy-burndown-triage.md` 的 verification command 字面量。
- Out of scope（report-only）：`.large-file-guard.json` 其余 86 条 exclude 与 `maxLines` 阈值本身；`scripts/governance/write_entropy_baseline.py`（1,150 行，同模式兄弟，未立单）；任何 retention / registry / audit 的行为变更。
- 记录在案的偏离：`tests/test_node22_refresh_timer_health.py`（3436 行）在 master 上就超线且从未豁免。#1099 的拆分使它读 runner 源码的 4 处字面量断言失效，必须 repoint 14 行；而 guard 是整文件 touch 门，任何 touch 都被拒。复刻 hook 自身的判定（`.large-file-guard.json` 的 exclude + hook 内置 `DEFAULT_EXCLUDE`，`fnmatch` 同时套全路径与 basename；行数用 `open(errors="ignore")` + `str.splitlines()`）对全量 `git ls-files` 实测：另有 **184 个** tracked 文件同处「超线且未豁免」之态，其中 44 个是 PNG/JPG/PDF 这类按行计数无意义的二进制。说明 exclude 列表是「撞到过的文件」而非原则清单。本 PR 为它加一条 exclude 并另立 #2532 收口——它不是任何拆分产物的替代豁免，净账 96 → 87。
- 已核销不实现：#2026 / #2074 / #2102（CLOSED-COMPLETED，实测均已 < 1000）、#1906（由 #1910 `f19d3e1c0` + #1903 `b7c3e680a` 完成，四个面实测 870/892/855/490）。
