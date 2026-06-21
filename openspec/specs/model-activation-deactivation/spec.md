# model-activation-deactivation Specification

## Purpose
TBD - created by archiving change m18-model-asset-operations. Update Purpose after archive.
## Requirements
### Requirement: Model Activation and Deactivation
The backend SHALL expose audited model activation and deactivation operations with deterministic state transitions.

Canonical lifecycle states are `inactive`, `active`, `deprecated`, and `superseded`. The active uniqueness scope is `(basin_id, basin_version_id)`.

#### Scenario: Activate model
WHEN an authorized model_admin activates an inactive model after passing preflight
THEN that model becomes active for its basin/version scope and the previous active model in the same scope becomes `superseded` in the same transaction.

#### Scenario: Deactivate model
WHEN an authorized model_admin deactivates a model after passing preflight
THEN the model no longer appears in default active listings while historical queries remain available.

#### Scenario: Concurrent activation
WHEN two activation requests target the same `(basin_id, basin_version_id)` scope concurrently
THEN the database transaction preserves one active model and returns a stable conflict or already-current result for the losing request.

#### Scenario: Repeat same operation
WHEN the same activation/deactivation request is repeated
THEN the operation is idempotent or returns a stable already-current result without duplicate audit confusion.

### Requirement: Lifecycle response basin scoping consistency
All internal SQL methods that produce rows passed to `_model_public_projection` for `POST /api/v1/models/{id}/lifecycle` responses SHALL populate `basin_id` and `basin_name` via JOINs into `core.basin_version` and `core.basin`. The lifecycle response contract MUST be uniform regardless of which internal method (`_fetch_model_lifecycle_row`, `_fetch_active_model_for_scope`, `_update_model_lifecycle_state`) produced the row.

#### Scenario: Lifecycle response basin_id consistency across internal SQL paths
WHEN `POST /api/v1/models/{id}/lifecycle` is called with operations `activate`, `deactivate`, `deprecate`, `switch_version`, or `rollback_version`
THEN the response `model` and `previous_model` fields MUST include populated `basin_id` and `basin_name` values
AND this MUST hold regardless of which internal method produced the row (`_fetch_model_lifecycle_row`, `_fetch_active_model_for_scope`, or `_update_model_lifecycle_state`)
AND each such method's executed SQL MUST contain `JOIN core.basin_version` and `JOIN core.basin` with `basin_id` and `basin_name` in the projection
AND a unit test MUST exist for each of `_fetch_active_model_for_scope` and `_update_model_lifecycle_state` that mocks the cursor and asserts the executed SQL contains the required JOINs + projection, locking the invariant against future SQL drift

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

