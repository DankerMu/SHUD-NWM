## ADDED Requirements

### Requirement: Cutover atomically flips the station set within the activation transaction

The cutover SHALL, within the same DB transaction as the direct-grid model activation (mounted on the Change `source-specific-model-variant-routing` lifecycle extension point, whose hook context carries the transaction cursor, `basin_version_id`, previous active model, target model, and source scope), re-point the display station set of the `basin_version` to exactly the TARGET variant's mirror set. Row selection SHALL be bound to the Change 4 persisted discriminators — never to a bare "legacy vs cell" role reading, and never to `active_flag` alone:

- **Activate set**: exactly the `met.met_station` mirror rows bound to the activating TARGET variant's built mapping-asset identity, resolved from the activation context's target model — its `resource_profile.direct_grid_forcing` (`model_input_package_id` + `binding_checksum`) matched against the Change 4 registration row shape (`station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"`, `station_role='direct_grid_cache'`, the derived-cache `properties_json` binding-identity fields, `grid_snapshot_id` FK to the target's registered snapshot) — set `active_flag=true`.
- **Deactivate set**: every other `met.met_station` row of the same `basin_version` — the legacy station set (`grid_snapshot_id IS NULL`, carrying no mapping-asset mirror identity) AND every other direct-grid generation's mirror rows (a prior or sibling generation's mapping-asset identity, per the Change 4 rule that multiple built generations' mirror rows MAY coexist) — set `active_flag=false`.

The station-MVT source query SHALL NOT be changed to filter by `model_id`.

#### Scenario: flip is atomic with model activation

- **WHEN** the cutover transaction for a `basin_version` commits
- **THEN** the whole station set is re-pointed in that single transaction — exactly the target variant's mirror rows end `active_flag=true` and every other `met.met_station` row of the `basin_version` (legacy rows and every non-target direct-grid generation's mirror rows) ends `active_flag=false`
- **AND** the flip is mounted on the Change `source-specific-model-variant-routing` lifecycle extension point, not on a separate display routing table.

#### Scenario: partial flip cannot persist

- **WHEN** any step of the cutover transaction (activation, state clone, or the flag flip) fails before commit
- **THEN** the whole transaction rolls back
- **AND** no `met.met_station.active_flag` change persists, so there is no "previous set deactivated but target set not activated" or "target set activated before model activation" intermediate state.

#### Scenario: fix-forward direct→direct′ re-flip activates only the target generation

- **WHEN** a `basin_version` whose active model is direct-grid generation M1 (its mirror set display-active) is cut over fix-forward to generation M1′, with both generations' mirror rows coexisting per the Change 4 registration contract (both `grid_snapshot_id` NOT NULL, possibly bound to the same snapshot, distinguished only by their mapping-asset station identities)
- **THEN** on commit exactly the M1′ mirror rows (matched by M1′'s `model_input_package_id` + `binding_checksum` identity) are `active_flag=true`
- **AND** every M1 mirror row and every M0 legacy row of the `basin_version` is `active_flag=false`, so the committed display set is never M1 ∪ M1′.

#### Scenario: multiple registered-but-inactive generations do not widen the activate set

- **WHEN** two or more built direct-grid generations' mirror rows are registered for the same `basin_version` and a cutover activates one target generation
- **THEN** only the target generation's mirror rows become `active_flag=true`
- **AND** every non-target generation's mirror rows remain (or become) `active_flag=false`.

#### Scenario: station-MVT source query is unchanged

- **WHEN** the flip runs
- **THEN** the station-MVT source-identity query still filters only by `basin_version_id` and `active_flag=true`
- **AND** it is NOT modified to add a `model_id` predicate.

### Requirement: The flip hook engages only for direct-grid cutover activations

The station-flag flip SHALL be a pre-activation hook under the Change 4 extension-point contract (hooks run inside the lifecycle transaction on every real supersede+activate swap and self-gate; on the idempotent already-current short-circuit the hook is not invoked at all — owned by Change 4's contract). The flip hook SHALL be a no-op — writing no `met.met_station` row and recording an audited skip reason — unless BOTH hold in the hook context, mirroring the Change 5 clone-hook applicability classifier: (a) the target model classifies as direct-grid under Change 4's single classifier (its `resource_profile.direct_grid_forcing` parses through `workers/forcing_producer/direct_grid_contract.py:load_forcing_mapping_contract_from_manifest` with `forcing_mapping_mode='direct_grid'`; a contract that is absent, malformed, or non-direct classifies as legacy-mapping, fail-closed), and (b) the activation context carries a previous active model for the scope. The engaged forms are therefore exactly the legacy→direct-grid cutover and the direct→direct′ fix-forward.

#### Scenario: legacy-target activation leaves the station set untouched

- **WHEN** an activation-class operation's target model classifies as legacy-mapping — e.g. a routine `switch_version` or `rollback_version` on one of the 13 live legacy production basins
- **THEN** the flip hook no-ops with the audited skip reason `target_not_direct_grid` and mutates no `met.met_station` row
- **AND** the 13 production basins' normal lifecycle operations leave their station sets and station-MVT layers unchanged (regression-locked by a negative test).

#### Scenario: first activation with no previous active model does not flip

- **WHEN** a direct-grid variant is activated for a basin scope that has no previous active model (a fresh basin routed direct-grid from day one)
- **THEN** the flip hook no-ops with the audited skip reason `no_previous_active_model` and the activation commits with `met.met_station` untouched
- **AND** the display bring-up of a first station set on such a basin is owned by the rollout change (Change 7), not by this cutover flip — there is no previous display set to cut over from.

### Requirement: Shadow-period mirror stays inactive so no mixed display occurs

The system SHALL keep every direct-grid cell-station mirror at `active_flag=false` for the entire shadow period (before its generation's cutover), so the station-MVT layer never renders the legacy and cell station sets simultaneously and never feeds their union to the MVT feature budget.

#### Scenario: shadow-period mirror is not displayed

- **WHEN** a direct-grid variant is registered but not yet activated **AND** its cell-station mirror rows exist with `active_flag=false`
- **THEN** the station-MVT layer for that `basin_version` returns only the currently display-active station set
- **AND** it returns no shadow-period mirror station, so no mixed set is emitted.

#### Scenario: post-cutover layer shows only the target generation's cell stations

- **WHEN** the cutover transaction for a `basin_version` has committed
- **THEN** the station-MVT layer returns only the target variant's cell-station mirror rows
- **AND** it returns zero legacy stations and zero non-target-generation mirror stations.

#### Scenario: dual-track mixed display is never emitted

- **WHEN** legacy station rows and one or more direct-grid generations' mirror rows exist for the same `basin_version` at any instant
- **THEN** at most one of those station sets has `active_flag=true`, enforced by the single-transaction flip's "exactly the target set active, all other rows false" predicate
- **AND** the station-MVT feature set is never the union of two sets, so it cannot overflow the MVT feature budget by mixing them.

#### Scenario: shadow mirror `active_flag=false` is regression-locked

- **WHEN** the shadow-period registration path writes a cell-station mirror
- **THEN** a regression test asserts the mirror row is written with `active_flag=false`
- **AND** the assertion fails closed if a mirror is ever registered `active_flag=true`.
