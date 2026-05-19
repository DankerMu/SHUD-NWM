## ADDED Requirements

### Requirement: Model Operation Audit
Every model lifecycle operation SHALL produce redacted, queryable audit evidence.

#### Scenario: Successful operation
WHEN activation, deactivation, version switch, or rollback succeeds
THEN audit records actor, `roles[]`, action id, model_id, basin_version_id, previous/new active state, reason, request id, and lineage checksums.

#### Scenario: Blocked operation
WHEN preflight or RBAC blocks an operation
THEN evidence records the blocker reason without mutating model state.

#### Scenario: Sensitive lineage
WHEN model lineage contains local paths or sensitive URI components
THEN audit output redacts them using public-safe projection.
