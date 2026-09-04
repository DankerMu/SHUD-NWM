## 1. 端到端打通一条路由（stop-gate）

- [x] 1.1 只做 `Basin` 一个 schema（5 字段、非 patch 占用、形状经 `stores/__tests__/overviewData.test.ts:34-39` 佐证、旧 yaml 里带 2 处 `nullable: true` 故同时验证空联合转换），走完整条链路：手写 schema 注入 + `GET /api/v1/basins` 的 200 响应改写 → `app.openapi()` → 重生成 `openapi/nhms.v1.yaml` → `pnpm generate:api` → `Basin` 出现在 `types.ts`。
  - 已完成（commit `da3e63c1`）。证据：`pytest tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_api_contract.py -q` **70 passed**；`git diff` 证明 `tests/test_openapi_31_contract.py` 与 `apps/api/routes/models.py` 均未改动；`pnpm check:api-types` exit 0；Redocly 1.25.13 exit 0（3 个 pre-existing warning，数量未变）；`types.ts` 的 `Basin` 与 `b97c16e2^` 逐字符相同。
  - patch 顺序已实测：把新 patch 分别放在注册位置与 `_patch_layer_metadata_openapi` **之前**两种排列，`SuccessEnvelope` 均存在且相等，`ApiSuccessEnvelope` 未泄漏，`allOf[0]` 均为 `$ref: SuccessEnvelope`。

## 2. (b) 配置面

- [x] 2.1 `pnpm add -D @types/node @types/geojson@7946.0.16`（版本对齐当前传递依赖，避免重复实例），提交 `pnpm-lock.yaml`（CI 用 `--frozen-lockfile`）。不新增 `types` 数组（会收窄默认包含范围），不新建 `tsconfig.node.json`（`src/__tests__/playwrightConfig.test.ts` 相对 import 了 `playwright.config.helpers`，拆 project 后仍在同一程序内，属 YAGNI）。
  - Required evidence：`pnpm exec tsc --noEmit -p tsconfig.app.json` 的 TS2307（`node:*`×8 / `geojson`×9）、TS2580（`process`×6）、TS2550（×7）四类归零，逐类计数前后对比贴 PR body。
- [x] 2.2 把 `@types/node` 从 `^26.4.1` 改钉 `^20`，对齐 CI 的 `node-version: "20"`（`ci.yml:157,391`；仓库无 `.nvmrc`/`engines`）。类型比运行时新会让测试用到 Node 20 上不存在的 API 而类型检查照过。改后须复验四类仍归零、`pnpm test` / `pnpm build` 仍绿、`pnpm install --frozen-lockfile` 仍同步；若 vite/vitest 的 optional peer 因此告警，记录实际影响再定去留。

## 3. (a) 未被 patch 占用的路由

- [x] 3.1 为以下路由补具名 200 响应：`/api/v1/basins`（1.1 完成）、`/api/v1/basins/{basin_id}/versions`、`/api/v1/basin-versions/{id}/river-segments`（列表/GeoJSON）、`.../river-segments/{segment_id}`（详情）、`/api/v1/models`、`/api/v1/models/{model_id}`、`POST /api/v1/models/{model_id}/preflight`、`POST .../lifecycle`、`/api/v1/runs`、`/api/v1/metrics/stage-duration`、`/api/v1/metrics/success-rate`、`.../forecast-series`。
  - **形状权威是当前 store / handler 实现**（见 design.md「Shape Oracle」）。`b97c16e2^:openapi/nhms.v1.yaml` 只作起草稿：逐个 schema 必须打开构造它的 store 函数、枚举实际写入的 key 集合，**不是**拿旧 yaml 做 diff。重点核对 `_model_public_projection`（`packages/common/model_registry.py:3601`，28 字段）、`store.list_runs` 行形状、`store.preflight_model_operation`（21 字段）、`store.forecast_series` / `_empty_forecast_response`（`forecast_store.py:3987`）。
  - **`required` 同样以运行时为准**，不得照抄旧 yaml。旧 yaml 的 `required` 是手写的，实测已知一处说谎：`RiverSeriesResponse.required` 含 `frequency_thresholds`（该字段要删）。判据是 store 函数是否**恒**写入该 key。
  - **已知必须偏离旧 yaml 的五处**（实测，见 proposal.md）：① 删 `HydroRun.product_quality`；② 删 `RiverSeriesResponse.frequency_thresholds`（`properties` 与 `required` **两处都改**）；③ 删 `SplicedForecastResponse.frequency_thresholds` —— 该字段在旧 yaml 里是 `{type: object, allOf: [$ref FloodFrequencyThresholds], nullable: true}`，**照抄会 `$ref` 到一个本次不补写的组件，造成悬空引用**；④ 删 `RunStatus` 的 `frequency_done` 枚举值。**合并后 task 7.4 实机复核推翻了本条原来的依据**（原文：「DB enum 共 11 值、从无此值」）——node-27 生产库有 12 值、含 `frequency_done`；它由 `b97c16e2` 从**已应用**的 `000003_enums.sql` 里追溯删除，而 PostgreSQL 无法 DROP enum 成员，所以生产上删不掉。删除动作本身仍正确，但依据是「该阶段已被 `b97c16e2` 主动退休、当前 `*.py` 零写入、live 零行、且 `RunStatus` 只被 `HydroRun.status` 响应体引用」，不是「DB 从来没有」。账本漂移已立单 #2048；⑤ 给 `SeriesSegment` 补 `variable`。发现其他差异同样以当前实现为准，并逐条记进 PR body。
  - `RunType` 无需偏离：旧 yaml 的 `['analysis','forecast','hindcast']` 与 `000045_hydro_run_type_hindcast.sql` 后的 DB enum 一致。
  - **`forecast-series` 特例**：200 是裸 `oneOf: [RiverSeriesResponse, SplicedForecastResponse]`，**没有 SuccessEnvelope**（handler 直接 return，不经 `_ok`）。不得套用其余 13 条的 `allOf` 组合。
  - **`_set_operation_response_schema` 只处理 GET**（`openapi_patching.py:1054` 硬编码 `.get("get")`），对两条 POST 路由会**静默 no-op**。给它加 `method` 参数或对这两条直写 `operation["responses"]`。
  - Required evidence：`uv run pytest tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_api_contract.py -q` 全绿；Redocly exit 0；**逐路由贴 `grep` 输出证明其 200 确实 `$ref` 到新具名 schema**（不是仍停在 FastAPI 默认的 `{type: object, additionalProperties: true}`）。
  - Non-goal：不改任何 handler 返回值构造，不加 `response_model=`。

- [x] 3.2 所有新增可空字段一律写 `anyOf: [X, {"type": "null"}]`。**禁止 `nullable: true`，也禁止 type 数组形式 `type: [T, "null"]`** —— 后者会顶穿 `tests/test_openapi_31_contract.py:125` 的 `_scalar_type_union_null_count(finalized) == BASELINE_NULLABLE_COUNT - 1`（该函数统计整篇 finalized 文档里 type 为含 `"null"` 列表的节点，不限 patch 注入）。复用 1.1 引入的 `_null_union` helper。
  - Required evidence：新增 schema 模块内 `grep -c "nullable"` 为 0、`grep` 无 type 数组空联合；`test_openapi_31_contract.py` 的 `BASELINE_NULLABLE_COUNT` 值与 :125/:126 两条断言的**等式关系**均未改动（`git diff` 证明）。

- [x] 3.3 **（3.2 的前提，不是收尾——先做）** 把新增的 patch 函数补进 `tests/test_openapi_31_contract.py:363-373` 的 `_pre_finalized_runtime_schema()` patch 元组。该元组当前硬编码 7 个函数、与 `apps/api/main.py:_patch_openapi_schema` 各自维护，**新 patch 不进元组则空联合断言对新 schema 完全失明** —— 也就是说 3.2"type 数组会顶穿 `:125`"这个约束，只有在 patch 进了元组之后才真正成立。
  - **第一条待补项是 `_patch_basin_registry_openapi`**（`da3e63c1` 引入，当前不在元组里；对 Basin 无害因为它无 `nullable` 且两个 `anyOf` 在 `:126` 等式两侧自平衡，但必须补上）。
  - 这是**扩大覆盖**，不是放松 oracle：`BASELINE_NULLABLE_COUNT` 的**值**必须保持 111，只允许元组变长。此任务预期产生非空的测试文件 diff。
  - Required evidence：元组包含全部新 patch 后 `uv run pytest tests/test_openapi_31_contract.py -q` 仍绿且 `BASELINE_NULLABLE_COUNT = 111` 未改。

- [x] 3.4 新增真实响应 ↔ OpenAPI 校验测试（仓库已有 `jsonschema>=4.23.0`，`pyproject.toml:41`）。当前**没有任何测试拿响应体校验 schema**：`test_openapi_drift.py` 只断言 yaml == 运行时 schema（两边同时说谎也绿），`test_api_contract.py` 逐字段手写断言、从不 import `jsonschema`。新测试加载 `openapi/nhms.v1.yaml`，对本次涉及的每条路由解析其 200 schema（有 envelope 的先解 `allOf`），用既有 fixture 响应做 `jsonschema.validate`。fixture 来源已核实齐备且**不需要真实 DB**：`tests/test_api_contract.py`（含 `forecast-series` @:987、`stage-duration` @:1010）与 `tests/test_monitoring_api.py`（`success-rate` @:2491）的 TestClient + stub 响应。
  - **`$ref` 解析**：取出的子 schema 内含 `$ref: '#/components/schemas/X'`，直接丢给 `jsonschema.validate` 解析不了。用 `{"allOf": [<子schema>], "components": doc["components"]}` 作为传入根（fragment-only 指针按传入根解析），或显式构造 `referencing.Registry`。
  - **必须先断言"不是默认体"**：逐路由断言解析出的 data schema 含 `$ref`。否则 operation 若仍是 FastAPI 默认的 `{type: object, additionalProperties: true}`，`jsonschema.validate(任意对象, 该 schema)` **恒真** —— "注入了孤儿组件但没改写 operation"这条完整的谎言路径会全绿通过，正是本任务要堵的缺口。
  - Required evidence：新测试对每条路由至少一个 fixture 通过；**mutation 逐路由做**（不是全局一次），每条路由删/改一个必填字段都能让它变红（贴输出）。
  - Non-goal：不覆盖本次范围外的路由。

## 4. (a) 已被 patch 占用的路由

- [x] 4.1 `GET /api/v1/met/stations`（`_patch_met_stations_list_openapi`）与 `GET /api/v1/queue/depth`（`_patch_pipeline_openapi`）：在既有 patch 函数内注入 `MetStation` / `GeoJsonPoint` / `MetStationPage` / `QueueDepth` 并改写各自 operation 的 200 组合，注入顺序须在既有改写**之后**或与之合并，避免被后置覆写。
  - Required evidence：`uv run pytest tests/test_openapi_drift.py tests/test_openapi_31_contract.py -q` 绿；yaml 里这两条路由的 200 确实 `$ref` 到新具名 schema（贴 `grep` 输出）。

## 5. 前端消费方与残留

- [x] 5.1 重生成 `types.ts`（`pnpm generate:api`），确认 **24 个新补写的 OpenAPI 具名 schema** 全部可解析（`ModelLifecycleRequest` 不补写、走 5.3 重指；`GeoJsonPoint` 因 `MetStation` 运行时无 `geom` 而不补写）。
  - **禁止注入孤儿组件**：每个新增 component 都必须被至少一条 operation 的响应 `$ref` 到。只塞 `components.schemas` 不改 operation 能让 tsc 变绿，但那是形状谎言（Redocly 因 `--skip-rule no-unused-components` 也拦不住）。
- [x] 5.2 `LineageResponse` / `ForcingVersion` / `QcResult` 三个改为前端本地 interface（在 `apps/frontend/src/lib/m11/overviewDataContracts.ts` 就地声明），替换 `components['schemas'][...]` 引用。`/api/v1/lineage/river-point` 后端从未实现，没有路由就不可能有 OpenAPI schema。
  - **不得改动 `overviewData.ts:1397-1410` 的 try/catch 与 `partialErrors` 行为** —— 删掉该调用会让 `'河段追溯暂不可用'` 这条 partialError 消失，等于改 UI。
  - Required evidence：`pnpm test` 绿；`git diff` 证明该调用与错误处理逐字未变。
- [x] 5.3 `ModelLifecycleRequest` → `ModelLifecyclePayload` 重指前先读 `types.ts:988-1000` 确认 `override_missing_active` 的 TS 可选性（Pydantic 的 required-with-default 在 TS 侧通常落成非可选，与旧的 `?:` 不同），据实处理。
- [x] 5.4 `StageDurationMetric` / `SuccessRateMetric`：两个 alias 当前无任何 importer（`TrendPanel.tsx` 走无类型 `client.GET`），但路由与形状都活着（`e2e/monitoring.mocked.spec.ts:110-118` 有精确 fixture）。两条路由都活着，**其 schema 无论如何都要补写**（受 5.1 反孤儿规则约束）；可选的只是前端那两个 alias 保留还是删除。**结论：保留。**测量阶段判断的“两个 alias 无 importer”是错的——`components/monitoring/TrendPanel.tsx:8` 确实 `import type` 了它们并在 `:25,:33,:43,:44` 使用，删除会是回归。
- [x] 5.5 清理 (c) 类下游噪音（TS7006 / TS2322 / TS2488 / TS18046 等）中未被 (a)(b) 自动消解的残留，直到 tsc 归零。
  - Required evidence：`cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json` **exit 0**；`pnpm test && pnpm build && pnpm check:api-types` 全绿。
  - Non-goal：不用 `any`/`@ts-ignore`/`@ts-expect-error` 兑换绿色；确需抑制的逐处在 PR body 记录理由。

## 6. 补门

- [x] 6.1 `apps/frontend/package.json` 增加 `"typecheck": "tsc --noEmit -p tsconfig.app.json"`。
- [x] 6.2 `.github/workflows/ci.yml` 的 `frontend-build` job 增加**独立命名 step**（如 `- name: Typecheck frontend`）执行 `pnpm typecheck`。
  - **顺序陷阱**：现有 step 是融合的一行 `pnpm install --frozen-lockfile && pnpm build && pnpm test`（`ci.yml:395-396`）。照字面"置于 `pnpm build` 之前"会把新 step 排到 `pnpm install` **之前**，那时没有 `node_modules`、`tsc` 根本不存在。正确做法：**先把 `pnpm install --frozen-lockfile` 拆成独立 step**，typecheck step 置于其后、build 之前。
  - Required evidence：一次真实 PR CI run 的 `frontend-build` job 里该 step 显示 **executed + passed**（非 skip、非 warning-only），贴 run 链接。
  - 已完成。证据：<https://github.com/DankerMu/SHUD-NWM/actions/runs/33838417298/job/100915577920> —— `Frontend Build` job 的 step 6 `Typecheck frontend` 为 `completed/success`（非 skip），排在 step 5 `Install frontend dependencies` 之后、step 7 `Build and test frontend` 之前，顺序陷阱已按拆分方案规避。

## 7. 证据与收尾

- [x] 7.1 PR body 列出 **33 个名字的逐名归宿表**：24 个 OpenAPI 具名（载体 · 路由 · 形状来源 store 函数 · 与旧 yaml 的差异）、1 个重指（`ModelLifecycleRequest`→`ModelLifecyclePayload`）、1 个因运行时无该字段而删（`GeoJsonPoint`）、3 个前端本地、4 个随死字段整支删除。24+1+1+3+4=33。
- [x] 7.6 为两条越界发现立 issue（只报不修）。① **已立单 #2038**——立单核查确认这是真实的脱敏绕过而非潜在风险（同一响应体内 `preflight.lineage.mesh_properties.source_path` 为 `[redacted]`、`model.mesh_properties_json.source_path` 为真实路径），并纠正了原观察两处：`/preflight` **不受影响**，但 `PUT .../active` 同样受影响。② **已立单 #2047**，但原始观察的**归因是反的**：文档「多出 `frequency_done`」这一半并非它们独有的错，同一个值在 live 生产库里**至今存在**（`db/migrations/` 相对生产的漂移才是根因，见 #2048）。立单核查另纠正两点——文档真正的缺陷是同时**多了**已退役的 `frequency_done`、**少了** `000013` 补入的 `pending`（冻在原始快照上的双向陈旧）；且 `forcing-copyback-backfill.md:66` 那条 SQL 在生产上**命中 4742 行**（全为 `published`），并非原观察所说的「恒不命中」，只有 `frequency_done` 那个析取项惰性。在全新迁移建的库上该字面量会报 `invalid input value for enum hydro.run_status`。原始观察：① `_model_public_projection`（`model_registry.py:3601-3622`）不 pop `mesh_properties_json`，而 `_model_asset_detail:3545` 会 pop —— lifecycle 响应会外泄原始 mesh properties JSON，属潜在路径/血缘泄漏；② `docs/spec/03_database_design.md:68`、`docs/appendices/C_database_schema_draft.md:28`、`docs/runbooks/forcing-copyback-backfill.md:66` 把 `frequency_done` 当作 `hydro.run_status` 合法成员，其中一处写进 `status IN (...)` 的 SQL、对该字面量恒不命中。
- [x] 7.2 记录 residual：新增 schema 是纯文档，**不做运行时校验**（与 `b97c16e2^` 之前的状态一致，非能力降级）；`response_model=` 升级另立 issue。
- [x] 7.3 为 `/api/v1/lineage/river-point` 后端路由缺失（生产静默降级）立 issue。已立单 **#2039**；立单时对公网实测确认 404，并查实修法是造功能而非注册路由（无血缘图存储，`docs/spec/04_api_design.md:227-251` 与前端 interface 两份契约互斥）。
- [x] 7.4 **（合并后补跑）** node-27 跑后端契约测试（`tests/test_api_contract.py` 与 3.4 的新校验测试按 CLAUDE.md 路由需真实 DB oracle 复核）。
  - 附带一行闭环删 `frequency_done` 的决定：`SELECT unnest(enum_range(NULL::hydro.run_status));`
  - **本任务当初写下的怀疑（「排除不了带外 `ALTER TYPE`」）方向对、机制猜错，而且它是本 issue 唯一拦住该错误的控制点。**
    实测：live 12 值、含 `frequency_done`；不是带外 `ADD VALUE`，是 `b97c16e2` 追溯改写了已应用的迁移文件。
    判据是中段序号——`frequency_done@7` 夹在 `parsed@6` 与 `published@8` 之间且为**整数**，而中段 `ADD VALUE`
    必产生分数（`pending@2.5` 即是）；尾部追加同样拿整数，故「整数即原始」不可一般化。
  - receipt（node-27 @ `d812def6`）：`uv run pytest tests/test_api_contract.py`
    `tests/test_openapi_response_conformance.py tests/test_openapi_drift.py tests/test_openapi_31_contract.py -q`
    → **149 passed in 28.67s**。
  - 牵出的账本漂移已立单 **#2048**（重头不是枚举：`hydro.hydro_run` 的 6 个 partial index 全部与账本背离，
    其中 4 个对 `services/tiles/mvt.py:1466` 的谓词不蕴含），PR #2037 上已补两条更正评论。
- [x] 7.5 PR body 记一句 `PUT /api/v1/models/{model_id}/active`：旧 yaml 里它同样 `$ref` 到 `ModelLifecycleResult`，但前端无 typed 消费方（`apps/frontend/src` 无任何 `client.PUT(`），不构成 tsc 缺口，故不在本次 14 条路由内 —— 代价是同一形状在文档里具名/匿名并存，显式取舍。

## Evidence Floor

- `cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json` exit 0
- `cd apps/frontend && pnpm test && pnpm build && pnpm check:api-types` 全绿
- `uv run pytest tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_api_contract.py -q` 绿
- `npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components` exit 0
- `uv run ruff check .` 绿
- `openspec validate frontend-typecheck-contract-restoration --strict --no-interactive` 绿
- 一次真实 PR CI run 证明 typecheck step executed + passed
- `BASELINE_NULLABLE_COUNT` 的**值**为 111 未变，且 `:125` `_scalar_type_union_null_count == BASELINE_NULLABLE_COUNT - 1` 与 `:126` `_anyof_with_null_branch_count == 1 + _pre_existing_anyof_null_count` 两条**等式关系**未被改写（`git diff` 证明）。对 `tests/test_openapi_31_contract.py` 允许且要求的唯一改动是 `_pre_finalized_runtime_schema()` 的 patch 元组变长（task 3.3）；3.4 新增独立测试文件不受此限。
- 3.4 的响应校验测试存在且带 mutation 验证
