# Tasks: entropy-burndown-2026-10

## 约定（对所有 task 生效）

- **本地验证命令**：后端 `uv run ruff check . && uv run pytest -q <本 task 的测试文件>`；前端 `cd apps/frontend && corepack pnpm lint && corepack pnpm typecheck && corepack pnpm test`；改 `docs/**`/`openspec/specs/**`/`AGENTS.md` 的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **行为保持 oracle**：每个重构 task 的既有测试文件必须零改动通过（spec 明确允许的 monkeypatch 目标迁移除外，且只限该 task 搬走的名字）。
- **oracle 节点**：全部本地；本 change 不改展示端运行时行为，不需要 node-27 live receipt。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。
- **审计引用**：模块与行号以 `docs/governance/entropy-audit-2026-10-16.md` 与 design.md Context 为准；issue 正文只写契约不写行号。

## 1. 导入分层守卫与契约抽取（spec: import-layering-guard）

- [ ] 1.1 新增 `tests/test_import_layering_guard.py` 与 `tests/import_layering_allowlist.json`（初始列出当前 11 处 `packages/common -> workers` 与 1 处 `workers/shud_runtime -> services` 导入，作为 expand 窗口的允许项），按 spec 实现"向上导入即失败、陈旧 allowlist 条目即失败"；在 `scripts/select_ci_tests.py` 为 `packages/**`、`workers/**` 增加该测试的 guarded-owner 路由。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py -k "layering or guard"`；人为在 `packages/common/` 加一行 `from workers.x import y` 确认失败后撤销（PR 描述记录）。

  **Suggested fixture level:** compact - 新增守卫测试与路由规则，无运行时代码。

  **Minimal mergeable slice:** atomic - 守卫、allowlist 与路由三者缺一则守卫要么在 CI 上不跑、要么立刻红。

- [ ] 1.2 expand：新建 `packages/common/contracts/{direct_grid,time_normalization,region,hydrologic_fingerprint}.py`，把 design D1 列出的符号搬入；四个 worker 原模块改为 re-export shim（`base.py` 保留适配器基类，`rewrite.py` 保留算法）；`workers/shud_runtime/runtime.py` 改从 `time_normalization` 取所需助手；`packages/common` 的 7 个文件改为从 `contracts` 导入；allowlist 缩为只剩 shim 自身的 re-export 行。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_forcing_producer*.py tests/test_data_adapters*.py tests/test_mapping_builder*.py tests/test_state_clone*.py tests/test_state_manager*.py tests/test_model_registry*.py`（按实际文件名展开），全部零改动通过；`grep -rn "from workers" packages/` 为空。

  **Suggested fixture level:** expanded - 跨 5 个模块的符号搬迁，70 个导入点，fingerprint 校验与 direct-grid 契约属 project-profile 的 forcing/identity 风险轴。

  **Minimal mergeable slice:** `contracts/direct_grid.py` + `direct_grid_contract.py` shim + `packages/common` 的 6 处 direct_grid 导入改向 - 该模块只依赖 `source_identity`、无 monkeypatch 风险、13 个导入点全部经 shim 保持可用，可独立合并保绿；其余三个契约模块作为后继。

- [ ] 1.3 contract：迁移仓库内全部旧路径导入到 `packages.common.contracts.*`（含 tests），删除 `direct_grid_contract.py`、`region.py` shim，去掉 `base.py`/`rewrite.py` 的 re-export，allowlist 清空。

  Depends on: 1.2

  Verify：`uv run pytest -q tests/test_import_layering_guard.py` 且 allowlist 为 `[]`；`grep -rn "direct_grid_contract\|data_adapters.region" --include='*.py' .` 为空；全量 `uv run pytest -q`（本地或 node-27）通过。

  **Suggested fixture level:** compact - 机械改导入路径加删文件，守卫与全量测试是 oracle。

  **Minimal mergeable slice:** atomic - 删 shim 与改导入必须同 PR，否则要么导入断、要么 shim 留存。

## 2. 状态词汇单一来源（spec: status-vocabulary）

- [ ] 2.1 新增 `packages/common/status_vocab.py`（八个集合 + Literal 别名 + `normalize_qc_verdict`）与 `tests/test_status_vocab_parity.py` 的 schema/DB-enum 对照部分；同 PR 把 `schemas/pipeline_job.schema.json`、`schemas/run_status.schema.json` 的 enum 对齐到词汇（补 `reservation_lost`、`complete`、`published`、`frequency_done`、`pending` 等 design D3 记录的 delta），并在 design.md 记录最终 delta 表。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py`；CI `check-jsonschema` 示例循环通过。

  **Suggested fixture level:** expanded - 改公共 schema enum，project-profile 的 run_status/pipeline_job 契约风险轴。

  **Minimal mergeable slice:** atomic - 词汇与 schema 对齐测试互为 oracle，先改任一边都会让 parity 红。

- [ ] 2.2 `apps/api/routes/pipeline.py` 的状态元组、cycle status 集合与 `_STAGE_ORDER` 无关部分改为从 `status_vocab` 派生；OpenAPI enum 随之重生成，`apps/frontend/src/api/types.ts` 同步重生成。

  Depends on: 2.1

  Verify：`uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py tests/test_pipeline_routes*.py`（按实际文件名）；`cd apps/frontend && corepack pnpm run check:api-types`。

  **Suggested fixture level:** expanded - 改 OpenAPI 对外 enum 与前端生成类型，dual-mounted 路由安全/契约风险轴。

  **Minimal mergeable slice:** atomic - API 元组、OpenAPI 与生成类型必须同 PR，否则 `test_api_contract` 红。

- [ ] 2.3 orchestrator 内 11 个重复集合（design Context 列出）改为导入 `status_vocab`；`tests/test_status_vocab_parity.py` 增加"重复字面量集合扫描 + 缩减 allowlist"部分，初始 allowlist 列出 orchestrator 以外的剩余站点。

  Depends on: 2.1

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal*.py`，既有用例零改动通过。

  **Suggested fixture level:** expanded - orchestrator 状态机的终态集合是 state-machine 不变量。

  **Minimal mergeable slice:** `chain_types.py`、`chain_runtime_utils.py`、`file_orchestration_journal.py` 三个已含 `reservation_lost` 的站点改为导入 - 它们与词汇内容一致，改向无语义变化、可独立保绿；其余 8 个站点（内容本就缺 `reservation_lost`，改向后行为补齐）作为后继并单独记录行为差异。

- [ ] 2.4 其余站点（`packages/common/{forecast_store,met_store}.py`、`workers/output_parser/parser.py`、`services/production_closure/*`、`services/slurm_gateway/real_backend.py`、`services/m24_live/receipt.py`、`scripts/*`、`apps/api/routes/hydro_display_constants.py`）改为导入；QC verdict 站点改用 `normalize_qc_verdict`；allowlist 清空。

  Depends on: 2.3

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py` 且 allowlist 为 `[]`；各站点既有测试零改动通过。

  **Suggested fixture level:** compact - 导入改向加归一化函数调用。

  **Minimal mergeable slice:** `packages/common` 与 `workers/output_parser` 两处 - display-ready 集合内容一致、改向无语义变化；production_closure 的 verdict 归一化作为后继（引入 `normalize_qc_verdict` 调用）。

## 3. 时间/标识助手去重（spec: time-helper-consolidation）

- [ ] 3.1 在 `packages/common/contracts/time_normalization.py` 补齐 `utc_now`、`parse_iso_utc`、`format_iso_utc`（带 docstring 与往返测试）；新增 `tests/test_time_helper_consolidation.py` 与 `tests/time_helper_ceiling.json`（初始 ceiling = 当前计数）。

  Depends on: 1.2

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py`。

  **Suggested fixture level:** compact - 新增三个纯函数与一个计数测试。

  **Minimal mergeable slice:** atomic - 规范函数与 ceiling 测试同生，否则后续批次无 oracle。

- [ ] 3.2 批次 `packages/`：替换本地重复助手为规范导入并删除本地定义，降低 ceiling。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py tests/test_forecast_store*.py tests/test_object_store*.py`（按实际）；ceiling 文件已下调。

  **Suggested fixture level:** compact - 机械替换，既有测试为 oracle。

  **Minimal mergeable slice:** atomic - 单包一批即最小刀；批内再拆无独立验证路径。

- [ ] 3.3 批次 `services/`（含 orchestrator 的 `_utcnow`：保留 facade 名以维持 39 处 monkeypatch，改为 `from ... import utc_now as _utcnow`）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py tests/test_production_scheduler.py tests/test_file_orchestration_journal*.py`。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

- [ ] 3.4 批次 `workers/` + `apps/`。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py` 加各模块既有测试。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

- [ ] 3.5 批次 `scripts/`（含 13 个本地 source 归一化器改为 `normalize_source_id`）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py` 加各脚本既有测试；`grep -rn "def _normalize_source" scripts/` 为空。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

## 4. lint 棘轮（spec: lint-ratchet）

- [ ] 4.1 `pyproject.toml` ruff `select` 加 `B`、`BLE`；用 `ruff check --select B,BLE --output-format json` 生成 `per-file-ignores` 并提交；`uv run ruff check .` 绿。

  Depends on: 无

  Verify：`uv run ruff check .`；在一个未冻结文件临时加 `except Exception: pass` 确认报 BLE001 后撤销。

  **Suggested fixture level:** compact - 配置加生成清单。

  **Minimal mergeable slice:** atomic - 规则与冻结清单必须同 PR。

- [ ] 4.2 新增 `tests/test_any_ratchet.py` 与 `tests/any_ratchet_ceiling.json`（初始 ceiling = 引入时计数），计数方法与审计扫描器一致（AST 注解内 `Any`）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_any_ratchet.py`；把 ceiling 临时调低确认失败后恢复。

  **Suggested fixture level:** compact - 一个计数测试。

  **Minimal mergeable slice:** atomic - 测试与 ceiling 文件同生。

## 5. journal 拆分（spec: orchestrator-structural-burndown；聚簇与顺序见 design D7 与本节）

聚簇（来源：Stage 2 只读结构映射）：A8 context mapping（约 105 行）→ A1 read attribution（185）→ 原语（打破 A7 环的 7 个函数 + 强制类型助手）→ A6 replay/JSON（262）→ A5 cycle scope/candidates/cohorts（约 1 315）→ A7 segments/identity/paths/discovery（约 1 170，**最高风险**：含被 monkeypatch 的名字与其调用者）→ A4 retry helpers（约 2 290，`FileJournalRetryService` 与 `_utcnow` 留在 facade）。Repository 类本身（11 063 行）不在本 change 拆分（Not yet specified）。

- [ ] 5.1 前置：facade 补 `__all__` 完整清单与 `# noqa: F401` 透传名；新增 `tests/test_file_orchestration_journal_facade.py`（re-export 卫生测试：每个 re-export 名至少有一个导入/patch 方）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_file_orchestration_journal_facade.py`；既有 76 个引用 journal 的测试文件零改动通过。

  **Suggested fixture level:** compact - 只加透传声明与一个卫生测试。

  **Minimal mergeable slice:** atomic - 卫生测试与透传清单互为 oracle。

- [ ] 5.2 抽取 A8 → `file_journal_context.py`，A1 → `file_journal_read_attribution.py`。

  Depends on: 5.1

  Verify：既有 journal 测试零改动通过；`tests/test_file_orchestration_journal_facade.py` 通过。

  **Suggested fixture level:** compact - 两个叶子聚簇，无 patch 名。

  **Minimal mergeable slice:** A8 单独 - 最小叶子、无被 patch 名；A1 作为后继。

- [ ] 5.3 抽取原语 → `file_journal_primitives.py`，A6 → `file_journal_replay.py`（`_stat_signature` 的调用者留在 facade 直到 5.5）。

  Depends on: 5.2

  Verify：同 5.2。

  **Suggested fixture level:** compact - 纯函数搬迁。

  **Minimal mergeable slice:** 原语模块单独 - 它是 A7 环的解扣，可独立保绿；A6 作为后继。

- [ ] 5.4 抽取 A5 → `file_journal_candidates.py`（`_run_manifest_model_package_identity` 留 facade 或改为运行时通过 facade 取 `read_bytes_limited_no_follow`）。

  Depends on: 5.3

  Verify：同 5.2，另加 `tests/test_gateway_reconcile_*.py`。

  **Suggested fixture level:** expanded - 候选/队列判定是 scheduler 不变量。

  **Minimal mergeable slice:** atomic - 该聚簇内部互相调用，拆一半会让 facade 与 sibling 互导。

- [ ] 5.5 抽取 A7 → `file_journal_paths.py`，同 PR 把 `read_bytes_limited_no_follow`/`list_directory_no_follow_limited`/`_stat_signature`/`_iter_*` 的 patch 目标改到新模块（`test_file_orchestration_journal_read_cache.py` 8 处、`test_gateway_reconcile_*.py` 10 处）。

  Depends on: 5.4

  Verify：全部 journal 相关测试通过，diff 中 tests 改动仅限 patch 目标字符串。

  **Suggested fixture level:** expanded - 最高风险步骤：文件发现与 identity 校验 + patch 目标迁移。

  **Minimal mergeable slice:** atomic - 被 patch 名与调用者必须同时搬，否则 patch 失效。

- [ ] 5.6 抽取 A4 retry helpers → `file_journal_retry_helpers.py`（`FileJournalRetryService` 与 `_utcnow` 留 facade），随后 contract：facade 去掉无导入方的 re-export，卫生测试断言为零。

  Depends on: 5.5

  Verify：同 5.2；`wc -l services/orchestrator/file_orchestration_journal.py` 较基线下降 ≥ 5 000 行并记入 PR。

  **Suggested fixture level:** compact - 辅助函数搬迁加清理。

  **Minimal mergeable slice:** A4 抽取单独 - 可独立保绿；contract 清理作为后继。

## 6. CI 选择器拆分（spec: ci-test-selector-routing）

固定 changed-path 列表（前后字节一致 oracle）：`packages/common/state_manager.py`、`apps/api/routes/pipeline.py`、`apps/frontend/src/App.tsx`、`docs/runbooks/current-production-ops.md`、`scripts/select_ci_tests.py`、`tests/test_production_scheduler.py`、`db/migrations/000003_enums.sql`。

- [ ] 6.1 前置：`PATH_TEST_RULES` 为三个 sibling 各加一行；`_collection_smoke_required` 匹配 sibling；解决 `scripts` 无 `__init__.py` 的导入路径（沿用其他脚本的 `sys.path` 模式）；记录固定列表的四个输出作为基线文件 `tests/fixtures/select_ci_tests_baseline_outputs.json`。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_select_ci_tests.py`；基线文件与当前输出一致。

  **Suggested fixture level:** compact - 路由规则与基线快照。

  **Minimal mergeable slice:** atomic - 规则行、smoke 匹配与基线三者是同一前置。

- [ ] 6.2 B1 + `PathTestRule` → `select_ci_tests_tables.py`。

  Depends on: 6.1

  Verify：四个输出字节一致；`tests/test_select_ci_tests.py` 通过。

  **Suggested fixture level:** compact - 数据搬迁。

  **Minimal mergeable slice:** atomic - 常量表互相 `*` 展开，拆半则导入断。

- [ ] 6.3 B2 规则表 → `select_ci_tests_rules.py`，同 PR 更新读源码的两处 pin（node-id 扫描、`select_tests` exec）。**最高风险步骤。**

  Depends on: 6.2

  Verify：四个输出字节一致；`tests/test_select_ci_tests.py` 通过，diff 中 tests 改动仅限两处 pin 路径。

  **Suggested fixture level:** expanded - 静默路由回归风险。

  **Minimal mergeable slice:** atomic - 三张规则表引用同一批常量与 `PathTestRule`。

- [ ] 6.4 B3 import-graph derivation → `select_ci_tests_imports.py`，`_SUITE_IMPORTER_PARSE_STATS` patch 目标改到 sibling。

  Depends on: 6.3

  Verify：同 6.3。

  **Suggested fixture level:** compact - 逻辑搬迁，patch 目标随动。

  **Minimal mergeable slice:** atomic - 解析缓存与统计是同一状态。

## 7. 前端 lint 与分层（spec: frontend-lint-and-layering）

- [ ] 7.1 `pages/hydroMet/bootstrap.ts` → `stores/hydroMetBootstrap.ts`（导出不变，4 个导入方改路径）。

  Depends on: 无

  Verify：`corepack pnpm typecheck && corepack pnpm test`；`grep -rn "pages/hydroMet/bootstrap" src/` 为空。

  **Suggested fixture level:** compact - 文件搬迁。

  **Minimal mergeable slice:** atomic - 搬迁与导入改路径同 PR。

- [ ] 7.2 引入 ESLint flat config（三条规则）、`lint` 脚本、`frontend-build` job 加 `pnpm lint` 与 `pnpm check:api-types`；`import/no-cycle` 零报告。

  Depends on: 7.1

  Verify：`corepack pnpm lint` 退出 0；临时在 `src/stores` 加 `@/pages/x` 导入确认报错后撤销；CI job 日志出现两步。

  **Suggested fixture level:** compact - 工具链配置，无运行时代码。

  **Minimal mergeable slice:** atomic - 规则、脚本与 CI 步骤缺一则门不存在。

- [ ] 7.3 `lib/constants.ts` 状态类型改为生成别名，`FAILED_STATUSES` 单一常量，两个 monitoring 组件改用。

  Depends on: 无

  Verify：`corepack pnpm typecheck && corepack pnpm test`。

  **Suggested fixture level:** compact - 类型别名与常量去重。

  **Minimal mergeable slice:** atomic - 别名与消费点同 PR。

- [ ] 7.4 API 错误单一归一化：六个本地归一化器委托或删除；两个静默站点改为记录 store error；新增 vitest 断言。

  Depends on: 无

  Verify：`corepack pnpm test`（新增两个失败路径用例）；mocked-regression lane `corepack pnpm run test:e2e:mocked-regression` 期望值不变。

  **Suggested fixture level:** expanded - 改 API 失败路径，project-profile 的 `unwrapApiData`/`getApi` 消费者契约要求 malformed-payload 测试。

  **Minimal mergeable slice:** 两个静默站点改为记录错误 + 对应 vitest - 可独立保绿；六个归一化器的收敛作为后继。

## 8. 治理报告与 baseline（spec: entropy-automation、governance-entropy-baseline）

- [ ] 8.1 `check_stale_routes.py` 跳过 `openspec/changes/archive/`，`skipped_path_families` 标注作用域；更新 `tests/test_entropy_audit_*` 对应用例。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py tests/test_entropy_audit_retired_paths.py`；`audit_repo_entropy.py --format json` 的 `stale-display-route-token` 计数下降且无 archive 路径。

  **Suggested fixture level:** compact - 一个 path-family 谓词。

  **Minimal mergeable slice:** atomic - 谓词与测试同 PR。

- [ ] 8.2 `tests/test_entropy_audit_baseline_writer_summary.py` 去字面量改读 baseline summary；随后 `uv run python scripts/governance/write_entropy_baseline.py` 刷新 `latest.json` 并提交归档快照。

  Depends on: 8.1

  Verify：`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 在刷新前后均通过；governance workflow 的"baseline not written"步骤在 PR 上通过。

  **Suggested fixture level:** compact - 测试去钉死加一次 writer 运行。

  **Minimal mergeable slice:** 去字面量 - 可独立保绿；刷新 baseline 作为后继（同 PR 亦可，但刷新后计数变化需要先有新断言）。

## 9. 指令文件与退役引用（spec: scoped-agent-instructions、legacy-store-reference-retirement）

- [ ] 9.1 九个模块各一份 AGENTS.md（四节模板），引用与命令可运行。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；死链扫描为零（用审计用的 links 脚本或等价 grep）。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 九个文件独立但共享一次作者通读与同一验证路径，按用户决定合为一个 issue。

- [ ] 9.2 `node27_pgdata_workload_plan.py` 移除 `LEGACY_HYPERTABLE` 与 SQL/默认关系中的引用；新增"字符串只出现在注释/docstring"测试。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node27_pgdata_workload_plan*.py`（按实际）。

  **Suggested fixture level:** compact - 删除一个常量与两处引用。

  **Minimal mergeable slice:** atomic - 常量与引用同删。

- [ ] 9.3 运行手册、ADR 与五个 active spec 的 legacy-store/routing-column 文本改过去时或加 DOC_STATUS 标记。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；`openspec validate --specs --strict --no-interactive`（按 CLI 实际子命令）。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 同一术语的一次性清理。
