# file-state-snapshot-index (delta)

## ADDED Requirements

### Requirement: Predecessor-pending blocked evidence SHALL carry an operator self-heal signal

Blocked evidence for typed reason `state_snapshot_index_prior_checkpoint_missing_after_history` emitted by the §8 gate path (`registry_cutover_transition`-carrying emission in `scheduler_generation_gate.strict_warm_start_evidence`) SHALL carry `self_heal_expected` — true if and only if `state_history.latest_usable_state.valid_time` equals the evidence's `required_prior_cycle_time` (the emitted §8.6 predecessor's own exact warm-start state exists, so the single-level backfill closes the gap) — and `operator_action_required` (its negation);
when `operator_action_required` is true the evidence SHALL name the operator
action (`backfill_predecessor_state`) and the runbook path
`docs/runbooks/scheduler-dbfree-typed-reasons.md`. The `failure` block
(`retryable=True`, `permanent=False`) and the gate decision itself SHALL remain
unchanged. The legacy (pre-§8, checksum-less/no-declaration or untrusted-index)
emission site is out of scope: it returns passthrough `None` for the
no-earlier-history geometry and never emits a shape requiring the distinction.

#### Scenario: no-earlier-history geometry flags operator action

- **GIVEN** a candidate whose state index holds only entries strictly later
  than the candidate cycle (`state_history.history_exists=False`)
- **WHEN** the §8 gate emits
  `state_snapshot_index_prior_checkpoint_missing_after_history` blocked
  evidence
- **THEN** the evidence carries `operator_action_required=True`,
  `self_heal_expected=False`, `operator_action="backfill_predecessor_state"`
  and a runbook path, while `failure.retryable` stays `True`

#### Scenario: exact-predecessor-state geometry stays self-heal

- **GIVEN** a candidate whose state index holds a usable current-generation
  state exactly at `T − required_lead_hours` (the emitted predecessor's own
  warm-start state)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `self_heal_expected=True` and
  `operator_action_required=False`

#### Scenario: multi-cycle gap geometry flags operator action despite earlier history

- **GIVEN** a candidate whose only usable strictly-earlier state sits at
  `T − 2·required_lead_hours` or earlier (`state_history.history_exists=True`
  but `latest_usable_state.valid_time != required_prior_cycle_time`)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True` and
  `self_heal_expected=False` — a ≥2-cycle outage does not self-heal under
  single-level §8.6 backfill and must not be labeled as converging

#### Scenario: emitted predecessor reproduces the blocked shape under a real §8 gate

- **GIVEN** `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and a successor
  blocked with `block_predecessor_pending` in the no-earlier-history geometry
- **WHEN** `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the emitted predecessor is itself blocked with the same typed
  reason and `operator_action_required=True`, demonstrating the gap does not
  self-heal without operator backfill
