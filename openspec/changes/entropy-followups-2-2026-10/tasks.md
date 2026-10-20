## 约定

- 每个 issue 一个 PR，分支 `feat/issue-<N>-entropy-followups-2-2026-10`，从合并后的 master 切出；顺序 #3042 → #3034 → #2986 → #2978。
- 后端 PR 跑「本组 Verification ∪ `select_tests(<git diff --name-only origin/master...HEAD>)`」并在 PR body 记录选出集合与计数。
- 不需要 node-22 / node-27。
- 本目录在最后一个 PR 合并后 `openspec archive`。

## 1. #3042 选择器：facade guard 输入 → current-repo 断言

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Resource limits / large input / discovery — selected：输入路径集合是 `COMPATIBILITY_FACADE_CONFIGS` 的镜像（选择器不 import 仓库模块），必须有 drift pin。
- Public API / CLI / script entry — not selected：选择器 CLI 输出格式不变；`meta_guard_only` 语义不变（由 pin (c) 证明）。
- 其余 pack — not selected。

- [x] 1.1 在 `scripts/select_ci_tests_tables.py` 新增 `COMPATIBILITY_FACADE_GUARD_INPUT_PATHS`（镜像 `scripts/governance/entropy_audit/schema.py` 中 `COMPATIBILITY_FACADE_CONFIGS` 的 `relative_path` 与 `inventory_path`，共 4 个）与 `COMPATIBILITY_FACADE_GUARD_CURRENT_REPO_TESTS`（两个 node id：`tests/test_entropy_audit_report_contract.py::test_compatibility_facade_guard_current_repo_passes_with_inventories`、`tests/test_entropy_audit_baseline_writer_summary.py::test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo`）。
- [x] 1.2 在 `scripts/select_ci_tests.py` 的 `select_tests` 中按 #2323 rider 形状接线：只做集合并，不设 `matched`，不参与 stop 规则，不影响 unknown-backend 回退；`_write_github_output` 计算 `meta_guard_only` 时跳过它；所在整文件已被选中时不追加 node（仿 `scripts/select_ci_tests.py:555-566` 的 `ENTROPY_AUDIT_REPORT_CONTRACT_TEST not in selected` 检查）。facade-patched 全局的读取方式遵循 6.x 约定（`select_tests` 读 facade 全局）。`_collection_smoke_required` docstring 与 `_write_github_output` 注释同步列出新 rider；若既有精确 pin 因此变化，只允许并入这两个 node 并在 PR 列出（fixture review 2026-10-19 实测：没有精确 pin 以这 4 个路径为输入）。
  Known limit（父流程决定不扩 scope）：`docs/**` 不在 `.github/workflows/ci.yml` 的 `backend:` filter 中，只改 inventory 文档的 PR 不开 backend lane，所以 rider 对两份 inventory 只在别的改动打开 lane 时生效；本 PR 不改 ci.yml，PR body 记录此限制。
- [x] 1.3 `tests/test_select_ci_tests.py` 新增 pin：(a) 两个 node id 的函数在对应文件中真实存在（AST，仿 `test_production_topology_hard_gate_node_names_a_real_test_function`）；(b) 镜像集合 == 从 `COMPATIBILITY_FACADE_CONFIGS` 派生的 `{relative_path, inventory_path}`（测试可以 import governance 模块）；(c) 4 个路径逐一参数化：都选中两个 node 且 `meta_guard_only=false`；另参数化 `[tests/test_deleted_x.py, tests/integration_helpers.py] × [两份 inventory]` 组合输入，断言 `meta_guard_only=true` 且 `collection_smoke_required=true`（仿 `test_github_output_topology_rider_does_not_mask_the_meta_guard_collapse`）；(d) changed = `services/orchestrator/scheduler.py` + `scripts/governance/entropy_audit/facade_guard.py` 时，两个整文件在选择结果中、两个 node 不在。

Evidence Floor:
- 对 4 个路径逐个跑 `uv run python scripts/select_ci_tests.py --changed-file <file> --github-output <new tmp>`：`tests_json` 都含两个 node（PR 贴表）。
- 先红后绿：先写 1.3 测试再接线。演练：从镜像集合删一条路径 → (b) 红；删 rider 接线 → (c) 全红。
- `uv run ruff check .`；`uv run pytest -q tests/test_select_ci_tests.py` ∪ selector 选出集。

## 2. #3034 选择器：剩余 14 个 journal facade patch 方

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Resource limits / discovery — selected：以 node id 追加，避免把 49.5 s 的 `test_gateway_reconcile_comment_capability.py` 与 redirect 套件 `test_orchestration_chain.py` 整文件拉进来。
- 其余 pack — not selected。

决定（父流程，fixture review 2026-10-19 后）：按判据取**全集 14 个 node**，不只 issue 点名的 4 个。判据：测试函数体内 import journal facade 并 patch 其全局名（含 `_utcnow`），或函数体内 import `tests.file_journal_legacy_seed_helpers`，或经 `tests/gateway_reconcile_helpers.py` 的 `_file_cohort_repository(versioned=False)` 间接调用 seed helper（它 patch facade 的 `_is_legacy_unversioned_reserved_forecast_master`）。base 082d3a691 上的全集：
  - `tests/test_orchestration_chain.py` 的 9 个 `_utcnow` patcher：`test_public_operator_recovery_reuses_demoted_master_identity_and_reclaims`、`test_public_operator_recovery_hostile_context_attempt_and_anchor_cannot_override`、`test_two_concurrent_public_operator_recovery_cycles_submit_once`、`test_public_operator_recovery_never_reports_pipeline_already_active`、`test_public_operator_recovery_post_append_projection_fault_still_submits_once`、`test_public_operator_recovery_identity_release_stays_non_reclaimable`、`test_public_operator_recovery_automatic_absence_keeps_retry_suffix_replacement`、`test_public_automatic_absence_post_append_projection_fault_still_submits_once`、`test_two_concurrent_public_automatic_absence_cycles_with_projection_fault_submit_once`（参数化函数按函数级 node id 写，不带参数后缀）
  - `tests/test_gateway_reconcile_comment_accounting.py::test_valid_custom_coverage_permits_exactly_one_retry_and_marker_free_is_unchanged`、`::test_legacy_custom_comment_adapter_remains_callable_but_cannot_prove_versioned_zero`
  - `tests/test_gateway_reconcile_comment_capability.py::test_capability_probe_not_called_with_unversioned_only_rows`、`::test_marker_free_historical_master_is_read_only_to_accepted_submit_reconcile`
  - `tests/test_gateway_reconcile_master_transitions.py::test_marker_free_master_compatibility_mutations_remain_available`
  实现者在 base 上重新枚举确认（grep + AST），若与上表不同则以实测为准并在 PR 列出差异。

- [x] 2.1 `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`（`scripts/select_ci_tests_tables.py`）末尾追加这些 node id，附 `# #3034` 注释；同步追加到 `tests/test_select_ci_tests.py` 的 `_FILE_JOURNAL_FACADE_PATCHER_TESTS` 字面量。若某既有测试对选择结果做 node 级精确断言（redirect 套件的 `FILE_JOURNAL_READ_STATE_TESTS` 等）因此变化，只允许并入这些 node，PR 列出。
- [x] 2.2 selector 没有通用的 node/整文件去重；沿用 `FILE_JOURNAL_READ_STATE_TESTS` 的先例（node 与整文件并存，见 `test_select_tests_maps_file_journal_read_state_without_whole_legacy_suites`），不新增去重逻辑。

Evidence Floor:
- 对 facade 与全部 `services/orchestrator/file_journal_*.py` 逐个跑 selector：输出都含新增 node（PR 贴表）。
- 先红后绿：先改字面量（#3028 的 pin 变红），再改表（转绿）。
- 每个新增 node id 用 `uv run pytest --collect-only -q <node>` 证明可收集。
- `uv run ruff check .`；`uv run pytest -q tests/test_select_ci_tests.py` ∪ selector 选出集。

## 3. #2986 拆分 `M11StationForcingPopup.test.tsx`

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Legacy compatibility — selected：收集到的 `it` 标题集合与断言不变（evidence = 集合对比 + `git diff` 中 `expect(` 行只有搬移）。
- 其余 pack — not selected：生产代码零改动。

- [ ] 3.1 按 issue #2986「解决思路·推荐」拆分：共享常量/类型/helper 移入不被收集的 `__tests__/m11StationForcingPopupFixtures.tsx`；`it` 按主题分到 3–5 个 `M11StationForcingPopup<Topic>.test.tsx`；每个新测试文件各自声明 4 条 `vi.mock(...)` 与 `beforeEach`/`afterEach`；原文件删除（或保留为其中一个主题文件）；从 `.large-file-guard.json` 删除该 exclude，不加替代 exclude。

Evidence Floor: 拆分前后 `it` 集合相等（24 个，PR 附对比；比较时去掉 `vitest list` 输出中的文件路径前缀，只比 `<describe> > <title>`，每个新文件的 describe 名保持 `M11StationForcingPopup`）；helper 模块中不得出现 `vi.mock`、顶层 `vi.mocked(...)` 或 hooks；每个新文件 `wc -l` < 1000；`expect(` 行多重集合拆分前后相等（排序后 diff 为空）；`cd apps/frontend && corepack pnpm typecheck && corepack pnpm lint && corepack pnpm test` 绿；`git diff --stat origin/master -- apps/frontend/src ':!apps/frontend/src/**/__tests__/**'` 为空；`.large-file-guard.json` 不含该路径。

## 4. #2978 `getApiErrorMessage` 对非 JSON 错误体 fail-closed

Suggested fixture level: compact
Minimal mergeable slice: atomic

决定（父流程，issue 方案 (a)）：非 `Error` 且非对象的值（原始字符串、数字等）一律返回 `fallback`。理由：契约是 JSON envelope；`apps/api` 无有意返回非 JSON 错误体的路由（实现者复核 `git grep -n "PlainTextResponse\|HTMLResponse\|media_type=.text" apps/api`）；启发式方案多一条规则面且仍可能漏主机名。`Error` 实例与 envelope 行为不变。

Risk packs:
- Error handling / rollback / partial outputs — selected：原始错误体被 fallback 取代；evidence = 4.2 调用面测试。
- Auth / permissions / secrets — selected：代理 banner / 主机名不再外泄到 UI；evidence = 4.1 HTML 行与 4.2 断言不含 `<html`。
- Legacy compatibility — selected：envelope、`Error`、非 envelope 对象三类行为不变；evidence = response.test.ts 对应行不改。
- 其余 pack — not selected。

- [ ] 4.0（阻塞 4.1）`apps/frontend/src/lib/hydroMet/riverForecast.ts:496-499` 的 `capHydroMetRiverForecastMessages` 把客户端生成的契约消息字符串经 `formatHydroMetStationSeriesMessage`（`apps/frontend/src/lib/hydroMet/stationSeries.ts:99`）传给 `getApiErrorMessage`，方案 (a) 会把它们全换成 fallback。先把 `formatHydroMetStationSeriesMessage` 拆成「错误取值」与「字符串格式化（sanitize + token cap + UI string）」两半，cap 函数直接调用字符串那一半；回归 oracle：`apps/frontend/src/pages/hydroMet/__tests__/bootstrap.test.ts:920-986` 的 `messages` 断言不改且保持绿。
- [ ] 4.1 改 `apps/frontend/src/api/response.ts` 非对象分支为返回 `fallback`；`apps/frontend/src/api/__tests__/response.test.ts` 只有 `:26`（HTML）与 `:42`（数字 42）两行期望改为 `fallback`，另新增一行纯文本代理错误 → `fallback`；其余行不改，PR 列出新旧期望。实现前 `git grep` 所有 `getApiErrorMessage(` 调用点：除 4.0 已处理的河段契约消息外，若还有调用方有意传入原始字符串文案，STOP 报告。
- [ ] 4.2 新增一个调用面测试：monitoring store（`apps/frontend/src/stores/monitoring.ts` 的 retry/cancel 或 fetch 路径）以 openapi-fetch 风格的非 JSON 错误（`error` 为 nginx 502 HTML 字符串）驱动，断言状态/抛出的消息是 fallback 文案且不含 `<html`。

Evidence Floor: 先红后绿（4.2 与 HTML 行在改前红）；`cd apps/frontend && corepack pnpm typecheck && corepack pnpm lint && corepack pnpm test`；`corepack pnpm run test:e2e:mocked-regression` 期望值不变（或 PR CI 的 mocked e2e 4 片全绿作为等价证据，若本地无法运行需说明）。
