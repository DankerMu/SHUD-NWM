## Context

PR [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) fixed `list_models` (`packages/common/model_registry.py:1650`) to JOIN basin tables so `basin_id`/`basin_name` populate the wire response. Unit test `test_list_models_exposes_basin_id_and_basin_name_from_join` ([tests/test_model_registration.py:3660](tests/test_model_registration.py:3660)) locks the SQL shape via mocked cursor + substring assertion — catches "JOIN removed" drift but not "JOIN executes against drifted schema".

Issue [#598](https://github.com/DankerMu/SHUD-NWM/issues/598) asks for a real-DB integration test that runs against actual TimescaleDB schema and asserts wire response carries the populated values. This catches schema-drift class:
- migration renames `core.basin_version.basin_id`
- FK between basin_version → basin dropped
- a future ALTER reshuffles JOIN dependencies

`tests/test_real_database_integration.py` already has `test_real_schema_api_and_postgis_spatial_smoke` ([line 397](tests/test_real_database_integration.py:397)) which calls `GET /api/v1/models?active=all` but asserts only `item["model_id"] == MODEL_ID`. The new test is dedicated (issue specifies "focused test"), with its own setup so its failure mode (basin contract drift) doesn't get mixed with the smoke-test scope.

## Goals / Non-Goals

**Goals**
1. Lock `list_models` wire-shape invariant against real TimescaleDB schema at CI time.
2. Catch schema/FK drift before node-27 deploy.
3. Use existing seed fixtures (`apply_migrations_from_zero` + `seed_issue_126_data`) — no new DB setup code.

**Non-Goals**
- 不扩展 `_fetch_active_model_for_scope` / `_update_model_lifecycle_state` 等 lifecycle 路径的 real-DB 覆盖（PR #599 已用单测 + 端到端 mock 覆盖；如未来真需 lifecycle real-DB，独立 issue 跟进）。
- 不动 `test_real_schema_api_and_postgis_spatial_smoke`（避免 smoke test 失败混淆 basin 契约失败的根因；新 test dedicated）。
- 不引入新的 fixtures / seed helpers（只复用既有）。

## Decisions

### Decision 1: dedicated test vs extending smoke test

**Choice**: 新增 dedicated `test_list_models_real_db_returns_basin_id_and_basin_name`，独立 `apply_migrations_from_zero` + `seed_issue_126_data` setup。

**Alternatives**:
- **Extend `test_real_schema_api_and_postgis_spatial_smoke`**: cheaper (复用同 DB setup)，但 smoke test 失败时根因诊断变难（"是 basin contract 还是 forecast 还是 jobs?"）。Issue 明确要求 "focused test"。否决。
- **Parametric test combining list + lifecycle real-DB**: 跨 issue 边界（lifecycle 真实 DB 测试归 #599 后续 / 独立 issue），本 PR 不引。否决。

### Decision 2: 断言 basin_name 字面值 vs 仅断 truthy

**Choice**: 断言 `basin_name == "Issue 126 Integration Basin"` 字面相等（与 seed fixture 同源），并断言 `basin_id == BASIN_ID`（常量）。

**理由**: 字面相等比 `is not None` 更强 — 锁住的不只是 "non-null"，还包括 "正确的 JOIN 拿到正确的 row"（catch JOIN 走到错误 basin_version 的 bug class）。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| `real-db-integration` job 时长增加 | 单测仅 1 次额外 HTTP call + 几行断言，可忽略（DB seed 已由现有 test 摊销）。 |
| seed fixture 漂移（basin_name 字符串改动）测试 brittle | seed_issue_126_data 是 deterministic fixture，与本测试同提交点同步漂移；并非真生产数据，无外部依赖。 |
| 测试只走 active=all + 含 seeded model，未覆盖 active=true / 多 model 场景 | 本 PR 范围内 issue 明确 minimal；多 model / 多 basin 场景可独立 follow-up（非本 issue scope）。 |

## Migration Plan

1. 本地 `uv run pytest -q -m integration tests/test_real_database_integration.py::test_list_models_real_db_returns_basin_id_and_basin_name`（如本地有 TimescaleDB sidecar）或 CI `real-db-integration` job 跑过即可。
2. 无 node-27 deploy（test-only）。
3. 回滚：删 1 个测试函数 + 1 个 openspec change，git revert。
