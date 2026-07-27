# runtime-evidence-and-operations — delta for scheduler-identity-blocked-convergence

## ADDED Requirements

### Requirement: No-progress convergence facts SHALL be readable from scheduler evidence

Scheduler evidence SHALL expose, for each reserved-unbound reconcile outcome, the identity-blocked consecutive-pass counter and the `identity_mismatch_released` action when a release occurs, in both the full-fidelity `restart_reconcile` block and the bounded (size-limited) compaction of that block. The reconcile proof aggregation SHALL count a release as a reserved-status durable write. The budget-demoted decision `blocked_strict_warm_start_init_state_mismatch` SHALL remain readable from the bounded candidate summary through the existing `decision` key. These are per-job convergence facts; cross-reason no-progress aggregation and alerting remain out of scope (tracked separately).

#### Scenario: Convergence facts survive bounded-evidence compaction

- **WHEN** a pass records identity-blocked outcomes (or a release) and the evidence payload exceeds the size limit so the bounded compaction applies
- **THEN** the compact `restart_reconcile` outcome rows still carry the consecutive-pass counter and the release action, and demoted candidates still show `blocked_strict_warm_start_init_state_mismatch` under the `decision` key
