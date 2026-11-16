## MODIFIED Requirements

### Requirement: Activation runs ordered pre-activation extension-point hooks inside the transaction

The cutover transaction SHALL execute an ordered sequence of pre-activation hooks inside the same database transaction before the supersede+activate swap, and this change SHALL define only the hook contract and register empty/no-op hooks, implementing neither state clone nor station flag flip.

#### Scenario: Hooks run in declared order before the swap

- **WHEN** a variant activation begins
- **THEN** the registered pre-activation hooks run in their declared, stable order, each receiving the transaction cursor and the activation context (basin_version_id, previous active model, target model, source scope)
- **THEN** the supersede+activate swap runs only after all hooks return successfully.

#### Scenario: A raising hook aborts the whole transaction

- **WHEN** any pre-activation hook raises during activation
- **THEN** the entire lifecycle transaction rolls back with no model made active and no model superseded
- **THEN** the scheduler registry manifest is not re-published, so there is no "activated but hooks not run" intermediate state.

#### Scenario: This change ships empty hooks with mount points reserved for later changes

- **WHEN** the extension-point contract is defined by this change
- **THEN** the registered hooks are empty/no-op and change no state
- **THEN** the contract reserves ordered mount points for a fingerprint-gated state-clone hook (Change 5) and a `met.met_station.active_flag` flip hook (Change 8) without re-opening the lifecycle transaction.
- **THEN** the `station_flag_flip` mount point stays reserved as a default no-op after its only consumer, the Change 8 station-flag flip hook, was retired by #3164; no production code registers a hook there.

#### Scenario: Hooks do not run on the already-current path

- **WHEN** an activation request targets a model that is already the scope's active model (the idempotent already-current short-circuit)
- **THEN** the pre-activation hooks are NOT executed, because no supersede+activate swap occurs and there is no state change for a clone or flip hook to act on
- **THEN** the operation returns a stable already-current result with no second activation-success audit row and no duplicate transition.
