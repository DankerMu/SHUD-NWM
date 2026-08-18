# file-state-snapshot-index (delta)

## ADDED Requirements

### Requirement: Predecessor-pending blocked evidence SHALL carry an operator self-heal signal

Blocked evidence for typed reason `state_snapshot_index_prior_checkpoint_missing_after_history` emitted by the §8 gate path (`registry_cutover_transition`-carrying emission in `scheduler_generation_gate.strict_warm_start_evidence`) SHALL carry `self_heal_expected` — true if and only if the emitted §8.6 predecessor's own exact warm-start verification succeeds, computed by the same provider verification the predecessor's own gate would run (`strict_warm_start_evidence` at `valid_time = required_prior_cycle_time` with the candidate's package checksum and lead hours, requiring `ready=True`) so that identity, generation/lineage, `usable_flag`, and state-object availability/content are all covered — and `operator_action_required` (its negation). Any verification shortfall — wrong-generation entry at the slot, missing/corrupt state object, absent entry, malformed evidence — SHALL resolve to `operator_action_required=True` (fail toward escalation, never toward false reassurance). The signal is single-level by definition: it answers "will the single-level §8.6 backfill close THIS candidate's gap", and operator triage reads the discovered successor's record; a deeper chain stall is signaled by the successor's `operator_action_required=True`, not by fields on emitted-predecessor records. The evidence SHALL also carry a compact `self_heal_probe` sub-object recording the probe's `ready` and typed `reason` so operators can see why self-heal was ruled out.
When `operator_action_required` is true the evidence SHALL name the operator
action (`backfill_predecessor_state`) and the runbook path
`docs/runbooks/scheduler-dbfree-typed-reasons.md`. The `failure` block
(`retryable=True`, `permanent=False`) and the gate decision itself SHALL remain
unchanged. The legacy (pre-§8, checksum-less/no-declaration or untrusted-index)
emission site is out of scope: it returns passthrough `None` for the
no-earlier-history geometry; on the `history_exists=True` geometry it emits the
typed reason WITHOUT the signal fields, and operators fall back to the manual
criteria documented in the runbook (field absence is not a self-heal guarantee).
`operator_action_required` SHALL survive the bounded-evidence summarization
tier (retained in `_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS`) so the runbook's
single-boolean triage remains executable on summarized passes.

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

#### Scenario: wrong-generation entry at the predecessor slot flags operator action

- **GIVEN** a candidate whose state index holds an old-generation `usable_flag=true`
  entry exactly at `T − required_lead_hours` (current-generation history sits
  elsewhere or nowhere)
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True` and
  `self_heal_expected=False` — the emitted predecessor would block on
  `state_snapshot_index_generation_mismatch` and never run, and a
  wrong-generation entry at the slot never diverts the successor's own typed
  reason

#### Scenario: missing state object at the predecessor slot flags operator action

- **GIVEN** a candidate whose state index holds a current-generation
  `usable_flag=true` entry exactly at `T − required_lead_hours` whose state
  object is absent from the object store
- **WHEN** the same typed reason is emitted
- **THEN** the evidence carries `operator_action_required=True`,
  `self_heal_expected=False`, and a `self_heal_probe.reason` of
  `state_snapshot_index_object_missing`

#### Scenario: emitted predecessor reproduces the blocked shape under a real §8 gate

- **GIVEN** `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and a successor
  blocked with `block_predecessor_pending` in the no-earlier-history geometry
- **WHEN** `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the emitted predecessor is itself blocked with the same typed
  reason and `operator_action_required=True`, demonstrating the gap does not
  self-heal without operator backfill

#### Scenario: emitted predecessor is admitted under a real §8 gate in the self-heal geometry

- **GIVEN** `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and a successor
  blocked with the same typed reason whose state index holds the emitted
  predecessor's own verified warm-start state (`self_heal_expected=True`,
  `self_heal_probe.ready=True`)
- **WHEN** `emit_predecessor_candidates` runs against the real §8 gate
- **THEN** the emitted predecessor is ADMITTED (present in the candidate
  list, absent from blocked) and the successor's emission record carries
  `status="emitted"` — the "stand down" half of the signal is backed by the
  machinery, not only by field values
