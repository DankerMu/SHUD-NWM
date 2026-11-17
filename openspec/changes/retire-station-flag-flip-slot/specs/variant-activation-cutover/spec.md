## REMOVED Requirements

### Requirement: Activation runs ordered pre-activation extension-point hooks inside the transaction

**Reason**: It reserved a `station_flag_flip` mount point for the Change 8 station-flag flip hook. #3164 retired that
hook (no production code ever registered it), so the slot has no consumer. The validator does not allow a MODIFIED
that drops a scenario, so the requirement is restated below without the reservation.

**Migration**: Replaced by "Activation runs the ordered pre-activation hook chain inside the transaction". The hook
contract, the declared-order dispatch, abort-on-raise and the already-current skip are unchanged; the only mount point
is `state_clone`.

## ADDED Requirements

### Requirement: Activation runs the ordered pre-activation hook chain inside the transaction

The cutover transaction SHALL execute the ordered pre-activation hook chain inside the same database transaction,
before the supersede+activate swap, without re-opening the lifecycle transaction. The chain's mount points SHALL be
`PRE_ACTIVATION_HOOK_MOUNT_POINTS = ("state_clone",)`, dispatched in declared order. Each mount point SHALL start as a
default no-op that changes no state. Registering a hook on any other mount point, including the retired
`station_flag_flip`, SHALL be rejected.

#### Scenario: Hooks run in declared order before the swap

- **WHEN** a variant activation begins
- **THEN** the registered pre-activation hooks run in their declared, stable order, each receiving the transaction
  cursor and the activation context (basin_version_id, previous active model, target model, source scope)
- **THEN** the supersede+activate swap runs only after all hooks return successfully.

#### Scenario: A raising hook aborts the whole transaction

- **WHEN** any pre-activation hook raises during activation
- **THEN** no later hook runs, and the entire lifecycle transaction rolls back with no model made active and no model
  superseded
- **THEN** the scheduler registry manifest is not re-published, so there is no "activated but hooks not run"
  intermediate state.

#### Scenario: Default hooks change no state

- **WHEN** no hook has been registered on a mount point
- **THEN** that mount point's default hook is a no-op and the lifecycle behaves exactly as it does without the chain.

#### Scenario: The retired station-flag mount point is rejected

- **WHEN** a caller registers a hook on `station_flag_flip` or any other name outside `PRE_ACTIVATION_HOOK_MOUNT_POINTS`
- **THEN** registration raises, naming the valid mount points, and no hook is installed.

#### Scenario: Hooks do not run on the already-current path

- **WHEN** an activation request targets a model that is already the scope's active model (the idempotent
  already-current short-circuit)
- **THEN** the pre-activation hooks are NOT executed, because no supersede+activate swap occurs and there is no state
  change for a hook to act on
- **THEN** the operation returns a stable already-current result with no second activation-success audit row and no
  duplicate transition.
