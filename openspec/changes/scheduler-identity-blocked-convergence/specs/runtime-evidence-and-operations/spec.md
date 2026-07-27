# runtime-evidence-and-operations — delta for scheduler-identity-blocked-convergence

## ADDED Requirements

### Requirement: No-progress convergence facts SHALL be readable from scheduler evidence

Scheduler evidence SHALL expose, for each reserved-unbound reconcile outcome, the identity-blocked consecutive-pass counter and the `identity_mismatch_released` action when a release occurs, in both the full-fidelity `restart_reconcile` block and the bounded (size-limited) compaction of that block. The reconcile proof aggregation SHALL count a release as a reserved-status durable write. The budget-demoted decision `blocked_strict_warm_start_init_state_mismatch` SHALL remain readable from the bounded candidate summary through the existing `decision` key. These are per-job convergence facts; cross-reason no-progress aggregation and alerting remain out of scope (tracked separately).

#### Scenario: Convergence facts survive bounded-evidence compaction

- **WHEN** a pass records identity-blocked outcomes (or a release) and the evidence payload exceeds the size limit so the bounded compaction applies
- **THEN** the compact `restart_reconcile` outcome rows still carry the consecutive-pass counter and the release action, and demoted candidates still show `blocked_strict_warm_start_init_state_mismatch` under the `decision` key

## MODIFIED Requirements

### Requirement: Bounded evidence observability floor

When the scheduler pass evidence payload exceeds the configured size bound and the bounded fallback shape is emitted, the artifact SHALL preserve an operator-readable observability floor — the true computed pass status, per-candidate summary rows, and a compact restart-reconcile block — without weakening the fail-closed top-level status contract or the hard size bound.

#### Scenario: pre-limit status is preserved inside the limit block

- WHEN the evidence payload exceeds `max_evidence_bytes` and the bounded fallback payload is written
- THEN the top-level `status` remains `resource_limit_blocked` and `limit.reason` remains `evidence_size_limit_exceeded`
- AND `limit.pre_limit_status` records the pass status computed before the fallback (the key is omitted when the source payload carried no status)
- AND downstream consumers of the top-level status require no change.

#### Scenario: candidate lists degrade to bounded summaries before being dropped

- WHEN the bounded fallback payload is constructed
- THEN `candidates`, `blocked_candidates`, and `skipped_candidates` are populated row-for-row with fixed-key summary rows carrying candidate identity (including the readiness-reader identity keys `source_id`, `cycle_time_utc`, `scenario_id`, and for admitted candidates `run_id` and `forcing_version_id`), status, reason, and the incident-critical candidate state-evidence subset (scheduler decision, missing-forcing repair status, journal-predecessor quarantined skip reason), each value passed through from the already-redacted source payload with keys absent from a row when the source value is absent or null
- AND `limit.candidate_lists` is `summarized`
- AND only if the summarized payload still exceeds the bound does the existing droppable tier empty the lists, progressively in field order and stopping as soon as the payload fits, so a partial drop can leave the later lists as summaries
- AND `limit.candidate_lists` is set to `dropped` only when that tier empties a candidate list that still held rows; emptying an already-empty candidate list drops nothing and the marker stays `summarized`
- AND the artifact never exceeds `max_evidence_bytes`, and a payload that cannot fit even after all degradation tiers still fails closed with the existing write error.

#### Scenario: restart-reconcile incident evidence survives the fallback compactly

- WHEN the source evidence payload carries a `restart_reconcile` block and the bounded fallback payload is constructed
- THEN the fallback retains a compact `restart_reconcile` block exposing its status, `reserved_unbound_error`, `inflight_error`, and per-outcome summary rows limited to job identity, action, status, reconciliation reason class, `identity_blocked_streak`, `quarantine_reason`, and `quarantine_field`
- AND when the source payload has no `restart_reconcile` block the fallback omits the key.

#### Scenario: within-limit evidence is byte-identical to the pre-change contract

- WHEN the evidence payload fits within `max_evidence_bytes`
- THEN the artifact carries full candidate detail and contains neither `limit.pre_limit_status` nor `limit.candidate_lists`.

#### Scenario: terminal limit compaction remains the fail-closed floor

- WHEN even the summarized-and-dropped payload exceeds the bound and the existing terminal limit-compaction tier rewrites the `limit` block to its reason-only form
- THEN `limit.pre_limit_status` and `limit.candidate_lists` are permitted to disappear with the rest of the compacted `limit` block, preserving the pre-existing fail-closed behavior unchanged.
