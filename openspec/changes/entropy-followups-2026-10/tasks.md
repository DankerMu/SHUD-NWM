## 约定

- 每个 issue 一个 PR，分支 `feat/issue-<N>-entropy-followups-2026-10`，从合并后的 master 切出；顺序 #3028 → #3032 → #3027 → #3029 → #3030 → #3025 → #3031。
- 每个 PR 勾选自己的 task 组；本目录在最后一个 PR 合并后 `openspec archive`。
- 后端 PR 的 pytest 一律跑「本组 Verification 清单 ∪ `select_tests(<本 PR 全部路径>)`」，PR body 记录选出集合与计数（路径集取 `git diff --name-only origin/master...HEAD`）。
- 不需要 node-22 / node-27：本 change 没有运行时、DB 或调度行为改动；issue 里写 node-27 的验收项（#3032、#3031）按各组的「偏离」行改为本地 + CI。

## 1. #3028 选择器：journal facade 的 patch 方路由 + 逐 sibling pin

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Resource limits / large input / discovery — selected：pin 的参数源是磁盘 glob，必须断言非空。
- Public API / CLI / script entry — not selected：选择器 CLI 与输出格式不变。
- Legacy compatibility — not selected：只追加 tuple 元素，不改既有规则顺序。
- 其余 pack — not selected：不涉及 IO / schema / auth / 并发 / 发布。

Scope 修正（相对 issue 正文，fixture review 2026-10-19 实测于 base `e038cf4b9`）：对 facade 与全部 7 个 `file_journal_*.py` 跑选择器，以下 **10** 个在函数体内 import journal facade 并 patch 其全局名的套件都未被选中（issue 只点名了前两个）：`tests/test_gateway_reconcile_{file_cohort_comment,identity_release,claimant_exclusivity,inventory,round10,writer_launch,writer_quiescence,writer_receipts,file_submit_barrier}.py` 与 `tests/test_safe_fs_lock.py`（经 `_record_lock_opens` patch facade 调用的 `open_lock_file_no_follow`）。`scripts/ci/full_test_durations.json` 合计约 30 s。

- [x] 1.1 在 `scripts/select_ci_tests_tables.py` 的 `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS` 末尾按字母序追加上述 10 个文件，附一行 `# #3028` 注释说明原因（函数体 import facade + patch facade 全局名，导入图推导不到）。不改任何规则顺序或其他表。
- [x] 1.2 在 `tests/test_select_ci_tests.py` 新增参数化测试：参数 = `sorted(Path("services/orchestrator").glob("file_journal_*.py"))` 再加 facade `services/orchestrator/file_orchestration_journal.py`（模块级断言 glob 非空）；每个参数断言 (i) `select_tests([p])` ⊇ `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`，(ii) 一个**字面量** tuple（按 #1827 惯例逐字写出 1.1 的 10 个路径，参照 `tests/test_select_ci_tests.py:13008` 附近写法）中每个路径 ∈ `select_tests([p])`，(iii) 不含 broad-only 标记 `tests/test_state_clone.py`。写法参照 `tests/test_select_ci_tests.py:8645-8666` 选择器自身 sibling 的参数化 pin。
- [x] 1.3 既有 `STOP_RULE_AT_SITE_EXTENSIONS`、`test_at_site_extensions_did_not_widen_the_stop_rules`、`test_file_journal_sibling_glob_rule_red_when_removed` 不改且保持绿；唯一的精确集合断言 `tests/test_select_ci_tests.py:1534-1600` 经符号引用自动跟随，不改。

Evidence Floor:
- 对 facade 与磁盘上全部 `file_journal_*.py` 逐个跑 `uv run python scripts/select_ci_tests.py --changed-file <file> --github-output <new tmp>`：每条输出都含上述 10 个文件（PR 贴表）。
- 演练（记录后还原）：(a) 删掉 `scripts/select_ci_tests_rules.py` 中的 sibling glob 规则 → 1.2 的每个 sibling 参数都红；(b) 从 1.1 中去掉一个文件 → 1.2 的字面量断言 (ii) 对每个参数都红。
- `uv run ruff check .`；`uv run pytest -q tests/test_select_ci_tests.py` ∪ `select_tests(<全部 PR 路径>)` 全绿。

Verification: `uv run ruff check . && uv run pytest -q tests/test_select_ci_tests.py`（∪ selector 选出集）

## 2. #3032 orchestrator 拆分模块可作首个导入（混合方案）

Suggested fixture level: expanded
Minimal mergeable slice: atomic（守卫测试与修复必须同 PR，否则守卫红）

决定（fixture review 2026-10-19 后由父流程拍板，记于 design.md）：只用 A / A′ / 单名 B 修复能纯移动 import 修好的 9 个模块；其余 8 个进守卫的精确棘轮 allowlist，同 PR 立 follow-up issue。不改任何函数体、默认参数或类体。这对应 issue 验收第 1 条的「备选」分支（allowlist 恰为剩余模块且有记录）。

Risk packs（设计见 design.md）:
- Public API / CLI / script entry — selected：facade 是所有生产入口的导入路径；evidence = 全新解释器导入 `cli`/`chain`/`scheduler`/`monitoring`。
- Legacy compatibility / examples — selected：facade 的 `vars()` 键集、对象 identity、`__module__`、MRO、monkeypatch 目标不变；evidence = 2.3 identity 用例 + 套件。
- Concurrency / shared state / ordering — selected（模块初始化顺序）：evidence = 2.2 守卫 + 两个演练。
- Resource limits / large input / discovery — selected：守卫从磁盘发现模块并断言非空，线程池 `min(8, os.cpu_count() or 1)`。
- 其余 pack — not selected：不改 IO / schema / auth / 配置 / 发布。

- [ ] 2.1 先写 2.2 测试（allowlist 暂空）在 base 上运行，记录红的 17 个模块（issue 的 16 个 + `services.orchestrator.scheduler_config` 包本身）。
- [ ] 2.2 新增 `tests/test_orchestrator_first_import.py`：只从文件系统发现模块（`Path("services/orchestrator").rglob("*.py")` → 点分名，`__init__.py` 映射为包名，含 `services.orchestrator` 本身；测试进程内不 import 任何 orchestrator 模块；断言非空；禁用 `pkgutil.walk_packages`——base 上它抛 `scheduler_config` 的 AttributeError，head 上会把 facade 导进 pytest 进程）；每个模块一个全新解释器子进程（`sys.executable -c "import M"`，cwd = 仓库根），有界线程池并发；失败报模块名 + traceback 前 20 行；断言失败集合 **恰等于** allowlist（allowlist 每项一行理由，引用 design.md 的逐模块结论）。
- [ ] 2.3 同文件 identity 用例：子进程先导入 `services.orchestrator.scheduler_types` 再导入 `scheduler`，先导入 `services.orchestrator.scheduler_config.path_modes` 再导入 `scheduler`，与先导入 `scheduler` 的子进程比较：`getattr(scheduler, "ProductionSchedulerConfig")` 强制解析后的非 dunder `vars()` 键集相等；`ProductionScheduler.__module__` 与 `ProductionSchedulerConfig.__module__` 均为 `"services.orchestrator.scheduler"`；`scheduler.ProductionSchedulerConfig is scheduler_config.ProductionSchedulerConfig`。chain 侧：先导入 `chain_forecast_orchestrator_runtime` 再导入 `chain`，`ForecastOrchestrator.__mro__[1:3]` 为两个 mixin。
- [ ] 2.4 按 design.md 修：A（`chain_forecast_orchestrator_runtime`、`scheduler_types`、`scheduler_gateway`、`scheduler_evidence_proofs`）、A′（`scheduler_adapters.py:18`）、B（仅 `scheduler.ProductionSchedulerConfig`，替换 `scheduler.py:374-377`；PR 贴 bare-global grep，覆盖 `scheduler.py` 与三个 `globals().update(...)` 复制命名空间的模块）。某行修不通则移入 allowlist 并写理由，不得引入新机制。PR 列出 17 个模块各自的去向。
- [ ] 2.5 删除 `tests/test_preserve_final_component_loop_spelling.py:45-50` 的 import-order fixture（注释 + 导入行），并删除 `tests/test_select_ci_tests.py` `INTENTIONAL_RULE_GAP_EXCLUSIONS`（`:11606-11624`）里仅因该导入存在的行及注释（先重新推导 `services/orchestrator/__init__.py` 那条边是否仍在）；`tests/test_gateway_reconcile_identity_release.py:492`、`tests/test_orchestrator_demote_reclaim_lifecycle.py:349` 的 `# import order` 涉及仍在 allowlist 的 `chain_forecast_orchestrator_cycle`，保留。
- [ ] 2.6 selector 路由：在 `PATH_TEST_RULES` 索引 0（所有 `services/orchestrator` stop rule 之前）加**一条非 stop 行**，pattern `services/orchestrator/*.py`（`fnmatch` 的 `*` 跨 `/`，覆盖子包；刻意不用 `services/orchestrator/**`，否则撞 duplicate-pattern pin `tests/test_select_ci_tests.py:8204-8231`、`next(... == "services/orchestrator/**")` 查找 `:3416`/`:3855`/`:12555` 与 `test_preservation_builder_guard_reds_when_one_rule_leg_is_removed` 的 `strip()`（base :23246-23262，HEAD :23285-23301）），只选 `tests/test_orchestrator_first_import.py`。把守卫文件加入以下精确相等选择 pin 的期望集合，其余不改，PR 逐条列出：`tests/test_select_ci_tests.py` 的 `test_select_tests_maps_orchestrator_chain_types_to_manifest_surface_nodes`、`test_select_tests_maps_orchestrator_manifest_surface_without_whole_slow_suites`、`test_select_tests_maps_file_journal_read_state_without_whole_legacy_suites`、`test_select_tests_maps_known_slow_manifest_test_file_changes_with_surface_changes_to_focused_nodes`、`test_select_tests_keeps_broad_orchestrator_fallback_for_other_orchestrator_changes`、`test_released_reservation_recovery_module_selects_its_exact_suites`、`test_conditional_redirect_owner_focused_when_surface_present`、`test_scheduler_runtime_selects_the_copyback_mutex_suite`（共 8 个，2026-10-19 在 d308507a1 上加入 index-0 行实测恰好这 8 个变红）。`tests/test_select_ci_tests.py` 加正向参数化用例：16 条 stop rule 各取一个路径 + 一个普通拆分模块路径 + `services/orchestrator/scheduler_config/config.py`，每个都选中守卫文件。
- [ ] 2.7 立 follow-up issue：剩余 8 个 allowlist 模块需要的 call-time 改写（类基、默认参数、复制命名空间）。PR body 链接它。

Evidence Floor:
- 守卫在 base 红（17 个模块名）、在 head 仅 allowlist 内的模块失败且测试绿；演练 1：把一个 A 修复模块的 back-import 移回顶部 → 守卫红并点名该模块；演练 2：删一个 allowlist 条目 → 守卫红；均还原。
- 全新解释器 `import services.orchestrator.{cli,chain,scheduler,monitoring}` 成功。
- `uv run pytest -q tests/test_orchestrator_first_import.py tests/test_production_scheduler.py tests/test_warm_start_chaining.py tests/test_file_orchestration_journal.py tests/test_orchestration_chain.py tests/test_preserve_final_component_loop_spelling.py tests/test_scheduler_journal_root_authority.py tests/test_scheduler_root_check_loop_convergence.py tests/test_select_ci_tests.py` ∪ `select_tests(<全部 PR 路径>)` 全绿；`uv run ruff check .`。
- PR push 后手动 `workflow_dispatch` 一次全量 `Unit Tests (full)`，所有分片绿。

偏离（相对 issue）：(1) issue 写 node-27 跑 scheduler/chain 套件；node-27 停机且这些套件不连 DB，改为本地 + PR CI 定向 + 一次全量分片 run。(2) issue 推荐方案要求 16 个全修；按 fixture review 实测 8 个需要 call-time 改写，降为混合方案 + follow-up issue（issue 备选分支）。

Verification: 见 Evidence Floor。

## 3. #3027 恢复选择器注释原文

Suggested fixture level: none
Minimal mergeable slice: atomic

Risk packs: 全部 not selected — 只改注释 / docstring。

- [x] 3.1 按 `git show a0b6ccf9d -- scripts/select_ci_tests.py` 的 `-` 行逐行恢复 issue #3027 列出的 10 行（`scripts/select_ci_tests_{imports,tables,rules}.py`）；只动这 10 行。

Evidence Floor: `grep -nE 'auto-skipker|eitherker|soked|# ks |theker|gatingker|akdown|retryker|infrakdown|Dockerker' scripts/select_ci_tests*.py` 无输出；`git diff -U0` 只含注释 / docstring 行（AST 比对 base/head 三个文件 `ast.dump` 相同，docstring 那一处除外并在 PR 说明）；`uv run ruff check scripts/select_ci_tests*.py`；`uv run pytest -q tests/test_select_ci_tests.py` ∪ selector。

## 4. #3029 改名残留的注释 / docstring

Suggested fixture level: none
Minimal mergeable slice: atomic

Risk packs: 全部 not selected — 只改注释 / docstring。

- [x] 4.1 按 issue #3029「解决思路·推荐」改 5 处文字；不改代码行。

Evidence Floor: issue #3029 验收标准中的 5 条 grep / 检查；`uv run ruff check .`；`uv run pytest -q tests/test_hydro_display_mvt_scaling_discovery.py tests/test_node27_timeseries_retention.py tests/test_file_journal_late_lookup_seams.py` ∪ selector。

## 5. #3030 删除死代码 `_mapping_has_runtime_root_fields`

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Legacy compatibility — selected：名字不在 `__all__`、无调用方、无 patch；evidence = 全仓 `git grep` 为空（openspec 历史记录除外）。
- 其余 pack — not selected。

- [x] 5.1 删除 `services/orchestrator/file_journal_retry_helpers.py` 中的定义；从 `tests/fixtures/file_journal_cluster_map.json` 的 `clusters.retry_helpers` 删掉该名字（保持 `json.dumps(indent=2, sort_keys=True) + "\n"` 序列化）。不改 `retry.py`。

Evidence Floor: `git grep -n "_mapping_has_runtime_root_fields" -- services tests scripts apps packages workers` 无输出；fixture `git diff --numstat` 为 `0 1`；issue #3030 Verification 命令 ∪ selector 全绿。

## 6. #3025 `apiErrorCode` 精度用例

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Schema / field names — selected：钉住只读 envelope `error.error.code`；evidence = 6.1 用例 + 变异演练。
- 其余 pack — not selected：运行时代码零改动。

- [ ] 6.1 在 `apps/frontend/src/stores/__tests__/overviewDataSourceSelection.test.ts`（或同目录新文件）经真实 store 路径（`loadOverview` / precip index 请求）加参数化用例：envelope `{error:{code:'PRECIP_CYCLE_NOT_MIRRORED'}}` → `not_mirrored`（对照）；顶层 `{code:'PRECIP_CYCLE_NOT_MIRRORED', message}` → `error`；`{error:'PRECIP_CYCLE_NOT_MIRRORED'}` → `error`；`{error:{code:42}}` → `error`。需要时在 `apps/frontend/src/test/overviewDataFixture.ts` 加构造非 envelope 错误体的小 helper。不导出 `apiErrorCode`，不改 `overviewDataStore.ts`。

Evidence Floor: 变异演练——把 `apiErrorCode` 临时改为也接受顶层 `code` → 新用例至少一条红；还原后绿（PR 记录）；`cd apps/frontend && corepack pnpm typecheck && corepack pnpm lint && corepack pnpm test`；`git diff --stat -- apps/frontend/src/stores/overviewDataStore.ts` 为空。

## 7. #3031 working-set 测试与宿主 `/home` 布局无关

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- File IO / path safety — selected（测试侧）：fake `stat`/`statvfs` 同时认字面路径与 `Path(p).resolve(strict=False)`，在调用时计算；evidence = macOS 3/3 绿 + Linux CI 绿。
- 其余 pack — not selected：生产代码 `packages/common/node27_resource_governance_collection.py` 不改。

- [ ] 7.1 按 issue #3031「解决思路·推荐」改 `tests/test_node27_working_set.py` 的 `observations` fixture 与 `:293` 路径断言；`:297` 同设备身份断言保留；不加 platform skip。

Evidence Floor: macOS `uv run pytest -q "tests/test_node27_working_set.py::test_cli_configured_paths_and_duplicate_device_labels"` 3/3 绿，且改前 3/3 红（记录）；`uv run pytest -q tests/test_node27_working_set.py tests/test_node27_resource_governance.py` ∪ selector 本地绿；PR CI（Linux）定向车道含 `tests/test_node27_working_set.py` 且绿；selector 不选 `tests/test_node27_resource_governance.py`（实测 count=2），本地跑并在 PR 记录；`git diff --stat -- packages/` 为空。

偏离（相对 issue）：issue 写 node-27 验 Linux 不回归；改为 GitHub CI 的 Ubuntu runner（`/home` 非 symlink，同属 Linux 语义），node-27 停机。
