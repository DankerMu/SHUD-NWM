## ADDED Requirements

### Requirement: Model activation SHALL NOT clone state

The model lifecycle operation SHALL NOT clone, write or refuse on warm state:
- it runs no pre-activation hook;
- it accepts no cold-start approval input;
- it writes no `hydro.state_snapshot` row.

State carry-over at a cutover is performed outside activation by the node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py`, whose
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

## REMOVED Requirements

### Requirement: The clone step engages only for direct-grid activations with a previous active model

**Reason**: The activation-time clone hook is retired (#3189). It was never registered in production, so no activation
ever engaged it.

**Migration**: Warm/cold partitioning, per basin and in `baseline_cutover` mode only, is done by the node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py` (`baseline_cutover`).

### Requirement: Explicit cold-start approval is the only way to commit an engaged cutover without a clone row

**Reason**: The approval input existed only to let an engaged activation-time clone commit without a clone row. With the
hook retired, the input, its audit records and its result block are deleted (#3189).

**Migration**: A cold start is declared per basin through the clone CLI's `baseline_cutover` cold-basin partition. Any other cold start is a cold start by omission and is the operator's responsibility. The stable refusal code
`state_clone_cold_start_approval_required` is still produced by the core clone function (`packages/common/state_clone.py`).

### Requirement: The cloned checkpoint is published to the scheduler file state index before the registry manifest re-publish

**Reason**: The publisher reads DB clone rows (`get_latest_clone_row_for_model_source`). The retired hook was their only
production source: the clone CLI writes the scheduler file state index, not `hydro.state_snapshot`. After #3189 no
production path produces the rows this requirement publishes.

**Migration**: Whether the post-commit state-index publisher seam is kept or retired is tracked in #3204. The clone CLI
writes the file state index directly.

### Requirement: State clone commits atomically with model activation

**Reason**: The clone no longer runs inside activation (#3189), so there is no shared transaction.

**Migration**: Atomicity now holds per basin/source unit of the clone CLI; see `fingerprint-gated-state-clone`
"File-index clone invocations preserve durable abort evidence and primary errors".
