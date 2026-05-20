## ADDED Requirements

### Requirement: Auth Readiness Evidence
Production readiness validation SHALL emit truthful auth/RBAC evidence that separates deterministic policy checks from live identity proof.

#### Scenario: Deterministic policy validation
WHEN the validation lane runs with fixture users and roles
THEN evidence records allowed/denied decisions for every protected action and marks `execution_mode=policy_simulated` or `backend_route_executed`.

#### Scenario: Live proof missing
WHEN no live IdP is configured
THEN summary evidence sets final production auth readiness to release-blocked with `execution_mode=release_blocked` and explicit removal criteria.

#### Scenario: Live proof supplied
WHEN live IdP configuration is supplied in an opt-in environment
THEN evidence records `execution_mode=live_proof`, provider metadata, role mapping result, and protected action checks without exposing tokens.
AND final auth readiness is satisfied only when explicit allowed and denied live-proof subjects prove, for every canonical action, an allowed live decision and a denied no-mutation live decision with internally consistent actor and role-mapping evidence.
AND the live-proof subjects are distinct identities, or the same actor carries non-contradictory raw-role and mapped-role evidence across allowed and denied proof.

#### Scenario: Execution mode truthfulness
WHEN auth readiness evidence is emitted
THEN it uses only `policy_simulated`, `backend_route_executed`, `live_proof`, or `release_blocked` as execution modes and does not satisfy live production readiness with deterministic or simulated evidence.
