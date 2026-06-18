# audit-decision-recording Specification

## Purpose
TBD - created by archiving change m17-production-auth-rbac. Update Purpose after archive.
## Requirements
### Requirement: Audit Decision Recording
Allowed, denied, and release-blocked protected actions SHALL produce redacted audit/evidence records suitable for operations review.

#### Scenario: Allowed action audit
WHEN an authorized user performs a protected action
THEN the audit record includes actor, `roles[]`, action id, target, decision, request id, `reason`, `reason_code`, `execution_mode`, previous/new state when applicable, and redacted lineage.

#### Scenario: Denied action audit
WHEN a user is denied by policy
THEN the audit or evidence record includes the denied decision, `reason`, `reason_code`, and `execution_mode` without mutating the target.

#### Scenario: Release-blocked action audit
WHEN a protected action is blocked because live production auth is configured but unproven
THEN the audit or evidence record includes `decision=release_blocked`, `reason`, `reason_code`, `execution_mode=release_blocked`, removal criteria, and no target mutation.

#### Scenario: Secret-shaped values
WHEN request payloads, config, URI fields, or logs contain token/password/userinfo/query/fragment-shaped values
THEN emitted audit/evidence replaces them with redaction markers.

