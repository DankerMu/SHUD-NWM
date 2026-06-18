# model-version-switch-rollback Specification

## Purpose
TBD - created by archiving change m18-model-asset-operations. Update Purpose after archive.
## Requirements
### Requirement: Model Version Switch and Rollback
The system SHALL support safe version switch and rollback using recorded prior active state.

#### Scenario: Version switch
WHEN an authorized user switches active model version for a basin
THEN the system records previous active model, new active model, reason, actor, and downstream impact.

#### Scenario: Rollback available
WHEN a rollback is requested and prior active state exists in audit/history
THEN the previous model is restored through the same preflight and audit path.

#### Scenario: Rollback unavailable
WHEN no trustworthy prior state exists
THEN rollback is blocked with a stable error and no model state is changed.

#### Scenario: Current active mismatch
WHEN rollback evidence expects current active model A but the scope currently has active model B
THEN rollback is blocked with a stale-history error and no model state is changed.

#### Scenario: Repeated rollback
WHEN the same rollback request is repeated after the previous request succeeded
THEN the system returns already-current or idempotent success without duplicate contradictory audit rows.

