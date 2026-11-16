## Why

Governance-7 made the six highest-entropy implementation surfaces visible and
inventoried, but most of those surfaces are still large compatibility facades
or lane aggregators. This change turns the inventories into implementation
work so future behavior changes land in deep owner modules instead of growing
the same shallow files again.

## What Changes

- Deepen the `services/orchestrator/scheduler.py` compatibility facade by moving
  remaining implementation families behind owner modules while preserving the
  current `ProductionScheduler` entrypoint and legacy import/monkeypatch paths.
- Deepen the `services/orchestrator/chain.py` compatibility facade by moving
  remaining stage, manifest, accounting, retry, publication, worker-adapter,
  and persistence families behind owner modules without changing orchestration
  behavior.
- Complete the two-node E2E lane decomposition plan behind the existing
  `validate_two_node_e2e_evidence(config)` entrypoint, including shared
  producer, identity, current-run, redaction, path-safety, source-scope, and
  final aggregation contracts.
- Complete the production readiness lane decomposition plan behind the existing
  `validate_readiness(config)` and `validate_readiness_item(item)` entrypoints,
  keeping deterministic review evidence separate from live proof acceptance.
- Deepen the API bootstrap surface by separating OpenAPI patching, role-aware
  router registration, static/health mounting, and startup wiring while keeping
  `create_app(env=None)` stable.
- Deepen the frontend M11 map surface by separating data builders, MapLibre
  primitives, interaction dispatch, camera/error state, and popup/selection
  coordination while keeping `M11MapLibreSurface` stable.
- Add focused verification and issue boundaries for every owner family so no
  phase leaves a half-extracted module without parity tests or compatibility
  evidence.

## Capabilities

### New Capabilities

- `scheduler-facade-deepening`: Complete scheduler owner-module extraction while preserving legacy scheduler facade contracts.
- `chain-facade-deepening`: Complete orchestration-chain owner-module extraction while preserving chain facade contracts.
- `two-node-e2e-lane-deepening`: Complete two-node E2E evidence lane extraction behind the stable validator entrypoint.
- `readiness-validation-lane-deepening`: Complete production readiness lane extraction behind the stable readiness entrypoints.
- `api-bootstrap-deepening`: Separate API app bootstrap responsibilities without changing runtime role behavior.
- `frontend-map-surface-deepening`: Separate M11 map surface responsibilities without changing map behavior or display evidence boundaries.

### Modified Capabilities

- None. This change introduces implementation-architecture capabilities and
  does not intentionally modify product/API behavior requirements.

## Impact

- Affected backend modules:
  `services/orchestrator/scheduler.py`, `services/orchestrator/scheduler_*`,
  `services/orchestrator/chain.py`, `services/orchestrator/chain_*`,
  `services/orchestrator/reservation.py`, `services/orchestrator/retry.py`,
  `services/orchestrator/persistence.py`, and
  `services/orchestrator/production_contract.py`.
- Affected production-closure modules:
  `services/production_closure/two_node_e2e_evidence.py`,
  `services/production_closure/two_node_e2e_*`,
  `services/production_closure/readiness_validation.py`, and future
  `services/production_closure/readiness_*` owner modules.
- Affected API/frontend modules:
  `apps/api/main.py`, `apps/api/runtime_mode.py`, `apps/api/routes/**`,
  `apps/frontend/src/components/map/M11MapLibreSurface.tsx`, and adjacent M11
  map components/helpers.
- No database migration, public route removal, Slurm behavior change,
  display-readonly capability expansion, or entropy hard gate enablement is in
  scope.
