## Why

Carried follow-up from PR [#596](https://github.com/DankerMu/SHUD-NWM/pull/596) (issue [#598](https://github.com/DankerMu/SHUD-NWM/issues/598)). PR #596 fixed `list_models` SQL to JOIN `core.basin_version + core.basin` and surface `basin_id`/`basin_name` on every wire object. Unit test `test_list_models_exposes_basin_id_and_basin_name_from_join` (mocked cursor + substring assertion) catches "someone removes the JOIN" drift, but does NOT catch "JOIN executes but returns null/wrong values against real TimescaleDB" (e.g. future migration renames `core.basin_version.basin_id` or breaks FK).

A real-DB integration test locks the wire-shape invariant against schema drift before reaching node-27.

## What Changes

- **`tests/test_real_database_integration.py`**: add new `test_list_models_real_db_returns_basin_id_and_basin_name` under `pytestmark = pytest.mark.integration`. Test calls `GET /api/v1/models?active=all` against a seeded TimescaleDB instance (uses existing `apply_migrations_from_zero` + `seed_issue_126_data` fixtures), finds the seeded model by `MODEL_ID`, and asserts the response item carries `basin_id == BASIN_ID` and `basin_name == "Issue 126 Integration Basin"`.
- No production code change. No OpenAPI change.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `model-activation-deactivation`: ADD scenario *list_models real-DB integration test asserts basin_id/basin_name populated* under the existing requirement chain — provides CI-time real-DB lock for the JOIN contract anchored by PR #596 unit test + the post-#599 `Lifecycle response basin scoping consistency` requirement.

## Impact

- **Code**: `tests/test_real_database_integration.py` — ~15-20 lines (new test + assertions).
- **API 契约**: 无变化。
- **OpenAPI**: 无变化。
- **CI**: 路径 scope `tests/**` → 触发 `real-db-integration` job 跑（既有 job 已有 TimescaleDB sidecar，本测试加入复用同环境）。
- **Receipts**: 不需 node-27 live receipt（test-only addition，无 deploy-affecting 行为变化）。
