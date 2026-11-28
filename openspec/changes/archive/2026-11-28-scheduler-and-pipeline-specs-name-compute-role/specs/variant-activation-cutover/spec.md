## MODIFIED Requirements

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
