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

## Contract Clarifications

- Route naming may follow existing API conventions during implementation, but the stable contract is the service boundary plus canonical M17 action ids: `models.activate`, `models.deactivate`, `models.switch_version`, `models.rollback_version`, and `models.supersede`.
- `deprecate` does not introduce a new M17 action id in this issue. It SHALL reuse `models.deactivate` authorization with `operation=deprecate` in preflight/audit evidence until a future OpenSpec change adds a dedicated `models.deprecate` action.
- Deactivation that would leave a required operational basin without an active model is blocked by default. Override requires `sys_admin`, canonical action `models.deactivate`, a non-empty request reason, and explicit `override_missing_active=true` evidence; deterministic validation may exercise the blocked and override paths without live credentials.
- Audit write failure is fail-closed for mutating lifecycle operations: if the operation cannot persist the required audit/evidence row in the same transaction, the lifecycle state mutation SHALL roll back and return a stable blocked/release-blocked result with no model state change.

## Transition Policy

| Operation | From | To | Notes |
|---|---|---|---|
| activate | inactive/deprecated/superseded | active | Supersedes the prior active model in the same scope atomically. |
| deactivate | active | inactive | Blocked if the scope requires an active model and no replacement/override is present. |
| switch_version | inactive/deprecated/superseded candidate + active current | candidate active, current superseded | Equivalent to activate with explicit previous-active audit. |
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

## Workflow Fixture

Fixture level: expanded
Project profile: other
Repair intensity: high

Why:
- This change adds public API/service and UI controls for mutating model lifecycle state.
- It touches auth/RBAC, model registry state transitions, audit evidence, preflight blockers, frontend controls, and production readiness drills.
- Incorrect ordering or partial mutation can break forecast routing by changing the active model for a basin/version scope.

Change surface:
- Backend model registry lifecycle service/API for activate, deactivate, switch version, rollback, supersede, and deprecate.
- Preflight validation for basin/river/mesh/package lineage, object URI prefix, checksum reread, copied-root evidence, active conflicts, missing-active risk, and downstream impact.
- Audit/evidence rows for allowed, blocked, repeated, and rollback operations.
- M14 model asset UI controls gated by M17 roles, including preflight summary, confirmation, blocked reasons, audit reference, and stale refresh.
- Deterministic production-like validation drill and documentation/progress updates.

Must preserve:
- Existing readonly model registry/model asset APIs and UI browsing remain compatible.
- Imported Basins/model rows remain inactive by default until an explicit lifecycle operation activates them.
- M17 canonical role/action matrix remains the authorization source for all mutating operations.
- Historical queries by explicit model/version id remain valid after activate, supersede, deprecate, switch, or rollback.
- Deterministic tests and validation do not require production object-store delete/upload or live credentials.

Must add/change:
- Canonical lifecycle states are `inactive`, `active`, `deprecated`, and `superseded`.
- Active uniqueness is enforced for `(basin_id, basin_version_id)`.
- Activation and version switch atomically activate the candidate and supersede the previous active in the same scope.
- Deactivation is blocked when it would leave a required operational basin without active model unless an explicitly authorized override permits it.
- Rollback requires trustworthy prior active-state evidence and blocks on missing/stale history.
- Preflight and audit evidence are redacted and include stable request ids, actor, roles, action id, target ids, previous/new states, blockers, warnings, residual risk, and downstream impact.

Selected risk packs:
- Public API / CLI / script entry: lifecycle APIs and UI-triggered service actions are public protected entrypoints.
- Config / project setup: deterministic validation and readiness drills must not require live production services.
- File IO / path safety / overwrite: object URI, copied-root, checksum, and audit payloads may contain sensitive local/source paths.
- Schema / columns / units / field names: lifecycle state, preflight output, audit evidence, and UI/API response contracts are stable schemas.
- Resource limits / large input / discovery: preflight may inspect lineage/package evidence and downstream impact surfaces.
- Legacy compatibility / examples: existing readonly model asset browsing, Basins import, and active model consumers must remain compatible.
- Error handling / rollback / partial outputs: denied/preflight-blocked/stale-history paths must not partially mutate active state.
- Release / packaging / dependency compatibility: deterministic drills must report production-like capability without live object-store mutation.
- Documentation / migration notes: validation docs and progress must describe supported mutating scope and live/non-goal boundaries.

Risk packs considered:
- Public API / CLI / script entry: selected - lifecycle operations are protected public API/UI actions.
- Config / project setup: selected - deterministic drill and live/non-live boundaries must be explicit.
- File IO / path safety / overwrite: selected - object URI, copied-root, checksum, and audit evidence require safe projection.
- Schema / columns / units / field names: selected - lifecycle, preflight, audit, and UI contracts are schema-bearing.
- Geospatial / CRS / shapefile sidecars: not selected - no geometry transforms or CRS parsing are added; lineage compatibility is checked by existing registry metadata.
- Time series / forcing / temporal boundaries: not selected - no forcing/time-series computation changes.
- Numerical stability / conservation / NaN: not selected - no hydrologic solver or numeric fitting changes.
- Solver runtime / performance / threading: not selected - no SHUD runtime/threading behavior changes.
- Resource limits / large input / discovery: selected - preflight must keep lineage/package/downstream checks bounded.
- Legacy compatibility / examples: selected - readonly registry/model asset consumers and active model lookups must keep working.
- Error handling / rollback / partial outputs: selected - lifecycle operations must be atomic and fail without partial state changes.
- Release / packaging / dependency compatibility: selected - production-like validation must not require live object-store upload/delete.
- Documentation / migration notes: selected - supported mutating scope and exclusions must be documented.

Invariant Matrix:
- Governing invariant: every model lifecycle operation derives authorization, preflight, state transition, and audit evidence from the same basin/version/model identity before any registry state mutation, and the `(basin_id, basin_version_id)` scope has at most one active model after every successful operation.
- Source-of-truth identity/contract: `model_id`, `basin_id`, `basin_version_id`, current active model id, candidate model id, canonical action id, actor/roles, request id, package checksum/object URI, prior audit/history id, and lifecycle state.
- Producers: model registry store/service, lifecycle API route handlers, preflight producer, audit/evidence producer, frontend operation controls, deterministic validation drill.
- Validators/preflight: M17 `require_policy_evidence`/route auth, lifecycle transition validator, active uniqueness check, lineage/package checksum/object URI/copied-root checks, rollback history/stale-state validator, redaction helper.
- Storage/cache/query: `core.model_instance`, model registry related basin/river/mesh/package metadata, `ops.audit_log`, deterministic validation artifacts, frontend model asset store/cache.
- Public routes/entrypoints: model lifecycle API endpoints and M14 UI operation controls; direct service helpers used by deterministic drills.
- Frontend/downstream consumers: model asset detail/list views, active-model forecast/routing lookups, readiness summary readers, audit viewers/tests.
- Failure paths/rollback/stale state: RBAC denied, preflight blocked, active conflict, missing active risk, stale rollback history, repeated operation, concurrent activation loser, audit write failure handling.
- Evidence/audit/readiness: lifecycle audit rows, preflight reports, rollback history evidence, deterministic production-like drill output, docs and `progress.md`.
- Regression rows:
  - authorized activation with valid lineage and no stale state -> candidate active, previous active superseded in one transaction, audit/preflight evidence recorded.
  - missing/forbidden role -> stable `AUTH_REQUIRED` or `RBAC_FORBIDDEN`, no registry state change, blocked evidence where applicable.
  - invalid lineage/object URI/checksum/copied-root evidence -> preflight blocker, no registry state change, redacted blocker evidence.
  - deactivation would leave required scope without active model -> blocked unless explicit authorized override is present.
  - rollback with prior active-state evidence and matching current active -> prior model active, current active superseded, audit links prior evidence.
  - rollback with missing or stale history -> stable blocked/release-blocked result, no state change.
  - repeated operation -> idempotent/already-current result without duplicate contradictory audit rows.
  - concurrent activation in same `(basin_id, basin_version_id)` scope -> at most one active model; loser gets stable conflict/already-current result.
  - viewer/analyst UI user -> mutating controls hidden or disabled; backend denial remains authoritative.
  - stale frontend state after backend blocker -> UI refreshes model state and shows blocked reason/audit reference without claiming success.
  - audit payload with local path, object URI userinfo/query/fragment, checksum, or request reason -> redacted stable schema.

Non-goals:
- Uploading arbitrary new model packages.
- Deleting production model packages or object-store assets.
- Real live object-store mutation or enterprise live proof beyond deterministic readiness evidence.
- Changing hydrologic skill, calibration acceptance criteria, or solver runtime behavior.

Review focus:
- Lifecycle state transitions are atomic, idempotent where required, and preserve active uniqueness.
- Preflight runs before mutation and blocks unsafe lineage/package/active-risk states with stable evidence.
- M17 RBAC is enforced before mutating operations and denied paths do not mutate.
- Audit/preflight/readiness evidence is redacted, schema-stable, and tied to the exact model/basin/version identity.
- UI controls are gated but backend denial remains authoritative; stale state refresh is covered.
