## Context

Current model registry supports Basins-backed discovery/import and active model audit for activation. M18 turns this into a complete, safe operator lifecycle surface that can be tested with current data and later used with production assets.

## Design Decisions

- Lifecycle operations are explicit API/service actions; direct database edits are not accepted.
- Canonical lifecycle states are `inactive`, `active`, `deprecated`, and `superseded`. Existing imported models remain `inactive` by default.
- Active uniqueness scope is `(basin_id, basin_version_id)` unless a later design explicitly broadens it; at most one model is active in that scope.
- Activation is atomic: the candidate model becomes `active`, the previous active model in the same scope becomes `superseded`, and historical queries remain available by explicit model/version ids.
- Deactivation changes an `active` model to `inactive` only when preflight proves the scope may have no active model or a replacement is activated in the same transaction.
- Every active switch runs preflight checks and emits an impact summary before mutation.
- Deactivation is allowed only when it does not leave required operational basins without an active model unless an explicit override role/policy permits it.
- Rollback uses prior audit state or active-history evidence; if prior state is unavailable, rollback is release-blocked rather than guessed.
- Object-store deletes/uploads are out of scope; operations change registry state and audit only.

## Transition Policy

| Operation | From | To | Notes |
|---|---|---|---|
| activate | inactive/deprecated | active | Supersedes the prior active model in the same scope atomically. |
| deactivate | active | inactive | Blocked if the scope requires an active model and no replacement/override is present. |
| switch_version | inactive/deprecated candidate + active current | candidate active, current superseded | Equivalent to activate with explicit previous-active audit. |
| rollback_version | current active + prior superseded/inactive | prior active, current superseded | Requires trustworthy prior audit/history. |
| supersede | active/inactive/deprecated | superseded | Used when replaced by a newer active model; historical queries remain valid. |
| deprecate | inactive/superseded | deprecated | Marks a model not recommended for future activation without deleting it. |

Preflight minimum fields: `basin_id`, `basin_version_id`, candidate `model_id`, current active `model_id`, `river_network_version_id`, `mesh_version_id`, package checksum, object URI prefix validation result, copied-root evidence status when applicable, downstream surfaces, blockers, warnings, and request id.

## Dependency Order

- M17 RBAC should land before production use; M18 can still define endpoints and tests with fixture auth.
- Preflight and audit before UI mutating controls.
- Rollback semantics after activation/deactivation/switch history is recorded.

## Risks and Mitigations

- Risk: bad activation breaks forecast routing. Mitigation: preflight checks basin/river/model compatibility and impact.
- Risk: rollback guesses previous state. Mitigation: rollback requires explicit prior audit/history record.
- Risk: UI exposes operations to unauthorized roles. Mitigation: depend on backend policy and frontend denial tests.

## Verification

- `openspec validate m18-model-asset-operations --strict`
- Backend model registry/API/audit tests.
- Frontend operation UI tests when controls are added.
- Production-like validation evidence for activation/deactivation/rollback drills using deterministic data.
