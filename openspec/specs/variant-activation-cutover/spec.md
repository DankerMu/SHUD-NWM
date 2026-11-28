# variant-activation-cutover Specification

## Purpose
Specify how a model variant becomes the active model for its basin:
- activation reuses the single-transaction lifecycle and runs no pre-activation hook (retired by #3189);
- a lifecycle transition publishes nothing to the scheduler file plane; the file-provider refresh lane of the compute role republishes it (post-commit publishers retired by #3204);
- a superseded model permanently leaves the dispatch candidate set.

## Requirements

### Requirement: Cutover reuses the existing single-transaction activation lifecycle

The cutover switch SHALL be the existing `POST /api/v1/models/{model_id}/lifecycle` `activate` operation executed by `model_lifecycle_operation`, reusing its single-transaction swap, `FOR UPDATE` concurrency safety, idempotency, and same-transaction audit, and SHALL NOT introduce a new per-`(basin, source)` routing table or a separate cutover state machine.

#### Scenario: Activating the variant supersedes the prior active model in one transaction

- **WHEN** an authorized operator activates a direct-grid variant for a basin whose scope has a currently active model
- **THEN** within the same database transaction the prior active model becomes `superseded` and the variant becomes `active`
- **THEN** the transition writes both `lifecycle_state` and `active_flag` together and inserts one audit row in the same transaction.

#### Scenario: Cutover is concurrency-safe and idempotent

- **WHEN** two activation requests target the same `(basin_id, basin_version_id)` scope concurrently
- **THEN** the transaction preserves one active model and returns a stable conflict or already-current result to the losing request
- **THEN** repeating the same activation returns a stable already-current result without a duplicate transition.

#### Scenario: No new routing table is introduced

- **WHEN** the cutover selects which model a basin runs
- **THEN** the active model is resolved through the existing lifecycle active-model authority and the partial unique index on `basin_version_id`
- **THEN** no separate per-`(basin, source)` routing table is created or consulted.

### Requirement: Permanent retirement — a superseded model exits the dispatch candidate set

The system SHALL exclude a `superseded` model from the scheduler dispatch candidate set by filtering `lifecycle_state == 'active'`, and this behavior SHALL be locked by a regression test; a superseded model SHALL be retained as immutable lineage rather than destroyed.

#### Scenario: The superseded model is no longer dispatched

- **WHEN** a cutover supersedes the prior active model and activates the variant
- **THEN** the superseded model is excluded from the scheduler dispatch candidate list because the consumption face filters `lifecycle_state == 'active'`
- **THEN** only the newly active variant is dispatched for the basin.

#### Scenario: Regression test locks the active-only dispatch filter

- **WHEN** the scheduler registry dispatch candidate list is produced
- **THEN** a regression test asserts that a `superseded` (or otherwise non-`active`) model is excluded and only `lifecycle_state == 'active'` rows are dispatch candidates
- **THEN** a future refactor that re-admits a non-active model to dispatch fails that test.

#### Scenario: Retired is not destroyed

- **WHEN** a model is superseded by a cutover
- **THEN** its `core.model_instance` row and package remain immutable and available for historical products, fingerprint-gated state clone, and offline calibration-replay
- **THEN** the superseded row is never deleted by the cutover.

### Requirement: A model lifecycle transition SHALL NOT publish the scheduler file plane

The DB model lifecycle operation SHALL publish nothing to the compute role's scheduler file plane: neither the registry manifest
nor the state index, on any outcome. A committed transition alone therefore does not change the compute role's dispatch set.

The compute role's registry manifest, and so its dispatch set, changes only through the operator procedure in
`docs/runbooks/production-ops/recalibration-and-archive.md`:
- node-27 `scripts/provision_direct_grid_scheduler_registry.py` writes the DB rows and a staging registry, never the
  canonical manifest;
- on the compute control host, `scripts/node22_model_succession.py` runs the steps of its `--kind`: `recalibration`, `cold_start`,
  `add_basin` or `remove_basin` (runbook §5.7–§5.7.4);
- the merged publish goes through `scripts/node22_publish_merged_scheduler_registry.py`.

The compute role's scheduler and its refresh timer keep maintaining the state index themselves; that is unrelated to the
lifecycle operation.

#### Scenario: A committed activation publishes nothing

- **WHEN** an `activate`, `switch_version`, `rollback_version`, or active-removing `deactivate` commits
- **THEN** the lifecycle operation returns its result without invoking any manifest or state-index publisher, and the
  model registry store exposes no post-commit publisher registration

#### Scenario: Supersede addressed at the active model is preflight-blocked and changes nothing

- **WHEN** a standalone `supersede` addresses the scope's currently active model
- **THEN** preflight blocks it with `MISSING_ACTIVE_RISK` and no state transition occurs

#### Scenario: An audit-persistence failure rolls the transition back

- **WHEN** the lifecycle audit insert fails after the transition
- **THEN** the result is `blocked` with the last blocker `LIFECYCLE_AUDIT_PERSISTENCE_FAILED` and the transaction is
  not committed
