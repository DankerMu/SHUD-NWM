## 0. Baselines（拆分前落盘，全组共用 oracle）

- [x] 0.1 `pytest --collect-only -q` suffix 集合落盘：`test_retention_copyback_mutex`(25) / `test_scheduler_state_index_copyback_replay`(32) / `test_entropy_audit_script`(410) / `test_scheduler_file_provider_refresh`(315) / `test_publish_scheduler_file_registry`(59)
- [x] 0.2 `scripts/governance/audit_repo_entropy.py --format json` 报告落盘（exit 0）
- [x] 0.3 CLI stdout 落盘（均 exit 0）：`python -m scripts.scheduler_file_provider_refresh --help`、`python -m scripts.scheduler_file_provider_refresh --dry-run --help`（#1099 验收字面量）、`python -m scripts.publish_scheduler_file_registry --help`
- [x] 0.4 `current-production-ops.md` 的 165 处引用文件清单与 1 处锚点落盘
- [x] 0.5 核销已完成 issue：#2026 / #2074 / #2102 实测 < 1000（CLOSED）；#1906 四面实测 870/892/855/490（由 #1910 + #1903 完成）

## 1. #1611 — `tests/test_scheduler_state_index_copyback_replay.py`（1378 行 / 32 用例）

- [x] 1.1 拆为多个 collectible 分区，每个 < 1000 行；共享定义进非收集 helper
- [x] 1.2 零断言改动（diff 内测试体只有位置移动）
- [x] 1.3 删除 `.large-file-guard.json` 该条 exclude，零替代 exclude
- [x] 1.4 按 D5 落地路由：`scripts/select_ci_tests.py` 新增 `PathTestRule` **显式枚举**本组全部分区（该文件当前对本语料零条规则，仅靠同名推导）；`tests/test_select_ci_tests.py` 新增 tracked-tree 守卫（恰好 N 分区 + M helper）；二者落地后 `uv run pytest -q tests/test_select_ci_tests.py` 绿
- [x] 1.5 Evidence：suffix 集合与 baseline byte-identical（32）；分区全量 pytest 绿

## 2. #2259 — `services/orchestrator/retention.py`(1107) + `tests/test_retention_copyback_mutex.py`(1119 / 25 用例)

- [x] 2.1 抽 copyback-mutex lane 到 `services/orchestrator/retention_copyback_mutex.py`，只搬 **3 个**符号：`_CopybackLockBudget` / `_delete_entry` / `_remove_tree_under_copyback_mutex`。**`_resolve_copyback_lock_root` 留在 `retention.py`**——它的唯一调用点是 `run_retention`（`retention.py:938`，本组不搬），且它自身调用 `_sanitize_root_candidate`(`:489`) 与 `EXTRA_ROOT_NOT_ABSOLUTE_REASON`(`:109`)（均不在搬运集内），搬走会造成 `retention.py` ↔ 新模块的顶层循环 import；它也不是 monkeypatch 面，D1 不要求搬
- [x] 2.2 按 D1 裁定 patch target：`acquire_copyback_batch_lock` / `release_copyback_batch_lock` / `remove_tree_allow_symlinks` 的调用点归属确定后，patch target 同 commit 改指真实 owner，原模块**不得** re-export 已搬走的 patch 符号
- [x] 2.3 测试侧：176 行 fixture 前言并入 `tests/retention_test_helpers.py`；正文按行为切 collectible 分区，各 < 1000 行，无 collectible 兼容 shim
- [x] 2.4 删除 `.large-file-guard.json` 两条 exclude（`services/orchestrator/retention.py`、`tests/test_retention_copyback_mutex.py`），零替代 exclude
- [x] 2.5 按 D5 落地路由：retention 的 `PathTestRule` 显式枚举新 owner 模块与全部新分区；新增 tracked-tree 守卫；`tests/test_select_ci_tests.py` 契约用例更新并绿
- [x] 2.6 Evidence：suffix 集合与 baseline byte-identical（25）；每个负向 patch 用例经「故意破坏真实调用点 → 用例转红」核验非 vacuous

## 3. #1101 — `tests/test_scheduler_file_provider_refresh.py`（9614 行 / 315 用例）

- [x] 3.1 按 §-boundary 拆分区，每个 < 1000 行（issue 表的 5 文件方案已 stale，按实测行数扩到所需数量）
- [x] 3.2 共享 fixture/helper 进 `tests/scheduler_refresh_helpers.py`（非收集）
- [x] 3.3 删除该条 exclude，零替代 exclude
- [x] 3.4 按 D5 落地路由：refresh 语料的 `PathTestRule` 显式枚举全部新分区（按 target 字符串定位规则，不按行号）；新增 tracked-tree 守卫；`tests/test_select_ci_tests.py` 绿
- [x] 3.5 Evidence：suffix 集合与 baseline byte-identical（315）；分区全量 pytest 绿

## 4. #1099 — `scripts/scheduler_file_provider_refresh.py`（3639 行）

- [x] 4.1 拆 `scripts/scheduler_refresh/` 包，实测拆为 10 个 module（constants / identity / config / classification / receipt_validation / cutover_declaration / receipt / precommit_gate / providers / runner），最大 716 行；`main` 与 `_build_parser` 按 D1 留在 facade（二者读 `__doc__`，搬走会改 argparse description）
- [x] 4.2 旧路径按 D1 保为 facade（非 2 行 shim）：`python -m scripts.scheduler_file_provider_refresh` CLI 契约、env、flag、退出码不变。**按 D1 全部 20 个 patch 符号取第 1 种裁定（真实调用点可达）**，实现方式为 facade 的 `__setattr__` 广播——对每个属性写入，同步写到包内所有「当前绑定同一对象」的 module，等价还原拆分前的单一 namespace 语义；零调用点改写，零 test repoint
- [x] 4.3 删除该条 exclude，零替代 exclude
- [x] 4.4 selector：`SCHEDULER_REFRESH_PACKAGE_MODULES` + 10 条显式 `PathTestRule`（不用 `**` glob），facade 行改用 `SCHEDULER_REFRESH_RUNNER_TESTS`；`NODE22_REFRESH_READER_EDGES` 增 runner.py / receipt.py 两条 reader edge；`tests/test_select_ci_tests.py` 增 `test_scheduler_refresh_package_tracked_tree_is_exactly_ten_modules`
- [x] 4.5 Evidence：`--help` **与** `--dry-run --help` 两份 stdout 均与 baseline 逐字节相等；refresh 315 / publish 59 / node-22 probe + registry-audit + safe-fs 251 全绿；AST+sha256 oracle 零 missing、1 条 changed（`_CUTOVER_DECLARATION_SCHEMA_PATH` 的 `parent.parent` → `parents[2]`，下沉一层目录的必要修正，实测解析到同一绝对路径）；20/20 patch 符号经「关闭广播 → 转红」核验非 vacuous

## 5. #1102 — `tests/test_publish_scheduler_file_registry.py`（3218 行 / 59 用例）

- [x] 5.1 按 CLI / manifest / cutover-audit 拆分区，每个 < 1000 行；fixture 进非收集 helper
- [x] 5.2 删除该条 exclude，零替代 exclude
- [x] 5.3 按 D5 落地路由：publish 语料的全部规则引用点按 target 字符串 `tests/test_publish_scheduler_file_registry.py` 全仓 grep 定位并改指新分区（行号已失效，六组先后改同一文件）；新增 tracked-tree 守卫；`tests/test_select_ci_tests.py` 绿
- [x] 5.4 Evidence：suffix 集合与 baseline byte-identical（59）；分区全量 pytest 绿

## 6. #1100 — `scripts/publish_scheduler_file_registry.py`（1495 行）

- [x] 6.1 下沉 `package_version_for_model` + helper 与 manifest publisher 薄壳到独立 module；#1097 已 CLOSED，`_normalize_cutover_gate_audit` 按其落地位置复用而非重复实现
- [x] 6.2 CLI 保 argparse + main + 结构化 error 序列化；参数/退出码/schema_version/audit 字段语义不变
- [x] 6.3 删除该条 exclude，零替代 exclude
- [x] 6.4 Evidence：`--help` stdout 与 baseline 逐字节相等；`tests/test_publish_*.py` 全绿

## 7. #1823 — `tests/test_entropy_audit_script.py`（9860 行 / 410 用例）

- [x] 7.1 按测试族拆 `tests/test_entropy_audit_*.py`，每个 < 1000 行；共享 helper 进 `tests/entropy_audit_helpers.py`（非收集）
- [x] 7.2 按 D1 处理对 `audit_repo_entropy` 的 module-attribute patch（本组不搬生产侧，patch target 不变）
- [x] 7.3 删除该条 exclude，零替代 exclude
- [x] 7.4 落盘 #1809 遗留：5 处 expected-command → `tests/test_gateway_reconcile_*.py` glob；删除 `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` 与 `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` 的 "frozen pre-#1809 guard literal" 行
- [x] 7.5 治理面全量同步（按字符串 grep 定位，不按行号）：`scripts/select_ci_tests.py` 中 target 为 `tests/test_entropy_audit_script.py` 的三条 PathTestRule 改为显式枚举新分区、`tests/test_select_ci_tests.py` 的 pin、`_ScopedAgentContextConfig` 四处字面量、4 个 scoped `AGENTS.md`、`docs/governance/*_INVENTORY.md` 与 `entropy-burndown-triage.md` 的 verification command 改为 `tests/test_entropy_audit_*.py` glob；并按 D5 新增 tracked-tree 守卫
- [x] 7.6 Evidence：suffix 集合与 baseline byte-identical（410）；分区全量 pytest 绿

## 8. #1842 — `scripts/governance/audit_repo_entropy.py`（9000 行）

- [x] 8.1 按 `_check_*` 家族 + 常量/schema + cli/report 拆模块，每个 < 1000 行；shared support module 承接公共常量/helper
- [x] 8.2 按 D1 裁定：被 `tests/test_entropy_audit_*.py` patch 的私有 helper 与其调用点同模块；搬走者同 commit 改 patch target 且原模块不 re-export
- [x] 8.3 删除该条 exclude，零替代 exclude
- [x] 8.4 selector 与 verification literal 与第 7 组一次性收口（不重复改动）
- [x] 8.5 Evidence（D3）：本组拆分**单独**取 before/after `--format json`。`metadata` 只比 8 个稳定键（`schema_version`/`mode`/`check_family_count`/`executed_check_families`/`skipped_path_families`/`max_scanned_text_file_bytes`/`max_artifact_fingerprint_bytes`/`baseline_path`）逐键相等，`generated_at`/`repo_root`/`finding_count`/`budget_counted_count`/`gate_eligible_count`/`baseline_exists`/`baseline_written`/`summary_counts`/`structural_file_budget`/`compatibility_facade_guard`/`scoped_agent_context` 显式排除；`findings[].check_id` 集合、`high_spread_patterns`/`module_heatmap` key 集合、顶层 public 函数集合相等；退出码不变；`findings` 全量 diff 期望为空（baseline 801 条中零条命中 entropy audit 自身路径），出现残差须逐条归因到本次搬运路径并在 PR body 列举，不可归因即回退本组
- [x] 8.6 Evidence：`uv run pytest -q tests/test_entropy_audit_*.py` 全绿

## 9. #1103 — `docs/runbooks/current-production-ops.md`（6249 行）

- [x] 9.1 拆 `docs/runbooks/production-ops/` sub-runbook，每个 < 1000 行；主文件降为索引 landing page 且 < 1000 行
- [x] 9.2 命令、jq 表达式、复现步骤逐字保留；已用锚点 `#311-pipeline-job-provenance-sidecar-and-recovery-2420` 仍 resolve
- [x] 9.3 删除该条 exclude，零替代 exclude
- [x] 9.4 `scripts/select_ci_tests.py` 中 target 为 `docs/runbooks/current-production-ops.md` 的路由（按字符串 grep 定位）同步覆盖 `docs/runbooks/production-ops/**`
- [x] 9.5 **阻塞依赖（前置扫描发现，非 issue 原文）**：`tests/test_node22_entrypoint_invariant.py` 用 `_read("docs/runbooks/current-production-ops.md")` 对该文档正文断言（`:89`/`:103` node-22 active 命令无裸 `uv`、`:558`、`:669`）。正文搬进 sub-runbook 后这些断言会**静默空过**（扫不到命令 → `remaining == []` → 绿）。必须让这些 reader 扫 `production-ops/` 全树。该文件 1003 行、未豁免，故本组同时把它拆到 1000 行以下（只超 3 行，按 section 切两半即可）——**不新增豁免**，#2532 的单条额度已用尽。拆分沿用本批 oracle：collection suffix 逐字节相等 + AST 指纹 + 显式 PathTestRule + tracked-tree 守卫
- [x] 9.6 空过红证：把一条 node-22 active 命令的裸 `uv` 形式写进某个 sub-runbook，断言 `:103` 一族转红——证明 reader 确实扫到了新位置
- [x] 9.5 Evidence：165 处引用文件逐一确认链接/锚点仍 resolve；markdown-lint 绿

## 10. 收口

- [x] 10.0 第 10 组 cleanup commit：修掉本批自身造成的悬空引用（注释/docstring/散文，零可执行改动，AST 对 21 个 .py 逐一相等）；`openspec/specs/**` 的 live requirement 漂移另立 #2533

- [x] 10.1 `wc -l` 全量核验：本批产出的每个文件 < 1000
- [x] 10.2 `.large-file-guard.json` diff 为 10 条删除 + 至多 1 条已记录的非替代新增（见 Evidence Floor 2），`maxLines` 与其余条目逐字节不变
- [x] 10.3 `uv run ruff check .` 绿
- [x] 10.4 `openspec validate split-oversized-surfaces-batch --strict --no-interactive` 绿
- [x] 10.5 node-27 真实 DB pytest（`TMPDIR=/home/nwm/tmp`）覆盖 retention / refresh / publish / entropy 全部分区 —— 最终 head `a5b4e730`，**2059 passed / 0 failed / 0 skipped**（643.06s），另补 #1103 的 5 个 runbook reader 套件 **97 passed**；receipt 贴在 PR #2534
- [x] 10.6 PR body 声明：CI 定向选择可能降级为 collect-only 冒烟，node-27 为真实 oracle

## Evidence Floor

1. 每个被拆测试文件的 `--collect-only` suffix 集合与 §0 baseline **byte-identical**（25/32/315/59/410）。
2. `.large-file-guard.json` 恰好删除 10 条 exclude；`maxLines` 与其余条目逐字节不变。**唯一允许的新增**是 `tests/test_node22_refresh_timer_health.py`（3436 行，pre-existing 超线、从未豁免，#1099 必须 repoint 它读 runner 源码的 4 处字面量断言，而 guard 是整文件 touch 门）——它不是任何拆分产物的替代豁免，必须在 PR body 记偏离并由 #2532 收口撤销。除此之外零新增。
3. 本批产出的每个 `.py` / `.md` 文件 `wc -l` < 1000。
4. 每个被搬走的 monkeypatch 符号：原模块不再 re-export，且负向用例经「破坏真实调用点 → 转红」核验（D1）。
5. `audit_repo_entropy.py` 单组 parity（D3）：8 个稳定 metadata 键、`check_id` 集合、`module_heatmap`/`high_spread_patterns` key 集合、顶层 public 函数集合逐一相等；`findings` 全量 diff 为空，或残差逐条归因到本次搬运路径并在 PR body 列举。
6. 三份 CLI stdout（`refresh --help`、`refresh --dry-run --help`、`publish --help`）与 baseline 逐字节相等。
7. 每个产出新测试分区的组（#1611/#2259/#1101/#1102/#1823）各自落地：`scripts/select_ci_tests.py` 一条显式枚举新分区的 `PathTestRule` + `tests/test_select_ci_tests.py` 一条 tracked-tree 守卫（恰好 N 分区 + M helper）；之后 `uv run pytest -q tests/test_select_ci_tests.py` 绿。单绿不算证据（D5）。
8. `uv run ruff check .` + `openspec validate --strict --no-interactive` 绿。
9. node-27 实机 pytest 覆盖全部受影响分区。
