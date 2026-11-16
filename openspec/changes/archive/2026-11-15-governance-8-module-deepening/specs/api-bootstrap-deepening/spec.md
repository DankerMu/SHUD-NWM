## ADDED Requirements

### Requirement: API app factory remains stable
The API bootstrap extraction SHALL keep `create_app(env=None)`, runtime config
state, registered route behavior, static frontend serving, health routes,
error handlers, middleware behavior, and display cache warmup stable.

#### Scenario: display-readonly app starts
- **WHEN** `create_app` starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** runtime config reports display readonly capability flags, Slurm
  routes are not registered, static/health routes are served as before, and
  control-plane mutation routes remain fail-closed

#### Scenario: compute-control or dev app starts
- **WHEN** `create_app` starts with `compute_control` or `dev_monolith`
- **THEN** existing route registration, middleware, OpenAPI schema behavior,
  and local tests remain compatible

### Requirement: Bootstrap responsibilities move to focused modules
The API bootstrap extraction SHALL separate OpenAPI patching, route registry,
static/health mounting, runtime role wiring, and startup cache behavior without
duplicating runtime-role parsing or authorization policy logic.

#### Scenario: OpenAPI patching is extracted
- **WHEN** schema patch helpers move out of `apps/api/main.py`
- **THEN** the generated OpenAPI content for runtime, pipeline, station-series,
  QHH latest-product, MVT, flood, and layer metadata remains equivalent

#### Scenario: router registration is extracted
- **WHEN** route inclusion moves behind a registry helper
- **THEN** display-readonly, compute-control, dev-monolith, and slurm-gateway
  role rules remain aligned with `apps/api/runtime_mode.py` and
  `docs/governance/ROLE_BOUNDARY.md`

### Requirement: API extraction is role-boundary tested
Each API bootstrap extraction PR SHALL include focused role-boundary and
OpenAPI verification. Protected mutation auth guard and request-body validation
SHALL be retained on a stable seam in this change; this change only strengthens
guard-seam tests and documents the retention boundary unless a future change
opens a separate owner-module extraction.

#### Scenario: API bootstrap slice is complete
- **WHEN** an API bootstrap owner module is introduced
- **THEN** runtime mode tests, API tests, role-boundary static tests, and any
  affected OpenAPI/frontend type checks pass before merge

#### Scenario: protected mutation seam is retained
- **WHEN** API bootstrap extraction reaches protected mutation auth guard or
  request-body validation code
- **THEN** the PR adds or keeps tests for request id, error shape, auth policy,
  and fail-closed display behavior without mixing that seam into route registry
  or OpenAPI patch extraction

#### Scenario: API inventory is updated with the extraction
- **WHEN** API bootstrap responsibilities change ownership or retention
  classification
- **THEN** the structural disposition inventory records the owner module,
  retained facade surface, removal condition if any, and focused verification
  command in the same PR
