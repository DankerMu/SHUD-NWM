## ADDED Requirements

### Requirement: Auth Readiness Evidence
Production readiness validation SHALL emit truthful auth/RBAC evidence that separates deterministic policy checks from live identity proof.

#### Scenario: Deterministic policy validation
WHEN the validation lane runs with fixture users and roles
THEN evidence records allowed/denied decisions for every protected action and marks `execution_mode=policy_simulated` or `backend_route_executed`.

#### Scenario: Live proof missing
WHEN no live IdP is configured
THEN summary evidence sets final production auth readiness to release-blocked with explicit removal criteria.

#### Scenario: Live proof supplied
WHEN live IdP configuration is supplied in an opt-in environment
THEN evidence records provider metadata, role mapping result, and protected action checks without exposing tokens.
