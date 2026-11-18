## ADDED Requirements

### Requirement: A model lifecycle transition SHALL NOT publish the scheduler file plane

The DB model lifecycle operation SHALL publish nothing to the node-22 scheduler file plane: neither the registry manifest
nor the state index, on any outcome. A committed transition alone therefore does not change node-22's dispatch set.

node-22's registry manifest, and so its dispatch set, changes only through the operator procedure in
`docs/runbooks/production-ops/recalibration-and-archive.md`:
- node-27 `scripts/provision_direct_grid_scheduler_registry.py` writes the DB rows and a staging registry, never the
  canonical manifest;
- node-22 `scripts/node22_model_succession.py` runs copyback, preflight, begin, clone, publish, refresh and finish, or,
  with `--kind remove_basin`, removes manifest rows;
- the merged publish goes through `scripts/node22_publish_merged_scheduler_registry.py`.

The node-22 scheduler and its refresh timer keep maintaining the state index themselves; that is unrelated to the
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

## REMOVED Requirements

### Requirement: Dispatch-set-changing lifecycle transitions re-publish the scheduler registry manifest

**Reason**: Retired by #3204. The post-commit manifest and state-index publisher seams never had a production
registration, so every real transition ran the default no-op. Wiring them would have created a second, competing
manifest writer on node-27 next to the node-22 operator path. The node-22 path owns the `frd_muziyao` manifests and the
private object-store root. After #3189 the state-index publisher would also have had no DB clone rows to publish.

**Migration**: None at runtime, because production behaviour is unchanged. node-22's dispatch set changes through the
operator procedure in `docs/runbooks/production-ops/recalibration-and-archive.md`. See the ADDED
requirement "A model lifecycle transition SHALL NOT publish the scheduler file plane".
