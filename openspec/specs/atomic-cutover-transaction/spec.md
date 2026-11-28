# atomic-cutover-transaction Specification

## Purpose
Record that model activation does not transfer warm state. The activation-time state clone was retired by #3189. State carry-over at a cutover is an operator procedure through the clone CLI of the compute role (`scripts/node22_clone_direct_grid_cutover_states.py`), and this capability no longer guarantees that an activated-but-not-transferred state is never observable.

## Requirements

### Requirement: Model activation SHALL NOT clone state

The model lifecycle operation SHALL NOT clone, write or refuse on warm state:
- it runs no pre-activation hook;
- it accepts no cold-start approval input;
- it writes no `hydro.state_snapshot` row.

State carry-over at a cutover is performed outside activation by the clone CLI of the compute role, `scripts/node22_clone_direct_grid_cutover_states.py`, whose
`baseline_cutover` mode partitions basins into warm candidates (fingerprint-gated clone into the scheduler state index)
and declared cold basins (verified to have no target state; nothing fabricated). The warm/cold partition exists only in
`baseline_cutover` mode and is declared per basin.

This capability no longer guarantees that an activated-but-not-transferred state is never observable. Ordering between
the clone CLI and activation is an operator procedure.

#### Scenario: Activation of a direct-grid variant writes no state row

- **WHEN** a direct-grid variant is activated for a scope with a previous active model
- **THEN** the activation commits through the single-transaction lifecycle without invoking any clone, and no
  `hydro.state_snapshot` row is written by the lifecycle operation

#### Scenario: The lifecycle operation takes no cold-start approval

- **WHEN** a caller inspects `PsycopgModelRegistryStore.model_lifecycle_operation`
- **THEN** it has no `cold_start_approval` parameter and its result carries no `cold_start_approval` block
