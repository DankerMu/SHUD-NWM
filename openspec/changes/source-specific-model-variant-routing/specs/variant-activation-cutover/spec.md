## ADDED Requirements

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

#### Scenario: Hooks do not run on the already-current path

- **WHEN** an activation request targets a model that is already the scope's active model (the idempotent already-current short-circuit)
- **THEN** the pre-activation hooks are NOT executed, because no supersede+activate swap occurs and there is no state change for a clone or flip hook to act on
- **THEN** the operation returns a stable already-current result with no second activation-success audit row and no duplicate transition.

### Requirement: Dispatch-set-changing lifecycle transitions re-publish the scheduler registry manifest

The lifecycle operation SHALL re-publish the scheduler registry manifest via `publish_scheduler_registry_manifest` after ANY successful transition that changes the dispatch candidate set — `activate`, `switch_version`, `rollback_version`, and a `deactivate` that removes the currently active model — and SHALL NOT re-publish when an operation is blocked by preflight, rolled back by a raising hook, or returns already-current without a state change. The trigger SHALL sit on the uniform post-commit tail of the lifecycle operation so that, as defensive future-proofing, any other active-removing transition ever admitted past preflight re-publishes under the same rule without new wiring — a standalone `supersede` addressed at the currently active model is such a transition, and today it never commits (preflight blocks it with `MISSING_ACTIVE_RISK`; the missing-active override is supported for `deactivate` only); supersession of the prior active model inside an `activate`/`switch_version`/`rollback_version` swap is covered by that swap's single re-publish.

#### Scenario: Manifest is re-published on successful activation

- **WHEN** a variant activation commits successfully
- **THEN** `publish_scheduler_registry_manifest` re-emits the registry manifest to NFS
- **THEN** the node-22 DB-free scheduler passively consumes the new active model through its `list_models(active=True)` read, with no direct database connection.

#### Scenario: Every dispatch-set-changing operation re-publishes, not only activate

- **WHEN** a `switch_version` or `rollback_version` swaps the active model, or a `deactivate` removes the currently active model (the §11.2 step-4 pause-production lever, committed via the sys_admin missing-active override)
- **THEN** the manifest is re-published exactly once after that successful commit, so node-22 never keeps serving a dispatch candidate set the database no longer has
- **THEN** a transition that does not change the dispatch candidate set (e.g. deprecating an already-inactive model) does not require a re-publish.

#### Scenario: A blocked, rolled-back, or already-current operation does not re-publish

- **WHEN** an operation is blocked by a preflight blocker, aborted by a raising pre-activation hook, or returns already-current without a state change
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
