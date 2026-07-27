# runtime-evidence-and-operations — delta for scheduler-bounded-evidence-observability

## ADDED Requirements

### Requirement: Bounded evidence observability floor

When the scheduler pass evidence payload exceeds the configured size bound and the bounded fallback shape is emitted, the artifact SHALL preserve an operator-readable observability floor — the true computed pass status, per-candidate summary rows, and a compact restart-reconcile block — without weakening the fail-closed top-level status contract or the hard size bound.

#### Scenario: pre-limit status is preserved inside the limit block

- WHEN the evidence payload exceeds `max_evidence_bytes` and the bounded fallback payload is written
- THEN the top-level `status` remains `resource_limit_blocked` and `limit.reason` remains `evidence_size_limit_exceeded`
- AND `limit.pre_limit_status` records the pass status computed before the fallback (the key is omitted when the source payload carried no status)
- AND downstream consumers of the top-level status require no change.

#### Scenario: candidate lists degrade to bounded summaries before being dropped

- WHEN the bounded fallback payload is constructed
- THEN `candidates`, `blocked_candidates`, and `skipped_candidates` are populated row-for-row with fixed-key summary rows carrying candidate identity (including the readiness-reader identity keys `source_id`, `cycle_time_utc`, `scenario_id`, and for admitted candidates `run_id` and `forcing_version_id`), status, reason, and the incident-critical candidate state-evidence subset (missing-forcing repair status, journal-predecessor quarantined skip reason), each value passed through from the already-redacted source payload with keys absent from a row when the source value is absent or null
- AND `limit.candidate_lists` is `summarized`
- AND only if the summarized payload still exceeds the bound are the lists emptied by the existing droppable tier, with `limit.candidate_lists` set to `dropped`
- AND the artifact never exceeds `max_evidence_bytes`, and a payload that cannot fit even after all degradation tiers still fails closed with the existing write error.

#### Scenario: restart-reconcile incident evidence survives the fallback compactly

- WHEN the source evidence payload carries a `restart_reconcile` block and the bounded fallback payload is constructed
- THEN the fallback retains a compact `restart_reconcile` block exposing its status, `reserved_unbound_error`, and per-outcome summary rows limited to action, reason, `quarantine_reason`, and `quarantine_field`
- AND when the source payload has no `restart_reconcile` block the fallback omits the key.

#### Scenario: within-limit evidence is byte-identical to the pre-change contract

- WHEN the evidence payload fits within `max_evidence_bytes`
- THEN the artifact carries full candidate detail and contains neither `limit.pre_limit_status` nor `limit.candidate_lists`.

#### Scenario: terminal limit compaction remains the fail-closed floor

- WHEN even the summarized-and-dropped payload exceeds the bound and the existing terminal limit-compaction tier rewrites the `limit` block to its reason-only form
- THEN `limit.pre_limit_status` and `limit.candidate_lists` are permitted to disappear with the rest of the compacted `limit` block, preserving the pre-existing fail-closed behavior unchanged.
