## ADDED Requirements

### Requirement: Readiness Evidence Schema
Production readiness evidence SHALL use a stable schema for status, execution mode, dependencies, artifacts, residual risk, and blocker removal criteria.

`status` values are `passed`, `failed`, `blocked`, `not_executed`, and `release_blocked`. `execution_mode` values are `deterministic`, `policy_simulated`, `backend_route_executed`, `dry_run_sink`, `simulated_drill`, `live_proof`, and `not_executed`.

#### Scenario: Deterministic evidence
WHEN a deterministic validation item passes
THEN evidence records `status=passed`, `execution_mode=deterministic`, artifact paths, and does not imply live proof.

#### Scenario: Deterministic preflight blocked
WHEN a deterministic check requires a local fixture or dependency that is missing
THEN evidence records `status=blocked`, `execution_mode=not_executed`, and the missing deterministic dependency.

#### Scenario: Missing live dependency
WHEN a required live dependency is unavailable
THEN evidence records `status=release_blocked`, `execution_mode=not_executed` or a non-live mode, owner/action text, and removal criteria.

#### Scenario: Out-of-scope dependency
WHEN CLDAS or incomplete national data is not considered in the current scope
THEN evidence records `status=not_executed`, `execution_mode=not_executed`, and the explicit exclusion reason rather than pass/fail.
