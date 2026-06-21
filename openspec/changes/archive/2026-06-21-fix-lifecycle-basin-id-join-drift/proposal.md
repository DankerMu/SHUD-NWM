## Why

Carried follow-up from PR #596 reviewer-2 (issue [#599](https://github.com/DankerMu/SHUD-NWM/issues/599)). PR #596 fixed `list_models` and `get_model_internal` JOIN drift so `basin_id` / `basin_name` survive on the wire. Reviewer-2 surfaced two siblings on the **lifecycle** path that retain the same drift class:

| Method | File:line | SQL shape | basin_id on wire? |
|---|---|---|---|
| `_fetch_active_model_for_scope` | [packages/common/model_registry.py:1929-1953](packages/common/model_registry.py:1929) | `SELECT mi.* FROM core.model_instance mi` (no JOIN) | NO |
| `_update_model_lifecycle_state` | [packages/common/model_registry.py:2289-2305](packages/common/model_registry.py:2289) | `UPDATE ... RETURNING *` (no JOIN) | NO |
| `_fetch_model_lifecycle_row` (reference) | [packages/common/model_registry.py:1897-1927](packages/common/model_registry.py:1897) | JOINS `basin_version + basin + river_network_version + mesh_version` | YES |

Both feed `_model_public_projection` for `POST /api/v1/models/{id}/lifecycle`. Per OpenAPI [openapi/nhms.v1.yaml:2406-2412](openapi/nhms.v1.yaml:2406), `ModelLifecycleResult.model` / `.previous_model` reference `ModelInstance` which declares `basin_id` (nullable). Lifecycle responses currently return `basin_id == null` because the SQL never JOINs `basin_version → basin` for these two paths — the field is "valid wire" but semantically empty in the admin UI ([apps/frontend/src/stores/modelAssets.ts:642,654-655](apps/frontend/src/stores/modelAssets.ts:642)).

Severity is **lower than PR #596's popup bug** (admin UI, not user-facing hot path); but same class of drift — should be tracked and closed.

## What Changes

- **`_fetch_active_model_for_scope`** (preferred fix-direction): rewrite SQL to mirror `_fetch_model_lifecycle_row`'s JOIN shape (`basin_version → basin + river_network_version + mesh_version`), keep `ORDER BY mi.created_at DESC, mi.model_id` and `LIMIT 1`, append `lock_clause` last.
- **`_update_model_lifecycle_state`** (CTE-wrap pattern): wrap the `UPDATE ... RETURNING *` in a `WITH updated AS (...)` CTE then `SELECT u.*, b.basin_id, b.basin_name, ...` joining the related dimension tables. Keeps atomicity (single round-trip, single transaction) and avoids the option-B extra round-trip overhead.
- **Regression tests** (mirror PR #596 pattern — actual template is `test_list_models_exposes_basin_id_and_basin_name_from_join` at [tests/test_model_registration.py:3660](tests/test_model_registration.py:3660)): for both methods, mock the cursor and assert the executed SQL contains `JOIN core.basin_version` + `JOIN core.basin` + `basin_id` + `basin_name` projection. Plus one end-to-end test through `model_lifecycle_operation` asserting `result["model"]["basin_id"]` populated (locks the spec scenario at integration boundary, not just SQL text).
- **No OpenAPI change**: `basin_id` is already declared on `ModelInstance` (nullable); this fix populates it where it was previously null.
- **No spec change to wire contract**: existing `model-activation-deactivation` requirement already covers lifecycle scope; this change ADDS a new requirement enforcing "lifecycle responses MUST populate `basin_id`/`basin_name` consistently across all internal SQL paths" so the invariant is anchored in spec and tested.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `model-activation-deactivation`: ADD requirement *Lifecycle response basin scoping consistency* — enforces that `_fetch_active_model_for_scope` and `_update_model_lifecycle_state` populate `basin_id`/`basin_name` via JOIN so `POST /api/v1/models/{id}/lifecycle` response is uniform regardless of which internal path produced the row.

## Impact

- **Code**: `packages/common/model_registry.py` — 2 SQL rewrites (~20-30 lines each); `tests/test_model_registry.py` — 2-3 new tests (mock + projection); no public API change.
- **API 契约**: 无 schema 变化（`basin_id` 已在 ModelInstance 上 nullable 声明，仅填充行为修正）。
- **OpenAPI**: 无变化。
- **CI**: 路径 scope 命中 `packages/**` → `unit-test` 跑；`tests/**` → 同。无 e2e/real-db 触发（pure pyhton SQL test，无 schema 触碰）。
- **Receipts**: 不需 node-27 live receipt（pure refactor SQL，无 deploy-affecting wire contract 变化；frontend behavior unchanged in semantics — `basin_id` was already nullable on wire, just populated now in additional paths）。

## Archive ordering

No collision risk: this change is sole MODIFIER of `model-activation-deactivation` in the current unarchived set.
