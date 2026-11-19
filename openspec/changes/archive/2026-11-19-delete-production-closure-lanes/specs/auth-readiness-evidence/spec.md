## REMOVED Requirements

### Requirement: Auth Readiness Evidence

**Reason**: The auth/RBAC readiness evidence was emitted only by the production ops and readiness (`validate-ops`, `validate-readiness`) validation lanes, which are deleted (#3221): they are no longer an acceptance mechanism and nothing in CI or on either node runs them.
**Migration**: None; backend authorization itself stays specified by `backend-auth-context` and `rbac-policy-enforcement`.
