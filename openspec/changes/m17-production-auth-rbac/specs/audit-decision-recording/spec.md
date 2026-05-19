## ADDED Requirements

### Requirement: Audit Decision Recording
Allowed, denied, and release-blocked protected actions SHALL produce redacted audit/evidence records suitable for operations review.

#### Scenario: Allowed action audit
WHEN an authorized user performs a protected action
THEN the audit record includes actor, `roles[]`, action id, target, decision, request id, previous/new state when applicable, and redacted lineage.

#### Scenario: Denied action audit
WHEN a user is denied by policy
THEN the audit or evidence record includes the denied decision and reason without mutating the target.

#### Scenario: Secret-shaped values
WHEN request payloads, config, URI fields, or logs contain token/password/userinfo/query/fragment-shaped values
THEN emitted audit/evidence replaces them with redaction markers.
