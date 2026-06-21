## ADDED Requirements

### Requirement: List-models real-DB integration test for basin_id/basin_name population
The CI `real-db-integration` job SHALL run a focused integration test asserting that `GET /api/v1/models?active=all` returns items with populated `basin_id` and `basin_name` against an actual TimescaleDB schema. This locks the wire-shape invariant established by PR #596 against future schema/FK drift (rename of `core.basin_version.basin_id`, dropped JOIN dependency, etc.) before reaching node-27.

#### Scenario: list_models real-DB integration test asserts basin_id/basin_name populated
WHEN CI `real-db-integration` job executes
THEN a test named `test_list_models_real_db_returns_basin_id_and_basin_name` in `tests/test_real_database_integration.py` (under `pytestmark = pytest.mark.integration`) MUST exist
AND it MUST seed `core.basin` + `core.basin_version` + `core.model_instance` via the `seed_issue_126_data` helper
AND it MUST call `GET /api/v1/models?active=all` via the FastAPI TestClient
AND it MUST locate the response item where `model_id == MODEL_ID` (the seeded value)
AND it MUST assert that item's `basin_id == BASIN_ID` (literal constant) AND `basin_name == "Issue 126 Integration Basin"` (literal seeded value)
AND the test MUST fail loudly if future schema drift causes either field to be null or contain a different value
