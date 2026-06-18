# readiness-evidence-schema Specification

## Purpose
TBD - created by archiving change m19-production-readiness-proof. Update Purpose after archive.
## Requirements
### Requirement: Readiness Evidence Schema
Production readiness evidence SHALL use a stable schema for status, execution mode, dependencies, artifacts, residual risk, and blocker removal criteria.

`status` values are `passed`, `failed`, `blocked`, `not_executed`, and `release_blocked`. `execution_mode` values are `deterministic`, `policy_simulated`, `backend_route_executed`, `dry_run_sink`, `simulated_drill`, `live_proof`, and `not_executed`.

Each readiness item SHALL include `required_for_final`, `live_proof_accepted`, `artifact_refs`, `residual_risk`, and `removal_criteria`. The summary SHALL include `final_production_readiness_claimed`.

Allowed status/execution-mode combinations are:

- `passed`: `deterministic`, `policy_simulated`, `backend_route_executed`, `dry_run_sink`, `simulated_drill`, or `live_proof`.
- `failed`: any executed mode except `not_executed`.
- `blocked`: `not_executed`.
- `not_executed`: `not_executed`.
- `release_blocked`: `not_executed`, `policy_simulated`, `dry_run_sink`, `simulated_drill`, or `live_proof`.

Failed required live proof SHALL be represented as `status=release_blocked`, `execution_mode=live_proof`, `required_for_final=true`, and `live_proof_accepted=false`.

#### Scenario: Deterministic evidence
WHEN a deterministic validation item passes
THEN evidence records `status=passed`, `execution_mode=deterministic`, artifact paths, and does not imply live proof.

#### Scenario: Deterministic preflight blocked
WHEN a deterministic check requires a local fixture or dependency that is missing
THEN evidence records `status=blocked`, `execution_mode=not_executed`, and the missing deterministic dependency.

#### Scenario: Missing live dependency
WHEN a required live dependency is unavailable
THEN evidence records `status=release_blocked`, `execution_mode=not_executed` or a non-live mode, owner/action text, and removal criteria.

#### Scenario: Invalid status/mode pair
WHEN evidence contains `status=blocked` with an executed mode or `status=failed` with `execution_mode=not_executed`
THEN schema validation rejects the item and records a deterministic validation failure.

#### Scenario: Required live proof fails
WHEN a required live proof executes but fails or is not accepted
THEN evidence records `status=release_blocked`, `execution_mode=live_proof`, `required_for_final=true`, `live_proof_accepted=false`, residual risk, and removal criteria.

#### Scenario: Out-of-scope dependency
WHEN CLDAS or incomplete national data is not considered in the current scope
THEN evidence records `status=not_executed`, `execution_mode=not_executed`, and the explicit exclusion reason rather than pass/fail.

#### Scenario: Final readiness claim
WHEN any `required_for_final=true` readiness item is missing, failed, release-blocked, or not accepted as live proof
THEN the summary records `final_production_readiness_claimed=false`.

#### Scenario: Redacted bounded evidence
WHEN readiness evidence includes provider metadata, sink metadata, receipts, artifact paths, URLs, tokens, credentials, query strings, or oversized/deep payloads
THEN persisted artifacts and stdout redact sensitive values and bound payload depth/size while preserving stable status/blocker fields.

