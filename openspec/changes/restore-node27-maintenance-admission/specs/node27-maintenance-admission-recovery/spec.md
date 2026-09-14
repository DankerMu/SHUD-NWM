## ADDED Requirements

### Requirement: Explicitly authorized incident-only maintenance recovery

The incident recovery SHALL preserve original runtime, governance pin, formal configuration, historical receipts and fixed lifecycle lock while executing at most one bounded compression catch-up per approval. A failed or refused operation SHALL NOT count as successful recovery.

#### Scenario: Temporary budget exception

- **WHEN** the operator approves the #2349 incident exception and all safety checks pass
- **THEN** a private paired budget may launch only compression with bound1 and a matching finite outer wall, without cold activation or original env changes

#### Scenario: Unsafe or interrupted operation

- **WHEN** source, pin, path, timer, candidate or process safety checks fail, or a catch-up is partial or indeterminate
- **THEN** launch or readiness promotion is refused, evidence is preserved, and no automatic large-write retry or lock deletion occurs

### Requirement: Genuine original-service health precedes fresh admission

Recovery SHALL require fresh semantic success receipts and real success of both original maintenance services before a new prepare. The prepare SHALL preserve target415 and original active/pin authority and SHALL NOT execute T0 or production cutover.

#### Scenario: Fresh successful admission

- **WHEN** bounded catch-up and original maintenance services succeed, protected hashes match, and timer states are restored
- **THEN** prepare uses a new hash-bound input bundle and never-created state and its actual result is recorded

#### Scenario: Cosmetic service success

- **WHEN** the only evidence is reset-failed, stale receipt, refused_lock, or an old preparation result
- **THEN** recovery remains incomplete
