## MODIFIED Requirements

### Requirement: Bounded evidence observability floor

When the scheduler pass evidence payload exceeds the configured size bound and the bounded fallback shape is emitted, the artifact SHALL preserve an operator-readable observability floor — the true computed pass status, per-candidate summary rows, and a compact restart-reconcile block — without weakening the fail-closed top-level status contract or the hard size bound.

The restart-reconcile lane contract SHALL be covered by a payload-driven
property in addition to the exact-shape assertions: for every lane key of the
SOURCE payload's `restart_reconcile` block whose value carries an `outcomes`
sequence, the bounded artifact SHALL carry that lane with the same number of
outcome rows. The property SHALL derive its lane set from the source payload
and SHALL NOT read the compactor's lane constant, because the compactor
iterates that constant itself and a constant-driven property could never
observe a lane the constant omits — the very shape that let a lane be
discarded unnoticed. The exact-shape assertions SHALL be kept alongside the
property: they are what catches an EXTRA key leaking into the floor, which a
presence property cannot. Declared residual: a lane that exists only in the
producer and was never mirrored into the test payload remains invisible to
both; producer-to-fixture synchronisation is a separate obligation.

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
- AND the marker is monotone: once `limit.candidate_lists` is `dropped`, a later summarize pass SHALL NOT downgrade it back to `summarized` — empty candidate lists under a `dropped` marker mean rows were cut, and the marker keeps saying so
- AND the artifact never exceeds `max_evidence_bytes`, and a payload that cannot fit even after all degradation tiers still fails closed with the existing write error.

#### Scenario: restart-reconcile incident evidence survives the fallback compactly

- WHEN the source evidence payload carries a `restart_reconcile` block and the bounded fallback payload is constructed
- THEN the fallback retains a compact `restart_reconcile` block exposing its status, `reserved_unbound_error`, and `inflight_error`
- AND the fallback retains per-outcome summary rows for **both** reconcile lanes — `inflight` and `reserved_unbound` — each lane's rows limited to job identity, action, status, reconciliation reason class, `identity_blocked_streak`, `quarantine_reason`, and `quarantine_field`
- AND a lane absent from the source payload stays absent from the fallback, and a lane present without outcome rows SHALL NOT be given a fabricated empty `outcomes` list
- AND when the source payload has no `restart_reconcile` block the fallback omits the key.

#### Scenario: a dropped lane SHALL NOT be indistinguishable from an empty lane

- WHEN a bounded fallback artifact is read by an operator or an acceptance check asking whether a pass recorded any `identity_mismatch_blocked` or `identity_mismatch_released` outcome
- THEN the answer SHALL be derivable from the artifact, because the lane carrying those outcomes (`inflight` for jobs bound to a Slurm id, `reserved_unbound` for reserved unbound jobs) is present whenever the source payload carried it
- AND the artifact SHALL NOT present a syntactically valid `restart_reconcile` block whose missing lane reads as "no such outcomes occurred" when the lane was in fact discarded.

#### Scenario: every source lane with outcomes is present in the bounded block

- WHEN the shared incident test payload — which carries both `inflight` and `reserved_unbound` lanes with outcome rows — is passed through the bounded fallback at the tier that retains `restart_reconcile`
- THEN for every source lane key whose value carries an `outcomes` sequence, the bounded `restart_reconcile` block carries that key with an `outcomes` list of the same length
- AND the check enumerates the source payload's lane keys and does not consult the compactor's lane constant
- AND removing either lane's rebuild from the compactor makes this check fail while the compactor's own constant is unchanged
- AND a source lane that carries a `count` but no `outcomes` sequence is not demanded in the bounded block.

#### Scenario: within-limit evidence is byte-identical to the pre-change contract

- WHEN the evidence payload fits within `max_evidence_bytes`
- THEN the artifact carries full candidate detail and contains neither `limit.pre_limit_status` nor `limit.candidate_lists`.

#### Scenario: completed audit detail is summarized before the bounded fallback

- WHEN a normal pass payload exceeds `max_evidence_bytes` only because it repeats terminal skipped-candidate history or retention entry inventories
- THEN the writer MAY summarize terminal `skipped_candidates` to their bounded identity/reason rows and replace retention entry lists with their existing count/frontier summary
- AND non-terminal skipped candidates retain their complete evidence
- AND the pass status remains its computed non-blocked status, no `limit` block is introduced, and `evidence_compaction` records the normal-detail projection
- AND if that projection still exceeds the bound, the existing fail-closed bounded fallback applies unchanged.

#### Scenario: terminal limit compaction remains the fail-closed floor

- WHEN even the summarized-and-dropped payload exceeds the bound and the existing terminal limit-compaction tier rewrites the `limit` block to its reason-only form
- THEN `limit.pre_limit_status` and `limit.candidate_lists` are permitted to disappear with the rest of the compacted `limit` block, preserving the pre-existing fail-closed behavior unchanged.
