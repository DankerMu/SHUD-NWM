## Why

M9 delivered Basins-backed model registry import and read APIs, while M14 will expose readonly asset browsing. Operators also need safe model lifecycle operations using currently available Basins/demo data: activation, deactivation, version switch planning, audit, rollback evidence, and guarded UI/API flows.

## What Changes

- Add audited backend model lifecycle operations for activate, deactivate, supersede/deprecate, and rollback-to-previous-active where existing data supports it.
- Require preflight validation before any active model switch: model package lineage, basin/river/mesh compatibility, no active conflict, and downstream impact summary.
- Add frontend model asset operation controls gated by M17 RBAC.
- Add deterministic tests using existing Basins/model registry fixtures; no production upload/delete is required.
- Record rollback and audit evidence suitable for production readiness without claiming live operational proof.

## Capabilities

### New Capabilities

- `model-operation-preflight`
- `model-activation-deactivation`
- `model-version-switch-rollback`
- `model-operation-audit`
- `model-operation-ui-controls`

## Impact

- Model registry store/API, audit log, model asset UI from M14, production readiness validation, tests, and docs.
- May update OpenAPI for mutating model lifecycle endpoints.
- Uses existing Basins-backed model data and deterministic fixtures.

## Non-Goals

- Uploading arbitrary new model packages.
- Deleting production model packages or object-store assets.
- Changing hydrologic skill or calibration acceptance criteria.
