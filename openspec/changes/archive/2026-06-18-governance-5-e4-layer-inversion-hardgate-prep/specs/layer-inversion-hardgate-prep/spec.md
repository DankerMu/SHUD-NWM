## ADDED Requirements

### Requirement: Services do not import API layer modules

Non-API service and shared modules SHALL NOT import from `apps.api.*`.

#### Scenario: entropy audit scans service modules
- **WHEN** the audit scans `services/**` and `packages/**`
- **THEN** no `apps-api-layer-inversion` finding is emitted for current code

### Requirement: API-specific behavior stays at the API boundary

API response adaptation SHALL remain in `apps/api` routes or injected adapters, not in lower-level service modules.

#### Scenario: service returns tile metadata
- **WHEN** a service function produces tile metadata or query data
- **THEN** the API route adapts it to HTTP response details without requiring the service to import `apps.api.*`

### Requirement: Shared helpers live below the API layer

Reusable constants, error codes, and formatting helpers needed outside API routes SHALL live in shared or service modules that do not depend on `apps.api.*`.

#### Scenario: production closure validation needs a shared error helper
- **WHEN** readonly validation code needs a helper currently owned by `apps.api`
- **THEN** the helper is moved to or wrapped by a lower-level module before the service imports it

### Requirement: Layer inversion can become future hard-gate eligible

After current findings are fixed, the audit SHALL be able to mark `apps-api-layer-inversion` as a future hard-gate candidate without failing on known baseline findings.

#### Scenario: layer inversion is reported as role-boundary drift
- **WHEN** `apps.api.*` imports are found outside API-owned code
- **THEN** the audit reports them under the standalone `apps-api-layer-inversion` check id and role-boundary governance face instead of merging them into API retirement or display cleanup

#### Scenario: current layer inversions are gone
- **WHEN** the entropy audit runs after this change
- **THEN** the `apps-api-layer-inversion` count is zero and future gate eligibility can be considered separately

### Requirement: Existing readonly validation exception is reconciled

The role-boundary documentation SHALL be updated if this change removes or replaces the documented readonly validation API-probe exception.

#### Scenario: readonly validation no longer imports API modules
- **WHEN** `services/production_closure/readonly_db_validation.py` stops importing API modules
- **THEN** `docs/governance/ROLE_BOUNDARY.md` no longer documents that import as an active exception

#### Scenario: readonly validation still needs an API-owned probe
- **WHEN** readonly validation still needs to exercise FastAPI app or route behavior
- **THEN** the probe is isolated behind an API-owned adapter or explicitly documented as not gate-eligible without moving API construction into shared modules
