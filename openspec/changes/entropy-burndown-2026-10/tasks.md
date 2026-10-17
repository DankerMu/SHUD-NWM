# Tasks: entropy-burndown-2026-10

## 约定（对所有 task 生效）

- **本地验证命令**：后端 `uv run ruff check . && uv run pytest -q <本 task 的测试文件>`；前端 `cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`，自 7.2 合并后再加 `corepack pnpm lint`（7.1/7.3/7.4/7.5 不跑 lint）；改 `docs/**`/`openspec/specs/**`/`AGENTS.md`/`infra/**` 的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **行为保持 oracle**：每个重构 task 的既有测试文件必须零改动通过，例外只限各 task 明文列出的"允许的测试改动"（patch 目标字符串、导入/mock 路径、identity 断言的模块对象），且只限该 task 搬走的名字。
- **oracle 节点**：除 2.2（公共 enum 扩一值，需 node-27 live receipt）外全部本地。
- **CI 路由**：新增的守卫/棘轮测试必须在 `scripts/select_ci_tests.py` 的 `select_tests` 中以 #1656/#2185 形态的 **additive loop**（stop 规则循环之后、不设 `matched`、不参与 stop 规则）加选，否则 PR 上不跑或被 `stop_on_match` owner 遮蔽（CLAUDE.md：PR 只跑定向测试）；受影响的精确集合断言按先例同 PR 更新；改了 selector 的 task 都跑 `tests/test_select_ci_tests.py`。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。
- **审计引用**：模块与行号以 `docs/governance/entropy-audit-2026-10-16.md` 与 design.md Context 为准；issue 正文只写契约不写行号。
- **非 atomic 首刀**：带"首刀 Verify"行的 task，首刀 issue 以该行为验收；整 task 的 Verify 由后继 issue 承担。

## 1. 导入分层守卫与契约抽取（spec: import-layering-guard）

- [ ] 1.1 新增 `tests/test_import_layering_guard.py` 与 `tests/import_layering_allowlist.json`（`{file, module}` 条目，初始为当前 11 处 `packages/common -> workers` 与 1 处 `workers/shud_runtime -> services`，按 (file, module) 去重后 9 条；扫描根只有 `apps packages services workers scripts tests`），按 spec 实现"向上导入即失败、条目在 AST 中不存在即失败、向下导入不需条目"；在 `scripts/select_ci_tests.py` 的 `select_tests` 中按 #1656/#2185 形态加一个 additive loop：`packages/**`、`workers/**` 任一路径改动即加选该测试（不参与 stop 规则）。允许的测试改动：`tests/test_select_ci_tests.py` 中因该 rider 而变的精确集合断言（同 #1656/#2185 先例，PR 列出）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py`；守卫单测含"向下导入条目不报"与"导入已删除即 stale"两个样例；selector 断言：只改 `workers/forcing_producer/direct_grid_contract.py`（stop 规则 owner）时守卫仍被选中；人为在 `packages/common/` 加一行 `from workers.x import y` 确认失败后撤销（PR 描述记录）。

  **Suggested fixture level:** compact - 新增守卫测试与路由规则，无运行时代码。

  **Minimal mergeable slice:** atomic - 守卫、allowlist 与路由三者缺一则守卫要么在 CI 上不跑、要么立刻红。

- [ ] 1.2 expand：新建 `packages/common/contracts/{direct_grid,time_normalization,region,hydrologic_fingerprint}.py`，把 spec 列出的符号搬入（`time_normalization` 含 `base.py` 九个纯助手 + `check_three_way_time_consistency`；`hydrologic_fingerprint` 含 `verify_hydrologic_core_fingerprint_equal` 的 AST 传递闭包：基类、四个异常、`HydrologicCoreFingerprint`、`compute_hydrologic_core_fingerprint`、`_validate_category_files`、三个常量等，PR 附闭包清单）；五个原模块改为显式 re-export shim（`direct_grid_contract.py`、`region.py`、`base.py` 保留适配器基类与 dataclass、`rewrite.py` 其余异常改继承契约基类、`time_consistency.py`）；`workers/shud_runtime/runtime.py` 与 `packages/common` 的 7 个文件改从 `contracts` 导入；allowlist 变为 `[]`；selector 在 `PATH_TEST_RULES` 为四个 contract 模块各加一行与旧路径 owner 规则等价的规则（相同测试集、相同 `stop_on_match` 语义）。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_forcing_producer*.py tests/test_data_adapter_resolution.py tests/test_era5_adapter.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_shud_runtime.py tests/test_mapping_builder*.py tests/test_state_clone*.py tests/test_state_manager*.py tests/test_model_registry*.py tests/test_orchestration_chain.py`，全部零改动通过；`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers' packages/` 为空（macOS grep 不认 `\s`）；selector 断言"改 `packages/common/contracts/direct_grid.py` 选中 direct-grid 契约测试集"。

  首刀 Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_model_registry*.py tests/test_state_clone*.py`；allowlist 中不再有 `direct_grid_contract` 条目；`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers\.forcing_producer\.direct_grid_contract' packages/` 为空（docstring/注释中的提及不计）。

  **Suggested fixture level:** expanded - 跨 5 个模块的符号搬迁，约 65 个导入文件，fingerprint 校验与 direct-grid 契约属 project-profile 的 forcing/identity 风险轴。

  **Minimal mergeable slice:** `contracts/direct_grid.py` + `direct_grid_contract.py` shim + selector 等价规则行 + `packages/common` 的 4 个文件 7 行 direct_grid 导入改向 - 该模块只依赖 `source_identity`、无 monkeypatch 风险、12 个导入文件全部经 shim 保持可用，可独立合并保绿；其余三个契约模块作为后继。

- [ ] 1.3 contract：迁移仓库内全部旧路径导入与模块属性访问到 `packages.common.contracts.*`（含 tests；`chain_compat_runtime` 的 owner 模块改指向契约模块），删除 `direct_grid_contract.py`、`region.py` shim，去掉 `base.py`/`rewrite.py`/`time_consistency.py` 的 re-export；`tests/test_import_layering_guard.py` 增加 contract 检查函数（AST 扫描 `from`/`import`/`import … as` 无旧路径、无旧模块的被搬名字）。允许的测试改动：`tests/test_orchestration_chain.py` 中与旧 owner 模块比较的 identity 断言改为契约模块，及其钉住的 `CHAIN_COMPATIBILITY_INVENTORY.md` owner 行同步更新。

  Depends on: 1.2

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_orchestration_chain.py`（含新增的 contract 检查函数）；`uv run python -c "import services.orchestrator.chain"` 成功；全量 `uv run pytest -q`（本地或 node-27）通过。

  **Suggested fixture level:** compact - 机械改导入路径加删文件，守卫与全量测试是 oracle。

  **Minimal mergeable slice:** atomic - 删 shim 与改导入必须同 PR，否则要么导入断、要么 shim 留存；importer 迁移本身在 1.2 的 shim 窗口内已可合并，这里的刀是 contract。

## 2. 状态词汇单一来源（spec: status-vocabulary）

- [ ] 2.1 新增 `packages/common/status_vocab.py`（design D3 表中全部命名集合 + 三个有序 tuple + `Literal` 别名 + `normalize_qc_verdict`；hydro 的三个集合从 `scheduler_state_types.py` 移入并由其同名重新绑定同一对象）与 `tests/test_status_vocab_parity.py`：每个命名集合对照从 canonical 站点钉入测试的字面量；schema/折叠 DB enum 对照（复用 `tests/test_hydro_status_set_parity.py` 的折叠 helper）；重复字面量扫描（排除 `status_vocab.py` 自身）+ `tests/status_literal_allowlist.json`（在当前树上跑扫描，命中逐条裁决，键为 `file` + 排序成员 + reason）；每个命名集合的 Python 类型（set/frozenset/tuple 含顺序）与 canonical 站点一致。同 PR：`schemas/pipeline_job.schema.json` enum 加 `reservation_lost`（按 `PIPELINE_JOB_STATUS_ORDER` 顺序）；selector 以 #1656 形态的 additive loop 为 `apps|packages|services|workers|scripts/**` 加选该测试（允许的测试改动：受影响的精确集合断言，PR 列出）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_hydro_status_set_parity.py tests/test_select_ci_tests.py`；CI `check-jsonschema` 示例循环通过；PR 附 allowlist 裁决表。

  **Suggested fixture level:** expanded - 改公共 schema enum，project-profile 的 run_status/pipeline_job 契约风险轴。

  **Minimal mergeable slice:** atomic - 词汇与对照测试互为 oracle，先改任一边都会让 parity 红。

- [ ] 2.2 `apps/api/routes/pipeline.py` 的状态 tuple 与四个本地集合改为从 `status_vocab` 绑定（tuple 用 `PIPELINE_JOB_STATUS_ORDER`）；手改 `openapi/nhms.v1.yaml` 的 enum（该文件为手工维护）；`cd apps/frontend && corepack pnpm generate:api` 重生成 `src/api/types.ts`。

  Depends on: 2.1

  Verify：`uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py tests/test_api_contract_pipeline_ops.py tests/test_pipeline_logs_artifacts.py tests/test_response_model_preservation_pipeline.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_file_journal_full_tree_budget_contract.py`；`PYTHONHASHSEED=0` 与 `=1` 下 `tests/test_openapi_drift.py` 均通过；`cd apps/frontend && corepack pnpm run check:api-types`；**node-27 live receipt**：display API 对含 `reservation_lost` 行的 pipeline 查询返回 200 且 schema 校验通过（CLAUDE.md oracle 路由）。

  **Suggested fixture level:** expanded - 改 OpenAPI 对外 enum 与前端生成类型，dual-mounted 路由安全/契约风险轴。

  **Minimal mergeable slice:** atomic - API tuple、OpenAPI 与生成类型必须同 PR，否则 `test_api_contract` 红。

- [ ] 2.3 orchestrator 内站点改为导入命名集合（D3 表 orchestrator 行：`chain_types` 绑 `set(PIPELINE_JOB_TERMINAL)` 并继续作为共享对象、`chain_runtime_utils`、journal `:621`/`:707`、`scheduler`、`scheduler_candidates`、`persistence`、`scheduler_state_types` 重新绑定、六处 success 集合、`accepted_submit_identity` 派生 + parity pin）；allowlist 中这些条目删除。

  Depends on: 2.1

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forced_resubmit_veto.py tests/test_hydro_status_set_parity.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`，既有用例零改动通过。

  首刀 Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forced_resubmit_veto.py tests/test_orchestration_chain.py`；allowlist 中 `chain_types`、`chain_runtime_utils`、journal `:621` 条目已删除。

  **Suggested fixture level:** expanded - orchestrator 状态机的终态集合是 state-machine 不变量。

  **Minimal mergeable slice:** `chain_types.py`、`chain_runtime_utils.py`、`file_orchestration_journal.py:621` 三个 `PIPELINE_JOB_TERMINAL` 站点改为绑定 - 内容逐字相同（7 值）、`chain_types` 保持 `set` 与共享对象，`test_forced_resubmit_veto` 的类型/identity 钉死照旧通过；其余站点（各自对应不同命名集合，同样内容相同）作为后继。

- [ ] 2.4 其余站点改为导入（tuple 站点绑定词汇中同序的 tuple：`packages/common/{forecast_store,met_store}.py`、`workers/output_parser/parser.py`、`apps/api/routes/hydro_display_constants.py`、`services/production_closure/{slurm_validation,met_validation,readiness_item_contracts,readiness_scheduler_evidence}.py`、`services/slurm_gateway/real_backend.py`、`scripts/{run_qhh_continuous,node27_frontier_stall_alert,m24_gateway_proof}.py`、`scripts/basin_retirement/database.py`）；QC verdict **读取/比较**站点改用 `normalize_qc_verdict`（receipt 写入与校验器保持原拼写，allowlist `distinct-vocabulary`）；allowlist 中无 `pending-migration` 条目。

  Depends on: 2.3

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forecast_store_routing.py tests/test_output_parser.py tests/test_output_parser_dual_write.py tests/test_output_parser_cli.py tests/test_production_slurm_validation.py tests/test_production_met_validation.py tests/test_m24_receipt.py tests/test_m24_gateway_proof.py` 加 `scripts/select_ci_tests.py` 对本 PR diff 选出的集合（`met_store`、`real_backend`、`basin_retirement` 无同名测试文件，PR 记录选出集合）；allowlist 无 `pending-migration`；m24 receipt fixture 带 `PASS` 仍通过校验。

  首刀 Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forecast_store_routing.py tests/test_output_parser.py tests/test_output_parser_dual_write.py tests/test_hydro_display_mvt_scaling.py`；allowlist 中 `forecast_store`、`met_store`、`parser`、`hydro_display_constants` 条目已删除。

  **Suggested fixture level:** compact - 导入改向加归一化函数调用。

  **Minimal mergeable slice:** `packages/common` 两文件 + `workers/output_parser` + `hydro_display_constants` 四处 - 各自绑定内容相同的命名集合（display-ready 与 parse-ready 是两个集合），无语义变化；production_closure 的 verdict 读侧归一化作为后继（引入 `normalize_qc_verdict` 调用）。

## 3. 时间/标识助手去重（spec: time-helper-consolidation）

- [ ] 3.1 在 `packages/common/contracts/time_normalization.py` 补齐 `utc_now`、`parse_iso_utc`、`format_iso_utc`（带 docstring 与往返测试）；新增 `tests/test_time_helper_consolidation.py`（fullmatch 规则）、`tests/time_helper_ceiling.json`（初始 ceiling = 当前计数，PR 附命中清单）与 `tests/time_helper_allowlist.json`（空或含已裁决的不同概念函数）；selector 以 #1656 形态的 additive loop 加选（允许的测试改动：受影响的精确集合断言，PR 列出）。

  Depends on: 1.2

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py tests/test_select_ci_tests.py`。

  **Suggested fixture level:** compact - 新增三个纯函数与一个计数测试。

  **Minimal mergeable slice:** atomic - 规范函数与 ceiling 测试同生，否则后续批次无 oracle。

- [ ] 3.2 批次 `packages/`：替换 3.1 命中清单中 `packages/` 下的函数（fullmatch 口径，时间助手 + `object_store_forcing._normalize_source_id` 一处归一化器）为规范导入并删除本地定义，降低 ceiling。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py tests/test_forecast_store_routing.py tests/test_object_store_forcing.py tests/test_object_store_roots.py tests/test_state_manager*.py` 加 selector 对本 PR diff 选出的集合（PR 记录）；ceiling 文件已下调。

  **Suggested fixture level:** compact - 机械替换，既有测试为 oracle。

  **Minimal mergeable slice:** atomic - 单包一批即最小刀；批内再拆无独立验证路径。

- [ ] 3.3 批次 `services/`：3.1 命中清单中 `services/` 下的函数（含 orchestrator 的 `_utcnow`：保留 facade 名以维持 monkeypatch，改为 `from ... import utc_now as _utcnow`；fullmatch 命中的三个归一化器；`scheduler_runtime_roots._normalize_sources(Sequence)` 不命中、不动）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py tests/test_production_scheduler.py tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

- [ ] 3.4 批次 `workers/` + `apps/`：3.1 命中清单中这两个根下的函数（`apps/` 下无 fullmatch 命中的归一化器；`_normalize_source_for_query/_strict` 名字不匹配、不动）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py` 加被改模块的既有测试（PR 列出文件名）。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

- [ ] 3.5 批次 `scripts/`：3.1 命中清单中 `scripts/` 下的函数（时间助手 17 处；`_normalize_sources(values, env)` 名字不匹配、不动）。

  Depends on: 3.1

  Verify：`uv run pytest -q tests/test_time_helper_consolidation.py` 加被改脚本的既有测试（PR 列出文件名）；`tests/test_time_helper_consolidation.py` 在 `scripts/` 下报零非 allowlist 命中。

  **Suggested fixture level:** compact - 同 3.2。

  **Minimal mergeable slice:** atomic - 同 3.2。

## 4. lint 棘轮（spec: lint-ratchet）

- [ ] 4.1 `pyproject.toml` ruff `select` 加 `B`、`BLE`；用 `ruff check --select B,BLE --output-format json` 生成 `per-file-ignores` 并提交；`uv run ruff check .` 绿。

  Depends on: 无

  Verify：`uv run ruff check .`；在一个未冻结文件临时加 `except Exception: pass` 确认报 BLE001 后撤销。

  **Suggested fixture level:** compact - 配置加生成清单。

  **Minimal mergeable slice:** atomic - 规则与冻结清单必须同 PR。

- [ ] 4.2 新增 `tests/test_any_ratchet.py` 与 `tests/any_ratchet_ceiling.json`（初始 ceiling = 引入时计数；计数定义见 spec：注解内 `Name`/`Attribute` `Any`，字符串注解先解析）；selector 以 #1656 形态的 additive loop 加选（允许的测试改动：受影响的精确集合断言，PR 列出）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_any_ratchet.py tests/test_select_ci_tests.py`；把 ceiling 临时调低确认失败后恢复。

  **Suggested fixture level:** compact - 一个计数测试。

  **Minimal mergeable slice:** atomic - 测试与 ceiling 文件同生。

## 5. journal 拆分（spec: orchestrator-structural-burndown；聚簇顺序见 design D7）

聚簇顺序：context → read_attribution → primitives（打破 paths 环的标识/段原语）→ replay → candidates → paths（**最高风险**：含被 monkeypatch 名的调用者）→ retry_helpers。`FileOrchestrationJournalRepository`、`FileJournalRetryService` 与 facade 驻留的被 patch 名不搬。函数级归属由 5.0 的 map 决定，本节不再估算行数。

- [ ] 5.0 聚簇图：新增 `tests/fixtures/file_journal_cluster_map.json`（每个顶层定义 → 一个聚簇或 `facade`；每个被 patch 名 → 其聚簇与全部调用点聚簇；全部九个被 patch 名（`_utcnow`、Repository 调用的五个 IO/stat/iter 名、以及 Repository/RetryService 调用的 `_released_candidate_cycle_scope`、`_cycle_scope_from_job_id`、`_next_file_manual_retry_job_id_for_run`）标为 `facade`）与 `tests/test_file_journal_cluster_map.py`（覆盖、唯一、被 patch 名规则）。

  Depends on: 2.3, 3.3

  Verify：`uv run pytest -q tests/test_file_journal_cluster_map.py`；PR 附 `grep` 得到的全部 patch 点清单（含 `tests/test_file_orchestration_journal.py` 与 `test_gateway_reconcile_writer_quiescence.py` 的点）。

  **Suggested fixture level:** compact - 只读映射加一个覆盖测试，不改运行时代码。

  **Minimal mergeable slice:** atomic - map 与覆盖测试互为 oracle。

- [ ] 5.1 前置：facade 补 `__all__`（只列被导入的名）与 `# noqa: F401` 透传；新增 `tests/test_file_orchestration_journal_facade.py`（re-export 卫生测试：`__all__` 每个名至少有一个 AST 解析（别名属性访问、字符串 patch 目标）得到的使用方）。

  Depends on: 5.0

  Verify：`uv run pytest -q tests/test_file_orchestration_journal_facade.py tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`；既有 76 个引用 journal 的测试文件零改动通过。

  **Suggested fixture level:** compact - 只加透传声明与一个卫生测试。

  **Minimal mergeable slice:** atomic - 卫生测试与透传清单互为 oracle。

- [ ] 5.2 抽取 context → `file_journal_context.py`，read_attribution → `file_journal_read_attribution.py`（按 map）。

  Depends on: 5.1

  Verify：`uv run pytest -q tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py tests/test_file_orchestration_journal_facade.py tests/test_file_journal_cluster_map.py`，零改动通过。

  首刀 Verify：同上，map 中 `context` 聚簇的名字已不在 facade 顶层定义中。

  **Suggested fixture level:** compact - 两个叶子聚簇，无被 patch 名。

  **Minimal mergeable slice:** context 单独 - 最小叶子、无被 patch 名；read_attribution 作为后继。

- [ ] 5.3 抽取 primitives → `file_journal_primitives.py`，replay → `file_journal_replay.py`。

  Depends on: 5.2

  Verify：同 5.2。

  首刀 Verify：同 5.2，`primitives` 聚簇已搬出。

  **Suggested fixture level:** compact - 纯函数搬迁。

  **Minimal mergeable slice:** primitives 模块单独 - 它是 paths 环的解扣，可独立保绿；replay 作为后继。

- [ ] 5.4 抽取 candidates → `file_journal_candidates.py`：`_released_candidate_cycle_scope`、`_cycle_scope_from_job_id`、`_next_file_manual_retry_job_id_for_run` 被 Repository/RetryService 调用（且测试直接以 `journal_module.<name>` 调用），**留在 facade**；搬走的调用者与调用其它 facade 驻留名（如 `read_bytes_limited_no_follow`）的函数用函数体内延迟导入经 facade 属性取用。无测试改动。

  Depends on: 5.3

  Verify：同 5.2，另加 `tests/test_gateway_reconcile_*.py`；新增断言：patch facade 的 `read_bytes_limited_no_follow` 为计数 stub 后调用已搬到 `file_journal_candidates.py` 的 `_run_manifest_model_package_identity`（map 中该聚簇唯一调用 facade 驻留名的函数；若 map 另有，以 map 为准），计数 > 0。

  **Suggested fixture level:** expanded - 候选/队列判定是 scheduler 不变量。

  **Minimal mergeable slice:** atomic - 该聚簇内部互相调用，拆一半会让 facade 与 sibling 互导。

- [ ] 5.5 抽取 paths → `file_journal_paths.py`：`_stat_signature`、`_iter_regular_json_files`、`_iter_jsonl_files`、`read_bytes_limited_no_follow`、`list_directory_no_follow_limited` **留在 facade**（Repository 调用它们）；搬走的顶层调用者改为函数体内延迟导入经 facade 属性调用。无测试改动。

  Depends on: 5.4

  Verify：全部 journal 相关测试零改动通过；新增断言：patch `file_orchestration_journal._stat_signature` 为计数 stub 后调用一个已搬到 `file_journal_paths.py` 的函数，计数 > 0。

  **Suggested fixture level:** expanded - 最高风险步骤：文件发现与 identity 校验路径搬迁。

  **Minimal mergeable slice:** atomic - 搬走的调用者与延迟查找规则必须同 PR，否则 facade patch 对搬走的调用者失效。

- [ ] 5.6 抽取 retry_helpers → `file_journal_retry_helpers.py`（`FileJournalRetryService` 与 `_utcnow` 留 facade；调用 `_utcnow` 的函数经延迟导入取 facade 属性，8 个 patch `_utcnow` 的测试文件零改动），随后 contract：facade `__all__` 去掉无使用方的名，卫生测试与 map 测试断言 facade 顶层定义 == map 的 `facade` 聚簇。

  Depends on: 5.5

  Verify：同 5.2 加 8 个 patch `_utcnow` 的测试文件；`tests/test_file_journal_cluster_map.py` 的"facade 顶层 == map facade"断言通过；PR 记录 facade 行数前后值（信息项，非验收）。

  首刀 Verify：同 5.2 加 8 个 `_utcnow` 测试文件，`retry_helpers` 聚簇已搬出。

  **Suggested fixture level:** expanded - 最大聚簇，且含 `_utcnow` 被 patch 调用者。

  **Minimal mergeable slice:** retry_helpers 抽取单独 - 可独立保绿；contract 清理作为后继。

## 6. CI 选择器拆分（spec: ci-test-selector-routing）

固定 changed-path 列表（前后字节一致 oracle，在每个 6.x PR 的父提交与 head 各跑一次比较，不提交快照）：`packages/common/state_manager.py`、`apps/api/routes/pipeline.py`、`apps/frontend/src/App.tsx`、`docs/runbooks/current-production-ops.md`、`scripts/select_ci_tests.py`、`tests/test_production_scheduler.py`、`db/migrations/000003_enums.sql`。

- [ ] 6.1 前置：`PATH_TEST_RULES` 为三个 sibling 各加一行；`_collection_smoke_required` 匹配 sibling；解决 `scripts` 无 `__init__.py` 的导入路径（沿用其他脚本的 `sys.path` 模式）；盘点 `tests/test_select_ci_tests.py` 在 facade 上 patch 的全部全局名并写入 PR 描述；`tests/test_select_ci_tests.py` 新增 "只改 `scripts/select_ci_tests_rules.py` 时返回与改 facade 相同的 meta-guard 集合"断言；新增 `scripts/ci_selector_snapshot.sh`（对固定列表输出四个 output 的 JSON，供父提交/head 比较）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_select_ci_tests.py`；`scripts/ci_selector_snapshot.sh` 在父提交与 head 输出字节一致（固定列表不含 sibling 路径；sibling 路由由 spec 的 "Sibling edit selects the meta guard" 断言覆盖）。

  **Suggested fixture level:** compact - 路由规则与快照脚本。

  **Minimal mergeable slice:** atomic - 规则行、smoke 匹配与快照脚本是同一前置。

- [ ] 6.2 B1 + `PathTestRule` → `select_ci_tests_tables.py`；facade 以导入绑定同名。

  Depends on: 6.1

  Verify：父提交与 head 四个输出字节一致；`tests/test_select_ci_tests.py` 零改动通过。

  **Suggested fixture level:** compact - 数据搬迁。

  **Minimal mergeable slice:** atomic - 常量表互相 `*` 展开，拆半则导入断。

- [ ] 6.3 B2 规则表 → `select_ci_tests_rules.py`，facade 以导入绑定 `PATH_TEST_RULES` 等（`select_tests` 继续读 facade 全局，patch 生效）；同 PR 更新读源码的两处 pin（node-id 扫描、`select_tests` exec）。**最高风险步骤。**

  Depends on: 6.2

  Verify：父提交与 head 四个输出字节一致；`tests/test_select_ci_tests.py` 通过，diff 中 tests 改动仅限两处 pin 路径；"patch facade `PATH_TEST_RULES` 仍能改变输出"断言通过。

  **Suggested fixture level:** expanded - 静默路由回归风险。

  **Minimal mergeable slice:** atomic - 三张规则表引用同一批常量与 `PathTestRule`。

- [ ] 6.4 B3 import-graph derivation 与 `_SUITE_IMPORTER_PARSE_STATS` → `select_ci_tests_imports.py`（所有读者一起搬）；搬走的函数读 facade 可 patch 全局时由 `select_tests` 以参数传入；允许的测试改动：`_SUITE_IMPORTER_PARSE_STATS` 的全部引用（两个测试中的四行：两处 setattr 与两处读取）。

  Depends on: 6.3

  Verify：同 6.3；tests 改动仅限 `_SUITE_IMPORTER_PARSE_STATS` 的引用行。

  **Suggested fixture level:** compact - 逻辑搬迁，patch 目标随动。

  **Minimal mergeable slice:** atomic - 解析缓存与统计是同一状态。

## 7. 前端 lint 与分层（spec: frontend-lint-and-layering）

- [ ] 7.1 `pages/hydroMet/bootstrap.ts` → `stores/hydroMetBootstrap.ts`（导出不变；7 个生产导入方与 8 个测试文件（含 `bootstrap.test.ts` 的路径读取）只改路径字符串，共 15 个文件）。

  Depends on: 无

  Verify：`corepack pnpm typecheck && corepack pnpm test`；`git grep -n "pages/hydroMet/bootstrap" apps/frontend/src` 为空；diff 中测试改动仅限路径字符串。

  **Suggested fixture level:** compact - 文件搬迁。

  **Minimal mergeable slice:** atomic - 搬迁与导入/mock 改路径同 PR。

- [ ] 7.2 引入 ESLint flat config（三条规则，依赖 `typescript-eslint`、`eslint-plugin-import-x`、`eslint-import-resolver-typescript`；restricted-imports 块 `ignores` 测试文件）、`lint` 脚本、`frontend-build` job 加 `pnpm lint`。

  Depends on: 7.1

  Verify：`corepack pnpm lint` 退出 0；临时在 `src/stores` 非测试文件加 `@/pages/x` 导入确认报 `no-restricted-imports` 后撤销；临时在 `src/lib` 注入 `@/` 互导环确认报 `import-x/no-cycle` 后撤销；CI job 日志出现 lint 步。

  **Suggested fixture level:** compact - 工具链配置，无运行时代码。

  **Minimal mergeable slice:** atomic - 规则、脚本与 CI 步骤缺一则门不存在。

- [ ] 7.3 `lib/constants.ts` 状态类型改为生成别名，`FAILED_STATUSES` 单一常量（成员不变），两个 monitoring 组件改用。

  Depends on: 无

  Verify：`corepack pnpm typecheck && corepack pnpm test`。

  **Suggested fixture level:** compact - 类型别名与常量去重。

  **Minimal mergeable slice:** atomic - 别名与消费点同 PR。

- [ ] 7.4 API 错误单一归一化：三个本地归一化器委托 `getApiErrorMessage` 或删除，`apiErrorCode` 改为解析共享 `ApiErrorShape`；`useNationalBasinGeo` 加错误载体（`NationalBasinGeoState.error`，失败不缓存）；`lib/mvtLayerMetadata.ts` 的无调用方 loader 删除；新增 vitest 断言。

  Depends on: 7.1

  Verify：`corepack pnpm test`（新增 `useNationalBasinGeo` 失败路径用例 + absent-data 渲染断言；loader 删除后 `git grep` 无引用）；mocked-regression lane `corepack pnpm run test:e2e:mocked-regression` 期望值不变。

  首刀 Verify：`corepack pnpm test`，失败路径用例通过，loader 已删除。

  **Suggested fixture level:** expanded - 改 API 失败路径，project-profile 的 `unwrapApiData`/`getApi` 消费者契约要求 malformed-payload 测试。

  **Minimal mergeable slice:** `useNationalBasinGeo` 错误载体 + 删除死 loader + 对应 vitest - 可独立保绿；四个归一化器的收敛作为后继。

- [ ] 7.5 `frontend-build` job 加 `pnpm check:api-types` 步。

  Depends on: 无

  Verify：CI job 日志出现该步并通过；临时改 `openapi/nhms.v1.yaml` 一个描述字段不重生成，确认该步失败后撤销（PR 记录）。

  **Suggested fixture level:** none - 一行 CI 配置。

  **Minimal mergeable slice:** atomic - 单行。

## 8. 治理报告与 baseline（spec: entropy-automation、governance-entropy-baseline）

- [ ] 8.1 `check_stale_routes.py` 跳过 `openspec/changes/archive/`，`skipped_path_families` 加一条作用域字符串；更新 `tests/test_entropy_audit_*` 对应用例。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py tests/test_entropy_audit_retired_paths.py`；`audit_repo_entropy.py --format json` 的 `stale-display-route-token` 计数下降且无 archive 路径。

  **Suggested fixture level:** compact - 一个 path-family 谓词。

  **Minimal mergeable slice:** atomic - 谓词与测试同 PR。

- [ ] 8.2 `tests/test_entropy_audit_baseline_writer_summary.py` 删除对 tracked 计数的两条字面量断言、保留 HEAD inventory 比较并加 tracked 断言（正整数、`total_modules == len(modules)`）；随后 `uv run python scripts/governance/write_entropy_baseline.py` 刷新 `latest.json` 并提交归档快照。

  Depends on: 8.1

  Verify：`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 在刷新前后均通过；刷新后临时新增一个 `tests/test_zz_tmp.py` 确认仍通过后删除；governance workflow 的"baseline not written"步骤在 PR 上通过。

  首刀 Verify：`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 在未刷新的树上通过。

  **Suggested fixture level:** compact - 测试去钉死加一次 writer 运行。

  **Minimal mergeable slice:** 去字面量 - 可独立保绿；刷新 baseline 作为后继（同 PR 亦可）。

## 9. 指令文件与退役引用（spec: scoped-agent-instructions、legacy-store-reference-retirement）

- [ ] 9.1 九个模块（含 `services/precip`，不含 `packages/scheduler`）各一份 AGENTS.md（四节模板），引用与命令可运行。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；对九个文件做相对路径与反引号仓库路径存在性检查（一行 shell，PR 记录）为零死链。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 九个文件共享一次作者通读与同一验证路径。

  **Width exception:** merged-tasks - 九个文件互相独立但没有代码、只有一条验证路径，拆成九个 issue 对交付没有价值（用户决定）。

- [ ] 9.2 `node27_pgdata_workload_plan.py` 移除 `LEGACY_HYPERTABLE` 与三处引用（SQL `IN` 列表、候选加载器父表集合、默认 `expected_relations`）；新增"字符串只出现在注释/docstring/`_REFUSAL_ADVICE`"测试；新增 catalog 无 legacy 表时"无缺失关系"用例与"legacy 父表候选被拒 `PLAN_UNRELATED_PARENT`"用例。允许的测试改动：`tests/test_node27_pgdata_workload.py` 中喂入 legacy 候选并期待成功的用例改为期待拒绝。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node27_pgdata_workload_plan.py tests/test_node27_pgdata_workload.py tests/test_display_coverage_refresh.py`。

  **Suggested fixture level:** compact - 删除一个常量与三处引用，一个用例改向。

  **Minimal mergeable slice:** atomic - 常量与引用同删。

- [ ] 9.3 三个 runbook/ADR 与五个 active spec（spec 点名）的 legacy-store/routing-column 文本改过去时或加 DOC_STATUS 标记。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；`openspec validate --specs --no-interactive`（非 strict：HEAD 上 201 个既有 spec 因缺 scenario 不过 strict，与本 change 无关）。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 同一术语的一次性清理。
