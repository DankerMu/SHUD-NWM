## ADDED Requirements

### Requirement: Lifecycle response basin scoping consistency
All internal SQL methods that produce rows passed to `_model_public_projection` for `POST /api/v1/models/{id}/lifecycle` responses SHALL populate `basin_id` and `basin_name` via JOINs into `core.basin_version` and `core.basin`. The lifecycle response contract MUST be uniform regardless of which internal method (`_fetch_model_lifecycle_row`, `_fetch_active_model_for_scope`, `_update_model_lifecycle_state`) produced the row.

#### Scenario: Lifecycle response basin_id consistency across internal SQL paths
WHEN `POST /api/v1/models/{id}/lifecycle` is called with operations `activate`, `deactivate`, `deprecate`, `switch_version`, or `rollback_version`
THEN the response `model` and `previous_model` fields MUST include populated `basin_id` and `basin_name` values
AND this MUST hold regardless of which internal method produced the row (`_fetch_model_lifecycle_row`, `_fetch_active_model_for_scope`, or `_update_model_lifecycle_state`)
AND each such method's executed SQL MUST contain `JOIN core.basin_version` and `JOIN core.basin` with `basin_id` and `basin_name` in the projection
AND a unit test MUST exist for each of `_fetch_active_model_for_scope` and `_update_model_lifecycle_state` that mocks the cursor and asserts the executed SQL contains the required JOINs + projection, locking the invariant against future SQL drift
