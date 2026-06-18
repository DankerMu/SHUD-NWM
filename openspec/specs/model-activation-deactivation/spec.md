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

