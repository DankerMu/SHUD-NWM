# variant-activation-cutover Specification

## Purpose
Specify how a model variant becomes the active model for its basin:
- activation reuses the single-transaction lifecycle and runs no pre-activation hook (retired by #3189);
- every dispatch-set-changing transition re-publishes the scheduler registry manifest;
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

### Requirement: Dispatch-set-changing lifecycle transitions re-publish the scheduler registry manifest

The lifecycle operation SHALL re-publish the scheduler registry manifest via `publish_scheduler_registry_manifest` after ANY successful transition that changes the dispatch candidate set — `activate`, `switch_version`, `rollback_version`, and a `deactivate` that removes the currently active model — and SHALL NOT re-publish when an operation is blocked by preflight, rolled back (for example by an audit-persistence failure), or returns already-current without a state change. The trigger SHALL sit on the uniform post-commit tail of the lifecycle operation so that, as defensive future-proofing, any other active-removing transition ever admitted past preflight re-publishes under the same rule without new wiring — a standalone `supersede` addressed at the currently active model is such a transition, and today it never commits (preflight blocks it with `MISSING_ACTIVE_RISK`; the missing-active override is supported for `deactivate` only); supersession of the prior active model inside an `activate`/`switch_version`/`rollback_version` swap is covered by that swap's single re-publish.

#### Scenario: Manifest is re-published on successful activation

- **WHEN** a variant activation commits successfully
- **THEN** `publish_scheduler_registry_manifest` re-emits the registry manifest to NFS
- **THEN** the node-22 DB-free scheduler passively consumes the new active model through its `list_models(active=True)` read, with no direct database connection.

#### Scenario: Every dispatch-set-changing operation re-publishes, not only activate

- **WHEN** a `switch_version` or `rollback_version` swaps the active model, or a `deactivate` removes the currently active model (the §11.2 step-4 pause-production lever, committed via the sys_admin missing-active override)
- **THEN** the manifest is re-published exactly once after that successful commit, so node-22 never keeps serving a dispatch candidate set the database no longer has
- **THEN** a transition that does not change the dispatch candidate set (e.g. deprecating an already-inactive model) does not require a re-publish.

#### Scenario: A blocked, rolled-back, or already-current operation does not re-publish

- **WHEN** an operation is blocked by a preflight blocker, rolled back by an audit-persistence failure, or returns already-current without a state change
- **THEN** the scheduler registry manifest is not re-published
- **THEN** the previously published manifest remains the authority for the compute plane.

#### Scenario: Supersede addressed at the active model is preflight-blocked today and re-publishes nothing

- **WHEN** a standalone `supersede` operation addresses the scope's currently active model
- **THEN** preflight blocks the operation with `MISSING_ACTIVE_RISK` (no override path admits it — the missing-active override is supported for `deactivate` only), no state transition occurs, and the manifest is not re-published
- **THEN** if such an active-removing transition is ever admitted past preflight, the uniform post-commit trigger re-publishes for it under this requirement without new wiring.

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
