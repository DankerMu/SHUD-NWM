## 1. New test

- [x] 1.1 在 `tests/test_real_database_integration.py` 紧邻 `test_real_schema_api_and_postgis_spatial_smoke` (line 397) 之后或之前，新增 `test_list_models_real_db_returns_basin_id_and_basin_name(integration_database_url, tmp_path, monkeypatch)`。
- [x] 1.2 用 `apply_migrations_from_zero(integration_database_url)` + `seed_issue_126_data(integration_database_url, object_root=tmp_path / "object-store")` + `set_integration_env(...)` 完成 setup（mirror line 402-405 模式）。
- [x] 1.3 `with TestClient(app) as client:` 调 `client.get("/api/v1/models", params={"active": "all"})`，assert `response.status_code == 200`。
- [x] 1.4 从 `response.json()["data"]["items"]` 找 `item["model_id"] == MODEL_ID` 的条目（用 `next(...)` 表达，缺则 raise `AssertionError`）。
- [x] 1.5 断言 `item["basin_id"] == BASIN_ID` AND `item["basin_name"] == "Issue 126 Integration Basin"`（字面相等，常量来自 `tests.integration_helpers`）。

## 2. Verify

- [ ] 2.1 本地若有 TimescaleDB sidecar，跑 `uv run pytest -q -m integration tests/test_real_database_integration.py::test_list_models_real_db_returns_basin_id_and_basin_name`；否则依赖 CI `real-db-integration` job 验证。
- [x] 2.2 `uv run ruff check tests/test_real_database_integration.py` clean
- [x] 2.3 `openspec validate add-real-db-list-models-basin-assertion --strict --no-interactive` PASS

## 3. PR / merge hygiene

- [ ] 3.1 PR body `Closes #598`，附 Chinese 工作总结
- [ ] 3.2 review-loop log append 一行
- [ ] 3.3 OpenSpec archive：合并后 `openspec archive add-real-db-list-models-basin-assertion --yes`（无 collision risk — 与 #599 的 `model-activation-deactivation` 修改互不重叠，两者均加新 Requirement）。
