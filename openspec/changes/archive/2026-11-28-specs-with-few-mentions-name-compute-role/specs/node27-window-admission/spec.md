## MODIFIED Requirements

### Requirement: Child qualification does not claim parent rollout completion

The two child deliveries SHALL preserve the original f24 five-round and final-review lineage and SHALL NOT reset or
disguise that review counter. Their qualification SHALL NOT execute operations of the compute role, retention, manual ledger/state
repair or production pin removal, or claim T0 and parent completion.

#### Scenario: Child evidence is ready for parent execution

- **GIVEN** isolated migration/rollback and old-pin/new-active handoff proofs plus read-only final-target audit and
  actual fresh prepare have passed
- **WHEN** the child merge gate is completed
- **THEN** parent #1987 still owns production window execution, immediate post-cutover owned unstage and full task 5.2
  evidence, and #2280 still requires actual uncached four-route validation
