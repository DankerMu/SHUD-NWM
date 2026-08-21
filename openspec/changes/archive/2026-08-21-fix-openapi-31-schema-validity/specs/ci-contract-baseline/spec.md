## ADDED Requirements

### Requirement: OpenAPI error validation MUST be green without weakening rules

The exact CI OpenAPI Validate command using pinned Redocly 1.25.13 and only the existing `no-unused-components` skip MUST exit zero with no errors. The repair MUST NOT downgrade the declared dialect, disable `spec`, `security-defined`, or `no-empty-servers`, add a broad config suppression, or replace validation with a warning-only/collect-only check.

#### Scenario: Declared 3.1 contract passes the pinned validator

- **WHEN** CI lints `openapi/nhms.v1.yaml` with the repository command
- **THEN** all dialect, security-defined, and server errors are absent and the command exits zero

#### Scenario: Removing a load-bearing repair leg fails

- **WHEN** a mutation removes null normalization, the same-origin server, root security, or one protected-operation override
- **THEN** a contract test or the pinned Redocly command fails and names the missing invariant

### Requirement: OpenAPI changes MUST execute drift and generated-type assertions

A pull request changing `openapi/**` MUST open the backend targeted-test gate and select `tests/test_openapi_drift.py`, `tests/test_api_contract.py`, and `tests/test_openapi_31_contract.py`; a change to `apps/api/openapi_patching.py` MUST also select the drift and 3.1 invariant suites while preserving its existing API consumer tests. Generated frontend types MUST match the static schema, and a semantics-preserving dialect repair MUST leave the committed business type artifact byte-identical.

#### Scenario: OpenAPI-only change reaches assertions

- **WHEN** the changed-file set contains only `openapi/nhms.v1.yaml`
- **THEN** the backend filter is true and targeted selection includes the drift, API contract, and 3.1 dialect/security invariant suites without core-smoke fallback or zero-assertion collapse

#### Scenario: Patch-owner change reaches all consumers

- **WHEN** `apps/api/openapi_patching.py` changes
- **THEN** targeted selection includes the drift suite, 3.1 dialect/security invariant suite, generated-type/API contract suite, and the existing broad API consumer suites

#### Scenario: Generated business types do not drift

- **WHEN** all legacy nullable nodes are re-expressed with equivalent 3.1 unions and security/server metadata is added
- **THEN** `check:api-types` and the Python generated-type byte comparison pass with no change to `apps/frontend/src/api/types.ts`
