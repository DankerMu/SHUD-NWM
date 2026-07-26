# file-state-snapshot-index Specification

## Purpose
TBD - created by archiving change pin-env-override-state-lineage-blocks. Update Purpose after archive.
## Requirements
### Requirement: The warm-start env override SHALL NOT admit candidates blocked by state-lineage invariants

With `NHMS_REQUIRE_FORECAST_WARM_START=false` and a valid in-window cutover declaration, the §8 gate SHALL still block a candidate for which no checkpoint sits at the expected predecessor identity key, the index holds no usable entry at the candidate's `valid_time`, and the transition decision is `BLOCK_PREDECESSOR_PENDING` — whether the usable state-index history sits strictly earlier than or strictly later than the candidate cycle (typed reason `state_snapshot_index_prior_checkpoint_missing_after_history`) — and a candidate for which current-generation history exists and the checkpoint at the expected predecessor identity key — `valid_time` equal to the candidate cycle, producing `cycle_id` of cycle minus the source cadence, matching `lead_hours` — carries a different generation token (typed reason `state_snapshot_index_generation_mismatch`, transition decision `BLOCK_WRONG_GENERATION`); within these preconditions the env override never bypasses state-lineage blocks.

#### Scenario: Env override does not admit a missing predecessor

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid, in-window, and its `effective_cycle_utc` is
  strictly earlier than the candidate cycle, and usable state-index
  history exists strictly earlier than the candidate cycle (the gate's
  history signal is generation-agnostic) but holds no checkpoint at the
  expected predecessor identity key (a candidate at the declaration's
  effective cycle with old-generation-only history is instead admitted
  as declared cold start — see the sibling scenario "Old-generation
  checkpoints do not block declared cold start")
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`
- **AND** the recorded transition decision is `BLOCK_PREDECESSOR_PENDING`
- **AND** no candidate is admitted

#### Scenario: Env override does not admit a missing predecessor when no earlier usable history exists

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid, in-window, and its `effective_cycle_utc` is
  strictly earlier than the candidate cycle, current-generation usable
  state-index entries exist only at `valid_time` strictly later than the
  candidate cycle (so the gate's strictly-earlier history probe reports
  no history while the generation-scoped transition signal still sees
  current-generation history), and no checkpoint sits at the expected
  predecessor identity key
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`
- **AND** the recorded transition decision is `BLOCK_PREDECESSOR_PENDING`
- **AND** the blocked evidence records `state_history.history_exists = false`
- **AND** no candidate is admitted

#### Scenario: Env override does not admit a wrong-generation checkpoint

- **WHEN** `NHMS_REQUIRE_FORECAST_WARM_START=false`, the cutover
  declaration is valid and in-window, current-generation history exists,
  and the checkpoint at the expected predecessor identity key carries a
  generation token different from the candidate's (a wrong-generation
  checkpoint with NO current-generation history at a declaration's
  effective cycle is instead admitted as declared cold start — see the
  sibling scenario "Old-generation checkpoints do not block declared
  cold start")
- **THEN** the candidate is blocked with typed reason
  `state_snapshot_index_generation_mismatch`
- **AND** the recorded transition decision is `BLOCK_WRONG_GENERATION`
- **AND** no candidate is admitted

### Requirement: Completed-cycle skips SHALL be gated by journal-recorded predecessor identity

When readiness scoring would skip cycle T as already completed, the scheduler SHALL compare the `init_state_id` recorded on the COMPLETED hydro run's row of the journal entry against the expected predecessor identity token for T (computed from the candidate's source, model, cycle time, expected predecessor `cycle_id`, and required lead hours); when that recorded identity shares the expected token's base key (same source, model, and valid time) but carries a different lineage suffix, the scheduler SHALL treat T as not-canonical-ready without suppressing backfill selection and without mutating or deleting the journal entry, while a matching token, an absent or suffix-less recorded identity, a recorded identity with a different base key (including earlier-valid-time fallback warm-start states), or an identity recorded only on a non-completed (placeholder) hydro-run row superseded by a pipeline terminal SHALL preserve the existing skip behavior unchanged.

#### Scenario: Positive identity mismatch quarantines the completed entry

- **WHEN** the journal holds a completed cycle-T entry whose non-empty
  recorded `init_state_id` shares the expected predecessor token's base key
  (same source, model, and valid time T) but carries a different lineage
  suffix (wrong predecessor cycle or lead)
- **THEN** T is not reported as complete by readiness scoring
- **AND** T remains eligible for backfill selection
- **AND** the journal entry's on-disk content is byte-identical after the
  scoring pass (immutable audit entry)

#### Scenario: Matching identity preserves the completed skip

- **WHEN** the completed cycle-T entry's recorded `init_state_id` equals the
  expected predecessor identity token
- **THEN** T is skipped as completed exactly as before this change

#### Scenario: Absent or suffix-less recorded identity preserves legacy behavior

- **WHEN** the completed cycle-T entry records no `init_state_id`, or records
  a suffix-less legacy identity equal to the expected token's base key
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

#### Scenario: Superseded placeholder hydro-run row is not judged

- **WHEN** the completed cycle-T entry's completion is decided by a pipeline
  terminal while its hydro-run row is a non-completed placeholder
  (`created`/`staged`/`submitted`) carrying a recorded `init_state_id` —
  such as under the `forecast_state_save_qc` terminal mode
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

#### Scenario: Fallback warm start with a different base key is not quarantined

- **WHEN** the completed cycle-T entry's recorded `init_state_id` carries a
  different base key than the expected token — such as an earlier-valid-time
  fallback warm-start state legally selected under
  `NHMS_REQUIRE_FORECAST_WARM_START=false`
- **THEN** no quarantine judgement is made and T is skipped as completed
  exactly as before this change

