# legacy-reactivation-guard Specification

## Purpose
TBD - created by archiving change source-specific-model-variant-routing. Update Purpose after archive.
## Requirements
### Requirement: Legacy-mapping re-activation is refused once a basin has direct-grid activation history

The lifecycle preflight SHALL refuse, fail-closed and with no override, any `activate`, `switch_version`, or `rollback_version` whose target is a legacy-mapping model for a basin that has direct-grid activation history.

The three predicate terms are pinned as follows:

- **Target** means the model that would become `active` as a result of the operation: for `activate` and `switch_version` the addressed model; for `rollback_version` the restored model (the `previous_model` that the rollback re-activates, `packages/common/model_registry.py` `restored_model`), NOT the addressed currently-active model.
- **Direct-grid classification** uses exactly one classifier everywhere the guard needs it: a model classifies as direct-grid if and only if its `resource_profile.direct_grid_forcing` parses successfully through `workers/forcing_producer/direct_grid_contract.py:load_forcing_mapping_contract_from_manifest` with `forcing_mapping_mode='direct_grid'`; a row whose contract is absent, malformed, or non-direct classifies as legacy-mapping (fail-closed). The same classifier applies to the refused target, the permitted fix-forward target, and the history predicate.
- **Direct-grid activation history** is derived from an append-only source, never from mutable current `lifecycle_state` alone: the basin scope has history when there EXISTS an `ops.audit_log` record of a successful activation-class transition (`action IN ('models.activate','models.switch_version','models.rollback_version')` with `details->>'outcome' IN ('allowed','rollback')`, the record shape already queried at `packages/common/model_registry.py:1996-2009`) for the `basin_version_id` whose resulting-active model (`details->'updated_model'`) classifies as direct-grid, OR when the scope's currently active model classifies as direct-grid. Later `deactivate`/`deprecate`/`supersede` transitions cannot erase this history, and no lifecycle-state mutation of a never-activated variant can fabricate it.

#### Scenario: Activating a legacy model is refused after direct-grid history exists

- **WHEN** an operator requests `activate` on a legacy-mapping model for a basin whose audit history contains a successful direct-grid activation
- **THEN** the preflight returns a blocked result identifying the legacy-reactivation blocker and performs no state transition
- **THEN** no model is superseded, no model becomes active, and no scheduler manifest is re-published.

#### Scenario: switch_version to a legacy target is refused

- **WHEN** an operator requests `switch_version` whose addressed target classifies as legacy-mapping for a basin with direct-grid activation history
- **THEN** the preflight refuses the operation as a blocker
- **THEN** the refusal is identical in effect to the `activate` refusal: no transition, no manifest re-publish.

#### Scenario: rollback_version whose restored model is legacy-mapping is refused

- **WHEN** an operator requests `rollback_version` addressed at the currently active direct-grid model and the restored model (`previous_model_id`) classifies as legacy-mapping
- **THEN** the preflight refuses the operation, because the guard classifies the model that would become active — the restored model — not the addressed model
- **THEN** the refusal holds even when trustworthy prior active state exists in audit/history: the legacy-reactivation guard takes precedence over rollback availability, with no transition and no manifest re-publish.

#### Scenario: Direct-grid activation history survives deactivate and deprecate

- **WHEN** a cut-over basin's active direct-grid variant is deactivated (including via the sys_admin missing-active override, leaving it `inactive`) and optionally deprecated afterwards, and an operator then requests `activate` on the basin's legacy model
- **THEN** the guard still refuses the legacy activation, because the append-only `ops.audit_log` activation record persists regardless of the direct-grid rows' current `lifecycle_state`
- **THEN** there is no sequence of supported lifecycle operations that erases direct-grid activation history and re-admits legacy re-activation.

#### Scenario: Supersede of a never-activated variant does not fabricate history

- **WHEN** a registered direct-grid variant that was never activated is transitioned `inactive` → `superseded` via the `supersede` operation on a basin whose active model is legacy
- **THEN** the guard is not armed, because no successful activation-class audit record exists for a direct-grid model in the scope and the currently active model is not direct-grid
- **THEN** the basin's normal legacy `activate`/`switch_version` operations proceed exactly as before.

#### Scenario: A malformed direct-grid contract classifies as legacy-mapping and is refused distinctly

- **WHEN** an activation-class operation on a basin with direct-grid activation history targets a model whose `resource_profile.direct_grid_forcing` declares `forcing_mapping_mode='direct_grid'` but fails the contract parser (e.g. missing `binding_checksum` or malformed `station_bindings`)
- **THEN** the target classifies as legacy-mapping (fail-closed) and the operation is refused
- **THEN** the blocker distinguishes "invalid direct-grid contract" from "legacy reactivation" so an operator can tell a broken fix-forward candidate from a genuine legacy target.

#### Scenario: The guard has no override

- **WHEN** any override flag, reason, or elevated role accompanies a legacy re-activation request on a basin with direct-grid history
- **THEN** the guard still refuses the operation, because rejecting rollback to legacy is a fixed product decision (grill 2026-07-09)
- **THEN** there is no code path that admits the legacy re-activation.

### Requirement: direct-to-direct fix-forward is permitted

The lifecycle preflight SHALL permit activation whose target is itself a direct-grid variant even on a basin with direct-grid activation history, so fix-forward (direct→direct′) is available.

#### Scenario: Activating a direct-grid variant on a basin with direct-grid history is allowed

- **WHEN** an operator activates a direct-grid variant (its `resource_profile.direct_grid_forcing` parses successfully with `forcing_mapping_mode='direct_grid'`, the same classifier as the refusal predicate) on a basin with direct-grid activation history
- **THEN** the legacy-reactivation guard does not block the operation, because the target classifies as direct-grid
- **THEN** the cutover proceeds through the normal supersede+activate transaction (subject to the other existing preflight blockers).

### Requirement: The guard is inert without direct-grid activation history

The lifecycle preflight SHALL leave legacy activation permitted for a basin that has no direct-grid activation history, including a basin whose only direct-grid variant is `inactive` (registered but never activated).

#### Scenario: An inactive-only direct-grid variant does not arm the guard

- **WHEN** a basin has a direct-grid variant registered with `lifecycle_state='inactive'` and no successful activation-class audit record exists for any direct-grid model in the scope
- **THEN** an `activate` request on the basin's legacy model is not blocked by the legacy-reactivation guard
- **THEN** registering a variant (this mechanism-only change) does not arm the guard for any basin, because registration writes no activation audit record.

#### Scenario: The 13 live legacy basins are unaffected

- **WHEN** the guard evaluates a live basin that has no direct-grid variant at all
- **THEN** legacy activation, deactivation, and version switching behave exactly as before this change
- **THEN** the guard introduces no new blocker for basins without direct-grid activation history.

### Requirement: The guard scopes to production activation operations only

The legacy-reactivation guard SHALL apply only to the lifecycle activation-class operations and SHALL NOT interfere with offline replay or calibration that uses the legacy package outside the lifecycle activate path.

#### Scenario: Offline replay/calibration with the legacy package is not blocked

- **WHEN** an offline replay or calibration run uses the legacy model package directly, not via a lifecycle `activate`/`switch_version`/`rollback_version` operation
- **THEN** the guard does not fire, because it is scoped to production activation operations
- **THEN** the legacy package remains usable as immutable calibration/replay lineage.

