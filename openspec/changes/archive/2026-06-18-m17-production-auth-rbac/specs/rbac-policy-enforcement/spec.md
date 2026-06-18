## ADDED Requirements

### Requirement: RBAC Policy Enforcement
The backend SHALL enforce role-based policy for production-sensitive actions before mutating system state.

The canonical action ids are `pipeline.retry_run`, `pipeline.cancel_run`, `pipeline.rerun_cycle`, `qc.override_result`, `tiles.republish`, `sources.update_config`, `models.activate`, `models.deactivate`, `models.switch_version`, `models.rollback_version`, `models.supersede`, and `users.manage`.

#### Scenario: Operator action allowed
WHEN an `operator` requests rerun, cancel, retry, or tile republish
THEN the backend authorizes only the allowed action set and records a policy decision.

#### Scenario: Model admin action allowed
WHEN a `model_admin` requests model activation or deactivation
THEN the backend authorizes model lifecycle actions but denies sys_admin-only source configuration changes.

#### Scenario: Viewer denied
WHEN a `viewer` requests any mutating operator/model/sys-admin action
THEN the backend returns a stable forbidden error and no database/object-store mutation occurs.

#### Scenario: Analyst read-only
WHEN an `analyst` requests QC override, pipeline mutation, model lifecycle mutation, source config update, tile republish, or user management
THEN the backend returns `403 RBAC_FORBIDDEN`, records `decision=deny`, and no target state changes.

#### Scenario: Sys admin action allowed
WHEN a `sys_admin` requests source config update or user management
THEN the backend authorizes the action, records `decision=allow`, and writes audit evidence.

#### Scenario: Release blocked dependency
WHEN a protected action requires live auth proof that is configured as release-blocked
THEN the backend returns `503 RELEASE_BLOCKED`, records `decision=release_blocked`, and no target state changes.
