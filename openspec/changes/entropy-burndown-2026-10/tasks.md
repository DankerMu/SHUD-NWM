# Tasks: entropy-burndown-2026-10

## 约定（对所有 task 生效）

- **本地验证命令**：后端 `uv run ruff check . && uv run pytest -q <本 task 的测试文件>`；前端 `cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`，自 7.2 合并后再加 `corepack pnpm lint`（7.1/7.3/7.4/7.5 不跑 lint）；改 `docs/**`/`openspec/specs/**`/`AGENTS.md`/`infra/**` 的 task 另跑 `uv run pytest -q tests/test_entropy_audit_report_contract.py`。
- **行为保持 oracle**：每个重构 task 的既有测试文件必须零改动通过，例外只限各 task 明文列出的"允许的测试改动"（patch 目标字符串、导入/mock 路径、identity 断言的模块对象），且只限该 task 搬走的名字。
- **oracle 节点**：除 2.2（公共 enum 扩一值，需 node-27 live receipt）外全部本地。
- **CI 路由**：新增的守卫/棘轮测试必须在 `scripts/select_ci_tests.py` 的 `select_tests` 中以 #1656/#2185 形态的 **additive loop**（stop 规则循环之后、不设 `matched`、不参与 stop 规则）加选，否则 PR 上不跑或被 `stop_on_match` owner 遮蔽（CLAUDE.md：PR 只跑定向测试）；受影响的精确集合断言按先例同 PR 更新；改了 selector 的 task 都跑 `tests/test_select_ci_tests.py`。
- **依赖**：`Depends on` 列出的是必须先合并的 task；未列出的 task 之间可并行。
- **审计引用**：模块与行号以 `docs/governance/entropy-audit-2026-10-16.md` 与 design.md Context 为准；issue 正文只写契约不写行号。
- **非 atomic 首刀**：带"首刀 Verify"行的 task，首刀 issue 以该行为验收；整 task 的 Verify 由后继 issue 承担。

## Risk packs（issue-risk-contract.md；selected 的每一项映射到下方某 task 的测试、Verify 命令或显式 non-goal）

- Public API / CLI / script entry — selected：2.2 改 `openapi/nhms.v1.yaml` 对外 enum 与 `apps/api/routes/pipeline.py` 状态 tuple（node-27 live receipt + 生成类型 diff）；6.x 拆 `scripts/select_ci_tests.py` 时 CLI 与 `select_tests` facade 签名不变（6.1 快照脚本的字节级输出比较是 oracle）；7.5 CI job 加一步；6.1 新增本地专用 `scripts/ci_selector_snapshot.sh`（oracle：父/head 字节一致）；4.1 新增不按路径 scope 的 `lint-ruff` CI job。其余 task 不新增入口。
- Config / project setup — selected：4.1 ruff `select` + 生成的 `per-file-ignores`；7.2 ESLint flat config 与 `lint` 脚本；7.5 `ci.yml`；各自以 `uv run ruff check .` / `corepack pnpm lint` 退出码为证据。
- File IO / path safety / overwrite — selected：5.5 搬 `file_journal_paths.py` 时 `read_bytes_limited_no_follow`、`list_directory_no_follow_limited` 等 no-follow 原语**留在 facade**（tasks.md 5.5 明文），既有测试零改动是 oracle；其余 task 不碰文件 IO。
- Schema / columns / units / field names — selected：2.1 `status_vocab.py` 与 `schemas/*.json` / DB 枚举的 parity 测试；2.2 `pipeline_job` enum 增 `reservation_lost`（唯一的 schema 变化，design Risks）；7.3 前端状态类型改生成别名。
- Auth / permissions / secrets — not selected：不触及鉴权、角色或 secret 路径；2.2 只改响应 enum 值集合，不改路由挂载或权限。
- Concurrency / shared state / ordering — selected：7.4 首刀改模块级 `cached`/`inflight` 的失败语义（被拒绝的 `inflight` 在 settle 时清空，并发等待者拿到同一失败但都不缓存）；2.1 `scheduler_state_types` 的 hydro 集合保持 #1581 的共享对象身份与 `DURABLE` 可变性（`set(...)` 重绑，词汇 frozenset 不直接共享）；2.3 `chain_types` 继续作为共享对象（`set(PIPELINE_JOB_TERMINAL)` 绑定，不换 identity）；5.x 被 patch 名经 facade 属性延迟取用以保 monkeypatch 生效（5.0 聚簇图列出全部九个被 patch 名）；2.2 tuple 顺序 `PIPELINE_JOB_STATUS_ORDER` 固定。
- Resource limits / large input / discovery — not selected：无新的发现/扫描路径；1.1/4.2 的 AST 扫描只读仓库树，规模固定。
- Legacy compatibility / examples — selected：1.2 expand 窗口保留 re-export shim，1.3 contract 删 shim 并以守卫的 contract 检查函数封死旧路径；9.2/9.3 退役 legacy store 引用（表已于 2026-09-19 drop，design D10）。
- Error handling / rollback / partial outputs — selected：7.4 首刀是 hook 失败路径 + 载荷合法性校验（静态 geojson、非 envelope 消费者，project-profile 的 `unwrapApiData`/`getApi` 契约不直接触发，malformed 用例按同等标准补）；7.4 后继：`apiErrorCode` 以 `ApiErrorShape` 类型化、`apiFetch` fallback 经 envelope，两个脱敏包装（`getSafeModelAssetErrorMessage`、`settledError`）与 M11 标签掩码 `safeM11ErrorMessage` 原样保留（脱敏边界）；1.2/5.x/6.x 采用 expand–contract，半搬状态可合并（design Migration Plan）。
- Release / packaging / dependency compatibility — selected：4.1 的 per-file-ignores 清单在 `uv.lock` 钉住的 ruff 版本下生成，升级 ruff 须同 PR 重生成（钉死块）；7.2 新增 `typescript-eslint`、`eslint-plugin-import-x`、`eslint-import-resolver-typescript` 三个 devDependency（lockfile 同 PR，`corepack pnpm install --frozen-lockfile` 绿）；后端无依赖变更。
- Documentation / migration notes — selected：9.1 九份 AGENTS.md、9.3 runbook/ADR/spec 文本、8.x 审计报告契约，由 `tests/test_entropy_audit_report_contract.py` 文档门覆盖；`docs/governance/entropy-audit-2026-10-16.md` 行号引用以 design Context 为准。

## 1. 导入分层守卫与契约抽取（spec: import-layering-guard）

- [ ] 1.1 新增 `tests/test_import_layering_guard.py` 与 `tests/import_layering_allowlist.json`（`{file, module}` 条目，初始为当前 11 处 `packages/common -> workers` 与 1 处 `workers/shud_runtime -> services`，按 (file, module) 去重后 9 条；扫描根只有 `apps packages services workers scripts tests`），按 spec 实现"向上导入即失败、条目在 AST 中不存在即失败、向下导入不需条目"；在 `scripts/select_ci_tests.py` 的 `select_tests` 中按 #1656/#2185 形态加一个 additive loop：`packages/**`、`workers/**` 任一路径改动即加选该测试（不参与 stop 规则）。允许的测试改动：`tests/test_select_ci_tests.py` 中因该 rider 而变的精确集合断言（同 #1656/#2185 先例，PR 列出）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py`；守卫单测含"向下导入条目不报"与"导入已删除即 stale"两个样例；selector 断言：只改 `workers/forcing_producer/direct_grid_contract.py`（stop 规则 owner）时守卫仍被选中；人为在 `packages/common/` 加一行 `from workers.x import y` 确认失败后撤销（PR 描述记录）。

  钉死的契约（fixture review 2026-10-17）：

  - **导入方范围**：1.1 只解析 `packages/**/*.py` 与 `workers/**/*.py` 的导入；六个根名 `apps packages services workers scripts tests` 只用于判断被导入目标属于哪一层，`tests/`、`scripts/` 不作为导入方扫描。层规则恰好两条：`packages` 不得导入 `{workers, services, apps, scripts}`；`workers` 不得导入 `{services, apps}`（`workers -> scripts` 今天为 0，不禁止、不扩大）。守卫模块级声明 `_SCAN_ROOTS = ("packages", "workers")`。
  - **导入节点口径**：`ast.walk` 全模块（函数体、`if TYPE_CHECKING:`、`try/except ImportError` 内都算）；跳过相对导入（`level > 0`；`workers/` 下有 298 处，读 `module` 前先判 `None`）；字符串注解与 `importlib.import_module`/`__import__` 不在范围。`module` = 写出的完整点分名（`import workers.x.y as z` 记 `workers.x.y`；`from workers.x import y` 记 `workers.x`；`import a, workers.b` 按别名分开）；层归属只看第一段；stale 比对用完整点分名精确相等。
  - **allowlist 格式与校验**：`tests/import_layering_allowlist.json` 顶层 list，每条恰好 `{"file", "module"}` 两键，`file` 为相对仓库根的 POSIX 路径且必须位于 `packages/` 或 `workers/` 下，按 `(file, module)` 排序；重复对、多余键、未排序、`file` 不存在、条目对应导入在该文件 AST 中不存在（同文件同模块多行只算一条，任一行在即非 stale）、条目对应的导入存在但并非向上 → 全部判失败并点名条目。初始 9 条（fixture review 实测，12 条导入语句去重）：`packages/common/grid_registry_bbox_guard.py`|`workers.data_adapters.region`；`packages/common/model_registry.py`|`workers.forcing_producer.direct_grid_contract`；`packages/common/model_registry_contracts.py`|`workers.forcing_producer.direct_grid_contract`；`packages/common/state_cli.py`|`workers.data_adapters.base`；`packages/common/state_clone.py`|`workers.forcing_producer.direct_grid_contract`；`packages/common/state_clone.py`|`workers.mapping_builder.rewrite`；`packages/common/state_manager.py`|`workers.data_adapters.base`；`packages/common/station_set_flip.py`|`workers.forcing_producer.direct_grid_contract`；`workers/shud_runtime/runtime.py`|`services.orchestrator.time_consistency`。
  - **文件发现与解析**：照 `tests/test_path_canonicalization_family_guard.py` 的写法：`REPO_ROOT = Path(__file__).resolve().parents[1]`，`sorted(root.rglob("*.py"))`，排除 `{"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}`；`SyntaxError` 直接抛出不跳过。扫描与比对函数接受 `repo_root` 参数：「新增向上导入即失败」「stale 条目」「向下导入不需条目」三个样例在 `tmp_path` 合成树上跑（spec 样例引用的 `contracts/region.py` 到 1.2 才有），另一个测试跑真实仓库树；失败信息写出文件与导入。
  - **selector**：additive loop 只认 `.py`：`path.endswith(".py") and _any_path_matches([path], IMPORT_LAYERING_GUARD_ROOTS)`，`IMPORT_LAYERING_GUARD_ROOTS = ("packages/**", "workers/**")`，放在 #1627 loop 之后、#2323 之前，不设 `matched`（照字面「任一路径」会让 `packages/common/sql/x.sql` 选中守卫，`test_select_tests_pins_known_empty_selection_classes` 变红）。另加 `PathTestRule("tests/import_layering_allowlist.json", ("tests/test_import_layering_guard.py",))`（先例 `tests/fixtures/*.json` 行；只改 allowlist 的 PR 否则选出空集、掉到零断言冒烟）+「删掉即红」断言；`tests/test_select_ci_tests.py` 加根集合比对测试（解析守卫的 `_SCAN_ROOTS` 与 selector 的 `IMPORT_LAYERING_GUARD_ROOTS` 等价）与「去掉一个根即红」测试，同三个先例。PR Boundary 偏离：allowlist 路由行 + 两个 selector 配套测试。
  - **会变红的精确集合断言**（启发式下限，以首跑为准，PR 逐个列出）：`tests/test_select_ci_tests.py` 中 runtime、direct_grid owner（`:1076`/`:1123` 为 Verify 的 scenario）、issue_548、safe_fs、forecast_store、station_set_flip 等约 10 处。只改 `workers/forcing_producer/direct_grid_contract.py` 今天已经由三个先例 loop 选中四个守卫，新 loop 在 stop 循环之后即自然成立。
  - **design D2 口径**：以 spec/tasks 为准（9 条去重条目；additive loop 而非 owner 规则），D2 文字已同步修正。

  **Suggested fixture level:** compact - 新增守卫测试与路由规则，无运行时代码。

  **Minimal mergeable slice:** atomic - 守卫、allowlist 与路由三者缺一则守卫要么在 CI 上不跑、要么立刻红。

- [ ] 1.2 expand：新建 `packages/common/contracts/{direct_grid,time_normalization,region,hydrologic_fingerprint}.py`，把 spec 列出的符号搬入（`time_normalization` 含 `base.py` 九个纯助手 + `check_three_way_time_consistency`；`hydrologic_fingerprint` 含 `verify_hydrologic_core_fingerprint_equal` 的 AST 传递闭包：基类、四个异常、`HydrologicCoreFingerprint`、`compute_hydrologic_core_fingerprint`、`_validate_category_files`、三个常量等，PR 附闭包清单）；五个原模块改为显式 re-export shim（`direct_grid_contract.py`、`region.py`、`base.py` 保留适配器基类与 dataclass、`rewrite.py` 其余异常改继承契约基类、`time_consistency.py`）；`workers/shud_runtime/runtime.py` 与 `packages/common` 的 7 个文件改从 `contracts` 导入；allowlist 变为 `[]`；selector 在 `PATH_TEST_RULES` 为四个 contract 模块各加一行与旧路径 owner 规则等价的规则（相同测试集、相同 `stop_on_match` 语义）。

  Depends on: 1.1

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_forcing_producer*.py tests/test_data_adapter_resolution.py tests/test_era5_adapter.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_shud_runtime.py tests/test_mapping_builder*.py tests/test_state_clone*.py tests/test_state_manager*.py tests/test_model_registry*.py tests/test_orchestration_chain.py tests/test_grid_registry_bbox.py tests/test_download_bbox_clip.py tests/test_producer_bbox_preflight.py`，全部零改动通过；`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers' packages/` 为空（macOS grep 不认 `\s`）；selector 断言"改 `packages/common/contracts/direct_grid.py` 选中 direct-grid 契约测试集"。

  首刀 Verify：`uv run ruff check .`；`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_model_registry*.py tests/test_state_clone*.py tests/test_direct_grid_e2e.py tests/test_direct_grid_variant_registration.py tests/test_legacy_reactivation_guard.py tests/test_mapping_builder_binding.py tests/test_mapping_builder_cli.py tests/test_mapping_builder_integration.py tests/test_direct_grid_display_cutover_flip_atomic.py tests/test_direct_grid_display_cutover_flip_mvt_set.py tests/test_direct_grid_display_cutover_history.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py`；`uv run pytest -q tests/test_forcing_producer.py -k direct_grid_contract`；`uv run python -c "import apps.api.main, workers.forcing_producer, services.orchestrator.scheduler_candidates, services.orchestrator.chain"`；`git show origin/master:workers/forcing_producer/direct_grid_contract.py | diff - packages/common/contracts/direct_grid.py` 为空；allowlist 中不再有 `direct_grid_contract` 条目；`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers\.forcing_producer\.direct_grid_contract' packages/` 为空（docstring/注释中的提及不计）。

  钉死的契约（1.2 首刀 direct_grid，fixture review 2026-10-17）：

  - **搬什么**：`workers/forcing_producer/direct_grid_contract.py`（519 行，无 `__all__`、无模块 docstring、无导入期副作用）整文件逐字节复制为 `packages/common/contracts/direct_grid.py`（不加 docstring，`from __future__ import annotations` 仍在第 1 行）；公开名 6 常量 + 3 类 + 2 函数，私有 `_SAFE_STATION_FORCING_FILENAME` 与 10 个 helper 一并搬。**陷阱**：第 15 行 `"direct_grid_contract"` 是 `DIRECT_GRID_SECTION_KEYS` 的 manifest 节键，不是模块路径，盲 sed 会改坏。`packages/common/contracts/__init__.py` 为 0 字节（同 `packages/common/__init__.py`；`pyproject.toml` 的 `setuptools.packages.find` 需要它；不做 re-export，后继 sibling 不被急切加载；不需 selector 行）。`source_identity.py` 只导入 `__future__`/`typing`，无环。
  - **shim**：`workers/forcing_producer/direct_grid_contract.py` 改为一行 docstring + `from packages.common.contracts.direct_grid import (...)`（ruff 顺序：常量、类、函数）+ `__all__` 恰为 11 个公开名：`DIRECT_GRID_MODE`、`DIRECT_GRID_SECTION_KEYS`、`IDW_MODE`、`MAX_DIRECT_GRID_STATION_BINDINGS`、`REQUIRED_MANIFEST_FIELDS`、`REQUIRED_STATION_FIELDS`、`DirectGridContractError`、`DirectGridForcingContract`、`DirectGridStationBinding`、`load_forcing_mapping_contract_from_manifest`、`parse_direct_grid_forcing_contract`；不 re-export 私有名或模块自身的导入（`normalize_source_id`、`Mapping` 等，无人经此路径导入）；不需 `from __future__`；`__all__` 覆盖 F401。
  - **四处改向**：只把 `workers.forcing_producer.direct_grid_contract` 换成 `packages.common.contracts.direct_grid`；`model_registry.py` 保持 `X as X` 形式（~:157-160 四条单名语句）；随后 `uv run ruff check --fix`（预期 `model_registry`、`state_clone`、`station_set_flip` 三处 I001 重排）；docstring 中旧路径提及（`model_registry_contracts.py:486,540`、`state_clone.py:36,347`、`station_set_flip.py:41,253,256`）不动。allowlist 恰删 4 条（`packages/common/{model_registry,model_registry_contracts,state_clone,station_set_flip}.py` × `workers.forcing_producer.direct_grid_contract`），剩 5 条（含 `state_clone.py` × `workers.mapping_builder.rewrite`）；1.1 的 guard 双向强制（残留条目 = stale，残留导入 = 向上导入）。
  - **计数修正（issue/design）**：旧模块的导入方是 20 个文件（AST 计）：4 个改向的 `packages/common` + 16 个留在 shim 上（8 个非测试绝对导入：scripts 2、services 2、workers 4；2 个相对导入 `workers/forcing_producer/__init__.py:3`、`store.py:25`；6 个测试文件）；design 的「12」是非测试绝对导入的 grep 数；「7 行」= 7 条 import 语句。「12 个导入文件全部不改」不成立——其中 4 个正是改向文件。
  - **patch 点：零**（字符串 patch 目标、`patch.object`/`setattr`、模块对象导入、`importlib`/`__import__`、`sys.modules`、`__module__`/pickle、私有名经旧路径导入，均 0 命中）；同一性：shim 名、4 个改向模块、`workers.forcing_producer.__init__` re-export 与契约模块对象 `is` 相同，`__module__` 变为 `packages.common.contracts.direct_grid`，无人检查。
  - **selector「等价规则」的精确定义**：新常量 `DIRECT_GRID_CONTRACT_CANONICAL_PATH = "packages/common/contracts/direct_grid.py"`（不追加进 `DIRECT_GRID_SURFACE_PATH_PATTERNS`——1.3 删 `[0]` 会移位）；新行 `PathTestRule(DIRECT_GRID_CONTRACT_CANONICAL_PATH, (*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS), stop_on_match=True)` 紧跟旧行（~:2993-2999）之后；旧行保留到 1.3。有效选择比旧路径**宽**是有意的：#1744 共享库规则（~:7317-7329）对 `packages/common/**` 在规则循环之外恒加 `CORE_SMOKE_TESTS`，stop 规则压不住。新 exact-set 测试：该路径选中 == `{*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS, *SHARED_BASELINE_CONTRACT, INVARIANT_SUITE_PATH, WRITE_SURFACE_SCAN_PATH, FAMILY_GUARD_PATH, RESOLVE_SURFACE_GUARD_PATH, <1.1 的 guard 路径常量>}`（1.1 落地后 25 个）且 `"tests/test_forcing_producer.py" not in selected`；`STOP_RULE_AT_SITE_EXTENSIONS`（master ~:12463）加 `(DIRECT_GRID_CONTRACT_CANONICAL_PATH, DIRECT_GRID_CONTRACT_IMPORTER_TESTS)`；按 #2916 形状（~:836-847）加 red-when-removed 测试（删行后 `len == len(PATH_TEST_RULES) - 1` 且 importer 套件离开选择）。既有 exact-set 断言不会因 1.2 变红。旧路径的 `INTENTIONAL_RULE_GAP_EXCLUSIONS` redirect 对仍成立（`tests/test_forcing_producer.py:43` 仍导入旧路径）。
  - **real-DB 车道缺口（决定：首刀一并补）**：旧路径匹配 `.github/workflows/ci.yml:136` 的 `database:` 过滤器 `workers/forcing_producer/**` 与 `INTEGRATION_TRIGGER_ROOT_GLOBS`（`tests/test_select_ci_tests.py` ~:15174），新路径两者都不匹配，而 `tests/test_direct_grid_variant_registration.py` 有 integration 标记用例消费该解析器。同 PR 把 `packages/common/contracts/direct_grid.py` 加进 ci.yml `database:` 过滤器与 `INTEGRATION_TRIGGER_SOURCES`（注册表测试强制两者同步），记 PR Boundary 偏离。
  - **allowed test edit**：`tests/test_node27_connection_attribution.py` ~:899-905 的 met_store 理由文本（「model_registry 在 157 行导入 `workers.forcing_producer.direct_grid_contract`」「met_store 在单元运行时 `sys.modules` 内」）改后为假；只更新 detail 字符串，verdict 仍 `UNREACHABLE`（仍可达的静态路径：`apps/api/routes/pipeline.py -> services/orchestrator/__init__.py -> chain.py -> workers/canonical_converter/converter.py`）；记偏离。
  - **导入图变化（PR 记录）**：运行时 `apps.api.main` 不再加载 14 个模块（`packages/common`：`grid_registry_bbox_guard`、`grid_registry_store`、`grid_signature`、`met_store`、`node27_timeseries_discovery`、`shud_forcing_contract`、`timescale_write_guard`、`timeseries_compression_fence`；`workers`：`canonical_converter` 包与 `.converter`、`data_adapters.region`、`forcing_producer` 包、`.producer`、`.direct_grid_contract`），新增加载 `packages.common.contracts` 与 `.direct_grid`；被丢掉的 13 个源文件无顶层副作用（唯一装饰器 `@runtime_checkable`）；display 单元静态闭包与 8 个 delegated 组件闭包不变。
  - **顺序与跟踪**：#2920 合并后再切分支，复用它在 `tests/test_select_ci_tests.py` 引入的 guard 路径常量；无新测试文件；Verify 前 `git add` 两个新源文件。
  - **活性演练**：(1) allowlist 删后回退一处改向 → 1.1 guard 失败并点名文件与导入，再还原；(2) 从 shim 导入列表删一个名 → `import workers.forcing_producer` 抛 ImportError；(3) selector red-when-removed 测试。
  - **级别 expanded 成立**：三席 `correctness`、`test-evidence+spec-compliance`、`integration`（selector/CI 路由与导入图）；风险包 Legacy compatibility、Error handling / rollback（expand–contract 半状态）、Public API / script entry。
  - **残余风险（记录）**：单独改 `contracts/direct_grid.py` 时选择不含 `packages/common` 消费者套件（与旧行等价，但它们现在是直接导入方）——候选后续；docstring 里的旧点路径在 1.3 删 shim 后成死引用，1.2/1.3 spec 均未覆盖。

  钉死的契约（1.2 后继 time_normalization，fixture review 2026-10-17）：

  - **搬什么（11 个定义，逐字）**：`workers/data_adapters/base.py` 的九个助手按原顺序（`ensure_utc` :12、`parse_cycle_time` :19、`parse_cycle_date` :33、`format_cycle_time` :42、`cycle_id_for` :46、`valid_time_for` :51、`parse_resolution_segments` :55、`generate_segmented_forecast_hours` :85、`validate_forecast_hours` :113），然后 `services/orchestrator/time_consistency.py` 的 `_ensure_utc`（:16，闭包成员，保持私有、shim 不 re-export；它与 `ensure_utc` 语义不同——replace-only vs `astimezone`——不在本 change 合并，留作 3.x 候选）与 `check_three_way_time_consistency`（:20）。契约模块只导入 `re`、`datetime`（`UTC, date, datetime, timedelta`）与 `packages.common.source_identity.normalize_source_id`，不导入 `base`（无环）；无 `__all__`（同 `direct_grid.py`）；docstring 只列这 10 个公开函数，**不**预告 3.1 的 `utc_now`/`parse_iso_utc`/`format_iso_utc`；把 `time_consistency.py` 现有 docstring 中「为何与 chain 分开」的说明搬到契约模块。`base.py` 无模块级常量。
  - **`base.py` shim**：留下 8 个类（`CycleDiscovery` :155、`ManifestEntry`、`DownloadManifest`、`DownloadFileResult`、`DownloadPlanResult`、`VerificationFailure`、`VerificationResult`、`DataSourceAdapter`）；显式 re-import 九个助手；`__all__` 17 个名（8 类 + 9 助手）；删掉 `re`、`UTC`、`timedelta`、`normalize_source_id` 导入（grep 无 `base.re`/`base.UTC`/`base.timedelta`/`base.normalize_source_id`/star-import），保留 `ABC`、`abstractmethod`、`dataclass`、`field`、`date`、`datetime`、`Any`。留下的唯一调用点 `DownloadManifest.from_dict` → `parse_cycle_time`（:239）以裸名调用模块全局，原型证实 patch `base.parse_cycle_time` 仍能拦截。`time_consistency.py` shim：一行 docstring + `from packages.common.contracts.time_normalization import check_three_way_time_consistency` + 单名 `__all__`。
  - **三处改向**：`workers/shud_runtime/runtime.py:50` 改为括号形式 `from packages.common.contracts.time_normalization import (\n    check_three_way_time_consistency as _check_three_way_time_consistency,\n)`（保留别名；单行 126 字符超 E501；`.large-file-guard.json:19` 已排除该文件）；`packages/common/state_manager.py`、`state_cli.py` 改从契约导入；随后 `uv run ruff check --fix`（预期 `state_manager`、`state_cli`、`runtime`、`base` 四处 I001）。这是 `workers` 向 `services`/`apps` 的唯一向上导入，改后 `workers/shud_runtime` 为零。
  - **patch 点与同一性**：10 个搬迁名经 `base`/`time_consistency`/`runtime._check_three_way_time_consistency`/`chain._check_three_way_time_consistency` 的 patch 点为 **0**；`test_production_scheduler.py` 的 `scheduler_module.format_cycle_time`、`scheduler_discovery_module.cycle_id_for` 是消费者自己的绑定、不受影响；其 `scheduler_discovery_module._ensure_utc` 是 `scheduler_state._ensure_utc`（`scheduler_discovery.py:26`），不是搬迁名。`test_{gfs,ifs,era5}_adapter.py`、`test_download_bbox_clip.py:22-23` 经模块对象属性访问，走 shim 正常。`chain.py:14/18` 模块对象 + `chain_compat_runtime.py:375-441` 的 `getattr`/`hasattr` + `tests/test_orchestration_chain.py:12366-12451` 的 `is` 断言与 `:12500-12510` 清单字符串：原型证实对象全同一、两个 drift 元组为 `()`；#2922 不碰这些测试与 `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md`（1.3 改指）。
  - **allowlist 恰删 3 条**：`packages/common/state_cli.py` × `workers.data_adapters.base`、`packages/common/state_manager.py` × 同、`workers/shud_runtime/runtime.py` × `services.orchestrator.time_consistency`（guard 对 stale 条目报错，删除是强制的）；不钉剩余数量（#2921 后剩 `grid_registry_bbox_guard` × region、`state_clone` × rewrite 两条）。不勾 1.2（region、fingerprint 后继仍开）。
  - **selector「等价规则」的显式决定（偏离 D1 措辞，记录）**：两个旧 owner 都不是 stop 规则——`base.py` 匹配 `workers/data_adapters/**`（~:3313，10 测试）与 `workers/data_adapters/base.py`（~:3335，5 测试）；`time_consistency.py` 只匹配宽泛的 `services/orchestrator/**`（~:3649，123 测试），照抄会把约 110 个 orchestrator 套件永久挂到 10 个函数的模块上。新行（非 stop，紧跟 `base.py` 行之后）：`PathTestRule(TIME_NORMALIZATION_CONTRACT_PATH, (*DATA_ADAPTERS_DIRECTORY_TESTS, *DATA_ADAPTERS_BASE_HELPER_TESTS, "tests/test_warm_start_chaining.py"))`，两个常量从既有内联元组抽出，`select_tests` 对 `base.py` 与 `workers/data_adapters/x.py` 的输出前后字节一致；不含 `tests/test_warm_start.py`（:275/:277 只是 docstring 提及）。exact-set 测试的期望集由这些常量 + `SHARED_BASELINE_CONTRACT` + 4 个既有附加守卫 + 1.1 guard 常量构成；按 #2916 形状加 red-when-removed 测试，以 `tests/test_gfs_adapter.py` 与 `tests/test_warm_start_chaining.py` 为 tell（不用 `test_production_scheduler`，它经 CORE_SMOKE 也会到）。**不加** `STOP_RULE_AT_SITE_EXTENSIONS` 条目（那是 stop 规则专用）；**不改** real-DB 车道（`workers/data_adapters/**`、`time_consistency.py` 都不匹配 `database:` 过滤器与 `INTEGRATION_TRIGGER_*`，与 #2921 不同）；`time_consistency.py` 与 `base.py` 行到 1.3 前不动；`base.py` 的 `INTENTIONAL_RULE_GAP_EXCLUSIONS` 对（~:11233-11303）仍有效。新路径今日匹配不到任何规则（只选 9 个：CORE_SMOKE + 4 守卫）。
  - **Verify（本片）**：`uv run ruff check .`；`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_warm_start_chaining.py tests/test_download_bbox_clip.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_era5_adapter.py tests/test_data_adapter_resolution.py tests/test_shud_runtime.py tests/test_state_manager*.py tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py`；`uv run python -c "import services.orchestrator.chain, workers.shud_runtime.cli, apps.api.main"`（compat drift 检查在导入期抛错；`shud_runtime.cli` 是 node-22 计算入口）；同一性单行：对 `base.__all__` 中存在于 `vars(contracts)` 的每个 `n`，`getattr(base, n) is getattr(contracts, n)`，且 `time_consistency.check_three_way_time_consistency is contracts.check_three_way_time_consistency`；逐字搬迁用 AST 探针（对每个 `FunctionDef` 以 `ast.get_source_segment` 比较 `git show origin/master:` 的源与契约模块副本，恰 11 个定义、零差异）替代 #2921 的字节相等；两条 grep 为空：`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers\.data_adapters\.base' packages/`、`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+(services|apps)([.[:space:]]|$)' workers/`；Verify 前 `git add` 新文件。
  - **导入图变化（PR 记录）**：`workers.shud_runtime.cli` 不再加载 `services.orchestrator`（`__init__`）、`.production_contract`、`.time_consistency`、`services.artifacts(.reader)`、`workers.data_adapters(.base)`；运行时一方模块 25 → 20，静态闭包 128 → 21；被丢掉的模块无顶层副作用（`services/orchestrator/__init__.py` 只急切导入 `production_contract`，`chain` 经 `__getattr__` 懒加载）；8 个 `REGISTERED_COMPONENTS` 与 display 单元的静态闭包各增 1 个模块、connect-surface 集合不变，`tests/test_node27_connection_attribution*.py` 应无需改。
  - **PR Boundary 偏离（记录）**：allowlist 删 3 条、两个抽出的 selector 常量、exact-set 与 red-when-removed 测试。**原子性**：六个助手是一条调用链（`cycle_id_for`→`format_cycle_time`→`parse_cycle_time`→`ensure_utc`，加 `valid_time_for`、`parse_cycle_date`），三个 forecast-hour 助手与 `time_consistency` 部分技术上可分但无收益且 `base.py` 导入块要改两次，保持原子；issue 的「九个助手互相调用」措辞偏重。
  - **残余风险（记录）**：3.x 后 `_ensure_utc` 与 `ensure_utc` 并存于契约模块（不匹配 3.1 的 fullmatch 计数）；新行与两条旧 `base.py` 行永久共享测试集（常量抽出防漂移）；3.1 及其它 `packages/**` rider 会改 exact-set 期望集，后落地者按「允许的测试改动」更新；node-22 冻结 `.venv` 的 editable finder 未查（#2921 先把 `contracts` 包放上计算路径，问题会先在那里暴露）。

  钉死的契约（1.2 后继 region，fixture review 2026-10-17）：

  - **纯逐字节搬迁**：`workers/data_adapters/region.py`（75 行：docstring :1-13；`os`、`dataclass` 导入；`DEFAULT_BBOX_SOUTH/NORTH/WEST/EAST` :21-24；`GeoBBox` frozen dataclass :27-56；`_env_float` :59；`china_buffered_bbox_from_env` :69；无 `__all__`，导入期副作用只有 `@dataclass`）整文件复制为 `packages/common/contracts/region.py`，`git show origin/master:workers/data_adapters/region.py | diff - packages/common/contracts/region.py` 为空；契约模块不导入任何 `packages.common` 内容，与 guard 无环。
  - **shim**：一行 docstring + `from packages.common.contracts.region import (DEFAULT_BBOX_EAST, DEFAULT_BBOX_NORTH, DEFAULT_BBOX_SOUTH, DEFAULT_BBOX_WEST, GeoBBox, china_buffered_bbox_from_env)` + `__all__` 恰为这 6 个名；不导出 `_env_float`/`os`/`dataclass`（全仓库无人经旧路径访问）；不需 `from __future__`；docstring 不写 `<file>.py:N`（hard-line-reference 棘轮，`check_line_references.py:38-43`）。
  - **改向**：只改 `packages/common/grid_registry_bbox_guard.py:67` 一行；ruff I001 会把它挪到 `:66` `grid_registry_store` 导入之上；总行数仍 221，`line_reference_baseline.json` 中 producer 的 `grid_registry_bbox_guard.py:167-179` 引用不动。文字提及不动：guard docstring `:4`/`:157`、`select_ci_tests.py:3310` 注释、`docs/governance/entropy-audit-2026-10-16.snapshot.json:864`。
  - **导入方（AST，7 文件）**：改向 1（guard）；留在 shim 的非测试 3（`workers/forcing_producer/producer.py:45`、`gfs_adapter.py:44` 与 `ifs_adapter.py:43` 的 `from .region`）；测试 4（`test_grid_registry_bbox.py:58`、`test_producer_bbox_preflight.py:65`、`test_forcing_producer.py:457` 函数内、`test_download_bbox_clip.py:12` importlib）零改动。`workers/data_adapters/__init__.py` 无 re-export；services/apps/scripts 无引用。**patch 点 0**；同一性断言 `test_grid_registry_bbox.py:630`（`default is china_buffered_bbox_from_env`）原型通过；无人检查 `__module__`/pickle。
  - **allowlist 恰删 1 条**：`packages/common/grid_registry_bbox_guard.py` × `workers.data_adapters.region`；guard 无其它向上导入；不钉剩余条数。`contracts/__init__.py` 由 #2921 提供，本片不建。不勾 1.2。
  - **oracle 孤儿（决定）**：`tests/test_download_bbox_clip.py` 不在任何 `PATH_TEST_RULES` 行、也不在 `INTENTIONAL_RULE_GAP_EXCLUSIONS`（`:12-14,22-23` 全经 `importlib.import_module`，gap 推导看不到）；改 region/契约/guard 都选不到它。**只**把它加进新契约行，记为偏离 D1「相同测试集」（先例 #2922 加 `test_warm_start_chaining`）；不加进 `workers/data_adapters/**` 行。
  - **selector 等价规则（精确）**：旧 owner 仅 `workers/data_adapters/**`（非 stop；行 ~:3306，pattern :3313，内联 10 元组 :3314-3325），无 `region.py` 同名行。新常量 `REGION_CONTRACT_PATH = "packages/common/contracts/region.py"`；新行 `PathTestRule(REGION_CONTRACT_PATH, (*DATA_ADAPTERS_DIRECTORY_TESTS, "tests/test_download_bbox_clip.py"))`，非 stop，紧跟 `workers/data_adapters/**` 行之后、`base.py` 行（~:3327）之前。`DATA_ADAPTERS_DIRECTORY_TESTS` 与 #2922 是同一个从 :3314-3325 抽出的 10 元组：**先合并者抽取，后合并者 rebase 后复用、不得重定义**；证明 `select_tests` 对 `workers/data_adapters/{region,base,gfs_adapter}.py` 输出前后一致。不加 `STOP_RULE_AT_SITE_EXTENSIONS`；不改 real-DB 车道；不改 `INTENTIONAL_RULE_GAP_EXCLUSIONS`。exact-set 用公式：`set(select_tests([REGION_CONTRACT_PATH])) == {*DATA_ADAPTERS_DIRECTORY_TESTS, "tests/test_download_bbox_clip.py", *CORE_SMOKE_TESTS, INVARIANT_SUITE_PATH, WRITE_SURFACE_SCAN_PATH, FAMILY_GUARD_PATH, RESOLVE_SURFACE_GUARD_PATH, <1.1 guard 常量>}`（参考计数 20）；red-when-removed 按 #2916 形状，tell 为 `test_download_bbox_clip.py`、`test_grid_registry_bbox.py`、`test_gfs_adapter.py`（不用 `test_production_scheduler.py`）。
  - **Verify（本片）**：`uv run ruff check .`；`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_grid_registry_bbox.py tests/test_download_bbox_clip.py tests/test_producer_bbox_preflight.py tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_forcing_producer.py`（`test_forcing_producer.py` 跑整文件：`-k bbox` 选中 0 个是空跑）；import 冒烟 `uv run python -c "import packages.common.grid_registry_bbox_guard, workers.data_adapters.gfs_adapter, workers.data_adapters.ifs_adapter, workers.forcing_producer.producer"`；同一性单行：shim `__all__` 每个名 `getattr(shim,n) is getattr(contract,n)`，且 guard 的 `env_reader` 默认值 `is shim.china_buffered_bbox_from_env`；两条 grep 为空：`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers\.data_adapters\.region' packages/`、`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+(workers|services|apps|scripts)([.[:space:]]|$)' packages/common/contracts/`；Verify 前 `git add` 新文件。
  - **导入图**：guard 唯一生产导入方 `producer.py:17` 在 `:45` 又经 shim 导入 region，所以加载 guard 的入口仍加载 `workers.data_adapters(.region)`：净变化 +1 模块（`packages.common.contracts.region`）、0 丢弃（`apps.api.main` 118→120、`scheduler` 140→142 含 `contracts` 包；`shud_runtime.cli` 28→28）。`test_node27_connection_attribution*.py` 无需改。
  - **级别**：issue 的 compact 成立（两席 `correctness`、`test-evidence+spec-compliance`；无 real-DB 车道变化、无入口丢模块，不需 `integration` 席）。**与 #2922 的冲突**：allowlist 两边删除取并集（#2921 后 region 条目与 `state_cli` 条目在 JSON 里相邻，文本冲突无语义冲突）；与 #2923 无共用常量，只在 `tests/test_select_ci_tests.py` 追加位置 rebase。
  - **残余风险（记录）**：`test_download_bbox_clip.py` 对 `workers/data_adapters/**` 与 `base.py` 行同样不可见（既有缺口）；只改 guard 的 PR 选不到两个 bbox 测试；测试 docstring 里 `workers/data_adapters/region.py:44`、`:41-49`（`test_producer_bbox_preflight.py:629,692`）在 1.3 删 shim 后成死引用（不在 line-reference 扫描范围）；其它 `packages/**` rider 会改 exact-set 期望集。

  钉死的契约（1.2 后继 hydrologic_fingerprint，fixture review 2026-10-17）：

  - **契约模块范围 = 22 个定义**：`verify_hydrologic_core_fingerprint_equal` 在 `rewrite.py` 内的 21 名 AST 闭包 + `STATE_COMPATIBILITY_SURFACES`（`rewrite.py:689-691`，从 gate 出发够不到，但 `state_clone.py:131` 导入它且由 LABELS 派生，必须同搬——spec/issue 把它归进闭包是归错类，实现上照搬）。闭包（master 行号）：`SpAttRewriteError` :160-170、`MissingPackageFileError` :434-472、`UnknownCategoryError` :475-497、`MissingCategoryError` :500-521、`HydrologicCoreFingerprintMismatchError` :524-562、`NON_SP_ATT_CATEGORIES` :646-654、`HYDROLOGIC_CORE_FINGERPRINT_LABELS` :660-671、`HydrologicCoreFingerprint` :840-867、`_ParsedSpAtt` :874-890、`_sha256_file` :893-899、`_read_bytes_and_split_lines` :902-916、`_parse_header_counts` :919-937、`_parse_sp_att_file` :940-1067、`_validate_category_files` :1313-1351、`_resolve_and_sha256` :1354-1376、`_sha256_bytes`/`_compute_file_category_hash`/`_canonicalize_sp_att_non_forc`/`_validate_surface_set` :1469-1584、`compute_hydrologic_core_fingerprint` :1587-1756、`verify_hydrologic_core_fingerprint_equal` :1759-1863。issue 点名 11 个，漏了 `_ParsedSpAtt` 与 9 个私有助手。原顺序逐字搬、连同紧贴上方的 `#` 注释块；契约模块只导入 `from __future__ import annotations`、`hashlib`、`pathlib`、`collections.abc.Mapping/Sequence`、`dataclasses.dataclass`；不加 `__all__`。`.sp.att` 解析器整条链落进 fingerprint 模块是闭包强制的，不另起模块，PR 写明。**行数**：原型 896 行（单行 docstring），`.large-file-guard.json` `maxLines: 1000`，docstring/分节注释预算约 100 行，不加豁免。
  - **`rewrite.py` 导回清单**：11 个公开名全部 re-export（基类、四个异常、`HydrologicCoreFingerprint`、两个函数、三个常量）；5 个私有名因留下代码仍调用而以原名导回：`_ParsedSpAtt`（:2137）、`_parse_sp_att_file`（:1145、:1271-1272、:1997）、`_resolve_and_sha256`（:1445、:1451）、`_sha256_file`（:1131-1132、:1993、:2097、:2109）、`_validate_category_files`（:1438）；另 6 个私有名不导回（无留下的调用方；测试只经 `rewrite_module.` 访问 `_sha256_file` :830/:843 与不搬的 `_LEGACY_WEATHER_PATTERNS`）。删 `import hashlib`（否则 F401）。import 块后加 `__all__` 38 名——恰等于 `workers/mapping_builder/__init__.py:156` 从 rewrite 导入的清单（集合相等已实测），用于覆盖 8 个只为 re-export 导入的名的 F401。留下的 12 个异常的 `class X(SpAttRewriteError)` 语句逐字节不动，继承契约基类靠导回名自然实现（不写 `hf.SpAttRewriteError`）；顺手清掉留下的空 `# ---` 分节注释。
  - **`_sha256_file` seam 按方案 (a)，不改测试**：`copy_and_rewrite_sp_att_forc` 的 :1993/:2097/:2109 是裸名调用、查 `rewrite` 模块全局，`tests/test_mapping_builder_rewrite.py:843` 的 patch 继续生效（全仓唯一 patch 点；`_parse_sp_att_file` 内部不算 hash，计数不受影响）。演练：给 `rewrite._sha256_file` 装会抛错的 sentinel，`rewrite.record_sp_att_checksums(a, b)` 应抛；改为只 patch `contracts._sha256_file`，同一调用正常返回（原型拦截 2 次）。**seam 收窄（记录）**：patch `rewrite._sha256_file` 不再拦截 `verify_non_sp_att_checksums_equal`（经已搬走的 `_resolve_and_sha256`）与 fingerprint 路径的 hash，今日无测试依赖。
  - **允许的测试改动（恰两处字符串，记偏离；issue 的「`test_mapping_builder*` 零改动」改为「除这一处外零改动」）**：(1) `tests/test_mapping_builder_cli.py:2186` 的 `__module__` 期望值改为 `"packages.common.contracts.hydrologic_fingerprint"`（同一函数对象的 `__module__` 必变；re-export 改不了，wrapper 违反 spec「same object」）；全仓只此一处 `__module__` 断言。(2) `tests/test_node27_connection_attribution.py:893` `grid_registry_store.py` 的 detail 路径 `state_clone -> workers/mapping_builder/rewrite.py -> algorithm.py` 改后不存在，只写稳定尾边「`workers/forcing_producer/producer.py -> packages/common/grid_registry_bbox_guard.py imports this module`」（不写 `file_orchestration_journal.py`/`scheduler_file_providers.py`，5.x 会再过期）；verdict 仍 UNREACHABLE（`discovered == classified` 在 master/#2921/#2921+#2923 三棵树上均成立，delegated 8 组件一致）。
  - **`state_clone.py` 改向**：原导入 :129-135，五个名四个在闭包内；改后 `state_clone.py` 无任何向上导入（`ast.walk` 确认）。allowlist 恰删 1 条 `packages/common/state_clone.py` × `workers.mapping_builder.rewrite`，不钉剩余数。异常捕获点（`state_clone.py:595/:607`、`scripts/model_succession/tools.py:130`）按导入名捕获、对象同一；无 `except rewrite.X`、pickle、`__qualname__`；错误串一律 `type(e).__name__`。`ruff --fix` 预期 `rewrite.py`、`state_clone.py` 各一处 I001。docstring 旧路径提及（`state_clone.py:276,360,370,372`、`rewrite.py` 模块 docstring）不改。
  - **导入方（AST，14 文件）**：改向 1；留在 shim 13（非测试 6：`workers/mapping_builder/{__init__:156, binding:212, cli:120, evidence:136}`、`scripts/model_succession/tools.py:49`、`scripts/node22_clone_direct_grid_cutover_states.py:65`；测试 7）。
  - **selector 等价规则**：旧路径只归非 stop 目录规则 `workers/mapping_builder/**`（~:3452-3475，`MAPPING_BUILDER_TESTS`），同名推导无额外测试；新常量 `HYDROLOGIC_FINGERPRINT_CONTRACT_PATH`，新行 `PathTestRule(HYDROLOGIC_FINGERPRINT_CONTRACT_PATH, MAPPING_BUILDER_TESTS)`（非 stop，紧跟其后）；exact-set 期望 `_tracked_mapping_builder_suites() | set(SHARED_BASELINE_CONTRACT) | {INVARIANT_SUITE_PATH, WRITE_SURFACE_SCAN_PATH, FAMILY_GUARD_PATH, RESOLVE_SURFACE_GUARD_PATH, <1.1 guard 常量>}`（#2920 后 18）加等价断言 `== set(select([rewrite.py])) | set(SHARED_BASELINE_CONTRACT)`；red-when-removed 按 #2916 形状，tell 用 `tests/test_mapping_builder_rewrite.py`；不加 `STOP_RULE_AT_SITE_EXTENSIONS`；不动 real-DB 车道；`test_state_clone*` 的 edge-consumer 对（~:11447-11450，`expected_gaps` ~:15122）仍有效。
  - **Verify（本片）**：`uv run ruff check .`；`uv run pytest -q tests/test_import_layering_guard.py tests/test_select_ci_tests.py tests/test_mapping_builder*.py tests/test_state_clone*.py tests/test_forcing_producer*.py tests/test_node22_model_succession_cold_start.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py`；import 冒烟 `uv run python -c "import apps.api.main, workers.mapping_builder.cli, packages.common.state_clone, scripts.model_succession.tools, scripts.node22_clone_direct_grid_cutover_states"`；同一性：`rewrite.__all__` 中存在于 `vars(contract)` 的每个名 `is` 相同、`state_clone` 五个名同、5 个导回私有名同、留下的 12 个异常 `issubclass(contract.SpAttRewriteError)`；AST 逐字探针恰 22 个定义零差异；`git grep -nE '^[[:space:]]*(from|import)[[:space:]]+workers\.mapping_builder' packages/` 为空；Verify 前 `git add` 新源文件。活性：allowlist 删后回退改向 → guard 红；shim 删一名 → `import workers.mapping_builder` ImportError；selector red-when-removed；seam 正反探针。
  - **导入图（PR 记录）**：单独 `import packages.common.state_clone` 一方模块 39 → 19（不再加载 `workers.mapping_builder` 全包、`workers.forcing_producer`/`.producer`、`canonical_converter`、`data_adapters.region` 与 7 个 `packages/common` 模块）；`apps.api.main` 不变（106，`state_clone` 懒加载）；`workers.mapping_builder.cli` +1；display 单元静态闭包不再含 `workers/mapping_builder/*`。
  - **原子性成立且比 issue 说的更强**：除异常层级外，`compute → _canonicalize_sp_att_non_forc → _parse_sp_att_file` 与 `_compute_file_category_hash → _resolve_and_sha256 → _sha256_file` 两条链也逼着整个闭包一次搬完。**顺序**：#2920、#2921 合并后切分支；与 #2922/#2964 并行时在 `tests/test_select_ci_tests.py` 冲突，后合者 rebase。**PR Boundary 偏离清单**：两处测试字符串、`rewrite.py` 的 `__all__`、删 `hashlib`、selector 常量/行/两测试、allowlist 删一条。
  - **残余风险（记录）**：只改契约模块时选择集不含 `test_state_clone*`（与旧路径等价）；1.3 要把 38 名 `__all__` 缩回、`mapping_builder/__init__.py` 改指契约模块、处理 `rewrite_module._sha256_file` 属性访问，私有名导回在 1.3 后仍需要。

  **Suggested fixture level:** expanded - 跨 5 个模块的符号搬迁，约 65 个导入文件，fingerprint 校验与 direct-grid 契约属 project-profile 的 forcing/identity 风险轴。

  **Minimal mergeable slice:** `contracts/direct_grid.py` + `direct_grid_contract.py` shim + selector 等价规则行 + `packages/common` 的 4 个文件 7 行 direct_grid 导入改向 - 该模块只依赖 `source_identity`、无 monkeypatch 风险、其余 16 个导入文件全部经 shim 保持可用，可独立合并保绿；其余三个契约模块各为一个后继（time_normalization、region、hydrologic_fingerprint 互相独立，均依赖首刀建立的包骨架；"allowlist 为 `[]`"只在三片都合并后成立，由 1.3 验收）。

- [ ] 1.3 contract：迁移仓库内全部旧路径导入与模块属性访问到 `packages.common.contracts.*`（含 tests；`chain_compat_runtime` 的 owner 模块改指向契约模块），删除 `direct_grid_contract.py`、`region.py` shim，去掉 `base.py`/`rewrite.py`/`time_consistency.py` 的 re-export；`tests/test_import_layering_guard.py` 增加 contract 检查函数（AST 扫描 `from`/`import`/`import … as` 无旧路径、无旧模块的被搬名字）。允许的测试改动：`tests/test_orchestration_chain.py` 中与旧 owner 模块比较的 identity 断言改为契约模块，及其钉住的 `CHAIN_COMPATIBILITY_INVENTORY.md` owner 行同步更新。

  Depends on: 1.2

  Verify：`uv run pytest -q tests/test_import_layering_guard.py tests/test_orchestration_chain.py`（含新增的 contract 检查函数）；`uv run python -c "import services.orchestrator.chain"` 成功；全量 `uv run pytest -q`（本地或 node-27）通过。

  **Suggested fixture level:** compact - 机械改导入路径加删文件，守卫与全量测试是 oracle。

  **Minimal mergeable slice:** atomic - 删 shim 与改导入必须同 PR，否则要么导入断、要么 shim 留存；importer 迁移本身在 1.2 的 shim 窗口内已可合并，这里的刀是 contract。

## 2. 状态词汇单一来源（spec: status-vocabulary）

- [ ] 2.1a 新增 `packages/common/status_vocab.py`（design D3 表中全部命名集合 + 三个有序 tuple + `Literal` 别名 + `normalize_qc_verdict`；`scheduler_state_types.py` 的四处同名重新绑定同一对象）与 `tests/test_status_vocab_parity.py` 的 canonical 对照部分：每个命名集合对照从 canonical 站点钉入测试的字面量与 Python 类型；schema/折叠 DB enum 对照（复用 `tests/test_hydro_status_set_parity.py` 的折叠 helper）；selector 路由。**不改任何 schema**（`schemas/pipeline_job.schema.json` 的 `reservation_lost` 归 2.2）；重复字面量扫描与 allowlist 归 2.1b。

  Depends on: 无

  Verify：`uv run ruff check .`；`uv run pytest -q tests/test_status_vocab_parity.py tests/test_hydro_status_set_parity.py tests/test_api_contract_pipeline_ops.py tests/test_select_ci_tests.py`；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（2.1a，fixture review 2026-10-17 + split）：

  - **schema 不动**：`tests/test_api_contract_pipeline_ops.py:464-467`（经 `tests/api_contract_helpers.py:125-127`）断言 schema enum == 硬编码 11 值 `OPS_JOB_STATUS_ENUM`（`:72-82`）== OpenAPI `BasinResult.status.enum` == `PipelineJob.status.enum`，而 selector 把 schema 文件路由到空集（PR 车道绿、master 红）。2.1a 的 parity 断言 `tuple(s for s in PIPELINE_JOB_STATUS_ORDER if s not in _PENDING_PUBLIC_DELTA) == schema enum`，`_PENDING_PUBLIC_DELTA = ("reservation_lost",)`；spec「schema enum SHALL equal `PIPELINE_JOB_STATUS_ORDER`」由 2.2 完全满足（2.2 删 `_PENDING_PUBLIC_DELTA`、把 `reservation_lost` 加进 `OPS_JOB_STATUS_ENUM` :72、加 `PathTestRule("schemas/pipeline_job.schema.json", (parity, test_api_contract_pipeline_ops, test_file_journal_full_tree_budget_contract))`）。「Schema enum drift fails parity」场景以纯函数 + 合成输入实现。
  - **折叠 helper（唯一允许的既有测试改动）**：`tests/test_hydro_status_set_parity.py:61-118` 的 `_hydro_run_status_enum_members()` 无参、正则写死 `hydro.run_status`（:43-58），3 个探针 monkeypatch `_MIGRATIONS_DIR`（:293）。只新增 `_fold_enum_members(schema, type_name, *, create_sentinel, add_sentinel)`（调用时读 `_MIGRATIONS_DIR`），`_hydro_run_status_enum_members()` 改为对它的调用；9 个既有测试零改动通过；parity 测试 `from tests.test_hydro_status_set_parity import _fold_enum_members`（先例 `tests/api_contract_helpers.py:30`）；不做第二份折叠实现。
  - **重新绑定是四处、在 `services/orchestrator/scheduler_state_types.py`**（不是 packages/common）：`:48 DURABLE_HYDRO_SUCCESS_STATUSES = set(_vocab....)`（必须仍是可变 set；变异探针 `test_hydro_status_set_parity.py:148-159`）；`:57 HYDRO_RUN_CODE_CLEARING_STATUSES = _vocab.HYDRO_RUN_CODE_CLEARING_STATUSES`（同一 frozenset；写赋值而非裸 from-import——无 `__all__`，F401）；`:60 FAILED_PIPELINE_STATUSES = set(...)`；`:58 TERMINAL_PIPELINE_SUCCESS_STATUSES = set(_vocab.PIPELINE_JOB_SUCCESS)`（2.1a 做绑定，2.3 不再碰）。消费者绑定 `scheduler_state_types` 创建的对象。
  - **D3 补行（扫描会命中、无合法 allowlist 理由，2.1a 一并入词汇并在 2.3 绑定）**：`scheduler_state_types.py:28 ACTIVE_PIPELINE_STATUSES`（set，成员同 `_ACTIVE_JOB_STATUSES` 与 `persistence.ACTIVE_MANUAL_RETRY_STATUSES`）、`:32 ACTIVE_HYDRO_STATUSES`（set；六处 `:214-259` 钉同一性）、`FAILED_PIPELINE_STATUSES` 副本（`chain.py:217`、`chain_repository_state.py:19`、`chain_source_cycle.py:13`）、`persistence.RESERVATION_ACTIVE_STATUSES`（`:17`，tuple）——同「搬入并重绑」规则。
  - **有序 tuple**：`PIPELINE_JOB_STATUS_ORDER` 按 spec 第 11 行（12 值，`reservation_lost` 在 `submission_failed` 之后）；`HYDRO_RUN_STATUS_ORDER` 按 `schemas/run_status.schema.json:53`（14）；`CYCLE_STATUS_ORDER` 按 DB ordinal 并尊重 AFTER（`canonical_incomplete` 在 `canonical_ready` 之后；`cancelled` 在 `failed_publish` 之后）。`CYCLE_UPSERT_PRESERVED` = 拼接的 10 元 tuple（`met_store.py:22-31` 8 + `_TERMINAL_CYCLE_STATUSES`）；`PIPELINE_JOB_ACTIVE_MANUAL_RETRY` 是 **tuple**（`persistence.py:11`）；`SLURM_RAW_STATES` = `real_backend.SLURM_STATE_MAP` 的 17 个键（`:172`，parity 读 dict 键）；`Literal` 别名拼出成员并断言 `typing.get_args(Alias) == ORDER`。
  - **`normalize_qc_verdict`**：显式映射 {pass,PASS,passed,PASSED}→passed、{fail,FAIL,failed,FAILED}→failed、{blocked,BLOCKED}→blocked；`""`、`None`、非 str → `ValueError`；2.1a 不接任何调用者。
  - **parity 钉每个 canonical 站点对象的类型（基线 `f7be77f22`）**；站点在 2.1a 不改。清单（名 ← 站点 → 成员/类型）：`PIPELINE_JOB_STATUS_ORDER` ← `apps/api/routes/pipeline.py:61` tuple 11（+`reservation_lost` → 12）；`PIPELINE_JOB_INTERNAL_STATUSES` = {reserved, cancellation_pending, reconcile_unverified}，来自 `accepted_submit_identity.py:189` `ACCEPTED_SUBMIT_MASTER_STATUSES`（frozenset 14；(12−skipped)∪INTERNAL = 14）；`PIPELINE_JOB_TERMINAL` ← `chain_types.py:15` set 7（含 `reservation_lost`），副本 `chain_runtime_utils.py:32`、`file_orchestration_journal.py:621` 是独立字面量 set；`PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION` ← `scheduler.py:354` 与 `scheduler_candidates.py:74` 各 set 6；`PIPELINE_JOB_TERMINAL_GUARD` ← `persistence.py:9` set 4、`_OVERRIDES` ← `:10` set 2、`_ACTIVE_MANUAL_RETRY` ← `:11` tuple 4；`PIPELINE_JOB_SUCCESS` ← `scheduler_state_types.py:58` set {succeeded, complete, published}（副本 `chain.py:216`、`chain_runtime_utils.py:41`、`chain_source_cycle.py:12`、`chain_repository_state.py:23`、`retry.py:80`；inline journal:6898,6902,15262、persistence.py:186,188、forcing_submit_identity.py:208）；`PIPELINE_JOB_ACTIVE` ← `pipeline.py:76` set 4、`_PENDING` ← `:77`、`_FAILED_UI` ← `:78`；`FAILED_PIPELINE_STATUSES` ← `scheduler_state_types.py:60` set 4；`DURABLE_HYDRO_SUCCESS_STATUSES` ← `:48` set 4；`HYDRO_RUN_CODE_CLEARING_STATUSES` ← `:57` frozenset 6；`HYDRO_RUN_STATUSES` ← `schemas/run_status.schema.json:53` enum 14；折叠 `hydro.run_status` = 12（000003 的 10 + `pending` 000013:3 + `frequency_done` 000062:17），减 `frequency_done` = 11 ⊆ 14；`HYDRO_RUN_TERMINAL_UI` ← `pipeline.py:79` set 6；`DISPLAY_PRODUCT_READY_STATUSES` ← `hydro_display_constants.py:31` set 3；`QHH_LATEST_READY_RUN_STATUSES` ← `forecast_store.py:28` tuple 3；`PARSE_READY_RUN_STATUSES` ← `parser.py:48` tuple 3、`FAILABLE_RUN_STATUSES` ← `:49` tuple 6；`FORECAST_CYCLE_SUCCESS_STATUSES` ← `file_orchestration_journal.py:707` set 4；`CYCLE_STATUSES` = 折叠 `met.cycle_status` 18（000003:35-52 的 16 + `cancelled` 000013:7 + `canonical_incomplete` 000027:7）；`CYCLE_TERMINAL_UI` ← `pipeline.py:80` set 10；`CYCLE_TERMINAL_UPSERT` ← `met_store.py:16` tuple 2；`SLURM_TERMINAL_STATES` ← `slurm_validation.py:78` set 11；`SLURM_TERMINAL_SUCCESS` ← `run_qhh_continuous.py:69` {COMPLETED}、`SLURM_TERMINAL_FAILURE` ← `:70` set 8；`QC_VERDICTS` 新 {passed, failed, blocked}；`READINESS_ITEM_STATUSES` ← `readiness_item_contracts.py:5` frozenset 5。
  - **selector**：`.py` additive loop（#1656 形态，`apps|packages|services|workers|scripts/**`，放 #1627 之后、#2323 之前）；`tests/test_status_vocab_parity.py` 追加进 `PathTestRule("db/migrations/*.sql", …)`（~:5264-5276）；新行 `PathTestRule("schemas/run_status.schema.json", (parity,))`；`PathTestRule("packages/common/status_vocab.py", ("tests/test_hydro_status_set_parity.py",))`；exact-set 断言与 4.2 同根（#2937 pin 清单 ~33 处 + `:1189` rider 集合 + `:14180-14181` 计数 "3"→"4"），与 1.1/3.1/4.2 谁先合并谁先改。PR Boundary 偏离：以上路由行 + 受影响断言。
  - **风险包**：Concurrency（`scheduler_state_types` hydro 集合保持 #1581 共享对象身份与 `DURABLE` 可变性，词汇 frozenset 不直接共享）；Schema（只读不改）。三席 `correctness`、`test-evidence+spec-compliance`、`integration`（selector 路由 + 对象身份）。

  **Suggested fixture level:** expanded - project-profile 的 run_status/pipeline_job 契约风险轴 + #1581 共享对象身份。

  **Minimal mergeable slice:** atomic - 词汇与 canonical 对照测试互为 oracle。

- [ ] 2.1b `tests/test_status_vocab_parity.py` 的重复字面量扫描（排除 `status_vocab.py` 自身）+ `tests/status_literal_allowlist.json`（在当前树上跑扫描，命中逐条裁决，键为 `file` + 排序成员 + reason）+ allowlist 路由。

  Depends on: 2.1a

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_select_ci_tests.py`；PR 附 allowlist 裁决表（估算 130 命中 / 43 文件 / 111 唯一键：services 106、apps 13、scripts 6、packages 3、workers 2；Set 117 / Tuple 13）。

  钉死的契约（2.1b，fixture review 2026-10-17 + split）：

  - **扫描口径**：只看 `ast.Set` 与 `ast.Tuple`（List 不算；`frozenset({...})` 不重复计数）；`ast.walk` 全模块；星号元素只比较常量元素或跳过（择一并写明）；大小写敏感（`receipt.py:57 ("PASS","BLOCKED")` 不命中，因此不能是 allowlist 条目）；排除 tests 与 `status_vocab.py`。
  - **allowlist**：顶层 list，条目恰好 `{file, members(sorted), reason}`，唯一、按 `(file, members)` 排序；stale（文件缺失 / 键不再匹配）失败；消息写 `file:line`；reason ∈ `pending-migration | distinct-vocabulary | not-a-status-set`；`PathTestRule("tests/status_literal_allowlist.json", (parity,))` + red-when-removed。
  - **D3 补行的处置在 2.1b 前定**：`ACTIVE_PIPELINE_STATUSES`、`ACTIVE_HYDRO_STATUSES`、`FAILED_PIPELINE_STATUSES` 三副本、`RESERVATION_ACTIVE_STATUSES` 已由 2.1a 入词汇，allowlist 中记 `pending-migration`（2.3 清）。
  - **2.2 的 `apps/api/routes/pipeline.py:76-80` 是五个本地集合**（不是四个）。

  **Suggested fixture level:** compact - 纯测试 + 数据文件。

  **Minimal mergeable slice:** atomic - 扫描与 allowlist 互为 oracle。

- [ ] 2.2 `apps/api/routes/pipeline.py` 的状态 tuple 与五个本地集合（:76-80）改为从 `status_vocab` 绑定（tuple 用 `PIPELINE_JOB_STATUS_ORDER`）；`schemas/pipeline_job.schema.json` enum 加 `reservation_lost`（按 `PIPELINE_JOB_STATUS_ORDER` 顺序，从 2.1 移来）并删 parity 的 `_PENDING_PUBLIC_DELTA`、把 `reservation_lost` 加进 `tests/api_contract_helpers.py` 的 `OPS_JOB_STATUS_ENUM`、加 `PathTestRule("schemas/pipeline_job.schema.json", (parity, test_api_contract_pipeline_ops, test_file_journal_full_tree_budget_contract))`；手改 `openapi/nhms.v1.yaml` 的 enum（该文件为手工维护）；`cd apps/frontend && corepack pnpm generate:api` 重生成 `src/api/types.ts`；allowlist 中 `pipeline.py` 的条目删除。

  Depends on: 2.1b

  Verify：`uv run pytest -q tests/test_api_contract.py tests/test_openapi_drift.py tests/test_api_contract_pipeline_ops.py tests/test_pipeline_logs_artifacts.py tests/test_response_model_preservation_pipeline.py tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py tests/test_file_journal_full_tree_budget_contract.py`；`PYTHONHASHSEED=0` 与 `=1` 下 `tests/test_openapi_drift.py` 均通过；`cd apps/frontend && corepack pnpm run check:api-types`；**node-27 live receipt**：display API 对含 `reservation_lost` 行的 pipeline 查询返回 200 且 schema 校验通过（CLAUDE.md oracle 路由）。

  **Suggested fixture level:** expanded - 改 OpenAPI 对外 enum 与前端生成类型，dual-mounted 路由安全/契约风险轴。

  **Minimal mergeable slice:** atomic - API tuple、OpenAPI 与生成类型必须同 PR，否则 `test_api_contract` 红。

- [ ] 2.3 orchestrator 内站点改为导入命名集合（D3 表 orchestrator 行：`chain_types` 绑 `set(PIPELINE_JOB_TERMINAL)` 并继续作为共享对象、`chain_runtime_utils`、journal `:621`/`:707`、`scheduler`、`scheduler_candidates`、`persistence`、`scheduler_state_types` 重新绑定、六处 success 集合、`accepted_submit_identity` 派生 + parity pin）；allowlist 中这些条目删除。

  Depends on: 2.1b

  Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forced_resubmit_veto.py tests/test_hydro_status_set_parity.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_file_orchestration_journal_read_cache.py`，既有用例零改动通过。

  首刀 Verify：`uv run pytest -q tests/test_status_vocab_parity.py tests/test_forced_resubmit_veto.py tests/test_orchestration_chain.py`；allowlist 中 `chain_types`、`chain_runtime_utils`、journal `:621` 条目已删除。

  **Suggested fixture level:** expanded - orchestrator 状态机的终态集合是 state-machine 不变量。

  **Minimal mergeable slice:** `chain_types.py`、`chain_runtime_utils.py`、`file_orchestration_journal.py:621` 三个 `PIPELINE_JOB_TERMINAL` 站点改为绑定 - 内容逐字相同（7 值）、`chain_types` 保持 `set` 与共享对象，`test_forced_resubmit_veto` 的类型/identity 钉死照旧通过；其余站点（各自对应不同命名集合，同样内容相同）作为后继。

- [ ] 2.4 其余站点改为导入（tuple 站点绑定词汇中同序的 tuple：`packages/common/{forecast_store,met_store}.py`、`workers/output_parser/parser.py`、`apps/api/routes/hydro_display_constants.py`、`services/production_closure/{slurm_validation,met_validation,readiness_item_contracts,readiness_scheduler_evidence}.py`、`services/slurm_gateway/real_backend.py`、`scripts/{run_qhh_continuous,node27_frontier_stall_alert,m24_gateway_proof}.py`、`scripts/basin_retirement/database.py`）；QC verdict **读取/比较**站点改用 `normalize_qc_verdict`（receipt 写入与校验器保持原拼写，allowlist `distinct-vocabulary`）；allowlist 中无 `pending-migration` 条目。

  Depends on: 2.1b（首刀：四个非 orchestrator 站点，不继承 2.3）；后继另依赖 2.3 与 2.2（"allowlist 无 `pending-migration`"要等 orchestrator 条目与 `apps/api/routes/pipeline.py` 条目都清掉）

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

  Verify：`uv run ruff check .`；`uv run ruff --version`（记录到 PR）；在未冻结文件 `packages/common/region.py` 临时加完整的 `try:\n    pass\nexcept Exception:\n    pass` 确认报 BLE001 后撤销；在已冻结文件 `services/orchestrator/file_orchestration_journal.py` 确认同样代码不报；`uv run pytest -q tests/test_select_ci_tests.py`（`pyproject.toml` 是路由触发路径）；actionlint（CI `Workflow Lint`）绿。

  钉死的契约（fixture review 2026-10-17）：

  - **CI 门（事实更正 + 决策）**：今天没有任何 workflow 跑 ruff（`grep -rn ruff .github/` 为空；唯一的 `uv run ruff check .` 在 `Makefile`），design D5 原句「CI already runs ruff」错误，已改。4.1 在 `.github/workflows/ci.yml` 新增一个**不按路径 scope** 的 job `lint-ruff`（`uv sync --all-extras --dev` 后 `uv run ruff check .`；不挂在 `changes` filter 下，因为它守的正是 `pyproject.toml` 与全仓 `.py`），不改既有任何 job 或命令，不加 `ruff format --check`。PR Boundary 从「仅 `pyproject.toml`」扩为 `pyproject.toml` + `ci.yml`，偏离记录写明；job 须过 `Workflow Lint`（actionlint）。
  - **配置形状**：`select = ["E", "F", "I", "B", "BLE"]`；新增 `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls = ["fastapi.Depends", "fastapi.Query", "fastapi.Path"]`（仓库惯例是位置默认参数：`apps/api` 有 67 处 `= Depends(`、77 处 `= Query(`；按文件冻结 B008 会让以后每个新路由模块变红；全局 `ignore = ["B008"]` 否决）；独立的 `[tool.ruff.lint.per-file-ignores]` table 放在 `[tool.ruff.lint]` 之后（现无 `ignore`/`extend-exclude`/`target-version`，无已有表可合并）。
  - **生成规则**：在干净 checkout 上、**最终配置**（含 extend-immutable-calls）下跑 `uv run ruff check --select B,BLE --output-format json .`，键 = JSON `filename` 转相对仓库根 POSIX 路径（加引号、字典序；157 个路径无 glob 元字符），值 = 该文件实际违反的具体码排序列表（`BLE001`/`B0xx`，不写 `B`/`BLE` 前缀），生成块不加注释；生成命令逐字记进 PR body，不新建 `scripts/governance/` helper。禁止 `--fix`/`--unsafe-fixes`（584 条中 200 条可自动修，清理不在本 issue）。全部冻结：含 `tests/`（45 文件）、16 个 `openspec/changes/**` receipt/probe 脚本、`db/seeds/seed_demo.py`；不用 `extend-exclude`（会连 E/F/I 一起关掉）。
  - **预期清单规模（2026-10-17，ruff 0.15.12）**：最终配置下约 510 条 / 150 文件 / 182 个 (file, code) 对；剩余 B008 只在 `tests/test_forecast_api.py` 与 `tests/test_scheduler_backfill_predecessor.py`；B905 触发是因 target 推断为 py311，属预期；`preview` 未开，B043/B901/B903/B909 不在清单。
  - **搬移/改名规则（写入「约定」，对 5.x/6.x 与 `openspec archive` 生效）**：纯搬移或改名可以给目标文件新增或重键 `per-file-ignores` 条目，但只能带源文件条目里已有的码，且源+目标每个码的总数不超过搬移前源文件的数；其余新增都算增长、禁止。已冻结且会被后续任务搬的：`services/orchestrator/file_orchestration_journal.py`（BLE001×7、B904×9）、`workers/shud_runtime/runtime.py`（BLE001×3、B904×3）；未归档 change 下的 receipt/probe 脚本在 archive 时路径变化，按此规则重键。
  - **Release/dependency 风险包补 4.1**：清单在 ruff 0.15.12（`uv.lock`）下生成，`pyproject.toml` 只写 `ruff>=0.3.0` 下限；以后升级 ruff 若有 B 规则从 preview 转 stable，须同 PR 重新生成清单；PR 证据记录 `uv run ruff --version`。Documentation：4.1 无文档改动；`docs/governance/entropy-audit-2026-10-16.md` 的 "no bugbear" 是带日期的审计快照，不改。
  - **残余风险**：per-file-ignores 里多余的码 ruff 不报（RUF100 只管 `noqa`），清理 PR 修完违规后必须同步删掉对应码，本 issue 不加检查。

  **Suggested fixture level:** compact - 配置加生成清单。

  **Minimal mergeable slice:** atomic - 规则与冻结清单必须同 PR。

- [ ] 4.2 新增 `tests/test_any_ratchet.py` 与 `tests/any_ratchet_ceiling.json`（初始 ceiling = 引入时计数；计数定义见 spec：注解内 `Name`/`Attribute` `Any`，字符串注解先解析）；selector 以 #1656 形态的 additive loop 加选（允许的测试改动：受影响的精确集合断言，PR 列出）。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_any_ratchet.py tests/test_select_ci_tests.py`；把 ceiling 临时调低 1 确认失败后恢复；把 ceiling 临时调高到 `floor(count/0.95)+1` 确认失败并要求降低 ceiling 后恢复（两者都记入 PR，且各有 `(count, ceiling)` 纯函数单测）。

  钉死的契约（fixture review 2026-10-17）：

  - **计数定义**：扫描根 `apps packages services workers scripts`（`_SCAN_ROOTS = ("apps", "packages", "services", "workers", "scripts")` 模块级常量；「不含 tests」只指不扫 `tests/` 根，根内不按文件名过滤），`sorted(root.rglob("*.py"))`，排除 `{"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}`（按相对路径 `parts`），`SyntaxError` 直接抛出。注解位置 = `posonlyargs/args/kwonlyargs/vararg/kwarg` 的 `.annotation`、`returns`、所有 `AnnAssign.annotation`（含嵌套函数与 `if TYPE_CHECKING:`；`ast.walk` 全模块）。注解子树内每个 `Name(id="Any")` 与每个 `Attribute(attr="Any")`（不论 base；真实树有 105 处 `_chain.Any`/`_scheduler.Any`，只认 `typing.Any` 会少 105）各计 1，`dict[str, Any] | Any` 计 2。字符串注解：注解子树内每个 str `ast.Constant` 用 `ast.parse(value, mode="eval")` 解析后递归再走；例外两条：`Literal[...]` 下标内的字符串不解析（`schema.py:146` 的 `Literal["class", ...]` 解析会炸，全树 51 处）；其它解析失败的字符串计 0 并继续。不计：注解之外的 `Any`（裸别名赋值 `X = Callable[..., Any]`、`TypeAlias` 的值、`cast(Any, …)`、`isinstance`）、`# type:` 注释；别名不展开（把 `Any` 挪进别名可绕过计数——记为残余风险）。
  - **ceiling 文件**：`tests/any_ratchet_ceiling.json` 恰好 `{"ceiling": <int>}`——单键、`type(v) is int`（bool 拒绝）、`>= 0`；多键/缺键/非 int 均失败并点名文件。初始值 = 实现分支 base HEAD 实测计数（PR 记录计数与 base SHA；2026-10-17 在 `76e4070be` 实测 9500，不得照抄）。
  - **5 % 规则用整数算**：`count > ceiling` 失败；`count * 20 < ceiling * 19` 失败；不用浮点（ceiling 9500 时 9025 过、9024 失败、9501 失败）。失败信息：超上限时给出 count、ceiling、超出量、文件路径，并写「remove `Any` annotations; raising the ceiling is an explicit, reviewed ratchet change」；低于 5 % 时给出 count、ceiling 并写 `set "ceiling" to <count> in tests/any_ratchet_ceiling.json`。不做 ceiling 历史核查（spec 的 "only decrease" 指 ceiling 本身，调高是显式评审改动）。
  - **测试布局**：计数函数接受 `repo_root`；spec 两个 scenario（"Added Any fails"、"Cleanup must lower"）在 `tmp_path` 合成树 + 自己的 ceiling 文件上跑，不碰真实 ceiling；另一个测试跑真实树；`(count, ceiling)` 判定抽成纯函数并有双向单测。
  - **selector**：常量 `ANY_RATCHET_TEST = "tests/test_any_ratchet.py"`、`ANY_RATCHET_ROOTS = ("apps/**", "packages/**", "services/**", "workers/**", "scripts/**")`；loop `if path.endswith(".py") and _any_path_matches([path], ANY_RATCHET_ROOTS): selected.add(ANY_RATCHET_TEST)`，不设 `matched`，放在 #1627 loop（及 1.1 的 loop，若已合并）之后、#2323 之前。加 `PathTestRule("tests/any_ratchet_ceiling.json", ("tests/test_any_ratchet.py",))`（紧邻 `tests/fixtures/*.json` 行；今天只改该文件选出空集、会掉到零断言冒烟）+「删掉即红」断言；`tests/test_select_ci_tests.py` 加根集合比对测试（解析测试模块 `_SCAN_ROOTS` 与 `ANY_RATCHET_ROOTS`）与「去掉一个根即红」测试（先例 `:13536`/`:14198`）。PR Boundary 偏离：ceiling 路由行 + 两个 selector 配套测试。
  - **会变红的精确集合断言（启发式下限，约 35 处，首跑为准、PR 逐个列出）**：`RIVER_SEGMENT_WRITE_SURFACE_ROOTS`（`scripts/select_ci_tests.py:290`）对 `.py` 是五根的严格超集（只多 `db/**`），所以期望集含 `WRITE_SURFACE_SCAN_PATH` 且改动路径是五根下 `.py` 的断言都会红：`tests/test_select_ci_tests.py` `:941 :1050 :1081 :1128 :1152 :1179 :1312 :1496 :1755 :1841 :1936 :2064 :2357 :2533 :2565 :2633 :2665 :2946 :2971 :3394 :3544 :4809 :6588 :7409 :7450 :7525 :7572 :7598 :7665 :7952 :12625 :12671 :15275`；三处要多做：`:1189` 的 rider 排除集要加新 rider；`:14180-14181` 的 `count == "3"` 变 `"4"` 且 `tests` 字符串加 rider；`:2432`（`.sql`）、`:7933`、`:13941`（`db/**`）不受影响。新 rider 不会翻转 `meta_guard_only`（五根下 `.py` 今天已选中 write-surface scan）。
  - **跨任务残余风险**：5.x facade 包装与本 change 其它新代码加 `*args: Any` 会抬高计数，那些 PR 须同 PR 调 ceiling。

  **Suggested fixture level:** compact - 一个计数测试。

  **Minimal mergeable slice:** atomic - 测试与 ceiling 文件同生。

## 5. journal 拆分（spec: orchestrator-structural-burndown；聚簇顺序见 design D7）

聚簇顺序：context → read_attribution → primitives（打破 paths 环的标识/段原语）→ replay → candidates → paths（**最高风险**：含被 monkeypatch 名的调用者）→ retry_helpers。`FileOrchestrationJournalRepository`、`FileJournalRetryService` 与 facade 驻留的被 patch 名不搬。函数级归属由 5.0 的 map 决定，本节不再估算行数。

- [ ] 5.0 聚簇图：新增 `tests/fixtures/file_journal_cluster_map.json`（每个顶层定义 → 一个聚簇或 `facade`；每个被 patch 名 → 其聚簇与全部调用点聚簇；全部九个被 patch 名（`_utcnow`、Repository 调用的五个 IO/stat/iter 名、以及 Repository/RetryService 调用的 `_released_candidate_cycle_scope`、`_cycle_scope_from_job_id`、`_next_file_manual_retry_job_id_for_run`）标为 `facade`）与 `tests/test_file_journal_cluster_map.py`（覆盖、唯一、被 patch 名规则）。

  Depends on: 2.3, 3.3

  Verify：`uv run pytest -q tests/test_file_journal_cluster_map.py tests/test_select_ci_tests.py`；`uv run ruff check .`；PR 附扫描器自身输出的全部 patch 点清单（file:line，含 `tests/test_file_orchestration_journal.py` 与 `test_gateway_reconcile_writer_quiescence.py` 的点），并与 `git grep -nE 'file_orchestration_journal\.[A-Za-z_][A-Za-z0-9_]*"' -- tests` 及 `git grep -nE -A2 '(setattr|patch\.object)\($' -- tests` 交叉核对；PR 记录生成基线 SHA 与按 kind 的顶层定义计数。

  钉死的契约（fixture review 2026-10-17）：

  - **生成基线**：只在 #2927、#2928、#2933 全部合并后生成 map（3.3 把 `def _utcnow` 变为 import 绑定；2.3 可能把 `TERMINAL_PIPELINE_STATUSES`、`_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES` 变为 import），依赖影响内容而非仅行号。HEAD `2d9076d77` 实测：顶层 337 个唯一名（214 def、22 class、95 Assign、6 AnnAssign）+ 159 个 import 绑定，无其它顶层语句；issue 的「214 个顶层定义」只数 def。
  - **JSON shape（精确）**：`{"module": "services/orchestrator/file_orchestration_journal.py", "clusters": {恰好 8 键 facade|context|read_attribution|primitives|replay|candidates|paths|retry_helpers: [已排序、唯一、非空 str]}, "patched": {名: {"binding": "def"|"class"|"assign"|"import", "cluster": 聚簇, "callers": {聚簇: [已排序的顶层单元名]}}}}`；不带 `schema_version`（与 1.1/4.2 fixture 一致）；多键/缺键失败并点名；`json.dumps(..., indent=2, sort_keys=True)` 写出。`callers` 必须列到函数（5.4 要知道聚簇内唯一调用 facade 驻留名的函数，5.5 要搬走调用者清单），class 是一个单元、方法不单列；`FileOrchestrationJournalRepository` 的 `callers` 为 `{}`（journal 内只在注解出现，`from __future__ import annotations`），不是错误。
  - **被 patch 名集合是 19 个，不是 9 个**：`_utcnow`、`read_bytes_limited_no_follow`、`list_directory_no_follow_limited`、`_stat_signature`、`_iter_regular_json_files`、`_iter_jsonl_files`、`_released_candidate_cycle_scope`、`_cycle_scope_from_job_id`、`_next_file_manual_retry_job_id_for_run`、`stat_no_follow`、`unlink_no_follow`、`open_lock_file_no_follow`、`_runtime_root_env_candidate`、`accepted_submit_candidate_immutable_evidence`、`_is_legacy_unversioned_reserved_forecast_master`、`MAX_FILE_JOURNAL_RECORDS`、`MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES`、`MAX_FILE_JOURNAL_READ_CACHE_ENTRIES`、`FileOrchestrationJournalRepository`。任务文本的「九个」只是放置已定的子集。额外十个的放置：`stat_no_follow`（Repository + `_iter_discovered_files`/`_iter_retention_surface_files` 调用）→ `facade`，因此 5.5 的「五个留 facade」改为「按 map」（实为六个）；`accepted_submit_candidate_immutable_evidence`（仅 `_accepted_submit_candidate_evidence`、`_candidate_row_self_bound_identity` 调用，无 class 调用者）→ `facade`，否则要改 `tests/test_file_orchestration_journal.py:8289` 的 patch，与 5.4「无测试改动」冲突；`_is_legacy_unversioned_reserved_forecast_master`（`tests/file_journal_legacy_seed_helpers.py:37` 以 `mock.patch.object` patch，唯一调用者 `_refuse_legacy_unversioned_reserved_forecast_master`）两者同留 `facade`；`MAX_FILE_JOURNAL_*` 三常量、`unlink_no_follow`、`open_lock_file_no_follow`、`_runtime_root_env_candidate`、`FileOrchestrationJournalRepository` → `facade`（调用者全在 class 内或就是该 class）。
  - **import 绑定名不进 `clusters`**：`binding == "import"` ⇒ 名不在任何 `clusters` 列表且 `cluster == "facade"`（今日为 `read_bytes_limited_no_follow`、`list_directory_no_follow_limited`、`stat_no_follow`、`unlink_no_follow`、`open_lock_file_no_follow`、`_runtime_root_env_candidate`、`accepted_submit_candidate_immutable_evidence`，3.3 后再加 `_utcnow`）；`binding != "import"` ⇒ 名在 `clusters[cluster]`。「九个标 facade」读作 `patched[n].cluster == "facade"`，不是把名加进 `clusters.facade`。
  - **顶层定义的定义**：`tree.body` 直接的 def / class / Assign / AnnAssign 名；import 排除；含 dunder 与私有名（`__all__` → `facade`，`LOGGER`、`_ClassT` 也计）；嵌套定义归其外层单元；顶层 `if`/`try`/`with` 语句大声失败（facade 今日没有）；sibling 文件中 `if TYPE_CHECKING:` 块只允许 import。
  - **覆盖检查跟着抽取走（选项 a，5.2–5.5 保持「map 测试零改动」）**：聚簇 `c` 的宿主文件是 `services/orchestrator/file_journal_<c>.py`（存在时），否则是 facade；每个已存在 sibling 的顶层 def/class/Assign/AnnAssign 名 == `clusters[c]`；facade 的顶层名 == `clusters.facade` ∪ 尚无 sibling 的聚簇；callers 从全部宿主文件取，sibling 内函数体对 facade 模块别名的属性访问也计为调用；迟查检查：sibling 文件在顶层或函数内都不得以任何 import 绑定 facade 驻留的被 patch 名。这些规则在 5.0 时为空真，必须按 1.1/#2920 先例用 `tmp_path` 合成用例覆盖：已搬聚簇通过；sibling 以 `from … import` 绑定 `_stat_signature` 失败；sibling 多出一个定义失败。5.2+ 对 map JSON 的编辑只允许为搬迁新建的 helper 增名，不得重分配。
  - **测试规则**：(1) shape；(2) 双向覆盖——未分配名失败、陈旧条目失败、失败点名定义；(3) 一名只在一个聚簇；(4) `patched` 集合 == 扫描器所得；(5) `binding` 与 facade AST 一致；(6) `callers` == AST 所得（注解子树外的 `Name` load，含嵌套函数，归到外层顶层单元）；(7) 放置规则：九个名 + `FileOrchestrationJournalRepository` + `FileJournalRetryService` 必须 `facade`；任一调用者在 `facade` ⇒ 名为 `facade`；`cluster != facade` ⇒ `callers.keys() == {cluster}`；调用者是赋值单元（import 时读取，无法迟查，今日 `_LATEST_REPLAY_ORDER_SENTINEL` 读 `MAX_FILE_JOURNAL_RECORDS`）⇒ 与该名同聚簇。
  - **扫描器必须覆盖 `tests/` 实际用到的全部 patch 形式**：(a) 任意调用、任一位置的 str 位置参数以 `"services.orchestrator.file_orchestration_journal."` 开头（含自定义 `_patch(...)` helper：`tests/test_file_orchestration_journal_read_cache.py` 755–884 七处）；(b) `setattr`/`delattr`/`patch.object(X, "name")`，X 经 `import … as`、`from services.orchestrator import file_orchestration_journal [as X]`、`importlib.import_module(...)` 或从返回模块的 helper 赋值的局部（`journal = _journal()`，`tests/test_file_journal_read_blocked_consumers.py:52-55`，patch 于 `:343`）绑定到 journal 模块；(c) 参数别名点 `tests/test_safe_fs_lock.py:227`（经 `_record_lock_opens(monkeypatch, file_orchestration_journal)` `:241`）——追踪调用实参或列入带站点的显式钉死常量。排除：`setattr(journal_module.os, "stat")`（patch `os`）、`setattr(journal.FileOrchestrationJournalRepository, ...)`（class 属性）、同名但针对 `scheduler_module`/`retention`/`scheduler_discovery_module`/`legacy_chain` 的 patch。
  - **logger 名接缝**：`tests/test_orchestration_chain.py:15485,15501,15734,15763`、`tests/test_retry.py:3281,4224,4236,4286,4296` 按 `record.name == "services.orchestrator.file_orchestration_journal"` 过滤；`_emit_reclaim_projection_warning` 留 `facade`（唯一用 `LOGGER` 的顶层函数）。
  - **聚簇分配由 implementer 按 5.2–5.6 任务文本的职责决定**，此后 map 是权威；PR 附跨聚簇引用矩阵（非 facade 单元 → 引用的、在 D7 顺序中更晚抽取的聚簇名）作为信息性抽取风险清单，不做断言。
  - **selector**：把 `"tests/test_file_journal_cluster_map.py"` 追加进 `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`（`scripts/select_ci_tests.py` ~:1618，注释引 #1581 先例 ~:1621-1626；at-site pin 与 exact set 都展开该常量，无需改字面量）；新增 `PathTestRule("tests/fixtures/file_journal_cluster_map.json", ("tests/test_file_journal_cluster_map.py",))`（~:4757-4781 旁；没有通用 `tests/fixtures/*.json` 规则，否则只改 JSON 的 PR 选 0 个测试）+ 一条 red-when-removed 测试。PR Boundary 偏离：一行路由 + 一条 selector 测试。
  - **5.1 的「补 `__all__`」是扩展**：`__all__` 已存在（~:729-741，11 个名）。
  - **残余风险（记录）**：并发 journal PR 新增顶层定义会打红 map 测试（有意，且 selector 路由使其在 PR 车道而非仅 master 暴露）；仅在测试文件新增 journal patch 点不会路由到 map 测试，只有 master 全量跑能抓。

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

- [ ] 5.5 抽取 paths → `file_journal_paths.py`：`_stat_signature`、`_iter_regular_json_files`、`_iter_jsonl_files`、`read_bytes_limited_no_follow`、`list_directory_no_follow_limited`、`stat_no_follow` 按 5.0 map **留在 facade**（Repository 调用它们）；搬走的顶层调用者改为函数体内延迟导入经 facade 属性调用。无测试改动。

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

  钉死的契约（fixture review 2026-10-17）：

  - **sibling 行**：在 facade 行 `PathTestRule("scripts/select_ci_tests.py", (SELECTOR_META_GUARD_TEST,))`（`scripts/select_ci_tests.py:6716-6719`）旁各加一条精确字符串行：`scripts/select_ci_tests_tables.py`、`scripts/select_ci_tests_rules.py`、`scripts/select_ci_tests_imports.py`，`tests=(SELECTOR_META_GUARD_TEST,)`、默认 `stop_on_match=False`；不用 glob。文件不存在也能匹配（`PATH_TEST_RULES` 用 fnmatch 字符串；两处 stat 改动路径的 sniff 对缺失路径返回 False；`_test_target_exists` 只 stat 测试目标）。不新建 `tests/test_select_ci_tests_{tables,rules,imports}.py`（同名推导会对 sibling 生效）。
  - **`_collection_smoke_required`**（`:7189`）：保留精确字符串成员判定，字面量扩为四个源 + 套件：`path in ("scripts/select_ci_tests.py", "scripts/select_ci_tests_tables.py", "scripts/select_ci_tests_rules.py", "scripts/select_ci_tests_imports.py", SELECTOR_META_GUARD_TEST)`；不用 `startswith`；docstring `:7179-7180` 同步。
  - **导入形式**：只用点分导入 `from scripts.select_ci_tests_<x> import …`，放在受保护的 `if str(Path(__file__).resolve().parents[1]) not in sys.path: sys.path.insert(0, ...)` 之后（先例 `scripts/publish_scheduler_file_registry.py:30-39`，其注释解释了这一情形；`scripts/node22_model_succession.py` 无 insert，不作先例）；禁止裸 `import select_ci_tests_tables`（pytest 下断且产生第二个模块身份）。sibling 绝不导入 facade，facade 可 patch 的值以参数传入（D7）。insert 在 6.1 落地：`tests/test_select_ci_tests.py:7467-7494` 的 exec 探针会把 `tmp_path` 留在 `sys.path` 前端（session 级泄漏，monkeypatch 不回滚），今天无害，6.3 前不允许改该测试——PR 记录「泄漏已知、接受」。
  - **新断言两腿**：(a) 对三个 sibling 参数化：`select_tests([sibling], repo_root=Path(".")) == select_tests(["scripts/select_ci_tests.py"], repo_root=Path("."))`，与计算出的 facade 结果比（今天 4 个目标：硬门禁节点、`test_river_segment_write_surface_scan.py`、`test_select_ci_tests.py`、`test_timescale_write_guard_wire_site_invariant.py`），不与字面量 `[SELECTOR_META_GUARD_TEST]` 比；(b) `_github_output_fields(tmp_path, [sibling], repo_root=Path("."))`（helper `tests/test_select_ci_tests.py:8086`）在 `count`、`tests_json`、`meta_guard_only`、`collection_smoke_required` 四键上等于 facade 的——没有 (b)，smoke 谓词改动无 oracle。父提交上 sibling 路径今天选出 8 个核心冒烟、`collection_smoke_required=false`，断言在父提交上应红。
  - **`scripts/ci_selector_snapshot.sh`**：100755、`#!/usr/bin/env bash` + `set -euo pipefail`；用法 `ci_selector_snapshot.sh [SELECTOR_ROOT] [REPO_ROOT]`，两者默认 `git rev-parse --show-toplevel`；头注释写明本地专用、不得在 node-22 跑（uv 禁令）；`unset GITHUB_ACTIONS`。每个输入调一次 `uv run python "$SELECTOR_ROOT/scripts/select_ci_tests.py" --changed-file <tmp> --repo-root "$REPO_ROOT" --github-output <tmp>`（与 `.github/workflows/ci.yml:510` 的调用形式一致），固定列表为 tasks.md 6.x 约定的七个路径按序硬编码（`packages/common/state_manager.py`、`apps/api/routes/pipeline.py`、`apps/frontend/src/App.tsx`、`docs/runbooks/current-production-ops.md`、`scripts/select_ci_tests.py`、`tests/test_production_scheduler.py`、`db/migrations/000003_enums.sql`），逐路径各一次 + 七路径合并一次；stdout 输出单个 JSON 对象，键为输入标签按序，每个值恰好 `count`、`tests_json`、`meta_guard_only`、`collection_smoke_required` 四键按此顺序（排除第五行 `tests=`），用 `uv run python` 拼 JSON 不用 jq；selector stderr 不捕获；成功退出 0，selector 失败经 `set -e` 传播。脚本正文不得含拓扑硬门禁 token（`scripts/` 是扫描根）。
  - **父/head 比较方法（§6 全部沿用）**：`P=$(git merge-base origin/master HEAD)`；`git archive "$P" scripts | tar -x -C <scratch>/parent`（不建 worktree、不 stash、不 `git checkout <P> -- …`、不单文件 `git show` facade——6.2 起父 facade 会经命名空间包解析到 head 的 sibling，比较会假绿）；`scripts/ci_selector_snapshot.sh "$PWD" > head.json`、`scripts/ci_selector_snapshot.sh <scratch>/parent "$PWD" > parent.json`（父 selector 代码 + head 仓库根，只有 selector 代码变）；`cmp parent.json head.json`。
  - **`.sh` 路由**：加第四行 `PathTestRule("scripts/ci_selector_snapshot.sh", (SELECTOR_META_GUARD_TEST,))` +「删掉即红」断言（今天经 `_is_backend_shell_path` 掉到 6 个核心冒烟）；PR Boundary 偏离：「三行」变「四行」。
  - **Q2**：6.1 定为平铺文件（sibling 3 个 ≤ 4），不建 `scripts/ci_selector/`。
  - **PR 盘点格式**：表 name | 测试里用的别名 | `setattr` 次数 | 留 facade 或在 6.x 搬走。实测 11 个名、57 次 `monkeypatch.setattr`：`PATH_TEST_RULES` 40（29 经 `select_ci_tests`、11 经 `_prod_module`）、`SUPPORT_MODULE_TEST_RULES` 5、`_build_suite_importer_index` 3（6.4 搬走，facade 全局读于 `:7206`）、`_SUITE_IMPORTER_PARSE_STATS` 2（6.4 搬走）、`TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS`、`RIVER_SEGMENT_WRITE_SURFACE_ROOTS`、`RIVER_SEGMENT_WRITE_SURFACE_SQL_ROOTS`、`PATH_CANONICALIZATION_FAMILY_GUARD_ROOTS`、`CORE_SMOKE_TESTS`、`PRODUCTION_TOPOLOGY_SCAN_ROOTS`、`PRODUCTION_TOPOLOGY_SCAN_FILES` 各 1；无字符串形式 `patch(...)`/`patch.object`。两处源码 pin：exec 变异 `:7467-7494`、node-id 正则扫描 `:10930`。

  **Suggested fixture level:** compact - 路由规则与快照脚本。

  **Minimal mergeable slice:** atomic - 规则行、smoke 匹配与快照脚本是同一前置。

- [ ] 6.2 B1 + `PathTestRule` → `select_ci_tests_tables.py`；facade 以导入绑定同名。

  Depends on: 6.1

  Verify：`uv run ruff check .`；`uv run pytest -q tests/test_select_ci_tests.py tests/test_entropy_audit_line_references.py`；`uv run python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`；6.1 的父提交/head 快照 `cmp` 为 0（PR 附一对 `shasum -a 256 parent.json head.json`）；值等价探针（见钉死契约）全绿；`tests/test_select_ci_tests.py` 唯一改动是 node-id 扫描的源路径（`git diff -U0 origin/master -- tests/test_select_ci_tests.py` 只有那一处）；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  **Suggested fixture level:** compact - 数据搬迁。

  钉死的契约（fixture review 2026-10-17）：

  - **B1 成员是 132 个名，不是 122 个**：66 `*_TESTS`、42 `*_PATH`、7 `*_ROOTS`、6 `*_MODULES`（`scripts/select_ci_tests.py` ~:235-2957）+ `PathTestRule`（~:1357-1366）+ 被 B1 tuple 引用的 10 个单例（`SELECTOR_META_GUARD_TEST` :19、`PROVISION_DIRECT_GRID_DRY_RUN_TEST` :828、`PROVISION_OUTPUT_REGISTRY_PARENT_TEST` :829、`PYTHON_ENVIRONMENT_TRUTH_TEST` :934、`NODE22_ENTRYPOINT_INVARIANT_TEST` :975、`NODE22_ENTRYPOINT_INVARIANT_PYTHON_SCAN_TEST` :985、`REVIEW_GATE_ISSUE_MEMORY_TEST` :1167、`REVIEW_GATE_CLI_TEST` :1171、`SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST` :1327、`DIRECT_GRID_DISPLAY_CUTOVER_FLIP_ATOMIC_TEST` :1953），它们留 facade 会让 sibling 反向导入 facade 成环；无一被测试 patch、无一依赖 facade。留在 facade：`*_PATHS`（:2856、:2894）、`*_PATH_PATTERNS`（3 个）、`PRODUCTION_OPS_SUBRUNBOOK_GLOB` 及其余约 40 个只被规则表引用的单例（归 6.3）。**逐字搬、保持源顺序、每个定义连同其前导注释块一起搬**（B1 内全部引用都指向前文，零前向引用）；facade 删除 `from dataclasses import dataclass`（否则 F401）；指向「下面/上面那条规则」的注释（如 ~:1891-1897）改为点名 `scripts/select_ci_tests.py` 的 `PATH_TEST_RULES`，~:1353-1354 的孤立注释留 facade。
  - **facade 绑定形式**：紧跟 6.1 的受保护 `sys.path.insert` 之后写显式 `from scripts.select_ci_tests_tables import (...)` 列全 132 个名（先例 `scripts/publish_scheduler_file_registry.py:30-39`，过 E402）；不用 `import *`（F405 且缺名静默）。facade 自身未使用、只供测试读取的 13 个名用 `X as X` 抑制 F401：`BASIN_RETIREMENT_PACKAGE_MODULES`、`ENTROPY_AUDIT_PACKAGE_MODULES`、`MERGED_REGISTRY_PUBLISH_PACKAGE_MODULES`、`MODEL_SUCCESSION_PACKAGE_MODULES`、`PUBLISH_REGISTRY_PACKAGE_MODULES`、`SCHEDULER_REFRESH_PACKAGE_MODULES`、`DIRECT_GRID_CONTRACT_TESTS`、`DIRECT_GRID_E2E_TESTS`、`NODE22_REFRESH_TIMER_HEALTH_RUNBOOK_TESTS`、`DIRECT_GRID_DISPLAY_CUTOVER_FLIP_ATOMIC_TEST`、`NODE22_ENTRYPOINT_INVARIANT_TEST`、`NODE22_ENTRYPOINT_INVARIANT_PYTHON_SCAN_TEST`、`PUBLISH_REGISTRY_OWNER_PATH`；`ruff check --fix` 排序该块。不新增「facade 暴露全部名」测试（spec 只允许三类测试改动；缺名在导入或使用时即失败）；sibling 的 `__all__` 可选。测试一律以点号 `scripts.select_ci_tests` 导入（`tests/test_select_ci_tests.py:29-30`），`scripts` 无 `__init__.py`，script 模式依赖 6.1 的 insert。
  - **line-reference 硬门会红**：`SCHEDULER_IMPORTER_TESTS` 内 ~:1529 的注释含 `` `select_ci_tests.py:3809-3811` ``，基线按文件键（`scripts/governance/entropy_audit/line_reference_baseline.json:134`），搬进 sibling 即算新增，打红 `tests/test_entropy_audit_line_references.py:223` 与每个 PR 都跑的 `Production Topology Hard Gate`。处理：注释改为点名符号（`select_tests` 的 changed-test importer-closure 分支；原行号已陈旧）；删除基线条目 `"select_ci_tests.py:3809-3811": 1`（允许缩小）；不为 sibling 加基线键。6.3 前瞻：~:5901 的 `` `select_ci_tests.py:5354-5364` `` 在 `PATH_TEST_RULES` 内，同一个坑。
  - **large-file-guard 会拦提交**：sibling 约 1,700 行，`.large-file-guard.json` `maxLines` 1000，PreToolUse hook 拒绝 `git commit`；facade 已在 exclude（`.large-file-guard.json:67`）。同一 commit 把 `scripts/select_ci_tests_tables.py` 加进 `exclude`，记 PR Boundary 偏离；6.3 的 rules 模块（约 4,200 行）同样需要。
  - **node-id 扫描会静默失覆盖**：`tests/test_select_ci_tests.py` ~:10984-10996 只正则扫 facade 源文本；47 个钉死 node id 中 38 个只在 B1 tuple 内（`DIRECT_GRID_CONTRACT_TESTS`、`ORCHESTRATOR_MANIFEST_SURFACE_TESTS`、`FILE_JOURNAL_READ_STATE_TESTS`、`FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`），搬后该测试零改动仍绿但只查 9 个。6.2 就把那一处 `read_text` 改为读 `sorted(Path("scripts").glob("select_ci_tests*.py"))` 全部文件（spec 允许的「node-id 扫描」类改动，只是提前）；6.3 只剩 exec pin 一处。
  - **值等价探针（6.2 的主 oracle，本地跑、结果贴 PR body、不提交）**：7 路快照只触及少数表，且 `select_tests` 返回的是集合派生的过滤列表（~:7430），常量内部顺序不会反映到输出。探针：用 `importlib.util.spec_from_file_location` 从 6.1 的 `git archive` 暂存目录加载父提交 facade（不走包导入），再加载 head facade 与 `scripts.select_ci_tests_tables`；对 131 个数据名断言 `getattr(parent, n) == getattr(head_tables, n)` 且 `getattr(head_facade, n) is getattr(head_tables, n)`；`PathTestRule` 比较 `dataclasses.fields`；`PATH_TEST_RULES`、`SUPPORT_MODULE_TEST_RULES`、`CHANGED_TEST_FILE_RULES` 逐元素比较 `dataclasses.astuple`（跨两份加载的类 `==` 恒 False）。tuple `==` 对顺序敏感，这就是「源顺序保持」的证明。
  - **活性演练（本地、不提交、PR 记录）**：(a) 从 sibling 删一个名 → facade 导入 `ImportError`（`uv run python scripts/select_ci_tests.py --help` 或 ~:8079 的 CLI 测试）；(b) 交换一个搬迁 tuple 的两个元素 → 值等价探针红（快照可能不红）；(c) 改一个搬迁 tuple 内的 node id → glob 后的 node-id 扫描红（今日 facade-only 扫描会保持绿）。
  - **被 patch 全局**：与 6.1 清单一致（11 个名）；搬走的 `CORE_SMOKE_TESTS`、四个 `*_ROOTS`、`PRODUCTION_TOPOLOGY_SCAN_ROOTS` 的读者（`select_tests`、`_is_production_topology_scan_input`）都留 facade，patch facade 绑定仍生效；无字符串形式 `patch()`。exec 变体探针（~:7528-7556）无需改：它只改写 `select_tests` 文本，exec 副本从 `sys.modules` 取到已加载的 sibling。
  - **路由**：6.1 的精确行已把 `scripts/select_ci_tests_tables.py` 路由到 `SELECTOR_META_GUARD_TEST`，`_collection_smoke_required` 已覆盖，6.2 不加新行；基线 JSON 改动经 ~:6804-6805 路由到 `LINE_REFERENCE_TEST`。Verify 前必须 `git add scripts/select_ci_tests_tables.py`（`_tracked_python_files("scripts")` ~:5498 与 one-hop 域 ~:5937 只看 tracked 文件）。`INTENTIONAL_RULE_GAP_EXCLUSIONS` 在测试文件内（~:11120），不在 selector。选择器常量在 `scripts`/`tests`/`.github` 下无外部导入方。
  - **PR Boundary 偏离（记录）**：`.large-file-guard.json` 排除项、基线条目删除、line-reference 注释改写、node-id 扫描 glob、tasks.md 6.2/6.3 措辞。facade 实为 7,707 行。
  - **残余风险**：6.2 后任何向 facade 新增 `*_TESTS`/`*_PATH` 常量的 PR 会重新引入 B1 漂移（facade 导入处加一行指针注释）；6.3 继承约 40 个只被规则表读的常量、~:5901 的行引用与体积排除；值等价探针是 PR body 证据而非提交测试。


  **Minimal mergeable slice:** atomic - 常量表互相 `*` 展开，拆半则导入断。

- [ ] 6.3 B2 规则表 → `select_ci_tests_rules.py`，facade 以导入绑定 `PATH_TEST_RULES` 等（`select_tests` 继续读 facade 全局，patch 生效）；同 PR 更新读源码的 `select_tests` exec pin（node-id 扫描已在 6.2 改为 glob 全部 `scripts/select_ci_tests*.py`）；同 commit 把 `select_ci_tests_rules.py` 加进 `.large-file-guard.json` 排除项，并处理 `PATH_TEST_RULES` 内 ~:5901 的 `` `select_ci_tests.py:5354-5364` `` 行引用（改注释 + 删基线条目）。**最高风险步骤。**

  Depends on: 6.2

  Verify：父提交与 head 四个输出字节一致；`tests/test_select_ci_tests.py` 通过，diff 中 tests 改动仅限 exec pin 路径；"patch facade `PATH_TEST_RULES` 仍能改变输出"断言通过。

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

  Verify：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`；`git grep -n "pages/hydroMet/bootstrap" apps/frontend/src` 为空；`git diff --name-status -M origin/master...HEAD -- apps/frontend` 恰好一行 `R100 apps/frontend/src/pages/hydroMet/bootstrap.ts -> apps/frontend/src/stores/hydroMetBootstrap.ts` + 14 行 `M`、无 `A`/`D`（R100 同时证明导出不变、无 shim）；`git diff -U0 origin/master...HEAD -- apps/frontend/src | grep -E '^[+-][^+-]'` 为 30 删 30 增且每行含 `pages/hydroMet/bootstrap` 或 `hydroMetBootstrap`；`git grep -o hydroMetBootstrap -- apps/frontend/src | wc -l` 为 32；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（fixture review 2026-10-17，pass）：

  - **导入形式**：32 处一律 `@/stores/hydroMetBootstrap`（`stores/` 内兄弟文件已互相用 `@/stores/...`，如 `stores/overviewData.ts:8-10`），不用相对路径；`vi.mock(...)`、`typeof import(...)`、`vi.importActual(...)` 字符串与 import 字符串逐字相同；只做纯字符串替换，不重排 import、不跑格式化。
  - **15 个文件**：生产 7（各 1 行）`stores/hydroMetProductData.ts:4`、`stores/stationLayerData.ts:9`（多行 import 的 `} from` 行）、`components/map/M11StationForcingPopup.tsx:36`、`components/map/M11RiverForecastPanel.tsx:20`、`pages/OverviewPage.tsx:40`、`pages/m11/useHydroMetProduct.ts:4`（type-only）、`pages/m11/useStationLayer.ts:5`（type-only）；测试 8（23 行 25 处）：`components/map/__tests__/M11RiverForecastPanel.test.tsx` :9/:14/:15、`M11RiverForecastPanelTouchZoom.test.tsx` :7/:18/:19、`M11StationForcingPopup.test.tsx` :10/:16/:17、`M11StationForcingPopupChartResize.test.tsx` :9/:23/:24、`M11StationForcingPopupTouchZoom.test.tsx` :8/:19/:20、`pages/m11/__tests__/useStationLayer.test.tsx` :4/:18/:19（:19 两处）、`stores/__tests__/stationLayerData.test.ts` :3/:16/:17（:17 两处）、`pages/hydroMet/__tests__/bootstrap.test.ts` :22 与 :580（`readFileSync` 路径 `'src/pages/hydroMet/bootstrap.ts'` → `'src/stores/hydroMetBootstrap.ts'`；:559/:572 的注释不动）。
  - **`bootstrap.test.ts` 留在 `pages/hydroMet/__tests__/`**，不改名不搬（spec 点名该位置）；之后 `pages/hydroMet/` 只剩测试，整理是另一单。无 barrel（`src/stores/index.ts`、`src/index.ts` 不存在）。搬后 `src/pages` 之外无非测试文件导入 `@/pages`（7.2 的 restricted-imports 从零违规起步）；被搬文件只导入 `@/api/*`、`@/lib/hydroMet/*`。
  - **不动**：`settledError`（`bootstrap.ts:292`/`:325`，7.4 后继在新路径改）；`tests/test_direct_grid_display_cutover_model_resolution.py:48,542`（注释/docstring，改了会打开 backend filter）；`docs/governance/API_CONTRACT_*.md`、`docs/plans/`、`docs/governance/entropy-audit-2026-10-16.*`、`openspec/changes/archive/**` 与本 change 自身 design/proposal/spec 里的路径引用（无测试读取；改 `docs/**` 会加文档门）。
  - **Risk packs**：无 pack 点名 7.1——纯搬迁，前端改动只能打红 typecheck/lint/tests 不动生产数据；Legacy pack 明确：不留 shim，旧路径删除。PR Boundary：15 个前端文件 + tasks.md 7.1 勾选。CI 只走 `frontend` filter（`frontend-build` + `frontend-e2e`，Playwright 分片按 spec 文件切、不受源文件搬迁影响），不登记 selector。

  **Suggested fixture level:** compact - 文件搬迁。

  **Minimal mergeable slice:** atomic - 搬迁与导入/mock 改路径同 PR。

- [ ] 7.2 引入 ESLint flat config（三条规则，依赖 `typescript-eslint`、`eslint-plugin-import-x`、`eslint-import-resolver-typescript`；restricted-imports 块 `ignores` 测试文件）、`lint` 脚本、`frontend-build` job 加 `pnpm lint`。

  Depends on: 7.1

  Verify：`cd apps/frontend && corepack pnpm install --frozen-lockfile && corepack pnpm lint; echo $?` 为 0；drill A（`src/stores/lintDrillUpward.ts`：`import { x } from '@/pages/x'` + `export const drillUpward = x` → `1:1 … no-restricted-imports`，退出 1）；drill B（`src/lib/lintDrillA.ts` / `lintDrillB.ts` 用**值导入**互导 → 两文件各报 `1:1 Dependency cycle detected import-x/no-cycle`）；三个 drill 文件删除后 lint 再次退出 0、`git status` 干净；`npx eslint --print-config src/stores/stationLayerData.ts` 的 rules 恰为三条、`--print-config src/stores/__tests__/overviewData.test.ts` 恰为两条（不含 `no-restricted-imports`）；以上输出贴进 PR；`uv run pytest -q tests/test_ci_shard_tests.py tests/test_ci_workflow_locked_install.py tests/test_select_ci_tests.py`；CI job 日志出现 `Lint frontend` 步。

  钉死的契约（fixture review 2026-10-17）：

  - **防空过（本次 revise 的唯一硬伤）**：配置必须写 `settings['import-x/extensions']: ['.ts', '.tsx']` 并注释原因——不写时 import-x 只解析 `.js` 依赖，246 文件树上 `no-cycle` 约 20 ms 且 `src/lib` 的 `@/` 与 `./` 互导环都不报（实测 vacuous）；写上后约 1.4–1.7 s、环能报出。不展开 `importX.flatConfigs.typescript`（会带旧版 `import-x/resolver` 与 `resolver-next` 混用）。环 drill 是「门在工作」的唯一证据，不可省；drill B 必须值导入（`import type` 一侧会让环消失）。
  - **依赖与写法（偏离 issue 的「eslint 9 / `tseslint.config`」，记录）**：`eslint@^10.13.0`（engines `^20.19.0||^22.13.0||>=24`；CI `node-version: "20"` 解析到 20.x 可用，Dockerfile node 22）、`typescript-eslint@^8.71.2`（peer eslint `^10`，ts `<6.1`，仓库 ts 5.9.3）、`eslint-plugin-import-x@^4.17.1`、`eslint-import-resolver-typescript@^4.4.5`；不装 `@eslint/js`、不引任何 recommended 预设，只开三条规则；用 `defineConfig` from `eslint/config`（`tseslint.config` 在 8.71 已 `@deprecated`）；resolver 用 `'import-x/resolver-next': [createTypeScriptImportResolver({ project: `${import.meta.dirname}/tsconfig.app.json` })]`，不用旧 key `import-x/resolver`；`languageOptions: { parser: tseslint.parser }`，**不开 `projectService`**（三条规则不需类型信息）。
  - **restricted 块**：核心规则 `no-restricted-imports`（非 `@typescript-eslint/` 版），`files: ['src/{stores,components,lib,api,hooks}/**/*.{ts,tsx}']`，`ignores: ['**/__tests__/**', '**/*.test.*']`，`patterns: [{ group: ['@/pages', '@/pages/**', '**/pages', '**/pages/**'], message }]`（相对 `../pages/X` 会报；`stores/__tests__/` 下不报）。`no-explicit-any` 覆盖整个 `src` 含测试（今日 0 违规，`src/api/types.ts` 生成文件无 `any`）；不加全局 `ignores`（脚本只跑 `eslint src`）；`"lint": "eslint src"` 照原文，不加 `--max-warnings`（今日无 `eslint-disable`）。
  - **实测基线（7.1 后树，246 文件）**：三条规则均 0 违规、退出 0；7.1 前树恰 4 条 `no-restricted-imports`（`M11RiverForecastPanel.tsx:20`、`M11StationForcingPopup.tsx:36`、`hydroMetProductData.ts:4`、`stationLayerData.ts:4`），cycle 0（与 design「无文件级环」一致）；`src/App.tsx` 的动态 `import('./pages/...')` 不在受限目录。整个 lint 约 3.0 s，不需 `maxDepth`。
  - **PR Boundary 偏离（记录）**：4 个文件 + tasks.md 勾选，非 issue 写的 3 个——`ci.yml:587/:630` 与 `infra/docker/Dockerfile.app:9` 都 `pnpm install --frozen-lockfile`，`apps/frontend/pnpm-lock.yaml` 必须同 PR（实测 +1142/-0 行，既有条目不变；`@unrs/resolver-binding-linux-x64-gnu` 已在 lockfile）；large-file-guard 默认排除 `pnpm-lock.yaml`。
  - **CI 步骤原文与位置**：`- name: Lint frontend` / `run: cd apps/frontend && pnpm lint`（裸 `pnpm`，该 job 用 pnpm/action-setup；`test_ci_shard_tests.py:420` 只允许 `frontend-e2e` 用 corepack）；一律紧贴 `Build and test frontend`（~:592）之前；无论 7.2/7.5 谁先合，终态顺序 `Typecheck frontend` → `Typecheck frontend tooling` → `Check generated API types`（7.5）→ `Lint frontend` → `Build and test frontend`，后合者按此解冲突；`:574-576` job 注释补 "lint"。`frontend: apps/frontend/**` 过滤器（ci.yml:197）覆盖 config/package.json/lockfile；`test_ci_shard_tests.py:417` 子串匹配无步数断言。
  - **selector**：无 Python 改动、无需路由；`scripts/select_ci_tests.py` 无读取 `apps/frontend/package.json` 的规则。
  - **不改**：`apps/frontend/AGENTS.md`（:93-102 命令清单无 lint，归 9.1）与 `docs/VALIDATION.md`，只在偏离记录列为已知过时，避免打开 docs lane。
  - **残余风险（记录）**：纯类型导入环检测不到；核心 `no-restricted-imports` 不查动态 `import()`；`frontend-e2e` 浏览器缓存 key 含 `hashFiles(pnpm-lock.yaml)`，本 PR 与合并后 master 各一次缓存未命中。

  **Suggested fixture level:** compact - 工具链配置，无运行时代码。

  **Minimal mergeable slice:** atomic - 规则、脚本与 CI 步骤缺一则门不存在。

- [ ] 7.3 `lib/constants.ts` 状态类型改为生成别名，`FAILED_STATUSES` 单一常量（成员不变），两个 monitoring 组件改用。

  Depends on: 无

  Verify：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`；负向演练：临时给 `openapi/nhms.v1.yaml` 的 `PipelineStage.display_status` enum（约 :5719；孪生 `status` 约 :5729 一起加）加一个值，`corepack pnpm generate:api` 后 `corepack pnpm typecheck` 应恰好在 `constants.ts` 的 `STATUS_COLORS` 与 `StageCard.tsx` 的 `statusIcons` 两处 `Record<PipelineStatus, …>` 报缺 key，记录后 `git checkout -- openapi/nhms.v1.yaml apps/frontend/src/api/types.ts` 撤销（PR 记录）；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（fixture review 2026-10-17）：

  - **alias 精确路径**：`export type PipelineStatus = components['schemas']['PipelineStage']['display_status']`（`api/types.ts:1835`；**不是**同名的 `components['schemas']['PipelineStatus']`——那是 cycle 对象类型 `:1792`）；`export type JobStatus = components['schemas']['PipelineJob']['status']`（`:1859`；`BasinResult['status']` `:1818` 成员相同，design Context 引的行号指向后者，等价）。导入写法 `import type { components } from '@/api/types'`（先例 `lib/mvtLayerMetadata.ts:2`、`lib/hydroMet/riverForecast.ts:3`；`api/types.ts` 无 import，不成环）。成员对比：`PipelineStatus` 手写 6 == 生成 6；`JobStatus` 手写 11 == 生成 11（仅顺序不同，union 无影响）。导出名 `PipelineStatus`/`JobStatus`、`STATUS_COLORS`、`STAGE_NAMES` 不变，消费方零改动；`StageCard.tsx:26` 的 `as PipelineStatus` 变冗余但保留。
  - **`FAILED_STATUSES`**：`export const FAILED_STATUSES: ReadonlySet<string> = new Set(['failed', 'submission_failed', 'permanently_failed', 'cancelled', 'partially_failed'] as const satisfies readonly JobStatus[])`——元素类型必须是 `JobStatus`（`submission_failed`/`permanently_failed` 不在 `PipelineStatus`），外层 `ReadonlySet<string>` 因 `JobsTable.tsx:44` 的 `statusClass(status: string)` 调 `.has(status)`（不收窄其参数，diff 只改常量来源）。用 Set 不用数组。
  - **两个组件**：删 `BasinFailures.tsx:19` 与 `JobsTable.tsx:39` 的本地 `const failedStatuses = new Set(...)`（模块内 const、未导出、无外部引用，不留 deprecated alias），改 `import { FAILED_STATUSES } from '@/lib/constants'`；三处 `.has` 调用点（`BasinFailures.tsx:29`、`JobsTable.tsx:46`、`:274`）只改标识符。`retryableStatuses`（`JobsTable.tsx:40`，`JobsTable.test.tsx:226` 钉住）与 `activeStatuses`（`:41`）不动；不给 hydro `RunStatus`（`types.ts:1696`）加 alias。
  - **spec「exhaustive switch」读作「exhaustive `Record<PipelineStatus, …>`」**：代码里无 status 的穷举 `switch`，穷举点只有 `constants.ts:31` `STATUS_COLORS` 与 `StageCard.tsx:12` `statusIcons`；`JobStatus` 无穷举消费者（`JobFilters.tsx:17` 是数组、`stores/monitoring.ts:45` 可选字段）。
  - **与 2.2 的关系**：2.2 只给 `PipelineJob`/`BasinResult` 的 job enum 加 `reservation_lost`，不碰 `PipelineStage.display_status`；合并顺序无关、无 typecheck 断点（`JobStatus` 多一成员只让下拉框少一个选项）。剩余风险（属 2.2 fixture）：yaml 里 job enum 有两份内联（`BasinResult` :5606、`PipelineJob` :5762），2.2 必须两处同改。
  - **测试**：无测试直接 import constants 的集合，union 不变，vitest 零改动。
  - **PR Boundary**：三个代码文件 + tasks.md 7.3 勾选（显式放行；因此跑 openspec validate）。CI 只走 `frontend` filter，不登记 selector。


  **Suggested fixture level:** compact - 类型别名与常量去重。

  **Minimal mergeable slice:** atomic - 别名与消费点同 PR。

- [ ] 7.4 API 错误单一归一化：三个本地归一化器委托 `getApiErrorMessage` 或删除，`apiErrorCode` 改为解析共享 `ApiErrorShape`；`useNationalBasinGeo` 加错误载体（`NationalBasinGeoState.error`，失败不缓存）；`lib/mvtLayerMetadata.ts` 的无调用方 loader 删除；新增 vitest 断言。

  Depends on: 7.1（仅后继：`settledError` 在 bootstrap 模块；首刀的两个站点不碰 bootstrap，无前置）

  Verify：`corepack pnpm test`（新增 `useNationalBasinGeo` 失败路径用例 + absent-data 渲染断言；loader 删除后 `git grep` 无引用）；mocked-regression lane `corepack pnpm run test:e2e:mocked-regression` 期望值不变。

  首刀 Verify：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`，失败路径用例通过，`git grep -n fetchLayerCatalogMetadata` 为空；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（首刀 #2955，fixture review 2026-10-17；level 保持 expanded，触发项是模块级共享状态 `cached`/`inflight` 的失败语义改变，不是 `unwrapApiData`/`getApi` 契约——该 hook 用裸 `fetch` 读 `public/geo/national-basin-domain.geojson` 静态文件、无 envelope；完整 risk-contract block 见 design「D6 (7.4 首刀)」）：

  - **改动面**：`apps/frontend/src/pages/m11/useNationalBasinGeo.ts`（`fetchOne` :22-31、`loadNationalBasinGeo` :33-44、模块级 `cached`/`inflight` :19-20、hook :50-73）；`apps/frontend/src/lib/mvtLayerMetadata.ts` 删 `fetchLayerCatalogMetadata`（:49-60）及随之死掉的 `isLayerRecord`（:100-102）与 `:1` 的 `apiFetch` import（`noUnusedLocals` 会打红），文件保留、其它导出不动（`M11MapLibreSurface.tsx:71`、`m11MapBuilders.ts:8-13`、`M11MapLibreSurfaceHoverOrder.test.tsx:12` 在用）；用例并入现有 `pages/m11/__tests__/useNationalBasinGeo.test.tsx`（`resetModules` + `stubGlobal('fetch')` 模式），不新建文件。
  - **新增**：`NationalBasinGeoState.error: string | null`，初值 null，只放 hook `useState`，绝不模块级；失败 = fetch reject、`!response.ok`、JSON 解析失败、`features` 不是数组；错误文案固定：非 2xx → `new Error(\`national basin domain HTTP ${status}\`)`，malformed（含非 JSON body，统一包成同一 Error）→ `new Error('national basin domain payload malformed')`，网络 reject → `getApiErrorMessage(err, '全国流域边界加载失败')`（`api/response.ts:11`，取 `Error.message`）；失败时 `cached` 保持 null、`inflight` settle 时在共享 promise 链上清为 null（不在实例回调里；`cancelled` 标志只丢结果）；重试开始时 `error` 清 null 并 `setLoading(true)` 同处；成功后 `error` 为 null。不加 AbortController（`inflight` 多消费者共享）；单一固定 URL，不引入缓存键。
  - **必须保持**：成功路径不变（一次 fetch、river 永不 fetch、`river: null`、会话内缓存；现有用例 :9-26 零改动通过）；首帧 `loading = active && !cached`；inactive 返回 `{domain:null, river:null, loading:false, error:null}`；失败时 `domain`/`river` 为 null、`loading` 收敛 false（改动前的 absent 形状），`OverviewPage.tsx:424` `withStaticBasinBboxes(basins, null)` 原样返回、`:715` `boundaryLoading` 为 false；OverviewPage 不读 `error`、不加渲染分支（D6、spec）。接受的差异：失败不再缓存，之后的挂载/再激活以 `loading=true` 重试（与首载同样的瞬态）。
  - **必需证据**：(a) fetch → 500 ⇒ absent 形状 + `error === "national basin domain HTTP 500"`；(b) 200 + `{"type":"FeatureCollection","features":null}` ⇒ absent 形状 + `error === "national basin domain payload malformed"`；(c) 200 + 非 JSON body ⇒ 同 (b)；(d) 不缓存：同一模块实例内先 500 再 200（`rerender` 把 `active` true→false→true，或 unmount 后再 `renderHook`、中间不 `resetModules`）⇒ fetch 调用 2 次、domain 有值、`error` null；(e) 现有成功用例零改动；(f) `git grep -n fetchLayerCatalogMetadata` 为空。absent-data oracle 是 `src/lib/__tests__/m11StaticBasinFallback.test.ts:79-81`、`:102-105`，不改。
  - **兄弟面**：并发消费者共享 `inflight`（都拿到同一失败、都不缓存、无 unhandled rejection）；11 个 `OverviewPage*.test.tsx` 与 `App*.test.tsx` 既不 stub fetch 也不 mock 该 hook（改后每次挂载重试，无断言 fetch 次数）；mocked-regression lane 的 `page.route` 全在 `/api/` 下，`/geo/` 由 vite dev 从 `public/geo` 提供，期望不变。CI：vitest 全量在 `frontend-build` 跑，不登记 selector。
  - **Non-goals**：渲染 `error`；AbortController；逐 feature 校验；三个本地归一化器与 `apiErrorCode`（#2956）；把 hook 挪到 `unwrapApiData`。PR Boundary：两个源文件 + 一个既有测试文件；不勾 tasks.md（整 task 的 Verify 与 7.4 复选框归后继 #2956）。

  钉死的契约（7.4 后继 #2956，fixture review 2026-10-17）：

  - **方向修正**：issue/spec 写的「三个归一化器变薄包装或删除」在 master 上大部分已成立，且两个外层是脱敏边界，变「薄」会泄露后端字符串。钉死：`getSafeModelAssetErrorMessage`（`stores/modelAssets.ts:241-245`，已委托 `getApiErrorMessage` 后把除 `MODEL_ASSET_DETAIL_IDENTITY_MISMATCH` 外的一切掩回 fallback；oracle `stores/__tests__/modelAssets.test.ts:522,532,542,1042`）**no-op，函数体逐字节不动**；`settledError`（7.1 后在 `stores/hydroMetBootstrap.ts`，已委托并加 `sanitizeHydroMetMessage` URI 脱敏；单一调用方；oracle `pages/hydroMet/__tests__/bootstrap.test.ts:487-499`）**no-op，不内联**；`safeM11ErrorMessage`（`stores/overviewDataRequestPlan.ts:14-16`，函数体 `` `${label}: ${fallback}` ``、不接收 error、是 M11 有意丢弃拒绝原因的标签掩码，design D2；7 个钉死字符串 `stores/__tests__/overviewDataShapeGuards.test.ts:79,80,92,110,149,175,176`）**保持代码不动**，spec/D6 文字改为「M11 label mask，不消费 error」并把它从归一化器名单移除（spec 原文「four」实列三名，一并修正）。
  - **`apiErrorCode`（`stores/overviewDataStore.ts:170-176`）无运行时变化**：在 :5 既有 `@/api/response` import 加 `type ApiErrorShape`；函数体钉为 `if (!error || typeof error !== 'object') return null; const code = (error as ApiErrorShape).error?.code; return typeof code === 'string' ? code : null`；保持局部、不导出、不挪进 `response.ts`（单一调用方 :210；导出会诱使与精度不同的 `hydroMetStationSeriesErrorCode` 合并）；保留运行时 `typeof === 'string'` 守卫；精度只认 `error.error.code`，无顶层 `code` 回落。`ApiErrorShape` 是手写类型（`response.ts:1-9`），**不得**别名到生成的 `components['schemas']['ErrorResponse']`（后者字段必填，`getApiErrorMessage` 依赖宽松解析）；`check:api-types` 不受影响。
  - **唯一绕过 envelope 的站点**：`overviewDataStore.ts:336-338` 的 `apiFetch` fallback `if (!response.ok) throw new Error('获取图层列表失败')` 改为 `throw new Error(getApiErrorMessage(await response.json().catch(() => null), '获取图层列表失败'))`（非 JSON/空体回落原字符串；标准 envelope 给 `error.message`）；UI 不变：两个消费者（:571-585 经 `bootstrapFailureWord`/`safeM11ErrorMessage`，:665-666 经 `settledValue` 发 `label: 暂不可用`）都丢弃 message；`overviewDataShapeGuards.test.ts:101` 仍绿。**范围外**：`components/map/m11MvtRetryProtocol.ts:64,77-80`（MapLibre 瓦片传输，抛 `AJAXError`，改会改重试语义）、`useNationalBasinGeo.ts:24`（首刀）、`api/base.ts:11`（`apiFetch` 实现本身）。**已合规不碰**：JobsTable:202（也在 #2954 文件集）、LogModal、TrendPanel、MonitoringPage、forecast.ts、monitoring.ts、stationSeries/riverForecast（经 `formatHydroMetStationSeriesMessage`）、hydroMetProductData:80、stationLayerData:276。不新建共享 helper（`sanitizeHydroMetMessage(getApiErrorMessage(x, f), f)` 六处重复是范围蔓延）。
  - **新增 `src/api/__tests__/response.test.ts`**：`getApiErrorMessage` 今日无直接单测；按输入表做特征测试、不改实现：`null`/`undefined`/`''`/`0`/`false` → fallback；`Error('x')` → `'x'`、`Error('')` → fallback；网络失败（openapi-fetch 重抛 `TypeError`）→ `.message`；AbortError `DOMException` → `.message`；非 JSON 错误体（openapi-fetch 交出原始文本串）→ 该串（`String(e)`）；后端 envelope `{request_id, status:'error', error:{code,message,details}}`（`apps/api/errors.py:394-401`）→ `error.message`；`{error:{code}}` 无 message → 顶层 `message` 否则 fallback；`{detail:'Not found'}`（`apps/api/startup_wiring.py:87`，唯一非 envelope 响应）→ fallback；数字等原始值 → `String(e)`。vitest 在 `frontend-build` 全量跑，无需 selector 登记。
  - **整 task 的 Verify 由后继承担，7.4 复选框由后继勾**（首刀不勾）：absent-data 渲染断言以首刀钉的 oracle 为准（`src/lib/__tests__/m11StaticBasinFallback.test.ts:79-81,102-105`），不新增组件级测试；复跑首刀证据（`git grep -n fetchLayerCatalogMetadata` 为空、`pages/m11/__tests__/useNationalBasinGeo.test.tsx` 通过）。
  - **Verify（后继）**：`cd apps/frontend && corepack pnpm typecheck && corepack pnpm test`；`uv run ruff check .`；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`（spec/tasks 文字改动）；mocked-regression lane 以 CI `frontend-e2e` 4 分片为 oracle（本地 `playwright-core@1.59.1` 钉 chromium 1217、本机缓存只有 1243，需联网安装，不要求本地跑；该 lane 无 spec 断言 API 错误文案，只证明无其它回归，vitest 才是文案 oracle；`e2e/support/riverWindow.mocked.ts:374` 的 404 `PRECIP_CYCLE_NOT_MIRRORED` envelope 会经过 `apiErrorCode`）；若 #2953 已合并则加 `corepack pnpm lint`（`no-explicit-any` 排除 `as any`）；`check:api-types` 不需要。活性演练：`apiErrorCode` 返回 `null` → `stores/__tests__/overviewDataSourceSelection.test.ts:365-378` 与 `pages/__tests__/OverviewPagePrecipOverlay.test.tsx:219,571` 红（`not_mirrored` 变 `error`）；model-asset 掩码改为返回 `rawMessage` → `modelAssets.test.ts:522,532,542,1042` 红。
  - **级别降为 compact**（共享状态风险已在首刀；本片运行时增量只是一个 fallback 的 `Error.message` 且无 UI 显示；掩码函数由 no-op pin 与既有测试围住）；若 diff 触及三个包装函数体任一，回到 expanded。席位：`correctness+test-evidence`。
  - **冲突与顺序**：依赖 #2952（`settledError` 路径）；#2955 文件集不重叠（仅 tasks.md 勾选归属）；#2954 碰 `constants.ts`/`BasinFailures.tsx`/`JobsTable.tsx`，本片不碰 JobsTable；#2957 只碰 `ci.yml`。PR 偏离记录须指向本 pin（issue body 仍写「三个归一化器变薄包装或删除」）。
  - **残余风险（记录，范围外）**：非 JSON 错误体（502 HTML/纯文本）经 `String(error)` 原样到达用户——monitoring toasts（`JobsTable.tsx:202`、`MonitoringPage.tsx:176,214`）未脱敏显示，代理错误页可能带内部主机名；design Non-Goals 禁止在此改，另立 issue。另有两个精度不同的错误码提取器（`lib/hydroMet/stationSeries.ts:106-116` 顶层 `code` 回落、`m11MvtRetryProtocol.ts:78-79`）不在四个命名函数内，不动。

  **Suggested fixture level:** expanded - 改 API 失败路径，project-profile 的 `unwrapApiData`/`getApi` 消费者契约要求 malformed-payload 测试。

  **Minimal mergeable slice:** `useNationalBasinGeo` 错误载体 + 删除死 loader + 对应 vitest - 可独立保绿；`apiErrorCode` 类型化 + `apiFetch` fallback 入 envelope + `getApiErrorMessage` 特征测试作为后继（两个脱敏包装与 M11 标签掩码保持不动）。

- [ ] 7.5 `frontend-build` job 加 `pnpm check:api-types` 步。

  Depends on: 无

  Verify：本 PR 的 Frontend Build job 日志出现该步并通过（正向证据，链接记入 PR）；本地负向演练（不推破坏提交）：`sed -i '' 's/River segment GeoJSON payload budget exceeded\./River segment GeoJSON payload budget exceeded (drill)./' openapi/nhms.v1.yaml` → `cd apps/frontend && corepack pnpm check:api-types` 非零（diff 落在 `types.ts` 约 :2743 的 `@description`）→ `git checkout -- openapi/nhms.v1.yaml` 后再跑为 0（PR 记录）；`actionlint .github/workflows/ci.yml` 本地预检（CI 的 `Workflow Lint` 为准）；`uv run ruff check .`；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（fixture review 2026-10-17，pass）：

  - **前置（阻塞，非 revise）**：改前在干净树上跑 `cd apps/frontend && corepack pnpm check:api-types` 必须退出 0（yaml 与 `types.ts` 最后一次同在 eb67c49de 改动，但该脚本从未在 CI 跑过）；非零则停下报告边界决策，本 PR 不重生成 `types.ts`。
  - **步骤原文与位置**：在 `Typecheck frontend tooling`（`ci.yml:590-591`）之后、`Build and test frontend`（:592）之前插入，与该 job 既有形式一致（裸 `pnpm`、`cd apps/frontend &&`、无 `working-directory`、无 `continue-on-error`；该 job 不用 corepack——`test_ci_shard_tests.py:420` 断言只有 `frontend-e2e` 用 `corepack prepare`）：
    ```yaml
          - name: Check generated API types
            run: cd apps/frontend && pnpm check:api-types
    ```
    同文件 :574-576 的 job 注释（"typecheck, build, vitest and the bundle check only ... about 2m40s"）补一句生成类型检查；`timeout-minutes: 10` 不动。
  - **用仓库脚本**：`check:api-types`（`package.json:23`）生成到 `/tmp/nhms-api-types.ts` 再 diff，不写回树，后续 build/test 不受影响；不另拼 `generate:api` + `git diff`。生成器 `openapi-typescript@7.13.0` 已在 lockfile。
  - **不改 filter**：`openapi/**` 已在 `frontend` filter（`ci.yml:199`），且 `tests/test_select_ci_tests.py:13170-13175` `_FRONTEND_LANE_SWITCH_ENTRIES` 钉住；只改 yaml 不重生成的 PR 本来就会起 Frontend Build，无 PR Boundary 偏离。不加进 `frontend-e2e`；不加 `pnpm lint`（7.2）。
  - **钉住的字面量不受影响**：`test_ci_shard_tests.py:417` 用子串 `in` 比四条命令、无步数断言；`:413-414` 禁 playwright/`strategy`；`test_select_ci_tests.py` 只拿 `"\n  frontend-build:"` 做切片边界。改 `ci.yml` 的 selector 路由（`:6730-6748`）会选中 `test_ci_shard_tests.py` 等，PR 定向 lane 自然覆盖 pin，不加 selector 行。
  - **文档**：不改 `docs/VALIDATION.md:572-573`（会越出「仅 CI yaml 一行」、起 docs lane 与文档门）——在 PR 偏离记录里记为已知过时清单 + follow-up；CLAUDE.md 只列 job 名不列步骤，无需重生成。PR Boundary：`ci.yml` 一步（+ 一行注释）+ tasks.md 7.5 勾选。

  **Suggested fixture level:** none - 一行 CI 配置。

  **Minimal mergeable slice:** atomic - 单行。

## 8. 治理报告与 baseline（spec: entropy-automation、governance-entropy-baseline）

- [ ] 8.1 `check_stale_routes.py` 跳过 `openspec/changes/archive/`，`skipped_path_families` 加一条作用域字符串；更新 `tests/test_entropy_audit_*` 对应用例。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py tests/test_entropy_audit_retired_paths.py tests/test_entropy_audit_route_authority_lists.py tests/test_entropy_audit_route_authority_caching.py`；`audit_repo_entropy.py --format json` 的 `stale-display-route-token` 计数下降且无 archive 路径（PR 记录前后计数）。

  钉死的契约（fixture review 2026-10-17）：

  - **跳过放在哪一层**：只在 `check_stale_routes.py` `_check_stale_route_tokens` 的循环里、`rel = _rel(root, path)` 之后，用 `rel.startswith("openspec/changes/archive/")` 提前 `continue`（带尾斜杠、只看仓库根下该前缀、不匹配任意 `archive/` 或 `docs/archived/`；`_rel` 已是 POSIX，不再规范化；作用于该前缀下全部被扫描的文本文件）。不放进 `_iter_text_files` / `SCAN_SKIP_*`（`constants.py`、`repo_files.py`），那条发现路径被 `check_paths_and_api.py`、`check_topology.py`、`check_env_and_tokens.py` 共用，放错层会一并改掉 `placeholder-path-token`（archive 下 79 条）与 topology/env 检查；不做共享 helper，不加进 `audit_repo_entropy.py` 的 facade 名单。`_path_is_route_authority_expanded_scope` 不动（其中 `openspec/changes/archive/` 分支变为不可达但保留）。
  - **`skipped_path_families`**：`scripts/governance/entropy_audit/report.py` 里是写死的 `sorted([...])` 字面量（现 8 项：`.git`、`.nhms-*`、`.venv`、`artifacts`、`caches`、`data`、`dist`、`node_modules`），新增 spec 原文字符串 `openspec/changes/archive (stale-display-route-token only)`；测试断言该字符串 `count(...) == 1` 且原 8 项仍在。消费方只有 `write_entropy_baseline.py` 原样透传；无测试钉长度或整表。
  - **允许的测试改动（三个既有用例会变红）**：`tests/test_entropy_audit_route_authority_lists.py` 的 `test_archived_openspec_expanded_route_token_without_marker_remains_budget_counted` 反转为 spec 的 "Archived change is not a finding"；同文件 `test_complete_archive_marker_allowlists_archived_openspec_expanded_route_token` 删除（`docs/archived/` 的 marker 路径已由同文件其它用例覆盖）；`tests/test_entropy_audit_route_authority_caching.py` 的 `test_route_authority_current_repo_m26_archive_evidence_uses_complete_marker_allowlist` 改写为「真实仓库中没有任何 `stale-display-route-token` finding 的 `evidence_path` 以 `openspec/changes/archive/` 开头」（即 PR 的「无 archive 路径」证据）。
  - **正负用例用同一行 legacy token**（活动 `openspec/changes/<x>/` 只在 legacy scope，`/forecast` 等 expanded alias 永远 0 条、假绿）：例如 `"Current route link ?next=/hydro-met.\n"` 分别写到 `openspec/changes/archive/m26/tasks.md`（期望 0 条）与 `openspec/changes/m26/tasks.md`（期望 1 条，`evidence_path` 等于该路径）。
  - **前后计数（2026-10-17 实测，PR 记录）**：`stale-display-route-token` 709 → 123；其中 archive 路径 586 → 0（317 unallowlisted + 269 allowlisted）；stale 的 `budget_counted` 317 → 0；`metadata.finding_count` 834 → 248；archive 下 `placeholder-path-token` 79 → 79（Out of Scope 保留）。
  - **不碰**：`.entropy-baseline/latest.json`（8.2 刷新）、`docs/governance/entropy-audit-2026-10-16.md`、selector 不需改：`check_stale_routes.py` 与 `report.py` 已路由到全部 `ENTROPY_AUDIT_TESTS` 分区（前提：不新增模块文件）。8.2 不会被提前打红：改后 occurrence 123 ≥ 10 仍为 `spread_risk == "high"`；`openspec/archive` 模块靠 79 条 placeholder finding 留在 heatmap。
  - **PR Boundary 偏离**：issue 写「一个谓词 + 测试」，实为两个脚本（`check_stale_routes.py` skip + `report.py` 字面量）+ 四个测试文件 + `docs/governance/entropy-report.example.md` 的 `skipped_path_families` 列表同步加入第 9 项（无测试读取该文件，8.2 两片都不碰它，所以归 8.1）。
  - **计数归因修正**：issue/design Context 说「665 条 finding 落在 archive，是 stale-route 造成的」不准确：665 = 586 stale + 79 placeholder；8.1 只去掉 586 条。

  **Suggested fixture level:** compact - 一个 path-family 谓词。

  **Minimal mergeable slice:** atomic - 谓词与测试同 PR。

- [ ] 8.2 `tests/test_entropy_audit_baseline_writer_summary.py` 删除对 tracked 计数的两条字面量断言、保留 HEAD inventory 比较并加 tracked 断言（正整数、`total_modules == len(modules)`）；随后 `uv run python scripts/governance/write_entropy_baseline.py` 刷新 `latest.json` 并提交归档快照。

  Depends on: 8.1

  Verify：`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 在刷新前后均通过；刷新后临时新增一个 `tests/test_zz_tmp.py` 确认仍通过后删除；governance workflow 的"baseline not written"步骤在 PR 上通过。

  首刀 Verify：`uv run pytest -q tests/test_entropy_audit_baseline_writer_summary.py` 在未刷新的树上通过。

  钉死的契约（fixture review 2026-10-17）：

  - **首刀（#2959）只改一个测试文件**：删除 `tests/test_entropy_audit_baseline_writer_summary.py` 对 tracked `latest.json` 的两条字面量断言（`total_test_files == 247`、`total_instruction_files == 3`），是删除不是放宽（不改成区间、`>=` 或缺文件即 skip；tracked 文件缺失应失败）。替换断言：整文件加载 `tracked = json.loads(BASELINE.read_text(...))`、`tracked_v1_summary = tracked["summary"]`；对 `total_test_files`、`total_instruction_files` 断言 `type(v) is int and v > 0`（`isinstance` 会放过 `True`）；断言 `tracked_v1_summary["total_modules"] == len(tracked["modules"])`（`modules` 是顶层 dict，writer 以 `len(modules)` 计）。保留 fresh summary 与 live inventory 的比较。**不**对 tracked 文件调用 `_assert_required_baseline_fields`（它要求 `check_family_count`，未刷新的树上没有）。
  - **首刀活性证明**：临时把 tracked `latest.json` 的 `summary.total_modules` 改成不等于 `len(modules)` 的值，测试应变红，随后 `git checkout -- .entropy-baseline/latest.json` 还原。首刀不做 `test_zz_tmp.py` 演练（归后继）。selector 无需改（该测试已在 `ENTROPY_AUDIT_TESTS`，writer 脚本已路由到全部分区）。首刀与 8.1 无硬依赖、可并行；唯一硬序是首刀 → 后继。
  - **后继（#2960）操作顺序**：(1) 从 master 切净分支，`git status --porcelain` 必须为空（writer 的 `_assert_report_visible_worktree_clean` 拒绝任何 `.entropy-baseline/` 之外的脏/未跟踪路径，含本地 `openspec/config.yaml` 之类未跟踪文件）；(2) 跑一次 `uv run python scripts/governance/write_entropy_baseline.py`；(3) 原样提交 writer 产物——归档文件名由 writer 以本次运行的 `now`（`%Y-%m-%dT%H%M%SZ`）命名、字节等于旧 `latest.json`，**不要手工改名**（`repo-entropy-audit` skill 文档说的「用旧快照自身时间戳」与 writer 实现不符，仓库先例 `.entropy-baseline/2026-06-12T160245Z.json` 内 `timestamp` 为 `15:33:06Z`；skill 文档漂移记为 Note，不在本 change 修）；(4) 勾 tasks.md 复选框（是对「仅 `.entropy-baseline/`」边界的偏离，PR 记录）；(5) `test_zz_tmp.py` 演练必须 `git add`（不提交）后再跑 pytest，否则 `git ls-files` 口径看不见该文件、通过是平凡的；演练完 `git rm --cached -f` 并删除。
  - **后继的 shape 变化**：刷新后 `summary` 新增 `check_family_count`，`version` 仍为 1；PR 记录。`branch` 字段记录当前分支名（`feat/...`），无断言要求等于 master，不阻塞。
  - **后继 CI 覆盖为零**：`.entropy-baseline/*.json` 不匹配 `backend` 过滤器任何模式，PR 不起 `unit-test-targeted`，合并后 master 的 `Unit Tests (full)` 也不跑；刷新前后本地 pytest 输出是唯一 oracle，贴进 PR body；接受该缺口并记录（不改 `ci.yml`）。额外证据：刷新后 `audit_repo_entropy.py --format json` 无任何 finding 的 `evidence_path` 落在 `.entropy-baseline/` 下。
  - **合并顺序**：8.1 → 8.2 首刀 → 9.1 → 8.2 后继（后继在 8.1 后能收进 `skipped_path_families` 新项与 709→123 的计数，在 9.1 后指令文件计数为 15 而非 6；提前跑仍绿、只是计数陈旧）。
  - **`docs/governance/entropy-report.example.md` 的第 9 项同步归 8.1**（该文件无测试读取，8.2 两片都不碰它）。

  **Suggested fixture level:** compact - 测试去钉死加一次 writer 运行。

  **Minimal mergeable slice:** 去字面量 - 可独立保绿；刷新 baseline 作为后继（同 PR 亦可）。

## 9. 指令文件与退役引用（spec: scoped-agent-instructions、legacy-store-reference-retirement）

- [ ] 9.1 九个模块（含 `services/precip`，不含 `packages/scheduler`）各一份 AGENTS.md（四节模板），引用与命令可运行。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py`；标题顺序检查与死引用检查两条 shell（钉死块原文）对九个文件输出零 `BAD-HEADINGS`/`DEAD*` 行（PR 记录）；每个文件 Focused Verification 里钉住的 pytest 命令本地各跑一次并记录 pass/skip 数；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`。

  钉死的契约（fixture review 2026-10-17）：

  - **标题契约**：`# <Module> Agent Instructions` + 前言（说明本文件为 `<dir>/` 对根 `AGENTS.md` 的 scoped 补充并链接 `openspec/glossary.md`），然后恰好四个 `##`、顺序固定：`## Required Reading`、`## Ownership Boundaries`、`## State And Mutation Fences`（或 `## Error Model`：`services/precip`（`errors.py`）与 `services/slurm_gateway`（`validation_errors.py`）用后者，其余七个用前者）、`## Focused Verification`。检查：`for f in <nine>; do h=$(grep '^## ' "$f" | tr '\n' '|'); [ "$h" = "## Required Reading|## Ownership Boundaries|## State And Mutation Fences|## Focused Verification|" ] || [ "$h" = "## Required Reading|## Ownership Boundaries|## Error Model|## Focused Verification|" ] || echo "BAD-HEADINGS $f"; done`。英文、每文件 ≤ 约 100 行（现有四个 79–103 行）、手写（现有四个无生成标记；`instructions/agents/*` 只生成根文件，`codex.md` 明言子树 AGENTS.md 按需手写）、反引号路径、不用 markdown 链接、fenced `uv run pytest -q ...` 块、文件末尾带现有四个共用的治理块（`uv run pytest -q tests/test_entropy_audit_*.py`、`openspec validate --all --strict --no-interactive`）。
  - **死引用一行 shell**（bash；对现有四个文件实测零死链）：`bash -c 'for f in "$@"; do d=$(dirname "$f"); grep -oE "(^|[^A-Za-z0-9_./-])(apps|packages|services|workers|scripts|tests|docs|openspec|schemas|db|infra|config|instructions|openapi|\.github)/[A-Za-z0-9_./*-]*" "$f" | sed -E "s/^[^A-Za-z.]//; s/[.,;:]+\$//" | sort -u | while read -r p; do compgen -G "$p" >/dev/null || echo "DEAD $f: $p"; done; grep -oE "\`[A-Za-z0-9_.-]+\.(py|md|sh|json|ya?ml|sql|toml)\`" "$f" | tr -d "\`" | sort -u | while read -r p; do [ "$p" = AGENTS.md ] || [ "$p" = CLAUDE.md ] || test -e "$d/$p" || echo "DEAD-BARE $f: $p"; done; grep -oE "\]\([^)#]+" "$f" | cut -c3- | grep -vE "^[a-z]+:" | while read -r p; do test -e "$d/$p" || echo "DEAD-LINK $f: $p"; done; done; echo scan-complete' _ <nine files>`（覆盖 fenced 块内的仓库根路径、glob 展开、裸反引号文件名对本目录、`](...)` 链接）。
  - **禁止 token（硬门禁）**：`scripts/AGENTS.md` 不得含旧端口字面量、不得把 node-22 与本机存储服务并置、不得出现连接串或副本类 token、不得把 `infra/env/display.env` 与 writer 字样并置（`scripts/` 是拓扑扫描根且 `.md` 被扫）——uv-only 与 node-22 3.12.7/禁 `uv sync` 规则链接根 `CLAUDE.md` 而不复述；五个 `workers/*/AGENTS.md` 不得含 `DIAGNOSTIC-ONLY`、`run_qhh_cycle`、`run_qhh_continuous`、`run_qhh_backend_smoke`、`create_qhh_shud_manifest`、`scripts/run_qhh_*`（`check_env_and_tokens.py` 扫 `workers/`，`qhh-diagnostic-token` 在 `HARD_GATE_CHECK_IDS`）。九个文件一律不写 `file:line`（`check_line_references.py` 不扫 markdown，行号只会烂掉；符号引用如 `app.py:create_app()` 可以）。不提未合并的兄弟任务（4.1 ruff 棘轮、1.2 `contracts/`、1.1 分层守卫、2.x `status_vocab`——路径不存在会被死引用扫描打红）；不把九个加进 `schema.py` 的 `SCOPED_AGENT_CONTEXT_CONFIGS`（PR 边界是九个 markdown）。
  - **Focused Verification 按表钉死**（排除环境门控/巨型套件：`test_e2e.py`（e2e）、`test_e2e_ifs.py`（grib）、`test_real_slurm_gateway.py`、`test_production_scheduler.py`、integration 标记的 `test_grid_snapshot_registration.py`）：`packages/common` → 无目录规则，用 selector-stdin 形式 `printf '%s\n' <changed path> | uv run python scripts/select_ci_tests.py`，示例 `tests/test_source_identity.py tests/test_safe_fs.py tests/test_safe_fs_lock.py tests/test_object_store_roots.py`；`services/precip` → `tests/test_precip_overlay.py tests/test_node27_mvt_prewarm.py tests/test_node27_raw_retention.py tests/test_api_contract.py tests/test_openapi_drift.py`；`workers/model_registry` → `tests/test_model_registration.py tests/test_model_registry_basin_versions.py tests/test_model_registry_list_basins.py tests/test_basins_registry_import.py`（全集 = selector 行 35 文件）；`workers/data_adapters` → `tests/test_gfs_adapter.py tests/test_ifs_adapter.py tests/test_era5_adapter.py tests/test_data_adapter_resolution.py tests/test_object_store_roots.py`；`workers/forcing_producer` → `tests/test_forcing_producer.py tests/test_forcing_producer_cli.py tests/test_production_met_validation.py tests/test_source_identity.py`；`workers/mapping_builder` → `tests/test_mapping_builder_*.py`（= `MAPPING_BUILDER_TESTS`）；`workers/grid_registry` → `tests/test_grid_drift_lifecycle.py tests/test_grid_stability_verification.py tests/test_shared_binding_eligibility.py tests/test_grid_registry_bbox.py`；`services/slurm_gateway` → `tests/test_gateway.py tests/test_slurm_gateway_app.py tests/test_slurm_route_contract.py tests/test_slurm_route_security_contract.py tests/test_gateway_reconcile_*.py`；`scripts` → `tests/test_select_ci_tests.py`（改 selector 时）否则 selector-stdin 形式。
  - **Required Reading（≥1 真实路径，均已 `test -e`）**：`packages/common` → `docs/governance/ROLE_BOUNDARY.md`、`docs/adr/0009-path-canonicalization-dereference-doctrine.md`、`docs/runbooks/tier-node27-timeseries-storage.md`；`services/precip` → `openspec/specs/precipitation-raster-overlay/spec.md`、`docs/runbooks/canonical-precip-mirror-backfill.md`；`workers/model_registry` → `openspec/specs/model-registration/spec.md`、`openspec/specs/basins-registry-import/spec.md`、`docs/runbooks/reingest-bootstrapped-basin.md`；`workers/data_adapters` → `openspec/specs/gfs-data-acquisition/spec.md`、`openspec/specs/ifs-data-adapter/spec.md`、`openspec/specs/era5-data-acquisition/spec.md`、`openspec/specs/adapter-cycle-hours/spec.md`；`workers/forcing_producer` → `docs/runbooks/object-store-forcing-series-read.md`、`openspec/specs/forcing-production/spec.md`、`openspec/specs/direct-grid-binding-artifact/spec.md`、`docs/adr/0006-forcing-csv-out-of-basins-package-identity.md`；`workers/mapping_builder` → `openspec/specs/element-grid-ownership-mapping/spec.md`、`openspec/specs/mapping-input-integrity/spec.md`、`openspec/specs/mapping-evidence-package/spec.md`；`workers/grid_registry` → `openspec/specs/grid-drift-lifecycle/spec.md`、`openspec/specs/grid-signature-stability-verification/spec.md`、`openspec/specs/source-shared-binding-eligibility/spec.md`；`services/slurm_gateway` → `docs/runbooks/slurm-gateway-preflight.md`、`openspec/specs/real-slurm-gateway-contract/spec.md`、`openspec/specs/slurm-gateway-node22-deployment/spec.md`、`docs/governance/ROLE_BOUNDARY.md`；`scripts` → `docs/runbooks/ci-test-routing.md`、`openspec/specs/ci-test-selector-routing/spec.md`、`openspec/specs/python-environment-truth/spec.md`、`scripts/diagnostic/qhh/README.md`。
  - **已核实**：docs 门（`tests/test_entropy_audit_report_contract.py`）只经硬门禁抓禁止 token，不检查九个文件的标题/链接/命令——所以上面两条 shell 是真正的验收。8.2 的 baseline 计数不会被 9.1 打红（`:77` 的 `== 3` 比的是 tracked `.entropy-baseline/latest.json`，`:78-85` 两侧都是 live）。CI 路由：`ci.yml` `backend` filter 含 `packages/**`、`services/**`、`workers/**`，`.md` 也匹配，八个文件会打开 backend lane（按 `PATH_TEST_RULES` 静态匹配：precip 6、model_registry 35、data_adapters 10（含 `test_production_scheduler.py`/`test_e2e.py`）、forcing_producer 16、mapping_builder 8、slurm_gateway 34；`packages/common`/`grid_registry` 无目录规则；`scripts/AGENTS.md` 只加硬门禁节点）——PR 描述说明 docs PR 会跑较重的定向后端 lane 的原因。
  - **Width exception: merged-tasks**（issue 已记录）；PR Boundary：九个新 markdown + tasks.md 9.1 勾选。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 九个文件共享一次作者通读与同一验证路径。

  **Width exception:** merged-tasks - 九个文件互相独立但没有代码、只有一条验证路径，拆成九个 issue 对交付没有价值（用户决定）。

- [ ] 9.2 `node27_pgdata_workload_plan.py` 移除 `LEGACY_HYPERTABLE` 与三处引用（SQL `IN` 列表、候选加载器父表集合、默认 `expected_relations`）；新增"字符串只出现在注释/docstring/`_REFUSAL_ADVICE`"测试；新增 catalog 无 legacy 表时"无缺失关系"用例与"legacy 父表候选被拒 `PLAN_UNRELATED_PARENT`"用例。允许的测试改动：`tests/test_node27_pgdata_workload.py` 中喂入 legacy 候选并期待成功的用例改为期待拒绝。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_node27_pgdata_workload_plan.py tests/test_node27_pgdata_workload.py tests/test_display_coverage_refresh.py`。

  钉死的契约（fixture review 2026-10-17）：

  - **模块路径**：`packages/common/node27_pgdata_workload_plan.py`（不是 `scripts/`）。常量 `LEGACY_HYPERTABLE = ("hydro", "river_timeseries_legacy")` 在 `:34`；三处引用：SQL `IN` 列表 `:68-69`（字面量 tuple，非常量引用）、loader 父表集合 `:368`（`{NARROW_HYPERTABLE, LEGACY_HYPERTABLE}`）、默认 `expected_relations` `:397`（字面量）；模块 docstring `:3-4` 也提到。`git grep LEGACY_HYPERTABLE` 只命中 `:34`/`:368`，无模块 import 它；`node27_timeseries_discovery.py` 的 `LEGACY_HYPERTABLES` 是另一符号，不动。
  - **改法**：常量直接删，不留 private 别名；`:368` 改为 `if parent != NARROW_HYPERTABLE:`（或单元素 set）；SQL 删 `:69` 并去掉 `:68` 行尾逗号，保留 `IN (('hydro', 'river_timeseries'))`（合法单元素 row constructor）；默认 `expected_relations` 变为 `("river_timeseries",)`；模块 docstring 改为只描述 narrow，可加一句「legacy 兄弟表已于 2026-09-19 drop（`db/migrations/000060_river_timeseries_contract.sql:97`）」。`_REFUSAL_ADVICE`（在 `packages/common/display_coverage.py:757`）逐字节不变，`display_coverage.py` 不动。不改 `tests/river_ts_plan_criteria.py:118` 的行号注释（line-reference 门不扫 `tests/`，且测试改动只限明文允许的一处）。
  - **「无缺失关系」scenario 的可观测口径**（模块不计算 missing relations，只回传 `expected_relations`，不新造输出字段）：(a) `load_candidate_relations` 只收到 `hypertable_name == "river_timeseries"` 的行时正常返回、不拒绝；(b) `evaluate_explain_json_plan(...)` 不传 `expected_relations` 时 `result["expected_relations"] == ["river_timeseries"]`，且 `inspect.signature(evaluate_explain_json_plan).parameters["expected_relations"].default == ("river_timeseries",)`，`not hasattr(module, "LEGACY_HYPERTABLE")`。
  - **SQL 断言直接钉在常量上**：`"river_timeseries_legacy" not in CANDIDATE_CHUNKS_SQL` 且 `re.findall(r"\('(\w+)', '(\w+)'\)", CANDIDATE_CHUNKS_SQL) == [("hydro", "river_timeseries")]`（AST 守卫只能指到 SQL 字符串起始行 `:48`）。
  - **允许的测试改动（唯一一处）**：`tests/test_node27_pgdata_workload.py` 的 `test_candidate_loader_binds_narrow_legacy_and_physical_compressed`（`:676`）：把 `:690` 的 `hypertable_name` 改为 `"river_timeseries"`、保留 `:719-720` 的 compressed/candidate 断言、函数名去掉 `legacy`；在 `:727-734` 的 `"other"` 用例旁新增 legacy 行的 `_assert_code(..., "PLAN_UNRELATED_PARENT")`（`PLAN_UNRELATED_PARENT` 唯一产生点是 `:368-369` 的集合检查，去掉 legacy 后无需再改代码）。`:426` 的 `assert "hydro.river_timeseries_legacy" not in sql` 是 forecast SQL capture，不动。
  - **字符串位置守卫**：写进 `tests/test_node27_pgdata_workload_plan.py`（已在 `NODE27_PGDATA_WORKLOAD_TESTS`，改 plan 模块即被 stop 规则选中，`scripts/select_ci_tests.py:4493`；不新建文件、不加 additive loop、Verify 不加 `tests/test_select_ci_tests.py`）。用 AST 不用逐行 regex：扫 `git ls-files` 下 `apps packages services workers scripts` 的 `*.py`（不含 `tests/`），检查每个 str `ast.Constant` 含 `river_timeseries_legacy` 的节点；放行 docstring（Module/ClassDef/FunctionDef/AsyncFunctionDef body 首个 `Expr` Constant）与 `packages/common/display_coverage.py` 中 `_REFUSAL_ADVICE` 那条 `Assign` value 子树；其余一律失败。防空跑：断言 advice 那处确实被扫到；自测：合成源码 `X = "hydro.river_timeseries_legacy"` 必须报违规，docstring 与注释样例通过。改动前命中：代码常量 3 处（plan `:34`、`:48`、`:397`）、docstring 3 处（plan `:1`、`river_ts_render.py:1`、`scripts/node27_refresh_coverage.py:2`）、advice 1 处；改动后代码命中为 0。显式剩余风险：以后在 `services/` 等目录加回该字符串时 PR 上选不中此守卫，靠 master 全量回归。
  - **oracle**：本地足够，不需新的 node-27 `psql` receipt（表已 drop 证据：migration 000060 `:97`、receipt `openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-19-i9-contract-window/README.md:91`、`openspec/glossary.md:44`）。issue Out of Scope「任何运行时行为变化」不违反：live catalog 上第二个 tuple 本来匹配不到行，拒绝分支只有单测能走到（D10）。`tests/test_display_coverage_refresh.py` 只在本地 Verify 跑（CI 只在 `display_coverage.py` 变更时选中）。

  **Suggested fixture level:** compact - 删除一个常量与三处引用，一个用例改向。

  **Minimal mergeable slice:** atomic - 常量与引用同删。

- [ ] 9.3 三个 runbook/ADR 与五个 active spec（spec 点名）的 legacy-store/routing-column 文本改过去时或加 DOC_STATUS 标记。

  Depends on: 无

  Verify：`uv run pytest -q tests/test_entropy_audit_report_contract.py tests/test_node27_timeseries_retention.py tests/test_node27_autopipeline_handoff.py tests/test_timescale_write_guard.py tests/test_node27_lifecycle_contract.py tests/test_node27_timeseries_compression.py tests/test_sql_shape_helpers.py tests/test_hydro_run_public_projection.py`；`openspec validate --specs --no-interactive`（非 strict：HEAD 245 pass；strict 下 `autopipe-parse-failure-residency` 与 `hypertable-compression` 原本 pass，改后必须仍 pass）；`openspec validate entropy-burndown-2026-10 --strict --no-interactive`；判别 grep（钉死块）每个命中要么落在完整 marker 区间内（用 `archive_status._complete_archive_status_marker_ranges` 核对），要么是钉死块列出的过去时 + glossary 指针行（PR 记录）。

  钉死的契约（fixture review 2026-10-17）：

  - **范围更正（spec 已改）**：「无 active runbook 指示运维读 `timeseries_store`」只对**河道**路由列 `hydro.hydro_run.timeseries_store` 与 `hydro.river_timeseries_legacy` 成立；tier runbook §4.12（`:6908-7349`，NOT YET EXECUTED）读的是 forcing 列 `met.forcing_version.timeseries_store`（gate SQL `:7052`、post-check `:7303`），该列在 node-27 仍存在（`000066` 未应用，`openspec/glossary.md:45`、ADR 0002 `:247-256`）——§4.12、runbook `:186-206` forcing 段与 ADR 0002「Amendment (2026-10-11)」一律不动。
  - **判别 grep**（无既有测试背书，level none 不新增守卫）：`git grep -nE "hydro_run\.timeseries_store|hydro\.hydro_run SET timeseries_store|river_timeseries_legacy" -- docs/runbooks docs/adr ':!docs/runbooks/receipts'`，HEAD 基线 18 处：runbook `:192, 3608, 3617, 3623, 3642, 5493, 5495, 6545, 6549, 6565, 6572, 6592, 6602, 6793`、ADR `:163, 167, 821`、service-bringup `:69`（`:6019` 的裸 `_legacy` 是 `000052` 的索引改名后缀，不算）。
  - **「完整 marker」按解析器口径**：`archive_status._archive_status_scope` 只接受裸 `section`；runbook `:5472`（`archive_scope: section, §4.6 through ...`）与 `:6535`（`..., §4.10.3 only`）现为不完整——改为 `archive_scope: section`，限定语挪进 `retained_for`。覆盖规则：块放在标题之后只到下一个任意级标题；块紧放在 N 级标题之前则延伸到下一个 ≤N 级标题；spec 里放在 `## Requirements` 之前覆盖到文件末尾（已在 scratch 上用 openspec validate 验证 marker 放 `## Requirements` 前 / requirement 体内 / `####` scenario 前均通过非 strict 且 strict 计数不变）。
  - **八文件动作**：(1) `docs/runbooks/tier-node27-timeseries-storage.md`：(a) 在 `:6299` `### 4.10` 之前加 section marker 覆盖整个 §4.10（到 `:6777` `### 4.11`）：`status: historical baseline` / `current_authority: db/migrations/000060_river_timeseries_contract.sql; openspec/glossary.md` / `superseded_by: none` / `status_since: 2026-09-19` / `archive_scope: section` / `retained_for: record of the executed I8 expand/re-forward window and the #2382 reparse runner (deleted 2026-09-18, bd06c6ddc)`（§4.10.2 `:6518-6524` 与 §4.10.6 `:6732-6775` 仍指示读 legacy 路由、跑已删脚本）；(b) 修 `:5472`、`:6535` 的 scope；(c) §4.11 `:6793-6800` 改过去时 + glossary 指针（fence 代码 `packages/common/timeseries_compression_fence.py:31,56-78` 仍映射 `_legacy` 兄弟，不可 marker 包裹）；(d) `:192`、`:3608-3645`、`:5493-5495` 已是过去时，只加 glossary 指针；(e) §4.12 与 `:186-206` 不动。(2) `docs/runbooks/production-ops/service-bringup.md` `:67-80` 已过去时：仅在 `:69` 加 `openspec/glossary.md` "legacy store" 指针，不声称 legacy cohort 已完全离开窗口。(3) `docs/adr/0002-…md` 已合规、不改（河道提及 `:163-169, 243, 332, 821-823` 皆过去时；`:255-297` 是 live forcing amendment；ADR 惯例是追加 amendment + section marker，不改正文）。(4) `openspec/specs/node27-window-admission/spec.md`：`## Requirements`（`:5`）前加 section marker，`status: historical baseline`、`status_since: 2026-09-19`（工具只在 `openspec/changes/archive/2026-09-15-node27-post-d12-reforward/tools/`）。(5) `node27-river-narrow-reparse-backfill/spec.md`：同上，`status_since: 2026-09-18`（`scripts/node27_river_narrow_reparse_backfill.py` 于 bd06c6ddc 删除）。(6) `autopipe-parse-failure-residency/spec.md`：`:10` requirement 末句（`legacy_store_refused` 分类）改过去时，说明自 #1993 起这类 run 与其它 `failed` run 一样被 watch/alert/re-alert（`tests/test_node27_parse_failure_residency_alert.py:623-634`、`docs/runbooks/production-ops/parse-failure-residency-alert.md:63`）；`:48-52` scenario 在其 `####` 标题前加 marker，`status_since: 2026-10-11`（6a843b84f）。(7) `hypertable-compression/spec.md` 只改 prose：(a) `:1244-1247` 从 skip code 列表删 `HANDOFF_APPLY_LEGACY_STORE_REFUSED` 并加过去时注（`tests/test_node27_autopipeline_handoff.py:1745` 钉其为普通失败，2026-10-11 移除）；(b) `:1504` 的 `hydro.river_timeseries_legacy` 示例标为 historical（2026-09-19 drop）+ glossary 指针，scenario 标题「table order is preserved across hypertables」逐字节不动（`tests/test_node27_timeseries_compression.py:639`）。(8) `forecast-api/spec.md` 只改 prose：`:269-272`、`:281`、`:306` 改过去时（`packages/common/river_ts_render.py:81` 按名拒绝 `"legacy"`，`tests/test_sql_shape_helpers.py:789` 钉住）；`:321` 不动（live allowlist 示例，`tests/test_hydro_run_public_projection.py`）。
  - **不动**：任何测试文件；标题 `### 4.3 Decompress procedure`（锚 `#43-decompress-procedure`，`tests/test_node27_autopipeline_handoff.py:1689`、`tests/test_timescale_write_guard.py:373`）；ADR 标题 `Revision 2026-08-11`（`tests/test_node27_timeseries_retention.py:2322`、`tests/test_node27_lifecycle_contract.py:104`）；runbook §8.x；runbook 内不新增 `RETENTION_*`/`COMPLETENESS_*`/`DRILL_*` 全大写 token（`tests/test_node27_timeseries_retention.py:336-350` 反向遍历）；9.2 的面（`display_coverage.py`、保留 legacy 名的 docstring）；`openspec/changes/archive/**`；`docs/governance/entropy-audit-2026-10-16.md`；`docs/governance/DOC_STATUS.md`（无逐文档登记表，解析器不读）。
  - **`status_since` 用事实日期而非今天**：2026-09-18（reparse 工具删除）、2026-09-19（`000060` 应用）、2026-10-11（forcing 拒绝词汇移除）；`status: historical baseline` + `superseded_by: none`（无替代程序；`superseded` 需真实目标）。
  - **CI 路由事实**：`ci.yml:81` 把 `docs/runbooks/tier-node27-timeseries-storage.md` 按名列入 backend filter，本 PR 会开定向后端 lane（selector 对该 runbook 选 16 项：hard-gate、`test_forecast_api`、`test_forecast_store_routing`、`test_node27_c4_production_acceptance{,_boundaries}`、`test_node27_lifecycle_contract`、`test_node27_pgdata_workload{,_io,_plan,_server_identity}`、`test_node27_timeseries_compression{,_budget,_runner_config,_wrappers}`、`test_node27_timeseries_lifecycle_lock`、`test_select_ci_tests`；service-bringup 选 11 项含 `test_node22_entrypoint_invariant`、`test_python_environment_truth`、`test_role_boundary_static`；ADR 不选；spec 只选 hard-gate）；`tests/test_node27_timeseries_retention.py` 读该 runbook但 selector 不选，故列入 Verify 本地跑。熵审计对八文件现有 0 finding、无 legacy-store token 检查，报告契约测试判别不了 9.3，判别靠上面的 grep。PR Boundary：八个文档 + tasks.md 9.3 勾选 + 本 spec scenario 已由 fixture review 改写。

  **Suggested fixture level:** none - 纯文档。

  **Minimal mergeable slice:** atomic - 同一术语的一次性清理。
