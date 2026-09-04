## Risk Triage

- Fixture level: **expanded**（issue 建议 M；实测超出，理由见下）。触及 OpenAPI 契约面 + CI 门控面 + 前端构建配置三条风险轴。
- 规模偏离记录：issue #2022 估 **M**、列 23 个 schema。传递闭包实测为 **33 个**（`components.schemas` 内 `$ref` 闭包，10 个嵌套依赖同样缺失），旧 yaml 里对应 **1089 行** schema 定义、**87 个** `nullable: true` 节点需转 3.1 空联合，涉及 **14 条**后端路由。仍按单 PR 交付，因为 (b)+门单独交付**满足不了任何一条验收标准**（门在 (a) 绿之前必然红），issue 自己的"(b)+门 / (a) 契约恢复"拆法在验收层面不成立。

## Must-Preserve Behavior

1. **零运行时响应体变更**。所有 14 条路由的 handler 返回值构造不得改动一个字节。实测依据：`response_model=` 会按模型过滤字段（探针：声明 `{stage,depth}` 后 `EXTRA` 字段被丢弃），`responses={200:{"model":M}}` 与手写 patch 注入都不会。本次选后者。
2. **`BASELINE_NULLABLE_COUNT = 111` 不变**（`tests/test_openapi_31_contract.py:20`）。该计数只统计 3.0 风格 `nullable: true`（`_remove_nullable_keywords`，`apps/api/openapi_patching.py:105`，只对 `nullable` 键动手）。新增 schema 一律用 `anyOf: [X, {"type":"null"}]`。
   **禁止 type 数组形式**（`type: [T, "null"]`）：`tests/test_openapi_31_contract.py:125` 的 `_scalar_type_union_null_count(finalized) == BASELINE_NULLABLE_COUNT - 1` 统计的是**整篇 finalized 文档**里 type 为含 `"null"` 列表的节点，不限于 patch 注入的，任何新增 type 数组都会把它顶过 110 而变红。`anyOf` 形式安全，因为 :126 的 `_anyof_with_null_branch_count(finalized) == 1 + _pre_existing_anyof_null_count(pre_finalized)` 在等式两侧同时计入新节点、自动平衡。
3. **`LineageResponse` 消费方行为逐字不变**：仍调用不存在的路由、仍 404、仍被 try/catch 转成 `'河段追溯暂不可用'` 并进 `partialErrors`。删除该调用会改变 UI（partialError 消失），不在本次范围。
4. **既有 32 个 openapi 契约测试保持绿**，且不得放松任何断言或基线常数来换绿。
5. **Redocly 1.25.13 lint 保持 exit 0**（基线实测绿 / 3 warnings）。本次是 `b97c16e2` 以来最大的一次 yaml 增量。

## Seams Under Test

- `app.openapi()` → `openapi/nhms.v1.yaml`：由 `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` 全文档等值钉死。yaml 必须从运行时重生成，不能手改。
- `openapi/nhms.v1.yaml` → `apps/frontend/src/api/types.ts`：由 `pnpm check:api-types`（`openapi-typescript` 重生成后逐字节 diff）钉死。
- `types.ts` → 前端消费方：由新增的 `pnpm typecheck` 门钉死。**这是本次新建的、此前完全缺失的一环。**
- patch 函数 → operation 响应：`_set_operation_response_schema`（`openapi_patching.py:1079`，`da3e63c1` 后行号）与 `_patch_station_series_openapi` 的 `operation["responses"] = {...}` 直写，两种既有写法。

## Carrier Decision（为什么不是 Pydantic 模型）

`_patch_layer_metadata_openapi`（`openapi_patching.py:398-421`）会**主动 pop 掉**路由 `response_model=` 生成的具名组件（`LayerListResponse` / `LayerValidTimesResponse`）再内联手写 schema —— 这正是仅存两处 `response_model=` 的 schema 名在 yaml 里查无此名的原因。且 `/api/v1/met/stations`、`/api/v1/queue/depth`、`/api/v1/pipeline/*`、`/api/v1/runtime/config` 已被 patch 函数占用，路由侧声明会被 patch 后置覆写。

统一走手写注入还有两个决定性理由：
- **envelope 组合逐路由不同**。13 条是 `allOf: [SuccessEnvelope, {required:[data], properties:{data: X}}]`；但 `forecast-series` 是**裸 `oneOf: [RiverSeriesResponse, SplicedForecastResponse]`，没有 envelope**（`forecast.py:35` 的 handler 直接 return，不经 `_ok`）。Pydantic 载体可以用一个泛型 `SuccessEnvelope[T]` 覆盖那 13 条，所以"要造 14 个包装模型"并不成立；但 forecast-series 仍要单独处理，且下一条的 pop 问题无解。
- **patch 会 pop 掉路由生成的组件**（`_patch_layer_metadata_openapi` 实测 pop `LayerListResponse`(:403)、`ApiSuccessEnvelope`(:405)，再以 `SuccessEnvelope` 重注册(:408)）。已被 patch 占用的路由，路由侧声明会被后置覆写。统一走注入是唯一不用同时维护两套机制的选择。
- 旧 yaml 已是 JSON Schema 形式，可作为**起草稿**降低转写成本；但形状正确性以当前 store 实现为准，见下节。

## Shape Oracle（评审 P1-1 后修正）

**权威是当前 store / handler 实现。** `b97c16e2^:openapi/nhms.v1.yaml` 是从未被校验过的手工文档（`proposal.md` 已证），只作起草稿。实测双向漂移都存在：

- 多写（旧 yaml 有、今天没有）：`HydroRun.product_quality`、`RiverSeriesResponse.frequency_thresholds`、`RunStatus.frequency_done` —— 三者在 `apps/ packages/ services/ workers/` 与整个前端均零命中。
- 漏写（今天有、旧 yaml 没有）：`SeriesSegment.variable`（`packages/common/forecast_store.py:4031` 无条件写入）。

每个 schema 的字段集必须从构造它的 store 函数反推，重点是这几个大的：`_model_public_projection`（`packages/common/model_registry.py:3601`，28 字段）、`_hydro_run_response`（`packages/common/forecast_store.py:3964`，函数体只是 `_json_ready(dict(row))` 纯透传，真正的 key 集合由 `forecast_store.py:925-937` 的 `SELECT h.*, mi.river_network_version_id, bv.basin_id, COALESCE(...) AS source` 决定）、`store.preflight_model_operation`（21 字段，测量阶段仅 11 个有 fixture 佐证）。

**并补上缺失的守卫**：仓库已有 `jsonschema>=4.23.0` 依赖（`pyproject.toml:41`），但目前**没有任何测试拿真实响应校验 OpenAPI**——`test_openapi_drift.py` 只断言 yaml == 运行时 schema（两边同时说谎也绿），`test_api_contract.py` 是逐字段手写断言、从不 import `jsonschema`。本次新增该校验测试，让"形状以运行时为准"成为可执行约束而非口号。

## Evidence Mapping

| 验收标准 | 证据命令 | 节点 |
|---|---|---|
| tsc 归零 | `cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json` | 本地 |
| 前端三绿 | `pnpm test && pnpm build && pnpm check:api-types` | 本地 |
| 后端契约 | `uv run pytest tests/test_openapi_drift.py tests/test_openapi_31_contract.py tests/test_api_contract.py -q` | 本地 + node-27 真实 DB |
| yaml 合法 | `npx --yes @redocly/cli@1.25.13 lint openapi/nhms.v1.yaml --skip-rule no-unused-components` | 本地 |
| 门真的跑了 | 一次真实 PR CI run 里 `frontend-build` 的 typecheck step 显示 executed + passed（非 skip） | CI |
| 33 名归宿 | PR body 逐名表格 | — |

## Non-Goals

- 不把 `responses`/patch 注入升级为 `response_model=`（会改响应体）。
- 不实现 `/api/v1/lineage/river-point` 路由。
- 不把 `check:api-types` 接进 CI（issue 明确排除）。
- 不改任何 handler 的返回值构造、不改 `BASELINE_NULLABLE_COUNT`、不放松任何既有断言。
- 不把 `refused` 补进 `ModelLifecycleResult.status` enum（当前四值 `allowed|blocked|already_current|rollback`，`openapi/nhms.v1.yaml:4617-4622`）：`_record_state_clone_refusal_audit`（`packages/common/model_registry.py:3396-3401`）返回 `"status": "refused"`，经 `model_lifecycle_operation:2357` 正常 return（非异常）、路由 `apps/api/routes/models.py:629-640` 直送 `_ok` 得 **200**。**可达性前提**：今天不可达 —— `register_pre_activation_hook` 的调用方只有 tests 与 `openspec/changes/direct-grid-display-cutover/evidence/rehearse/rehearse.py:473-474`，API 进程内始终是默认的 `_default_no_op_hook`（`model_registry.py:606-610`）。一旦有人把真实 pre-activation hook 接进 API 进程，该 enum 连同本次刻意省略的 `cold_start_approval` 字段一起变成契约谎言。
