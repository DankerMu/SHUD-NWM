## 1. SQL rewrite: `_fetch_active_model_for_scope`

- [x] 1.1 在 `packages/common/model_registry.py:1929-1953` 重写 SQL：mirror `_fetch_model_lifecycle_row` JOIN shape — `JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id` + `JOIN core.basin b ON b.basin_id = bv.basin_id` + `JOIN core.river_network_version rnv ON rnv.river_network_version_id = mi.river_network_version_id` + `JOIN core.mesh_version mv ON mv.mesh_version_id = mi.mesh_version_id`。
- [x] 1.2 Projection 字段必须与 [packages/common/model_registry.py:1903-1913](packages/common/model_registry.py:1903) 的 `_fetch_model_lifecycle_row` 逐字一致（`mi.*, COALESCE(...) AS lifecycle_state, b.basin_id, b.basin_name, bv.checksum AS basin_checksum, rnv.segment_count, rnv.checksum AS river_network_checksum, mv.mesh_uri, mv.checksum AS mesh_checksum, mv.properties_json AS mesh_properties_json`）。理由：保证三个方法 raw row 形状统一，下游（audit-insert / preflight）不会因新路径缺字段 KeyError。
- [x] 1.3 保留 `WHERE mi.basin_version_id = %s AND mi.active_flag = true AND COALESCE(mi.lifecycle_state, 'active') = 'active'` 谓词。
- [x] 1.4 保留 `ORDER BY mi.created_at DESC, mi.model_id LIMIT 1`。
- [x] 1.5 保留 `{lock_clause}` 在 SELECT 末尾，**严格 mirror `_fetch_model_lifecycle_row` 形态（不加 `OF mi`）**。所有现存 callers 都 `for_update=False`，且 canonical 形态已在 production 多年无升级事故；保持 parity 避免本 PR 引入与 canonical 不同的锁定语义。

## 2. SQL rewrite: `_update_model_lifecycle_state` (CTE wrap)

- [x] 2.1 在 `packages/common/model_registry.py:2289-2305` 改写：把现有 `UPDATE core.model_instance SET ... RETURNING *` 包在 `WITH updated AS (UPDATE ... RETURNING *)` CTE，外层 `SELECT u.*, COALESCE(u.lifecycle_state, ...) AS lifecycle_state, b.basin_id, b.basin_name, bv.checksum AS basin_checksum, rnv.segment_count, rnv.checksum AS river_network_checksum, mv.mesh_uri, mv.checksum AS mesh_checksum, mv.properties_json AS mesh_properties_json FROM updated u JOIN core.basin_version bv ON bv.basin_version_id = u.basin_version_id JOIN core.basin b ON b.basin_id = bv.basin_id JOIN core.river_network_version rnv ON rnv.river_network_version_id = u.river_network_version_id JOIN core.mesh_version mv ON mv.mesh_version_id = u.mesh_version_id`。
- [x] 2.2 Projection 字段必须与 task 1.2 / `_fetch_model_lifecycle_row` 逐字一致（同一理由：下游 row 形状统一）。
- [x] 2.3 `cursor.fetchone()` 返回 row 形状不变（CTE 外层 SELECT 输出对调用方是 dict-like row）。

## 3. Regression tests

- [x] 3.1 在 `tests/test_model_registration.py` 找到 PR #596 的 `test_list_models_exposes_basin_id_and_basin_name_from_join` (line 3660) 作为模板邻位置，新增 `test_fetch_active_model_for_scope_joins_basin` — mock cursor, 调用 `_fetch_active_model_for_scope`, 断言 cursor.execute 第一个 args 的 SQL 字符串含 `JOIN core.basin_version`, `JOIN core.basin`, `b.basin_id`, `b.basin_name`，且 returned dict 含 `basin_id` 非 None。
- [x] 3.2 新增 `test_update_model_lifecycle_state_joins_basin` — 同样 mock cursor，断言 SQL 含 `WITH updated AS`, `JOIN core.basin_version`, `JOIN core.basin`, `b.basin_id`, `b.basin_name`，且 returned dict 含 `basin_id` 非 None。
- [x] 3.3 新增 `test_model_lifecycle_operation_response_populates_basin_id` — 用 fake cursor + monkeypatch 模拟一次 `activate` 操作走完 `model_lifecycle_operation` 完整 transition 链，断言 `result["model"]["basin_id"]` 非 None（spec scenario *Lifecycle response basin_id consistency across internal SQL paths* 的整合测试，覆盖 SQL 层 + `_model_public_projection` pass-through 层联合契约）。

## 4. Verify locally

- [x] 4.1 `uv run pytest -q tests/test_model_registration.py -k "list_models_exposes_basin or fetch_active_model_for_scope_joins or update_model_lifecycle_state_joins or model_lifecycle_operation_response"` PASS
- [x] 4.2 `uv run ruff check packages/common/model_registry.py tests/test_model_registration.py` clean
- [x] 4.3 `openspec validate fix-lifecycle-basin-id-join-drift --strict --no-interactive` PASS

## 5. PR / merge hygiene

- [ ] 5.1 PR body `Closes #599`，附 Chinese 工作总结
- [ ] 5.2 review-loop log append 一行
- [ ] 5.3 OpenSpec archive：合并后 `openspec archive fix-lifecycle-basin-id-join-drift --yes`（无 collision risk — sole modifier of `model-activation-deactivation`）
