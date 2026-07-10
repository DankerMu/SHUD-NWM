## MODIFIED Requirements

### Requirement: Model Version Switch and Rollback

The system SHALL support safe version switch and rollback using recorded prior active state, subject to the legacy-reactivation guard: a switch or rollback whose resulting-active model is a legacy-mapping model is refused, fail-closed and with no override, when the basin has direct-grid activation history (see the `legacy-reactivation-guard` capability).

#### Scenario: Version switch

WHEN an authorized user switches active model version for a basin
THEN the system records previous active model, new active model, reason, actor, and downstream impact.

#### Scenario: Rollback available

WHEN a rollback is requested and prior active state exists in audit/history, and the legacy-reactivation guard does not apply (the restored model is not a legacy-mapping model on a basin with direct-grid activation history)
THEN the previous model is restored through the same preflight and audit path.

#### Scenario: Rollback or switch refused by the legacy-reactivation guard

WHEN a rollback or version switch has trustworthy prior active state but its resulting-active model is a legacy-mapping model and the basin has direct-grid activation history
THEN the operation is blocked by the legacy-reactivation preflight blocker with no model state change and no scheduler manifest re-publish, even though prior state exists, and no override admits it.

#### Scenario: Rollback unavailable

WHEN no trustworthy prior state exists
THEN rollback is blocked with a stable error and no model state is changed.

#### Scenario: Current active mismatch

WHEN rollback evidence expects current active model A but the scope currently has active model B
THEN rollback is blocked with a stale-history error and no model state is changed.

#### Scenario: Repeated rollback

WHEN the same rollback request is repeated after the previous request succeeded
THEN the system returns already-current or idempotent success without duplicate contradictory audit rows.
