## MODIFIED Requirements

### Requirement: RBAC Policy Enforcement
The backend SHALL enforce role-based policy for production-sensitive actions before mutating system state.

The canonical action ids are `pipeline.retry_run`, `pipeline.cancel_run`, `pipeline.rerun_cycle`, `qc.override_result`, `tiles.republish`, `sources.update_config`, `models.activate`, `models.deactivate`, `models.switch_version`, `models.rollback_version`, `models.supersede`, `users.manage`, `slurm.submit_job`, `slurm.cancel_job`, and `slurm.reset_registry`.

#### Scenario: Operator action allowed
WHEN an `operator` requests rerun, cancel, retry, tile republish, Slurm job submission, Slurm array submission, or Slurm job cancellation
THEN the backend authorizes only the allowed action set and records a policy decision.

#### Scenario: Model admin action allowed
WHEN a `model_admin` requests model activation or deactivation, Slurm job submission, Slurm array submission, or Slurm job cancellation
THEN the backend authorizes those actions but denies sys_admin-only source configuration and Slurm registry reset actions.

#### Scenario: Viewer denied
WHEN a `viewer` requests any mutating operator/model/sys-admin action, including any Slurm mutation
THEN the backend returns a stable forbidden error and no database/object-store/Slurm/gateway-registry mutation occurs.

#### Scenario: Analyst read-only
WHEN an `analyst` requests QC override, pipeline mutation, model lifecycle mutation, source config update, tile republish, user management, or any Slurm mutation
THEN the backend returns `403 RBAC_FORBIDDEN`, records `decision=deny`, and no target state changes.

#### Scenario: Sys admin action allowed
WHEN a `sys_admin` requests source config update, user management, or an enabled Slurm mutation including registry reset
THEN the backend authorizes the action, records `decision=allow`, and writes audit evidence.

#### Scenario: Missing or invalid Slurm credential
WHEN submit, array submit, cancel, or an enabled reset receives no valid auth context
THEN the route returns `401 AUTH_REQUIRED` or `403 RBAC_FORBIDDEN` before constructing/calling the gateway and no Slurm or registry side effect occurs.

#### Scenario: Release blocked dependency
WHEN a protected action requires live auth proof that is configured as release-blocked
THEN the backend returns `503 RELEASE_BLOCKED`, records `decision=release_blocked`, and no target state changes.
