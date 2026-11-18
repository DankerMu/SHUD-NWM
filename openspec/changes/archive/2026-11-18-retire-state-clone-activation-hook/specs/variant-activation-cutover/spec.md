## MODIFIED Requirements

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

## REMOVED Requirements

### Requirement: Activation runs the ordered pre-activation hook chain inside the transaction

**Reason**: #3189 retires the last mount point (`state_clone`). No production code ever registered a hook on it, so every
real activation ran only the default no-op. With no mount point left, the chain, its registration and its dispatch are
dead code and are deleted.

**Migration**: None at runtime; behaviour is unchanged, because the only hook that ever ran was the no-op. State
carry-over at a cutover is done outside activation by the node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py`.
